"""The dashboard's pricing panel (`vending.pricing_panel`). Made-up data in a throwaway schema."""

import json
import re
from datetime import timedelta

from tests.conftest import store_poll
from tests.test_dashboard import MONDAY, catalog, fulfilled, page_conn, plan  # noqa: F401 - page_conn is a fixture
from vending import agent_runs, dashboard


def source(conn, sku, rank="primary", *, pack, expected, most, store="Walmart"):
    """A sourcing row as `python -m vending.sourcing set` stores it. Cents."""
    conn.execute("insert into sourcing_options (sku, rank, store, units_per_pack, expected_cents, max_cents,"
                 " checked_at, checked_by) values (%s, %s, %s, %s, %s, %s, %s, 'test')",
                 (sku, rank, store, pack, expected, most, MONDAY - timedelta(days=1)))


def rows(snap):
    assert snap["pricing"]["ok"], snap["pricing"]
    return {r["slot"]: r for r in snap["pricing"]["data"]["rows"]}


def section(page):
    start = page.index('<section id="pricing">')
    return page[start:page.index("</section>", start)]


def test_only_our_selections_and_nothing_about_the_others(conn, page_conn):
    # 12 isn't ours (the test assignment): a poll, a receipt, sourcing and a plan entry all name it.
    catalog(conn, ("ZZ-OTHER-SKU", "Zebra Fizz", "Zebra", None, "16 oz bottle"),
            ("FAKE-COLA-20", "Made-up Cola", "Made-up", None, "20 oz bottle"))
    source(conn, "ZZ-OTHER-SKU", pack=6, expected=987, most=990)
    fulfilled(conn, "pr-12", "12", "ZZ-OTHER-SKU", units_loaded=6, paid="5.55", at=MONDAY - timedelta(days=3))
    fulfilled(conn, "pr-15", "15", "FAKE-COLA-20", units_loaded=6, paid="6.00", at=MONDAY - timedelta(days=3))
    store_poll(conn, "p1", (MONDAY - timedelta(days=2)).isoformat(), {"12": (876, 4321), "15": (250, 10)})
    store_poll(conn, "p2", (MONDAY - timedelta(hours=1)).isoformat(), {"12": (865, 4325), "15": (250, 12)})
    run = agent_runs.start(conn, mode="live")
    agent_runs.finish(conn, run, "submitted", plan=plan(
        {"slot": "12", "sale_price": "9.15", "fill_to": 6, "reason": "secret other reason"},
        {"slot": "15", "sale_price": "2.75", "fill_to": 6, "reason": "sells out"}))
    for public in (False, True):
        snap = dashboard.snapshot(page_conn, MONDAY, public=public)
        found = rows(snap)
        assert list(found) == list(dashboard.our_selections(snap)) and "12" not in found
        panel, state = section(dashboard.render(snap)), dashboard.to_json(snap["pricing"])
        for leak in ("ZZ-OTHER", "Zebra", "8.76", "8.65", "9.15", "9.87", "9.90", "secret other", 'id="price-12"'):
            assert leak not in panel and leak not in state, leak
        assert 'id="price-15"' in panel and "sells out" in panel


def test_a_receipt_cost_gives_an_exact_floor_margin_and_legal_price(conn, page_conn):
    catalog(conn, ("FAKE-COLA-20", "Made-up Cola", "Made-up", None, "20 oz bottle"))
    # $11.10 for 12 units is 92.5 cents a unit: floor 0.925 x 1.08 = 0.999, up to $1.00. A cost
    # rounded to 93 cents first would give $1.05.
    fulfilled(conn, "pr-15", "15", "FAKE-COLA-20", units_loaded=12, paid="11.10", at=MONDAY - timedelta(days=3))
    store_poll(conn, "p1", (MONDAY - timedelta(hours=1)).isoformat(), {"15": (100, 3)})
    r = rows(dashboard.snapshot(page_conn, MONDAY))["15"]
    assert (r["price"], r["unit_cost"], r["cost_basis"], r["floor"], r["legal"]) == ("1.00", "0.93", "receipt", "1.00", True)
    assert r["margin"] == "0.00"          # $1.00 keeps 92.59 cents after tax, less 92.5
    assert r["floor_from"] == ["receipt cost 0.93: floor 1.00"]


