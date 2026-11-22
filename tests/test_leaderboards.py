import json
from contextlib import nullcontext
from datetime import date, datetime, timedelta, timezone

import pytest

from tests.conftest import store_poll
from tests.test_dashboard import fulfilled, page_conn  # noqa: F401
from vending import dashboard, leaderboards, ledger, pages, slots, stock

OPEN = datetime(2026, 12, 4, 12, tzinfo=timezone.utc)
SATURDAY = datetime(2026, 12, 5, 5, tzinfo=timezone.utc)  # midnight Eastern
NOW = SATURDAY + timedelta(hours=12)


def product(conn, sku, name, flavor="Apple", brand="Fictional"):
    conn.execute("insert into catalog_products (sku, name, line, flavor, size, last_response)"
                 " values (%s, %s, %s, %s, '12 oz can', '{}')", (sku, name, brand, flavor))


def test_opening_assignment_and_eastern_week(conn, page_conn):
    our_slots = slots.our_slots(conn)
    first, second = our_slots[:2]
    outside = next(r + c for r in slots.SHELVES for c in slots.COLUMNS if r + c not in our_slots)
    for sku in ("SAMPLE-A", "SAMPLE-B", "SAMPLE-FALL", "SAMPLE-OUTSIDE"):
        product(conn, sku, sku)
    ledger.record(conn, "opening", 60000, "opening", "test", at=OPEN)
    fulfilled(conn, "fall", first, "SAMPLE-FALL", units_loaded=30, paid="10", at=OPEN - timedelta(days=1))
    fulfilled(conn, "a", first, "SAMPLE-A", units_loaded=10, paid="10", at=OPEN)
    fulfilled(conn, "b", second, "SAMPLE-B", units_loaded=10, paid="10", at=SATURDAY)
    fulfilled(conn, "outside", outside, "SAMPLE-OUTSIDE", units_loaded=30, paid="10", at=OPEN)
    store_poll(conn, "fall", (OPEN - timedelta(hours=1)).isoformat(), {first: (108, 45), outside: (108, 1)})
    store_poll(conn, "opening", OPEN.isoformat(), {first: (108, 50), outside: (108, 1)})
    store_poll(conn, "utc-saturday", (SATURDAY - timedelta(hours=1)).isoformat(),
               {first: (108, 52), outside: (108, 15)})
    store_poll(conn, "eastern-saturday", SATURDAY.isoformat(),
               {first: (108, 55), second: (108, 0), outside: (108, 25)})
    store_poll(conn, "saturday-noon", NOW.isoformat(), {first: (108, 56), second: (108, 2)})
    store_poll(conn, "future", (NOW + timedelta(hours=1)).isoformat(), {first: (108, 60)})

    snap = dashboard.snapshot(page_conn, NOW, public=True)
    data = snap["leaderboards"]["data"]
    assert data["since_opening"]["product"] == [
        {"name": "SAMPLE-A", "sku": "SAMPLE-A", "units": 6, "loaded": 10, "available": 10,
         "revenue": "6.00", "sell_through": 60.0},
        {"name": "SAMPLE-B", "sku": "SAMPLE-B", "units": 2, "loaded": 10, "available": 10,
         "revenue": "2.00", "sell_through": 20.0},
    ]
    a, b = data["this_week"]["product"]
    assert (a["units"], a["loaded"], a["available"], a["sell_through"], a["revenue"]) == (4, 0, 8, 50.0, "4.00")
    assert (b["units"], b["loaded"], b["available"], b["sell_through"]) == (2, 10, 10, 20.0)
    flavor, = data["this_week"]["flavor"]
    brand, = data["this_week"]["brand"]
    assert (flavor["name"], flavor["units"], flavor["available"], flavor["sell_through"]) == ("Apple", 6, 18, 33.3)
    assert (brand["name"], brand["revenue"]) == ("Fictional", "6.00")
    assert (brand["available"], brand["sell_through"]) == (18, 33.3)
    assert "SAMPLE-OUTSIDE" not in dashboard.to_json(data) and "SAMPLE-FALL" not in dashboard.to_json(data)
    html = dashboard.render(snap)
    assert html.index('id="closet"') < html.index('id="leaderboards"')


