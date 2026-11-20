"""The season: an hour-by-hour event loop from Mon 30 Nov to the end of Wed 3 Feb.

Each hour customers buy, the machine is polled (a real EVA-DTS document through
`sync_history.store`), and the request sync reads the bridge (`sync_requests.sync`). Between polls,
events happen at their own times: the ledger opening, the catalog sync, the team's weekly sourcing
check, the agent's runs, and the worker's visits. After every agent run the whole state is saved,
so a stopped season resumes where it left off (`--resume`).
"""

from __future__ import annotations

import contextlib
import json
import pickle
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import anyio
import psycopg

from vending import agent, config, db, ledger, sourcing, stock, sync_catalog, sync_history, sync_requests
from vending import slots as slot_assignment
from vending.harness import Harness
from vending.simulate import world
from vending.simulate.bridge import SimBridge, Worker
from vending.simulate.clock import Clock, patched
from vending.simulate.world import EASTERN, Calendar, Demand, Machine

# Every sourcing file the team has committed, oldest first, so a later check of a SKU wins.
SEED_SOURCING = tuple(sorted((world.REPO / "db" / "sourcing").glob("*.csv")))
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
BY = "simulation"
# Who decides each run. Only "model" calls the model; anything else is refused, never sent to it.
DECIDERS = ("scripted", "scripted-wide", "model")


def check_decider(decider: str) -> None:
    if decider not in DECIDERS:
        raise ValueError(f"unknown decider {decider!r}: use one of {', '.join(DECIDERS)}")


# Columns the database stamps with its own clock (`default now()`). Our code fills them in while the
# season runs, so they hold wall time; `restamp` moves each one a step wrote to the simulated moment it
# belongs to, so the database reads as it would have at any simulated time. A step's rows are the ones
# stamped between the wall clock before it (`wall_clock`) and after it. "Before the season starts" can't
# pick them out: from 30 Nov 2026 the wall clock is past SIM_START, and simulated times overlap it.
WALL_STAMPED = (("polls", "synced_at"), ("config", "updated_at"), ("action_log", "at"),
                ("product_requests", "requested_at"), ("ledger_entries", "recorded_at"),
                ("purchase_submissions", "at"), ("purchase_submissions", "resolved_at"),
                ("agent_runs", "started_at"), ("agent_runs", "finished_at"), ("model_calls", "finished_at"),
                ("slot_assignment", "first_read_at"), ("slot_assignment", "last_checked_at"),
                ("agent_steps", "at"), ("sourcing_options", "updated_at"))


def wall_clock(conn: psycopg.Connection) -> datetime:
    """The database's own clock, unpatched: what `default now()` stamps. Take it before a step."""
    return conn.execute("select clock_timestamp()").fetchone()[0]


def restamp(conn: psycopg.Connection, at: datetime, since: datetime) -> None:
    """Move every wall-clock stamp written since `since` (a `wall_clock` reading) to the simulated `at`."""
    until = wall_clock(conn)
    for table, column in WALL_STAMPED:
        conn.execute(f"update {table} set {column} = %s where {column} between %s and %s", (at, since, until))


def restamp_all(conn: psycopg.Connection, at: datetime) -> None:
    """Move every wall-clock stamp to `at`. Only at setup, on a fresh database, where nothing is simulated yet."""
    for table, column in WALL_STAMPED:
        conn.execute(f"update {table} set {column} = %s where {column} is not null", (at,))


SCRATCH_ONLY = "The simulation runs only against a scratch database on 127.0.0.1 or localhost."
# Settings libpq reads besides the URL that can send a connection elsewhere: a host address it dials instead
# of the host name, and a service file whose entry names its own host.
SIDE_DOORS = ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE")


def check_scratch(url: str) -> None:
    """Refuse any database that isn't on this machine: the simulation writes freely.

    Every host and host address in the URL must be local, the URL may not name a service, and none of
    `SIDE_DOORS` may be set. A URL that can't be parsed is refused too (its text isn't repeated: it may
    hold a password)."""
    import os

    from psycopg import ProgrammingError
    from psycopg.conninfo import conninfo_to_dict
    try:
        params = conninfo_to_dict(url)
    except ProgrammingError:
        raise SystemExit(SCRATCH_ONLY + " That database URL can't be parsed.") from None
    if params.get("service"):
        raise SystemExit(SCRATCH_ONLY + " The URL names a service, which can name its own host.")
    found = [name for name in SIDE_DOORS if os.environ.get(name)]
    if found:
        raise SystemExit(SCRATCH_ONLY + f" Unset {', '.join(found)}: libpq would use it besides the URL.")
    hosts = [h for h in (params.get("host") or "").split(",")] + (params.get("hostaddr") or "").split(",")
    hosts = [h for h in hosts if h]
    if not hosts or any(h not in LOCAL_HOSTS for h in hosts):
        raise SystemExit(SCRATCH_ONLY)


