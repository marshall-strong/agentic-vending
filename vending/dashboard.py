"""A read-only web page for watching the agent: is it OK, what is it doing, what did it plan.

    python -m vending.dashboard              # http://127.0.0.1:8765
    python -m vending.dashboard --port 9000

It reads the database DATABASE_URL points at and never writes: every query runs
in a read-only transaction, and it never migrates. A panel whose table doesn't
exist yet says so instead of failing the page. `/state.json` returns the same
data as JSON, for a script or another agent.

Run this way it listens on 127.0.0.1 only, and has no login: open it on the
machine that runs it. The Container App serves the same page to anyone at
`/dashboard`, through `vending.pages`, which connects as a login that can only
read. Both routes scope the data to the ledger opening and current assignment.

The page, top to bottom:
 - The status bar, one row of six cells: the verdict (OK / check / problem) with the halt
   switch and the latest run; when the machine was last polled and the next sync; the order
   cutoff countdown; spendable (from the ledger once it has opened; before that, only that
   it is development spend); catalog approvals; and the plan (for the first fill before the slot lock,
   for the next restock after).
 - In the machine, one screen: the machine's whole front (`vending.machine_art`), our
   selections (the stored assignment, `vending.slots.our_slots`) with their package, count,
   price and next-restock badge, the rest half as wide and marked "Not ours"; with no assignment
   stored it says so instead of guessing. Beside it, Needs attention (each thing that is wrong
   or about to be, in a sentence; on the Container App that includes the worker's latest sync,
   which the worker passes in as `syncs`, the same dict as its `/health`) and the plan's
   changes. Picking one of ours shows its shelf's lanes from above and its facts instead; on a
   phone that is a sheet. Now shows the tag price (polls) and the product and units on hand
   (receipts, or a newer count); Next restock shows the latest plan: the slots it fills, a
   swap, a pull, a new price. Anything without a record says unknown.
 - Pricing (`vending.pricing_panel`): for each of our selections, the shelf price, the unit cost
   (receipts, else expected from sourcing), the floor and whether the price is legal, the margin,
   what sold, the plan's next price and why, and the shelf price over the last four weeks.
 - Money (`vending.money_panel`, #48): the balance, spendable and profit since the
   ledger's opening, where the money has gone, what pending requests could still
   cost, and the proposed budget (#113). Before the opening it says how to record it.
 - The plan: what changed since the plan before, then one line per slot that
   opens to the agent's reason, what it was, and what the slot holds and sold.
   Then the agent's weekly update and its notes to itself.
 - Runs: each run, how it ended, what it cost, and which slots its plan changed, with the steps
   it took (`agent_steps`): its reasoning, each tool call and what came back.
 - Tool calls: the last calls from `action_log`, filterable to writes or problems.
 - Requests and back stock: purchase requests, every catalog request with counts by status and
   the places left under the bridge's cap on open requests, and what is in the closet.
 - The footer: one small line on who has looked at the pages (`vending.visits`).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import sys
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, localcontext
from fractions import Fraction
from functools import partial
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import set_json_loads

from vending import capacity, db, leaderboards, machine_art, money_panel, plan_rounds, settings, slot_counts, visits
from vending import pricing_panel
from vending import stock as stock_mod
from vending import planner
from vending.planner import IN_STORE_CUTOFF
from vending.slots import DEVICE_ID, RECORD_HINT, SlotsNotRecorded, lock_at, our_slots
from vending.tools import machine_state
from vending.mascot import svg as mascot_svg  # after the rest: other PRs edit the lines above

HOST = "127.0.0.1"
PORT = 8765
REFRESH_SECONDS = 30
ACTIVITY_ROWS = 50
RUN_ROWS = 10
# Purchase requests read. Catalog requests are all read: the team needs every one of them.
REQUEST_ROWS = 10
# About how many open (pending) catalog requests the bridge lets one team have before it answers
# 429. Observed, not documented: it refused a filing with 25 open on 17 Nov 2026, and one at 24 on
# 15 Nov, though that day's count was inferred. Kept here, not in `vending.tools.catalog_request`,
# so the page imports nothing that needs time-zone data to start.
OPEN_REQUEST_CAP = 25
# A catalog request's statuses, in the order the counts show them (`db/migrations/005_product_requests.sql`).
CATALOG_STATUSES = ("pending", "approved", "rejected", "sending", "unknown")
# What a sync job's exit code means. The three syncs share them (`vending/sync_history.py` and the others).
SYNC_EXITS = {1: "finished with problems; the container's log has them",
              2: "the database is missing migrations; run `python -m vending.db migrate`",
              3: "a bridge error: unreachable, or it refused our key or answered with an error"}
# Purchase requests shown before the rest fold away.
REQUESTS_SHOWN = 5
# Every transaction on the dashboard's connection is read-only; Postgres refuses any write.
READ_ONLY = "-c default_transaction_read_only=on"
# Host headers the server answers, so another site can't rebind its name to us and read the page.
LOCAL_HOSTS = ("127.0.0.1", "localhost")
# A run still `running` after this long has almost certainly died.
STALE_RUN = timedelta(hours=2)
# How far back the tool-call counts in Needs attention look.
RECENT = timedelta(hours=24)
# Halted this close to the cutoff means no request can go out in time.
CUTOFF_SOON = timedelta(hours=24)
# The worker's hourly sync runs this many minutes past the hour (UTC), just after the course staff's
# poll on the hour: `SYNC_AT_MINUTE`, as `vending.worker` reads it, else this.
SYNC_AT_MINUTE = 5
# The instructors must approve every product for the first fill by Wed 25 Nov 2026. The status bar says
# so until the end of that day, Eastern. In UTC so the page needs no time-zone data.
APPROVALS_DUE = datetime(2026, 11, 26, 5, 0, tzinfo=timezone.utc)
# The machine's face: 5 shelves of 8 selections. Row r, column c is selection "rc".
SHELVES, COLUMNS = machine_art.SHELVES, machine_art.COLUMNS
# The mark on the wildcard slot's rows (#166): the one fixed slot that tests products.
WILD = machine_art.WILD_PILL
# What a free-text product name says about its package, for a lane named by a count.
_package = machine_art.package_from_name


def _connect() -> psycopg.Connection:
    url = settings.require("DATABASE_URL", "Copy the DATABASE_URL line from .env.example into .env.")
    return psycopg.connect(url, autocommit=True, connect_timeout=db.CONNECT_TIMEOUT,
                           options=db.session_options(url, READ_ONLY))


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


class Unavailable(Exception):
    """An optional panel's read failed (a query over the statement timeout, say); the rest of the page
    carries on without it. Its text is ours, never the database's."""


# Milliseconds an optional panel may wait, on a statement or a lock, before it gives up.
OPTIONAL_TIMEOUT_MS = 1_000


def _isolated(conn: psycopg.Connection, read: Callable[[], Any], timeout_ms: int = OPTIONAL_TIMEOUT_MS) -> Any:
    """Run an optional panel's `read` in a savepoint with its own short statement and lock
    timeouts, so a slow, locked or broken table costs the page at most `timeout_ms` and that panel.

    A missing table is still `Missing` (the fix is a migration); any other failure is `Unavailable`.
    The error's own text is left out: on a public page it would name the host and the user."""
    try:
        with conn.transaction():
            old = conn.execute("select current_setting('statement_timeout'), current_setting('lock_timeout')").fetchone()
            conn.execute("select set_config('statement_timeout', %s, true), set_config('lock_timeout', %s, true)",
                         (str(timeout_ms), str(timeout_ms)))
            data = read()
            # Put the snapshot's own limits back; on a failure the rollback to the savepoint does.
            conn.execute("select set_config('statement_timeout', %s, true), set_config('lock_timeout', %s, true)", old)
            return data
    except _NOT_THERE as error:
        raise Missing(str(error).splitlines()[0]) from error
    except Exception as error:  # noqa: BLE001 - an optional panel never takes the page down
        raise Unavailable(type(error).__name__) from error


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
    except Unavailable as error:
        return {"ok": False, "why": f"couldn't be read just now ({error})"}


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


def _eastern(value: datetime) -> datetime:
    """`value` in Eastern, the machine's zone; unchanged without the IANA database."""
    try:
        from zoneinfo import ZoneInfo
        return value.astimezone(ZoneInfo("America/New_York"))
    except Exception:   # Windows without the tzdata package
        return value


# action_log has no run ID. Before opening, keep only real operator and agent writes by name.
PRE_OPENING_WRITES = (
    "config.set", "halt",
    "ledger.adjustment", "ledger.capital", "ledger.opening", "ledger.tax_remitted",
    "pages.grant",
    "plan_store.load", "plan_store.pick",  # Operator commands in vending.plan_store.
    "product_requests.resolve", "request_backlog.add", "request_backlog.drop", "request_backlog.move",
    "request_catalog_product", "set_wildcard_slot", "slot_counts", "slots.record",
    "sourcing.remove", "sourcing.set", "submissions.resolve", "submit_desired_state",
)
ACTIVITY_VISIBLE = "(at <= %s and (at >= %s or (kind = 'write' and tool = any(%s))))"


def _status(conn: psycopg.Connection, now: datetime, opening: datetime | None) -> dict:
    flag = _optional(conn, "select value, updated_at, updated_by from config where key = 'halted'")
    # Fails closed like `vending.config.is_halted`: anything but JSON false is halted.
    halted = flag is None or flag["value"] is not False
    run = _optional(conn, "select id, started_at, finished_at, mode, model, status, detail, cost_usd"
                          " from agent_runs where mode = 'live' and started_at <= %s"
                          " order by started_at desc, id desc limit 1", (now,))
    if run is None:   # no runs, or an agent_runs older than cost_usd: try without it
        run = _optional(conn, "select id, started_at, finished_at, mode, model, status, detail"
                              " from agent_runs where mode = 'live' and started_at <= %s"
                              " order by started_at desc, id desc limit 1", (now,))
    if run and run["status"] == "running" and now - run["started_at"] > STALE_RUN:
        run["stale"] = True
    last = _optional(conn, f"select at, kind, tool, outcome from action_log where {ACTIVITY_VISIBLE}"
                           " order by at desc, id desc limit 1", (now, opening, list(PRE_OPENING_WRITES)))
    return {"halted": halted, "halt_changed_at": flag and flag["updated_at"],
            "halt_changed_by": flag and flag["updated_by"], "latest_run": run,
            "last_action": last, "next_cutoff": next_cutoff(now)}


def _our_slots(conn: psycopg.Connection) -> dict:
    """Our selections as a panel: the stored assignment (`vending.slots.our_slots`), or why there is none.

    Read once per snapshot, and everything the page draws for "ours" comes from it. With nothing
    stored (or a malformed row) the page says so and lights nothing, rather than guess.
    """
    try:
        return {"ok": True, "data": _guarded(conn, lambda: our_slots(conn))}
    except Missing as error:
        return {"ok": False, "why": f"{error}. Run `python -m vending.db migrate` on this database."}
    except SlotsNotRecorded as error:
        return {"ok": False, "why": str(error)}


def _wildcard(conn: psycopg.Connection, ours: tuple[str, ...]) -> str | None:
    """The wildcard slot (config `wildcard_slot`, set once, #166), when it is one of ours; else None.
    A wildcard the stored assignment doesn't give us is never marked."""
    row = _optional(conn, "select value from config where key = 'wildcard_slot'")
    value = row["value"] if row else None
    return value if isinstance(value, str) and value in ours else None


def our_selections(snap: dict) -> tuple[str, ...]:
    """Our selections as the snapshot read them; empty when no assignment is stored."""
    ours = snap.get("ours") or {}
    return tuple(ours["data"]) if ours.get("ok") else ()


def _plan(conn: psycopg.Connection, ours: tuple[str, ...], now: datetime,
          assignment_at: datetime | None) -> dict | None:
    columns = "id, started_at, finished_at, mode, model, status, plan, team_update, notes"
    found = _rows(conn, f"select {columns} from agent_runs where plan is not null and mode = 'live'"
                        " and started_at <= %s order by started_at desc, id desc limit 1", (now,))
    if not found:
        return None
    latest = found[0]
    previous = _optional(conn, f"select {columns} from agent_runs where plan is not null and mode = 'live'"
                               " and (started_at, id) < (%s, %s)"
                               " order by started_at desc, id desc limit 1",
                         (latest["started_at"], latest["id"]))
    now_slots = _by_slot(latest["plan"])
    before = _by_slot(previous["plan"]) if previous else {}
    # One row per selection assigned to us now, in the assignment's order. A stored plan can name
    # selections that are no longer ours (or the assignment can be missing): those aren't drawn.
    slots = [{"slot": slot, "now": now_slots.get(slot), "before": before.get(slot),
              "changed": previous is not None and _what(now_slots.get(slot)) != _what(before.get(slot))}
             for slot in ours]
    plan = latest["plan"] if isinstance(latest["plan"], dict) else {}
    reason, update, notes = plan.get("reason"), latest["team_update"], latest["notes"]
    if _older_assignment(latest["started_at"], assignment_at):
        reason = OLD_ASSIGNMENT
        update = notes = None
    for side, run in (("now", latest), ("before", previous)):
        if run and _older_assignment(run["started_at"], assignment_at):
            for row in slots:
                if row[side] and row[side].get("reason"):
                    row[side] = {**row[side], "reason": OLD_ASSIGNMENT}
    return {"run": {k: latest[k] for k in ("id", "started_at", "finished_at", "mode", "model", "status")},
            "previous_run": previous and previous["id"],
            "visit": plan.get("visit"), "reason": reason, "update": update, "notes": notes, "slots": slots}


_FILE_REFERENCE = re.compile(r"(?i)\bfile:|(?<!\w)[a-z]:[\\/]|\\\\|(?<![\w:/])/(?:[^/\s]+/)")


def _plan_text(value: str | None) -> str | None:
    """The plan's display text can cite a local file. Omit that text from the page and JSON."""
    if value is None:
        return None
    text = str(value)
    return "(local file reference omitted)" if _FILE_REFERENCE.search(text) else text


def _first_fill(conn: psycopg.Connection, ours: tuple[str, ...], assignment_at: datetime | None) -> dict | None:
    with conn.cursor(row_factory=dict_row) as cur:
        # Use the same JSON decoder as the laptop-file loader.
        set_json_loads(json.loads, context=cur)
        row = cur.execute("select id, loaded_at, label, plan, projection, slots, picked from first_fill_plans"
                          " order by picked desc, loaded_at desc, id desc limit 1").fetchone()
    if row is None:
        return None
    current = {"id": row["id"], "picked": row["picked"], "slots": []}
    if not ours:
        return {**current, "message": NO_ASSIGNMENT}
    if set(row["slots"]) != set(ours):
        return {**current, "message": PLAN_ASSIGNMENT_CHANGED}
    checked = plan_rounds.parse_round(row["label"], row["plan"], row["projection"], ours, saved_at=row["loaded_at"])
    older = _older_assignment(row["loaded_at"], assignment_at)
    return {**current, "fill_expected_cents": checked.fill_expected_cents,
            "fill_most_cents": checked.fill_most_cents, "fill_cap_cents": plan_rounds.FILL_CAP_CENTS,
            "slots": [{"slot": s.slot, "sku": s.sku, "product": _plan_text(s.product), "price_cents": s.price_cents,
                       "units": s.units, "reason": OLD_ASSIGNMENT if older and s.why else _plan_text(s.why),
                       "fill_check": s.fill_check,
                       "checks": {"floor_cents": s.floor_cents, "legal_price": s.legal_price,
                                  "capacity": s.capacity, "capacity_assumed": s.capacity_assumed,
                                  "expected_fill_share": _dollars(s.expected_cents),
                                  "max_fill_share": _dollars(s.most_cents),
                                  "refusals": [] if older else [_plan_text(message) for message in s.refusals]}}
                      for s in checked.slots.values()]}


def _tags(conn: psycopg.Connection, now: datetime) -> dict:
    """The price on each slot's tag now, from the latest poll; empty when there is none."""
    try:
        return {slot: s["price"] for slot, s in _sales(conn, now)["slots"].items()}
    except Exception:   # the Sales panel reports its own failure
        return {}


def _runs(conn: psycopg.Connection, now: datetime, tags: dict | None = None,
          ours: tuple[str, ...] = (), *, modes: tuple[str, ...] = ("live",)) -> list[dict]:
    """The last runs, newest first, each with the slots its plan changed from the plan before it.

    The newest run with a plan compares a "keep" against `tags`, the prices on the tags now, as
    the Plan panel does; older runs have no record of their tag prices.
    """
    rows = _rows(conn, """
        select r.id, r.started_at, r.finished_at, r.mode, r.model, r.status, r.detail, r.cost_usd,
               r.request_id, r.team_update, r.notes, r.plan,
               (select p.plan from agent_runs p
                 where p.mode = r.mode and p.plan is not null
                   and (p.started_at, p.id) < (r.started_at, r.id)
                 order by p.started_at desc, p.id desc limit 1) as previous_plan
          from agent_runs r where r.mode = any(%s) and r.started_at <= %s
         order by r.started_at desc, r.id desc limit %s""", (list(modes), now, RUN_ROWS))
    steps = _steps(conn, [r["id"] for r in rows])
    newest = True
    for r in rows:
        r["steps"] = steps.get(r["id"], [])
        plan, previous = r.pop("plan"), r.pop("previous_plan")
        r["has_plan"] = plan is not None
        r["compared"] = plan is not None and previous is not None
        r["changes"] = _plan_changes(plan, previous, tags if newest else None, ours) if r["compared"] else []
        newest = newest and plan is None
        r["stale"] = r["status"] == "running" and now - r["started_at"] > STALE_RUN
    return rows


