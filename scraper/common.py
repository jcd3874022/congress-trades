"""Shared helpers: config, run logging, statutory amount-range parsing.

Amount ranges are the disclosure categories printed on the filings themselves
(e.g. '$1,001 - $15,000', 'Over $50,000,000'). We parse bounds numerically from
the raw string rather than hardcoding a lookup table, and always store the raw
string alongside the parsed bounds for provenance.
"""
import json
import re

from db import get_conn

AMOUNT_RANGE_RE = re.compile(r"\$?\s*([\d,]+)\s*-\s*\$?\s*([\d,]+)")
AMOUNT_OVER_RE = re.compile(r"over\s*\$?\s*([\d,]+)", re.IGNORECASE)


def load_config():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("select key, value from congress_config")
            return {r["key"]: r["value"] for r in cur.fetchall()}


def parse_amount_range(raw):
    if not raw:
        return None, None
    m = AMOUNT_RANGE_RE.search(raw)
    if m:
        return (
            float(m.group(1).replace(",", "")),
            float(m.group(2).replace(",", "")),
        )
    m = AMOUNT_OVER_RE.search(raw)
    if m:
        return float(m.group(1).replace(",", "")), None
    return None, None


def start_run(source):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "insert into congress_scrape_runs (source) values (%s) returning id",
                (source,),
            )
            return cur.fetchone()["id"]


def finish_run(run_id, new_filings, new_trades, errors, status):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """update congress_scrape_runs\n                   set finished_at = now(), new_filings = %s, new_trades = %s,\n                       errors = %s, status = %s\n                   where id = %s""",
                (new_filings, new_trades, json.dumps(errors), status, run_id),
            )


def insert_trades(cur, filing_id, source_url, parser_version, trades):
    n = 0
    for t in trades:
        low, high = parse_amount_range(t.get("amount_range"))
        cur.execute(
            """insert into congress_trades\n                 (filing_id, transaction_date, notification_date, owner, ticker,\n                  asset_name, asset_type, transaction_type, amount_range,\n                  amount_low, amount_high, comment, source_url, parser_version)\n               values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                filing_id,
                t.get("transaction_date"),
                t.get("notification_date"),
                t.get("owner"),
                t.get("ticker"),
                t.get("asset_name"),
                t.get("asset_type"),
                t.get("transaction_type"),
                t.get("amount_range"),
                low,
                high,
                t.get("comment"),
                source_url,
                parser_version,
            ),
        )
        n += 1
    return n