def test_a_missing_cost_leaves_the_floor_unknown_and_an_expected_cost_is_labelled(conn, page_conn):
    catalog(conn, ("FAKE-FIZZ-12", "Made-up Fizz", "Made-up", None, "12 oz can"))
    source(conn, "FAKE-FIZZ-12", pack=24, expected=1200, most=1450)
    store_poll(conn, "p1", (MONDAY - timedelta(hours=1)).isoformat(), {"15": (250, 3), "16": (200, 9)})
    run = agent_runs.start(conn, mode="dry-run")
    agent_runs.finish(conn, run, "skipped", plan=plan(
        {"slot": "16", "product": "FAKE-FIZZ-12", "fill_to": 8, "sale_price": "2.00", "reason": "start it"}))
    snap = dashboard.snapshot(page_conn, MONDAY)
    found = rows(snap)
    unknown = found["15"]                  # no receipt, no plan, no sourcing
    assert (unknown["product"], unknown["unit_cost"], unknown["floor"], unknown["margin"]) == (None, None, None, None)
    assert unknown["legal"] is None and unknown["problems"] == []
    expected = found["16"]                 # no receipt: the plan's product and its sourcing
    assert (expected["product"], expected["product_from"]) == ("FAKE-FIZZ-12", "plan")
    assert (expected["unit_cost"], expected["cost_basis"]) == ("0.50", "expected")
    assert (expected["floor"], expected["legal"]) == ("0.70", True)   # $14.50 / 24 x 1.08 = 0.6525, up to 0.70
    assert expected["next"] is None        # the plan's price is the shelf's
    panel = section(dashboard.render(snap))
    row15 = panel[panel.index('id="price-15"'):panel.index('id="price-16"')]
    assert "floor unknown" in row15 and "unknown" in row15
    assert '<span class="pill warn">expected</span>' in panel


def test_an_illegal_price_is_flagged_now_and_in_the_plan(conn, page_conn):
    catalog(conn, ("FAKE-COLA-20", "Made-up Cola", "Made-up", None, "20 oz bottle"))
    fulfilled(conn, "pr-15", "15", "FAKE-COLA-20", units_loaded=6, paid="4.80", at=MONDAY - timedelta(days=3))
    fulfilled(conn, "pr-16", "16", "FAKE-COLA-20", units_loaded=6, paid="4.80", at=MONDAY - timedelta(days=3))
    store_poll(conn, "p1", (MONDAY - timedelta(hours=1)).isoformat(), {"15": (85, 1), "16": (252, 1)})
    run = agent_runs.start(conn, mode="dry-run")
    agent_runs.finish(conn, run, "skipped", plan=plan(
        {"slot": "15", "fill_to": 6, "sale_price": "7.00", "reason": "made-up reason"}))
    snap = dashboard.snapshot(page_conn, MONDAY)
    found = rows(snap)
    below = found["15"]                    # 80 cents a unit: floor 0.864, up to $0.90
    assert (below["floor"], below["legal"]) == ("0.90", False)
    assert below["problems"] == ["$0.85 is below the floor of $0.90; raise the price, or pick a cheaper store or "
                                 "pack (a max pack price below what the pack costs would leave the slot empty)"]
    assert below["margin"] == "-0.01"      # 85 / 1.08 = 78.7 cents kept, less 80
    assert below["next"]["legal"] is False and below["next"]["price"] == "7.00"
    assert below["next"]["problems"] == ["$7.00 is above the ceiling of $6.50"]
    assert found["16"]["legal"] is False and "not a multiple of 5 cents" in found["16"]["problems"][0]
    panel = section(dashboard.render(snap))
    assert panel.count("pr-row pr-illegal") == 2 and "2 shelf prices not legal; 1 plan price not legal." in panel
    assert "Plan: $7.00 is above the ceiling of $6.50" in panel and "&minus;$0.01" in panel
    # Needs attention says so too, once per price that fails, as a Check.
    texts = [a["text"] for a in dashboard.attention(snap) if a["level"] == "warn"]
    assert any(t.startswith("Selection 15: the shelf price $0.85 isn't legal. $0.85 is below") for t in texts)
    assert "Selection 15: the plan's price $7.00 isn't legal. $7.00 is above the ceiling of $6.50" in texts
    assert any(t.startswith("Selection 16: the shelf price $2.52 isn't legal.") for t in texts)


