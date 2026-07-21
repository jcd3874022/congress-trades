import logging
import os
import threading
import time

from flask import Flask, jsonify, render_template, request

from db import get_conn
from scraper import house, senate
from scraper.common import load_config

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")
app = Flask(__name__)
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
_scrape_lock = threading.Lock()


def scrape_all():
    if not _scrape_lock.acquire(blocking=False):
        return {"skipped": "scrape already running"}
    try:
        cfg = load_config()
        out = {}
        out["senate"] = senate.run(cfg)
        out["house"] = house.run(cfg)
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
                """select t.*, f.filer_name, f.source as chamber, f.filed_date,\n                          f.parse_status, f.filer_state\n                   from congress_trades t\n                   join congress_filings f on f.id = t.filing_id\n                   where (%(ticker)s::text is null or upper(t.ticker) = upper(%(ticker)s))\n                     and (%(member)s::text is null or f.filer_name ilike '%%' || %(member)s || '%%')\n                     and (%(chamber)s::text is null or f.source = %(chamber)s)\n                   order by coalesce(t.transaction_date, f.filed_date) desc nulls last, t.id desc\n                   limit %(limit)s offset %(offset)s""",
                {"ticker": ticker, "member": member, "chamber": chamber, "limit": limit, "offset": offset},
            )
            return jsonify([dict(r) for r in cur.fetchall()])


@app.get("/api/filings")
def api_filings():
    limit = min(int(request.args.get("limit", "100")), 500)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """select f.*, count(t.id) as trade_count\n                   from congress_filings f\n                   left join congress_trades t on t.filing_id = f.id\n                   group by f.id\n                   order by f.filed_date desc nulls last, f.id desc limit %s""",
                (limit,),
            )
            return jsonify([dict(r) for r in cur.fetchall()])


@app.get("/api/runs")
def api_runs():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("select * from congress_scrape_runs order by id desc limit 25")
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


@app.post("/api/scrape")
def api_scrape():
    if not _authed():
        return jsonify(error="unauthorized"), 401
    threading.Thread(target=scrape_all, daemon=True).start()
    return jsonify(started=True)
