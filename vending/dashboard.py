"""A read-only web page for watching the agent: status, plan, activity, requests.

    python -m vending.dashboard              # http://127.0.0.1:8765
    python -m vending.dashboard --port 9000

It reads the database DATABASE_URL points at and never writes: every query runs
in a read-only transaction, and it never migrates. A panel whose table doesn't
exist yet says so instead of failing the page. `/state.json` returns the same
data as JSON, for a script or another agent.

It listens on 127.0.0.1 only, and has no login: open it on the machine that
runs it, or reach it through an SSH tunnel. Don't put it on a public address.

Panels:
 - Status: the halt switch, the latest run, the last tool call, the next cutoff.
 - Plan: the latest run's plan for every slot next to the run before it, with
   its weekly update and notes (`agent_runs`, written by the agent loop).
 - Runs: the last runs, how each ended and what it cost.
 - Activity: the last tool calls from `action_log`; refused and failed ones stand out.
 - Requests: our purchase and catalog requests and where each stands.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import partial
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import set_json_loads

from vending import db, settings
from vending.planner import IN_STORE_CUTOFF
from vending.slots import SLOTS

HOST = "127.0.0.1"
PORT = 8765
REFRESH_SECONDS = 30
ACTIVITY_ROWS = 50
RUN_ROWS = 10
REQUEST_ROWS = 10
# Every transaction on the dashboard's connection is read-only; Postgres refuses any write.
READ_ONLY = "-c default_transaction_read_only=on"
# The runs the Status and Plan panels follow. Replays of past weeks show only under Runs.
WATCHED_MODES = ("live", "dry-run")
# Host headers the server answers, so another site can't rebind its name to us and read the page.
LOCAL_HOSTS = ("127.0.0.1", "localhost")
# A run still `running` after this long has almost certainly died.
STALE_RUN = timedelta(hours=2)


def _connect() -> psycopg.Connection:
    url = settings.require("DATABASE_URL", "Copy the DATABASE_URL line from .env.example into .env.")
    return psycopg.connect(url, autocommit=True, connect_timeout=db.CONNECT_TIMEOUT,
                           options=READ_ONLY)


# Tests swap this to point at a throwaway schema.
connect: Callable[[], psycopg.Connection] = _connect


class Missing(Exception):
    """A table or column the panel needs doesn't exist yet."""


# A table that isn't there yet, or is older than this code.
_NOT_THERE = (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn)


def _rows(conn: psycopg.Connection, sql: str, params: tuple = ()) -> list[dict]:
    try:
        # A savepoint, so a missing table doesn't abort the snapshot's transaction.
        with conn.transaction():
            return conn.cursor(row_factory=dict_row).execute(sql, params).fetchall()
    except _NOT_THERE as error:
        raise Missing(str(error).splitlines()[0]) from error


def _optional(conn: psycopg.Connection, sql: str, params: tuple = ()) -> dict | None:
    """One row, or None when there is none or its table isn't there yet."""
    try:
        rows = _rows(conn, sql, params)
    except Missing:
        return None
    return rows[0] if rows else None


def _panel(build: Callable[[], Any]) -> dict:
    """A panel's data, or why it has none. One missing table never blanks the page."""
    try:
        return {"ok": True, "data": build()}
    except Missing as error:
        return {"ok": False, "why": f"{error}. Run `python -m vending.db migrate` on this database."}


# --- Data ---------------------------------------------------------------------------------------

def next_cutoff(now: datetime) -> datetime | None:
    """The next Thursday 23:59 Eastern, the purchase-request deadline. None without the IANA database."""
    try:
        from zoneinfo import ZoneInfo
        eastern = ZoneInfo("America/New_York")
    except Exception:   # Windows without the tzdata package
        return None
    local = now.astimezone(eastern)
    days = (IN_STORE_CUTOFF.weekday - local.weekday()) % 7
    cutoff = datetime.combine(local.date() + timedelta(days=days), IN_STORE_CUTOFF.at, eastern)
    return cutoff if cutoff > local else cutoff + timedelta(days=7)