def _steps(conn: psycopg.Connection, run_ids: list[int]) -> dict[int, list[dict]]:
    """Each run's recorded steps, oldest first. None before migration 014 adds `agent_steps`."""
    if not run_ids:
        return {}
    try:
        found = _rows(conn, "select run_id, turn, kind, tool, content, is_error from agent_steps"
                            " where run_id = any(%s) order by run_id, id", (run_ids,))
    except Missing:
        return {}
    out: dict[int, list[dict]] = {}
    for s in found:
        out.setdefault(s.pop("run_id"), []).append(s)
    return out


def _plan_changes(plan: Any, previous: Any, tags: dict | None = None, ours: tuple[str, ...] = ()) -> list[dict]:
    """Each of our slots whose ask moved between two plans, with short labels for how, in `ours`' order.

    Only selections assigned to us now: a slot an older plan named but that isn't ours any more
    isn't listed.
    """
    tags = tags or {}
    now_slots, before = _by_slot(plan), _by_slot(previous)
    out = []
    for slot in ours:
        if _what(now_slots.get(slot)) != _what(before.get(slot)):
            labels = (_changes(now_slots.get(slot), before.get(slot), tags.get(slot)) if slot in now_slots
                      else ["dropped"])
            out.append({"slot": slot, "labels": labels})
    return out


def _by_slot(plan: Any) -> dict[str, dict]:
    """The plan's slot entries by slot. Tolerates a plan that isn't the expected shape."""
    if not isinstance(plan, dict) or not isinstance(plan.get("slots"), list):
        return {}
    return {str(e["slot"]): e for e in plan["slots"] if isinstance(e, dict) and "slot" in e}


def _what(entry: dict | None) -> tuple:
    """What a slot entry asks for, without its reason, so a reworded reason isn't a change."""
    if entry is None:
        return ()
    def norm(k: str, v: Any) -> str:
        if k == "sale_price" and v is not None:     # "2.5" and "2.50" are the same price
            try:
                return str(Decimal(str(v)).normalize())
            except ArithmeticError:
                pass
        return json.dumps(v, sort_keys=True, default=str)
    return tuple(sorted((k, norm(k, v)) for k, v in entry.items() if k != "reason"))


def _sales(conn: psycopg.Connection, now: datetime) -> dict:
    """Price and units sold per slot from the stored polls, as `get_machine_state` reports them."""
    reply = _guarded(conn, lambda: machine_state.get_machine_state(conn, now))
    if not reply.success:
        return {"as_of": None, "stale": False, "window_start": None, "slots": {}}
    return {"as_of": reply.as_of, "stale": reply.stale, "window_start": reply.window_start,
            "slots": {s.slot: {"price": s.price, "sold": s.units_sold_in_window, "per_day": s.units_per_day,
                               "per_class_day": s.units_per_class_day, "rate_basis": s.rate_basis}
                      for s in reply.data}}


def _stock(conn: psycopg.Connection, now: datetime) -> dict:
    """What each slot and the closet hold, from fulfilled receipts and polls (`vending.stock`).

    "sold" still counts units sold across a product change that can't be split
    (`SlotCount.unattributed`) as the slot's, as before #172; labelling them is
    #172 item 4."""
    found = _guarded(conn, lambda: stock_mod.stock(conn, until=now))
    return {"slots": {s.slot: {"product": s.product, "on_hand": s.units, "loaded": s.loaded,
                               "sold": s.sold + s.unattributed,
                               "lost": s.lost, "since": s.since, "last_moved": s.last_moved,
                               "unit_cost": _dollars(s.unit_cost),
                               # Exact cents per unit ("185/2"), for the pricing panel's floor.
                               "unit_cost_cents": None if s.unit_cost is None else str(s.unit_cost)}
                      for s in found.slots},
            "closet": [{"product": c.product, "units": c.units, "unit_cost": _dollars(c.unit_cost)}
                       for c in found.closet],
            "warnings": list(found.warnings)}


def _closet_only(conn: psycopg.Connection) -> dict:
    """The stock panel when no assignment is readable: no slot is ours, but the closet isn't a selection,
    so it is still counted from the receipts (`vending.stock.closet`), never shown as empty."""
    found = _guarded(conn, lambda: stock_mod.closet(conn))
    return {"slots": {}, "closet": [{"product": c.product, "units": c.units, "unit_cost": _dollars(c.unit_cost)}
                                    for c in found],
            "warnings": []}


def _dollars(cents: Fraction | None) -> str | None:
    """Exact cents as dollars text, rounded half up to the cent. Never a float."""
    if cents is None:
        return None
    with localcontext() as ctx:
        ctx.prec = 40
        value = Decimal(cents.numerator) / Decimal(cents.denominator) / 100
    return str(value.quantize(Decimal("0.01"), ROUND_HALF_UP))


def _purchase_rows(conn: psycopg.Connection, opening: datetime | None, now: datetime) -> tuple[list[dict], int]:
    rows = _rows(conn, """select request_id, submitted_at, off_cycle, status, total_paid, decision_note,
                                 fulfilled_at, updated_at
                          from purchase_requests order by submitted_at desc, request_id desc""")
    receipts: dict[str, list[dict]] = {}
    if opening is not None and any(r["status"] == "fulfilled" for r in rows):
        for line in _guarded(conn, lambda: stock_mod.receipt_lines(conn)):
            receipts.setdefault(line["request_id"], []).append(line)
    visible, hidden = [], 0
    for row in rows:
        status = row["status"]
        fulfilled_at = row.pop("fulfilled_at")
        updated_at = row.pop("updated_at")
        lines = receipts.get(row["request_id"], [])
        live_lines = [line for line in lines if line["at"] is not None and opening <= line["at"] <= now]
        if status == "fulfilled":
            decision_at = min((line["at"] for line in lines if line["at"] is not None),
                              default=fulfilled_at or row["submitted_at"])
            show = bool(live_lines) if lines else (
                opening is not None and fulfilled_at is not None and opening <= fulfilled_at <= now)
        else:
            decision_at = updated_at or row["submitted_at"]
            show = status == "pending" or (opening is not None and opening <= decision_at <= now)
        if show:
            if live_lines and len(live_lines) < len(lines):
                row["total_paid"] = sum((line["total_paid"] or 0 for line in live_lines), Decimal(0))
            visible.append({**row, "lines": len(live_lines),
                            "units_loaded": sum(line["units_loaded"] or 0 for line in live_lines),
                            "unavailable": sum(line["outcome"] == "unavailable" for line in live_lines),
                            "alternates": sum(line["outcome"] == "alternate" for line in live_lines)})
        elif decision_at <= now and (opening is None or decision_at < opening):
            hidden += 1
    return visible, hidden


def _spend(conn: psycopg.Connection, now: datetime, opening: datetime, purchases: list[dict]) -> dict:
    model = _rows(conn, """
        select count(*) as runs, coalesce(sum(cost_usd), 0) as total,
               coalesce(sum(cost_usd) filter (where started_at > %s), 0) as last_7_days,
               count(*) filter (where cost_usd is null and status <> 'running') as unpriced
          from agent_runs where mode = 'live' and started_at between %s and %s""",
                  (now - timedelta(days=7), opening, now))[0]
    fulfilled = [r for r in purchases if r["status"] == "fulfilled"]
    drinks = {"paid": sum((r["total_paid"] or 0 for r in fulfilled), Decimal(0)), "fulfilled": len(fulfilled),
              "pending": sum(r["status"] == "pending" for r in purchases),
              "midweek": sum(r["off_cycle"] and r["status"] != "rejected" for r in purchases)}
    return {"model": model, "drinks": drinks}


def _money_panel(conn: psycopg.Connection, now: datetime) -> dict:
    """The money panel's figures. The ledger replays every poll and receipt, so it is the read most
    likely to pass the public page's statement timeout. That cancels only its own savepoint, so it
    costs this panel, not the page. Other panels don't catch it yet: a cancelled read there still
    fails the whole snapshot."""
    try:
        data = _guarded(conn, lambda: money_panel.read(conn, now))
        if "notes" in data:
            data["notes"] = [note for note in data["notes"] if not note.endswith("development spend, not in the balance")]
        return data
    except psycopg.errors.QueryCanceled as error:
        raise Unavailable("reading the ledger took too long and was stopped; the rest of the page is "
                          "current, and the next refresh tries again") from error


def _recent_calls(conn: psycopg.Connection, now: datetime, opening: datetime | None) -> dict:
    rows = _rows(conn, f"select kind, outcome, count(*) as n from action_log where at > %s and {ACTIVITY_VISIBLE}"
                       " group by kind, outcome", (now - RECENT, now, opening, list(PRE_OPENING_WRITES)))
    counts = {"calls": 0, "writes": 0, "refused": 0, "failed": 0}
    for r in rows:
        counts["calls"] += r["n"]
        counts["writes"] += r["n"] if r["kind"] == "write" else 0
        if r["outcome"] in ("refused", "failed"):
            counts[r["outcome"]] += r["n"]
    return counts


def snapshot(conn: psycopg.Connection, now: datetime | None = None, public: bool = False,
             syncs: dict | None = None) -> dict:
    """Everything the page shows, from one consistent moment. Reads only.

    JSON numbers come back as Decimal, so a price stored as a number stays exact.
    The live view has the same filters on every route. On the public page, panel read errors
    go to stderr. `syncs` is the worker's latest sync round, as its `/health` shows
    it (`vending.worker.State.public`: outcomes and exit codes, never error text). The worker
    keeps that in memory, not in the database, so only a page the worker serves has it; run
    on its own, the dashboard has None and says nothing about the syncs.
    """
    set_json_loads(partial(json.loads, parse_float=Decimal), context=conn)
    with conn.transaction():
        conn.execute("set transaction isolation level repeatable read, read only")
        # Event timestamps come from Postgres. Use its clock for the same transaction's cutoff.
        now = now or conn.execute("select transaction_timestamp()").fetchone()[0]
        snap, assignment_at = _snapshot(conn, now)
    snap["syncs"] = syncs
    hide_errors(snap, public=public)
    only_ours(snap, assignment_at)
    snap["attention"] = attention(snap)
    return snap


# Shown in place of a failure's text on a public page.
HIDDEN = "(error text isn't shown on this public page; it is in the database and the container's log)"
PUBLIC_PANEL_ERROR = "This part of the page can't be read right now."


def hide_errors(snap: dict, *, public: bool = False) -> None:
    """Replace the free text of every failure with `HIDDEN`, in place.

    A failed tool call or run stores the raw error. A refusal keeps its text. On the public
    page, a panel's read error goes to stderr. Clean the snapshot before building Needs
    attention so both the page and `/state.json` get the same text.
    """
    runs = snap["runs"]["data"] if snap["runs"]["ok"] else []
    for run in (snap["status"].get("latest_run"), *runs):
        if not run or not (detail := run.get("detail")):
            continue
        if run["status"] == "failed":
            run["detail"] = HIDDEN
        elif public and run["status"] == "submitted" and detail.startswith("Submitted purchase request "):
            run["detail"] = detail.partition("; afterwards:")[0]
    if snap["activity"]["ok"]:
        for row in snap["activity"]["data"]:
            if row["outcome"] == "failed" and row["detail"]:
                row["detail"] = HIDDEN
    for run in runs:
        for step in run["steps"]:
            if step["kind"] == "tool_result" and step["is_error"]:
                step["content"] = HIDDEN
    if public:
        for name, panel in snap.items():
            if not isinstance(panel, dict) or panel.get("ok") is not False or "why" not in panel:
                continue
            why = panel["why"]
            if why != PUBLIC_PANEL_ERROR:
                print(f"dashboard: {name}: {why}", file=sys.stderr, flush=True)
                panel["why"] = PUBLIC_PANEL_ERROR


OLD_ASSIGNMENT = "Text omitted from before the current slot assignment."
OUTSIDE_ASSIGNMENT = "Tool payload omitted for a selection outside the current slot assignment."
POLL_REPLY_OMITTED = "Saved poll reply omitted because its selections could not be scoped."
NO_ASSIGNMENT = "Our slot assignment is not available."
PLAN_ASSIGNMENT_CHANGED = "planned for an earlier slot assignment: load a new plan"
ASSIGNMENT_RECORDED = "Slot assignment recorded. See the current assignment on this page."
_ASSIGNMENT_NOTICES = frozenset({OLD_ASSIGNMENT, OUTSIDE_ASSIGNMENT, POLL_REPLY_OMITTED, NO_ASSIGNMENT,
                                PLAN_ASSIGNMENT_CHANGED, ASSIGNMENT_RECORDED, RECORD_HINT})
_DROP = object()
_SLOT_KEYS = {"slot", "selection", "into_slot", "wildcard_slot"}
_SLOT_LISTS = {"slots", "selections", "assigned_slots", "our_slots"}
_POLL_REPLY_TOOLS = {"get_account_balance", "get_briefing", "get_machine_state", "get_pnl", "get_sales_history"}
_POLL_ROW_FIELDS = {"slot", "product", "price", "units", "revenue", "revenue_ex_tax",
                    "lifetime_units", "units_sold_in_window", "units_sold_since_load", "units_per_day",
                    "stock_on_hand", "effective_stock", "recent_units"}


def _older_assignment(at: datetime, assignment_at: datetime | None) -> bool:
    return assignment_at is None or at < assignment_at


def _scope_payload(value: Any, ours: set[str], key: str = "") -> Any:
    """Drop structured records naming an unassigned selection, including nested tool arguments and results."""
    if isinstance(value, list):
        items = [item for item in value
                 if key not in _SLOT_LISTS or isinstance(item, (dict, list)) or str(item) in ours]
        return [item for item in (_scope_payload(item, ours) for item in items) if item is not _DROP]
    if not isinstance(value, dict):
        return value
    setting = value.get("key")
    if isinstance(setting, str) and setting in _SLOT_KEYS and "value" in value and str(value["value"]) not in ours:
        return _DROP
    if any(str(value[k]) not in ours for k in _SLOT_KEYS & value.keys() if value[k] is not None):
        return _DROP
    scoped = {}
    for k, v in value.items():
        if len(k) == 2 and k[0] in "12345" and k[1] in "12345678" and k not in ours:
            continue
        kept = _scope_payload(v, ours, k)
        if kept is not _DROP:
            scoped[k] = kept
    if scoped != value:
        # A summary can describe a selection dropped from the structured part.
        for field in ("message", "detail", "reason", "note", "text", "warnings", "directive", "decision_note", "notes"):
            scoped.pop(field, None)
    return scoped


def _scope_poll_reply(value: Any, ours: set[str]) -> Any:
    """Keep per-slot figures from structured replies. Saved prose and totals may mix selections."""
    if not isinstance(value, dict):
        return _DROP
    for key in ("structuredContent", "result"):
        if key in value:
            value = value[key]
            if not isinstance(value, dict):
                return _DROP
    rows = value.get("data")
    if isinstance(rows, dict):   # get_briefing wraps its per-slot figures in the briefing
        rows = rows.get("slots")
    if not isinstance(rows, list):
        return _DROP
    return {"data": [{key: value for key, value in row.items()
                      if key in _POLL_ROW_FIELDS and not isinstance(value, (dict, list))}
                     for row in rows if isinstance(row, dict) and str(row.get("slot")) in ours]}


def _outside_assignment(text: str, ours: set[str]) -> bool:
    return text not in _ASSIGNMENT_NOTICES and bool(stock_mod._slots_named(text) - ours)


def _scope_text(value: Any, ours: set[str]) -> Any:
    """Redact free text in the display copy, leaving values such as prices and slot codes typed."""
    if isinstance(value, str):
        return OUTSIDE_ASSIGNMENT if _outside_assignment(value, ours) else value
    if isinstance(value, (list, tuple)):
        items = (_scope_text(item, ours) for item in value
                 if not isinstance(item, str) or not _outside_assignment(item, ours))
        return tuple(items) if isinstance(value, tuple) else list(items)
    if isinstance(value, dict):
        scoped = {}
        for key, item in value.items():
            # A payload can use prose as a map label. Its value belongs to that label.
            if isinstance(key, str) and " " in key and _outside_assignment(key, ours):
                continue
            # These values name the audit tool and the configuration field, respectively.
            if (key == "tool" and item == "slots.record") or (
                    key == "key" and isinstance(item, str) and item in _SLOT_KEYS):
                scoped[key] = item
            else:
                scoped[key] = _scope_text(item, ours)
        return scoped
    return value


