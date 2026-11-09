"""A read-only web page for watching the agent: is it OK, what is it doing, what did it plan.

    python -m vending.dashboard              # http://127.0.0.1:8765
    python -m vending.dashboard --port 9000

It reads the database DATABASE_URL points at and never writes: every query runs
in a read-only transaction, and it never migrates. A panel whose table doesn't
exist yet says so instead of failing the page. `/state.json` returns the same
data as JSON, for a script or another agent.

It listens on 127.0.0.1 only, and has no login: open it on the machine that
runs it, or reach it through an SSH tunnel. Don't put it on a public address.

The page, top to bottom:
 - Verdict and tiles: OK / check / problem, the halt switch, the latest run,
   the order cutoff countdown, the machine's last poll, and what has been spent.
 - Needs attention: each thing that is wrong or about to be, in a sentence.
 - The machine: the latest plan for every slot, laid out like the machine, with
   what changed since the plan before, stock on hand (receipts) and sales (polls).
 - The agent's weekly update and its notes to itself.
 - Runs: each run, how it ended, what it cost, and which slots its plan changed.
 - Tool calls: the last calls from `action_log`, filterable to writes or problems.
 - Requests and back stock: purchase and catalog requests, and what is in the closet.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, localcontext
from fractions import Fraction
from functools import partial
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import set_json_loads

from vending import db, settings
from vending import stock as stock_mod
from vending.planner import IN_STORE_CUTOFF
from vending.slots import SLOTS
from vending.tools import machine_state

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
# How far back the tool-call counts in Needs attention look.
RECENT = timedelta(hours=24)
# Halted this close to the cutoff means no request can go out in time.
CUTOFF_SOON = timedelta(hours=24)


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


def _guarded(conn: psycopg.Connection, read: Callable[[], Any]) -> Any:
    """Run `read` in a savepoint, so a missing table doesn't abort the snapshot's transaction."""
    try:
        with conn.transaction():
            return read()
    except _NOT_THERE as error:
        raise Missing(str(error).splitlines()[0]) from error


def _rows(conn: psycopg.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return _guarded(conn, lambda: conn.cursor(row_factory=dict_row).execute(sql, params).fetchall())


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
    run = _optional(conn, "select id, started_at, finished_at, mode, model, status, detail, cost_usd"
                          " from agent_runs where mode = any(%s) order by started_at desc, id desc limit 1",
                    (list(WATCHED_MODES),))
    if run is None:   # no runs, or an agent_runs older than cost_usd: try without it
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


def _runs(conn: psycopg.Connection, now: datetime) -> list[dict]:
    """The last runs, newest first, each with the slots its plan changed from the plan before it."""
    rows = _rows(conn, """
        select r.id, r.started_at, r.finished_at, r.mode, r.model, r.status, r.detail, r.cost_usd,
               r.request_id, r.team_update, r.notes, r.plan,
               (select p.plan from agent_runs p
                 where p.mode = r.mode and p.plan is not null and (p.started_at, p.id) < (r.started_at, r.id)
                 order by p.started_at desc, p.id desc limit 1) as previous_plan
          from agent_runs r order by r.started_at desc, r.id desc limit %s""", (RUN_ROWS,))
    for r in rows:
        plan, previous = r.pop("plan"), r.pop("previous_plan")
        r["has_plan"] = plan is not None
        r["compared"] = plan is not None and previous is not None
        r["changes"] = _plan_changes(plan, previous) if r["compared"] else []
        r["stale"] = r["status"] == "running" and now - r["started_at"] > STALE_RUN
    return rows


def _plan_changes(plan: Any, previous: Any) -> list[dict]:
    """Each slot whose ask moved between two plans, with short labels for how."""
    now_slots, before = _by_slot(plan), _by_slot(previous)
    out = []
    for slot in sorted(now_slots.keys() | before.keys(), key=_slot_order):
        if _what(now_slots.get(slot)) != _what(before.get(slot)):
            labels = _changes(now_slots.get(slot), before.get(slot)) if slot in now_slots else ["dropped"]
            out.append({"slot": slot, "labels": labels})
    return out


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


def _sales(conn: psycopg.Connection, now: datetime) -> dict:
    """Price and units sold per slot from the stored polls, as `get_machine_state` reports them."""
    reply = _guarded(conn, lambda: machine_state.get_machine_state(conn, now))
    if not reply.success:
        return {"as_of": None, "stale": False, "window_start": None, "slots": {}}
    return {"as_of": reply.as_of, "stale": reply.stale, "window_start": reply.window_start,
            "slots": {s.slot: {"price": s.price, "sold": s.units_sold_in_window, "per_day": s.units_per_day}
                      for s in reply.data}}


def _stock(conn: psycopg.Connection) -> dict:
    """What each slot and the closet hold, from fulfilled receipts and polls (`vending.stock`)."""
    found = _guarded(conn, lambda: stock_mod.stock(conn))
    return {"slots": {s.slot: {"product": s.product, "on_hand": s.units, "loaded": s.loaded, "sold": s.sold,
                               "lost": s.lost, "since": s.since, "unit_cost": _dollars(s.unit_cost)}
                      for s in found.slots},
            "closet": [{"product": c.product, "units": c.units, "unit_cost": _dollars(c.unit_cost)}
                       for c in found.closet],
            "warnings": list(found.warnings)}


def _dollars(cents: Fraction | None) -> str | None:
    """Exact cents as dollars text, rounded half up to the cent. Never a float."""
    if cents is None:
        return None
    with localcontext() as ctx:
        ctx.prec = 40
        value = Decimal(cents.numerator) / Decimal(cents.denominator) / 100
    return str(value.quantize(Decimal("0.01"), ROUND_HALF_UP))


def _spend(conn: psycopg.Connection, now: datetime) -> dict:
    model = _rows(conn, """
        select count(*) as runs, coalesce(sum(cost_usd), 0) as total,
               coalesce(sum(cost_usd) filter (where started_at > %s), 0) as last_7_days,
               count(*) filter (where cost_usd is null and status <> 'running') as unpriced
          from agent_runs where mode = any(%s)""", (now - timedelta(days=7), list(WATCHED_MODES)))[0]
    drinks = _rows(conn, """
        select coalesce(sum(total_paid) filter (where status = 'fulfilled'), 0) as paid,
               count(*) filter (where status = 'fulfilled') as fulfilled,
               count(*) filter (where status = 'pending') as pending,
               count(*) filter (where off_cycle and status <> 'rejected') as midweek
          from purchase_requests""")[0]
    return {"model": model, "drinks": drinks}


def _recent_calls(conn: psycopg.Connection, now: datetime) -> dict:
    rows = _rows(conn, "select kind, outcome, count(*) as n from action_log where at > %s group by kind, outcome",
                 (now - RECENT,))
    counts = {"calls": 0, "writes": 0, "refused": 0, "failed": 0}
    for r in rows:
        counts["calls"] += r["n"]
        counts["writes"] += r["n"] if r["kind"] == "write" else 0
        if r["outcome"] in ("refused", "failed"):
            counts[r["outcome"]] += r["n"]
    return counts


def snapshot(conn: psycopg.Connection, now: datetime | None = None) -> dict:
    """Everything the page shows, from one consistent moment. Reads only.

    JSON numbers come back as Decimal, so a price stored as a number stays exact.
    """
    now = now or datetime.now(timezone.utc)
    set_json_loads(partial(json.loads, parse_float=Decimal), context=conn)
    with conn.transaction():
        conn.execute("set transaction isolation level repeatable read, read only")
        snap = _snapshot(conn, now)
    snap["attention"] = attention(snap)
    return snap


def _snapshot(conn: psycopg.Connection, now: datetime) -> dict:
    return {
        "as_of": now,
        "status": _status(conn, now),
        "plan": _panel(lambda: _plan(conn)),
        "runs": _panel(lambda: _runs(conn, now)),
        "activity": _panel(lambda: _rows(
            conn, "select id, at, kind, tool, outcome, detail, arguments from action_log"
                  " order by at desc, id desc limit %s", (ACTIVITY_ROWS,))),
        "recent_calls": _panel(lambda: _recent_calls(conn, now)),
        "sales": _panel(lambda: _sales(conn, now)),
        "stock": _panel(lambda: _stock(conn)),
        "spend": _panel(lambda: _spend(conn, now)),
        "purchase_requests": _panel(lambda: _rows(conn, """
            select r.request_id, r.submitted_at, r.off_cycle, r.status, r.total_paid, r.decision_note,
                   count(l.line_no) as lines,
                   coalesce(sum(l.units_loaded), 0) as units_loaded,
                   count(*) filter (where l.outcome = 'unavailable') as unavailable,
                   count(*) filter (where l.outcome = 'alternate') as alternates
              from purchase_requests r left join purchase_lines l using (request_id)
             group by r.request_id order by r.submitted_at desc limit %s""", (REQUEST_ROWS,))),
        "catalog_requests": _panel(lambda: _rows(
            conn, "select id, requested_at, name, size, status, decision_note"
                  " from product_requests order by requested_at desc, id desc limit %s", (REQUEST_ROWS,))),
    }


def attention(snap: dict) -> list[dict]:
    """What a person should look at, worst first: `bad` (something broke), `warn` (check it soon)
    and `info` (worth knowing, not wrong). Built from the snapshot alone, so it reads nothing."""
    now, status = snap["as_of"], snap["status"]
    out: list[dict] = []

    def add(level: str, text: str) -> None:
        out.append({"level": level, "text": text})

    run = status["latest_run"]
    if run and run.get("stale"):
        add("bad", f"Run #{run['id']} has said `running` for {_span(now - run['started_at'])}: "
                   "the process probably died.")
    elif run and run["status"] == "failed":
        add("bad", f"The latest run, #{run['id']}, failed" + (f": {run['detail']}" if run.get("detail") else "."))

    cutoff = status["next_cutoff"]
    soon = cutoff is not None and cutoff - now < CUTOFF_SOON
    if status["halted"]:
        add("warn" if soon else "info",
            "The agent is halted, so no write tool can run" + (f" and the cutoff is in {_span(cutoff - now)}"
                                                                 if soon else "")
            + ". Turn it on with `python -m vending.halt off --by NAME`.")
    purchases = snap["purchase_requests"]
    if soon and purchases["ok"]:
        last_cutoff = cutoff - timedelta(days=7)
        sent = any(not r["off_cycle"] and r["submitted_at"] > last_cutoff and r["status"] != "rejected"
                   for r in purchases["data"])
        if not sent:
            add("warn", f"No purchase request for Friday yet, and the cutoff is in {_span(cutoff - now)}.")
    if purchases["ok"] and purchases["data"] and purchases["data"][0]["status"] == "rejected":
        r = purchases["data"][0]
        add("warn", f"Purchase request {r['request_id']} was rejected"
                    + (f": {r['decision_note']}" if r["decision_note"] else "."))

    calls = snap["recent_calls"]
    if calls["ok"] and (calls["data"]["refused"] or calls["data"]["failed"]):
        parts = [f"{n} {what}" for what, n in (("refused", calls["data"]["refused"]),
                                                ("failed", calls["data"]["failed"])) if n]
        add("warn", f"Tool calls in the last 24 hours: {' and '.join(parts)}. See Tool calls below.")

    sales = snap["sales"]
    if sales["ok"]:
        if sales["data"]["as_of"] is None:
            add("info", "No machine polls stored yet, so there are no prices or sales. "
                        "Run `python -m vending.sync_history`.")
        elif sales["data"]["stale"]:
            add("warn", f"The latest machine poll is {_span(now - sales['data']['as_of'])} old: "
                        "the history sync may have stopped.")

    catalog = snap["catalog_requests"]
    if catalog["ok"]:
        for r in catalog["data"]:
            if r["status"] in ("sending", "unknown"):
                add("warn", f"Catalog request for {r['name']} {r['size']} is `{r['status']}`: check it on the "
                            "bridge, then settle it with `python -m vending.product_requests resolve`.")

    stock = snap["stock"]
    if stock["ok"] and stock["data"]["warnings"]:
        n = len(stock["data"]["warnings"])
        add("info", f"{n} stock warning{'s' if n != 1 else ''} from the receipts: {stock['data']['warnings'][0]}"
                    + (" (and more in /state.json)" if n > 1 else ""))

    for key, name in (("plan", "Plan"), ("runs", "Runs"), ("activity", "Tool calls"), ("sales", "Sales"),
                      ("stock", "Stock"), ("purchase_requests", "Purchase requests"),
                      ("catalog_requests", "Catalog requests")):
        if not snap[key]["ok"]:
            add("info", f"{name} isn't available: {snap[key]['why']}")

    order = {"bad": 0, "warn": 1, "info": 2}
    return sorted(out, key=lambda a: order[a["level"]])


def verdict(items: list[dict]) -> str:
    """`problem`, `check` or `ok`: the worst level in Needs attention. `info` alone is still ok."""
    levels = {a["level"] for a in items}
    return "problem" if "bad" in levels else "check" if "warn" in levels else "ok"


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


def _span(delta: timedelta) -> str:
    """A rough length of time: `under a minute`, `12 min`, `3 h 5 min`, `2 d 14 h`."""
    minutes = int(abs(delta.total_seconds()) // 60)
    if minutes < 1:
        return "under a minute"
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    days, hours = divmod(hours, 24)
    return f"{days} d {hours} h" if hours else f"{days} d"


def _time(value: datetime | None, form: str = "abs", now: datetime | None = None) -> str:
    """A <time> the page's script keeps current in the viewer's zone. Server text until it runs.

    `abs` is a date and time, `clock` just the time, `ago` and `until` a span from `now`.
    """
    if value is None:
        return "&mdash;"
    utc = value.astimezone(timezone.utc)
    title = f"{utc:%Y-%m-%d %H:%M} UTC"
    if form == "ago" and now is not None:
        text = f"{_span(now - value)} ago"
    elif form == "until" and now is not None:
        text = _span(value - now)
    elif form == "clock":
        text = f"{utc:%H:%M} UTC"
    else:
        text = title
    return f'<time datetime="{utc.isoformat()}" data-f="{form}" title="{title}">{text}</time>'


def _money(value: Any) -> str:
    """Dollars as stored, exact. Whole cents get two places ($0.00, $9.60); finer amounts keep theirs."""
    if value is None:
        return "&mdash;"
    if isinstance(value, Decimal) and value == value.quantize(Decimal("0.01")):
        value = value.quantize(Decimal("0.01"))
    return f"${_e(value)}"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _pill(text: str, tone: str) -> str:
    return f'<span class="pill {tone}">{_e(text)}</span>'


_RUN_TONE = {"running": "info", "submitted": "ok", "skipped": "muted", "halted": "warn", "failed": "bad"}


def _run_pill(run: dict) -> str:
    if run.get("stale"):
        return _pill("probably died", "bad")
    return _pill(run["status"], _RUN_TONE.get(run["status"], "muted"))


def _missing(panel: dict) -> str | None:
    return None if panel["ok"] else f'<p class="empty">Not available: {_e(panel["why"])}</p>'


def _change_tone(label: str) -> str:
    if label in ("pull", "dropped"):
        return "bad"
    if label.startswith("$"):
        return "warn"
    if label.startswith("fill"):
        return "ok"
    return "info"


def _changes(now: dict | None, before: dict | None) -> list[str]:
    """Short labels for how a slot's entry moved from the previous plan: swap, $2.50 to $2.75, fill 6 to 8..."""
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
        labels.append(f"${before['sale_price']} to ${now['sale_price']}" if before.get("sale_price") is not None
                      else "price")
    old, new = before.get("fill_to"), now.get("fill_to")
    if isinstance(old, int) and isinstance(new, int) and new != old:
        labels.append(f"fill {old} to {new}")
    if bool(now.get("keep")) != bool(before.get("keep")):
        labels.append("keep" if now.get("keep") else "change")
    return labels or ["changed"]