def _status(conn: psycopg.Connection, now: datetime) -> dict:
    flag = _optional(conn, "select value, updated_at, updated_by from config where key = 'halted'")
    # Fails closed like `vending.config.is_halted`: anything but JSON false is halted.
    halted = flag is None or flag["value"] is not False
    run = _optional(conn, "select id, started_at, finished_at, mode, model, status, detail"
                          " from agent_runs where mode = any(%s) order by started_at desc, id desc limit 1",
                    (list(WATCHED_MODES),))
    if run and run["status"] == "running" and now - run["started_at"] > STALE_RUN:
        run["stale"] = True
    last = _optional(conn, "select at, kind, tool, outcome from action_log order by at desc, id desc limit 1")
    return {"halted": halted, "halt_changed_at": flag and flag["updated_at"],
            "halt_changed_by": flag and flag["updated_by"], "latest_run": run,
            "last_action": last, "next_cutoff": next_cutoff(now)}


def _plan(conn: psycopg.Connection) -> dict | None:
    columns = "id, started_at, finished_at, mode, model, status, plan, team_update, notes"
    found = _rows(conn, f"select {columns} from agent_runs where plan is not null and mode = any(%s)"
                        " order by started_at desc, id desc limit 1", (list(WATCHED_MODES),))
    if not found:
        return None
    latest = found[0]
    # Compared only with a plan of the same mode: a dry run against a live plan says nothing.
    previous = _optional(conn, f"select {columns} from agent_runs where plan is not null and mode = %s"
                               " and (started_at, id) < (%s, %s) order by started_at desc, id desc limit 1",
                         (latest["mode"], latest["started_at"], latest["id"]))
    now_slots = _by_slot(latest["plan"])
    before = _by_slot(previous["plan"]) if previous else {}
    slots = [{"slot": slot, "now": now_slots.get(slot), "before": before.get(slot),
              "changed": previous is not None and _what(now_slots.get(slot)) != _what(before.get(slot))}
             for slot in sorted(set(SLOTS) | now_slots.keys(), key=_slot_order)]
    plan = latest["plan"] if isinstance(latest["plan"], dict) else {}
    return {"run": {k: latest[k] for k in ("id", "started_at", "finished_at", "mode", "model", "status")},
            "previous_run": previous and previous["id"],
            "visit": plan.get("visit"), "reason": plan.get("reason"),
            "update": latest["team_update"], "notes": latest["notes"], "slots": slots}


def _slot_order(slot: str) -> tuple[int, str]:
    return (SLOTS.index(slot), "") if slot in SLOTS else (len(SLOTS), slot)


def _by_slot(plan: Any) -> dict[str, dict]:
    """The plan's slot entries by slot. Tolerates a plan that isn't the expected shape."""
    if not isinstance(plan, dict) or not isinstance(plan.get("slots"), list):
        return {}
    return {str(e["slot"]): e for e in plan["slots"] if isinstance(e, dict) and "slot" in e}


def _what(entry: dict | None) -> tuple:
    """What a slot entry asks for, without its reason, so a reworded reason isn't a change."""
    if entry is None:
        return ()
    return tuple(sorted((k, json.dumps(v, sort_keys=True, default=str)) for k, v in entry.items() if k != "reason"))


def snapshot(conn: psycopg.Connection, now: datetime | None = None) -> dict:
    """Everything the page shows, from one consistent moment. Reads only.

    JSON numbers come back as Decimal, so a price stored as a number stays exact.
    """
    now = now or datetime.now(timezone.utc)
    set_json_loads(partial(json.loads, parse_float=Decimal), context=conn)
    with conn.transaction():
        conn.execute("set transaction isolation level repeatable read, read only")
        return _snapshot(conn, now)