def test_opening_messages_and_empty_state(conn, page_conn):
    assert "Leaderboards start at the ledger opening" in dashboard.render(dashboard.snapshot(page_conn, NOW))
    ledger.record(conn, "opening", 60000, "opening", "test", at=OPEN)
    assert "Leaderboards start at the ledger opening" in dashboard.render(
        dashboard.snapshot(page_conn, OPEN - timedelta(seconds=1)))
    snap = dashboard.snapshot(page_conn, OPEN)
    assert snap["leaderboards"]["data"]["since_opening"]["product"] == []
    assert "No sales linked to products yet" in dashboard.render(snap)


def test_weekly_sell_through_with_a_sale_booked_after_its_swap(conn, page_conn):
    swapped, kept = slots.our_slots(conn)[:2]
    sku = "SAMPLE-CARRY"
    product(conn, sku, sku)
    ledger.record(conn, "opening", 60000, "opening", "test", at=OPEN)
    fulfilled(conn, "swapped-out", swapped, sku, units_loaded=1, paid="1", at=OPEN)
    fulfilled(conn, "kept", kept, sku, units_loaded=1, paid="1", at=OPEN)
    fulfilled(conn, "swapped-in", swapped, "SAMPLE-NEW", units_loaded=1, paid="1",
              at=SATURDAY - timedelta(hours=1))
    store_poll(conn, "opening", OPEN.isoformat(), {swapped: (108, 0), kept: (108, 0)})
    store_poll(conn, "week", NOW.isoformat(), {swapped: (216, 2, 324), kept: (108, 1)})

    data = leaderboards.read(page_conn, NOW)
    weekly = next(r for r in data["this_week"]["product"] if r["sku"] == sku)
    # Main took one unit out before the swap on Friday, but the poll that booked
    # it arrived on Saturday. Only the unit in the unchanged slot carried over.
    assert (weekly["units"], weekly["available"], weekly["sell_through"]) == (2, 1, None)
    all_time = next(r for r in data["since_opening"]["product"] if r["sku"] == sku)
    assert (all_time["units"], all_time["available"], all_time["sell_through"]) == (2, 2, 100.0)


@pytest.mark.parametrize("door_timed", [False, True])
def test_revenue_uses_the_ledger_week_tax_and_splits_a_swap(conn, page_conn, door_timed):
    selection = slots.our_slots(conn)[0]
    for sku in ("SAMPLE-OLD", "SAMPLE-NEW", "SAMPLE-AMBIGUOUS"):
        product(conn, sku, sku)
    ledger.record(conn, "opening", 60000, "opening", "test", at=OPEN)
    fulfilled(conn, "old", selection, "SAMPLE-OLD", units_loaded=10, paid="10", at=OPEN)
    fulfilled(conn, "new", selection, "SAMPLE-NEW", units_loaded=10, paid="10", at=OPEN + timedelta(hours=2))
    if door_timed:
        conn.execute("update purchase_requests set door_opened_at = %s, door_closed_at = %s where request_id = 'new'",
                     (OPEN + timedelta(hours=1), OPEN + timedelta(hours=2)))
    fulfilled(conn, "ambiguous", selection, "SAMPLE-AMBIGUOUS", units_loaded=10, paid="10",
              at=OPEN + timedelta(hours=5))
    store_poll(conn, "base", OPEN.isoformat(), {selection: (5, 0, 0)})
    store_poll(conn, "split", (OPEN + timedelta(hours=3)).isoformat(), {selection: (18, 2, 23)})
    end = OPEN + timedelta(hours=6)
    store_poll(conn, "ambiguous", end.isoformat(), {selection: (18, 3, 41)})

    snap = dashboard.snapshot(page_conn, end)
    boards = snap["leaderboards"]["data"]
    assert boards["unlinked_units"] == 1
    pnl = ledger.pnl(conn, OPEN, end)
    assert pnl.cost_of_sales_cents == 200
    assert boards["units_estimated"] == pnl.units_estimated == 1
    html = dashboard.render(snap)
    assert "1 sale not linked to a product" in html
    assert "Estimated cost for 1 unit sold across a swap" in html
    data = boards["since_opening"]
    assert [(r["sku"], r["units"], r["revenue"]) for r in data["product"]] == [
        ("SAMPLE-NEW", 1, "0.17"), ("SAMPLE-OLD", 1, "0.04"),
    ]
    assert data["brand"][0]["revenue"] == "0.21"
    assert ledger.takings(conn, OPEN, end).tax_cents == 3


