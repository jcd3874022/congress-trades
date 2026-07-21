"""House Clerk scraper.

Source: https://disclosures-clerk.house.gov. The Clerk publishes a yearly ZIP
containing an XML index of all financial disclosure filings; FilingType 'P' is
a Periodic Transaction Report. PTR PDFs live at a predictable URL by DocID.
Electronic PTRs contain extractable text tables; scanned/paper filings yield
no text and are recorded + linked but marked paper_skipped.
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

# One transaction line in an electronic House PTR, e.g.:
# 'SP Apple Inc (AAPL) [ST] P 01/02/2026 01/05/2026 $1,001 - $15,000'
LINE_RE = re.compile(
    r"^(?:(?P<owner>SP|DC|JT)\s+)?"
    r"(?P<asset>.+?)\s+"
    r"(?P<ttype>P|S\s*\(partial\)|S|E)\s+"
    r"(?P<tdate>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<ndate>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<amount>\$[\d,]+\s*-\s*\$[\d,]+|Over\s+\$[\d,]+)"
)
TICKER_RE = re.compile(r"\(([A-Z][A-Z0-9.\-]{0,9})\)")


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


def _parse_pdf(content):
    """Returns (trades, status). status: parsed | partial | unparseable | paper_skipped."""
    text_parts = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            text_parts.append(page.extract_text() or "")
    text = "\n".join(text_parts)
    if not text.strip():
        return [], "paper_skipped"
    trades, misses = [], 0
    for line in text.splitlines():
        line = line.strip()
        m = LINE_RE.match(line)
        if not m:
            if "$" in line and re.search(r"\d{2}/\d{2}/\d{4}", line):
                misses += 1  # looked like a transaction line but didn't parse
            continue
        asset = m.group("asset").strip()
        tick = TICKER_RE.search(asset)
        trades.append(
            {
                "transaction_date": _mmddyyyy(m.group("tdate")),
                "notification_date": _mmddyyyy(m.group("ndate")),
                "owner": m.group("owner"),
                "ticker": tick.group(1) if tick else None,
                "asset_name": asset,
                "transaction_type": m.group("ttype"),
                "amount_range": m.group("amount"),
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
                            """update congress_filings\n                               set parse_status=%s, is_paper=%s where id=%s""",
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