def _snapshot(conn: psycopg.Connection, now: datetime) -> dict:
    return {
        "as_of": now,
        "status": _status(conn, now),
        "plan": _panel(lambda: _plan(conn)),
        "runs": _panel(lambda: _rows(
            conn, "select id, started_at, finished_at, mode, model, status, detail, cost_usd, request_id"
                  " from agent_runs order by started_at desc, id desc limit %s", (RUN_ROWS,))),
        "activity": _panel(lambda: _rows(
            conn, "select id, at, kind, tool, outcome, detail, arguments from action_log"
                  " order by at desc, id desc limit %s", (ACTIVITY_ROWS,))),
        "purchase_requests": _panel(lambda: _rows(
            conn, "select request_id, submitted_at, off_cycle, status, total_paid, decision_note"
                  " from purchase_requests order by submitted_at desc limit %s", (REQUEST_ROWS,))),
        "catalog_requests": _panel(lambda: _rows(
            conn, "select id, requested_at, name, size, status, decision_note"
                  " from product_requests order by requested_at desc, id desc limit %s", (REQUEST_ROWS,))),
    }


def to_json(value: Any) -> str:
    def default(v: Any) -> Any:
        if isinstance(v, datetime):
            return v.isoformat()
        if isinstance(v, Decimal):
            return str(v)    # money stays exact text, as the tools send it
        raise TypeError(f"can't send {type(v).__name__} as JSON")
    return json.dumps(value, default=default, indent=1)


# --- Page ---------------------------------------------------------------------------------------

def _e(value: Any) -> str:
    return "" if value is None else escape(str(value))


def _time(value: datetime | None) -> str:
    """A <time> the page's script shows in the viewer's local time; UTC until it runs."""
    if value is None:
        return "&mdash;"
    utc = value.astimezone(timezone.utc)
    return f'<time datetime="{utc.isoformat()}">{utc:%Y-%m-%d %H:%M} UTC</time>'


def _changes(now: dict | None, before: dict | None) -> list[str]:
    """Short labels for how a slot's entry moved from the previous plan: swap, price, fill up..."""
    if now is None:
        return []
    if before is None:
        return ["new"]
    labels = []
    if now.get("fill_to") == 0 and before.get("fill_to") != 0:
        return ["pull"]
    if now.get("product") and now.get("product") != before.get("product"):
        labels.append("swap")
    if "sale_price" in now and str(now["sale_price"]) != str(before.get("sale_price")):
        labels.append("price")
    old, new = before.get("fill_to"), now.get("fill_to")
    if isinstance(old, int) and isinstance(new, int) and new != old:
        labels.append(f"fill {old} to {new}")
    if bool(now.get("keep")) != bool(before.get("keep")):
        labels.append("keep" if now.get("keep") else "change")
    return labels or ["changed"]


def _cell(s: dict, compared: bool) -> str:
    """One slot of the machine grid: what the plan asks for, how it changed, and why."""
    now = s["now"]
    tags = "".join(f'<span class="tag {"pull" if t == "pull" else ""}">{_e(t)}</span>'
                   for t in (_changes(now, s["before"]) if s["changed"] else []))
    if now is None:
        body = '<div class="muted">not in plan</div>'
    elif now.get("keep"):
        body = '<div class="what">keep as is</div>'
    else:
        terms = []
        if "sale_price" in now:
            terms.append(f"${_e(now['sale_price'])}")
        if "fill_to" in now:
            terms.append(f"fill to {_e(now['fill_to'])}")
        body = (f'<div class="what">{_e(now.get("product") or "same product")}</div>'
                f'<div class="muted">{" &middot; ".join(terms)}</div>')
    reason = now.get("reason") if now else None
    was = (f'<div class="muted small">was: {_entry_text(s["before"])}</div>'
           if compared and s["changed"] and s["before"] is not None else "")
    return (f'<div class="cell{" changed" if s["changed"] else ""}"><div class="top"><b>{_e(s["slot"])}</b>'
            f'<span>{tags}</span></div>{body}'
            + (f'<div class="muted small">{_e(reason)}</div>' if reason else "") + was + "</div>")


def _entry_text(entry: dict) -> str:
    if entry.get("keep"):
        return "keep as is"
    parts = [entry.get("product") or "same product"]
    if "sale_price" in entry:
        parts.append(f"${entry['sale_price']}")
    if "fill_to" in entry:
        parts.append(f"fill to {entry['fill_to']}")
    return _e(", ".join(str(p) for p in parts))