def _entry_text(entry: dict) -> str:
    if entry.get("keep"):
        return "keep as is"
    parts = [entry.get("product") or "same product"]
    if "sale_price" in entry:
        parts.append(f"${entry['sale_price']}")
    if "fill_to" in entry:
        parts.append(f"fill to {entry['fill_to']}")
    return _e(", ".join(str(p) for p in parts))


def _same_price(a: Any, b: Any) -> bool:
    try:
        return Decimal(str(a)) == Decimal(str(b))
    except ArithmeticError:
        return str(a) == str(b)


def _cell(slot: str, planned: dict | None, sold: dict | None, held: dict | None) -> str:
    """One slot of the machine: what the plan asks for and how it changed, what is in it, what it sold."""
    now = planned and planned["now"]
    changed = bool(planned and planned["changed"])
    if not now and not held and not (sold and (sold["price"] or sold["sold"] is not None)):
        return f'<div class="cell unknown" data-slot="{_e(slot)}"><b class="num">{_e(slot)}</b> <span>nothing known</span></div>'

    tags = "".join(_pill(t, _change_tone(t)) for t in (_changes(now, planned["before"]) if changed else []))
    product = (now or {}).get("product") or (held or {}).get("product")
    price_now = (sold or {}).get("price")
    pulled = bool(now and now.get("fill_to") == 0)

    lines = []
    if product:
        lines.append(f'<div class="product">{_e(product)}</div>')
    else:
        lines.append('<div class="product dim">no product known</div>')
    if held and now and now.get("product") and now["product"] != held["product"]:
        lines.append(f'<div class="sub">in it now: {_e(held["product"])}</div>')

    terms = []
    planned_price = now.get("sale_price") if now else None
    if planned_price is not None and price_now is not None and not _same_price(planned_price, price_now):
        terms.append(f'<s>${_e(price_now)}</s> <b>${_e(planned_price)}</b>')
    elif planned_price is not None or price_now is not None:
        terms.append(f"${_e(planned_price if planned_price is not None else price_now)}")
    if now and now.get("keep"):
        terms.append("keep as is")
    elif now and "fill_to" in now:
        terms.append("pull" if pulled else f"fill to {_e(now['fill_to'])}")
    if terms:
        lines.append(f'<div class="terms">{" &middot; ".join(terms)}</div>')

    # Stock bar: units on hand against what the plan fills to, or what was loaded.
    if held is not None:
        on_hand = held["on_hand"]
        fill_to = now.get("fill_to") if now else None
        target = fill_to if isinstance(fill_to, int) and fill_to > 0 else max(held["loaded"], on_hand, 1)
        share = min(on_hand / target, 1) if target else 0
        tone = "bad" if on_hand == 0 else "warn" if share <= 0.34 else "ok"
        lines.append(f'<div class="bar {tone}" role="img" aria-label="{on_hand} of {target} on hand">'
                     f'<i style="width:{round(share * 100)}%"></i></div>')
        stock_text = f"{on_hand} on hand"
    else:
        stock_text = "stock unknown"
    sold_n = (sold or {}).get("sold")
    sales_text = f"{sold_n} sold" if sold_n is not None else "sales unknown"
    lines.append(f'<div class="sub">{stock_text} &middot; {sales_text}</div>')

    reason = now.get("reason") if now else None
    if reason:
        lines.append(f'<div class="why" title="{_e(reason)}">{_e(reason)}</div>')
    if changed and planned["before"] is not None:
        lines.append(f'<div class="was">was: {_entry_text(planned["before"])}</div>')
    classes = "cell" + (" changed" if changed else "") + (" pulled" if pulled else "") + \
        (" empty-slot" if held is not None and held["on_hand"] == 0 else "")
    return (f'<div class="{classes}" data-slot="{_e(slot)}"><div class="top"><b class="num">{_e(slot)}</b>'
            f'<span class="tags">{tags}</span></div>{"".join(lines)}</div>')