def check_connected(conn: psycopg.Connection) -> None:
    """Refuse a connection that didn't land on this machine, whatever the URL said."""
    host, address = conn.info.host or "", getattr(conn.info, "hostaddr", "") or ""
    if host not in LOCAL_HOSTS or (address and address not in LOCAL_HOSTS):
        raise SystemExit(SCRATCH_ONLY + " This connection went somewhere else.")


@dataclass
class Event:
    at: datetime
    kind: str
    data: dict = field(default_factory=dict)


@dataclass
class State:
    seed: int
    decider: str
    model: str
    effort: str
    machine: Machine
    bridge: SimBridge
    clock: Clock
    events: list[Event]
    poll_at: datetime
    log: list[dict] = field(default_factory=list)
    days: dict = field(default_factory=dict)          # (date, slot) -> daily record
    runs: list[dict] = field(default_factory=list)
    done: bool = False
    run_day: int = 3                                  # the weekly run's weekday, Monday 0 (Thursday by default)

    @property
    def assignment(self) -> tuple[str, ...]:
        """Our slots in this season: the assignment it was given, which the simulated server hands out and
        `vending.slots` stores. Never a fixed list in the code."""
        return tuple(self.machine.slots)


def _weekly_events(cal: Calendar, check_ins: bool, run_day: int = 3) -> list[Event]:
    events = [Event(world.LEDGER_OPENS, "ledger_open"), Event(world.DAY_ONE_RUN, "agent_run", {"kind": "day-one"})]
    day = world.SIM_START.date()
    while datetime.combine(day, time(0), EASTERN) < world.COMPETITION_END:
        if datetime.combine(day, time(6), EASTERN) > world.SIM_START:
            events.append(Event(datetime.combine(day, time(6), EASTERN), "catalog_sync"))
        if day.weekday() == 2:
            events.append(Event(datetime.combine(day, time(12), EASTERN), "sourcing_check"))
        at = datetime.combine(day, world.WEEKLY_RUN_AT, EASTERN)
        if day.weekday() == run_day and world.COMPETITION_START <= at < world.COMPETITION_END:
            events.append(Event(at, "agent_run", {"kind": "weekly"}))
        at = datetime.combine(day, world.CHECK_IN_AT, EASTERN)
        if check_ins and day.weekday() == 0 and cal.is_class_day(day) and world.COMPETITION_START <= at:
            events.append(Event(at, "check_in"))
        day += timedelta(days=1)
    return events


def read_assignment(path: Path) -> tuple[str, ...]:
    """A season's slots from a file: the inventory server's answer (`GET /api/v1/slots`, the shape
    `vending.slots` stores), or just its list of slots. Checked as `vending.slots` checks the real one."""
    answer = json.loads(path.read_text(encoding="utf-8"))
    slots = answer if isinstance(answer, list) else answer.get("slots")
    device = answer.get("device_id", slot_assignment.DEVICE_ID) if isinstance(answer, dict) else slot_assignment.DEVICE_ID
    wrong = slot_assignment.problems(device, slots)
    if wrong:
        raise SystemExit(f"{path} can't be a slot assignment: " + "; ".join(wrong))
    return tuple(slots)


def new_state(seed: int, decider: str, model: str, effort: str, check_ins: bool, run_day: int = 3,
              assignment: tuple[str, ...] | list[str] = ()) -> State:
    """A season that hasn't started. `assignment` is our slots for it, from the server's answer, never a
    list written in the code (`read_assignment`)."""
    check_decider(decider)
    clock = Clock(world.SIM_START)
    machine = Machine.empty(tuple(assignment))
    bridge = SimBridge(machine, clock.now)
    return State(seed, decider, model, effort, machine, bridge, clock,
                 _weekly_events(Calendar(), check_ins, run_day), world.SIM_START + timedelta(minutes=5),
                 run_day=run_day)


# --- setup ---------------------------------------------------------------------------------------------

