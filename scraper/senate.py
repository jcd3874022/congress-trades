"""Senate eFD scraper.

Source: https://efdsearch.senate.gov (official Senate Electronic Financial
Disclosure system). Flow: accept the access agreement to establish a session,
query the PTR report index (report type 11 = Periodic Transaction Report),
then parse each electronic PTR's HTML transaction table. Paper/scanned filings
are recorded and linked but marked paper_skipped (no machine-readable text).
"""
import datetime as dt
import logging
import re
import time

import requests
from bs4 import BeautifulSoup

from db import get_conn
from scraper.common import finish_run, insert_trades, start_run

ROOT = "https://efdsearch.senate.gov"
UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
}
HREF_RE = re.compile(r'href="([^"]+)"')
log = logging.getLogger("senate")


def _session():
    s = requests.Session()
    s.headers.update(UA)
    s.get(f"{ROOT}/search/home/", timeout=30)
    csrf = s.cookies.get("csrftoken", "")
    s.post(
        f"{ROOT}/search/home/",
        data={"prohibition_agreement": "1", "csrfmiddlewaretoken": csrf},
        headers={"Referer": f"{ROOT}/search/home/"},
        timeout=30,
    )
    return s


def _index_rows(s, lookback_days):
    csrf = s.cookies.get("csrftoken", "")
    since = (dt.date.today() - dt.timedelta(days=lookback_days)).strftime("%m/%d/%Y")
    start, length, rows = 0, 100, []
    while True:
        payload = {
            "start": str(start),
            "length": str(length),
            "report_types": "[11]",
            "filer_types": "[]",
            "submitted_start_date": f"{since} 00:00:00",
            "submitted_end_date": "",
            "candidate_state": "",
            "senator_state": "",
            "office_id": "",
            "first_name": "",
            "last_name": "",
        }
        r = s.post(
            f"{ROOT}/search/report/data/",
            data=payload,
            headers={"Referer": f"{ROOT}/search/", "X-CSRFToken": csrf},
            timeout=60,
        )
        r.raise_for_status()
        batch = r.json().get("data", [])
        rows.extend(batch)
        if len(batch) < length:
            break
        start += length
    return rows


def _parse_date(raw):
    raw = (raw or "").strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%Y %H:%M:%S"):
        try:
            return dt.datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _parse_ptr_page(html):
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    trades = []
    if not table:
        return trades
    for tr in table.find_all("tr"):
        cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        if len(cells) < 8:
            continue
        # columns: #, Transaction Date, Owner, Ticker, Asset Name, Asset Type,
        # Type, Amount, Comment
        ticker = cells[3]
        trades.append(
            {
                "transaction_date": _parse_date(cells[1]),
                "owner": cells[2] or None,
                "ticker": None if ticker in ("--", "") else ticker,
                "asset_name": cells[4] or None,
                "asset_type": cells[5] or None,
                "transaction_type": cells[6] or None,
                "amount_range": cells[7] or None,
                "comment": cells[8] if len(cells) > 8 else None,
            }
        )
    return trades


def run(cfg):
    run_id = start_run("senate")
    new_filings, new_trades, errors = 0, 0, []
    parser_version = cfg.get("parser_version", "unknown")
    delay = float(cfg.get("request_delay_seconds", "0.5"))
    try:
        s = _session()
        rows = _index_rows(s, int(cfg.get("senate_lookback_days", "90")))
        log.info("senate index rows: %d", len(rows))
        for row in rows:
            try:
                first, last, office, link_html, date_str = row[:5]
                href_m = HREF_RE.search(link_html or "")
                if not href_m:
                    continue
                href = href_m.group(1)
                url = href if href.startswith("http") else ROOT + href
                doc_id = href.rstrip("/").split("/")[-1]
                is_paper = "/search/view/ptr/" not in href
                filing_type_text = BeautifulSoup(link_html, "html.parser").get_text(" ", strip=True)
                with get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """insert into congress_filings\n                                 (source, doc_id, filer_name, chamber_office,\n                                  filing_type, filed_date, source_url, is_paper,\n                                  parse_status, scrape_run_id)\n                               values ('senate', %s, %s, %s, %s, %s, %s, %s,\n                                       'pending', %s)\n                               on conflict (source, doc_id) do nothing\n                               returning id""",
                            (
                                doc_id,
                                f"{first.strip()} {last.strip()}".strip(),
                                (office or "").strip() or None,
                                filing_type_text or "PTR",
                                _parse_date(date_str),
                                url,
                                is_paper,
                                run_id,
                            ),
                        )
                        got = cur.fetchone()
                        if not got:
                            continue  # already ingested
                        filing_id = got["id"]
                        new_filings += 1
                        if is_paper:
                            cur.execute(
                                "update congress_filings set parse_status='paper_skipped' where id=%s",
                                (filing_id,),
                            )
                            continue
                        time.sleep(delay)
                        page = s.get(url, timeout=60)
                        page.raise_for_status()
                        trades = _parse_ptr_page(page.text)
                        n = insert_trades(cur, filing_id, url, parser_version, trades)
                        new_trades += n
                        cur.execute(
                            "update congress_filings set parse_status=%s where id=%s",
                            ("parsed" if n else "unparseable", filing_id),
                        )
            except Exception as e:  # keep going per-filing
                log.exception("senate filing failed")
                errors.append(str(e))
        finish_run(run_id, new_filings, new_trades, errors, "ok" if not errors else "ok")
    except Exception as e:
        log.exception("senate run failed")
        errors.append(str(e))
        finish_run(run_id, new_filings, new_trades, errors, "failed")
    return {"new_filings": new_filings, "new_trades": new_trades, "errors": errors}
