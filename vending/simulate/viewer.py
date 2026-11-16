"""Browse simulated seasons through our real dashboard and P&L page, at any moment of the season.

    python -m vending.simulate.viewer --sim agent=postgresql://postgres:sim@127.0.0.1:PORT/agent \\
        --sim baseline=postgresql://postgres:sim@127.0.0.1:PORT/baseline        # then open http://127.0.0.1:8790

`/` compares the simulations week by week: profit, units, stock-outs and model spend. `/NAME/?at=...`
is the agent dashboard (`vending.dashboard`) exactly as it would have looked at that simulated moment,
with a time bar to step an hour, a day or a week, or jump to any run or visit. `/NAME/pnl?at=...` is the
P&L page (`vending.pnl_report`) as of the same moment.

**How "as of" works.** Every row a season writes carries a simulated time (`season.restamp`). For each
page the viewer opens one transaction on that season's database, deletes every row stamped after the
moment asked for, builds the page with the real dashboard or P&L code, and rolls back. So the page is
what that code would have shown then, nothing is ever changed, and there is no second copy of the
logic to drift from the real pages. Only scratch databases on this machine are accepted.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Iterator
from urllib.parse import parse_qs, quote, urlsplit

import psycopg

from vending.simulate import world
from vending.simulate.world import EASTERN

PORT = 8790
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", ""}

# Every table with a time, and what to do with rows after the moment shown. Order matters for the
# foreign keys: losses and lines go before the requests they belong to.
CUT = (
    "delete from agent_runs where started_at > %(t)s",
    "delete from agent_steps where at > %(t)s",
    "update agent_runs set status = 'running', finished_at = null where finished_at > %(t)s",
    "delete from action_log where at > %(t)s",
    "delete from polls where started_at > %(t)s",
    "delete from purchase_line_losses where recorded_at > %(t)s",
    "delete from purchase_lines where resolved_at > %(t)s",
    "update purchase_requests set status = 'pending', total_paid = null, fulfilled_at = null, updated_at = null,"
    " last_response = null where fulfilled_at > %(t)s",
    "update purchase_requests set last_checked = %(t)s where last_checked > %(t)s",
    "delete from purchase_requests where submitted_at > %(t)s",
    "delete from purchase_submissions where at > %(t)s",
    "update model_calls set ledger_entry = null where at > %(t)s",
    "delete from model_calls where at > %(t)s",
    "delete from ledger_entries where at > %(t)s",
    "delete from catalog_syncs where synced_at > %(t)s",
    "delete from product_requests where requested_at > %(t)s",
)


def _connect(url: str) -> psycopg.Connection:
    """In UTC like every connection the app opens, keeping any options the URL sets (a search_path, say)."""
    from vending import db
    return psycopg.connect(url, options=db.session_options(url), connect_timeout=5)


class _InTransaction:
    """A connection already inside the viewer's transaction: a nested `transaction()` becomes a
    savepoint, and the pages' `set transaction isolation ...` (only allowed first) is skipped."""

    def __init__(self, conn: psycopg.Connection):
        self._conn = conn

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    def execute(self, query, params=None, **kw):
        if isinstance(query, str) and query.lstrip().lower().startswith("set transaction"):
            return self._conn.execute("select 1")
        return self._conn.execute(query, params, **kw)


@contextmanager
def as_of(url: str, moment: datetime) -> Iterator[_InTransaction]:
    """The season's database as it stood at `moment`, inside a transaction that is always rolled back."""
    from vending.simulate.clock import Clock, patched
    with _connect(url) as conn:
        try:
            with conn.transaction(force_rollback=True):
                for statement in CUT:
                    conn.execute(statement, {"t": moment})
                with patched(Clock(moment)):
                    yield _InTransaction(conn)
        finally:
            conn.rollback()


def season_bounds() -> tuple[datetime, datetime]:
    return world.SIM_START, world.COMPETITION_END


def parse_at(text: str | None) -> datetime:
    if not text:
        return world.COMPETITION_END - timedelta(minutes=1)
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=EASTERN)
    start, end = season_bounds()
    return min(max(moment, start), end - timedelta(minutes=1))


