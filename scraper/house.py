"""House Clerk scraper.

Source: https://disclosures-clerk.house.gov. The Clerk publishes a yearly ZIP
containing an XML index of all financial disclosure filings; FilingType 'P' is
a Periodic Transaction Report. PTR PDFs live at a predictable URL by DocID.

Parser (v1.1, validated against real filings): House PTR tables wrap rows
across physical lines - amount ranges split ('$15,001 -' / '$50,000'), asset
names continue below with the ticker, and some rows lead with a numeric
transaction ID instead of an owner code. We therefore group lines into logical
records first, then extract fields from the joined record. Garbled small-caps
section headers (rendered with NUL bytes) act as record boundaries; the
'Filing Status'/'Description' values after the colon survive cleanly and are
attached to the preceding transaction as comment metadata.
Scanned/paper filings yield no text and are recorded + linked, paper_skipped.
"""
import datetime as dt
import io
import logging
import re
import time
import xml.etree.ElementTree as ET
import zipfile

import pdfplumber
import requests

from db import get_conn
from scraper.common import finish_run, insert_trades, start_run

CLERK = "https://disclosures-clerk.house.gov/public_disc"
ZIP_URL = CLERK + "/financial-pdfs/{year}FD.zip"
PDF_URL = CLERK + "/ptr-pdfs/{year}/{doc_id}.pdf"
UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
}
log = logging.getLogger("house")

REC_START = re.compile(r"^(?:\d{6,}\b|(?:SP|DC|JT)\b)")
# some rows start directly with the asset (no owner/id); type + both dates
# always sit on the first physical line of a record, so that also opens one
TX_HINT = re.compile(r"\b(?:P|S\s*\(partial\)|S|E)\s+\d{2}/\d{2}/\d{4}\s+\d{2}/\d{2}/\d{4}")
CORE_RE = re.compile(
    r"^(?:(?P<txid>\d{6,})\s+)?(?:(?P<owner>SP|DC|JT)\s+)?(?P<pre>.+?)\s+"
    r"(?P<ttype>P|S\s*\(partial\)|S|E)\s+"
    r"(?P<tdate>\d{2}/\d{2}/\d{4})\s+(?P<ndate>\d{2}/\d{2}/\d{4})\s*(?P<rest>.*)$"
)
DOLLAR_RE = re.compile(r"\$[\d,]+")
OVER_RE = re.compile(r"Over\s+\$[\d,]+", re.I)
TICKER_RE = re.compile(r"\(([A-Z][A-Z0-9.\-]{0,9})\)")
ATYPE_RE = re.compile(r"\[([A-Z]{2})\]")
SKIP_SUBSTR = (
    "ID Owner Asset",
    "Type Date Gains",
    "* For the complete list",
    "Digitally Signed",
    "Clerk of the House",
    "Filing ID #",
    "Name:",
    "Status:",
    "State/District:",
)


def _mmddyyyy(raw):
    try:
        return dt.datetime.strptime((raw or "").strip(), "%m/%d/%Y").date()
    except ValueError:
        return None


def _index(session, year):
    r = session.get(ZIP_URL.format(year=year), timeout=180)
    r.raise_for_status()
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    xml_name = next(n for n in zf.namelist() if n.lower().endswith(".xml"))
    root = ET.fromstring(zf.read(xml_name))
    out = []
    for m in root.iter("Member"):
        def get(tag):
            return (m.findtext(tag) or "").strip()
        out.append(
            {
                "last": get("Last"),
                "first": get("First"),
                "filing_type": get("FilingType"),
                "state_dst": get("StateDst"),
                "filing_date": get("FilingDate"),
                "doc_id": get("DocID"),
                "year": get("Year") or str(year),
            }
        )
    return out


def _group_records(text):
    """Group physical lines into logical transaction records + attach metadata."""
    records, cur = [], None

    def flush():
        nonlocal cur
        if cur:
            records.append(cur)
            cur = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line in (">", "$200?"):
            continue
        if "\x00" in line:
            # garbled small-caps header = section/metadata boundary
            flush()
            if ":" in line and records:
                label_char = line[0].upper()
                val = line.split(":", 1)[1].replace("\x00", " ").strip()
                if val:
                    if label_char == "F":
                        records[-1].setdefault("meta", []).append(f"Filing status: {val}")
                    elif label_char == "D":
                        records[-1].setdefault("meta", []).append(val)
            continue
        if any(s in line for s in SKIP_SUBSTR):
            continue
        if REC_START.match(line) or TX_HINT.search(line):
            flush()
            cur = {"lines": [line]}
        elif cur:
            cur["lines"].append(line)
    flush()
    return records