def test_unlinked_units_use_the_shared_opening_baseline(conn, page_conn):
    selection = slots.our_slots(conn)[0]
    ledger.record(conn, "opening", 60000, "opening", "test", at=OPEN)
    fulfilled(conn, "load", selection, "SAMPLE-LAUNCH", units_loaded=8, paid="8", at=OPEN)
    store_poll(conn, "prior", (OPEN - timedelta(hours=1)).isoformat(), {selection: (108, 100, 10800)})
    store_poll(conn, "misread", OPEN.isoformat(), {selection: (108, 90, 9720)})
    end = OPEN + timedelta(hours=1)
    store_poll(conn, "recovered", end.isoformat(), {selection: (108, 102, 11016)})

    data = leaderboards.read(page_conn, end)
    assert data["unlinked_units"] == 2 and data["units_estimated"] == 0
    assert data["since_opening"]["product"] == []


@pytest.mark.parametrize("baseline_price, baseline_minutes", [(108, 30), (81, 30), (108, -30)])
def test_first_units_after_a_load(conn, page_conn, baseline_price, baseline_minutes):
    selection = slots.our_slots(conn)[0]
    product(conn, "SAMPLE-LAUNCH", "Sample Launch")
    ledger.record(conn, "opening", 60000, "opening", "test", at=OPEN)
    baseline = OPEN + timedelta(minutes=baseline_minutes)
    load = OPEN + timedelta(hours=1)
    end = load + timedelta(minutes=30)
    fulfilled(conn, "first", selection, "SAMPLE-LAUNCH", units_loaded=8, paid="8", at=load)
    store_poll(conn, "baseline", baseline.isoformat(), {selection: (baseline_price, 100, 10000)})
    store_poll(conn, "following", end.isoformat(), {selection: (108, 102, 10216)})

    snap = dashboard.snapshot(page_conn, end)
    data = snap["leaderboards"]["data"]
    rows = data["since_opening"]["product"]
    if baseline_minutes < 0:
        assert rows == [] and data["unlinked_units"] == 2
        assert "2 sales not linked to a product" in dashboard.render(snap)
    else:
        row, = rows
        assert (row["units"], row["revenue"], row["sell_through"]) == (2, "2.00", 25.0)
        assert data["unlinked_units"] == 0


@pytest.mark.parametrize("minutes_after_close", [-5, 0, 30])
def test_first_door_timed_load_needs_a_window_that_reaches_past_door_close(conn, page_conn, minutes_after_close):
    selection = slots.our_slots(conn)[0]
    sku = "SAMPLE-DOOR-FIRST"
    product(conn, sku, sku)
    ledger.record(conn, "opening", 60000, "opening", "test", at=OPEN)
    closed = OPEN + timedelta(hours=1)
    fulfilled(conn, "first", selection, sku, units_loaded=8, paid="8", at=OPEN + timedelta(minutes=10))
    conn.execute("update purchase_requests set door_opened_at = %s, door_closed_at = %s where request_id = 'first'",
                 (closed - timedelta(minutes=15), closed))
    store_poll(conn, "baseline", (closed - timedelta(minutes=10)).isoformat(), {selection: (108, 100)})
    end = closed + timedelta(minutes=minutes_after_close)
    store_poll(conn, "following", end.isoformat(), {selection: (108, 102)})

    data = dashboard.snapshot(page_conn, end)["leaderboards"]["data"]
    rows = data["since_opening"]["product"]
    if minutes_after_close <= 0:
        assert rows == [] and data["unlinked_units"] == 2
    else:
        row, = rows
        assert (row["sku"], row["units"], row["revenue"]) == (sku, 2, "2.00")
        assert data["unlinked_units"] == 0