def _tiles(snap: dict) -> str:
    now, s = snap["as_of"], snap["status"]
    tiles = []

    if s["halted"]:
        sub = (f'turned on {_time(s["halt_changed_at"], "ago", now)} by {_e(s["halt_changed_by"])}'
               if s["halt_changed_at"] else "no halt row: counts as halted")
        tiles.append(("Halt switch", '<span class="big bad-text">Halted</span>', f"no write tool can run &middot; {sub}"))
    else:
        tiles.append(("Halt switch", '<span class="big ok-text">Off</span>',
                      f'writes allowed &middot; turned off {_time(s["halt_changed_at"], "ago", now)} '
                      f'by {_e(s["halt_changed_by"])}'))

    run = s["latest_run"]
    if run is None:
        tiles.append(("Latest run", '<span class="big dim">None yet</span>', "the agent loop hasn't recorded a run"))
    else:
        if run["status"] == "running" and not run.get("stale"):
            big = f'Running <time datetime="{run["started_at"].astimezone(timezone.utc).isoformat()}" ' \
                  f'data-f="since">{_span(now - run["started_at"])}</time>'
        elif run.get("stale"):
            big = "Probably died"
        else:
            big = _e(run["status"].capitalize())
        cost = f' &middot; {_money(run["cost_usd"])}' if run.get("cost_usd") is not None else ""
        tone = " bad-text" if run.get("stale") or run["status"] == "failed" else ""
        tiles.append(("Latest run", f'<span class="big{tone}">{big}</span>',
                      f'#{run["id"]} {_e(run["mode"])} &middot; started {_time(run["started_at"], "ago", now)}{cost}'))

    cutoff = s["next_cutoff"]
    if cutoff is None:
        tiles.append(("Order cutoff", '<span class="big">Thu 23:59</span>',
                      "Eastern &middot; install <code>tzdata</code> for a countdown"))
    else:
        soon = cutoff - now < CUTOFF_SOON
        tiles.append(("Order cutoff",
                      f'<span class="big{" warn-text" if soon else ""}">{_time(cutoff, "until", now)}</span>',
                      f'{_time(cutoff)} &middot; 23:59 Eastern'))

    sales = snap["sales"]
    if not sales["ok"]:
        tiles.append(("Machine data", '<span class="big dim">&mdash;</span>', "polls table missing"))
    elif sales["data"]["as_of"] is None:
        tiles.append(("Machine data", '<span class="big dim">No polls</span>', "run the history sync"))
    else:
        d = sales["data"]
        total = sum(v["sold"] or 0 for v in d["slots"].values())
        tone = " warn-text" if d["stale"] else ""
        since = f'since {_time(d["window_start"])}' if d["window_start"] else "one poll only"
        tiles.append(("Machine data", f'<span class="big{tone}">{_time(d["as_of"], "ago", now)}</span>',
                      f"last poll &middot; {_plural(total, 'unit')} sold {since}"))

    spend = snap["spend"]
    if spend["ok"]:
        m, dr = spend["data"]["model"], spend["data"]["drinks"]
        unpriced = f' &middot; {m["unpriced"]} without a cost' if m["unpriced"] else ""
        visits = f' &middot; {_plural(dr["midweek"], "midweek visit")}' if dr["midweek"] else ""
        tiles.append(("Spent so far", f'<span class="big">{_money(dr["paid"])}</span>',
                      f'drinks, {_plural(dr["fulfilled"], "fulfilled request")}{visits}<br>'
                      f'model {_money(m["total"])} over {_plural(m["runs"], "run")} '
                      f'({_money(m["last_7_days"])} this week){unpriced}'))
    body = "".join(f'<div class="tile"><div class="label">{label}</div><div>{big}</div>'
                   f'<div class="sub">{sub}</div></div>' for label, big, sub in tiles)
    return f'<div class="tiles">{body}</div>'