def only_ours(snap: dict, assignment_at: datetime | None = None) -> None:
    """Scope the live dashboard to the current assignment before either response is built."""
    ours = set(our_selections(snap))
    if snap["counts"]["ok"]:
        snap["counts"]["data"] = {s: c for s, c in snap["counts"]["data"].items() if s in ours}
    if snap["stock"]["ok"]:
        stock = snap["stock"]["data"]
        stock["slots"] = {s: v for s, v in stock["slots"].items() if s in ours}
        # Only warnings about our selections or the closet (`vending.stock.warning_is_ours`).
        stock["warnings"] = [w for w in stock["warnings"] if stock_mod.warning_is_ours(w, ours)]
    if (sales := snap.get("sales")) and sales["ok"]:
        slots = sales["data"]["slots"]
        sales["data"]["slots"] = {s: v for s, v in slots.items() if s in ours}
    if (runs := snap.get("runs")) and runs["ok"]:
        for run in runs["data"]:
            if _older_assignment(run["started_at"], assignment_at):
                if any(run[k] for k in ("detail", "team_update", "notes", "steps")):
                    run["detail"] = HIDDEN if run["status"] == "failed" else OLD_ASSIGNMENT
                run["team_update"] = run["notes"] = None
                run["steps"] = []
            run.update(_scope_text({key: value for key, value in run.items() if key != "steps"}, ours))
            for step in run["steps"]:
                if step["kind"] not in ("tool_call", "tool_result") or step["is_error"]:
                    step["content"] = _scope_text(step["content"], ours)
                    continue
                poll_reply = step["kind"] == "tool_result" and step["tool"] in _POLL_REPLY_TOOLS
                try:
                    value = json.loads(step["content"], parse_float=Decimal)
                except ValueError:
                    if poll_reply:
                        step["content"] = POLL_REPLY_OMITTED
                    elif step["content"].lstrip().startswith(("{", "[")):
                        step["content"] = "Incomplete tool payload omitted."
                    else:
                        step["content"] = _scope_text(step["content"], ours)
                    continue
                scoped = _scope_poll_reply(value, ours) if poll_reply else _scope_payload(value, ours)
                if scoped is _DROP:
                    step["content"] = POLL_REPLY_OMITTED if poll_reply else OUTSIDE_ASSIGNMENT
                else:
                    scoped = _scope_text(scoped, ours)
                    if scoped != value:
                        step["content"] = to_json(scoped)
    if (activity := snap.get("activity")) and activity["ok"]:
        for row in activity["data"]:
            if _older_assignment(row["at"], assignment_at):
                if row["arguments"] or row["detail"]:
                    row["arguments"] = {}
                    row["detail"] = HIDDEN if row["outcome"] == "failed" else OLD_ASSIGNMENT
            else:
                args = _scope_payload(row["arguments"], ours)
                if args != row["arguments"]:
                    row["arguments"] = {} if args is _DROP else args
                    row["detail"] = HIDDEN if row["outcome"] == "failed" else OUTSIDE_ASSIGNMENT
                elif (row["kind"] == "read" and row["tool"] in _POLL_REPLY_TOOLS
                      and row["detail"] and row["detail"] != HIDDEN):
                    row["detail"] = POLL_REPLY_OMITTED
            # The assignment audit writer includes both the old and the new slot lists.
            if row["tool"] == "slots.record":
                row["detail"] = HIDDEN if row["outcome"] == "failed" else ASSIGNMENT_RECORDED
    run = snap.get("status", {}).get("latest_run")
    if run and run["detail"] and run["detail"] != HIDDEN and _older_assignment(run["started_at"], assignment_at):
        run["detail"] = OLD_ASSIGNMENT
    for name, panel in snap.items():
        if name not in ("runs", "pricing") or not panel["ok"]:
            snap[name] = _scope_text(panel, ours)
    if (pricing := snap.get("pricing")) and pricing["ok"]:
        for row in pricing["data"]["rows"]:
            for price in (row, row["next"]):
                if price:
                    # Price checks are generated from our own figures and fixed messages.
                    fields = {key: value for key, value in price.items() if key not in ("next", "problems")}
                    price.update(_scope_text(fields, ours))


def _catalog_counts(catalog: dict) -> dict:
    """Catalog requests by status, and the open ones against the bridge's cap, from the rows read.

    Open means `pending`: filed, and not yet approved or rejected. A `sending` or `unknown` row
    may or may not be open on the bridge too, so the places left may be fewer by that many.
    """
    if not catalog["ok"]:
        return catalog
    by_status = dict.fromkeys(CATALOG_STATUSES, 0)
    for r in catalog["data"]:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    open_ = by_status["pending"]
    return {"ok": True, "data": {"total": len(catalog["data"]), "by_status": by_status, "open": open_,
                                 "cap": OPEN_REQUEST_CAP, "left": max(OPEN_REQUEST_CAP - open_, 0),
                                 "unsettled": by_status["sending"] + by_status["unknown"]}}


def _snapshot(conn: psycopg.Connection, now: datetime) -> tuple[dict, datetime | None]:
    ours = _our_slots(conn)
    slots = tuple(ours["data"]) if ours["ok"] else ()
    assigned = _optional(conn, "select first_read_at from slot_assignment where id = 1") if slots else None
    assignment_at = assigned["first_read_at"] if assigned else None
    try:
        opening = _guarded(conn, lambda: stock_mod.opening_at(conn))
    except Missing:
        opening = None   # A partial migration cannot establish the live boundary.
    live_since = opening if opening is not None and opening <= now else None
    purchases = _panel(lambda: _purchase_rows(conn, live_since, now))
    comparison = "<" if live_since else "<="
    model_test_count = _optional(conn, f"select count(*) as n from agent_runs where mode in ('dry-run', 'replay')"
                                       f" and started_at {comparison} %s",
                                 (live_since or now,))
    catalog = _panel(lambda: _rows(
        conn, "select id, requested_at, name, size, status, sku, decision_note"
              " from product_requests order by requested_at desc, id desc"))
    if live_since is None:
        spend = {"ok": True, "data": None}
    elif purchases["ok"]:
        spend = _panel(lambda: _spend(conn, now, live_since, purchases["data"][0]))
    else:
        spend = {"ok": False, "why": "No live spend available."}
    snap = {
        "as_of": now,
        "ours": ours,
        "wildcard": _wildcard(conn, slots),
        "status": _status(conn, now, live_since),
        "plan": _panel(lambda: _plan(conn, slots, now, assignment_at)),
        "runs": _panel(lambda: _runs(conn, now, _tags(conn, now), slots)),
        "activity": _panel(lambda: _rows(
            conn, f"select id, at, kind, tool, outcome, detail, arguments from action_log where {ACTIVITY_VISIBLE}"
                  " order by at desc, id desc limit %s", (now, live_since, list(PRE_OPENING_WRITES), ACTIVITY_ROWS))),
        "recent_calls": _panel(lambda: _recent_calls(conn, now, live_since)),
        "sales": _panel(lambda: _sales(conn, now)),
        "stock": (_panel(lambda: _stock(conn, now) if ours["ok"] else _closet_only(conn)) if live_since
                  else {"ok": True, "data": {"slots": {}, "closet": [], "warnings": []}}),
        "counts": (_panel(lambda: _guarded(conn, lambda: slot_counts.latest(conn))) if ours["ok"] else
                   {"ok": True, "data": {}}),
        "spend": spend,
        "visits": _panel(lambda: _isolated(conn, lambda: visits.counts(conn, now))),
        "money": _panel(lambda: _money_panel(conn, now)),
        "leaderboards": _panel(lambda: _isolated(conn, lambda: leaderboards.read(conn, now))),
        "purchase_requests": ({"ok": True, "data": purchases["data"][0][:REQUEST_ROWS]}
                              if purchases["ok"] else purchases),
        "catalog_requests": catalog,
        "catalog_counts": _catalog_counts(catalog),
    }
    try:
        before_lock = now < lock_at()
    except settings.SettingsError:
        # The assignment panel explains the bad setting. Keep the weekly view available.
        before_lock = False
    if before_lock:
        snap["weekly_plan"] = snap["plan"]
        snap["plan"] = _panel(lambda: _isolated(conn, lambda: _first_fill(conn, slots, assignment_at)))
    hidden_runs = model_test_count["n"] if model_test_count else 0
    hidden_purchases = purchases["data"][1] if purchases["ok"] else 0
    if hidden_runs or hidden_purchases:
        snap["runs"]["hidden_tests"] = {"runs": hidden_runs, "purchases": hidden_purchases}
    snap["products"] = _products(conn, machine_art.lane_products(snap, slots))
    _check_plan_fills(snap)
    snap["pricing"] = _pricing(conn, now, snap)
    return snap, assignment_at


def _pricing(conn: psycopg.Connection, now: datetime, snap: dict) -> dict:
    """The pricing panel (`vending.pricing_panel`): its own reads (our selections, their sourcing, the
    ceilings, the price history) isolated like an optional panel, then built from the sales, stock and plan
    panels above. The reader finds our selections itself, in this same snapshot."""
    try:
        return {"ok": True, "data": pricing_panel.build(snap, _isolated(conn, lambda: pricing_panel.read(conn, now)), now)}
    except Exception as error:  # noqa: BLE001 - nothing in the pricing panel may take the page down
        # The database's text names tables (and can name the host), so it goes to the log, not the page or JSON.
        cause = error.__cause__ if isinstance(error, (Missing, Unavailable)) and error.__cause__ else error
        first = (str(cause).strip().splitlines() or [""])[0]
        print(f"dashboard: the pricing panel couldn't be read: {type(cause).__name__}: {first}",
              file=sys.stderr, flush=True)
        return {"ok": False, "why": PRICING_FAILED}


# Shown when the pricing panel's reads fail, in place of the error's text (on every page, public or not).
PRICING_FAILED = ("the pricing figures couldn't be read just now; the server's log has the detail. If it says a "
                  "table is missing, run `python -m vending.db migrate` on this database")


# Shown when the products read fails, in place of the error's text.
PRODUCTS_FAILED = ("the catalog couldn't be read, so packages are guessed from product names and capacities "
                   "are unknown")


def _products(conn: psycopg.Connection, skus: list[str]) -> dict:
    """The catalog rows and counted capacities for the products our lanes hold now or next, for the
    close-up's package shapes. A failure only costs the drawings their exactness: it rolls back to
    its savepoint, and the page draws packages from their names with unknown capacity."""
    try:
        return _panel(lambda: _guarded(conn, lambda: machine_art.read_products(conn, skus)))
    except (psycopg.Error, ValueError, TypeError, KeyError):
        return {"ok": False, "why": PRODUCTS_FAILED}


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

    ours = snap.get("ours") or {"ok": True}
    if not ours["ok"]:
        add("warn", "Which selections are ours isn't known, so the machine, prices and stock are blank: "
                    + ours["why"])

    sales = snap["sales"]
    if sales["ok"]:
        # Without our assignment the poll reader refuses, which isn't the same as no polls.
        if sales["data"]["as_of"] is None and ours["ok"]:
            add("info", "No machine polls stored yet, so there are no prices or sales. "
                        "Run `python -m vending.sync_history`.")
        elif sales["data"]["stale"]:
            add("warn", f"The latest machine poll is {_span(now - sales['data']['as_of'])} old: "
                        "the history sync may have stopped.")

    if (failing := _failing_syncs(snap.get("syncs"), now)) is not None:
        add("bad", failing)
    if (scheduled := _scheduled_run(snap.get("syncs"))) is not None:
        add(*scheduled)

    catalog = snap["catalog_requests"]
    if catalog["ok"]:
        for r in catalog["data"]:
            if r["status"] in ("sending", "unknown"):
                add("warn", f"Catalog request for {r['name']} {r['size']} is `{r['status']}`: check it on the "
                            "bridge, then settle it with `python -m vending.product_requests resolve`.")

    money = snap.get("money")
    if money and money["ok"]:
        for level, text in money_panel.attention(money["data"]):
            add(level, text)

    plan = snap.get("plan") or {}
    if plan.get("ok") and plan["data"]:
        first_fill = "weekly_plan" in snap
        for row in plan["data"]["slots"]:
            entry = row if first_fill else row.get("now")
            check = row.get("fill_check")
            if entry and check and check != capacity.NOT_COUNTED:
                reason = entry.get("reason")
                add("warn", f"Slot {row['slot']} {check}." + (f" {reason}" if reason else ""))

    stock = snap["stock"]
    if stock["ok"] and stock["data"]["warnings"]:
        n = len(stock["data"]["warnings"])
        add("info", f"{n} stock warning{'s' if n != 1 else ''} from the receipts: {stock['data']['warnings'][0]}"
                    + (" (and more in /state.json)" if n > 1 else ""))

    pricing = snap.get("pricing")
    if pricing and pricing["ok"] and pricing["data"].get("wildcard_stale"):
        add("warn", pricing_panel.WILDCARD_STALE)
    if pricing and pricing["ok"]:
        for r in pricing["data"]["rows"]:
            if r["legal"] is False:
                add("warn", f"Selection {r['slot']}: the shelf price ${r['price']} isn't legal. "
                            + (r["problems"][0] if r["problems"] else "See Pricing."))
            n = r["next"]
            if n and n["legal"] is False:
                add("warn", f"Selection {r['slot']}: the plan's price ${n['price']} isn't legal. "
                            + (n["problems"][0] if n["problems"] else "See Pricing."))

    for key, name in (("plan", "Plan"), ("runs", "Runs"), ("activity", "Tool calls"), ("sales", "Sales"),
                      ("stock", "Stock"), ("purchase_requests", "Purchase requests"),
                      ("catalog_requests", "Catalog requests"), ("money", "Money"),
                      ("leaderboards", "Leaderboards")):
        if key in snap and not snap[key]["ok"]:
            add("info", f"{name} isn't available: {snap[key]['why']}")

    order = {"bad": 0, "warn": 1, "info": 2}
    return sorted(out, key=lambda a: order[a["level"]])


def _failing_syncs(syncs: dict | None, now: datetime) -> str | None:
    """A sentence naming each job of the worker's latest round that didn't exit 0, or None when
    every job passed, no round has run yet, or the worker didn't pass its rounds in.

    A job that crashed, timed out or was still running has no exit code; the worker counts it as
    failing too (`State.healthy`), so it is named with its outcome.
    """
    if not syncs or not syncs.get("rounds"):
        return None
    failed = [r for r in syncs.get("results") or [] if r.get("exit_code") != 0]
    if not failed:
        return None
    jobs = ", ".join(f"`{r.get('job')}` " + (f"exit {r['exit_code']}" if r.get("exit_code") is not None
                                             else str(r.get("outcome") or "no exit code"))
                     for r in failed)
    when = ""
    try:
        when = f" ({_span(now - datetime.fromisoformat(syncs['last_round_at']))} ago)"
    except (KeyError, TypeError, ValueError):
        pass
    codes = sorted({r["exit_code"] for r in failed if r.get("exit_code") in SYNC_EXITS})
    meaning = "".join(f" Exit {c}: {SYNC_EXITS[c]}." for c in codes)
    return f"The worker's latest hourly sync{when} failed: {jobs}.{meaning} See `/health`."


# How Needs attention reads the last scheduled agent run (`vending.agent_schedule`), by its outcome.
SCHEDULED = {"failed": ("bad", "failed"), "missed": ("bad", "was missed: no run started before its latest start"),
             "halted": ("warn", "was halted: the halt switch was on"), "skipped": ("info", "skipped"),
             "submitted": ("info", "submitted a purchase request"), "running": ("info", "is running now")}


def _scheduled_run(syncs: dict | None) -> tuple[str, str] | None:
    """(level, sentence) on the last scheduled agent run, from the worker's `/health` (`agent_schedule`), or
    None when scheduled runs are off or the worker didn't pass its state in. A wrong setting is `bad`: the
    team thinks the agent runs, and it doesn't."""
    schedule = (syncs or {}).get("agent_schedule")
    if not isinstance(schedule, dict):
        return None
    if schedule.get("mode") == "invalid":
        return ("bad", "Scheduled agent runs are off because a setting is wrong (`AGENT_SCHEDULE_MODE`, "
                       "`AGENT_SCHEDULE` or `AGENT_SCHEDULE_FROM`). The container's log says which.")
    last = schedule.get("last")
    if not isinstance(last, dict) or last.get("outcome") not in SCHEDULED:
        return None
    level, what = SCHEDULED[last["outcome"]]
    try:
        due = _eastern(datetime.fromisoformat(last["due_at"]))
        when = f" due {due:%a %d %b}, {due.hour % 12 or 12}:{due:%M %p} ET"
    except (KeyError, TypeError, ValueError):
        when = ""
    kind = "dry run" if last.get("mode") == "dry-run" else "live run"
    run = f" (agent run {int(last['run_id'])})" if isinstance(last.get("run_id"), int) else ""
    see = "" if last["outcome"] in ("running", "submitted") else " `python -m vending.agent_schedule` says why."
    return level, f"The scheduled {kind}{when} {what}{run}.{see}"


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


