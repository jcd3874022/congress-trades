"""SEC EDGAR Form 4 (insider transactions) scraper.

Sources: sec.gov company_tickers.json (ticker->CIK map), data.sec.gov
submissions API (per-issuer filing index), and the Form 4 XML inside each
filing on sec.gov/Archives. No API key; SEC fair-use policy requires a
User-Agent with contact info (config: edgar_user_agent) and <=10 req/s.

Scope: rather than firehosing all Form 4s, we track the tickers that appear
in recent congressional trades (most recent first, capped by
edgar_max_tickers) plus any edgar_extra_tickers - the point is
cross-referencing insider activity against congressional consolidation.
Transaction codes: P = open-market purchase, S = open-market sale.
"""
import datetime as dt
import logging
import time
import xml.etree.ElementTree as ET

import requests

from db import get_conn
from scraper.common import finish_run, start_run

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}"
log = logging.getLogger("edgar")


def _text(el, path):
    node = el.find(path)
    return node.text.strip() if node is not None and node.text else None


def _num(el, path):
    v = _text(el, path)
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None


def _tracked_tickers(cfg):
    max_t = int(cfg.get("edgar_max_tickers", "150"))
    lookback = int(cfg.get("edgar_lookback_days", "90"))
    extra = [t.strip().upper() for t in cfg.get("edgar_extra_tickers", "").split(",") if t.strip()]
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """select t.ticker, max(coalesce(t.transaction_date, f.filed_date)) as last_tx\n                   from congress_trades t join congress_filings f on f.id = t.filing_id\n                   where t.ticker is not null\n                     and coalesce(t.transaction_date, f.filed_date) >= current_date - %s\n                   group by t.ticker order by last_tx desc limit %s""",
                (lookback, max_t),
            )
            tickers = [r["ticker"].upper() for r in cur.fetchall()]
    for t in extra:
        if t not in tickers:
            tickers.append(t)
    return tickers


def _parse_form4_xml(xml_bytes):
    root = ET.fromstring(xml_bytes)
    owner = root.find(".//reportingOwner")
    insider_name = _text(owner, ".//rptOwnerName") if owner is not None else None
    rel = owner.find(".//reportingOwnerRelationship") if owner is not None else None
    is_director = (_text(rel, "isDirector") in ("1", "true")) if rel is not None else None
    is_officer = (_text(rel, "isOfficer") in ("1", "true")) if rel is not None else None
    title = _text(rel, "officerTitle") if rel is not None else None
    trades = []
    for tx in root.findall(".//nonDerivativeTransaction"):
        code = _text(tx, ".//transactionCoding/transactionCode")
        shares = _num(tx, ".//transactionAmounts/transactionShares/value")
        price = _num(tx, ".//transactionAmounts/transactionPricePerShare/value")
        trades.append(
            {
                "transaction_date": _text(tx, ".//transactionDate/value"),
                "transaction_code": code,
                "acquired_disposed": _text(tx, ".//transactionAcquiredDisposedCode/value"),
                "shares": shares,
                "price": price,
                "value": (shares * price) if shares is not None and price is not None else None,
                "security_title": _text(tx, ".//securityTitle/value"),
            }
        )
    return insider_name, title, is_director, is_officer, trades


def run(cfg):
    run_id = start_run("edgar")
    new_filings, new_trades, errors = 0, 0, []
    parser_version = cfg.get("parser_version", "unknown")
    delay = float(cfg.get("request_delay_seconds", "0.5"))
    lookback = int(cfg.get("edgar_lookback_days", "90"))
    cutoff = (dt.date.today() - dt.timedelta(days=lookback)).isoformat()
    s = requests.Session()
    s.headers.update({"User-Agent": cfg.get("edgar_user_agent", "congress-trades/1.0")})
    try:
        tickers = _tracked_tickers(cfg)
        r = s.get(TICKER_MAP_URL, timeout=60)
        r.raise_for_status()
        cik_by_ticker = {v["ticker"].upper(): str(v["cik_str"]) for v in r.json().values()}
        for ticker in tickers:
            cik = cik_by_ticker.get(ticker)
            if not cik:
                continue
            try:
                time.sleep(delay)
                sub = s.get(SUBMISSIONS_URL.format(cik10=cik.zfill(10)), timeout=60)
                sub.raise_for_status()
                recent = sub.json().get("filings", {}).get("recent", {})
                rows = zip(
                    recent.get("form", []),
                    recent.get("accessionNumber", []),
                    recent.get("filingDate", []),
                )
                for form, acc, fdate in rows:
                    if form not in ("4", "4/A") or fdate < cutoff:
                        continue
                    acc_nodash = acc.replace("-", "")
                    base = ARCHIVE_URL.format(cik=cik, acc=acc_nodash)
                    with get_conn() as conn:
                        with conn.cursor() as cur:
                            cur.execute(
                                """insert into edgar_form4\n                                     (cik, ticker, accession, form_type, filed_date,\n                                      source_url, parse_status, scrape_run_id)\n                                   values (%s,%s,%s,%s,%s,%s,'pending',%s)\n                                   on conflict (accession) do nothing returning id""",
                                (cik, ticker, acc, form, fdate, base, run_id),
                            )
                            got = cur.fetchone()
                    if not got:
                        continue
                    form4_id = got["id"]
                    new_filings += 1
                    try:
                        time.sleep(delay)
                        idx = s.get(base + "/index.json", timeout=60)
                        idx.raise_for_status()
                        items = idx.json().get("directory", {}).get("item", [])
                        xml_name = next(
                            (i["name"] for i in items
                             if i["name"].lower().endswith(".xml") and "xsl" not in i["name"].lower()),
                            None,
                        )
                        if not xml_name:
                            raise ValueError("no form4 xml in filing index")
                        time.sleep(delay)
                        xml = s.get(f"{base}/{xml_name}", timeout=60)
                        xml.raise_for_status()
                        name, title, isdir, isoff, trades = _parse_form4_xml(xml.content)
                        with get_conn() as conn:
                            with conn.cursor() as cur:
                                cur.execute(
                                    """update edgar_form4 set insider_name=%s, insider_title=%s,\n                                       is_director=%s, is_officer=%s, parse_status='parsed'\n                                       where id=%s""",
                                    (name, title, isdir, isoff, form4_id),
                                )
                                for t in trades:
                                    cur.execute(
                                        """insert into edgar_trades\n                                             (form4_id, transaction_date, transaction_code,\n                                              acquired_disposed, shares, price, value,\n                                              security_title, source_url, parser_version)\n                                           values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                                        (
                                            form4_id, t["transaction_date"], t["transaction_code"],
                                            t["acquired_disposed"], t["shares"], t["price"],
                                            t["value"], t["security_title"],
                                            f"{base}/{xml_name}", parser_version,
                                        ),
                                    )
                                    new_trades += 1
                    except Exception as e:
                        errors.append(f"{ticker} {acc}: {e}")
                        with get_conn() as conn:
                            with conn.cursor() as cur:
                                cur.execute(
                                    "update edgar_form4 set parse_status='unparseable', parse_error=%s where id=%s",
                                    (str(e)[:500], form4_id),
                                )
            except Exception as e:
                log.exception("edgar ticker failed: %s", ticker)
                errors.append(f"{ticker}: {e}")
        finish_run(run_id, new_filings, new_trades, errors, "ok")
    except Exception as e:
        log.exception("edgar run failed")
        errors.append(str(e))
        finish_run(run_id, new_filings, new_trades, errors, "failed")
    return {"new_filings": new_filings, "new_trades": new_trades, "errors": errors}