def test_a_same_price_sale_after_a_door_timed_swap_follows_main(conn, page_conn):
    selection = slots.our_slots(conn)[0]
    ledger.record(conn, "opening", 60000, "opening", "test", at=OPEN)
    fulfilled(conn, "old", selection, "SAMPLE-OLD", units_loaded=8, paid="8", at=OPEN)
    opened, closed = OPEN + timedelta(hours=1), OPEN + timedelta(hours=2)
    fulfilled(conn, "swap", selection, "SAMPLE-NEW", units_loaded=8, paid="8", at=opened)
    conn.execute("update purchase_requests set door_opened_at = %s, door_closed_at = %s where request_id = 'swap'",
                 (opened, closed))
    baseline = opened + timedelta(minutes=15)
    end = closed + timedelta(minutes=15)
    store_poll(conn, "opening", OPEN.isoformat(), {selection: (108, 100)})
    store_poll(conn, "baseline", baseline.isoformat(), {selection: (108, 100)})
    store_poll(conn, "following", end.isoformat(), {selection: (108, 102)})

    sale, = stock.stock(conn, end).sales
    assert (sale.product, sale.units, sale.cost_cents) == ("SAMPLE-NEW", 2, 200)
    data = dashboard.snapshot(page_conn, end)["leaderboards"]["data"]
    row, = data["since_opening"]["product"]
    assert (row["sku"], row["units"], row["revenue"], data["unlinked_units"]) == ("SAMPLE-NEW", 2, "2.00", 0)


def test_edt_midnight_top_up_uncataloged_sku_and_weekly_tax(conn, page_conn):
    saturday = datetime(2027, 7, 10, 4, tzinfo=timezone.utc)  # midnight EDT
    opened = saturday - timedelta(days=2)
    end = saturday + timedelta(hours=2)
    selection = slots.our_slots(conn)[0]
    sku = "SAMPLE-UNCATALOGED"
    ledger.record(conn, "opening", 60000, "opening", "test", at=opened)
    fulfilled(conn, "fill", selection, sku, units_loaded=8, paid="8", at=opened)
    fulfilled(conn, "top-up", selection, sku, units_loaded=2, paid="2", at=saturday)
    store_poll(conn, "base", opened.isoformat(), {selection: (5, 0, 0)})
    store_poll(conn, "friday", (saturday - timedelta(minutes=1)).isoformat(), {selection: (5, 2, 10)})
    store_poll(conn, "saturday", saturday.isoformat(), {selection: (5, 5, 25)})
    store_poll(conn, "later", end.isoformat(), {selection: (5, 9, 45)})

    data = dashboard.snapshot(page_conn, end)["leaderboards"]["data"]
    week = data["this_week"]
    row, = week["product"]
    assert (row["sku"], row["name"], row["units"], row["loaded"], row["available"], row["sell_through"]) == (
        sku, sku, 7, 2, 8, 87.5,
    )
    assert week["flavor"][0]["name"] == week["brand"][0]["name"] == "Unknown"
    takings = ledger.takings(conn, opened, end)
    # All 45 cents are in one tax week (Sunday 4 to Saturday 10 July): 3.33 cents of tax, rounded to 3.
    # The Friday display week's 10 cents added 1 of it, the Saturday week's 35 cents the other 2.
    assert [(week.week, week.tax_cents) for week in takings.by_tax_week] == [(date(2027, 7, 4), 3)]
    assert [week.tax_cents for week in takings.by_week] == [1, 2]
    assert row["revenue"] == "0.33"
    assert data["since_opening"]["product"][0]["revenue"] == ledger.money(takings.cents - takings.tax_cents) == "0.42"