def milestones(url: str) -> list[tuple[datetime, str]]:
    """Runs and visits, to jump to: (when, label)."""
    with _connect(url) as conn:
        found = [(at, f"run {i} ({status})") for i, at, status in
                 conn.execute("select id, started_at, status from agent_runs order by started_at")]
        found += [(at, f"visit: {rid}") for rid, at in conn.execute(
            "select request_id, fulfilled_at from purchase_requests where fulfilled_at is not null")]
    return sorted(found)


# --- the season summary (the index) -----------------------------------------------------------------

def summary(url: str) -> dict:
    """Week-by-week figures for one season, from the ledger and the run records."""
    from vending import ledger
    with _connect(url) as conn:
        weeks = ledger.pnl_by_week(conn, world.LEDGER_OPENS, world.COMPETITION_END)
        spend = dict(conn.execute(
            "select date_trunc('day', at at time zone 'America/New_York')::date, sum(cost_usd)"
            " from model_calls group by 1").fetchall())
        model = conn.execute("select model, effort from model_calls order by id desc limit 1").fetchone()
        scripted = conn.execute("select count(*) from model_calls where message_id like 'msg_scripted%'").fetchone()[0]
        runs = conn.execute("select count(*), count(*) filter (where status = 'submitted'),"
                            " count(*) filter (where status = 'failed') from agent_runs").fetchone()
    out, running = [], Fraction(0)
    for w in weeks:
        token = sum((Decimal(v) for d, v in spend.items() if w.week <= d < w.week + timedelta(days=7)), Decimal(0))
        revenue = Fraction(w.takings_cents - w.tax_cents)
        gross = revenue - w.cost_of_sales_cents - w.shrinkage_cents
        profit = gross - w.visit_fees_cents - Fraction(token * 100)
        running += profit
        out.append({"week": w.week.isoformat(), "units": w.units_sold, "revenue": float(revenue) / 100,
                    "gross_margin": float(gross) / 100, "visit_fees": w.visit_fees_cents / 100,
                    "model_spend": float(token), "profit": float(profit) / 100, "cumulative": float(running) / 100})
    return {"decider": "scripted baseline" if scripted else "model",
            "model": None if scripted or not model else f"{model[0]} at {model[1]}",
            "runs": runs[0], "submitted": runs[1], "failed": runs[2], "weeks": out,
            "season_profit": float(running) / 100}


def _chart(sims: dict[str, dict]) -> str:
    """Cumulative profit per season, one line each, as inline SVG."""
    width, height, pad = 760, 260, 44
    series = {n: [w["cumulative"] for w in s["weeks"]] for n, s in sims.items()}
    values = [v for vs in series.values() for v in vs] or [0]
    low, high = min(0, min(values)), max(1, max(values))
    weeks = max(len(v) for v in series.values()) if series else 1
    x = lambda i: pad + i * (width - 2 * pad) / max(weeks - 1, 1)
    y = lambda v: height - pad - (v - low) * (height - 2 * pad) / (high - low)
    colors = ("#2563eb", "#d97706", "#059669", "#7c3aed", "#dc2626")
    parts = [f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img" '
             'aria-label="Cumulative profit by week">',
             f'<line x1="{pad}" x2="{width - pad}" y1="{y(0):.1f}" y2="{y(0):.1f}" stroke="#999"/>']
    for tick in (low, (low + high) / 2, high):
        parts.append(f'<text x="4" y="{y(tick) + 4:.1f}" font-size="11" fill="#555">${tick:,.0f}</text>')
    for i in range(weeks):
        parts.append(f'<text x="{x(i) - 8:.1f}" y="{height - 18}" font-size="11" fill="#555">W{i + 1}</text>')
    for k, (name, vs) in enumerate(series.items()):
        c = colors[k % len(colors)]
        pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(vs))
        parts.append(f'<polyline fill="none" stroke="{c}" stroke-width="2.5" points="{pts}"/>')
        if vs:
            parts.append(f'<text x="{x(len(vs) - 1) - 60:.1f}" y="{y(vs[-1]) - 8:.1f}" font-size="12" fill="{c}">'
                         f'{html.escape(name)} ${vs[-1]:,.0f}</text>')
    return "".join(parts) + "</svg>"