def _window(sales: dict) -> str:
    """The sales window as words: "the 7 days" when it is full, or how long it is when the polls
    don't reach back that far. It ends at the last poll (`machine_state.WINDOW`)."""
    d = sales["data"] if sales.get("ok") else {}
    start, end = d.get("window_start"), d.get("as_of")
    if not start or not end:
        return "the window"
    span = end - start
    if abs(span - timedelta(days=7)) <= timedelta(hours=1):
        return "the 7 days"
    # A gap in the polls can put the window's start more than 7 days back: then say how long it is.
    return "under a minute" if span < timedelta(minutes=1) else f"the {_span(span)}"


def _time(value: datetime | None, form: str = "abs", now: datetime | None = None) -> str:
    """A <time> the page's script keeps current in the viewer's zone. Server text until it runs.

    `abs` is a date and time, `clock` just the time, `ago` and `until` a span from `now`, and `et`,
    `etclock` and `etday` the time ("2:00 PM ET", "2:00 PM") or the day and time in Eastern, the machine's zone.
    """
    if value is None:
        return "&mdash;"
    utc = value.astimezone(timezone.utc)
    title = f"{utc:%Y-%m-%d %H:%M} UTC"
    if form == "ago" and now is not None:
        text = f"{_span(now - value)} ago"
    elif form == "until" and now is not None:
        text = _span(value - now)
    elif form in ("et", "etclock", "etday"):
        local = _eastern(value)
        eastern = getattr(local.tzinfo, "key", None) == "America/New_York"
        local = local if eastern else utc       # without the IANA database, say UTC rather than guess
        clock = f"{local:%I:%M %p}".lstrip("0") + ("" if form == "etclock" and eastern else " ET" if eastern else " UTC")
        text = (f"{local:%a} {local.day} {local:%b}, " if form == "etday" else "") + clock
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


def _changes(now: dict | None, before: dict | None, tag: Any = None) -> list[str]:
    """Short labels for how a slot's entry moved from the previous plan: swap, $2.50 to $2.75, fill 6 to 8...

    A price counts as changed against the previous plan's price, or, when that plan gave none
    (a "keep"), against `tag`, the price on the tag now; with neither, it reads "set $2.75".
    """
    if now is None:
        return []
    if before is None:
        return ["new"]
    labels = []
    if now.get("fill_to") == 0 and before.get("fill_to") != 0:
        return ["pull"]
    if now.get("product") and now.get("product") != before.get("product"):
        labels.append("swap")
    was = before.get("sale_price") if before.get("sale_price") is not None else tag
    if now.get("sale_price") is not None and was is not None and not _same_price(now["sale_price"], was):
        labels.append(f"${was} to ${now['sale_price']}")
    elif now.get("sale_price") is not None and was is None:
        labels.append(f"set ${now['sale_price']}")   # no earlier price known to compare with
    old, new = before.get("fill_to"), now.get("fill_to")
    if isinstance(old, int) and isinstance(new, int) and new != old:
        labels.append(f"fill {old} to {new}")
    if now.get("keep") and not before.get("keep"):
        labels.append("keep")
    # Leaving "keep as is" for an explicit entry that asks for nothing new says nothing more.
    return labels or (["restock"] if before.get("keep") else ["changed"])


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


def _change_kind(labels: list[str]) -> str:
    """The tone of a slot's change dot: a pull is `bad`, a price change `warn`, a swap or fill `info`."""
    if "pull" in labels or "dropped" in labels:
        return "bad"
    if "swap" in labels or "new" in labels:
        return "info"
    if any(label.startswith("$") or label == "price" for label in labels):
        return "warn"
    return "info"


def _fill(entry: dict | None) -> int | None:
    fill = (entry or {}).get("fill_to")
    return fill if isinstance(fill, int) and not isinstance(fill, bool) else None


def _check_plan_fills(snap: dict) -> None:
    """Check explicit weekly fills against the same catalog and counts as the close-up."""
    plan = snap.get("weekly_plan", snap["plan"])
    if not plan["ok"] or not plan["data"] or not snap["products"]["ok"]:
        return
    products = snap["products"].get("data") or {}
    catalog, overrides = products.get("catalog", {}), products.get("overrides", {})
    view = machine_view(snap)
    for row in plan["data"]["slots"]:
        entry = row.get("now")
        fill = _fill(entry)
        if fill is None or fill <= 0 or entry.get("keep"):
            continue
        product = view[row["slot"]]["next"]["product"]
        limit = capacity.for_product(product, catalog.get(product, {}).get("size"), overrides) if isinstance(product, str) else None
        row["fill_check"] = capacity.fill_check(fill, limit)


def machine_view(snap: dict) -> dict:
    """What "In the machine" draws for each of our slots, Now and Next restock. Reads nothing.

    Now: the price on the tag from the latest poll, and the product and units on hand from the
    receipts (`vending.stock`); each is None when there is no record of it. Next restock: the latest
    plan's product, price and fill, falling back to Now for what the plan leaves as is. `next` is
    None when there is no plan.
    """
    plan, sales, stock = snap.get("weekly_plan", snap["plan"]), snap["sales"], snap["stock"]
    p = plan["data"] if plan["ok"] else None
    planned = {s["slot"]: s for s in p["slots"]} if p else {}
    prices = sales["data"]["slots"] if sales["ok"] else {}
    held = stock["data"]["slots"] if stock["ok"] else {}
    counted = snap["counts"]["data"] if snap.get("counts", {}).get("ok") else {}
    out = {}
    for slot in our_selections(snap):
        h = held.get(slot) or {}
        now = {"product": h.get("product"), "units": h.get("on_hand"),
               "price": (prices.get(slot) or {}).get("price"), "source": "receipt" if h else None,
               "counted_at": None}
        c = counted.get(slot)
        # A person's count fills in what receipts don't know, and replaces them when it is newer
        # than the slot's latest receipt movement (a same-product refill after the count wins).
        moved = h.get("last_moved") or h.get("since")
        if c is not None and (not h or moved is None or c["counted_at"] > moved):
            now.update(product=c["product"], units=c["on_hand"], source=c["source"], counted_at=c["counted_at"])
            if now["price"] is None:
                now["price"] = c["price_on_tag"]
        entry = (planned.get(slot) or {}).get("now")
        nxt = change = None
        if p is not None:
            if entry is None:
                nxt = {"in_plan": False, "product": now["product"], "units": None, "price": None,
                       "old_price": None, "pulled": False, "fill_to": None}
            else:
                keep, fill = bool(entry.get("keep")), _fill(entry)
                price = entry.get("sale_price") if not keep and entry.get("sale_price") is not None else now["price"]
                moved = (price is not None and now["price"] is not None and not _same_price(price, now["price"]))
                nxt = {"in_plan": True,
                       "product": (None if keep else entry.get("product")) or now["product"],
                       "units": now["units"] if keep or fill is None else fill,
                       "price": price, "old_price": now["price"] if moved else None,
                       "pulled": fill == 0 and not keep, "fill_to": fill}
            if planned.get(slot, {}).get("changed"):
                labels = (_changes(entry, planned[slot]["before"], now["price"]) if entry is not None
                          else ["dropped"])
                change = {"labels": labels, "kind": _change_kind(labels), "reason": (entry or {}).get("reason")}
        out[slot] = {"now": now, "next": nxt, "change": change, "fill_to": _fill(entry),
                     "reason": (entry or {}).get("reason"), "wildcard": slot == snap.get("wildcard")}
    return out


def _attention_card(items: list[dict]) -> str:
    """Needs attention, beside the machine while nothing is picked. Never cut short: the side scrolls first."""
    bad = sum(1 for a in items if a["level"] == "bad")
    warn = sum(1 for a in items if a["level"] == "warn")
    badge = (_pill(_plural(bad, "problem"), "bad") if bad else _pill(_plural(warn, "check"), "warn") if warn
             else _pill(_plural(len(items), "note"), "info") if items else _pill("all clear", "ok"))
    return f'<div class="ops-card ops-attn" id="attention"><h3>Needs attention {badge}</h3>{_attention_html(items)}</div>'


def _pick(slot: str, inner: str, view: dict) -> str:
    """A row that picks one of our selections on the machine, or, for one not drawn there, opens its plan row."""
    if slot in view:
        return f'<a class="chg" id="chg-{_e(slot)}" href="{machine_art.pick_href(slot)}" data-sel="{_e(slot)}">{inner}</a>'
    return f'<a class="chg" href="#slot-{_e(slot)}">{inner}</a>'


def _changes_html(snap: dict, view: dict) -> str:
    """The plan's changes, beside the machine while nothing is picked. Before the slot lock, each line of
    the plan for the first fill; from the lock, each slot the latest plan changed, with the agent's
    reason. Each row picks its selection."""
    jump = '<a class="ops-jump" href="#plan">The plan &darr;</a>'
    plan = snap["plan"]
    p = plan["data"] if plan["ok"] else None
    if "weekly_plan" in snap:
        title = "First fill"
        if not plan["ok"]:
            body = _missing(plan)
        elif p is None:
            body = '<p class="dim">No plan for the first fill loaded yet.</p>'
        elif p.get("message"):
            body = f'<p class="dim">{_e(p["message"])}</p>'
        else:
            title += f' <span class="hint">{"picked plan" if p["picked"] else "latest, not picked"}</span>'
            rows = "".join(
                "<li>" + _pick(row["slot"], f'<b>{_e(row["slot"])}</b><span>'
                                            + (f"{WILD} " if row["slot"] == snap.get("wildcard") else "")
                                            + f'{_e(row["product"] or "no product")} '
                                            f'&middot; {plan_rounds.format_cents(row["price_cents"])} &middot; '
                                            f'{_plural(row["units"], "unit")}' + _fill_pill(row.get("fill_check"))
                                            + "</span>", view) + "</li>"
                for row in p["slots"])
            body = f'<ul class="changes ff">{rows}</ul>' if rows else '<p class="dim">The plan has no slots.</p>'
        return f'<div class="ops-card ops-chg" id="plan-changes"><h3>{title}{jump}</h3>{body}</div>'
    if not plan["ok"]:
        return f'<div class="ops-card ops-chg" id="plan-changes"><h3>Plan changes{jump}</h3>{_missing(plan)}</div>'
    if p is None:
        return (f'<div class="ops-card ops-chg" id="plan-changes"><h3>Plan changes{jump}</h3>'
                '<p class="dim">No plan yet.</p></div>')
    run = p["run"]
    head = f'<h3>Plan changes <span class="hint">run #{run["id"]}</span> {_run_pill(run)}{jump}</h3>'
    if not p["previous_run"]:
        body = '<p class="dim">First plan of this mode: nothing earlier to compare with.</p>'
    else:
        items = [(slot, v["change"]) for slot, v in view.items() if v["change"]]
        items += [(s["slot"], {"labels": ["dropped"], "kind": "bad", "reason": None})
                  for s in p["slots"] if s["changed"] and s["slot"] not in view]
        checks = {s["slot"]: s.get("fill_check") for s in p["slots"]}
        rows = "".join(
            "<li>" + _pick(slot, f'<span class="dotk {c["kind"]}"></span><b>{_e(slot)}</b><span>{_e(", ".join(c["labels"]))}'
                                 + (f" {WILD}" if slot == snap.get("wildcard") else "") + _fill_pill(checks.get(slot))
                                 + (f'<span class="why">{_e(c["reason"])}</span>' if c["reason"] else "") + "</span>",
                           view) + "</li>"
            for slot, c in items)
        body = (f'<ul class="changes">{rows}</ul>' if rows else
                f'<p class="dim">No slot changes since run #{p["previous_run"]}.</p>')
    return f'<div class="ops-card ops-chg" id="plan-changes">{head}{body}</div>'


def _fill_pill(check: str | None) -> str:
    """A plan row's under-fill flag (`capacity.fill_check`, #194) as a pill: a warning, or muted when the
    capacity isn't counted."""
    return f" {_pill(check, 'muted' if check == capacity.NOT_COUNTED else 'warn')}" if check else ""


def _fill_checks(snap: dict) -> dict:
    """Each selection's under-fill flag in the plan the machine shows for the next restock (`_check_plan_fills`)."""
    plan = snap.get("weekly_plan", snap["plan"])
    p = plan["data"] if plan["ok"] else None
    return {s["slot"]: s["fill_check"] for s in (p or {}).get("slots", []) if s.get("fill_check")}


def _first_fill_lines(snap: dict) -> dict:
    """Each selection's line in the plan for the first fill, for the close-up's facts. Before the slot lock only."""
    if "weekly_plan" not in snap or not snap["plan"]["ok"]:
        return {}
    p = snap["plan"]["data"]
    if not p or p.get("message"):
        return {}
    return {row["slot"]: {"text": f'{row["product"] or "no product"} · {plan_rounds.format_cents(row["price_cents"])} · '
                                  f'{_plural(row["units"], "unit")}'
                                  + (f' · {row["fill_check"]}' if row.get("fill_check") else ""),
                          "reason": row["reason"]}
            for row in p["slots"]}


def _cabinet_html(snap: dict, items: list[dict] | None = None) -> str:
    """In the machine, as one screen: the machine's whole front on the left (`vending.machine_art`);
    on the right, Needs attention and the plan's changes, or, once one of ours is picked, its shelf
    close up and its facts. On a phone the front comes first and the close-up is a sheet."""
    items = attention(snap) if items is None else items
    plan = snap.get("weekly_plan", snap["plan"])
    now, sales, stock = snap["as_of"], snap["sales"], snap["stock"]
    view = machine_view(snap) if snap["ours"]["ok"] else {}
    face = {f"{r}{c}" for r in range(1, SHELVES + 1) for c in range(1, COLUMNS + 1)}
    shown = {s: v for s, v in view.items() if s in face}
    home = f'<div class="ops-home">{_attention_card(items)}{_changes_html(snap, shown)}</div>'
    if not snap["ours"]["ok"]:
        return ('<section id="vm" class="vm" data-view="now"><h2>In the machine</h2>'
                f'<div class="ops"><div class="ops-front">{_missing(snap["ours"])}</div>'
                f'<div class="ops-side">{home}</div></div></section>')
    p = plan["data"] if plan["ok"] else None
    products = snap.get("products") or {"ok": False, "why": PRODUCTS_FAILED}
    sold, span = {}, "in the last 7 days"
    if sales["ok"]:
        sold = {s: v for s, v in sales["data"]["slots"].items() if s in shown}
        span = _window_words(sales["data"])
    front, panels = machine_art.split_html(shown, products["data"] if products["ok"] else None,
                                           p is not None and "weekly_plan" not in snap, sold, span,
                                           _first_fill_lines(snap), _fill_checks(snap))
    lede = ""
    if p is None:
        lede = '<p class="empty v-next">No plan yet: Next restock shows the machine as it is now.</p>'
    off_face = [s for s in our_selections(snap) if s not in face]
    if off_face:
        lede += f'<p class="sub">Also ours, not on the {SHELVES}&times;{COLUMNS} face: {_e(", ".join(off_face))}</p>'
    if sales["ok"] and sales["data"]["as_of"] is not None:
        polled = f'Prices from the poll {_time(sales["data"]["as_of"], "ago", now)}. '
    elif sales["ok"]:
        polled = "No poll yet, so no prices. "
    else:
        polled = "Prices aren't available. "
    stock_note = "" if stock["ok"] else "Stock isn't available. "
    shapes = "" if products["ok"] else f"Packages are approximate: {PRODUCTS_FAILED}. "
    legend = ('<details class="vm-legend" data-key="vm-legend"><summary><i class="dotk info"></i>swap or fill '
              '<i class="dotk warn"></i>price <i class="dotk bad"></i>pull &middot; '
              '<span class="honest">Stock is estimated from receipts and polls, not a sensor.</span> '
              '<span class="more">How to read it</span></summary>'
              "<p>Grey cells aren't ours: they show their number and nothing else, at half the width. On ours, the "
              "number is units on hand (Now) or after the plan's fill (Next restock), against the column's capacity; "
              "<b>~3</b> is from a photo count, less sales since, so at least that many were seen; <b>?</b> means no "
              "receipt or count yet. The bar is units against capacity: hatched means unknown, outlined means no "
              "capacity counted. The badge is what the next restock does: <b>+N</b> back, swap, new, $ for a new "
              "price, pull. The next restock is almost always the free Friday visit; a paid midweek visit is one too. "
              "In the close-up, dashed slots are empty room and \"+N at restock\" is the plan's fill less the units "
              f"on hand. {polled}{stock_note}{shapes}Packages are drawn from the catalog size; they are not photos.</p>"
              "</details>")
    seg = ('<span class="seg" role="group" aria-label="Show" hidden>'
           '<button type="button" data-view="now" aria-pressed="true">Now</button>'
           '<button type="button" data-view="next" aria-pressed="false">Next restock</button></span>')
    when = _restock(p)
    when = f'<span class="vm-when">Next restock &middot; {_e(when)}</span>' if when else ""
    panel = ('<aside class="vm-panel" aria-label="Close-up of the picked selection">'
             '<div class="vm-ph"><span class="vm-grab" aria-hidden="true"></span><span class="label">Close-up</span>'
             # "?#vm", not "#vm": without scripts closing must load the page without ?sel=, or the timed
             # reload would open the pick again. With scripts the click is handled in place.
             '<a class="vm-x" id="vm-close" href="?#vm" data-close aria-label="Close the close-up">'
             '<span aria-hidden="true">&times;</span> Close</a></div>'
             f'{panels}</aside>')
    scrim = '<a class="vm-scrim" href="?#vm" data-close tabindex="-1" aria-hidden="true"></a>'
    return (f'<section id="vm" class="vm" data-view="now">{machine_art.selection_css(shown)}'
            f'<h2 id="vm-title" tabindex="-1">In the machine <span class="hint">our {len(shown)} selections &middot; tap one to see it close up'
            f'</span>{when}{seg}</h2>{lede}'
            f'<div class="ops"><div class="ops-front">{front}{legend}</div>'
            f'<div class="ops-side">{home}{panel}</div></div>{scrim}</section>')


