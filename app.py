import logging
import os
import threading
import time

from flask import Flask, jsonify, render_template, request

from db import get_conn
from scraper import edgar, house, oge, senate
from scraper.common import load_config

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")
app = Flask(__name__)
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
_scrape_lock = threading.Lock()


SOURCES = {"senate": senate, "house": house, "edgar": edgar, "oge": oge}


def scrape_all(sources=None):
    if not _scrape_lock.acquire(blocking=False):
        return {"skipped": "scrape already running"}
    try:
        cfg = load_config()
        out = {}
        for name in (sources or list(SOURCES)):
            mod = SOURCES.get(name)
            if mod:
                out[name] = mod.run(cfg)
        return out
    finally:
        _scrape_lock.release()


def _scheduler():
    time.sleep(60)
    while True:
        try:
            scrape_all()
        except Exception:
            log.exception("scheduled scrape failed")
        try:
            hours = float(load_config().get("scrape_interval_hours", "6"))
        except Exception:
            hours = 6.0
        time.sleep(max(hours, 0.25) * 3600)


threading.Thread(target=_scheduler, daemon=True).start()


def _authed():
    return bool(ADMIN_TOKEN) and request.headers.get("X-Admin-Token") == ADMIN_TOKEN


@app.get("/healthz")
def healthz():
    return jsonify(ok=True)


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/trades")
def api_trades():
    ticker = request.args.get("ticker") or None
    member = request.args.get("member") or None
    chamber = request.args.get("chamber") or None
    limit = min(int(request.args.get("limit", "100")), 500)
    offset = int(request.args.get("offset", "0"))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """with x as (
                     select t.*, f.filer_name, f.source as chamber, f.filed_date,
                            f.parse_status, f.filer_state, f.chamber_office,
                            case when f.source = 'house' then 'member'
                                 when f.chamber_office ilike '%%(senator)%%'
                                   or trim(coalesce(f.chamber_office,'')) ilike 'senator'
                                 then 'member' else 'staff' end as filer_role
                     from congress_trades t
                     join congress_filings f on f.id = t.filing_id
                   )
                   select * from x
                   where (%(role)s::text is null or filer_role = %(role)s)
                     and (%(ticker)s::text is null or upper(ticker) = upper(%(ticker)s))\n                     and (%(member)s::text is null or filer_name ilike '%%' || %(member)s || '%%')\n                     and (%(chamber)s::text is null or chamber = %(chamber)s)\n                   order by coalesce(transaction_date, filed_date) desc nulls last, id desc\n                   limit %(limit)s offset %(offset)s""",
                {"ticker": ticker, "member": member, "chamber": chamber, "limit": limit, "offset": offset, "role": (request.args.get("role") or None)},
            )
            return jsonify([dict(r) for r in cur.fetchall()])


@app.get("/api/largest")
def api_largest():
    """Biggest transactions in a window, ranked by the statutory range floor\n    (amount_low) - the only defensible size figure, since filings disclose\n    ranges, not exact amounts."""
    days = int(request.args.get("days", "0") or 0)
    ttype = request.args.get("type") or None  # 'buy' | 'sell' | None
    chamber = request.args.get("chamber") or None
    limit = min(int(request.args.get("limit", "50")), 200)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """select t.*, f.filer_name, f.source as chamber, f.filed_date, f.filer_state\n                   from congress_trades t\n                   join congress_filings f on f.id = t.filing_id\n                   where t.amount_low is not null\n                     and (%(days)s = 0 or coalesce(t.transaction_date, f.filed_date)\n                          >= current_date - %(days)s)\n                     and (%(chamber)s::text is null or f.source = %(chamber)s)\n                     and (%(ttype)s::text is null\n                          or (%(ttype)s = 'buy' and t.transaction_type ilike 'p%%')\n                          or (%(ttype)s = 'sell' and t.transaction_type ilike 's%%'))\n                   order by t.amount_low desc, t.amount_high desc nulls last,\n                            coalesce(t.transaction_date, f.filed_date) desc\n                   limit %(limit)s""",
                {"days": days, "chamber": chamber, "ttype": ttype, "limit": limit},
            )
            return jsonify([dict(r) for r in cur.fetchall()])