def index_page(sims: dict[str, str]) -> str:
    data = {name: summary(url) for name, url in sims.items()}
    rows = []
    weeks = max((len(s["weeks"]) for s in data.values()), default=0)
    head = "".join(f"<th colspan=2>{html.escape(n)}</th>" for n in data)
    for i in range(weeks):
        cells = ""
        for s in data.values():
            w = s["weeks"][i] if i < len(s["weeks"]) else None
            cells += (f"<td>${w['profit']:,.2f}</td><td>{w['units']}</td>" if w else "<td></td><td></td>")
        week = next(s["weeks"][i]["week"] for s in data.values() if i < len(s["weeks"]))
        rows.append(f"<tr><td>W{i + 1} (from Sat {week})</td>{cells}</tr>")
    cards = "".join(
        f'<li><a href="/{quote(n)}/">{html.escape(n)}</a>: {html.escape(s["decider"])}'
        f'{" (" + html.escape(s["model"]) + ")" if s["model"] else ""}, {s["runs"]} runs, {s["submitted"]} submitted, '
        f'{s["failed"]} failed; season profit after model spend <b>${s["season_profit"]:,.2f}</b> '
        f'(<a href="/{quote(n)}/pnl">P&amp;L</a>)</li>' for n, s in data.items())
    sub = "".join("<th>profit</th><th>units</th>" for _ in data)
    return _page("Simulated seasons", f"""
<h1>Simulated seasons</h1>
<p class=warn>Simulated data: a made-up machine, customers and worker driving our real agent and tools.
Not the competition.</p>
<ul>{cards}</ul>
{_chart(data)}
<p class=note>Profit per week = revenue ex tax, less cost of sales, shrinkage, midweek fees and model spend
(model spend is counted here, though the simulation keeps it out of the ledger as test spend).</p>
<table><tr><th></th>{head}</tr><tr><th>Week</th>{sub}</tr>{''.join(rows)}</table>
<p><a href="/summary.json">summary.json</a></p>""")


# --- one season, as of a moment ------------------------------------------------------------------------