def _slot_num(slot: str) -> str:
    """A plan row's slot number. One on the machine's face is a link that picks it in the close-up."""
    if re.fullmatch(rf"[1-{SHELVES}][1-{COLUMNS}]", slot):
        return (f'<a class="num" id="num-{_e(slot)}" href="{machine_art.pick_href(slot)}" data-sel="{_e(slot)}" '
                f'aria-label="Selection {_e(slot)}: show it in the machine">{_e(slot)}</a>')
    return f'<b class="num">{_e(slot)}</b>'


def _wild_mark(slot: str, wildcard: str | None) -> str:
    """The wildcard's pill at the start of its plan row's text. Inside the text, not beside the number,
    so the row's columns stay in line."""
    return f"{WILD} " if slot == wildcard else ""


def _fill_check_html(check: str, reason: str | None) -> str:
    tone = "muted" if check == capacity.NOT_COUNTED else "warn"
    return (f'<span class="fill-check">{_pill(check, tone)}'
            + (f' &middot; {_e(reason)}' if reason else "") + "</span>")


def _plan_cell(s: dict, compared: bool, sold: dict | None, held: dict | None,
               window: str = "the window", wildcard: str | None = None) -> str:
    """One slot of the plan as a <details>: what it asks for in the summary; the reason, what it was
    and what the slot holds and sold inside."""
    slot, entry, changed = s["slot"], s["now"], compared and s["changed"]
    price_now = (sold or {}).get("price")
    if entry is None:
        what = '<span class="dim">no change</span>'
    elif entry.get("keep"):
        what = "keep as is"
    else:
        terms = [_e(entry.get("product") or "same product")]
        planned_price = entry.get("sale_price")
        if planned_price is not None and price_now is not None and not _same_price(planned_price, price_now):
            terms.append(f'<s>${_e(price_now)}</s> <b>${_e(planned_price)}</b>')
        elif planned_price is not None:
            terms.append(f"${_e(planned_price)}")
        if "fill_to" in entry:
            terms.append("pull" if entry.get("fill_to") == 0 else f"fill to {_e(entry['fill_to'])}")
        what = " &middot; ".join(terms)
    labels = (_changes(entry, s["before"], price_now) if entry is not None else ["dropped"]) if changed else []
    tags = "".join(_pill(t, _change_tone(t)) for t in labels)
    body = []
    reason = (entry or {}).get("reason")
    check = s.get("fill_check")
    if check:
        what += _fill_check_html(check, reason)
    if not check or not reason:
        body.append(f'<div class="why">{_e(reason)}</div>' if reason else '<div class="dim">No reason given.</div>')
    if changed and s["before"] is not None:
        body.append(f'<div class="was">was: {_entry_text(s["before"])}</div>')
    on_hand = (held or {}).get("on_hand")
    stock_text = f"{on_hand} on hand" if on_hand is not None else "stock unknown"
    sold_n = (sold or {}).get("sold")
    # The window's count is the slot's, not the product's: a swap at a visit inside the 7 days adds the
    # old product's sales to the new one's (#172). Say so, so nobody reads it as this product's demand.
    sales_text = (f"the slot sold {sold_n} in {window} to the last poll, whatever it held" if sold_n is not None
                  else "sales unknown")
    holds = f' of {_e(held["product"])}' if held is not None and held.get("product") else ""
    tag = f"${_e(price_now)} on the tag &middot; " if price_now is not None else ""
    body.append(f'<div class="sub">now: {tag}{stock_text}{holds} &middot; {sales_text}</div>')
    now_bits = [_e(held["product"]) if held is not None and held.get("product") else
                ('<span class="dim">empty</span>' if on_hand == 0 else '<span class="dim">product unknown</span>')]
    if price_now is not None:
        now_bits.append(f"${_e(price_now)}")
    if on_hand is not None:
        now_bits.append(f"{on_hand} left")
    return (f'<details class="pc{" changed" if changed else ""}" id="slot-{_e(slot)}" data-key="slot-{_e(slot)}">'
            f'<summary>{_slot_num(slot)}<span class="now">{" &middot; ".join(now_bits)}</span>'
            f'<span class="what">{_wild_mark(slot, wildcard)}{what}</span><span class="tags">{tags}</span></summary>'
            f'{"".join(body)}</details>')


def _plan_html(snap: dict) -> str:
    plan, sales, stock, now = snap["plan"], snap["sales"], snap["stock"], snap["as_of"]
    if not plan["ok"]:
        return _missing(plan)
    p = plan["data"]
    if p is None:
        return ('<p class="empty">No plan yet. Once the agent loop records a run with '
                '<code>vending.agent_runs</code>, each slot shows what it plans, why, and what changed.</p>')
    sold = sales["data"]["slots"] if sales["ok"] else {}
    held = dict(stock["data"]["slots"]) if stock["ok"] else {}
    # The same "now" as the machine view, so a slot known only from a count agrees in both panels.
    for slot, v in machine_view(snap).items():
        if v["now"]["product"] is not None or v["now"]["units"] is not None:
            held[slot] = {"product": v["now"]["product"], "on_hand": v["now"]["units"]}
    run = p["run"]
    changed = [s for s in p["slots"] if s["changed"]]
    head = (f'<p class="lede">Plan from run <b>#{run["id"]}</b> ({_e(run["mode"])}, '
            f'{_e(run["model"] or "model not recorded")}) {_run_pill(run)} '
            f'started {_time(run["started_at"], "ago", now)}. '
            + (f'<b>{len(changed)} of {len(our_selections(snap)) or len(p["slots"])} slots changed</b> '
               f'since run #{p["previous_run"]}.'
               if p["previous_run"] else "No earlier plan of this mode to compare with.") + "</p>")
    if p["visit"] or p["reason"]:
        head += (f'<p class="lede">Visit: <b>{_e(p["visit"] or "not given")}</b>'
                 + (f' &middot; {_e(p["reason"])}' if p["reason"] else "") + "</p>")
    head += '<p class="honest">A plan is a proposal, not a restock receipt.</p>'
    controls = ""
    if p["previous_run"] and changed:
        chips = "".join(
            f'<a class="pill {_change_tone(lab)}" href="#slot-{_e(s["slot"])}">{_e(s["slot"])} {_e(lab)}</a>'
            for s in changed for lab in (_changes(s["now"], s["before"], (sold.get(s["slot"]) or {}).get("price"))
                                         if s["now"] is not None else ["dropped"]))
        controls = (f'<div class="chips">{chips}</div>'
                    '<label class="only" hidden><input type="checkbox" id="changed-only"> '
                    'Show changed slots only</label>')
    cells = "".join(_plan_cell(s, bool(p["previous_run"]), sold.get(s["slot"]), held.get(s["slot"]), _window(sales),
                               snap.get("wildcard"))
                    for s in p["slots"])
    columns = ('<div class="plan-head" aria-hidden="true"><span>Slot</span><span>Now</span>'
               '<span>Next restock</span><span></span></div>')
    return head + controls + f'<div class="plan-grid" data-only="false">{columns}{cells}</div>'


def _first_fill_cell(row: dict, wildcard: str | None = None) -> str:
    slot, checks = row["slot"], row["checks"]
    check, reason = row.get("fill_check"), row["reason"]
    fill_note = _fill_check_html(check, reason) if check else ""
    reason_detail = f'<div class="why">{_e(reason or "No reason given.")}</div>' if not check or not reason else ""
    floor = checks["floor_cents"]
    legal = checks["legal_price"]
    price = "legal price" if legal else "price refused" if legal is False else "price check unavailable"
    capacity = checks["capacity"]
    cap = f"{capacity} units" if capacity is not None else "unknown"
    if checks["capacity_assumed"]:
        cap += " (assumed)"
    expected, maximum = checks["expected_fill_share"], checks["max_fill_share"]
    cost = f"${expected} expected, ${maximum} at most" if expected is not None else "unknown"
    findings = [f'Floor {plan_rounds.format_cents(floor) if floor is not None else "unknown"}',
                price, f"Capacity {cap}", f"Fill cost share {cost}"]
    problems = "".join(f'<div class="sub">{_e(reason)}</div>' for reason in checks["refusals"])
    return (f'<details class="pc" id="slot-{_e(slot)}" data-key="slot-{_e(slot)}">'
            f'<summary>{_slot_num(slot)}<span class="what">{_wild_mark(slot, wildcard)}{_e(row["product"])}'
            f' &middot; {plan_rounds.format_cents(row["price_cents"])}'
            f' &middot; {_plural(row["units"], "unit")}{fill_note}</span></summary>{reason_detail}'
            f'<div class="was"><span class="label">Code checks</span>'
            f'<div>{" &middot; ".join(_e(finding) for finding in findings)}</div>{problems}</div></details>')


def _plan_section(snap: dict) -> str:
    plan = snap["plan"]
    p = plan["data"] if plan["ok"] else None
    if "weekly_plan" not in snap:
        hint = ("outlined slots changed &middot; open a slot for the agent's reason"
                if p and p["previous_run"] else "open a slot for the agent's reason" if p else "")
        return (f'<section id="plan"><h2>The plan <span class="hint">{hint}</span></h2>'
                + _plan_html(snap) + _words_html(plan) + "</section>")

    hint = ""
    if not plan["ok"]:
        body = _missing(plan)
    elif p is None:
        body = '<p class="empty">No first-fill plan loaded.</p>'
    else:
        hint = "Picked plan" if p["picked"] else "latest, not picked"
        if p.get("message"):
            body = f'<p class="empty">{_e(p["message"])}</p>'
        else:
            cap = plan_rounds.format_cents(p["fill_cap_cents"])
            expected, maximum = p["fill_expected_cents"], p["fill_most_cents"]
            cost = (f"{plan_rounds.format_cents(expected)} expected, {plan_rounds.format_cents(maximum)} at most"
                    if expected is not None and maximum is not None else "unknown")
            hint += f" &middot; Fill cost {cost} against the proposed {cap} cap"
            cells = "".join(_first_fill_cell(row, snap.get("wildcard")) for row in p["slots"])
            body = f'<div class="plan-grid first-fill">{cells}</div>'
    return ('<section id="plan"><h2>Plan for the first fill (stocked ~30 Nov) '
            f'<span class="hint">{hint}</span></h2>{body}</section>')


def _switched(verb: str, at: datetime | None, by: str | None, now: datetime) -> str:
    """`turned off 3 h ago by kevin`, leaving out whichever of when and who isn't recorded."""
    if at is None and not by:
        return ""
    return (verb + (f" {_time(at, 'ago', now)}" if at is not None else "")
            + (f" by {_e(by)}" if by else ""))


# The planner's week and zone, as its `Config` defaults them, so the page reads the cutoff as it does.
_PLANNER_DEFAULTS = {f.name: f.default for f in dataclasses.fields(planner.Config)
                     if f.name in ("week_starts", "timezone")}


def _restock(plan: dict | None) -> str | None:
    """The restock an agent's plan is for, in words. Almost every restock is the free Friday visit:
    "Fri 4 Dec", the Friday the planner would still buy for when the plan's run started. That is the
    planner's own rule (`planner._past_cutoff`, `planner._week_of`): its week turns over on
    Saturday, and the cutoff passes only after the cutoff minute, so a run at 23:59:59 Thursday is
    still for this Friday. A paid midweek visit is a restock too: "midweek visit", since the plan
    records no date for it. None without a plan, or without the time-zone data to place the cutoff."""
    if not plan:
        return None
    if plan.get("visit") == "midweek":
        return "midweek visit"
    try:
        local = plan["run"]["started_at"].astimezone(planner._tz(_PLANNER_DEFAULTS["timezone"]))
    except Exception:   # Windows without the tzdata package
        return None
    week_starts = _PLANNER_DEFAULTS["week_starts"]
    friday = planner._week_of(local, week_starts) + timedelta(days=(4 - week_starts) % 7)
    if planner._past_cutoff(local, IN_STORE_CUTOFF, week_starts):
        friday += timedelta(days=7)
    return f"{friday:%a} {friday.day} {friday:%b}"


def next_sync(now: datetime, syncs: dict | None = None) -> datetime | None:
    """When the worker's next sync starts. The worker says so when it serves the page (`syncs`);
    otherwise it is its schedule: SYNC_AT_MINUTE past the next hour not yet passed. None when the
    worker keeps no schedule (`SYNC_AT_MINUTE=off`)."""
    try:
        due = datetime.fromisoformat(syncs["next_round_at"])
        if due.tzinfo is not None:
            return due
    except (KeyError, TypeError, ValueError):
        pass
    raw = (os.environ.get("SYNC_AT_MINUTE") or str(SYNC_AT_MINUTE)).strip().lower()
    try:
        minute = float(raw)
    except ValueError:
        return None
    if not 0 <= minute < 60:
        return None
    at = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(minutes=minute)
    return at if at > now else at + timedelta(hours=1)


def _window_words(sales: dict) -> str:
    """What the polls' sales cover: 'in the last 7 days', or how long it really is. The bounds are
    `_window`'s: the full window give or take an hour; a gap in the polls can put the start more than
    7 days back, and polls that began recently less, and then the words say its length."""
    start, end = sales.get("window_start"), sales.get("as_of")
    if start is None or end is None:
        return "since the first poll"
    span = end - start
    if abs(span - machine_state.WINDOW) <= timedelta(hours=1):
        return f"in the last {machine_state.WINDOW.days} days"
    return "in under a minute" if span < timedelta(minutes=1) else f"in the last {_span(span)}"


def _cell(label: str, value: str, sub: str, href: str, tone: str = "", link: str = "") -> str:
    """One cell of the status bar: a label, one value and a short line. The cell links to the section
    with the rest. With a `link` of its own (a chart), the cell is a box and only its label links to the
    section, since a link can't hold another."""
    cls = f'sc{" " + tone if tone else ""}'
    if link:
        return (f'<div class="{cls}"><a class="label" href="{href}">{label}</a>'
                f'<span class="sv">{value}</span><span class="sub">{sub}</span><span class="sub">{link}</span></div>')
    return (f'<a class="{cls}" href="{href}"><span class="label">{label}</span>'
            f'<span class="sv">{value}</span><span class="sub">{sub}</span></a>')


def _cents(cents: int) -> str:
    """Whole dollars without the cents, like the plan's cap: $366, $361.40."""
    text = plan_rounds.format_cents(cents)
    return text[:-3] if text.endswith(".00") else text