def _attention_html(items: list[dict]) -> str:
    if not items:
        return '<div class="attention clear"><b>All clear.</b> Nothing needs a person right now.</div>'
    icon = {"bad": "!", "warn": "!", "info": "i"}
    rows = "".join(f'<li class="{a["level"]}"><span class="dot">{icon[a["level"]]}</span>'
                   f'<span>{_code(_e(a["text"]))}</span></li>' for a in items)
    return f'<ul class="attention">{rows}</ul>'


def _code(text: str) -> str:
    """Escaped text with `backticks` shown as code."""
    parts = text.split("`")
    return "".join(f"<code>{p}</code>" if i % 2 else p for i, p in enumerate(parts)) if len(parts) % 2 else text


def _machine_html(snap: dict) -> str:
    plan, sales, stock, now = snap["plan"], snap["sales"], snap["stock"], snap["as_of"]
    p = plan["data"] if plan["ok"] else None
    planned = {s["slot"]: s for s in p["slots"]} if p else {}
    sold = sales["data"]["slots"] if sales["ok"] else {}
    held = stock["data"]["slots"] if stock["ok"] else {}

    if p:
        run = p["run"]
        changed = sum(1 for s in p["slots"] if s["changed"])
        head = (f'<p class="lede">Plan from run <b>#{run["id"]}</b> ({_e(run["mode"])}, '
                f'{_e(run["model"] or "model not recorded")}) {_run_pill(run)} '
                f'started {_time(run["started_at"], "ago", now)}. '
                + (f'<b>{_plural(changed, "slot")} changed</b> since run #{p["previous_run"]}.'
                   if p["previous_run"] else "No earlier plan of this mode to compare with.") + "</p>")
        if p["visit"] or p["reason"]:
            head += (f'<p class="lede">Visit: <b>{_e(p["visit"] or "not given")}</b>'
                     + (f' &middot; {_e(p["reason"])}' if p["reason"] else "") + "</p>")
    elif plan["ok"]:
        head = ('<p class="empty">No plan yet. Once the agent loop records a run with '
                '<code>vending.agent_runs</code>, each slot shows what it plans and what changed. '
                'Until then this shows stock and sales.</p>')
    else:
        head = _missing(plan)
    if not sold and not held and not p:
        head += ('<p class="empty">No stock or sales either: those come from fulfilled purchase requests and '
                 'machine polls. The grid below is our 20 slots, in machine order.</p>')

    cells = "".join(_cell(slot, planned.get(slot), sold.get(slot), held.get(slot))
                    for slot in sorted(set(SLOTS) | planned.keys(), key=_slot_order))
    legend = ('<p class="legend"><span class="sw changed"></span>changed since the last plan '
              '<span class="sw bar-ok"></span>on hand vs fill target '
              '<span class="sw bar-bad"></span>empty</p>')
    return head + f'<div class="machine">{cells}</div>' + legend