def _table(headers: list[str], rows: list[list[str]], *, row_class: list[str] | None = None) -> str:
    head = "".join(f"<th>{h}</th>" for h in headers)
    body = "".join(
        f'<tr class="{(row_class or [""] * len(rows))[i]}">' + "".join(f"<td>{c}</td>" for c in row) + "</tr>"
        for i, row in enumerate(rows))
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _missing(panel: dict) -> str | None:
    return None if panel["ok"] else f'<p class="muted">Not available: {_e(panel["why"])}</p>'


def _status_html(s: dict) -> str:
    badge = ('<span class="badge stop">HALTED</span> no write tool can run'
             if s["halted"] else '<span class="badge go">RUNNING</span> writes allowed')
    flag = (f' &middot; changed {_time(s["halt_changed_at"])} by {_e(s["halt_changed_by"])}'
            if s["halt_changed_at"] else "")
    run = s["latest_run"]
    if run is None:
        run_html = '<span class="muted">no runs recorded yet</span>'
    else:
        state = "probably died (still running after 2 hours)" if run.get("stale") else run["status"]
        run_html = (f'#{run["id"]} {_e(run["mode"])} &middot; <b>{_e(state)}</b> &middot; '
                    f'started {_time(run["started_at"])}' + (f' &middot; {_e(run["detail"])}' if run["detail"] else ""))
    last = s["last_action"]
    last_html = (f'{_time(last["at"])} &middot; {_e(last["kind"])} {_e(last["tool"])} &middot; {_e(last["outcome"])}'
                 if last else '<span class="muted">none</span>')
    cutoff = s["next_cutoff"]
    cutoff_html = (f'{_time(cutoff)} (Thursday 23:59 Eastern)' if cutoff
                   else 'Thursday 23:59 Eastern (install <code>tzdata</code> to show the date)')
    return (f'<dl><dt>Halt switch</dt><dd>{badge}{flag}</dd><dt>Latest run</dt><dd>{run_html}</dd>'
            f'<dt>Last tool call</dt><dd>{last_html}</dd><dt>Next order cutoff</dt><dd>{cutoff_html}</dd></dl>')


def _plan_html(panel: dict) -> str:
    if (missing := _missing(panel)) is not None:
        return missing
    p = panel["data"]
    if p is None:
        return ('<p class="muted">No plan yet. The agent loop (#39) records each run\'s plan, '
                'weekly update and notes here.</p>')
    run = p["run"]
    head = (f'<p>Run #{run["id"]} ({_e(run["mode"])}, {_e(run["model"] or "model not recorded")}), '
            f'started {_time(run["started_at"])}, <b>{_e(run["status"])}</b>.'
            + (f' Compared with run #{p["previous_run"]}.' if p["previous_run"] else " No earlier plan to compare.")
            + "</p>")
    if p["visit"] or p["reason"]:
        head += f'<p>Visit: <b>{_e(p["visit"] or "not given")}</b>. {_e(p["reason"])}</p>'
    texts = "".join(f'<h3>{title}</h3><div class="text">{_e(body)}</div>'
                    for title, body in (("Weekly update", p["update"]), ("Notes to itself", p["notes"])) if body)
    changed = sum(1 for s in p["slots"] if s["changed"])
    summary = (f'<p class="muted">{changed} slot{"s" if changed != 1 else ""} changed. '
               if p["previous_run"] else '<p class="muted">') +         "Laid out like the machine: our slots are rows 1 to 5, columns 5 to 8.</p>"
    cells = "".join(_cell(s, bool(p["previous_run"])) for s in p["slots"])
    return head + texts + "<h3>The plan, slot by slot</h3>" + summary + f'<div class="machine">{cells}</div>'


