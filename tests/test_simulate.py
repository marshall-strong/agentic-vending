"""Tests for the full-competition simulator (`vending.simulate`).

The pure tests check the made-up world: calendar, demand, polls, the worker and the visit times. The
database test runs the first week of a season end to end with the scripted decider (no model calls)
in a throwaway schema, and checks the books against the simulated machine.

`EXAMPLE` is the week replayer's made-up assignment (`vending.replay.cases.CASE_SLOTS`), not ours: a
season's slots are always handed in, never written into the simulator.
"""

from __future__ import annotations

import json
import urllib.request
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from vending.replay.cases import CASE_SLOTS as EXAMPLE
from vending.simulate import world
from vending.simulate.bridge import SimBridge, Worker, directive_loads
from vending.simulate.clock import Clock, RealWorldBlocked, patched
from vending.simulate.world import EASTERN, Calendar, Demand, Machine


def test_calendar_follows_the_institute():
    cal = Calendar()
    assert cal.is_class_day(date(2026, 12, 3))          # Winter Quarter starts
    assert not cal.is_class_day(date(2026, 12, 22))     # Winter Holiday Break
    assert date(2026, 12, 25) in cal.closed and not cal.is_open_weekday(date(2026, 12, 25))
    assert cal.is_open_weekday(date(2026, 12, 29))      # open, no classes
    assert cal.is_class_day(date(2027, 1, 18))          # MLK Day: open, classes in session
    assert cal.class_days(date(2026, 12, 3), date(2027, 2, 4)) == 36   # 13 in Dec, 20 in Jan, 3 in Feb


def test_polls_are_real_eva_dts_with_a_good_checksum():
    from vending.evadts import parse
    m = Machine.empty(EXAMPLE)
    m.slots["15"].sku, m.slots["15"].units = "REDBULL-ORIGINAL-8_4CAN", 3
    m.sell("15", 2)
    meta, raw = world.poll_document(m, datetime(2026, 12, 3, 13, 5, tzinfo=timezone.utc))
    poll = parse(raw)
    assert poll.crc_ok
    got = {s.selection: s for s in poll.selections}
    assert set(got) == set(m.slots)
    assert got["15"].lifetime_units == m.slots["15"].lifetime_units
    assert got["15"].price_cents == world.OPENING_PRICE_CENTS


def test_draws_repeat_with_the_seed_and_differ_by_key():
    assert world.rng(1, "a").random() == world.rng(1, "a").random()
    assert world.rng(1, "a").random() != world.rng(1, "b").random()
    assert world.rng(1, "a").random() != world.rng(2, "a").random()


def test_demand_shares_a_product_and_counts_what_empty_slots_lose():
    cal, d = Calendar(), Demand(elasticity=0.46, facing_exponent=0.5)
    m = Machine.empty(EXAMPLE)
    for s in ("15", "16"):
        m.slots[s].sku, m.slots[s].units, m.slots[s].price_cents = "REDBULL-ORIGINAL-8_4CAN", 5, 400
    nine = datetime(2026, 12, 8, 9, 0, tzinfo=EASTERN)          # a Tuesday morning
    sales, lost = d.slot_rates(cal, nine, m.slots)
    rb = world.BY_SKU["REDBULL-ORIGINAL-8_4CAN"]
    share = world.HOUR_SHARE[9] * world.DOW[1]
    assert sales["15"] == pytest.approx(rb.rate * 2 ** 0.5 / 2 * share)
    assert sales["25"] == 0 and lost["15"] == 0
    m.slots["16"].units = 0
    sales, lost = d.slot_rates(cal, nine, m.slots)
    assert sales["15"] == pytest.approx(rb.rate * share)          # the stocked one draws the product's demand
    assert lost["16"] == pytest.approx(rb.rate * (2 ** 0.5 - 1) * share)
    # Dearer sells less; a Saturday sells about nothing; a closed day sells nothing.
    m.slots["15"].price_cents = 500
    assert d.slot_rates(cal, nine, m.slots)[0]["15"] < sales["15"]
    assert d.slot_rates(cal, datetime(2026, 12, 25, 9, tzinfo=EASTERN), m.slots)[0]["15"] == 0