def _words_html(plan: dict) -> str:
    """The agent's weekly update for the team and its notes to itself, from the latest plan."""
    p = plan["data"] if plan["ok"] else None
    if not p or not (p["update"] or p["notes"]):
        return ""
    boxes = "".join(f'<div class="words"><div class="label">{title}</div><div class="text">{_e(body)}</div></div>'
                    for title, body in (("Weekly update for the team", p["update"]),
                                        ("Notes to itself, for next week", p["notes"])) if body)
    return f'<div class="pair">{boxes}</div>'


def _runs_html(panel: dict, now: datetime) -> str:
    if (missing := _missing(panel)) is not None:
        return missing
    if not panel["data"]:
        return ('<p class="empty">No runs recorded yet. The agent loop (#39) calls '
                '<code>agent_runs.start</code> and <code>finish</code>; each run then shows here with '
                'what it cost and what its plan changed.</p>')
    items = []
    for r in panel["data"]:
        end = r["finished_at"] or now
        meta = [_e(r["mode"]), _e(r["model"] or "model not recorded"),
                ("ran " if r["finished_at"] else "running ") + _span(end - r["started_at"])]
        if r["cost_usd"] is not None:
            meta.append(_money(r["cost_usd"]))
        if r["request_id"]:
            meta.append(f'request {_e(r["request_id"])}')
        if r["compared"]:
            chips = ("".join(_pill(f'{c["slot"]} {lab}', _change_tone(lab)) for c in r["changes"] for lab in c["labels"])
                     or '<span class="dim">no slot changes</span>')
        elif r["has_plan"]:
            chips = '<span class="dim">first plan of this mode</span>'
        elif r["status"] == "running" and not r["stale"]:
            chips = '<span class="dim">in progress: the plan shows here when the run ends</span>'
        else:
            chips = '<span class="dim">no plan recorded</span>'
        body = f'<div class="chips">{chips}</div>'
        if r["detail"]:
            body += f'<div class="detail">{_e(r["detail"])}</div>'
        if r["team_update"]:
            body += f'<div class="text clamp">{_e(r["team_update"])}</div>'
        if r["notes"]:
            body += f'<div class="note"><b>Notes:</b> {_e(r["notes"])}</div>'
        items.append(f'<li class="run{" replay" if r["mode"] == "replay" else ""}">'
                     f'<div class="head"><b>#{r["id"]}</b> {_time(r["started_at"])} {_run_pill(r)}</div>'
                     f'<div class="meta">{" &middot; ".join(meta)}</div>{body}</li>')
    return f'<ol class="timeline">{"".join(items)}</ol>'


def _activity_html(panel: dict, recent: dict) -> str:
    if (missing := _missing(panel)) is not None:
        return missing
    if not panel["data"]:
        return '<p class="empty">No tool calls yet. Every call the agent makes, reads included, lands here.</p>'
    counts = ""
    if recent["ok"]:
        c = recent["data"]
        counts = (f'<p class="lede">Last 24 hours: {_plural(c["calls"], "call")}, {c["writes"]} writes, '
                  f'<span class="{"bad-text" if c["refused"] else ""}">{c["refused"]} refused</span>, '
                  f'<span class="{"bad-text" if c["failed"] else ""}">{c["failed"]} failed</span>.</p>')
    rows = []
    for r in panel["data"]:
        problem = r["outcome"] in ("refused", "failed")
        args = json.dumps(r["arguments"], sort_keys=True, indent=1, default=str)
        tone = {"done": "ok", "refused": "bad", "failed": "bad"}.get(r["outcome"], "muted")
        classes = " ".join(c for c in (r["kind"], r["outcome"], "problem" if problem else "") if c)
        rows.append(
            f'<li class="{_e(classes)}"><span class="when">{_time(r["at"], "clock")}</span>'
            f'<span class="kind" title="{_e(r["kind"])}">{"W" if r["kind"] == "write" else "R"}</span>'
            f'<div class="what"><b>{_e(r["tool"])}</b> {_pill(r["outcome"], tone)}'
            + (f'<div class="detail">{_e(r["detail"])}</div>' if r["detail"] else "")
            + f'<details data-key="call-{_e(r["id"])}"><summary>arguments</summary><pre>{_e(args)}</pre></details>'
            f'</div></li>')
    filters = ('<div class="filter" role="group" aria-label="Show">'
               '<button type="button" data-show="all" aria-pressed="true">All</button>'
               '<button type="button" data-show="write" aria-pressed="false">Writes</button>'
               '<button type="button" data-show="problem" aria-pressed="false">Refused or failed</button></div>')
    return counts + filters + f'<ul class="feed" data-show="all">{"".join(rows)}</ul>'


_REQUEST_TONE = {"pending": "info", "fulfilled": "ok", "rejected": "bad", "sending": "warn",
                 "unknown": "warn", "approved": "ok"}


def _requests_html(purchases: dict, catalog: dict, now: datetime) -> str:
    out = '<h3>Purchase requests</h3>'
    if (missing := _missing(purchases)) is not None:
        out += missing
    elif not purchases["data"]:
        out += '<p class="empty">None submitted yet.</p>'
    else:
        items = []
        for r in purchases["data"]:
            lines = ""
            if r["lines"]:
                extra = [f'{r["unavailable"]} unavailable' if r["unavailable"] else "",
                         f'{r["alternates"]} alternate' if r["alternates"] else ""]
                lines = (f' &middot; {_plural(r["lines"], "line")}, {r["units_loaded"]} units loaded'
                         + "".join(f", {x}" for x in extra if x))
            items.append(
                f'<li><div class="head"><b>{_e(r["request_id"])}</b> {_pill(r["status"], _REQUEST_TONE.get(r["status"], "muted"))}'
                f'<span class="money">{"" if r["total_paid"] is None else _money(r["total_paid"])}</span></div>'
                f'<div class="meta">{"midweek ($25 visit)" if r["off_cycle"] else "Friday"} &middot; '
                f'sent {_time(r["submitted_at"], "ago", now)}{lines}</div>'
                + (f'<div class="detail">{_e(r["decision_note"])}</div>' if r["decision_note"] else "") + "</li>")
        out += f'<ul class="list">{"".join(items)}</ul>'
    out += '<h3>Catalog requests</h3>'
    if (missing := _missing(catalog)) is not None:
        out += missing
    elif not catalog["data"]:
        out += '<p class="empty">None yet.</p>'
    else:
        out += '<ul class="list">' + "".join(
            f'<li><div class="head"><b>{_e(r["name"])}</b> <span class="dim">{_e(r["size"])}</span> '
            f'{_pill(r["status"], _REQUEST_TONE.get(r["status"], "muted"))}</div>'
            f'<div class="meta">asked {_time(r["requested_at"], "ago", now)}</div>'
            + (f'<div class="detail">{_e(r["decision_note"])}</div>' if r["decision_note"] else "") + "</li>"
            for r in catalog["data"]) + "</ul>"
    return out