def _status_bar(snap: dict, items: list[dict] | None = None, charts: dict[str, str] | None = None) -> str:
    """The status bar: one compact row of six cells. Is the agent OK and on; how fresh the machine's
    data is and when the next sync comes; the order cutoff; what is spendable; catalog approvals;
    and the plan. Each cell links to its section. `charts` is where the server serves the chart
    pages (`render`): the Spendable cell links to the development-spend chart when it is given."""
    now, s = snap["as_of"], snap["status"]
    charts = charts or {}
    spend_link = (f'<a href="{_e(charts["spend"])}">Development spend by week</a>' if "spend" in charts else "")
    items = attention(snap) if items is None else items
    state = verdict(items)
    cells = []

    # The agent: the verdict, the halt switch and the latest run.
    if s["halted"]:
        value = f'<span class="verdict {state}">{_VERDICT_TEXT[state]}</span> <span class="bad-text">Halted</span>'
        switch = ("no write tool can run &middot; "
                  + (_switched("halt switched on", s["halt_changed_at"], s["halt_changed_by"], now)
                     or "no halt row: counts as halted"))
    else:
        value = f'<span class="verdict {state}">{_VERDICT_TEXT[state]}</span> <span class="ok-text">On</span>'
        switched = _switched("halt switched off", s["halt_changed_at"], s["halt_changed_by"], now)
        switch = "writes allowed" + (f" &middot; {switched}" if switched else "")
    run = s["latest_run"]
    if run is None:
        ran = "no run recorded yet"
    elif run.get("stale"):
        ran = f'<span class="bad-text">run #{run["id"]} probably died</span>'
    elif run["status"] == "running":
        ran = (f'run #{run["id"]} running for <time datetime="{run["started_at"].astimezone(timezone.utc).isoformat()}" '
               f'data-f="since">{_span(now - run["started_at"])}</time>')
    else:
        ran = (f'<span class="{"bad-text" if run["status"] == "failed" else ""}">run #{run["id"]} '
               f'{_e(run["status"])}</span> {_time(run["started_at"], "ago", now)}')
    cells.append(_cell("Agent", value, f"{switch}<br>{ran}", "#attention",
                       {"problem": "bad", "check": "warn"}.get(state, "")))

    # How fresh the machine's data is: when the last poll was taken and when the next sync reads it.
    # The sales window is said in words, never as a lone date that could read as the last update.
    sales = snap["sales"]
    tone = ""
    if not sales["ok"]:
        value, sub = '<span class="dim">&mdash;</span>', "polls table missing"
    elif not snap.get("ours", {"ok": True})["ok"]:
        # The poll reader refuses without our assignment, which isn't the same as no polls.
        value, sub = '<span class="dim">&mdash;</span>', "our slots aren't recorded"
    elif sales["data"]["as_of"] is None:
        value, sub = '<span class="dim">No polls</span>', "run the history sync"
    else:
        d = sales["data"]
        total = sum(v["sold"] or 0 for v in d["slots"].values())
        tone = "warn" if d["stale"] else ""
        due = next_sync(now, snap.get("syncs"))
        value = f'Polled {_time(d["as_of"], "ago", now)}'
        sub = (f'{_time(d["as_of"], "et")}' + (f' &middot; next sync ~{_time(due, "etclock")}' if due else "")
               + " &middot; " + (f'{total} sold {_window_words(d)}' if d["window_start"] else "one poll only"))
    cells.append(_cell("Machine poll", value, sub, "#vm", tone))

    cutoff = s["next_cutoff"]
    if cutoff is None:
        cells.append(_cell("Order cutoff", "Thu 23:59", "Eastern &middot; install <code>tzdata</code> for a countdown",
                           "#requests"))
    else:
        cells.append(_cell("Order cutoff", _time(cutoff, "until", now), _time(cutoff, "etday"), "#requests",
                           "warn" if cutoff - now < CUTOFF_SOON else ""))

    # Money: spendable from the ledger once it has opened; before that, only that it is development spend
    # (the live page shows no test purchases, #192).
    spend, money = snap["spend"], snap.get("money")
    ledgered = money["data"] if money and money["ok"] and money["data"].get("opened") else None
    if ledgered:
        # The total is summed exact by the panel and rounded once, not added up from the rounded parts.
        spendable = Decimal(ledgered["spendable"])
        cells.append(_cell("Spendable", _money(spendable),
                           f'Spent since the opening <b>{_money(Decimal(ledgered["spent"]))}</b> &middot; model '
                           f'{_money(Decimal(ledgered["tokens"]))}', "#money", "bad" if spendable < 0 else "",
                           link=spend_link))
    elif spend["ok"] and spend["data"] is None:
        # Before the opening the live page shows no test runs or test purchases (#192): only that this
        # is development spend, not from the $600, and where its chart is.
        opens = money["data"].get("opens_at") if money and money["ok"] else None
        cells.append(_cell("Spendable", '<span class="dim">Before the opening</span>',
                           "development spend, not from the $600"
                           + (f' &middot; the ledger opens {money_panel._day(opens)}' if opens else ""),
                           "#money", link=spend_link))
    elif spend["ok"]:
        # The ledger has opened (the live spend counts from it), but it couldn't give a balance (no slot
        # assignment, say): the balance is unknown, not unopened. What was spent since the opening shows.
        m, dr = spend["data"]["model"], spend["data"]["drinks"]
        unpriced = f' ({m["unpriced"]} without a cost)' if m["unpriced"] else ""
        cells.append(_cell("Spendable", '<span class="dim">Balance unavailable</span>',
                           f'Spent since the opening: drinks {_money(dr["paid"])}, model {_money(m["total"])}{unpriced}',
                           "#money", link=spend_link))
    else:
        cells.append(_cell("Spendable", '<span class="dim">&mdash;</span>', "not available", "#money"))

    # Catalog approvals: every product in the first fill needs one, by the deadline.
    counts = snap.get("catalog_counts") or {"ok": False}
    if counts["ok"]:
        c = counts["data"]
        by = c["by_status"]
        extra = "".join(f' &middot; <span class="warn-text">{by[k]} {k}</span>' for k in ("sending", "unknown") if by[k])
        rejected = f' &middot; {by["rejected"]} rejected' if by["rejected"] else ""
        last = APPROVALS_DUE - timedelta(hours=6)      # the deadline's day, in Eastern
        due = f" &middot; due {last:%a} {last.day} {last:%b}" if now < APPROVALS_DUE else ""
        left = f'<span class="{"bad-text" if not c["left"] else ""}">{_plural(c["left"], "place")} left</span>'
        cells.append(_cell("Catalog approvals", f'{by["approved"]} approved',
                           f'{by["pending"]} pending{rejected}{extra} &middot; {left}{due}', "#requests"))
    else:
        cells.append(_cell("Catalog approvals", '<span class="dim">&mdash;</span>', "not available", "#requests"))

    # The plan: before the slot lock, the plan for the first fill; from it, the agent's plan for the next
    # restock (almost always the free Friday visit; a paid midweek visit is a restock too).
    plan = snap["plan"]
    p = plan["data"] if plan["ok"] else None
    tone = ""
    if "weekly_plan" in snap:
        value = "For the first fill"
        if not plan["ok"]:
            sub = "not available"
        elif p is None:
            sub = "no plan loaded yet"
        elif p.get("message"):
            sub = _e(p["message"])
        else:
            most, cap = p["fill_most_cents"], p["fill_cap_cents"]
            cost = f"{_cents(most)} of {_cents(cap)}" if most is not None else "cost unknown"
            tone = "bad" if most is not None and most > cap else ""
            sub = (f'{_plural(len(p["slots"]), "slot")} &middot; {cost}'
                   + ("" if p["picked"] else " &middot; latest, not picked"))
    else:
        value = "For the next restock"
        if not plan["ok"]:
            sub = "not available"
        elif p is None:
            sub = "no plan yet"
        else:
            run = p["run"]
            when = _restock(p)
            moved = (_plural(sum(1 for x in p["slots"] if x["changed"]), "change") if p["previous_run"]
                     else f'first plan: {_plural(sum(1 for x in p["slots"] if x["now"] is not None), "slot")}')
            sub = ((f"{_e(when)} &middot; " if when else "") + f'{moved} &middot; run #{run["id"]} '
                   f'{_e(run["status"])} {_time(run["started_at"], "ago", now)}')
    cells.append(_cell("Plan", value, sub, "#plan", tone))
    return f'<nav class="strip" aria-label="Status">{"".join(cells)}</nav>'