def test_the_interloper_takes_demand_from_the_drinks_it_sells():
    cal, nine = Calendar(), datetime(2026, 12, 8, 9, 0, tzinfo=EASTERN)
    m = Machine.empty(EXAMPLE)
    for slot, sku, price in (("15", "PEPSI-ORIGINAL-12CAN", 150), ("16", "REDBULL-ORIGINAL-8_4CAN", 400),
                             ("17", "COCA-ORIGINAL-20BOT", 200)):
        m.slots[slot].sku, m.slots[slot].units, m.slots[slot].price_cents = sku, 5, price
    none = Demand().slot_rates(cal, nine, m.slots)[0]
    some = Demand(interloper=0.25).slot_rates(cal, nine, m.slots)[0]
    assert some["15"] == pytest.approx(none["15"] * 0.75)               # same drink, same price
    assert some["16"] == pytest.approx(none["16"] * (1 - 0.25 * 0.4))   # energy against its Reign
    assert some["17"] == none["17"]                                       # it sells no Coke
    pepsi = world.BY_SKU["PEPSI-ORIGINAL-12CAN"]
    assert world.interloper_loss(pepsi, 300, 0.25) == pytest.approx(0.5)   # twice its price: the cap of 2
    assert world.interloper_loss(pepsi, 600, 0.25) == pytest.approx(0.5)
    assert world.interloper_loss(pepsi, 75, 0.25) == pytest.approx(0.125)
    assert world.interloper_loss(pepsi, 300, 0.8) == world.INTERLOPER_MAX_LOSS
    assert world.interloper_loss(pepsi, 150, 0.0) == 0.0

def test_directive_loads_read_the_planner_table():
    text = ("Visit: scheduled\n\nSlot  Action     Product                  Price  Load  From        Pull\n"
            "15    switch     REDBULL-ORIGINAL-8_4CAN  $4.50  11    buy         -\n"
            "16    no change  -                        $4.50  -     -           -\n"
            "17    restock    REDBULL-ORIGINAL-8_4CAN  $4.50  3     back stock  -\n")
    assert directive_loads(text, EXAMPLE) == {"15": 11, "16": 0, "17": 3}
    assert directive_loads(text, ("16", "17")) == {"16": 0, "17": 3}        # only the season's slots


def _line(slot, sku="REDBULL-ORIGINAL-8_4CAN", maximum="40.50", change=True, packs=1):
    return {"slot": slot, "source": "purchase", "primary": {
        "sku": sku, "store": "Amazon", "url": "https://www.amazon.com/dp/B003PFPFIE", "packs": packs,
        "units_per_pack": 24, "max_price_per_pack": maximum, "sale_price": "4.50", "change_product": change}}


def test_bridge_refuses_what_the_real_one_refuses():
    from vending.bridge import BridgeAnswered
    clock = Clock(datetime(2026, 12, 3, 19, tzinfo=EASTERN))
    m = Machine.empty(EXAMPLE)
    m.slots["15"].sku, m.slots["15"].units = "COCA-ORIGINAL-20BOT", 4
    b = SimBridge(m, clock.now)
    with pytest.raises(BridgeAnswered) as refused:
        b.post("/api/v1/purchase-requests", {"lines": [_line("15", change=False), _line("99")]})
    assert refused.value.status == 422
    assert "unacknowledged_product_change" in refused.value.detail and "slot_not_owned" in refused.value.detail
    assert b.post("/api/v1/purchase-requests", {"lines": [_line("15")]})["status"] == "pending"
    with pytest.raises(BridgeAnswered) as busy:
        b.post("/api/v1/purchase-requests", {"lines": [_line("16")]})
    assert busy.value.status == 429