def test_a_price_that_fails_only_on_the_next_pack_says_so(conn, page_conn):
    catalog(conn, ("FAKE-COLA-20", "Made-up Cola", "Made-up", None, "20 oz bottle"))
    # The rail's units cost 80 cents (floor $0.90); the next pack could cost $1.50 a unit (floor $1.65).
    fulfilled(conn, "pr-15", "15", "FAKE-COLA-20", units_loaded=6, paid="4.80", at=MONDAY - timedelta(days=3))
    source(conn, "FAKE-COLA-20", pack=6, expected=800, most=900)
    store_poll(conn, "p1", (MONDAY - timedelta(hours=1)).isoformat(), {"15": (125, 1)})
    r = rows(dashboard.snapshot(page_conn, MONDAY))["15"]
    assert (r["floor"], r["legal"]) == ("1.65", False)
    assert r["problems"][-1] == ("It covers the units already in the slot (their floor is $0.90); the next pack at "
                                 "its most-we-pay price is what raises the floor.")


def test_a_price_change_shows_in_the_sparkline(conn, page_conn):
    store_poll(conn, "p0", (MONDAY - timedelta(days=40)).isoformat(), {"15": (200, 1)})   # before the window
    store_poll(conn, "p1", (MONDAY - timedelta(days=20)).isoformat(), {"15": (250, 2)})
    store_poll(conn, "p2", (MONDAY - timedelta(days=10)).isoformat(), {"15": (250, 4)})
    store_poll(conn, "p3", (MONDAY - timedelta(days=3)).isoformat(), {"15": (275, 6)})
    store_poll(conn, "p4", (MONDAY - timedelta(hours=1)).isoformat(), {"15": (275, 7)})
    snap = dashboard.snapshot(page_conn, MONDAY)
    history = rows(snap)["15"]["history"]
    # The window opens 28 days back, at the price of the poll before it ($2.00, 40 days back).
    assert [p["price"] for p in history["points"]] == ["2.00", "2.50", "2.75"]
    assert history["changes"] == 2 and history["last_at"] == MONDAY - timedelta(hours=1)
    panel = section(dashboard.render(snap))
    spark = re.search(r'<div class="pr-hist">(.*?)</div>', panel[panel.index('id="price-15"'):]).group(1)
    assert spark.count("<circle") == 2 and "2 changes" in spark
    assert "$2.00 at 12 Oct, $2.50 from 20 Oct, $2.75 from 6 Nov" in spark   # the start is the window's
    assert json.loads(dashboard.to_json(snap["pricing"]))["data"]["rows"][0]["history"]["changes"] == 2


def test_the_sparkline_draws_one_step_per_change():
    since = MONDAY - timedelta(days=28)
    history = {"points": [{"at": since, "cents": 250, "price": "2.50"},
                          {"at": since + timedelta(days=14), "cents": 300, "price": "3.00"}],
               "changes": 1, "last_at": MONDAY}
    from vending import pricing_panel
    svg = pricing_panel.sparkline(history, since, MONDAY)
    path = re.search(r'<path d="([^"]+)"', svg).group(1)
    assert path == "M3.0 23.0 H56.0 V3.0 H109.0"       # low at the start, up halfway, flat to now
    assert pricing_panel.sparkline({"points": [], "changes": 0, "last_at": None}, since, MONDAY).count("no polls") == 1


