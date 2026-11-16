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

SEED_SOURCING = world.REPO / "db" / "sourcing" / "2026-11-11-amazon.csv"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
BY = "simulation"


def check_scratch(url: str) -> None:
    """Refuse any database that isn't on this machine: the simulation writes freely."""
    from psycopg.conninfo import conninfo_to_dict
    params = conninfo_to_dict(url)
    hosts = [h for h in (params.get("host") or "").split(",")] + (params.get("hostaddr") or "").split(",")
    hosts = [h for h in hosts if h]
    if not hosts or any(h not in LOCAL_HOSTS for h in hosts):
        raise SystemExit("The simulation runs only against a scratch database on 127.0.0.1 or localhost.")


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


def _weekly_events(cal: Calendar, check_ins: bool) -> list[Event]:
    events = [Event(world.LEDGER_OPENS, "ledger_open"), Event(world.DAY_ONE_RUN, "agent_run", {"kind": "day-one"})]
    day = world.SIM_START.date()
    while datetime.combine(day, time(0), EASTERN) < world.COMPETITION_END:
        if datetime.combine(day, time(6), EASTERN) > world.SIM_START:
            events.append(Event(datetime.combine(day, time(6), EASTERN), "catalog_sync"))
        if day.weekday() == 2:
            events.append(Event(datetime.combine(day, time(12), EASTERN), "sourcing_check"))
        at = datetime.combine(day, world.WEEKLY_RUN_AT, EASTERN)
        if day.weekday() == 3 and world.COMPETITION_START <= at < world.COMPETITION_END:
            events.append(Event(at, "agent_run", {"kind": "weekly"}))
        at = datetime.combine(day, world.CHECK_IN_AT, EASTERN)
        if check_ins and day.weekday() == 0 and cal.is_class_day(day) and world.COMPETITION_START <= at:
            events.append(Event(at, "check_in"))
        day += timedelta(days=1)
    return events


def new_state(seed: int, decider: str, model: str, effort: str, check_ins: bool) -> State:
    clock = Clock(world.SIM_START)
    machine = Machine.empty()
    bridge = SimBridge(machine, clock.now)
    return State(seed, decider, model, effort, machine, bridge, clock, _weekly_events(Calendar(), check_ins),
                 world.SIM_START + timedelta(minutes=5))


# --- setup ---------------------------------------------------------------------------------------------

def setup(conn: psycopg.Connection, st: State, budget_usd: Decimal, run_cap_usd: Decimal) -> None:
    """What a person does before the first run (docs/agent.md, "Before the first run"), on the scratch DB."""
    now = st.clock.now()
    behind = db.pending(conn)
    if behind:
        raise SystemExit(f"migrate the scratch database first: {', '.join(behind)}")
    if conn.execute("select count(*) from agent_runs").fetchone()[0] or ledger.opening(conn) is not None:
        raise SystemExit("this database already holds a run; use a fresh scratch database (or --resume)")
    config.set_halted(conn, False, BY)
    slot_assignment.record(conn, st.bridge.slots(), now=now, by=BY)
    sync_catalog.sync(conn, st.bridge, now=now, log=lambda *_: None)
    for o in sourcing.read_csv(SEED_SOURCING, BY, now):
        sourcing.set_option(conn, o, now)
    config.set_value(conn, "model_test_daily_cap_cents", int(budget_usd * 100), BY)
    config.set_value(conn, "model_run_cap_cents", int(run_cap_usd * 100), BY)


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