@app.get("/api/clusters")
def api_clusters():
    """Tickers where buys consolidate across members: distinct buyers, buy\n    count, aggregate range floor/ceiling, buyer names, sell count for context.\n    Buy = transaction_type starting 'P' (House) / 'Purchase' (Senate)."""
    days = int(request.args.get("days", "0") or 0)
    min_buyers = int(request.args.get("min_buyers", "2"))
    limit = min(int(request.args.get("limit", "50")), 200)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """with w as (\n                     select t.ticker, t.transaction_type, t.amount_low, t.amount_high,\n                            f.filer_name\n                     from congress_trades t\n                     join congress_filings f on f.id = t.filing_id\n                     where t.ticker is not null\n                       and (%(days)s = 0 or coalesce(t.transaction_date, f.filed_date)\n                            >= current_date - %(days)s)\n                   )\n                   , c as (select ticker,\n                     count(*) filter (where transaction_type ilike 'p%%') as buys,\n                     count(distinct filer_name) filter (where transaction_type ilike 'p%%') as buyers,\n                     count(*) filter (where transaction_type ilike 's%%') as sells,\n                     sum(amount_low) filter (where transaction_type ilike 'p%%') as buy_floor,\n                     sum(amount_high) filter (where transaction_type ilike 'p%%') as buy_ceiling,\n                     array_agg(distinct filer_name) filter (where transaction_type ilike 'p%%') as buyer_names\n                   from w\n                   group by ticker\n                   having count(distinct filer_name) filter (where transaction_type ilike 'p%%') >= %(min_buyers)s\n                   ), e as (\n                     select f.ticker,\n                       count(*) filter (where t.transaction_code = 'P') as insider_buys,\n                       count(*) filter (where t.transaction_code = 'S') as insider_sells,\n                       sum(t.value) filter (where t.transaction_code = 'P') as insider_buy_value\n                     from edgar_trades t join edgar_form4 f on f.id = t.form4_id\n                     where (%(days)s = 0 or t.transaction_date >= current_date - %(days)s)\n                     group by f.ticker\n                   )\n                   select c.*, coalesce(e.insider_buys,0) as insider_buys,\n                          coalesce(e.insider_sells,0) as insider_sells, e.insider_buy_value\n                   from c left join e on e.ticker = c.ticker\n                   order by buyers desc, buy_floor desc nulls last\n                   limit %(limit)s""",
                {"days": days, "min_buyers": min_buyers, "limit": limit},
            )
            return jsonify([dict(r) for r in cur.fetchall()])


@app.get("/api/insiders")
def api_insiders():
    """Form 4 insider transactions for tracked tickers. Codes: P = open-market
    purchase, S = open-market sale; other codes (awards, options) included but
    filterable."""
    ticker = request.args.get("ticker") or None
    code = request.args.get("code") or None
    days = int(request.args.get("days", "0") or 0)
    limit = min(int(request.args.get("limit", "100")), 500)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """select t.*, f.ticker, f.insider_name, f.insider_title,\n                          f.is_director, f.is_officer, f.filed_date, f.form_type\n                   from edgar_trades t join edgar_form4 f on f.id = t.form4_id\n                   where (%(ticker)s::text is null or f.ticker = upper(%(ticker)s))\n                     and (%(code)s::text is null or t.transaction_code = %(code)s)\n                     and (%(days)s = 0 or t.transaction_date >= current_date - %(days)s)\n                   order by t.transaction_date desc nulls last, t.id desc\n                   limit %(limit)s""",
                {"ticker": ticker, "code": code, "days": days, "limit": limit},
            )
            return jsonify([dict(r) for r in cur.fetchall()])