def setup(conn: psycopg.Connection, st: State, budget_usd: Decimal, run_cap_usd: Decimal) -> None:
    """What a person does before the first run (docs/agent.md, "Before the first run"), on the scratch DB."""
    check_connected(conn)
    now = st.clock.now()
    behind = db.pending(conn)
    if behind:
        raise SystemExit(f"migrate the scratch database first: {', '.join(behind)}")
    if conn.execute("select count(*) from agent_runs").fetchone()[0] or ledger.opening(conn) is not None:
        raise SystemExit("this database already holds a run; use a fresh scratch database (or --resume)")
    config.set_halted(conn, False, BY)
    slot_assignment.record(conn, st.bridge.slots(), now=now, by=BY)
    sync_catalog.sync(conn, st.bridge, now=now, log=lambda *_: None)
    for path in SEED_SOURCING:
        for o in sourcing.read_csv(path, BY, now):
            if o.sku in world.BY_SKU:          # the two the loading chart can't size stay out (world.py)
                sourcing.set_option(conn, o, now)
    config.set_value(conn, "model_test_daily_cap_cents", int(budget_usd * 100), BY)
    config.set_value(conn, "model_run_cap_cents", int(run_cap_usd * 100), BY)
    restamp_all(conn, now)


# --- the hours -----------------------------------------------------------------------------------------

def _day_record(st: State, day: date, slot: str) -> dict:
    key = (day.isoformat(), slot)
    if key not in st.days:
        s = st.machine.slots[slot]
        st.days[key] = {"day": day.isoformat(), "slot": slot, "sku": s.sku, "price_cents": s.price_cents,
                        "start_units": s.units, "sold": 0, "revenue_cents": 0, "unmet": 0, "lost_expected": 0.0,
                        "demand_expected": 0.0, "empty_class_hours": 0.0, "end_units": s.units}
    return st.days[key]


def sell(st: State, cal: Calendar, demand: Demand, start: datetime, end: datetime) -> None:
    """Customers between two moments, both inside one poll interval."""
    seconds = (end - start).total_seconds()
    if seconds <= 0:
        return
    mid = start + (end - start) / 2
    rates, lost = demand.slot_rates(cal, mid.replace(minute=0, second=0, microsecond=0), st.machine.slots)
    f = seconds / 3600
    day = mid.astimezone(EASTERN).date()
    for slot, s in st.machine.slots.items():
        rec = _day_record(st, day, slot)
        drawn = world.poisson(world.rng(st.seed, "sale", slot, start.isoformat()), rates[slot] * f)
        price = s.price_cents
        sold = st.machine.sell(slot, drawn)
        rec["sold"] += sold
        rec["revenue_cents"] += sold * price
        rec["unmet"] += drawn - sold
        rec["lost_expected"] += lost[slot] * f
        rec["demand_expected"] += (rates[slot] + lost[slot]) * f
        if s.units == 0 and (rates[slot] + lost[slot]) > 0:
            rec["empty_class_hours"] += f
        rec["sku"], rec["price_cents"], rec["end_units"] = s.sku, s.price_cents, s.units


def poll(conn: psycopg.Connection, st: State, at: datetime) -> None:
    meta, raw = world.poll_document(st.machine, at)
    if sync_history.store(conn, meta, raw) != "ok":
        raise RuntimeError(f"the simulated poll at {at} didn't parse")


# --- events --------------------------------------------------------------------------------------------

AMAZON_DAYS = 2      # Amazon ships in two days (assumed); in-store items are bought on the visit day


def _uses_amazon(body: dict) -> bool:
    return any((line.get(k) or {}).get("store") == "Amazon" for line in body.get("lines") or []
               for k in ("primary", "alternate"))


def _visit_time(cal: Calendar, st: State, submitted: datetime, body: dict, request_id: str) -> datetime:
    """When the worker loads a request. One visit per request, so it waits for the slowest goods."""
    r = world.rng(st.seed, "visit", request_id)
    local = submitted.astimezone(EASTERN)
    landed = local + timedelta(days=AMAZON_DAYS) if _uses_amazon(body) else local
    if body.get("off_cycle"):
        # "May take longer than 24 hours" (restocking.md): one or two open weekdays later, in the afternoon.
        day = local.date()
        for _ in range(1 if r.random() < 0.6 else 2):
            day = cal.next_open_weekday(day + timedelta(days=1))
        day = max(day, cal.next_open_weekday(landed.date()))
        return datetime.combine(day, time(13), EASTERN) + timedelta(minutes=r.randrange(0, 240))
    if submitted < world.COMPETITION_START:
        # The day-one fill is loaded before the machine goes live, if the goods are in by then.
        if landed <= world.DAY_ONE_FILL:
            return world.DAY_ONE_FILL
        day = cal.next_open_weekday(landed.date())
        return datetime.combine(day, world.VISIT_AT, EASTERN)
    day = local.date() + timedelta(days=(4 - local.weekday()) % 7)
    if day == local.date() and local.time() >= world.VISIT_AT:
        day += timedelta(days=7)
    # Goods that land after Friday's visit are loaded at the next open weekday they're in, at no fee:
    # the worker loads "as soon as it lands" (assumed). A closed Friday moves to the next open weekday.
    day = cal.next_open_weekday(max(day, landed.date()))
    return datetime.combine(day, world.VISIT_AT, EASTERN) + timedelta(minutes=r.randrange(0, 120))