def test_worker_buys_loads_pulls_and_reprices():
    at = datetime(2026, 12, 4, 15, tzinfo=EASTERN)
    m = Machine.empty(EXAMPLE)
    m.slots["15"].sku, m.slots["15"].units = "COCA-ORIGINAL-20BOT", 4
    b = SimBridge(m, lambda: at)
    rid = b.post("/api/v1/purchase-requests", {"lines": [_line("15"), _line("16", maximum="1.00")]})["id"]
    Worker(seed=7).fulfil(b, rid, at, None)
    got = b.purchase_request(rid)
    first, second = (line["receipt"] for line in got["lines"])
    assert first["outcome"] == "primary" and first["units_pulled"] == 4 and first["pulled_sku"] == "COCA-ORIGINAL-20BOT"
    assert first["units_loaded"] + first["units_to_back_stock"] + first["units_lost"] == 24
    assert m.slots["15"].sku == "REDBULL-ORIGINAL-8_4CAN" and m.slots["15"].units == first["units_loaded"] <= 11
    assert m.slots["15"].price_cents == 450
    assert m.closet["COCA-ORIGINAL-20BOT"] == 4
    assert second["outcome"] == "unavailable" and m.slots["16"].sku is None       # over its $1.00 max
    assert m.slots["16"].price_cents == world.OPENING_PRICE_CENTS


def test_the_weekly_run_can_move_to_wednesday():
    from vending.simulate import season
    runs = lambda st: [e.at for e in st.events if e.kind == "agent_run" and e.data["kind"] == "weekly"]
    thu = runs(season.new_state(1, "scripted", "m", "medium", False, 3, EXAMPLE))
    wed = runs(season.new_state(1, "scripted", "m", "medium", False, 2, EXAMPLE))
    assert {t.weekday() for t in thu} == {3} and {t.weekday() for t in wed} == {2}
    assert thu[0].date() == date(2026, 12, 3) and wed[0].date() == date(2026, 12, 9)   # none before 3 Dec
    assert wed[-1].date() == date(2027, 2, 3) and len(wed) == len(thu) == 9
    # A Wednesday Amazon order lands Friday, in time for the free visit.
    st = season.new_state(1, "scripted", "m", "medium", False, 2, EXAMPLE)
    at = season._visit_time(Calendar(), st, wed[1], {"lines": [_line("15")]}, "w")
    assert at.astimezone(EASTERN).date() == date(2026, 12, 18)


def test_visit_times():
    from vending.simulate import season
    st = season.new_state(1, "scripted", "m", "medium", False, 3, EXAMPLE)
    cal = Calendar()
    amazon = {"lines": [_line("15")]}
    walmart = {"lines": [{"slot": "15", "source": "purchase", "primary": {**_line("15")["primary"], "store": "Walmart"}}]}
    local = lambda t: t.astimezone(EASTERN)
    # Day one: in by Wed 2 Dec.
    assert season._visit_time(cal, st, world.DAY_ONE_RUN, amazon, "a") == world.DAY_ONE_FILL
    # A Thursday Amazon order lands Saturday: loaded the next Monday, not Friday.
    thu = datetime(2026, 12, 10, 19, tzinfo=EASTERN)
    assert local(season._visit_time(cal, st, thu, amazon, "b")).date() == date(2026, 12, 14)
    assert local(season._visit_time(cal, st, thu, walmart, "c")).date() == date(2026, 12, 11)
    # A closed Friday (25 Dec) moves to the next open weekday; Mon 28 Dec is closed too.
    assert local(season._visit_time(cal, st, datetime(2026, 12, 24, 19, tzinfo=EASTERN), walmart, "d")).date() \
        == date(2026, 12, 29)


def test_tripwires_block_the_real_bridge_and_the_network():
    from vending import ledger
    from vending.bridge import Bridge
    clock = Clock(datetime(2026, 12, 3, 12, tzinfo=timezone.utc))
    with patched(clock):
        with pytest.raises(RealWorldBlocked):
            Bridge("http://example.invalid", "key")
        with pytest.raises(RealWorldBlocked):
            urllib.request.urlopen("http://example.invalid")
        assert ledger.db_now(None) == clock.now()
        from vending.tools import machine_state
        assert machine_state.datetime.now(timezone.utc) == clock.now()
    assert urllib.request.urlopen is not None and ledger.db_now is not None
    from vending.tools import machine_state
    assert machine_state.datetime is datetime