@app.get("/api/executive")
def api_executive():
    """OGE 278-T transactions by executive-branch officials."""
    ticker = request.args.get("ticker") or None
    official = request.args.get("official") or None
    days = int(request.args.get("days", "0") or 0)
    limit = min(int(request.args.get("limit", "100")), 500)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """select t.*, f.official_name, f.agency, f.position, f.posted_date
                   from oge_trades t join oge_filings f on f.id = t.filing_id
                   where (%(ticker)s::text is null or upper(t.ticker) = upper(%(ticker)s))
                     and (%(official)s::text is null or f.official_name ilike '%%' || %(official)s || '%%')
                     and (%(days)s = 0 or t.transaction_date >= current_date - %(days)s)
                   order by t.transaction_date desc nulls last, t.id desc
                   limit %(limit)s""",
                {"ticker": ticker, "official": official, "days": days, "limit": limit},
            )
            return jsonify([dict(r) for r in cur.fetchall()])


@app.get("/api/filings")
def api_filings():
    limit = min(int(request.args.get("limit", "100")), 500)
    status = request.args.get("status") or None
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """select f.*, count(t.id) as trade_count\n                   from congress_filings f\n                   left join congress_trades t on t.filing_id = f.id\n                   where (%(status)s::text is null or f.parse_status = %(status)s)\n                   group by f.id\n                   order by f.filed_date desc nulls last, f.id desc limit %(limit)s""",
                {"status": status, "limit": limit},
            )
            return jsonify([dict(r) for r in cur.fetchall()])


@app.get("/api/runs")
def api_runs():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """select r.*,
                     case when r.status = 'running' then
                       (select count(*) from congress_filings cf where cf.scrape_run_id = r.id)
                       + (select count(*) from edgar_form4 ef where ef.scrape_run_id = r.id)
                     else r.new_filings end as new_filings_live
                   from congress_scrape_runs r order by r.id desc limit 25"""
            )
            return jsonify([dict(r) for r in cur.fetchall()])


@app.get("/api/config")
def api_config_get():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("select * from congress_config order by key")
            return jsonify([dict(r) for r in cur.fetchall()])


@app.post("/api/config")
def api_config_set():
    if not _authed():
        return jsonify(error="unauthorized"), 401
    body = request.get_json(force=True)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """insert into congress_config (key, value) values (%s, %s)\n                   on conflict (key) do update set value = excluded.value, updated_at = now()""",
                (body["key"], str(body["value"])),
            )
    return jsonify(ok=True)


@app.post("/api/reparse")
def api_reparse():
    """Reset failed/partial House filings to pending so the queue re-processes
    them with the current parser. Deletes their existing trades to avoid dupes."""
    if not _authed():
        return jsonify(error="unauthorized"), 401
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """delete from congress_trades where filing_id in\n                     (select id from congress_filings\n                      where source='house' and parse_status in ('partial','unparseable'))"""
            )
            deleted = cur.rowcount
            cur.execute(
                """update congress_filings set parse_status='pending', parse_error=null\n                   where source='house' and parse_status in ('partial','unparseable')"""
            )
            reset = cur.rowcount
    return jsonify(ok=True, filings_reset=reset, trades_deleted=deleted)


@app.get("/api/debug/fetch")
def api_debug_fetch():
    """Admin-only network probe: status code of a GET from this service's IP."""
    if not _authed():
        return jsonify(error="unauthorized"), 401
    import requests as rq
    url = request.args.get("url")
    ua = request.args.get("ua") or load_config().get("edgar_user_agent", "probe")
    try:
        resp = rq.get(url, headers={"User-Agent": ua}, timeout=30)
        return jsonify(url=url, status=resp.status_code, length=len(resp.content))
    except Exception as e:
        return jsonify(url=url, error=str(e)), 200


@app.post("/api/scrape")
def api_scrape():
    if not _authed():
        return jsonify(error="unauthorized"), 401
    src = request.args.get("source")
    sources = [s.strip() for s in src.split(",")] if src else None
    threading.Thread(target=scrape_all, args=(sources,), daemon=True).start()
    return jsonify(started=True, sources=sources or "all")