def _spent(conn: psycopg.Connection) -> Decimal:
    return Decimal(conn.execute("select coalesce(sum(cost_usd), 0) from model_calls").fetchone()[0])


def _open_tools():
    from mcp.shared.memory import create_connected_server_and_client_session

    import logging

    from vending import server
    logging.getLogger("mcp").setLevel(logging.WARNING)      # "Processing request of type ..." on every call

    @contextlib.asynccontextmanager
    async def opened():
        async with create_connected_server_and_client_session(server.mcp) as session:
            listed = (await session.list_tools()).tools
            yield listed, session.call_tool
    return opened()


def _drop_cached_connection(server) -> None:
    """Close and forget the connection the MCP server's tools cache (`server._conn`)."""
    cached, server._conn = server._conn, None
    if cached is not None and not cached.closed:
        with contextlib.suppress(Exception):
            cached.close()


def run_agent(conn: psycopg.Connection, st: State, kind: str, budget: Decimal, run_cap: Decimal,
              min_run: Decimal) -> dict:
    from vending import server
    from vending.simulate.scripted import Scripted

    check_decider(st.decider)
    spent = _spent(conn)
    left = budget - spent
    record = {"kind": kind, "at": st.clock.now().isoformat(), "spent_before": str(spent)}
    if st.decider == "model" and left < min_run:
        record.update(status="not_run", detail=f"budget: ${left:.2f} left of ${budget:.2f}")
        st.runs.append(record)
        return record
    cap = min(run_cap, left) if st.decider == "model" else run_cap
    since = wall_clock(conn)
    config.set_value(conn, "model_run_cap_cents", int(cap * 100), BY)
    client = None if st.decider == "model" else Scripted(wide=st.decider == "scripted-wide")
    requests_before = set(st.bridge.requests)

    class TestHarness(Harness):
        """Every call is test spend (purpose `test`), as the brief for this run asks, so none reaches the ledger."""
        def __init__(self, client, connect, *, purpose, **kw):
            super().__init__(client, connect, purpose="test", **kw)

    # The MCP server's tools read these module globals. Point them at this season for the run only, and
    # put them back after, so a second season in the same process (or anything after it) never gets
    # this season's bridge or a connection cached in its schema.
    saved = agent.Harness, server.bridge, server.connect
    _drop_cached_connection(server)
    agent.Harness, server.bridge, server.connect = TestHarness, (lambda: st.bridge), db.connect
    try:
        run_id, out = anyio.run(lambda: agent.run(live=True, model=st.model, effort=st.effort, client=client,
                                                  now=st.clock.now, open_tools=_open_tools,
                                                  connect=db.connect))
    finally:
        agent.Harness, server.bridge, server.connect = saved
        _drop_cached_connection(server)
    at = st.clock.now()
    restamp(conn, at, since)
    conn.execute("update agent_runs set finished_at = %s where id = %s", (at + timedelta(minutes=3), run_id))
    record.update(run_id=run_id, status=out.status, detail=out.detail, request_id=out.request_id,
                  tool_calls=out.tool_calls, turns=out.turn + 1, answer=out.answer,
                  cost_usd=str(_spent(conn) - spent), run_cap=str(cap))
    st.runs.append(record)
    for request_id in sorted(set(st.bridge.requests) - requests_before):
        filed = st.bridge.requests[request_id]
        when = _visit_time(Calendar(), st, filed.submitted_at, filed.body, request_id)
        st.events.append(Event(when, "visit", {"request_id": request_id}))
        st.log.append({"at": at.isoformat(), "kind": "request_filed", "request_id": request_id,
                       "off_cycle": bool(filed.body.get("off_cycle")), "visit_at": when.isoformat()})
    return record