def test_empty_states(conn, page_conn):
    page = dashboard.render(dashboard.snapshot(page_conn, MONDAY))
    assert "Nothing to price yet" in section(page)
    conn.execute("delete from slot_assignment")
    snap = dashboard.snapshot(page_conn, MONDAY)
    assert snap["pricing"] == {"ok": True, "data": {**snap["pricing"]["data"], "rows": []}}
    assert "No slot assignment is stored, so nothing is priced" in section(dashboard.render(snap))


def test_a_missing_sourcing_table_blanks_only_this_panel(conn, page_conn):
    store_poll(conn, "p1", (MONDAY - timedelta(hours=1)).isoformat(), {"15": (250, 3)})
    conn.execute("drop table sourcing_options")
    snap = dashboard.snapshot(page_conn, MONDAY)
    assert not snap["pricing"]["ok"] and "sourcing_options" in snap["pricing"]["why"]
    assert snap["sales"]["ok"] and "Not available" in section(dashboard.render(snap))


def test_price_history_reads_our_slots_itself(conn):
    # Kevin, 19 Nov: no reader may take a caller's list of selections. Ours come only from the stored
    # assignment, read inside the reader, so another list can't be passed in (docs/integrity.md).
    import inspect

    import pytest

    from vending.slots import SlotsNotRecorded
    from vending.tools import machine_state
    assert list(inspect.signature(machine_state.price_history).parameters) == ["conn", "until", "since"]
    store_poll(conn, "p1", (MONDAY - timedelta(hours=1)).isoformat(), {"12": (876, 1), "15": (250, 1)})
    seen = {slot for _, slot, _ in machine_state.price_history(conn, MONDAY)}
    assert "12" not in seen and "15" in seen
    conn.execute("delete from slot_assignment")
    with pytest.raises(SlotsNotRecorded):
        machine_state.price_history(conn, MONDAY)


def test_the_wildcard_is_held_to_its_own_ceiling(conn, page_conn):
    # #166: the one wildcard may sell up to $8.00; every other selection stops at $6.50. Both the shelf
    # price and the plan's price use the planner's ceiling for that selection.
    from vending import config
    catalog(conn, ("FAKE-COLA-20", "Made-up Cola", "Made-up", None, "20 oz bottle"))
    for slot in ("15", "16"):
        fulfilled(conn, f"pr-{slot}", slot, "FAKE-COLA-20", units_loaded=6, paid="4.80", at=MONDAY - timedelta(days=3))
    config.set_value(conn, "wildcard_slot", "15", "test")
    store_poll(conn, "p1", (MONDAY - timedelta(hours=1)).isoformat(), {"15": (750, 1), "16": (750, 1)})
    run = agent_runs.start(conn, mode="dry-run")
    agent_runs.finish(conn, run, "skipped", plan=plan({"slot": "15", "fill_to": 6, "sale_price": "7.95"},
                                                      {"slot": "16", "fill_to": 6, "sale_price": "7.25"}))
    snap = dashboard.snapshot(page_conn, MONDAY)
    found = rows(snap)
    wildcard, regular = found["15"], found["16"]
    assert (wildcard["legal"], wildcard["problems"], wildcard["ceiling"]) == (True, [], "8.00")
    assert (wildcard["next"]["legal"], wildcard["next"]["problems"]) == (True, [])
    assert (regular["legal"], regular["problems"], regular["ceiling"]) == (False, ["$7.50 is above the ceiling of $6.50"], "6.50")
    assert regular["next"]["problems"] == ["$7.25 is above the ceiling of $6.50"]
    assert "the wildcard, 15, may go to $8.00" in section(dashboard.render(snap))