def _closet_html(stock: dict) -> str:
    if (missing := _missing(stock)) is not None:
        return missing
    closet = stock["data"]["closet"]
    if not closet:
        return '<p class="empty">Nothing in back stock, by the receipts.</p>'
    total = sum(c["units"] for c in closet)
    return (f'<p class="lede">{_plural(total, "unit")} of {_plural(len(closet), "product")}, '
            'at what they cost us (weighted average).</p><ul class="closet">' + "".join(
                f'<li><b>{_e(c["product"])}</b><span>{c["units"]} &times; '
                f'{"cost unknown" if c["unit_cost"] is None else _money(c["unit_cost"])}</span></li>'
                for c in closet) + "</ul>")


STYLE = """
:root { color-scheme:light dark;
  --bg:#f3f5f8; --surface:#fff; --sunk:#f7f8fa; --ink:#18202a; --muted:#5d6977; --line:#dfe4ea;
  --accent:#1f5fd1; --accent-soft:#e6efff;
  --ok:#16794a; --ok-soft:#e3f5ea; --warn:#9a5b00; --warn-soft:#fff2d9; --bad:#c0262d; --bad-soft:#fde7e7;
  --info:#1f5fd1; --info-soft:#e6efff; --bar:#e7ebf0; --shadow:0 1px 2px rgba(16,24,40,.06); }
@media (prefers-color-scheme: dark) { :root {
  --bg:#0e1116; --surface:#161b22; --sunk:#11151b; --ink:#e6eaf0; --muted:#94a0ae; --line:#2a313b;
  --accent:#6ea8ff; --accent-soft:#16294a;
  --ok:#4cc38a; --ok-soft:#12301f; --warn:#f0b450; --warn-soft:#3a2a10; --bad:#ff6b6b; --bad-soft:#3d1a1c;
  --info:#6ea8ff; --info-soft:#16294a; --bar:#262d36; --shadow:none; } }
* { box-sizing:border-box }
html { -webkit-text-size-adjust:100% }
body { margin:0; background:var(--bg); color:var(--ink);
  font:14px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; font-variant-numeric:tabular-nums }
main { max-width:1240px; margin:0 auto; padding:16px }
h1 { font-size:20px; margin:0 } h2 { font-size:15px; margin:0 } h3 { font-size:13px; margin:16px 0 8px; color:var(--muted);
  text-transform:uppercase; letter-spacing:.04em }
code { font:12px ui-monospace, "Cascadia Mono", Consolas, monospace; background:var(--sunk); border:1px solid var(--line);
  border-radius:4px; padding:0 4px; overflow-wrap:anywhere }
a { color:var(--accent) }
.dim, .muted { color:var(--muted) }
.ok-text { color:var(--ok) } .warn-text { color:var(--warn) } .bad-text { color:var(--bad) }
header.top { display:flex; flex-wrap:wrap; align-items:center; gap:8px 14px; margin-bottom:14px }
header.top .meta { color:var(--muted); font-size:12px; margin-left:auto }
.verdict { font-weight:700; font-size:13px; padding:3px 10px; border-radius:999px; border:1px solid currentColor }
.verdict.ok { color:var(--ok); background:var(--ok-soft) } .verdict.check { color:var(--warn); background:var(--warn-soft) }
.verdict.problem { color:var(--bad); background:var(--bad-soft) }
#live.failed { color:var(--bad); font-weight:600 }
.tiles { display:grid; grid-template-columns:repeat(auto-fit, minmax(190px, 1fr)); gap:10px }
.tile { background:var(--surface); border:1px solid var(--line); border-radius:10px; padding:12px 14px; box-shadow:var(--shadow); min-width:0 }
.label { font-size:11px; font-weight:600; letter-spacing:.05em; text-transform:uppercase; color:var(--muted) }
.big { display:inline-block; font-size:22px; font-weight:700; margin:2px 0 }
.sub { color:var(--muted); font-size:12px; overflow-wrap:anywhere }
.attention { list-style:none; margin:12px 0 0; padding:0; display:grid; gap:6px }
.attention li { display:flex; gap:10px; align-items:flex-start; padding:9px 12px; border-radius:8px; border:1px solid var(--line);
  background:var(--surface); overflow-wrap:anywhere }
.attention .dot { flex:none; width:20px; height:20px; border-radius:50%; display:grid; place-items:center; font-weight:800;
  font-size:12px; color:#fff; background:var(--muted) }
.attention li.bad { border-color:var(--bad); background:var(--bad-soft) } .attention li.bad .dot { background:var(--bad) }
.attention li.warn { border-color:var(--warn); background:var(--warn-soft) } .attention li.warn .dot { background:var(--warn) }
.attention li.info .dot { background:var(--info) }
div.attention.clear { margin-top:12px; padding:9px 12px; border-radius:8px; border:1px solid var(--ok); background:var(--ok-soft); color:var(--ok) }
section { background:var(--surface); border:1px solid var(--line); border-radius:12px; padding:14px 16px; margin:14px 0;
  box-shadow:var(--shadow); min-width:0 }
section > h2 { margin-bottom:8px; display:flex; align-items:baseline; gap:8px; flex-wrap:wrap }
section > h2 .hint { font-weight:400; font-size:12px; color:var(--muted) }
.grid2 { display:grid; grid-template-columns:minmax(0, 1fr); gap:0 14px }
@media (min-width:980px) { .grid2 { grid-template-columns:minmax(0, 1fr) minmax(0, 1fr) } .grid2 > section { margin:0 0 14px } }
.lede { margin:0 0 8px } .empty { color:var(--muted); background:var(--sunk); border:1px dashed var(--line); border-radius:8px;
  padding:10px 12px; margin:0 0 10px }
.pill { display:inline-block; font-size:11px; font-weight:600; line-height:18px; padding:0 8px; border-radius:999px;
  white-space:nowrap; vertical-align:1px }
.pill.ok { background:var(--ok-soft); color:var(--ok) } .pill.warn { background:var(--warn-soft); color:var(--warn) }
.pill.bad { background:var(--bad-soft); color:var(--bad) } .pill.info { background:var(--info-soft); color:var(--info) }
.pill.muted { background:var(--sunk); color:var(--muted); border:1px solid var(--line) }
.machine { display:grid; grid-template-columns:repeat(4, minmax(0, 1fr)); gap:8px }
.cell { background:var(--sunk); border:1px solid var(--line); border-radius:9px; padding:8px 10px; min-width:0 }
.cell.unknown { color:var(--muted); border-style:dashed; font-size:12px }
.cell.unknown .num { color:var(--ink); margin-right:4px }
.cell.changed { border-color:var(--accent); box-shadow:inset 0 0 0 1px var(--accent); background:var(--surface) }
.cell .top { display:flex; justify-content:space-between; gap:4px; align-items:flex-start; min-height:20px }
.cell .tags { display:flex; flex-wrap:wrap; gap:3px; justify-content:flex-end }
.cell .num { font-size:13px }
.cell .product { font-weight:650; overflow-wrap:anywhere; margin-top:2px }
.cell .terms { font-size:13px } .cell .terms s { color:var(--muted) }
.cell .why { font-size:12px; color:var(--muted); margin-top:3px; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical;
  overflow:hidden; overflow-wrap:anywhere }
.cell .was { font-size:11px; color:var(--muted); margin-top:3px; border-top:1px dashed var(--line); padding-top:3px; overflow-wrap:anywhere }
.bar { height:6px; border-radius:3px; background:var(--bar); margin:6px 0 3px; overflow:hidden }
.bar i { display:block; height:100%; border-radius:3px; background:var(--ok) }
.bar.warn i { background:var(--warn) } .bar.bad { background:var(--bad-soft); box-shadow:inset 0 0 0 1px var(--bad) }
.cell.empty-slot .num { color:var(--bad) }
.legend { font-size:12px; color:var(--muted); margin:10px 0 0; display:flex; flex-wrap:wrap; gap:4px 14px; align-items:center }
.sw { display:inline-block; width:12px; height:12px; border-radius:3px; margin-right:5px; vertical-align:-2px }
.sw.changed { border:2px solid var(--accent) } .sw.bar-ok { background:var(--ok) } .sw.bar-bad { background:var(--bad) }
.pair { display:grid; grid-template-columns:repeat(auto-fit, minmax(260px, 1fr)); gap:10px; margin-top:12px }
.words { background:var(--sunk); border:1px solid var(--line); border-radius:9px; padding:10px 12px }
.text { white-space:pre-wrap; overflow-wrap:anywhere; margin-top:4px }
.timeline { list-style:none; margin:0; padding:0 0 0 16px; border-left:2px solid var(--line) }
.timeline li { position:relative; padding:0 0 14px 4px }
.timeline li::before { content:""; position:absolute; left:-23px; top:5px; width:10px; height:10px; border-radius:50%;
  background:var(--surface); border:2px solid var(--muted) }
.timeline li:first-child::before { background:var(--accent); border-color:var(--accent) }
.timeline li.replay { opacity:.7 }
.head { display:flex; flex-wrap:wrap; gap:4px 8px; align-items:center }
.head .money { margin-left:auto; font-weight:600 }
.meta { color:var(--muted); font-size:12px; overflow-wrap:anywhere }
.chips { display:flex; flex-wrap:wrap; gap:4px; margin:5px 0 2px; font-size:12px }
.detail { font-size:13px; overflow-wrap:anywhere; margin-top:2px }
.clamp { display:-webkit-box; -webkit-line-clamp:3; -webkit-box-orient:vertical; overflow:hidden; font-size:13px }
.note { font-size:12px; color:var(--muted); border-left:2px solid var(--line); padding-left:8px; margin-top:5px; overflow-wrap:anywhere }
.filter { display:inline-flex; border:1px solid var(--line); border-radius:8px; overflow:hidden; margin:0 0 8px }
.filter button { font:inherit; font-size:12px; border:0; background:var(--surface); color:var(--ink); padding:4px 10px; cursor:pointer }
.filter button + button { border-left:1px solid var(--line) }
.filter button[aria-pressed="true"] { background:var(--accent-soft); color:var(--accent); font-weight:600 }
.feed { list-style:none; margin:0; padding:0; max-height:640px; overflow-y:auto }
.feed li { display:grid; grid-template-columns:auto auto minmax(0, 1fr); gap:8px; padding:7px 4px; border-top:1px solid var(--line) }
.feed li.write { background:linear-gradient(90deg, var(--warn-soft), transparent 60%) }
.feed li.problem { background:var(--bad-soft) }
.feed[data-show="write"] li:not(.write), .feed[data-show="problem"] li:not(.problem) { display:none }
.feed .when { font-size:12px; color:var(--muted); white-space:nowrap; padding-top:1px }
.feed .kind { width:20px; height:20px; border-radius:5px; display:grid; place-items:center; font-size:11px; font-weight:700;
  background:var(--sunk); border:1px solid var(--line); color:var(--muted) }
.feed li.write .kind { color:var(--warn); border-color:var(--warn) }
.feed .what { min-width:0; overflow-wrap:anywhere }
details summary { cursor:pointer; color:var(--muted); font-size:12px }
pre { margin:4px 0 0; font:12px ui-monospace, Consolas, monospace; background:var(--sunk); border:1px solid var(--line);
  border-radius:6px; padding:6px 8px; white-space:pre-wrap; overflow-wrap:anywhere; max-height:240px; overflow-y:auto }
.list { list-style:none; margin:0; padding:0 }
.list li { padding:8px 0; border-top:1px solid var(--line) } .list li:first-child { border-top:0 }
.closet { list-style:none; margin:0; padding:0; display:grid; grid-template-columns:repeat(auto-fill, minmax(210px, 1fr)); gap:0 16px }
.closet li { display:flex; justify-content:space-between; gap:8px; padding:5px 0; border-bottom:1px solid var(--line); font-size:13px; min-width:0 }
.closet li b { font-weight:600; overflow-wrap:anywhere } .closet li span { color:var(--muted); white-space:nowrap }
footer { color:var(--muted); font-size:12px; text-align:center; margin:18px 0 8px }
@media (max-width:640px) {
  main { padding:12px } .big { font-size:19px } header.top .meta { margin-left:0; width:100% }
  .tiles { grid-template-columns:repeat(2, minmax(0, 1fr)); gap:8px } .tile { padding:10px 12px }
  .tile:last-child:nth-child(odd) { grid-column:1 / -1 }
  .machine { grid-template-columns:repeat(2, minmax(0, 1fr)) }
  .feed li { grid-template-columns:auto minmax(0, 1fr) } .feed .kind { display:none } }
"""

