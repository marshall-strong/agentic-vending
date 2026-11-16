"""Tests for the full-competition simulator (`vending.simulate`).

The pure tests check the made-up world: calendar, demand, polls, the worker and the visit times. The
database test runs the first week of a season end to end with the scripted decider (no model calls)
in a throwaway schema, and checks the books against the simulated machine and the viewer's "as of".
"""

from __future__ import annotations

import urllib.request
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

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
    m = Machine.empty()
    m.slots["15"].sku, m.slots["15"].units = "REDBULL-ORIGINAL-8_4CAN", 3
    m.sell("15", 2)
    meta, raw = world.poll_document(m, datetime(2026, 12, 3, 13, 5, tzinfo=timezone.utc))
    poll = parse(raw)
    assert poll.crc_ok
    got = {s.selection: s for s in poll.selections}
    assert set(got) == set(m.slots)
    assert got["15"].lifetime_units == m.slots["15"].lifetime_units
    assert got["15"].price_cents == world.OPENING_PRICES["15"]


def test_draws_repeat_with_the_seed_and_differ_by_key():
    assert world.rng(1, "a").random() == world.rng(1, "a").random()
    assert world.rng(1, "a").random() != world.rng(1, "b").random()
    assert world.rng(1, "a").random() != world.rng(2, "a").random()


def test_demand_shares_a_product_and_counts_what_empty_slots_lose():
    cal, d = Calendar(), Demand(elasticity=0.46, facing_exponent=0.5)
    m = Machine.empty()
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


def test_directive_loads_read_the_planner_table():
    text = ("Visit: scheduled\n\nSlot  Action     Product                  Price  Load  From        Pull\n"
            "15    switch     REDBULL-ORIGINAL-8_4CAN  $4.50  11    buy         -\n"
            "16    no change  -                        $4.50  -     -           -\n"
            "17    restock    REDBULL-ORIGINAL-8_4CAN  $4.50  3     back stock  -\n")
    assert directive_loads(text) == {"15": 11, "16": 0, "17": 3}


def _line(slot, sku="REDBULL-ORIGINAL-8_4CAN", maximum="40.50", change=True, packs=1):
    return {"slot": slot, "source": "purchase", "primary": {
        "sku": sku, "store": "Amazon", "url": "https://www.amazon.com/dp/B003PFPFIE", "packs": packs,
        "units_per_pack": 24, "max_price_per_pack": maximum, "sale_price": "4.50", "change_product": change}}


def test_bridge_refuses_what_the_real_one_refuses():
    from vending.bridge import BridgeAnswered
    clock = Clock(datetime(2026, 12, 3, 19, tzinfo=EASTERN))
    m = Machine.empty()
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
    m = Machine.empty()
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
    assert m.slots["16"].price_cents == world.OPENING_PRICES["16"]


def test_visit_times():
    from vending.simulate import season
    st = season.new_state(1, "scripted", "m", "medium", False)
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
    from vending.simulate import season, viewer

    url, schema = schema_url
    monkeypatch.setattr(db, "connect", lambda: open_in(url, schema))
    monkeypatch.setenv("VENDING_TOOLS", "built")
    with open_in(url, schema) as conn:
        conn.execute("delete from slot_assignment")
        st = season.new_state(11, "scripted", "claude-opus-5-5", "medium", False)
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
    # The viewer cuts the season at a moment, and puts everything back after.
    schema_url_opts = f"{url}{'&' if '?' in url else '?'}options=-csearch_path%3D{schema}"
    with viewer.as_of(schema_url_opts, datetime(2026, 12, 1, 12, tzinfo=EASTERN)) as cut:
        assert cut.execute("select count(*) from agent_runs").fetchone()[0] == 1
        assert cut.execute("select count(*) from purchase_lines").fetchone()[0] == 0
    with open_in(url, schema) as conn:
        assert conn.execute("select count(*) from agent_runs").fetchone()[0] == len(st.runs)