def test_back_stock_from_a_reassigned_slot_keeps_its_cost(conn, page_conn):
    assignment = slots.our_slots(conn)
    old_slot, kept_slot = assignment[:2]
    new_slot = next(r + c for r in slots.SHELVES for c in slots.COLUMNS if r + c not in assignment)
    ledger.record(conn, "opening", 60000, "opening", "test", at=OPEN)
    product(conn, "SAMPLE-CLOSET", "Sample Closet")
    fulfilled(conn, "buy", old_slot, "SAMPLE-CLOSET", units_loaded=2, to_back_stock=6, paid="8", at=OPEN)
    conn.execute("update slot_assignment set slots = %s", ([new_slot, *assignment[1:]],))
    load = OPEN + timedelta(hours=1)
    end = OPEN + timedelta(hours=2)
    fulfilled(conn, "load", kept_slot, "SAMPLE-CLOSET", units_loaded=4, paid="0", at=load)
    conn.execute("update purchase_lines set source = 'back_stock', outcome = 'loaded', total_paid = null,"
                 " packs_bought = 0, units_bought = 0, units_from_back_stock = 4 where request_id = 'load'")
    store_poll(conn, "base", load.isoformat(), {old_slot: (108, 0), kept_slot: (108, 0)})
    store_poll(conn, "later", end.isoformat(), {old_slot: (108, 20), kept_slot: (108, 2)})

    counted = stock.stock(conn, end)
    closet, = counted.closet
    assert (closet.product, closet.units, closet.unit_cost) == ("SAMPLE-CLOSET", 2, 100)
    assert counted.sales[0].cost_cents == ledger.pnl(conn, OPEN, end).cost_of_sales_cents == 200
    snap = dashboard.snapshot(page_conn, end)
    assert snap["money"]["data"]["balance"] == "594.16"
    assert snap["money"]["data"]["spendable"] == "594.00"
    data = snap["leaderboards"]["data"]
    row, = data["since_opening"]["product"]
    assert (row["units"], row["loaded"], row["revenue"], data["unlinked_units"]) == (2, 4, "2.00", 0)


def test_top_ten_and_unknown_revenue(conn, page_conn):
    ledger.record(conn, "opening", 60000, "opening", "test", at=OPEN)
    first, last = {}, {}
    for i, selection in enumerate(slots.our_slots(conn)[:11], 1):
        sku = f"SAMPLE-{i:02d}"
        product(conn, sku, sku, flavor=sku, brand=sku)
        fulfilled(conn, sku, selection, sku, units_loaded=12, paid="10", at=OPEN)
        first[selection] = (108, 0, 0)
        last[selection] = (108, i, i * 108 if i < 11 else 0)
    store_poll(conn, "first", OPEN.isoformat(), first)
    store_poll(conn, "last", NOW.isoformat(), last)

    data = dashboard.snapshot(page_conn, NOW)["leaderboards"]["data"]
    for boards in (data["this_week"], data["since_opening"]):
        for rows in boards.values():
            assert [r["units"] for r in rows] == list(range(11, 1, -1))
            assert rows[0]["revenue"] is None
            assert rows[1]["revenue"] == "10.00"


@pytest.mark.parametrize("missing", ["ledger", "assignment"])
def test_public_state_matches_the_panel_and_isolates_missing_data(conn, page_conn, monkeypatch, missing):
    ledger.record(conn, "opening", 60000, "opening", "test", at=OPEN)
    monkeypatch.setattr(pages, "configured", lambda: True)
    monkeypatch.setattr(pages, "_connection", lambda: nullcontext(page_conn))
    real_snapshot = dashboard.snapshot
    monkeypatch.setattr(dashboard, "snapshot", lambda conn, **kwargs: real_snapshot(conn, NOW, **kwargs))

    reply = pages.respond(pages.STATE)
    assert reply.status == 200
    assert json.loads(reply.body)["leaderboards"] == real_snapshot(page_conn, NOW, public=True)["leaderboards"]
    html = pages.respond(pages.DASHBOARD)
    assert html.status == 200 and 'id="leaderboards"' in html.body
    if missing == "assignment":
        conn.execute("delete from slot_assignment")
        with pytest.raises(slots.SlotsNotRecorded):
            slots.our_slots(conn)
        assert leaderboards.read(page_conn, NOW) == {"opened": True, "assignment_missing": True}
        html = pages.respond(pages.DASHBOARD)
        assert html.status == 200 and "Slot assignment unavailable." in html.body
    else:
        conn.execute("drop table ledger_entries cascade")
    reply = pages.respond(pages.STATE)
    assert reply.status == 200
    panels = json.loads(reply.body)
    if missing == "assignment":
        assert panels["leaderboards"]["data"] == {"opened": True, "assignment_missing": True}
    else:
        # The public page names no table: the reason is a fixed sentence (`dashboard_live_only`).
        assert not panels["leaderboards"]["ok"] and panels["leaderboards"]["why"] == dashboard.PUBLIC_PANEL_ERROR
        local = real_snapshot(page_conn, NOW)["leaderboards"]
        assert not local["ok"] and "migrate" in local["why"]
    assert panels["stock"]["ok"]