# Keeps times current in the viewer's zone, and refreshes the page in place every REFRESH_SECONDS
# without losing scroll, open details or the tool-call filter. A failed refresh keeps the last page
# and says so. Without JavaScript the <noscript> meta refresh reloads the page instead.
SCRIPT = """(function () {
  var REFRESH = %d * 1000, show = 'all', lastOk = Date.now(), failed = false;
  try { show = localStorage.getItem('dash-show') || 'all'; } catch (e) {}
  function span(ms) {
    var m = Math.floor(Math.abs(ms) / 60000);
    if (m < 1) return 'under a minute';
    if (m < 60) return m + ' min';
    var h = Math.floor(m / 60); m = m %% 60;
    if (h < 24) return m ? h + ' h ' + m + ' min' : h + ' h';
    var d = Math.floor(h / 24); h = h %% 24;
    return h ? d + ' d ' + h + ' h' : d + ' d';
  }
  function times() {
    var now = Date.now();
    document.querySelectorAll('time[datetime]').forEach(function (t) {
      var d = new Date(t.getAttribute('datetime')), f = t.getAttribute('data-f');
      if (isNaN(d)) return;
      if (f === 'ago') t.textContent = span(now - d) + ' ago';
      else if (f === 'until') t.textContent = d > now ? span(d - now) : 'passed';
      else if (f === 'since') t.textContent = span(now - d);
      else if (f === 'clock') t.textContent = d.toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});
      else t.textContent = d.toLocaleString([], {weekday: 'short', month: 'short', day: 'numeric',
                                                  hour: 'numeric', minute: '2-digit'});
      t.title = d.toLocaleString();
    });
    var live = document.getElementById('live');
    if (live) {
      live.textContent = failed ? 'refresh failed, showing data from ' + span(now - lastOk) + ' ago'
                                : 'updated ' + span(now - lastOk) + ' ago';
      live.className = failed ? 'failed' : '';
    }
  }
  function filter() {
    document.querySelectorAll('.feed').forEach(function (f) { f.setAttribute('data-show', show); });
    document.querySelectorAll('.filter button').forEach(function (b) {
      b.setAttribute('aria-pressed', String(b.getAttribute('data-show') === show));
    });
  }
  document.addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest('.filter button');
    if (!b) return;
    show = b.getAttribute('data-show');
    try { localStorage.setItem('dash-show', show); } catch (err) {}
    filter();
  });
  function refresh() {
    if (document.hidden) return;
    fetch(location.pathname, {cache: 'no-store'}).then(function (r) {
      if (!r.ok) throw new Error(r.status);
      return r.text();
    }).then(function (html) {
      var fresh = new DOMParser().parseFromString(html, 'text/html').querySelector('main');
      var main = document.querySelector('main');
      if (!fresh || !main) throw new Error('no page');
      var open = Array.prototype.map.call(main.querySelectorAll('details[open][data-key]'),
                                          function (d) { return d.getAttribute('data-key'); });
      main.innerHTML = fresh.innerHTML;
      open.forEach(function (k) {
        var d = main.querySelector('details[data-key="' + CSS.escape(k) + '"]');
        if (d) d.open = true;
      });
      failed = false; lastOk = Date.now(); filter(); times();
    }).catch(function () { failed = true; times(); });
  }
  filter(); times();
  setInterval(refresh, REFRESH);
  setInterval(times, 15000);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) refresh(); });
})();"""