def test_scratch_only():
    from vending.simulate import season
    season.check_scratch("postgresql://u:p@127.0.0.1:5432/sim")
    with pytest.raises(SystemExit):
        season.check_scratch("postgresql://u:p@example.postgres.database.azure.com/vending")
    with pytest.raises(SystemExit):
        season.check_scratch("postgresql://u:p@127.0.0.1/sim?host=example.com")


def test_first_week_end_to_end(schema_url, monkeypatch, tmp_path):
    """Day one and week 1 with the scripted decider, through the real agent loop, tools and ledger."""
    from tests.conftest import open_in
    from vending import db, ledger, stock
    from vending import slots as slot_assignment
    from vending.simulate import report, season, summary

    url, schema = schema_url
    monkeypatch.setattr(db, "connect", lambda: open_in(url, schema))
    monkeypatch.setenv("VENDING_TOOLS", "built")
    with open_in(url, schema) as conn:
        conn.execute("delete from slot_assignment")
        st = season.new_state(11, "scripted", "claude-opus-5-5", "medium", False, 3, EXAMPLE)
        with patched(st.clock):
            season.setup(conn, st, Decimal(1), Decimal(1))
        stop = datetime(2026, 12, 5, tzinfo=EASTERN)
        st = season.run_season(conn, st, {"budget": 1, "run_cap": 1, "min_run": 0}, tmp_path / "c.pickle", stop,
                               say=lambda *_: None)
        statuses = [r["status"] for r in st.runs]
        assert statuses[0] == "submitted"                       # the day-one fill
        counted = stock.stock(conn, until=stop)
        assert {s.slot: (s.product, s.units) for s in counted.slots} == {
            k: (v.sku, v.units) for k, v in st.machine.slots.items() if v.sku}
        sold = sum(r["sold"] for r in st.days.values())
        assert ledger.units_sold(conn, world.LEDGER_OPENS, stop) == sold > 0
        assert conn.execute("select count(*) from action_log where at < %s", (world.SIM_START,)).fetchone()[0] == 0
        assert conn.execute("select count(*) from agent_runs where finished_at < started_at").fetchone()[0] == 0
        # The assignment came from the season, through the simulated server, and was confirmed from the start.
        stored = slot_assignment.stored(conn)
        assert stored.slots == EXAMPLE and stored.last_checked_at >= world.COMPETITION_START
        # The full detail and the compact summary, written as a finished season writes them.
        out = tmp_path / "first-week"
        out.mkdir()
        report.write(conn, st, out, {"budget": 1}, name="First week", strategy="the scripted rule")
    data = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    summary.check(data)
    assert data["simulated"] is True and data["id"] == "first-week" and data["assignment"] == list(EXAMPLE)
    assert data["decisions"][0]["status"] == "submitted" and data["weeks"][0]["week"] == 1
    assert data["totals"]["units_sold"] == sold
    assert {p.name for p in out.iterdir()} >= {"season.json", "weeks.json", "runs.json", "daily_slots.csv"}