def _runs_html(panel: dict) -> str:
    if (missing := _missing(panel)) is not None:
        return missing
    if not panel["data"]:
        return '<p class="muted">No runs recorded yet.</p>'
    rows = [[f'#{r["id"]}', _time(r["started_at"]), _e(r["mode"]), _e(r["model"]), _e(r["status"]),
             "" if r["cost_usd"] is None else f'${_e(r["cost_usd"])}', _e(r["request_id"]), _e(r["detail"])]
            for r in panel["data"]]
    return _table(["Run", "Started", "Mode", "Model", "Ended", "Cost", "Request", "Detail"], rows)


def _activity_html(panel: dict) -> str:
    if (missing := _missing(panel)) is not None:
        return missing
    if not panel["data"]:
        return '<p class="muted">No tool calls yet.</p>'
    rows, classes = [], []
    for r in panel["data"]:
        args = json.dumps(r["arguments"], sort_keys=True, default=str)
        rows.append([_time(r["at"]), _e(r["kind"]), _e(r["tool"]), _e(r["outcome"]), _e(r["detail"]),
                     f'<code>{_e(args if len(args) <= 160 else args[:157] + "...")}</code>'])
        classes.append(r["outcome"] if r["outcome"] in ("refused", "failed") else
                       ("write" if r["kind"] == "write" else ""))
    return _table(["When", "Kind", "Tool", "Outcome", "Detail", "Arguments"], rows, row_class=classes)


def _requests_html(purchases: dict, catalog: dict) -> str:
    out = "<h3>Purchase requests</h3>"
    if (missing := _missing(purchases)) is not None:
        out += missing
    elif not purchases["data"]:
        out += '<p class="muted">None submitted yet.</p>'
    else:
        out += _table(["Request", "Submitted", "Visit", "Status", "Paid", "Note"], [
            [_e(r["request_id"]), _time(r["submitted_at"]), "midweek ($25)" if r["off_cycle"] else "Friday",
             _e(r["status"]), "" if r["total_paid"] is None else f'${_e(r["total_paid"])}', _e(r["decision_note"])]
            for r in purchases["data"]])
    out += "<h3>Catalog requests</h3>"
    if (missing := _missing(catalog)) is not None:
        out += missing
    elif not catalog["data"]:
        out += '<p class="muted">None yet.</p>'
    else:
        out += _table(["When", "Product", "Size", "Status", "Note"], [
            [_time(r["requested_at"]), _e(r["name"]), _e(r["size"]), _e(r["status"]), _e(r["decision_note"])]
            for r in catalog["data"]])
    return out


STYLE = """
:root { --bg:#fff; --fg:#1d232b; --muted:#5f6b7a; --line:#d9dee5; --panel:#f6f8fa;
        --go:#1a7f37; --stop:#b42318; --warn:#fff4d6; --bad:#fde8e7; --chg:#e8f1ff; --accent:#0b5cd5; }
@media (prefers-color-scheme: dark) { :root { --bg:#14181d; --fg:#e6e9ee; --muted:#9aa5b2; --line:#2c333c;
        --panel:#1b2027; --go:#3fb950; --stop:#f85149; --warn:#3a3016; --bad:#3d1d1c; --chg:#172a45;
        --accent:#58a6ff; } }
* { box-sizing:border-box } body { margin:0; background:var(--bg); color:var(--fg);
  font:14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif }
main { max-width:1200px; margin:0 auto; padding:16px } h1 { font-size:20px; margin:0 0 4px }
h2 { font-size:16px; margin:0 0 10px } h3 { font-size:14px; margin:14px 0 6px }
section { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:14px; margin:14px 0 }
.muted { color:var(--muted) } .small { font-size:12px } code { font-size:12px; word-break:break-all }
dl { display:grid; grid-template-columns:max-content 1fr; gap:6px 16px; margin:0 } dt { color:var(--muted) }
.badge { font-weight:700; padding:2px 8px; border-radius:4px; color:#fff }
.go { background:var(--go) } .stop { background:var(--stop) }
.scroll { overflow-x:auto } table { border-collapse:collapse; width:100% }
th, td { text-align:left; vertical-align:top; padding:6px 8px; border-bottom:1px solid var(--line) }
th { font-weight:600; color:var(--muted) } tr.refused, tr.failed { background:var(--bad) }
tr.write { background:var(--warn) }
.machine { display:grid; grid-template-columns:repeat(4, minmax(0, 1fr)); gap:8px }
.cell { background:var(--bg); border:1px solid var(--line); border-radius:8px; padding:8px 10px; min-width:0 }
.cell.changed { border-color:var(--accent); box-shadow:0 0 0 1px var(--accent) }
.cell .top { display:flex; justify-content:space-between; gap:6px; align-items:baseline }
.cell .what { font-weight:600; overflow-wrap:anywhere }
.tag { display:inline-block; font-size:11px; font-weight:600; padding:0 7px; border-radius:999px;
       background:var(--chg); color:var(--accent); margin-left:4px; white-space:nowrap }
.tag.pull { background:var(--bad); color:var(--stop) }
.text { white-space:pre-wrap; background:var(--bg); border:1px solid var(--line); border-radius:6px; padding:8px 10px }
@media (max-width:600px) { dl { grid-template-columns:1fr } dt { margin-top:6px }
                           .machine { grid-template-columns:repeat(2, minmax(0, 1fr)) } }
"""