_VERDICT_TEXT = {"ok": "OK", "check": "Check", "problem": "Problem"}


def render(snap: dict, refresh: int = REFRESH_SECONDS) -> str:
    now = snap["as_of"]
    items = snap.get("attention")
    if items is None:
        items = attention(snap)
    state = verdict(items)
    p = snap["plan"]["data"] if snap["plan"]["ok"] else None
    machine_hint = ("rows 1 to 5, columns 5 to 8 &middot; outlined slots changed"
                    if p and p["previous_run"] else "rows 1 to 5, columns 5 to 8")
    head = (f'<header class="top"><h1>Agent dashboard</h1>'
            f'<span class="verdict {state}">{_VERDICT_TEXT[state]}</span>'
            f'<span class="meta">Read-only &middot; as of {_time(now)} &middot; <span id="live">'
            f'refreshes every {int(refresh)} s</span> &middot; <a href="state.json">JSON</a></span></header>')
    body = (head + _tiles(snap) + _attention_html(items)
            + f'<section id="machine"><h2>The machine <span class="hint">{machine_hint}</span></h2>'
            + _machine_html(snap) + _words_html(snap["plan"]) + "</section>"
            + '<div class="grid2">'
            + f'<section id="runs"><h2>Runs <span class="hint">newest first &middot; chips are what each plan '
              f'changed</span></h2>{_runs_html(snap["runs"], now)}</section>'
            + f'<section id="calls"><h2>Tool calls <span class="hint">last {ACTIVITY_ROWS}</span></h2>'
              f'{_activity_html(snap["activity"], snap["recent_calls"])}</section>'
            + "</div>"
            + '<div class="grid2">'
            + f'<section id="requests"><h2>Requests</h2>'
              f'{_requests_html(snap["purchase_requests"], snap["catalog_requests"], now)}</section>'
            + f'<section id="closet"><h2>Back stock <span class="hint">the closet, from receipts</span></h2>'
              f'{_closet_html(snap["stock"])}</section>'
            + "</div>"
            + '<footer>This page only reads. To stop the agent: <code>python -m vending.halt on --by NAME</code></footer>')
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<noscript><meta http-equiv="refresh" content="{int(refresh)}"></noscript>'
            f'<title>{_VERDICT_TEXT[state]} &middot; Agent dashboard</title>'
            f'<style>{STYLE}</style></head><body><main>{body}</main>'
            f'<script>{SCRIPT % int(refresh)}</script></body></html>')


def _error_page(message: str) -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<meta http-equiv="refresh" content="{REFRESH_SECONDS}"><title>No database &middot; Agent dashboard</title>'
            f'<style>{STYLE}</style></head><body><main><header class="top"><h1>Agent dashboard</h1>'
            f'<span class="verdict problem">No data</span></header>'
            f'<ul class="attention"><li class="bad"><span class="dot">!</span><span>Can\'t read the database: '
            f'{_e(message)}. This page tries again every {REFRESH_SECONDS} s.</span></li></ul></main></body></html>')


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
        self.send_header("X-Content-Type-Options", "nosniff")
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