def test_a_season_run_settles_an_open_submission_from_its_own_bridge(schema_url, monkeypatch):
    """Sol round 3, finding 6: a season's agent run checks open submissions against the season's bridge, not
    the real one. A submission whose POST got a validated 201 that was never recorded is settled from it."""
    from psycopg.types.json import Jsonb

    from tests.conftest import open_in
    from vending import db
    from vending.simulate import season
    from vending.simulate.bridge import Filed

    url, schema = schema_url
    monkeypatch.setattr(db, "connect", lambda: open_in(url, schema))
    monkeypatch.setenv("VENDING_TOOLS", "built")
    with open_in(url, schema) as conn:
        conn.execute("delete from slot_assignment")
        st = season.new_state(11, "scripted", "claude-opus-5-5", "medium", False, 3, EXAMPLE)
        with patched(st.clock):
            season.setup(conn, st, Decimal(1), Decimal(1))
            body = {"lines": [], "off_cycle": False}
            st.bridge.requests["pr_sim_seed"] = Filed("pr_sim_seed", body, st.clock.now())
            conn.execute("insert into purchase_submissions (plan_hash, body, status, created_id)"
                         " values ('h', %s, 'sending', 'pr_sim_seed')", (Jsonb(body),))
            season.run_agent(conn, st, "weekly", Decimal(1), Decimal(1), Decimal(0))
        assert ("GET", "/api/v1/purchase-requests/pr_sim_seed") in st.bridge.calls
        assert conn.execute("select status, request_id, resolved_by from purchase_submissions"
                            " where created_id = 'pr_sim_seed'").fetchone() == ("accepted", "pr_sim_seed", "auto-check")


def test_a_season_resolves_a_refused_post_as_a_person_would(conn):
    """The submit tool leaves a refused POST `unknown`; the season knows its bridge filed nothing, so it resolves
    the row as never filed, and the next run isn't blocked."""
    from psycopg.types.json import Jsonb

    from vending import submissions
    from vending.simulate import season

    refused, other = {"lines": [{"slot": "12"}], "off_cycle": False}, {"lines": [], "off_cycle": True}
    for body in (refused, other):
        conn.execute("insert into purchase_submissions (plan_hash, body, status) values ('h', %s, 'unknown')",
                     (Jsonb(body),))
    assert season.settle_refused(conn, [{"status": 422, "body": refused}, {"status": 404, "body": other}]) == [1]
    assert conn.execute("select status, resolved_by from purchase_submissions order by id").fetchall() == [
        ("not_filed", season.BY), ("unknown", None)]
    assert submissions.unresolved(conn).code == "submission_unresolved"


def test_a_seasons_slots_come_from_its_assignment(tmp_path):
    from vending.simulate import season
    good = tmp_path / "slots.json"
    good.write_text(json.dumps({"team": "t", "device_id": "vmc1", "slots": list(EXAMPLE)}), encoding="utf-8")
    assert season.read_assignment(good) == EXAMPLE
    st = season.new_state(1, "scripted", "m", "medium", False, 3, season.read_assignment(good))
    assert st.assignment == EXAMPLE and st.bridge.slots()["slots"] == list(EXAMPLE)
    for bad in (["15", "16"], {"device_id": "other", "slots": list(EXAMPLE)}, [*EXAMPLE[:19], "59"]):
        good.write_text(json.dumps(bad), encoding="utf-8")
        with pytest.raises(SystemExit):
            season.read_assignment(good)
    with pytest.raises(ValueError):
        season.new_state(1, "scripted", "m", "medium", False)          # no assignment, no season


def test_the_wide_rule_stocks_one_sourced_drink_per_slot():
    from vending.simulate import scripted, world
    slots = scripted.wide_day_one(EXAMPLE)
    assert sorted(slots) == sorted(EXAMPLE)
    assert len(set(slots.values())) >= 10
    assert all(any(s == sku for s, _ in world.STORE_UNIT_PRICE) for sku in slots.values())


def test_restamp_moves_only_the_rows_a_step_wrote_even_after_the_season_starts(schema_url, monkeypatch):
    """Review fix: on or after 30 Nov 2026 real time is past SIM_START, so "older than the start" can't
    pick out wall-clock rows. Restamping takes the rows stamped during the step, by the database clock."""
    from tests.conftest import open_in
    from vending.simulate import season

    url, schema = schema_url
    # Real time is past the season's start, as it will be from 30 Nov 2026.
    monkeypatch.setattr(world, "SIM_START", datetime(2020, 1, 1, tzinfo=EASTERN))
    simulated = datetime(2026, 12, 9, 19, 0, tzinfo=EASTERN)
    earlier = datetime(2026, 12, 3, 8, 0, tzinfo=EASTERN)
    with open_in(url, schema) as conn:
        conn.execute("insert into action_log (kind, tool, arguments, outcome, detail, at)"
                     " values ('write', 'old', '{}', 'done', 'already simulated', %s)", (earlier,))
        since = season.wall_clock(conn)
        conn.execute("insert into action_log (kind, tool, arguments, outcome, detail)"
                     " values ('write', 'new', '{}', 'done', 'written by this step')")
        season.restamp(conn, simulated, since)
        got = dict(conn.execute("select tool, at from action_log where tool in ('old', 'new')").fetchall())
    assert got["new"] == simulated                  # the step's row is at the simulated moment
    assert got["old"] == earlier                    # a row from an earlier step keeps its simulated time