def _visit_time(cal: Calendar, st: State, submitted: datetime, off_cycle: bool, request_id: str) -> datetime:
    r = world.rng(st.seed, "visit", request_id)
    local = submitted.astimezone(EASTERN)
    if submitted < world.COMPETITION_START:
        return world.DAY_ONE_FILL          # the day-one fill is loaded before the machine goes live
    if off_cycle:
        # "May take longer than 24 hours" (restocking.md): one or two open weekdays later, in the afternoon.
        day = local.date()
        for _ in range(1 if r.random() < 0.6 else 2):
            day = cal.next_open_weekday(day + timedelta(days=1))
        return datetime.combine(day, time(13), EASTERN) + timedelta(minutes=r.randrange(0, 240))
    day = local.date() + timedelta(days=(4 - local.weekday()) % 7)
    if day == local.date() and local.time() >= world.VISIT_AT:
        day += timedelta(days=7)
    day = cal.next_open_weekday(day)     # a closed Friday (25 Dec, 1 Jan) moves to the next open weekday
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


def run_agent(conn: psycopg.Connection, st: State, kind: str, budget: Decimal, run_cap: Decimal,
              min_run: Decimal) -> dict:
    from vending import server
    from vending.simulate.scripted import Scripted

    spent = _spent(conn)
    left = budget - spent
    record = {"kind": kind, "at": st.clock.now().isoformat(), "spent_before": str(spent)}
    if st.decider == "model" and left < min_run:
        record.update(status="not_run", detail=f"budget: ${left:.2f} left of ${budget:.2f}")
        st.runs.append(record)
        return record
    cap = min(run_cap, left) if st.decider == "model" else run_cap
    config.set_value(conn, "model_run_cap_cents", int(cap * 100), BY)
    server.bridge = lambda: st.bridge
    server.connect = db.connect
    client = Scripted() if st.decider == "scripted" else None
    requests_before = set(st.bridge.requests)

    class TestHarness(Harness):
        """Every call is test spend (purpose `test`), as the brief for this run asks, so none reaches the ledger."""
        def __init__(self, client, connect, *, purpose, **kw):
            super().__init__(client, connect, purpose="test", **kw)

    saved = agent.Harness
    agent.Harness = TestHarness
    try:
        run_id, out = anyio.run(lambda: agent.run(live=True, model=st.model, effort=st.effort, client=client,
                                                  now=st.clock.now, open_tools=_open_tools))
    finally:
        agent.Harness = saved
    at = st.clock.now()
    conn.execute("update agent_runs set started_at = %s, finished_at = %s where id = %s",
                 (at, at + timedelta(minutes=3), run_id))
    # Rows stamped by the database's own clock (wall time, before the season) take the simulated time.
    conn.execute("update purchase_submissions set at = %s where at < %s", (at, world.SIM_START))
    record.update(run_id=run_id, status=out.status, detail=out.detail, request_id=out.request_id,
                  tool_calls=out.tool_calls, turns=out.turn + 1, answer=out.answer,
                  cost_usd=str(_spent(conn) - spent), run_cap=str(cap))
    st.runs.append(record)
    for request_id in sorted(set(st.bridge.requests) - requests_before):
        filed = st.bridge.requests[request_id]
        when = _visit_time(Calendar(), st, filed.submitted_at, bool(filed.body.get("off_cycle")), request_id)
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
    cal, demand = Calendar(), Demand(opts.get("elasticity", 0.46), opts.get("facing_exponent", 0.5))
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
                sell(st, cal, demand, a, ev.at)
                a = ev.at
                st.clock.set(ev.at)
                st.events.remove(ev)
                handle(conn, st, ev, opts)
                if ev.kind in ("agent_run", "check_in"):
                    r = st.runs[-1] if st.runs else {}
                    say(f"{ev.at.astimezone(EASTERN):%a %d %b %H:%M} {r.get('kind')}: {r.get('status')}, "
                        f"${r.get('cost_usd', '0')} ({r.get('detail', '')[:140]})")
                    st.poll_at = nxt     # resume after this hour's events would re-sell the start of it
                    save(st, checkpoint)
                    st.poll_at = t
            sell(st, cal, demand, a, nxt)
            st.clock.set(nxt)
            poll(conn, st, nxt)
            sync_requests.sync(conn, st.bridge, now=nxt, log=lambda *_: None)
            st.poll_at = t = nxt
        st.done = True
        save(st, checkpoint)
    return st