# Shows each <time> in the viewer's local time zone; without it, times stay in UTC.
SCRIPT = """document.querySelectorAll('time[datetime]').forEach(function (t) {
  var d = new Date(t.getAttribute('datetime'));
  if (!isNaN(d)) t.textContent = d.toLocaleString([], {weekday:'short', month:'short', day:'numeric',
                                                         hour:'numeric', minute:'2-digit'});
});"""


def render(snap: dict, refresh: int = REFRESH_SECONDS) -> str:
    sections = [
        ("Status", _status_html(snap["status"])),
        ("Plan", _plan_html(snap["plan"])),
        ("Runs", _runs_html(snap["runs"])),
        ("Activity", _activity_html(snap["activity"])),
        ("Requests", _requests_html(snap["purchase_requests"], snap["catalog_requests"])),
    ]
    body = "".join(f"<section><h2>{title}</h2>{html}</section>" for title, html in sections)
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<meta http-equiv="refresh" content="{int(refresh)}"><title>Agent dashboard</title>'
            f'<style>{STYLE}</style></head><body><main><h1>Agent dashboard</h1>'
            f'<p class="muted small">Read-only. As of {_time(snap["as_of"])}; refreshes every {int(refresh)} s. '
            f'<a href="state.json">JSON</a></p>{body}</main><script>{SCRIPT}</script></body></html>')


def _error_page(message: str) -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta http-equiv="refresh" '
            f'content="{REFRESH_SECONDS}"><title>Agent dashboard</title><style>{STYLE}</style></head>'
            f'<body><main><h1>Agent dashboard</h1><section><h2>Can\'t read the database</h2>'
            f'<p>{_e(message)}</p></section></main></body></html>')


# --- Server -------------------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "vending-dashboard"

    def do_GET(self) -> None:  # noqa: N802 (the stdlib's name)
        path = self.path.split("?", 1)[0]
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        if host not in LOCAL_HOSTS:
            return self._send(403, "text/plain; charset=utf-8", "open this page as 127.0.0.1 or localhost")
        if path not in ("/", "/state.json"):
            return self._send(404, "text/plain; charset=utf-8", "not found")
        try:
            with connect() as conn:
                snap = snapshot(conn)
        except (psycopg.Error, settings.SettingsError) as error:
            message = str(error).strip().splitlines()[0] if str(error).strip() else type(error).__name__
            if path == "/state.json":
                return self._send(503, "application/json", to_json({"error": message}))
            return self._send(503, "text/html; charset=utf-8", _error_page(message))
        if path == "/state.json":
            return self._send(200, "application/json", to_json(snap))
        self._send(200, "text/html; charset=utf-8", render(snap))

    def _send(self, code: int, content_type: str, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 (the stdlib's name)
        pass   # one line per refresh would bury anything worth reading


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m vending.dashboard", description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args(argv)
    httpd = ThreadingHTTPServer((HOST, args.port), Handler)
    print(f"dashboard on http://{HOST}:{args.port}  (read-only; Ctrl+C to stop)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