def _parse_pdf(content):
    """Returns (trades, status). status: parsed | partial | unparseable | paper_skipped."""
    text_parts = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            text_parts.append(page.extract_text() or "")
    text = "\n".join(text_parts)
    if not text.strip():
        return [], "paper_skipped"

    records = _group_records(text)
    trades, misses = [], 0
    for rec in records:
        joined = " ".join(rec["lines"])
        m = CORE_RE.match(joined)
        if not m:
            misses += 1
            continue
        rest = m.group("rest") or ""
        over = OVER_RE.search(rest)
        dollars = DOLLAR_RE.findall(rest)
        if over:
            amount_range = over.group(0)
        elif len(dollars) >= 2:
            # wrap-safe: low/high are the first two $ tokens after the dates,
            # even when wrapped asset text interleaves between them
            amount_range = f"{dollars[0]} - {dollars[1]}"
        elif dollars:
            amount_range = dollars[0]
        else:
            misses += 1
            continue
        rest_asset = DOLLAR_RE.sub(" ", OVER_RE.sub(" ", rest)).replace(" - ", " ")
        asset = re.sub(r"\s+", " ", (m.group("pre") + " " + rest_asset)).strip(" -")
        tick = TICKER_RE.search(asset)
        atype = ATYPE_RE.search(asset)
        trades.append(
            {
                "transaction_date": _mmddyyyy(m.group("tdate")),
                "notification_date": _mmddyyyy(m.group("ndate")),
                "owner": m.group("owner"),
                "ticker": tick.group(1) if tick else None,
                "asset_name": asset,
                "asset_type": atype.group(1) if atype else None,
                "transaction_type": re.sub(r"\s+", " ", m.group("ttype")),
                "amount_range": amount_range,
                "comment": "; ".join(rec.get("meta", [])) or None,
            }
        )
    if trades and misses == 0:
        return trades, "parsed"
    if trades:
        return trades, "partial"
    return [], "unparseable"


def run(cfg):
    run_id = start_run("house")
    new_filings, new_trades, errors = 0, 0, []
    parser_version = cfg.get("parser_version", "unknown")
    delay = float(cfg.get("request_delay_seconds", "0.5"))
    max_pdfs = int(cfg.get("house_max_pdfs_per_run", "150"))
    years = [y.strip() for y in cfg.get("house_years", "").split(",") if y.strip()]
    s = requests.Session()
    s.headers.update(UA)
    try:
        # 1) sync the filing index (cheap; inserts new PTRs as 'pending')
        for year in years:
            for rec in _index(s, year):
                if rec["filing_type"] != "P" or not rec["doc_id"]:
                    continue
                url = PDF_URL.format(year=rec["year"], doc_id=rec["doc_id"])
                with get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """insert into congress_filings\n                                 (source, doc_id, filer_name, filer_state,\n                                  filing_type, filed_date, source_url,\n                                  parse_status, scrape_run_id)\n                               values ('house', %s, %s, %s, 'PTR', %s, %s,\n                                       'pending', %s)\n                               on conflict (source, doc_id) do nothing\n                               returning id""",
                            (
                                rec["doc_id"],
                                f"{rec['first']} {rec['last']}".strip(),
                                rec["state_dst"] or None,
                                _mmddyyyy(rec["filing_date"]),
                                url,
                                run_id,
                            ),
                        )
                        if cur.fetchone():
                            new_filings += 1
        # 2) work the pending PDF queue, newest first, capped per run
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """select id, doc_id, source_url from congress_filings\n                       where source='house' and parse_status='pending'\n                       order by filed_date desc nulls last limit %s""",
                    (max_pdfs,),
                )
                pending = cur.fetchall()
        for f in pending:
            try:
                time.sleep(delay)
                r = s.get(f["source_url"], timeout=120)
                if r.status_code == 404:
                    status, trades = "unparseable", []
                else:
                    r.raise_for_status()
                    trades, status = _parse_pdf(r.content)
                with get_conn() as conn:
                    with conn.cursor() as cur:
                        n = insert_trades(cur, f["id"], f["source_url"], parser_version, trades)
                        new_trades += n
                        cur.execute(
                            """update congress_filings\n                               set parse_status=%s, is_paper=%s, parse_error=null where id=%s""",
                            (status, status == "paper_skipped", f["id"]),
                        )
            except Exception as e:
                log.exception("house pdf failed: %s", f["doc_id"])
                errors.append(f"{f['doc_id']}: {e}")
                with get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "update congress_filings set parse_status='unparseable', parse_error=%s where id=%s",
                            (str(e)[:500], f["id"]),
                        )
        finish_run(run_id, new_filings, new_trades, errors, "ok")
    except Exception as e:
        log.exception("house run failed")
        errors.append(str(e))
        finish_run(run_id, new_filings, new_trades, errors, "failed")
    return {"new_filings": new_filings, "new_trades": new_trades, "errors": errors}