def handle(conn: psycopg.Connection, st: State, ev: Event, opts: dict) -> None:
    now = st.clock.now()
    if ev.kind == "ledger_open":
        ledger.record(conn, "opening", world.OPENING_CAPITAL_CENTS, "opening capital (simulation)", BY, at=now)
    elif ev.kind == "catalog_sync":
        sync_catalog.sync(conn, st.bridge, now=now, log=lambda *_: None)
    elif ev.kind == "sourcing_check":
        # The team re-checks its sourcing table each Wednesday at the shop's price that week (assumed).
        for o in sourcing.listed_options(conn):
            price = world.market_pack_cents(st.seed, o.sku, o.units_per_pack, o.store, now.astimezone(EASTERN).date())
            fresh = sourcing.Option(o.sku, o.rank, o.store, o.units_per_pack, price, sourcing.default_max(price),
                                    o.url, o.pack_description, now, BY, "weekly re-check (simulation)")
            sourcing.set_option(conn, fresh, now)
        st.log.append({"at": now.isoformat(), "kind": "sourcing_check"})
    elif ev.kind == "agent_run":
        run_agent(conn, st, ev.data["kind"], opts["budget"], opts["run_cap"], opts["min_run"])
        st.log.append({"at": now.isoformat(), "kind": "agent_run", "run": st.runs[-1].get("run_id"),
                       "status": st.runs[-1]["status"]})
    elif ev.kind == "check_in":
        counted = stock.stock(conn, until=now)
        empty = [s.slot for s in counted.slots if s.units <= 0]
        pending = any(r.status == "pending" for r in st.bridge.requests.values())
        st.log.append({"at": now.isoformat(), "kind": "check_in", "empty_by_receipts": empty, "pending": pending})
        # A Monday check-in runs the agent only when 4 or more slots are empty by our records, the
        # restock policy's bar for a $25 midweek visit (docs/restock-policy.md, rule 2).
        if len(empty) >= 4 and not pending:
            run_agent(conn, st, "check-in", opts["budget"], opts["run_cap"], opts["min_run"])
    elif ev.kind == "visit":
        request_id = ev.data["request_id"]
        directive = conn.execute("select directive from purchase_submissions where request_id = %s",
                                 (request_id,)).fetchone()
        events = Worker(st.seed).fulfil(st.bridge, request_id, now, directive[0] if directive else None)
        st.log.append({"at": now.isoformat(), "kind": "visit", "request_id": request_id, "lines": events,
                       "closet_after": dict(st.machine.closet)})
    else:
        raise ValueError(ev.kind)


# --- the loop ------------------------------------------------------------------------------------------

def save(st: State, path: Path) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(pickle.dumps(st))
    tmp.replace(path)


def load(path: Path) -> State:
    st = pickle.loads(path.read_bytes())
    st.bridge.now = st.clock.now
    return st


def run_season(conn: psycopg.Connection, st: State, opts: dict, checkpoint: Path, stop_at: datetime | None,
               say=print) -> State:
    cal, demand = Calendar(), Demand(opts.get("elasticity", 0.46), opts.get("facing_exponent", 0.5),
                                         opts.get("interloper", 0.0))
    with patched(st.clock):
        t = st.poll_at
        while t < world.COMPETITION_END:
            if stop_at is not None and t >= stop_at:
                say(f"stopped at {t.astimezone(EASTERN):%a %d %b %H:%M} ET; resume with --resume")
                save(st, checkpoint)
                return st
            nxt = t + timedelta(hours=1)
            due = sorted((e for e in st.events if t <= e.at < nxt), key=lambda e: (e.at, e.kind))
            a = t
            for ev in due:
                runs_before = len(st.runs)
                sell(st, cal, demand, a, ev.at)
                a = ev.at
                st.clock.set(ev.at)
                st.events.remove(ev)
                since = wall_clock(conn)
                handle(conn, st, ev, opts)
                restamp(conn, ev.at, since)
                if len(st.runs) > runs_before:
                    r = st.runs[-1]
                    say(f"{ev.at.astimezone(EASTERN):%a %d %b %H:%M} {r.get('kind')}: {r.get('status')}, "
                        f"${r.get('cost_usd', '0')} ({r.get('detail', '')[:140]})")
                    st.poll_at = nxt     # resume after this hour's events would re-sell the start of it
                    save(st, checkpoint)
                    st.poll_at = t
            sell(st, cal, demand, a, nxt)
            st.clock.set(nxt)
            since = wall_clock(conn)
            poll(conn, st, nxt)
            sync_requests.sync(conn, st.bridge, now=nxt, log=lambda *_: None)
            # The worker confirms the assignment every round (`vending.slots.check`); the first check from
            # the start confirms it for the competition, as live, so the tools go on reading our slots.
            slot_assignment.check(conn, st.bridge.slots(), now=nxt, by=BY)
            restamp(conn, nxt, since)
            st.poll_at = t = nxt
        st.done = True
        save(st, checkpoint)
    return st