def time_bar(name: str, url: str, moment: datetime, page: str) -> str:
    start, end = season_bounds()
    local = moment.astimezone(EASTERN)
    hours = int((end - start).total_seconds() // 3600)
    at_hour = int((moment - start).total_seconds() // 3600)

    def link(delta: timedelta, label: str) -> str:
        t = min(max(moment + delta, start), end - timedelta(minutes=1)).astimezone(EASTERN)
        return f'<a href="{page}?at={quote(t.isoformat(timespec="minutes"))}">{label}</a>'
    week = (local.date() - world.LEDGER_OPENS.astimezone(EASTERN).date()).days
    options = "".join(
        f'<option value="{html.escape(t.astimezone(EASTERN).isoformat(timespec="minutes"))}"'
        f'{" selected" if abs((t - moment).total_seconds()) < 60 else ""}>'
        f'{t.astimezone(EASTERN):%a %d %b %H:%M} {html.escape(label)}</option>'
        for t, label in milestones(url))
    other = "pnl" if page == "./" else "./"
    other_label = "P&amp;L" if page == "./" else "Dashboard"
    return f"""<div style="position:sticky;top:0;z-index:99;background:#fef3c7;border-bottom:2px solid #d97706;
padding:6px 12px;font:14px system-ui,sans-serif">
<b>SIMULATION {html.escape(name)}</b> &middot; as of <b>{local:%a %d %b %Y %H:%M} ET</b> (day {week + 1} after the
ledger opened) &middot; {link(-timedelta(weeks=1), "&laquo; week")} {link(-timedelta(days=1), "&lsaquo; day")}
{link(-timedelta(hours=1), "&lsaquo; hour")} &middot; {link(timedelta(hours=1), "hour &rsaquo;")}
{link(timedelta(days=1), "day &rsaquo;")} {link(timedelta(weeks=1), "week &raquo;")} &middot;
<select onchange="location.href='{page}?at='+encodeURIComponent(this.value)"><option>jump to a run or visit</option>
{options}</select> &middot; <a href="{other}?at={quote(local.isoformat(timespec='minutes'))}">{other_label}</a>
&middot; <a href="/">all simulations</a><br>
<input type=range min=0 max={hours} value={at_hour} style="width:100%"
onchange="var t=new Date({int(start.timestamp() * 1000)}+this.value*3600e3);location.href='{page}?at='+t.toISOString()">
</div>"""


def dashboard_page(name: str, url: str, moment: datetime) -> tuple[str, dict]:
    from psycopg.types.json import set_json_loads

    from vending import dashboard
    with as_of(url, moment) as conn:
        set_json_loads(partial(json.loads, parse_float=Decimal), context=conn._conn)
        snap = dashboard._snapshot(conn, moment.astimezone(timezone.utc))
        snap["attention"] = dashboard.attention(snap)
    stamp = quote(moment.astimezone(EASTERN).isoformat(timespec="minutes"))
    page = dashboard.render(snap, refresh=86400, json_href=f"state.json?at={stamp}",
                            nav=(("P&L", f"pnl?at={stamp}"), ("All simulations", "/")))
    return page.replace("<body>", "<body>" + time_bar(name, url, moment, "./"), 1), snap


def pnl_page_bare(url: str, moment: datetime) -> str:
    """The P&L page as of `moment`, without the time bar."""
    from vending import pnl_report
    with as_of(url, moment) as conn:
        report = pnl_report.load(conn, None, moment.astimezone(EASTERN).date())
        _, body = pnl_report.render(report)
    return body


def pnl_page(name: str, url: str, moment: datetime) -> str:
    return pnl_page_bare(url, moment).replace("<body>", "<body>" + time_bar(name, url, moment, "pnl"), 1)


def _page(title: str, body: str) -> str:
    return (f"<!doctype html><html><head><meta charset=utf-8><title>{html.escape(title)}</title><style>"
            "body{font:14px system-ui,sans-serif;margin:24px;max-width:1100px}table{border-collapse:collapse}"
            "td,th{border:1px solid #ddd;padding:4px 8px;text-align:right}td:first-child{text-align:left}"
            ".warn{background:#fef3c7;padding:6px 10px}.note{color:#555}</style></head>"
            f"<body>{body}</body></html>")


def make_handler(sims: dict[str, str]):
    class Handler(BaseHTTPRequestHandler):
        server_version = "vending-sim-viewer"

        def do_GET(self) -> None:  # noqa: N802
            parts = urlsplit(self.path)
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
            if host not in LOCAL_HOSTS:
                return self._send(403, "text/plain", "open this page as 127.0.0.1 or localhost")
            query = parse_qs(parts.query)
            bits = [b for b in parts.path.split("/") if b]
            try:
                if not bits:
                    return self._send(200, "text/html", index_page(sims))
                if bits == ["summary.json"]:
                    return self._send(200, "application/json",
                                      json.dumps({n: summary(u) for n, u in sims.items()}, indent=1))
                name, rest = bits[0], bits[1:]
                if name not in sims:
                    return self._send(404, "text/plain", f"no simulation {name}")
                moment = parse_at((query.get("at") or [None])[0])
                if rest in ([], ["index.html"]):
                    return self._send(200, "text/html", dashboard_page(name, sims[name], moment)[0])
                if rest == ["state.json"]:
                    from vending import dashboard
                    return self._send(200, "application/json",
                                      dashboard.to_json(dashboard_page(name, sims[name], moment)[1]))
                if rest == ["pnl"]:
                    return self._send(200, "text/html", pnl_page(name, sims[name], moment))
                return self._send(404, "text/plain", "not found")
            except Exception as error:   # show it: this is a local tool for the team
                return self._send(500, "text/plain", f"{type(error).__name__}: {error}")

        def _send(self, code: int, kind: str, body: str) -> None:
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", f"{kind}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args) -> None:
            pass
    return Handler


def main(argv: list[str] | None = None) -> int:
    from vending.simulate.season import check_scratch
    parser = argparse.ArgumentParser(prog="python -m vending.simulate.viewer", description=__doc__.splitlines()[0])
    parser.add_argument("--sim", action="append", required=True, metavar="NAME=URL",
                        help="a season's scratch database; repeat to compare")
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args(argv)
    sims = {}
    for item in args.sim:
        name, _, url = item.partition("=")
        check_scratch(url)
        sims[name] = url
    # One request at a time: "as of" swaps the clock for the whole process while a page is built.
    httpd = HTTPServer(("127.0.0.1", args.port), make_handler(sims))
    print(f"simulation viewer on http://127.0.0.1:{args.port}  (read-only; Ctrl+C to stop)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