@pytest.mark.parametrize("url, env", [
    ("postgresql://u:p@127.0.0.1/sim?service=team", {}),                    # a service file names the host
    ("postgresql://u:p@127.0.0.1/sim", {"PGSERVICE": "team"}),
    ("postgresql://u:p@127.0.0.1/sim", {"PGHOSTADDR": "10.1.2.3"}),        # libpq dials this, not the name
    ("postgresql://u:p@127.0.0.1/sim?hostaddr=10.1.2.3", {}),
    ("postgresql://u:p@[oops/sim", {}),                                      # malformed: refused, not a crash
    ("host=127.0.0.1 dbname=sim service=team", {}),
])
def test_scratch_only_closes_the_side_doors(monkeypatch, url, env):
    from vending.simulate import season
    for name in ("PGSERVICE", "PGHOSTADDR", "PGSERVICEFILE"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(SystemExit):
        season.check_scratch(url)


def test_setup_checks_where_the_connection_really_went():
    from types import SimpleNamespace

    from vending.simulate import season
    st = season.new_state(1, "scripted", "m", "medium", False, 3, EXAMPLE)
    for info in (SimpleNamespace(host="db.example.com", hostaddr=""),
                 SimpleNamespace(host="localhost", hostaddr="10.1.2.3")):
        conn = SimpleNamespace(info=info, execute=lambda *_a, **_k: pytest.fail("setup touched the database"))
        with pytest.raises(SystemExit):
            season.setup(conn, st, Decimal(1), Decimal(1))


class _FakeConn:
    """Enough of a connection for run_agent's own reads and writes; the agent run itself is faked."""
    closed = False

    def execute(self, *_a, **_k):
        return self

    def fetchone(self):
        return (datetime(2026, 11, 19, tzinfo=timezone.utc),)

    def close(self):
        self.closed = True


def test_run_agent_puts_the_servers_globals_back(monkeypatch):
    """Review fix: two seasons in one process. Each run sees its own bridge, and afterwards the MCP
    server's bridge, connector and cached connection are what they were, the connection closed."""
    from types import SimpleNamespace

    from vending import agent, config, server
    from vending.simulate import season

    monkeypatch.setattr(config, "set_value", lambda *_a, **_k: None)
    monkeypatch.setattr(season, "_spent", lambda conn: Decimal(0))
    original_bridge, original_connect = server.bridge, server.connect
    stale = _FakeConn()
    monkeypatch.setattr(server, "_conn", stale)
    seen, opened = [], []

    async def fake_run(**_k):
        seen.append(server.bridge())
        assert _k["bridge"]() is server.bridge()        # startup recovery reads the season's bridge too
        assert server._conn is None                      # nothing cached from before this run
        server._conn = _FakeConn()                       # what the tools would cache
        opened.append(server._conn)
        return 1, SimpleNamespace(status="skipped", detail="", request_id=None, tool_calls=0, turn=0, answer={})

    monkeypatch.setattr(agent, "run", fake_run)
    states = [season.new_state(n, "scripted", "m", "medium", False, 3, EXAMPLE) for n in (1, 2)]
    for st in states:
        season.run_agent(_FakeConn(), st, "weekly", Decimal(1), Decimal(1), Decimal(0))
        assert server.bridge is original_bridge and server.connect is original_connect
        assert server._conn is None and opened[-1].closed
    assert seen == [states[0].bridge, states[1].bridge] and stale.closed


def test_an_unknown_decider_is_refused_never_sent_to_the_model(monkeypatch):
    """Review fix: anything but the three deciders used to fall through to live model calls."""
    from vending import agent
    from vending.simulate import season

    monkeypatch.setattr(agent, "run", lambda **_k: pytest.fail("an unknown decider reached the agent"))
    for bad in ("scripted-x", "Model", "", "rule"):
        with pytest.raises(ValueError):
            season.new_state(1, bad, "m", "medium", False, 3, EXAMPLE)
    st = season.new_state(1, "scripted", "m", "medium", False, 3, EXAMPLE)
    st.decider = "scripted-typo"                  # a tampered or old checkpoint
    with pytest.raises(ValueError):
        season.run_agent(_FakeConn(), st, "weekly", Decimal(1), Decimal(1), Decimal(0))
    assert season.DECIDERS == ("scripted", "scripted-wide", "model")


class _Crash(Exception):
    pass


def _short_season(monkeypatch, tmp_path, crash: bool):
    """30 Nov to 13:00 with the database steps faked: which events ran, which hours were polled, what sold.
    With `crash`, the process dies just after the checkpoint the day-one run writes, and is resumed."""
    from vending.simulate import season

    polls: list = []

    def handle(conn, st, ev, opts):
        st.log.append({"kind": ev.kind, "at": ev.at.isoformat()})
        if ev.kind == "agent_run":
            st.runs.append({"kind": ev.data["kind"], "status": "submitted", "run_id": len(st.runs) + 1})
            # The day-one fill, so customers have something to buy in the rest of the hour.
            for slot in list(st.machine.slots)[:4]:
                st.machine.slots[slot].sku, st.machine.slots[slot].units = "REDBULL-ORIGINAL-8_4CAN", 10

    real_save, crashed = season.save, []

    def save(st, path):
        real_save(st, path)
        if crash and st.runs and not crashed:
            crashed.append(True)
            raise _Crash

    monkeypatch.setattr(world, "OFF_DAY", 40.0)       # 30 Nov has no classes; make customers come anyway
    monkeypatch.setattr(season, "handle", handle)
    monkeypatch.setattr(season, "poll", lambda conn, st, at: polls.append(at))
    monkeypatch.setattr(season, "save", save)
    monkeypatch.setattr(season.sync_requests, "sync", lambda *_a, **_k: None)
    monkeypatch.setattr(season.slot_assignment, "check", lambda *_a, **_k: None)
    st = season.new_state(5, "scripted", "m", "medium", False, 3, EXAMPLE)
    # Later in the same hour as the day-one run (hours start at :05), so a resume that skips it loses it.
    st.events.append(season.Event(datetime(2026, 11, 30, 10, 2, tzinfo=EASTERN), "probe"))
    stop, path = datetime(2026, 11, 30, 13, 0, tzinfo=EASTERN), tmp_path / f"crash-{crash}.pickle"
    try:
        st = season.run_season(_FakeConn(), st, {}, path, stop, say=lambda *_: None)
    except _Crash:
        st = season.run_season(_FakeConn(), season.load(path), {}, path, stop, say=lambda *_: None)
    sold = {k: v["sold"] for k, v in st.days.items()}
    return [e["kind"] for e in st.log], polls, sold


def test_resume_picks_up_mid_hour_where_the_checkpoint_was_taken(monkeypatch, tmp_path):
    """Review fix: the checkpoint after a run said the hour was done, so a resumed season skipped the
    rest of it: later events in the hour, its customers and its poll."""
    events, polls, sold = _short_season(monkeypatch, tmp_path, crash=False)
    again = _short_season(monkeypatch, tmp_path, crash=True)
    assert "probe" in events and again[0] == events
    assert sorted(set(again[1])) == sorted(set(polls)) and len(again[1]) == len(polls)
    assert again[2] == sold and sum(sold.values()) > 0