def _visitors_line(snap: dict) -> str:
    """Who has looked at the pages, as one small line for the footer. Not about running the machine."""
    seen = snap.get("visits")
    if not seen:
        return ""
    if not seen["ok"]:
        return '<p class="visitors">Visitors: unavailable just now.</p>'
    v = seen["data"]
    if not v["browsers"]:
        return '<p class="visitors">Visitors: none yet.</p>'
    return (f'<p class="visitors">Visitors: {_plural(v["browsers"], "browser")}, {_plural(v["visits"], "visit")}, '
            f'{_plural(v["sign_ins"], "sign-in")} since {_time(v["since"], "day")} &middot; this week: '
            f'{_plural(v["visits_7_days"], "visit")} from {_plural(v["browsers_7_days"], "browser")}.</p>')


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
    hidden = panel.get("hidden_tests")
    notice = (f'<p class="dim">{hidden["runs"]} test runs and {hidden["purchases"]} test purchases '
              'from before the opening are not shown here.</p>' if hidden else "")
    if (missing := _missing(panel)) is not None:
        return notice + missing
    if not panel["data"]:
        return notice + ('<p class="empty">No live runs yet. The agent loop (#39) calls '
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
        if r.get("steps"):
            body += _steps_html(r["id"], r["steps"])
        items.append(f'<li class="run{" replay" if r["mode"] == "replay" else ""}">'
                     f'<div class="head"><b>#{r["id"]}</b> {_time(r["started_at"])} {_run_pill(r)}</div>'
                     f'<div class="meta">{" &middot; ".join(meta)}</div>{body}</li>')
    return notice + f'<ol class="timeline">{"".join(items)}</ol>'


# Characters of a tool call or reply shown before "more"; reasoning is shown whole.
STEP_SHOWN = 280
_STEP_LABEL = {"thinking": "Thought", "text": "Wrote"}


def _cut(text: str) -> str:
    """Short text as is; long text cut, with the whole of it one click away."""
    if len(text) <= STEP_SHOWN:
        return f'<code>{_e(text)}</code>'
    return f'<details><summary><code>{_e(text[:STEP_SHOWN])}...</code></summary><pre>{_e(text)}</pre></details>'


def _steps_html(run_id: int, steps: list[dict]) -> str:
    """A run's steps, in order, folded away under one line: what it thought, called and was told."""
    calls = sum(1 for s in steps if s["kind"] == "tool_call")
    turns = max(s["turn"] for s in steps)
    items = []
    for s in steps:
        kind = s["kind"]
        if kind in ("thinking", "text"):
            what, body = _STEP_LABEL[kind], f'<div class="said">{_e(s["content"])}</div>'
        elif kind == "tool_call":
            what, body = f'Called <b>{_e(s["tool"])}</b>', _cut(s["content"])
        else:
            what = f'{_e(s["tool"])} {"failed" if s["is_error"] else "replied"}'
            body = _cut(s["content"])
        items.append(f'<li class="{kind}{" error" if s["is_error"] else ""}">'
                     f'<span class="t">{int(s["turn"])}</span><div><span class="what">{what}</span>{body}</div></li>')
    return (f'<details class="steps" data-key="steps-{int(run_id)}"><summary>How it decided: '
            f'{_plural(turns, "turn")}, {_plural(calls, "tool call")}</summary><ol>{"".join(items)}</ol></details>')


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


def _catalog_lede(counts: dict) -> str:
    """'40 requests: 22 pending, 15 approved, ...', then the open ones against the bridge's cap."""
    if not counts["ok"]:
        return ""
    c = counts["data"]
    by = c["by_status"]
    parts = [f'{by["pending"]} pending', f'{by["approved"]} approved', f'{by["rejected"]} rejected']
    parts += [f'<span class="warn-text">{by[s]} {s}</span>' for s in ("sending", "unknown") if by[s]]
    left = f'{c["left"]} place{"" if c["left"] == 1 else "s"} left'
    if c["unsettled"] and c["left"]:
        left += f', fewer if any of the {c["unsettled"]} sending or unknown reached the bridge'
    return (f'<p class="lede">{_plural(c["total"], "request")}: {", ".join(parts)}.</p>'
            f'<p class="lede"><b>Open: {c["open"]} of about {c["cap"]}</b> &middot; '
            f'<span class="{"bad-text" if not c["left"] else ""}">{left}</span>. '
            '<span class="dim">The bridge refuses a new request once about that many are pending '
            '(seen, not documented). Statuses are as our records last stored them.</span></p>')


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
        out += f'<ul class="list">{"".join(items[:REQUESTS_SHOWN])}</ul>'
        if len(items) > REQUESTS_SHOWN:
            out += (f'<details class="more" data-key="more-requests"><summary>{len(items) - REQUESTS_SHOWN} older'
                    f'</summary><ul class="list">{"".join(items[REQUESTS_SHOWN:])}</ul></details>')
    out += '<h3>Catalog requests</h3>'
    if (missing := _missing(catalog)) is not None:
        out += missing
    elif not catalog["data"]:
        out += _catalog_lede(_catalog_counts(catalog)) + '<p class="empty">None yet.</p>'
    else:
        out += _catalog_lede(_catalog_counts(catalog)) + '<ul class="list">' + "".join(
            f'<li><div class="head"><b>{_e(r["name"])}</b> <span class="dim">{_e(r["size"])}</span> '
            f'{_pill(r["status"], _REQUEST_TONE.get(r["status"], "muted"))}</div>'
            f'<div class="meta">asked {_time(r["requested_at"], "ago", now)}{_catalog_sku(r)}</div>'
            + (f'<div class="detail">{_e(r["decision_note"])}</div>' if r["decision_note"] else "") + "</li>"
            for r in catalog["data"]) + "</ul>"
    return out


def _catalog_sku(r: dict) -> str:
    """An approved request's SKU, which may not be the one we'd guess from the name; any other's
    only if one was recorded."""
    if r.get("sku"):
        return f' &middot; SKU <code>{_e(r["sku"])}</code>'
    return " &middot; no SKU recorded yet" if r["status"] == "approved" else ""


def _closet_html(stock: dict) -> str:
    if (missing := _missing(stock)) is not None:
        return missing
    closet = stock["data"]["closet"]
    if not closet:
        return '<p class="empty">Nothing in back stock, by the receipts.</p>'
    total = sum(c["units"] for c in closet)
    return (f'<p class="lede">{_plural(total, "unit")} of {_plural(len(closet), "product")}, '
            'at what they cost us (oldest first; a product bought at more than one price shows the average of what is left).</p><ul class="closet">' + "".join(
                f'<li><b>{_e(c["product"])}</b><span>{c["units"]} &times; '
                f'{"cost unknown" if c["unit_cost"] is None else _money(c["unit_cost"])}</span></li>'
                for c in closet) + "</ul>")


STYLE = """
:root { color-scheme:light dark;
  --bg:#f3f5f8; --surface:#fff; --sunk:#f7f8fa; --ink:#18202a; --muted:#5d6977; --line:#dfe4ea;
  --accent:#1f5fd1; --accent-soft:#e6efff;
  --ok:#16794a; --ok-soft:#e3f5ea; --warn:#9a5b00; --warn-soft:#fff2d9; --bad:#c0262d; --bad-soft:#fde7e7;
  --info:#1f5fd1; --info-soft:#e6efff; --bar:#e7ebf0; --shadow:0 1px 2px rgba(16,24,40,.06);
  --cab:#262b34; --cab-2:#1e222a; --cab-edge:#3a414d; --cab-brand:linear-gradient(90deg, #b4232a, #d9363e); }
@media (prefers-color-scheme: dark) { :root {
  --bg:#0e1116; --surface:#161b22; --sunk:#11151b; --ink:#e6eaf0; --muted:#94a0ae; --line:#2a313b;
  --accent:#6ea8ff; --accent-soft:#16294a;
  --ok:#4cc38a; --ok-soft:#12301f; --warn:#f0b450; --warn-soft:#3a2a10; --bad:#ff6b6b; --bad-soft:#3d1a1c;
  --info:#6ea8ff; --info-soft:#16294a; --bar:#262d36; --shadow:none;
  --cab:#1f242c; --cab-2:#181c22; --cab-edge:#363d48; --cab-brand:linear-gradient(90deg, #8f1d23, #b02a31); } }
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
header.top { display:flex; flex-wrap:wrap; align-items:center; gap:6px 12px; margin-bottom:8px }
header.top .meta { color:var(--muted); font-size:12px; margin-left:auto }
.verdict { font-weight:700; font-size:13px; padding:3px 10px; border-radius:999px; border:1px solid currentColor }
.verdict.ok { color:var(--ok); background:var(--ok-soft) } .verdict.check { color:var(--warn); background:var(--warn-soft) }
.verdict.problem { color:var(--bad); background:var(--bad-soft) }
#live.failed { color:var(--bad); font-weight:600 }
.demo-banner { position:sticky; top:0; z-index:20; padding:10px 16px; text-align:center; font-size:15px; font-weight:700;
  background:#ffd23f; color:#1a1400; border-bottom:3px solid #b8860b }
.demo-banner a { color:inherit; text-decoration:underline }
/* The status bar: one compact row of six cells; each links to its section. */
.strip { display:grid; grid-template-columns:repeat(6, minmax(0, 1fr)); background:var(--surface); border:1px solid var(--line);
  border-radius:10px; box-shadow:var(--shadow); overflow:hidden; margin-bottom:10px }
.sc { display:flex; flex-direction:column; gap:0; padding:5px 10px 6px; min-width:0; color:var(--ink); text-decoration:none;
  border-left:1px solid var(--line) }
.sc:first-child { border-left:0 } .sc:hover { background:var(--sunk) } .sc:focus-visible { outline:2px solid var(--accent); outline-offset:-2px }
.sc .label { font-size:10px } .sc .sv { font-size:16px; font-weight:700; line-height:1.25; overflow-wrap:anywhere }
.sc .sv .verdict { font-size:12px; padding:0 8px; vertical-align:2px }
.sc .sub { font-size:11.5px; line-height:1.35 } .sc .sub b { color:var(--ink); font-weight:600 }
.sc.warn .sv { color:var(--warn) } .sc.bad .sv { color:var(--bad) }
.sc.warn { box-shadow:inset 0 3px 0 var(--warn) } .sc.bad { box-shadow:inset 0 3px 0 var(--bad) }
@media (max-width:1100px) { .strip { grid-template-columns:repeat(3, minmax(0, 1fr)) }
  .sc:nth-child(3n+1) { border-left:0 } .sc:nth-child(n+4) { border-top:1px solid var(--line) } }
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
section > p.charts { margin:0 0 10px; font-size:13px }
header.top a.chartlink { font-weight:600; font-size:14px; padding:4px 14px; border-radius:999px; text-decoration:none;
  border:1px solid var(--accent); color:var(--accent) }
header.top a.chartlink:hover, header.top a.chartlink:focus-visible { background:var(--accent); color:var(--surface) }
.grid2 { display:grid; grid-template-columns:minmax(0, 1fr); gap:0 14px }
@media (min-width:980px) { .grid2 { grid-template-columns:minmax(0, 1fr) minmax(0, 1fr) } .grid2 > section { margin:0 0 14px } }
.lede { margin:0 0 8px } .empty { color:var(--muted); background:var(--sunk); border:1px dashed var(--line); border-radius:8px;
  padding:10px 12px; margin:0 0 10px }
.pill { display:inline-block; font-size:11px; font-weight:600; line-height:18px; padding:0 8px; border-radius:999px;
  white-space:nowrap; vertical-align:1px }
.pill.ok { background:var(--ok-soft); color:var(--ok) } .pill.warn { background:var(--warn-soft); color:var(--warn) }
.pill.bad { background:var(--bad-soft); color:var(--bad) } .pill.info { background:var(--info-soft); color:var(--info) }
.pill.muted { background:var(--sunk); color:var(--muted); border:1px solid var(--line) }
.honest { font-size:12px; color:var(--muted); font-style:italic; margin:0 0 8px }
a.pill { text-decoration:none }
/* In the machine: the close-up's own styles are `machine_art.STYLE`, added after these. */
.vm > h2 .seg { margin-left:auto }
.seg { display:inline-flex; border:1px solid var(--line); border-radius:999px; overflow:hidden; font-weight:400 }
.seg[hidden] { display:none }
.seg button { font:inherit; font-size:12px; font-weight:600; border:0; background:var(--surface); color:var(--muted);
  padding:4px 12px; cursor:pointer }
.seg button[aria-pressed="true"] { background:var(--accent); color:var(--surface) }
.vm[data-view="now"] .v-next, .vm[data-view="next"] .v-now { display:none !important }
/* In the machine, one screen: the front on the left; on the right Needs attention and the plan's changes,
   or the picked selection close up. On a phone the front comes first and the close-up is a sheet. */
.vm > h2 { margin-bottom:6px } .vm > h2 .vm-when { margin-left:auto; font-size:12px; font-weight:600; color:var(--muted) }
.vm > h2 .vm-when + .seg { margin-left:0 }
.ops { display:grid; grid-template-columns:minmax(0, 1fr); gap:12px; align-items:start }
.ops-front { display:grid; gap:6px; min-width:0 }
.ops-side, .ops-home { display:flex; flex-direction:column; gap:10px; min-width:0 }
.ops-card { background:var(--sunk); border:1px solid var(--line); border-radius:10px; padding:8px 10px; min-width:0 }
.ops-card h3 { display:flex; flex-wrap:wrap; align-items:center; gap:4px 6px; margin:0 0 6px; font-size:13px; color:var(--ink);
  text-transform:none; letter-spacing:0 }
.ops-card h3 .hint { font-weight:400; font-size:11.5px; color:var(--muted) } .ops-jump { margin-left:auto; font-size:11.5px; font-weight:400 }
.ops-attn .attention, .ops-attn div.attention.clear { margin:0 } .ops-attn .attention { gap:5px }
.ops-attn .attention li { padding:6px 9px; gap:8px; font-size:12.5px } .ops-attn .attention .dot { width:18px; height:18px; font-size:11px }
.ops-chg .changes { margin:0; gap:1px } .ops-chg .changes li { display:block }
a.chg { display:grid; grid-template-columns:auto auto minmax(0, 1fr); gap:2px 6px; align-items:baseline; padding:3px 5px; border-radius:6px;
  color:var(--ink); text-decoration:none; font-size:12.5px }
a.chg:hover, a.chg:focus-visible { background:var(--surface); outline:1px solid var(--line) }
.changes.ff a.chg { grid-template-columns:auto minmax(0, 1fr) }
a.chg > b { font:800 11px/16px Arial, sans-serif; background:#07080a; color:#fff; border-radius:3px; padding:0 4px }
a.chg .why { display:block; color:var(--muted); font-size:11.5px; overflow-wrap:anywhere }
.vm-panel { display:none; min-width:0; background:var(--sunk); border:1px solid var(--line); border-radius:12px; padding:8px 10px 10px }
.vm-ph { display:flex; align-items:center; gap:8px; margin:0 0 6px }
.vm-x { margin-left:auto; display:inline-flex; align-items:center; gap:4px; font-size:12px; font-weight:600; color:var(--ink);
  text-decoration:none; background:var(--surface); border:1px solid var(--line); border-radius:999px; padding:3px 11px }
.vm-x:hover, .vm-x:focus-visible { border-color:var(--accent); color:var(--accent); outline:none }
.vm-grab, .vm-scrim { display:none }
.vm[data-sel] .vm-panel, .vm:not([data-js]):has(.sd:target) .vm-panel, .vm:not([data-js]):has(.ma-shelf:target) .vm-panel { display:block }
@media (min-width:980px) {
  .ops { grid-template-columns:minmax(0, 1fr) minmax(340px, 37%) }
  .ops-side { position:sticky; top:8px; max-height:calc(100vh - 16px); overflow-y:auto; overscroll-behavior:contain }
  .ops-attn { flex:none } .ops-chg { flex:0 1 auto; min-height:150px; overflow-y:auto }
  .vm[data-sel] .ops-home, .vm:not([data-js]):has(.sd:target) .ops-home,
  .vm:not([data-js]):has(.ma-shelf:target) .ops-home { display:none }
  .vm-panel { position:relative } .vm-panel .vm-ph .label { display:none }
  .vm-panel .vm-x { position:absolute; top:7px; right:10px } .vm-panel .ma-sh { padding-right:84px; min-height:24px }
  .vm-panel .ma-lane svg { max-height:200px }
  .vm-panel .ma-nm { display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; font-size:11px }
  .vm-panel .ma-nm span, .vm-panel .ma-chips, .vm-panel .ma-nt, .vm-panel .ma-why, .vm-panel .ma-plan { display:none }
  .vm-panel .ma-ct b { font-size:16px } }
@media (min-width:820px) and (max-width:979px) { .vm-panel { order:-1; scroll-margin-top:8px } }
@media (max-width:819px) {
  .vm-panel { display:block; position:fixed; left:0; right:0; bottom:0; z-index:30; max-height:88vh; overflow-y:auto;
    overscroll-behavior:contain; border:0; border-radius:16px 16px 0 0; background:var(--surface); box-shadow:0 -10px 30px rgba(0,0,0,.35);
    padding:6px 12px calc(14px + env(safe-area-inset-bottom)); transform:translateY(105%); visibility:hidden;
    transition:transform .28s cubic-bezier(.2, .8, .3, 1), visibility 0s .28s }
  .vm[data-open] .vm-panel, .vm:not([data-js]):has(.sd:target) .vm-panel { transform:none; visibility:visible;
    transition:transform .28s cubic-bezier(.2, .8, .3, 1) }
  .vm-grab { display:block; position:absolute; left:50%; top:6px; width:40px; height:4px; margin-left:-20px; border-radius:2px;
    background:var(--line) }
  .vm-ph { position:sticky; top:-6px; z-index:3; background:var(--surface); padding:12px 0 6px; margin:0 }
  .vm-x { min-height:44px; padding:0 16px; font-size:14px }
  .vm[data-open] .vm-scrim, .vm:not([data-js]):has(.sd:target) .vm-scrim { display:block; position:fixed; inset:0; z-index:29;
    background:rgba(5,8,12,.5) }
  /* In the sheet: the picked lane big beside its facts, then the whole shelf under it. */
  .vm-panel .ma-sh { order:-2 } .vm-panel .sd-list { order:-1 } .vm-panel .sd { margin:0 0 10px }
  .vm-panel .sd-art { display:block; float:left; width:112px; margin:0 10px 4px 0; border-radius:8px; overflow:hidden; background:#0b0e12 }
  .vm-panel .sd-art:empty { display:none } .vm-panel .sd-art svg { display:block; width:100%; height:auto }
  .vm-panel .ma-nm, .vm-panel .ma-chips, .vm-panel .ma-nt, .vm-panel .ma-why, .vm-panel .ma-plan, .vm-panel .ma-t { display:none }
  .vm-panel .ma-meta { padding:3px 2px 5px; text-align:center } .vm-panel .ma-ct { margin:0; font-size:9px }
  .vm-panel .ma-ct b { font-size:13px } .vm-panel .ma-notours { min-height:0 } .vm-panel .ma-n { font-size:11px }
  .vm-panel .ma-lane:not(.ma-notours) { flex-grow:1 !important } .vm-panel .ma-strip .ma-lane svg { max-height:170px } }
@media (prefers-reduced-motion:reduce) { .vm-panel { transition:none !important } }
.dotk.info { background:var(--info) } .dotk.warn { background:var(--warn) } .dotk.bad { background:var(--bad) }
details.more > summary { cursor:pointer; color:var(--muted); font-size:12px; margin:6px 0 }
.card { background:var(--sunk); border:1px solid var(--line); border-radius:9px; padding:10px 12px; min-width:0 }
.card p { margin:4px 0 0 }
.changes { list-style:none; margin:6px 0 0; padding:0; display:grid; gap:6px }
.changes li { display:grid; grid-template-columns:auto auto minmax(0, 1fr); gap:6px; align-items:baseline; font-size:13px }
.changes .why { display:block; color:var(--muted); font-size:12px; overflow-wrap:anywhere }
.dotk { display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:5px }
.vm-legend { font-size:12px; color:var(--muted) } .vm-legend p { margin:6px 0 0 }
.vm-legend > summary .dotk { margin-left:6px } .vm-legend .honest { margin:0 } .vm-legend .more { text-decoration:underline }
.pc summary a.num { font-weight:700; color:var(--accent) }
.pill.wild { background:#4a3508; color:#f8cd75; border:1px solid #c99a2e } a.chg .pill.wild { margin-left:4px } .sd-t .pill.wild { margin-right:6px }
/* The plan: one line per slot; the reason opens beneath. */
.only { display:inline-flex; gap:6px; align-items:center; font-size:12px; margin:4px 0 8px; cursor:pointer }
.only[hidden] { display:none }
/* One row per slot, top to bottom: what is in it now beside what the plan asks for the next restock. */
.plan-grid { display:flex; flex-direction:column; gap:4px }
.plan-head, .pc summary { display:grid; grid-template-columns:34px minmax(0, 1fr) minmax(0, 1.25fr) minmax(90px, auto);
  gap:4px 12px; align-items:baseline }
.plan-head { padding:0 11px; font-size:11px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted) }
.pc summary { cursor:pointer; list-style:none } .pc summary::-webkit-details-marker { display:none }
.pc summary .now { min-width:0; overflow-wrap:anywhere; color:var(--muted) }
.plan-grid[data-only="true"] .pc:not(.changed) { display:none }
.pc { background:var(--sunk); border:1px solid var(--line); border-radius:8px; padding:6px 10px; min-width:0 }
.pc.changed { border-color:var(--accent); box-shadow:inset 0 0 0 1px var(--accent); background:var(--surface) }
.pc:target { outline:2px solid var(--accent) }
.pc summary { color:var(--ink); font-size:13px }
.pc summary .what { min-width:0; overflow-wrap:anywhere } .pc summary s { color:var(--muted) }
.pc .fill-check { display:block; margin-top:4px }
.pc .tags { display:inline-flex; flex-wrap:wrap; gap:3px; justify-content:flex-end }
.pc .why { font-size:13px; margin-top:4px; overflow-wrap:anywhere }
.pc .was { font-size:12px; color:var(--muted); margin-top:3px; border-top:1px dashed var(--line); padding-top:3px; overflow-wrap:anywhere }
.first-fill .pc summary { grid-template-columns:30px minmax(0, 1fr) auto }
.first-fill .pc summary .what::before { content:none }
.first-fill .pc summary::after { content:"▸"; color:var(--muted) }
.first-fill .pc[open] summary::after { content:"▾" }
.pair { display:grid; grid-template-columns:repeat(auto-fit, minmax(260px, 1fr)); gap:10px; margin-top:12px }
.words { background:var(--sunk); border:1px solid var(--line); border-radius:9px; padding:10px 12px }
.text { white-space:pre-wrap; overflow-wrap:anywhere; margin-top:4px }
.timeline { list-style:none; margin:0; padding:0 0 0 16px; border-left:2px solid var(--line) }
.timeline li { position:relative; padding:0 0 14px 4px }
.timeline li::before { content:""; position:absolute; left:-23px; top:5px; width:10px; height:10px; border-radius:50%;
  background:var(--surface); border:2px solid var(--muted) }
.timeline li:first-child::before { background:var(--accent); border-color:var(--accent) }
.timeline li.replay { opacity:.7 }
.steps { margin-top:6px } .steps ol { list-style:none; margin:6px 0 0; padding:0 }
.steps li { display:grid; grid-template-columns:auto minmax(0, 1fr); gap:8px; padding:5px 0; border-top:1px dashed var(--line); font-size:13px }
.steps .t { font-size:11px; color:var(--muted); min-width:14px; text-align:right; padding-top:2px }
.steps .what { display:block; font-size:12px; color:var(--muted) }
.steps .said { white-space:pre-wrap; overflow-wrap:anywhere } .steps li.thinking .said { font-style:italic }
.steps code { overflow-wrap:anywhere; white-space:pre-wrap } .steps pre { white-space:pre-wrap; overflow-wrap:anywhere; margin:4px 0 0 }
.steps li.error { background:var(--bad-soft) }
.timeline .steps li { padding:5px 0 } .timeline .steps li::before { content:none }
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
footer .visitors { margin:4px 0 0; font-size:11px; opacity:.8 }
@media (max-width:640px) {
  main { padding:12px } .big { font-size:19px } header.top .meta { margin-left:0; width:100% }
  .strip { grid-template-columns:repeat(2, minmax(0, 1fr)) } .sc { padding:6px 9px }
  .sc:nth-child(n) { border-left:1px solid var(--line); border-top:1px solid var(--line) }
  .sc:nth-child(odd) { border-left:0 } .sc:nth-child(-n+2) { border-top:0 } .sc .sv { font-size:15px }
  .plan-head { display:none }
  .pc summary { grid-template-columns:30px minmax(0, 1fr) } .pc summary .now::before { content:"now: " }
  .pc summary .what, .pc summary .tags { grid-column:2 } .pc summary .what::before { content:"Next restock: " }
  .pc summary .tags { justify-content:flex-start }
  .feed li { grid-template-columns:auto minmax(0, 1fr) } .feed .kind { display:none } }
"""

STYLE += money_panel.STYLE   # the money panel's own rules (#48)
STYLE += pricing_panel.STYLE   # the pricing panel's own rules
STYLE += leaderboards.STYLE

# Keeps times current in the viewer's zone, and refreshes the page in place every REFRESH_SECONDS
# without losing scroll, open details or the tool-call filter. A failed refresh keeps the last page
# and says so. Without JavaScript the <noscript> meta refresh reloads the page instead.

SCRIPT = """(function () {
  var REFRESH = %d * 1000, FROZEN = %d, show = 'all', view = 'now', only = false, lastOk = Date.now(), failed = false;
  // The demo copy keeps its toggles under its own keys, so flipping one there never moves the live page's.
  var PFX = '%s';
  // sel: the selection the close-up shows ('' shows Needs attention and the plan's changes instead);
  // sheet: whether it is open as a sheet (phones). Kept in memory, so a refresh keeps them and a new visit
  // starts with Needs attention.
  // opener: the id of what opened the close-up, so focus can go back to it even after a refresh
  // has replaced every element in <main>.
  var sel = '', sheet = false, opener = null;
  try {
    show = localStorage.getItem(PFX + 'show') || 'all';
    view = localStorage.getItem(PFX + 'view') === 'next' ? 'next' : 'now';
    only = localStorage.getItem(PFX + 'changed') === '1';
  } catch (e) {}
  function narrow() { return !!(window.matchMedia && matchMedia('(max-width: 819px)').matches); }
  // Between the phone's sheet and the desktop's side-by-side view the close-up sits under the front.
  function stacked() { return !!(window.matchMedia && matchMedia('(min-width: 820px) and (max-width: 979px)').matches); }
  function still() { return !!(window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches); }
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
    // A fixed (demo) page keeps its spans ("2 h ago") as of its own moment, not the viewer's, but
    // still shows dates and times in Eastern, like the live page, instead of the server's UTC.
    var now = Date.now();
    document.querySelectorAll('time[datetime]').forEach(function (t) {
      var d = new Date(t.getAttribute('datetime')), f = t.getAttribute('data-f');
      if (isNaN(d)) return;
      if (FROZEN && (f === 'ago' || f === 'until' || f === 'since')) return;
      // The Eastern forms (et, etclock, etday) are absolute already: they format below as on the live page.
      if (FROZEN && f !== 'et' && f !== 'etclock' && f !== 'etday') {
        var et = {timeZone: 'America/New_York'};
        if (f === 'clock') t.textContent = d.toLocaleTimeString([], Object.assign({hour: 'numeric', minute: '2-digit'}, et)) + ' ET';
        else if (f === 'day') t.textContent = d.toLocaleDateString([], Object.assign({weekday: 'short', day: 'numeric', month: 'short'}, et));
        else t.textContent = d.toLocaleString([], Object.assign({weekday: 'short', month: 'short', day: 'numeric',
                                                                 hour: 'numeric', minute: '2-digit'}, et)) + ' ET';
        t.title = d.toLocaleString([], et) + ' ET';
        return;
      }
      if (f === 'ago') t.textContent = span(now - d) + ' ago';
      else if (f === 'until') t.textContent = d > now ? span(d - now) : 'passed';
      else if (f === 'since') t.textContent = span(now - d);
      else if (f === 'et' || f === 'etclock' || f === 'etday') {
        // Eastern, the machine's zone, whatever the viewer's own zone is.
        var clock = d.toLocaleTimeString('en-US', {hour: 'numeric', minute: '2-digit', timeZone: 'America/New_York'});
        t.textContent = (f === 'etday' ? d.toLocaleDateString('en-GB', {weekday: 'short', day: 'numeric', month: 'short',
                                                                         timeZone: 'America/New_York'}) + ', ' : '')
                        + clock + (f === 'etclock' ? '' : ' ET');
      }
      else if (f === 'clock') t.textContent = d.toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});
      else if (f === 'day') t.textContent = d.toLocaleDateString([], {weekday: 'short', day: 'numeric', month: 'short'});
      else t.textContent = d.toLocaleString([], {weekday: 'short', month: 'short', day: 'numeric',
                                                  hour: 'numeric', minute: '2-digit'});
      t.title = d.toLocaleString();
    });
    var live = document.getElementById('live');
    if (live && !FROZEN) {
      live.textContent = failed ? 'refresh failed, showing data from ' + span(now - lastOk) + ' ago'
                                : 'updated ' + span(now - lastOk) + ' ago';
      live.className = failed ? 'failed' : '';
    }
  }
  function save(key, value) { try { localStorage.setItem(key, value); } catch (err) {} }
  function cellFor(s) { return s ? document.querySelector('.vm .fc[data-sel="' + CSS.escape(s) + '"]') : null; }
  // The controls only work with JavaScript, so the page sends them hidden and this shows them.
  function filter() {
    document.querySelectorAll('.feed').forEach(function (f) { f.setAttribute('data-show', show); });
    document.querySelectorAll('.filter button').forEach(function (b) {
      b.setAttribute('aria-pressed', String(b.getAttribute('data-show') === show));
    });
    // The picked selection, kept across refreshes. One that isn't ours any more (the assignment
    // changed) is dropped, and the side shows Needs attention again.
    if (sel && !cellFor(sel)) { sel = ''; sheet = false; }
    if (sheet && !narrow()) sheet = false;
    document.querySelectorAll('.vm').forEach(function (v) {
      v.setAttribute('data-view', view);
      v.setAttribute('data-js', '');
      if (sel) v.setAttribute('data-sel', sel); else v.removeAttribute('data-sel');
      if (sheet) v.setAttribute('data-open', ''); else v.removeAttribute('data-open');
    });
    document.querySelectorAll('.vm .fc[data-sel]').forEach(function (c) {
      if (c.getAttribute('data-sel') === sel) c.setAttribute('aria-current', 'true'); else c.removeAttribute('aria-current');
    });
    hero();
    document.querySelectorAll('.vm .seg').forEach(function (s) { s.hidden = false; });
    document.querySelectorAll('.vm .seg button').forEach(function (b) {
      b.setAttribute('aria-pressed', String(b.getAttribute('data-view') === view));
    });
    var box = document.getElementById('changed-only');
    if (box) { box.checked = only; box.closest('label').hidden = false; }
    document.querySelectorAll('.plan-grid[data-only]').forEach(function (g) { g.setAttribute('data-only', String(only)); });
  }
  // On a phone the sheet shows the picked lane big beside its facts, with the whole shelf under it.
  function hero() {
    document.querySelectorAll('.vm .sd-art').forEach(function (a) { a.textContent = ''; });
    if (!sheet || !sel) return;
    var lane = document.getElementById('lane-' + sel), card = document.getElementById('sel-' + sel);
    var art = card && card.querySelector('.sd-art'), svg = lane && lane.querySelector('svg');
    if (art && svg) art.appendChild(svg.cloneNode(true));
  }
  // Picking one of ours shows its shelf and facts beside the front; on a phone they slide up as a sheet.
  function choose(s, from) {
    if (!cellFor(s)) return false;
    sel = s; sheet = narrow(); opener = from && from.id ? from.id : opener;
    filter();
    var panel = document.querySelector('.vm-panel'), card = document.getElementById('sel-' + sel);
    if (sheet) {
      if (panel) panel.scrollTop = 0;
      if (card) card.focus({preventScroll: true});
    } else if (stacked()) {
      // One column: bring the close-up, under the front, into view.
      if (panel) panel.scrollIntoView({block: 'start', behavior: still() ? 'auto' : 'smooth'});
    } else if (from && !from.closest('.vm')) {
      // Picked from the plan below: bring the machine back into view.
      var vm = document.getElementById('vm');
      if (vm) vm.scrollIntoView({block: 'start', behavior: still() ? 'auto' : 'smooth'});
    }
    return true;
  }
  function shut() {
    if (!sel && !sheet) return;
    // Back to the opener; if a refresh took it away (a change row that is gone), to the selection's
    // cell on the front; if that is gone too, to the machine's heading.
    var back = (opener && document.getElementById(opener)) || document.getElementById('fc-' + sel)
               || document.getElementById('vm-title');
    sel = ''; sheet = false; opener = null; filter();
    if (back) back.focus({preventScroll: true});
  }
  function openSlot(id) {
    var d = document.getElementById(id);
    if (!d || d.tagName !== 'DETAILS') return;
    if (only && !d.classList.contains('changed')) { only = false; save(PFX + 'changed', '0'); filter(); }
    d.open = true;
  }
  document.addEventListener('click', function (e) {
    var t = e.target.closest ? e.target : e.target.parentElement;
    var b = t.closest('.filter button'), v = t.closest('.vm .seg button'), a = t.closest('a[href^="#slot-"]');
    if (b) { show = b.getAttribute('data-show'); save(PFX + 'show', show); filter(); }
    if (v) { view = v.getAttribute('data-view'); save(PFX + 'view', view); filter(); }
    if (a) openSlot(a.getAttribute('href').slice(1));
    var pick = t.closest('a[data-sel]'), shelf = t.closest('a[data-shelf]'), close = t.closest('[data-close]');
    var lane = !t.closest('a') && t.closest('.vm-panel article.ma-lane:not(.ma-notours)');
    if (pick && choose(pick.getAttribute('data-sel'), pick)) e.preventDefault();
    if (shelf) {
      var first = document.querySelector('.vm .fm-shelf[data-n="' + shelf.getAttribute('data-shelf') + '"] .fc[data-sel]');
      if (first && choose(first.getAttribute('data-sel'), shelf)) e.preventDefault();
    }
    if (lane) choose(lane.id.slice(5), null);
    if (close) { e.preventDefault(); shut(); }
    if (t.closest('a[href="#attention"]')) shut();
  });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') shut(); });
  document.addEventListener('change', function (e) {
    if (e.target.id !== 'changed-only') return;
    only = e.target.checked; save(PFX + 'changed', only ? '1' : '0'); filter();
  });
  function fromHash() {
    var h = /^#sel-([A-Za-z0-9_]+)$/.exec(location.hash), s = /^#shelf-([1-5])$/.exec(location.hash);
    if (h) return choose(h[1]);
    if (s) {
      var first = document.querySelector('.vm .fm-shelf[data-n="' + s[1] + '"] .fc[data-sel]');
      return first ? choose(first.getAttribute('data-sel')) : false;
    }
    openSlot(location.hash.slice(1));
    return false;
  }
  window.addEventListener('hashchange', fromHash);
  function refresh() {
    if (document.hidden) return;
    // The header tells the visit counter this is a refresh, not someone opening the page.
    fetch(location.pathname, {cache: 'no-store', headers: {'%s': '1'}}).then(function (r) {
      // Signed out (the password's cookie ran out): reload, so the password page shows.
      if (r.redirected && new URL(r.url).pathname === '/login') { location.reload(); throw new Error('signed out'); }
      if (!r.ok) throw new Error(r.status);
      return r.text();
    }).then(function (html) {
      var doc = new DOMParser().parseFromString(html, 'text/html');
      var fresh = doc.querySelector('main'), main = document.querySelector('main');
      if (!fresh || !main) throw new Error('no page');
      if (doc.title) document.title = doc.title;   // the verdict in the tab follows the page
      var open = Array.prototype.map.call(main.querySelectorAll('details[open][data-key]'),
                                          function (d) { return d.getAttribute('data-key'); });
      var panel = document.querySelector('.vm-panel'), side = document.querySelector('.ops-side');
      var scrolled = [panel ? panel.scrollTop : 0, side ? side.scrollTop : 0];
      // Keyboard focus, by the focused element's id: the swap replaces it with an equal new one.
      var focused = main.contains(document.activeElement) ? document.activeElement.id : '';
      main.innerHTML = fresh.innerHTML;
      open.forEach(function (k) {
        var d = main.querySelector('details[data-key="' + CSS.escape(k) + '"]');
        if (d) d.open = true;
      });
      failed = false; lastOk = Date.now(); filter(); times();
      panel = document.querySelector('.vm-panel'); side = document.querySelector('.ops-side');
      if (panel) panel.scrollTop = scrolled[0];
      if (side) side.scrollTop = scrolled[1];
      var again = focused && document.getElementById(focused);
      if (again) again.focus({preventScroll: true});
    }).catch(function () { failed = true; times(); });
  }
  filter(); times();
  // The browser scrolls to a #target after this runs: on a wide screen show the machine from its top
  // instead of wherever the facts happen to sit. Not in the one-column tablet layout: there the pick
  // has just scrolled the close-up into view, and that is where a direct link should land.
  if (location.hash && fromHash() && !sheet && !stacked()) {
    setTimeout(function () { var vm = document.getElementById('vm'); if (vm) vm.scrollIntoView(); }, 60);
  }
  if (!FROZEN) {
    setInterval(refresh, REFRESH);
    setInterval(times, 15000);
    document.addEventListener('visibilitychange', function () { if (!document.hidden) refresh(); });
  }
})();"""


_VERDICT_TEXT = {"ok": "OK", "check": "Check", "problem": "Problem"}


# The demo copy's banner (`vending.demo`). The words are fixed: people look for them.
DEMO_BANNER = "DEMO: made-up data, not our machine. The live page is "
DEMO_LIVE_HREF = "/dashboard"
DEMO_TITLE = "DEMO &middot; "
# The page remembers its toggles in localStorage. Same origin, so the demo needs its own keys.
LIVE_STORAGE, DEMO_STORAGE = "dash-", "demo-dash-"


def pickable(snap: dict, picked: str | None) -> str | None:
    """`picked` when it is one of our selections on the machine's face (the stored assignment), else None."""
    face = re.fullmatch(rf"[1-{SHELVES}][1-{COLUMNS}]", picked or "")
    return picked if face and picked in our_selections(snap) else None


def render(snap: dict, refresh: int = REFRESH_SECONDS, json_href: str = "state.json",
           nav: tuple[tuple[str, str], ...] = (), demo: bool = False,
           charts: dict[str, str] | None = None, picked: str | None = None) -> str:
    """The page. `json_href` is where its JSON twin lives, and `nav` is (label, href) links to
    sibling pages, for a server that mounts this page under a path beside others. `picked` is the
    request's `?sel=`: without scripts the timed reload keeps it, when it is one of ours.

    `demo` is for the made-up copy (`vending.demo`): a banner on top, "DEMO" in the tab title, and a
    fixed page. It never refreshes and keeps the times the server wrote, because the page's script would
    otherwise measure "3 days ago" and "in 1 day" from the viewer's own clock, not the snapshot's.

    `charts` is where that server serves the chart pages, by name ("money", "pnl", "spend"): the money
    panel and the spend tile link to the ones given. Run on its own, the page has none."""
    charts = charts or {}
    now = snap["as_of"]
    # Without scripts the page reloads itself; a pick of ours goes along (a #fragment never reaches us).
    picked = pickable(snap, picked)
    reload_to = f"?{visits.REFRESH_QUERY}" + (f"&sel={picked}#sel-{picked}" if picked else "")
    links = "".join(f' &middot; <a href="{_e(href)}">{_e(label)}</a>' for label, href in nav)
    items = snap.get("attention")
    if items is None:
        items = attention(snap)
    state = verdict(items)
    head = (f'<header class="top">{mascot_svg(state)}<h1>Agent dashboard</h1>'
            + (f'<a class="chartlink" href="{_e(charts["money"])}">Money chart</a>' if "money" in charts else "")
            + f'<span class="meta">Read-only &middot; as of {_time(now)} &middot; <span id="live">'
            + ("fixed snapshot, never refreshes" if demo else f"refreshes every {int(refresh)} s")
            + f'</span> &middot; <a href="{_e(json_href)}">JSON</a>{links}</span></header>')
    chart_links = " &middot; ".join(f'<a href="{_e(charts[name])}">{label}</a>' for name, label in
                                    (("money", "Money by day"), ("pnl", "Weekly P&amp;L")) if name in charts)
    money = (f'<section id="money"><h2>Money <span class="hint">from the ledger &middot; exact to the cent'
             " &middot; the competition counts from the ledger's opening; spend before it is development"
             f'</span></h2>' + (f'<p class="charts">Charts: {chart_links}</p>' if chart_links else "")
             + f'{money_panel.html(snap["money"], charts.get("pnl"))}</section>' if "money" in snap else "")
    pricing = pricing_panel.html(snap["pricing"], snap["ours"]) if "pricing" in snap else ""
    boards = (f'<section id="leaderboards"><h2>Product leaderboards</h2>{leaderboards.html(snap["leaderboards"])}</section>'
              if "leaderboards" in snap else "")
    body = (head + _status_bar(snap, items, charts) + _cabinet_html(snap, items) + pricing + money
            + _plan_section(snap)
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
            + boards
            + ('<footer>Made-up data from a fixed snapshot, shown with the live page\'s own code. Nothing here is '
               f'read from our database. The live page is <a href="{DEMO_LIVE_HREF}">{DEMO_LIVE_HREF}</a>.</footer>'
               if demo else
               '<footer>This page only reads. To stop the agent: <code>python -m vending.halt on --by NAME</code>'
               + _visitors_line(snap) + '</footer>'))
    # The query marks the reload as one, so the visit counter doesn't count it (`vending.visits`).
    # The demo never reloads.
    reload = ("" if demo else
              f'<noscript><meta http-equiv="refresh" content="{int(refresh)}; url={_e(reload_to)}"></noscript>')
    banner = (f'<div class="demo-banner" role="note">{_e(DEMO_BANNER)}<a href="{DEMO_LIVE_HREF}">{DEMO_LIVE_HREF}</a>'
              '</div>' if demo else "")
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            + ('<meta name="robots" content="noindex">' if demo else "")
            + reload
            + f'<title>{DEMO_TITLE if demo else ""}{_VERDICT_TEXT[state]} &middot; Agent dashboard</title>'
            f'<style>{STYLE}{machine_art.STYLE}</style></head><body>{banner}<main>{body}</main>'
            f'<script>{SCRIPT % (int(refresh), int(demo), DEMO_STORAGE if demo else LIVE_STORAGE, visits.REFRESH_HEADER)}'
            '</script></body></html>')


def _error_page(message: str) -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<meta http-equiv="refresh" content="{REFRESH_SECONDS}"><title>No database &middot; Agent dashboard</title>'
            f'<style>{STYLE}</style></head><body><main><header class="top">{mascot_svg("problem")}<h1>Agent dashboard</h1>'
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
        picked = parse_qs(urlsplit(self.path).query).get("sel", [None])[0]
        self._send(200, "text/html; charset=utf-8", render(snap, picked=picked))

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
