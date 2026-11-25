"""Tests for `get_machine_state`, on synthetic polls and receipts."""

from datetime import datetime, timedelta, timezone
from fractions import Fraction

from tests.conftest import TEST_SLOTS, store_poll
from vending.stock import ClosetCount, SlotCount
from vending.tools import machine_state
from vending.tools.machine_state import closet_items, get_machine_state, sold_in_window


def build(*args, **kwargs):
    """`machine_state.build` for the test assignment: the live tool passes `our_slots(conn)`."""
    return machine_state.build(*args, slots=TEST_SLOTS, **kwargs)

NOW = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)


def by_slot(states):
    return {s.slot: s for s in states}


def test_build_sales_over_the_window():
    states = by_slot(build(NOW, {"15": (250, 110)}, NOW - timedelta(days=7), {"15": 10}))
    assert len(states) == 20
    assert states["15"].price == "2.50"
    assert states["15"].units_sold_in_window == 10
    assert states["15"].units_per_day == "1.43"  # 10 / 7, rounded half up


def test_build_unknowns_are_null_not_zero():
    states = by_slot(build(NOW, {"15": (None, 5)}, NOW - timedelta(days=7), {}))
    assert states["15"].price is None
    assert states["15"].units_sold_in_window is None  # no baseline for the slot
    assert states["16"].lifetime_units is None  # slot missing from the poll
    assert states["15"].stock_on_hand is None


def test_window_sales_follow_the_counter_misread_rule():
    start, h = NOW - timedelta(days=7), timedelta(hours=1)

    def sold(*readings):  # the first reading is the poll before the window, the second its baseline
        return sold_in_window([("15", start + (n - 1) * h, v) for n, v in enumerate(readings)], start).get("15")
    assert sold(98, 100, 104, 110) == 10      # the rise up to the baseline is before the window
    assert sold(1000, 1000, 5, 1010) == 10    # a misread inside the window is not 1005 sold
    assert sold(1000, 5, 1003, 1010) == 10    # nor is a misread at the baseline poll itself
    assert sold(1000, 1000, 3, 8) == 5        # a board swap still counts the sales after it
    assert sold(1000, 1000, 3) == 0           # a lone drop at the latest poll counts nothing yet


def test_window_sales_need_a_reading_at_the_start():
    start = NOW - timedelta(days=7)
    rows = [("15", start, 100), ("15", NOW, 110), ("16", start + timedelta(days=1), 7), ("16", NOW, 9)]
    assert sold_in_window(rows, start) == {"15": 10}  # 16 wasn't in the baseline poll


def test_build_with_no_window():
    states = by_slot(build(NOW, {"15": (250, 3)}, None, {}))
    assert states["15"].units_per_day is None


def test_no_polls_says_how_to_fix_it(conn):
    reply = get_machine_state(conn)
    assert not reply.success and "sync_history" in reply.message


def test_reads_stored_polls(conn):
    store_poll(conn, "old", "2026-10-15T12:00:00+00:00", {"15": (250, 50)})
    store_poll(conn, "base", "2026-10-22T12:00:00+00:00", {"15": (250, 100), "16": (300, 7)})
    store_poll(conn, "mid", "2026-10-26T12:00:00+00:00", {"15": (250, 104)})
    store_poll(conn, "latest", "2026-10-29T12:00:00+00:00", {"15": (275, 114), "16": (300, 7), "11": (100, 999)})
    # Before `now` below and after "latest", so only the device filter keeps it out.
    store_poll(conn, "other-device", "2026-10-29T12:30:00+00:00", {"15": (1, 1)}, device_id="vmc2")
    reply = get_machine_state(conn, now=NOW + timedelta(hours=1))
    assert reply.success and reply.count == 20 and not reply.stale
    assert [s.slot for s in reply.data] == list(TEST_SLOTS)  # slot 11 isn't ours
    assert reply.as_of == NOW  # the vmc2 poll is ignored
    assert reply.window_start == NOW - timedelta(days=7)
    states = by_slot(reply.data)
    assert (states["15"].price, states["15"].units_sold_in_window, states["15"].units_per_day) == ("2.75", 14, "2.00")
    assert states["16"].units_per_day == "0.00"


def test_short_history_uses_the_first_poll(conn):
    store_poll(conn, "a", "2026-10-28T12:00:00+00:00", {"15": (250, 100)})
    store_poll(conn, "b", "2026-10-29T12:00:00+00:00", {"15": (250, 103)})
    states = by_slot(get_machine_state(conn, now=NOW).data)
    assert states["15"].units_per_day == "3.00"


def test_an_old_poll_is_flagged_stale(conn):
    store_poll(conn, "a", "2026-10-29T12:00:00+00:00", {"15": (250, 100)})
    assert not get_machine_state(conn, now=NOW + timedelta(hours=3)).stale
    reply = get_machine_state(conn, now=NOW + timedelta(hours=3, minutes=1))
    assert reply.stale and reply.message.startswith("STALE: the latest poll is 3.0 hours old")


# --- Receipt fields -------------------------------------------------------------

def count(slot="15", product="COKE20", units=20, loaded=24, sold=4, since=NOW - timedelta(days=5)):
    return SlotCount(slot, product, units, Fraction(50), loaded, sold, 0, since)


def test_build_fills_receipt_fields_for_a_slot_with_receipts():
    states = by_slot(build(NOW, {"15": (250, 114)}, NOW - timedelta(days=7), {"15": 14},
                           counts={"15": count()}, last_loaded={"15": NOW - timedelta(days=2, hours=3)},
                           sold_since_load={"15": 4}, now=NOW))
    s = states["15"]
    assert (s.product, s.stock_on_hand, s.units_sold_since_load, s.days_since_loaded) == ("COKE20", 20, 4, 2)
    assert s.days_of_stock_left == "10.0"  # 20 on hand / 2.00 a day
    assert states["16"].product is None and states["16"].stock_on_hand is None  # no receipts for 16


def test_days_of_stock_left_edges():
    def left(units, sold_in_week):
        return by_slot(build(NOW, {"15": (250, 100 + sold_in_week)}, NOW - timedelta(days=7), {"15": sold_in_week},
                             counts={"15": count(units=units)}, now=NOW))["15"].days_of_stock_left
    assert left(0, 0) == "0.0"     # empty is empty, whatever the rate
    assert left(5, 0) is None      # sold nothing: no end in sight, not "infinity"
    assert left(3, 7) == "3.0"


def test_no_rate_leaves_days_of_stock_left_null():
    states = by_slot(build(NOW, {"15": (250, 3)}, None, {}, counts={"15": count()}, now=NOW))
    assert states["15"].stock_on_hand == 20 and states["15"].days_of_stock_left is None


def test_closet_values_at_cost_and_unknown_cost_is_null():
    items, total = closet_items((ClosetCount("COKE20", 12, Fraction(100, 3)), ClosetCount("DASANI", 6, Fraction(50))))
    assert [(i.product, i.units, i.unit_cost, i.value_at_cost) for i in items] == [
        ("COKE20", 12, "0.33", "4.00"), ("DASANI", 6, "0.50", "3.00")]
    assert total == "7.00"
    _, total = closet_items((ClosetCount("COKE20", 12, None), ClosetCount("DASANI", 6, Fraction(50))))
    assert total is None


def fulfilled(conn, request_id, at, *lines):
    """A fulfilled request with receipt lines: (slot, sku, outcome, total_paid, loaded, to_closet, from_closet)."""
    conn.execute("insert into purchase_requests (request_id, submitted_at, off_cycle, request_body, status, fulfilled_at)"
                 " values (%s, %s, false, '{}', 'fulfilled', %s)", (request_id, at - timedelta(days=1), at))
    for n, (slot, sku, outcome, paid, loaded, to_closet, from_closet) in enumerate(lines):
        conn.execute(
            "insert into purchase_lines (request_id, line_no, slot, source, asked, outcome, sku, packs_bought,"
            " units_bought, total_paid, units_loaded, units_lost, units_to_back_stock, units_from_back_stock,"
            " units_pulled, slot_reassigned, resolved_at)"
            " values (%s, %s, %s, %s, '{}', %s, %s, 1, %s, %s, %s, 0, %s, %s, 0, false, %s)",
            (request_id, n, slot, "back_stock" if outcome == "loaded" else "purchase", outcome, sku,
             loaded + to_closet if outcome != "loaded" else 0, paid, loaded, to_closet, from_closet, at))


def test_reads_stock_and_closet_from_receipts(conn):
    store_poll(conn, "p0", "2026-10-22T12:00:00+00:00", {"15": (250, 100), "16": (300, 50)})
    store_poll(conn, "p1", "2026-10-24T12:00:00+00:00", {"15": (250, 100), "16": (300, 50)})
    # Load on the 24th: 20 Cokes in 15 and 4 to the closet for $12.00 (50 cents each).
    fulfilled(conn, "r1", datetime(2026, 10, 24, 15, tzinfo=timezone.utc), ("15", "COKE20", "primary", "12.00", 20, 4, 0))
    store_poll(conn, "p2", "2026-10-26T12:00:00+00:00", {"15": (250, 106), "16": (300, 50)})
    # Top-up on the 26th from the closet: 2 more Cokes.
    fulfilled(conn, "r2", datetime(2026, 10, 26, 15, tzinfo=timezone.utc), ("15", "COKE20", "loaded", None, 2, 0, 2))
    store_poll(conn, "p3", "2026-10-29T12:00:00+00:00", {"15": (250, 114), "16": (300, 50)})
    store_poll(conn, "elsewhere", "2026-10-29T13:00:00+00:00", {"15": (1, 999)}, device_id="vmc2")

    reply = get_machine_state(conn, now=NOW + timedelta(hours=2))
    s = by_slot(reply.data)["15"]
    assert s.product == "COKE20"
    assert s.stock_on_hand == 8              # 20 + 2 loaded, 14 sold; vmc2's counter is ignored
    assert s.units_sold_since_load == 8      # since the top-up on the 26th
    assert s.days_since_loaded == 2          # 26th 15:00 to 29th 14:00
    assert s.days_of_stock_left == "4.0"     # 8 / 2.00 a day
    assert [(c.product, c.units, c.value_at_cost) for c in reply.closet] == [("COKE20", 2, "1.00")]
    assert reply.closet_value_at_cost == "1.00"
    assert by_slot(reply.data)["16"].stock_on_hand is None
    assert "No receipts yet for slots" in reply.message and "16" in reply.message
    assert reply.warnings == []


def test_no_receipts_says_so_and_leaves_fields_null(conn):
    store_poll(conn, "a", "2026-10-29T12:00:00+00:00", {"15": (250, 100)})
    reply = get_machine_state(conn, now=NOW)
    assert all(s.stock_on_hand is None and s.product is None for s in reply.data)
    assert "No receipts are stored yet" in reply.message and reply.closet == []
    assert reply.closet_value_at_cost == "0.00"


def test_a_rejected_request_moves_no_stock(conn):
    store_poll(conn, "a", "2026-10-29T12:00:00+00:00", {"15": (250, 100)})
    fulfilled(conn, "r1", datetime(2026, 10, 28, tzinfo=timezone.utc), ("15", "COKE20", "primary", "12.00", 20, 0, 0))
    conn.execute("update purchase_requests set status = 'rejected'")
    assert by_slot(get_machine_state(conn, now=NOW).data)["15"].stock_on_hand is None


def test_sold_since_load_follows_the_counter_misread_rule():
    from vending import stock
    from vending.tools.machine_state import _sold_since_load
    t0, h = datetime(2026, 11, 2, 12, tzinfo=timezone.utc), timedelta(hours=1)
    line = dict(request_id="r1", line_no=0, slot="15", outcome="primary", sku="COKE20", total_paid="10.00",
                units_loaded=20, units_lost=0, units_to_back_stock=0, units_from_back_stock=0,
                units_pulled=0, pulled_sku=None, slot_reassigned=False, resolved_at=t0 + 2 * h)
    # Coke was there from a day before, so the load is a top-up: a window over a slot's first receipt has its
    # own rule (review round 2, item 5), and this test is about the misread rule.
    earlier = {**line, "request_id": "r0", "resolved_at": t0 - 24 * h}

    def sold(*readings):
        polls = [("15", t0 + (n if n < 2 else n + 1) * h, v) for n, v in enumerate(readings)]  # no poll at the load
        replay = stock.first_in_first_out(stock.movements_from_receipts([earlier, line], [])
                                          + stock.sales_from_counters(polls))
        return _sold_since_load({"15": replay.slots[0]}, t0 + 10 * h)["15"]

    assert sold(1000, 5, 1010) == 10      # a misread just before the load is not 1005 sold
    assert sold(1000, 1000, 3, 8) == 5    # a board swap after the load still counts
    assert sold(1000, 1002, 1010) == 8


def test_the_old_products_share_of_a_split_window_is_not_sold_since_the_swap():
    # #97: Arizona at $2.00 swapped for Red Bull at $4.00 at hour 3; the hour-5 poll shows
    # 2 units and 600 cents, so 1 Arizona sold before the swap and 1 Red Bull after it.
    from vending import stock
    from vending.tools.machine_state import _sold_since_load
    t0, h = datetime(2026, 11, 2, 12, tzinfo=timezone.utc), timedelta(hours=1)
    base = dict(request_id="r1", line_no=0, slot="15", outcome="primary", sku="ARIZONA", total_paid="5.00",
                units_loaded=10, units_lost=0, units_to_back_stock=0, units_from_back_stock=0,
                units_pulled=0, pulled_sku=None, slot_reassigned=False, resolved_at=t0)
    lines = [base, {**base, "request_id": "r2", "sku": "REDBULL", "total_paid": "15.00", "units_pulled": 9,
                    "pulled_sku": "ARIZONA", "slot_reassigned": True, "resolved_at": t0 + 3 * h}]
    polls = [("15", t0 + h, 100, 20000, 200), ("15", t0 + 5 * h, 102, 20600, 400)]
    replay = stock.first_in_first_out(stock.movements_from_receipts(lines, []) + stock.sales_from_counters(polls))
    assert _sold_since_load({"15": replay.slots[0]}, t0 + 6 * h)["15"] == 1


def test_a_misread_poll_moves_no_sales(conn):
    store_poll(conn, "p0", "2026-10-22T12:00:00+00:00", {"15": (250, 1000)})
    store_poll(conn, "p1", "2026-10-24T12:00:00+00:00", {"15": (250, 5)})      # misread, just before the load
    fulfilled(conn, "r1", datetime(2026, 10, 24, 15, tzinfo=timezone.utc), ("15", "COKE20", "primary", "12.00", 20, 0, 0))
    store_poll(conn, "p2", "2026-10-26T12:00:00+00:00", {"15": (250, 1010)})
    store_poll(conn, "p3", "2026-10-29T12:00:00+00:00", {"15": (250, 1014)})
    s = by_slot(get_machine_state(conn, now=NOW).data)["15"]
    assert (s.units_sold_in_window, s.units_per_day) == (14, "2.00")
    # Not 1009 sold since the load. The 10 in the window from 22 Oct to 26 Oct were measured over the slot's first
    # receipt, where nothing places them on Coke (review round 2, item 5), so only the later 4 are since the load.
    assert (s.stock_on_hand, s.units_sold_since_load) == (6, 4)
    assert s.days_of_stock_left == "3.0"


# --- A person's count (counted_*) -------------------------------------------------

def seen(slot="47", product="Something & Nothing, can", on_hand=5, at=NOW - timedelta(days=3), source="photo"):
    return {slot: {"count_id": "c1", "counted_at": at, "source": source, "product": product, "units_counted": on_hand,
                   "exact": source == "door", "price_on_tag": None, "note": None, "sold_since": 0, "on_hand": on_hand}}


def test_build_fills_counted_fields_where_receipts_dont_know_the_slot():
    states = by_slot(build(NOW, {"47": (350, 10)}, None, {}, seen=seen(), now=NOW))
    s = states["47"]
    assert (s.counted_product, s.counted_on_hand, s.count_source) == ("Something & Nothing, can", 5, "photo")
    assert s.counted_at == NOW - timedelta(days=3)
    assert s.product is None and s.stock_on_hand is None     # receipt fields stay unknown
    assert states["15"].counted_at is None                     # nobody counted 15


def test_build_a_count_seen_empty_is_zero_not_unknown():
    s = by_slot(build(NOW, {}, None, {}, seen=seen(slot="35", product=None, on_hand=0), now=NOW))["35"]
    assert s.counted_on_hand == 0 and s.counted_product is None and s.counted_at is not None


def test_build_receipts_newer_than_the_count_win():
    after = count(slot="47", since=NOW - timedelta(days=1))           # loaded after the count
    s = by_slot(build(NOW, {"47": (350, 10)}, None, {}, counts={"47": after}, seen=seen(), now=NOW))["47"]
    assert s.product == "COKE20" and s.counted_at is None
    before = count(slot="47", since=NOW - timedelta(days=5))          # the count came after the load
    s = by_slot(build(NOW, {"47": (350, 10)}, None, {}, counts={"47": before}, seen=seen(), now=NOW))["47"]
    assert s.product == "COKE20" and s.counted_on_hand == 5


def test_reads_the_latest_count_less_sales_since(conn):
    from vending import slot_counts
    store_poll(conn, "a", "2026-10-27T12:00:00+00:00", {"47": (350, 100), "35": (200, 40)})
    slot_counts.record(conn, slot_counts.parse({
        "count_id": "photo-1", "counted_at": "2026-10-27T13:00:00+00:00", "source": "photo", "device_id": "vmc1",
        "selections": [{"selection": "47", "product": "Something & Nothing, can", "units": 5, "price_on_tag": "3.50"},
                       {"selection": "35", "product": None, "units": 0, "price_on_tag": "2.00"}]}), "test")
    store_poll(conn, "b", "2026-10-28T12:00:00+00:00", {"47": (350, 100), "35": (200, 40)})
    store_poll(conn, "c", "2026-10-29T12:00:00+00:00", {"47": (350, 102), "35": (200, 40)})
    reply = get_machine_state(conn, now=NOW)
    states = by_slot(reply.data)
    assert (states["47"].counted_product, states["47"].counted_on_hand) == ("Something & Nothing, can", 3)
    assert states["35"].counted_on_hand == 0 and states["35"].counted_product is None
    assert states["15"].counted_at is None
    assert "A person's count (2026-10-27; 2 from a photo, so units are estimates)" in reply.message
    assert states["47"].counted_days_ago == 1 and "weak evidence" not in reply.message
    assert "Slots it saw holding stock: 47." in reply.message


def test_no_count_table_is_no_count(conn):
    store_poll(conn, "a", "2026-10-29T12:00:00+00:00", {"15": (250, 100)})
    conn.execute("drop table slot_counts")
    reply = get_machine_state(conn, now=NOW)
    assert reply.success and all(s.counted_at is None for s in reply.data) and "person's count" not in reply.message


def test_build_counted_days_ago_is_the_age_of_the_count():
    states = by_slot(build(NOW, {}, None, {}, seen=seen(at=NOW - timedelta(days=3, hours=5)), now=NOW))
    assert states["47"].counted_days_ago == 3
    assert states["15"].counted_days_ago is None


def test_build_an_old_count_is_still_shown_and_the_message_calls_it_weak():
    from vending.tools.machine_state import _count_note
    states = build(NOW, {}, None, {}, seen=seen(at=NOW - timedelta(days=20)), now=NOW)
    s = by_slot(states)["47"]
    assert (s.counted_on_hand, s.counted_days_ago) == (5, 20)     # shown, not hidden: the agent decides
    assert "weak evidence" in _count_note(states) and "47 (20 days)" in _count_note(states)
    fresh = build(NOW, {}, None, {}, seen=seen(at=NOW - timedelta(days=7)), now=NOW)
    assert "weak evidence" not in _count_note(fresh)               # a week is not yet old


def test_count_note_gives_the_range_of_dates_and_each_slot_source():
    from vending.tools.machine_state import _count_note
    both = {**seen(slot="47", at=NOW - timedelta(days=3), source="photo"),
            **seen(slot="35", product=None, on_hand=0, at=NOW - timedelta(days=1), source="door")}
    note = _count_note(build(NOW, {}, None, {}, seen=both, now=NOW))
    assert "(2026-10-26 to 2026-10-28; 1 from a photo, so units are estimates, 1 from the door open)" in note
    doors = _count_note(build(NOW, {}, None, {}, seen=seen(source="door"), now=NOW))
    assert "(2026-10-26; 1 from the door open)" in doors and "photo" not in doors


def test_count_note_lists_a_slot_seen_stocked_with_units_uncounted():
    from vending.tools.machine_state import _count_note
    both = {**seen(slot="15", product="Gorgie energy drink, blue can", on_hand=None),
            **seen(slot="35", product=None, on_hand=0)}
    note = _count_note(build(NOW, {}, None, {}, seen=both, now=NOW))
    assert "Slots it saw holding stock: 15." in note


def test_count_note_says_which_to_trust_when_receipts_and_count_both_show():
    from vending.tools.machine_state import _count_note
    before = count(slot="47", since=NOW - timedelta(days=5))          # the count came after the load
    states = build(NOW, {"47": (350, 10)}, None, {}, counts={"47": before}, seen=seen(), now=NOW)
    s = by_slot(states)["47"]
    assert s.stock_on_hand is not None and s.counted_on_hand == 5
    assert "Slots 47 have both stock_on_hand and counted_on_hand: the count is newer, so trust it." in _count_note(states)
    only = build(NOW, {"47": (350, 10)}, None, {}, seen=seen(), now=NOW)
    assert "both stock_on_hand" not in _count_note(only)


# --- Demand per class day while in stock (units_per_class_day) ------------------------

def test_build_without_receipts_gives_the_fall_prior_for_the_price():
    s = by_slot(build(NOW, {"15": (450, 110)}, NOW - timedelta(days=7), {"15": 10}))["15"]
    assert (s.units_per_class_day, s.class_days_in_stock) == ("1.354", None)
    assert s.rate_basis.startswith("fall prior: mid seller, premium-priced")
    assert s.class_days_of_stock_left is None                      # nothing known on hand


def test_build_uses_the_newer_count_for_class_days_left():
    s = by_slot(build(NOW, {"47": (250, 10)}, None, {}, seen=seen(on_hand=5), now=NOW))["47"]
    assert s.class_days_of_stock_left == "2.9"                     # 5 / 1.701, the fast prior at $2.50


def test_an_emptied_slot_rates_only_the_days_it_held_stock(conn):
    friday = datetime(2026, 10, 16, 20, tzinfo=timezone.utc)       # 4 PM Eastern
    store_poll(conn, "p0", "2026-10-16T12:00:00+00:00", {"15": (250, 100), "16": (300, 50)})
    fulfilled(conn, "r1", friday, ("15", "COKE20", "primary", "12.00", 8, 0, 0))
    store_poll(conn, "p1", "2026-10-19T22:00:00+00:00", {"15": (250, 103), "16": (300, 50)})
    store_poll(conn, "p2", "2026-10-20T22:00:00+00:00", {"15": (250, 106), "16": (300, 50)})
    store_poll(conn, "p3", "2026-10-21T22:00:00+00:00", {"15": (250, 108), "16": (300, 50)})   # empty from here
    store_poll(conn, "p4", "2026-10-22T22:00:00+00:00", {"15": (250, 108), "16": (300, 50)})
    fulfilled(conn, "r2", friday + timedelta(days=7), ("15", "COKE20", "primary", "12.00", 8, 0, 0))
    store_poll(conn, "p5", "2026-10-26T22:00:00+00:00", {"15": (250, 111), "16": (300, 50)})
    store_poll(conn, "p6", "2026-10-27T22:00:00+00:00", {"15": (250, 113), "16": (300, 50)})
    states = by_slot(get_machine_state(conn, now=datetime(2026, 10, 28, 12, tzinfo=timezone.utc)).data)
    s = states["15"]
    # Tue-Wed, then Mon-Wed: not Thu 22 Oct. The 3 the Monday poll measured from Friday morning, over the slot's
    # first receipt, can't be placed on Coke (review round 2, item 5): not in the units, and Mon 19 Oct, in their
    # window, not an in-stock day. They were 13 over 6 when the window counted as Coke's.
    assert s.class_days_in_stock == 5
    assert (s.units_per_class_day, s.rate_basis) == ("2.00", "own: 10 sold over 5 in-stock class days")
    assert (s.stock_on_hand, s.class_days_of_stock_left) == (3, "1.5")
    assert states["16"].units_per_class_day == "0.722" and states["16"].class_days_in_stock is None


def test_a_load_is_dated_by_door_close_when_the_receipt_has_door_times(conn):
    # The door closed at 12:30 but the worker filled in the line at 15:00. At 13:00 five days on, it is 5 days
    # since the door closed but 4 since resolved_at. The 13:00 poll's 3 sales were measured from 10:00, over the
    # slot's first receipt (the door opened at 12:00), so nothing places them on Coke (review round 2, item 5):
    # they were counted as sold since the load until then. The 2 measured from 13:00 came after the door closed,
    # so they are sold since it. Without door times the load is at 15:00, inside that window too: none are.
    now = datetime(2026, 10, 29, 13, tzinfo=timezone.utc)

    def state(door):
        conn.execute("delete from purchase_requests")
        conn.execute("delete from polls")
        store_poll(conn, "p0", "2026-10-24T10:00:00+00:00", {"15": (250, 100)})
        fulfilled(conn, "r1", datetime(2026, 10, 24, 15, tzinfo=timezone.utc),
                  ("15", "COKE20", "primary", "12.00", 20, 0, 0))
        if door:
            conn.execute("update purchase_requests set door_opened_at = %s, door_closed_at = %s",
                         (datetime(2026, 10, 24, 12, tzinfo=timezone.utc), datetime(2026, 10, 24, 12, 30, tzinfo=timezone.utc)))
        store_poll(conn, "p1", "2026-10-24T13:00:00+00:00", {"15": (250, 103)})
        store_poll(conn, "p2", "2026-10-29T10:00:00+00:00", {"15": (250, 105)})
        return by_slot(get_machine_state(conn, now=now).data)["15"]

    s = state(door=True)
    assert (s.units_sold_since_load, s.stock_on_hand, s.days_since_loaded) == (2, 15, 5)
    s = state(door=False)
    assert (s.units_sold_since_load, s.stock_on_hand, s.days_since_loaded) == (0, 18, 4)


def test_a_load_dated_after_now_is_not_the_latest_load_yet(conn):
    # At 10:00 a receipt filled in at 09:55 says the door closed at 10:05. The stock as of 10:00
    # doesn't have it yet, so it can't be the latest load either: days_since_loaded stays with r1's.
    now = datetime(2026, 10, 29, 10, tzinfo=timezone.utc)
    store_poll(conn, "p0", "2026-10-24T10:00:00+00:00", {"15": (250, 100)})
    fulfilled(conn, "r1", datetime(2026, 10, 24, 15, tzinfo=timezone.utc), ("15", "COKE20", "primary", "12.00", 20, 0, 0))
    fulfilled(conn, "r2", datetime(2026, 10, 29, 9, 55, tzinfo=timezone.utc), ("15", "COKE20", "primary", "6.00", 10, 0, 0))
    conn.execute("update purchase_requests set door_opened_at = %s, door_closed_at = %s where request_id = 'r2'",
                 (datetime(2026, 10, 29, 9, 58, tzinfo=timezone.utc), datetime(2026, 10, 29, 10, 5, tzinfo=timezone.utc)))
    store_poll(conn, "p1", "2026-10-29T09:00:00+00:00", {"15": (250, 104)})
    s = by_slot(get_machine_state(conn, now=now).data)["15"]
    # The 4 were measured from before r1, the slot's first receipt, so nothing places them on Coke (review round
    # 2, item 5): in the stock, not sold since the load (4 when that window counted as Coke's).
    assert (s.stock_on_hand, s.units_sold_since_load, s.days_since_loaded) == (16, 0, 4)
    later = by_slot(get_machine_state(conn, now=now + timedelta(hours=1)).data)["15"]
    assert (later.stock_on_hand, later.days_since_loaded) == (26, 0)


def test_warnings_about_a_slot_we_no_longer_hold_are_left_out(conn):
    # Two visits to slot 15 share a minute and were submitted together, so the stock warns about
    # both. After a reassignment that takes 15 away, the agent's machine state leaves them out,
    # as the dashboard does.
    store_poll(conn, "p0", "2026-10-24T10:00:00+00:00", {"15": (250, 100), "16": (300, 50)})
    at = datetime(2026, 10, 24, 15, tzinfo=timezone.utc)
    fulfilled(conn, "r1", at, ("15", "COKE20", "primary", "12.00", 20, 0, 0))
    fulfilled(conn, "r2", at, ("15", "COKE20", "primary", "6.00", 10, 0, 0))
    conn.execute("update purchase_requests set door_opened_at = %s, door_closed_at = %s",
                 (datetime(2026, 10, 24, 14, tzinfo=timezone.utc), datetime(2026, 10, 24, 14, 30, tzinfo=timezone.utc)))
    store_poll(conn, "p1", "2026-10-29T09:00:00+00:00", {"15": (250, 104), "16": (300, 50)})
    clashes = [w for w in get_machine_state(conn, now=NOW).warnings if "can't be told" in w]
    assert len(clashes) == 2
    conn.execute("update slot_assignment set slots = %s", ([s for s in TEST_SLOTS if s != "15"] + ["11"],))
    reply = get_machine_state(conn, now=NOW)
    assert not any("slot 15" in w for w in reply.warnings)


def test_door_openings_with_no_receipt_of_ours_change_nothing(conn):
    # #172: the door counter (EA2*DO) is machine-wide. It rises midweek (Wed 28 Oct, 10:00 to 11:00)
    # and twice more during our Friday visit on 30 Oct, from openings that aren't ours: no receipt
    # of ours goes with them. Stock and machine state must read exactly as with a counter that rises
    # only at our own visits. Slot 15 swaps Coke ($2.50) for Pepsi ($2.75) at that Friday visit.
    from vending.stock import stock

    now = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
    polls = [  # (session, time, slot 15 (price, units, cents), door counter ours only, with others' openings)
        ("p0", "2026-10-22T12:00:00+00:00", (250, 100, 25000), 5, 5),
        ("p1", "2026-10-23T20:00:00+00:00", (250, 100, 25000), 6, 6),     # our first load, Fri 23 Oct
        ("p2", "2026-10-28T10:00:00+00:00", (250, 104, 26000), 6, 6),
        ("p3", "2026-10-28T11:00:00+00:00", (250, 105, 26250), 6, 7),     # a midweek opening, not ours
        ("p4", "2026-10-30T18:00:00+00:00", (250, 107, 26750), 6, 7),
        ("p5", "2026-10-30T20:00:00+00:00", (275, 108, 27025), 7, 10),    # our visit, and two that overlap it
        ("p6", "2026-11-02T10:00:00+00:00", (275, 111, 27850), 7, 10),
    ]

    def read(others):
        conn.execute("delete from purchase_requests")
        conn.execute("delete from polls")
        for session_id, at, slot15, ours, theirs in polls:
            store_poll(conn, session_id, at, {"15": slot15, "16": (300, 50)}, door=theirs if others else ours)
        fulfilled(conn, "r1", datetime(2026, 10, 23, 19, 30, tzinfo=timezone.utc),
                  ("15", "COKE20", "primary", "12.00", 20, 0, 0))
        fulfilled(conn, "r2", datetime(2026, 10, 30, 19, 30, tzinfo=timezone.utc),
                  ("15", "PEPSI20", "primary", "10.00", 20, 0, 0))
        conn.execute("update purchase_lines set units_pulled = 13, pulled_sku = 'COKE20', slot_reassigned = true"
                     " where request_id = 'r2'")
        conn.execute("update purchase_requests set door_opened_at = %s, door_closed_at = %s where request_id = 'r2'",
                     (datetime(2026, 10, 30, 19, 0, tzinfo=timezone.utc),
                      datetime(2026, 10, 30, 19, 20, tzinfo=timezone.utc)))
        return stock(conn, until=now), get_machine_state(conn, now=now)

    ours_only, with_others = read(others=False), read(others=True)
    assert with_others == ours_only
    counted, reply = with_others
    [slot] = counted.slots
    assert (slot.product, slot.loaded, slot.units) == ("PEPSI20", 20, 16)
    assert by_slot(reply.data)["15"].stock_on_hand == 16


# --- Demand across a product change (#172 item 3) -----------------------------------

def swap(conn, new_sku, polls, now, units=20, then=None, fill=False):
    """Slot 15: Coke loaded Fri 23 Oct, swapped for `units` of `new_sku` at the Friday visit of 30 Oct
    (3:30 PM Eastern).

    `polls` are (time, (price, units, cents)) after the last pre-swap poll; no door times, so a window
    across the swap is split by the cents counter, or left unattributed when it can't be. `then(conn)`
    adds later receipts before the state is read. `fill` adds a poll every 3 hours between `polls`, for the
    slow-seller clock (`readings_of_15`)."""
    store_poll(conn, "p0", "2026-10-23T12:00:00+00:00", {"15": (200, 100, 20000)})
    fulfilled(conn, "r1", datetime(2026, 10, 23, 19, 30, tzinfo=timezone.utc), ("15", "COKE20", "primary", "12.00", 20, 0, 0))
    store_poll(conn, "p1", "2026-10-28T12:00:00+00:00", {"15": (200, 104, 20800)})
    store_poll(conn, "p2", "2026-10-30T18:00:00+00:00", {"15": (200, 106, 21200)})   # 14 Cokes left
    fulfilled(conn, "r2", datetime(2026, 10, 30, 19, 30, tzinfo=timezone.utc), ("15", new_sku, "primary", "20.00", units, 0, 0))
    conn.execute("update purchase_lines set units_pulled = 13, pulled_sku = 'COKE20', slot_reassigned = true"
                 " where request_id = 'r2'")
    if fill:
        readings_of_15(conn, [(f"q{n}", at, reading) for n, (at, reading) in enumerate(polls)])
    else:
        for n, (at, reading) in enumerate(polls):
            store_poll(conn, f"q{n}", at, {"15": reading})
    if then is not None:
        then(conn)
    return by_slot(get_machine_state(conn, now=now).data)["15"]


def test_the_old_products_share_of_a_split_window_is_not_the_new_products_demand(conn):
    # Coke at $2.00 for Red Bull at $4.00. The Monday poll's window holds the swap: 3 units for
    # $10.00, so 1 Coke before it and 2 Red Bulls after. Red Bull sold 10 over 5 class days.
    s = swap(conn, "REDBULL", [
        ("2026-11-02T14:00:00+00:00", (400, 109, 22200)),
        ("2026-11-04T14:00:00+00:00", (400, 113, 23800)),
        ("2026-11-06T14:00:00+00:00", (400, 117, 25400)),
    ], now=datetime(2026, 11, 6, 22, tzinfo=timezone.utc))
    assert (s.product, s.stock_on_hand, s.units_sold_since_load) == ("REDBULL", 10, 10)
    assert (s.units_per_class_day, s.rate_basis) == ("2.00", "own: 10 sold over 5 in-stock class days")
    assert s.class_days_in_stock == 5


def test_an_unattributed_swap_window_is_left_out_of_demand_and_sold_since_load(conn):
    # Same price on both sides, so the window across the swap (1 unit, polled at 6 PM Eastern the same
    # Friday) can't be split. That unit may have been a Coke: it is neither sold since the load nor in
    # the rate. The window ends on the swap's own day, so it costs no class day.
    s = swap(conn, "PEPSI20", [
        ("2026-10-30T22:00:00+00:00", (200, 107, 21400)),
        ("2026-11-03T14:00:00+00:00", (200, 111, 22200)),
        ("2026-11-05T14:00:00+00:00", (200, 114, 22800)),
        ("2026-11-06T20:00:00+00:00", (200, 117, 23400)),
    ], now=datetime(2026, 11, 6, 22, tzinfo=timezone.utc))
    assert (s.product, s.stock_on_hand) == ("PEPSI20", 9)
    assert (s.units_sold_since_load, s.units_per_class_day, s.class_days_in_stock, s.rate_basis) == (
        10, "2.00", 5, "own: 10 sold over 5 in-stock class days")


def test_a_multi_day_unattributed_window_drops_its_class_days_from_the_in_stock_days(conn):
    # The polls stop over the weekend and Monday: the first after the swap is Tue 3 Nov, 9 AM Eastern,
    # 3 units at the same price, unsplit. Mon 2 and Tue 3 Nov sold units nobody can place, so neither
    # counts as an in-stock day of Pepsi's: 10 sold over the 5 days left (Wed 4 to Tue 10), not 13 over 7.
    s = swap(conn, "PEPSI20", [
        ("2026-11-03T14:00:00+00:00", (200, 109, 21800)),
        ("2026-11-05T14:00:00+00:00", (200, 113, 22600)),
        ("2026-11-09T14:00:00+00:00", (200, 117, 23400)),
        ("2026-11-10T14:00:00+00:00", (200, 119, 23800)),
    ], now=datetime(2026, 11, 10, 22, tzinfo=timezone.utc))
    assert s.product == "PEPSI20"
    assert (s.units_sold_since_load, s.units_per_class_day, s.class_days_in_stock, s.rate_basis) == (
        10, "2.00", 5, "own: 10 sold over 5 in-stock class days")


def test_demand_counts_only_the_current_products_loads():
    # One receipt tops up Coke (5) and swaps to Pepsi (2) at the same moment. Pepsi sells out on Mon 2 Nov
    # and is refilled Thu 5 Nov. The Coke units never were Pepsi's stock, so they don't keep Pepsi in
    # stock over Tue 3 and Wed 4 Nov: its in-stock class days are Mon 2 and Fri 6 Nov. The count comes from
    # the replay, since demand reads the run's history from it (round 3).
    from vending.stock import Movement, replay
    from vending.tools.machine_state import _demands
    utc = timezone.utc
    at, now = datetime(2026, 10, 30, 19, 30, tzinfo=utc), datetime(2026, 11, 6, 22, tzinfo=utc)
    [pepsi] = replay([
        Movement(at, "purchase", "15", "COKE20", to_slot=5, paid_cents=Fraction(500)),
        Movement(at, "purchase", "15", "PEPSI20", to_slot=2, paid_cents=Fraction(200), reassigned=True),
        Movement(datetime(2026, 11, 2, 22, tzinfo=utc), "sale", "15", None, units=2,
                 since=datetime(2026, 11, 2, 12, tzinfo=utc)),
        Movement(datetime(2026, 11, 5, 19, tzinfo=utc), "purchase", "15", "PEPSI20", to_slot=2,
                 paid_cents=Fraction(200))], now).slots
    d = _demands({"15": pepsi}, {"15": (250, 0)}, now)
    assert (pepsi.product, pepsi.loaded, d["15"].units_sold, d["15"].class_days_in_stock) == ("PEPSI20", 4, 2, 2)


def test_unattributed_sales_can_empty_the_slot_so_its_days_arent_counted_in_stock(conn):
    # Review round 1, item 1. The swap puts in 1 Pepsi. Monday's poll, the first after it, shows 1 unit
    # sold that the counters can't place, so 0 or 1 Pepsi is left: it may have stood empty all week.
    # 5 more go in on Fri 6 Nov. Only Mon 9 Nov is surely in stock, so it is not 0 sold over 5 days.
    s = swap(conn, "PEPSI20", [
        ("2026-11-02T14:00:00+00:00", (200, 107, 21400)),
        ("2026-11-04T14:00:00+00:00", (200, 107, 21400)),
        ("2026-11-06T14:00:00+00:00", (200, 107, 21400)),
        ("2026-11-09T14:00:00+00:00", (200, 107, 21400)),
    ], now=datetime(2026, 11, 9, 22, tzinfo=timezone.utc), units=1, then=lambda conn: fulfilled(
        conn, "r3", datetime(2026, 11, 6, 19, 30, tzinfo=timezone.utc), ("15", "PEPSI20", "primary", "5.00", 5, 0, 0)))
    assert (s.product, s.stock_on_hand) == ("PEPSI20", 5)
    assert (s.class_days_in_stock, s.rate_basis.startswith("fall prior")) == (1, True)


def test_a_later_unplaced_window_drops_only_the_days_it_covers(conn):
    # Review round 1, item 2. Pepsi went in on 30 Oct and sold Mon to Fri. At the 6 Nov visit to swap it
    # for Red Bull (door open 19:00 to 19:40 UTC), two polls inside the open door show 1 unit sold: a sale
    # the receipt contradicts, left unattributed. Read at 19:30, before the door closes, the slot still
    # holds Pepsi. Only Fri 6 Nov is in that window, so Pepsi keeps Mon 2 to Thu 5 Nov: 4 days, not 0.
    def red_bull(conn):
        fulfilled(conn, "r3", datetime(2026, 11, 6, 19, 25, tzinfo=timezone.utc),
                  ("15", "REDBULL", "primary", "20.00", 20, 0, 0))
        conn.execute("update purchase_lines set units_pulled = 11, pulled_sku = 'PEPSI20', slot_reassigned = true"
                     " where request_id = 'r3'")
        conn.execute("update purchase_requests set door_opened_at = %s, door_closed_at = %s where request_id = 'r3'",
                     (datetime(2026, 11, 6, 19, tzinfo=timezone.utc), datetime(2026, 11, 6, 19, 40, tzinfo=timezone.utc)))

    s = swap(conn, "PEPSI20", [
        ("2026-10-30T22:00:00+00:00", (200, 106, 21200)),
        ("2026-11-02T22:00:00+00:00", (200, 108, 21600)),
        ("2026-11-03T22:00:00+00:00", (200, 110, 22000)),
        ("2026-11-04T22:00:00+00:00", (200, 112, 22400)),
        ("2026-11-05T22:00:00+00:00", (200, 114, 22800)),
        ("2026-11-06T14:00:00+00:00", (200, 115, 23000)),
        ("2026-11-06T19:10:00+00:00", (200, 115, 23000)),
        ("2026-11-06T19:20:00+00:00", (200, 116, 23200)),
    ], now=datetime(2026, 11, 6, 19, 30, tzinfo=timezone.utc), then=red_bull)
    assert s.product == "PEPSI20"
    assert (s.class_days_in_stock, s.units_per_class_day is not None) == (4, True)
    assert "4 in-stock class days of our own so far" in s.rate_basis


def test_a_product_that_comes_back_at_the_same_moment_counts_only_its_last_load(conn):
    # Review round 1, item 3. Three receipts share the 30 Oct visit's door times and replay in the order
    # they were submitted: 5 Coke, then 3 Pepsi (pulling the Coke), then 2 Coke (pulling the Pepsi).
    # The 2 Coke sell out on Monday and 4 go in on Thursday. The first 5 Coke aren't this run's, so
    # the slot held stock on Mon 2 and Fri 6 Nov only: 2 class days, not 5.
    utc = timezone.utc
    store_poll(conn, "p0", "2026-10-30T18:00:00+00:00", {"15": (250, 100)})
    for n, (rid, sku, units, pulled) in enumerate((("ra", "COKE20", 5, None), ("rb", "PEPSI20", 3, "COKE20"),
                                                   ("rc", "COKE20", 2, "PEPSI20"))):
        fulfilled(conn, rid, datetime(2026, 10, 30, 19, 30, tzinfo=utc), ("15", sku, "primary", "5.00", units, 0, 0))
        conn.execute("update purchase_requests set submitted_at = %s, door_opened_at = %s, door_closed_at = %s"
                     " where request_id = %s", (datetime(2026, 10, 29, 12, n, tzinfo=utc),
                                                datetime(2026, 10, 30, 19, tzinfo=utc),
                                                datetime(2026, 10, 30, 19, 20, tzinfo=utc), rid))
        if pulled:
            conn.execute("update purchase_lines set units_pulled = %s, pulled_sku = %s, slot_reassigned = true"
                         " where request_id = %s", (5 if pulled == "COKE20" else 3, pulled, rid))
    store_poll(conn, "p1", "2026-10-30T22:00:00+00:00", {"15": (250, 100)})
    store_poll(conn, "p2", "2026-11-02T22:00:00+00:00", {"15": (250, 102)})
    fulfilled(conn, "rd", datetime(2026, 11, 5, 19, tzinfo=utc), ("15", "COKE20", "primary", "5.00", 4, 0, 0))
    store_poll(conn, "p3", "2026-11-06T14:00:00+00:00", {"15": (250, 102)})
    s = by_slot(get_machine_state(conn, now=datetime(2026, 11, 6, 22, tzinfo=utc)).data)["15"]
    assert (s.product, s.stock_on_hand) == ("COKE20", 4)
    assert s.class_days_in_stock == 2


def test_the_slow_seller_clock_counts_in_stock_class_days_since_the_last_sale(conn):
    # Review round 1, item 4. Coke went in on Fri 30 Oct and sold through Thursday, 4 that day. A top-up
    # of Coke on Friday keeps the run (6 in-stock class days, read just after midnight on Tuesday: Mon 2 Nov is
    # out, since the unit Monday's poll measured from before the slot's first receipt can't be placed on Coke,
    # review round 2, item 5) and starts units_sold_since_load again (0). Neither is the slow-seller clock: since
    # its last sale it has been in stock and seen silent on Fri 6 and Mon 9 Nov only, so 2 class days.
    utc = timezone.utc
    # No poll between the load and Monday's, so that window is over the slot's first receipt.
    store_poll(conn, "p0", "2026-10-30T18:00:00+00:00", {"15": (250, 100)})
    fulfilled(conn, "r1", datetime(2026, 10, 30, 19, 30, tzinfo=utc), ("15", "COKE20", "primary", "12.00", 20, 0, 0))
    readings_of_15(conn, [(f"p{n + 1}", f"2026-11-{day:02d}T22:00:00+00:00", (250, units))
                      for n, (day, units) in enumerate(((2, 101), (3, 101), (4, 102), (5, 106), (6, 106), (9, 106)))]
                   + [("p7", "2026-11-10T05:30:00+00:00", (250, 106))])
    fulfilled(conn, "r2", datetime(2026, 11, 6, 19, 30, tzinfo=utc), ("15", "COKE20", "primary", "6.00", 6, 0, 0))
    s = by_slot(get_machine_state(conn, now=datetime(2026, 11, 10, 5, 45, tzinfo=utc)).data)["15"]
    assert (s.stock_on_hand, s.class_days_in_stock, s.units_sold_since_load) == (20, 6, 0)
    assert s.class_days_since_sale == 2


# --- Review round 2 ------------------------------------------------------------------

def test_a_sale_polled_at_the_swaps_own_moment_can_still_empty_the_slot(conn):
    # Round 2, item 1. As round 1's item 1, but the poll after the swap is taken at the swap's own moment
    # (19:30): the replay takes its 1 unattributed unit from the 1 Pepsi, so only Mon 9 Nov is sure.
    s = swap(conn, "PEPSI20", [
        ("2026-10-30T19:30:00+00:00", (200, 107, 21400)),
        ("2026-11-02T14:00:00+00:00", (200, 107, 21400)),
        ("2026-11-04T14:00:00+00:00", (200, 107, 21400)),
        ("2026-11-06T14:00:00+00:00", (200, 107, 21400)),
        ("2026-11-09T14:00:00+00:00", (200, 107, 21400)),
    ], now=datetime(2026, 11, 9, 22, tzinfo=timezone.utc), units=1, then=lambda conn: fulfilled(
        conn, "r3", datetime(2026, 11, 6, 19, 30, tzinfo=timezone.utc), ("15", "PEPSI20", "primary", "5.00", 5, 0, 0)))
    assert (s.product, s.stock_on_hand, s.class_days_in_stock) == ("PEPSI20", 5, 1)


FILL_EVERY = timedelta(hours=3)


def readings_of_15(conn, reads):
    """Store a poll of slot 15 at each `(id, UTC time, reading)`, `reading` as `store_poll` takes it, and between
    each two a poll every `FILL_EVERY` carrying the earlier reading: the course staff's poller reads every hour,
    and the slow-seller clock treats a gap of over 6 hours as a break (Kevin, 25 Nov, #172)."""
    reads = sorted(reads, key=lambda r: r[1])
    for n, (poll_id, at, reading) in enumerate(reads):
        store_poll(conn, poll_id, at, {"15": reading})
        if n + 1 < len(reads):
            following, k = datetime.fromisoformat(at) + FILL_EVERY, 0
            while following < datetime.fromisoformat(reads[n + 1][1]):
                store_poll(conn, f"{poll_id}f{k}", following.isoformat(), {"15": reading})
                following, k = following + FILL_EVERY, k + 1


def five_coke(conn, days=(2, 3, 4, 5, 6), late=()):
    """Slot 15: 5 Coke in on Fri 30 Oct (3:30 PM Eastern), and a poll at 5 PM Eastern on each November day in
    `days` and at each UTC time in `late`, none showing a sale; between them, a poll every 3 hours."""
    utc = timezone.utc
    fulfilled(conn, "r1", datetime(2026, 10, 30, 19, 30, tzinfo=utc), ("15", "COKE20", "primary", "5.00", 5, 0, 0))
    readings_of_15(conn, [("p0", "2026-10-30T18:00:00+00:00", (250, 100)), ("p1", "2026-10-30T22:00:00+00:00", (250, 100))]
                   + [(f"d{day}", f"2026-11-{day:02d}T22:00:00+00:00", (250, 100)) for day in days]
                   + [(f"l{n}", at, (250, 100)) for n, at in enumerate(late)])


def lose_all_five_on_monday(conn):
    conn.execute("insert into purchase_line_losses (request_id, line_no, position, location, sku, units, recorded_at)"
                 " values ('r1', 0, 0, 'coil', 'COKE20', 5, %s)", (datetime(2026, 11, 2, 15, tzinfo=timezone.utc),))


def test_the_slow_seller_clock_stops_when_a_loss_empties_the_slot(conn):
    # Round 2, item 2. The 5 Coke are recorded lost on Monday: no sale, but nothing left to sell.
    five_coke(conn)
    lose_all_five_on_monday(conn)
    s = by_slot(get_machine_state(conn, now=datetime(2026, 11, 6, 23, tzinfo=timezone.utc)).data)["15"]
    assert (s.stock_on_hand, s.class_days_in_stock, s.class_days_since_sale) == (0, 1, 0)


def test_the_slow_seller_clock_starts_again_at_the_refill_after_a_loss(conn):
    # Round 2, item 2. 5 more go in on Thursday: the clock runs from then, Fri 6 and Mon 9 Nov, read just
    # after midnight on Tuesday, which hasn't been watched to its end (round 2, item 3).
    five_coke(conn, days=(2, 3, 4, 5, 6, 9), late=("2026-11-10T06:00:00+00:00",))
    lose_all_five_on_monday(conn)
    fulfilled(conn, "r2", datetime(2026, 11, 5, 19, tzinfo=timezone.utc), ("15", "COKE20", "primary", "5.00", 5, 0, 0))
    s = by_slot(get_machine_state(conn, now=datetime(2026, 11, 10, 6, 30, tzinfo=timezone.utc)).data)["15"]
    assert (s.stock_on_hand, s.class_days_since_sale) == (5, 2)


def test_a_newer_count_of_zero_stops_the_slow_seller_clock(conn):
    # Round 2, item 2. A person counts slot 15 empty on Tuesday, after the receipts' last movement.
    five_coke(conn)
    conn.execute("insert into slot_counts (count_id, counted_at, counted_by, source, device_id, selection, product,"
                 " units, exact) values ('c1', %s, 'kevin', 'door', 'vmc1', '15', 'COKE20', 0, true)",
                 (datetime(2026, 11, 3, 15, tzinfo=timezone.utc),))
    s = by_slot(get_machine_state(conn, now=datetime(2026, 11, 6, 23, tzinfo=timezone.utc)).data)["15"]
    assert (s.counted_on_hand, s.class_days_since_sale) == (0, 0)


def test_a_slot_the_polls_stopped_reading_is_not_silent(conn):
    # Round 2, item 3. After the load, every poll leaves slot 15 out: the machine isn't stale, but nothing
    # says slot 15 sold nothing, so its clock doesn't run.
    utc = timezone.utc
    store_poll(conn, "p0", "2026-10-30T18:00:00+00:00", {"15": (250, 100), "16": (300, 50)})
    fulfilled(conn, "r1", datetime(2026, 10, 30, 19, 30, tzinfo=utc), ("15", "COKE20", "primary", "5.00", 5, 0, 0))
    for day in (2, 3, 4, 5, 6):
        store_poll(conn, f"d{day}", f"2026-11-{day:02d}T22:00:00+00:00", {"16": (300, 50)})
    reply = get_machine_state(conn, now=datetime(2026, 11, 6, 23, tzinfo=utc))
    assert not reply.stale
    assert by_slot(reply.data)["15"].class_days_since_sale == 0


def test_a_class_day_is_silent_only_once_a_reading_after_its_end_shows_no_sale(conn):
    # Round 2, item 3. Read at 12:30 AM Eastern on Tuesday, after a poll at 12:10 AM: Tuesday held stock as it
    # began (7 in-stock class days for the rate), but nothing has watched it yet, so the clock is 6.
    five_coke(conn, days=(2, 3, 4, 5, 6, 9), late=("2026-11-10T05:10:00+00:00",))
    s = by_slot(get_machine_state(conn, now=datetime(2026, 11, 10, 5, 30, tzinfo=timezone.utc)).data)["15"]
    assert (s.class_days_in_stock, s.class_days_since_sale) == (7, 6)


# --- Review round 3: one stock history per slot, from the replay ---------------------------------------------

UTC = timezone.utc


def coke(conn, reads):
    """Slot 15: 5 Coke in on Fri 30 Oct (3:30 PM Eastern), between polls at 2 PM and 6 PM reading its units counter
    at 100, then a poll at each `(UTC time, lifetime units)` in `reads`, or `(UTC time, units, takings cents)`
    (by default the takings are the units at $2.50); between them, a poll every 3 hours (`readings_of_15`)."""
    fulfilled(conn, "r1", datetime(2026, 10, 30, 19, 30, tzinfo=UTC), ("15", "COKE20", "primary", "5.00", 5, 0, 0))
    readings_of_15(conn, [("p0", "2026-10-30T18:00:00+00:00", (250, 100)), ("p1", "2026-10-30T22:00:00+00:00", (250, 100))]
                   + [(f"q{n}", at, (250, *reading)) for n, (at, *reading) in enumerate(reads)])


def evening(day, units=100, *takings):
    """A reading at 5 PM Eastern on that November day, with its takings counter if given."""
    return (f"2026-11-{day:02d}T22:00:00+00:00", units, *takings)


def lose(conn, units, at, position=0):
    """`units` Coke recorded lost from slot 15 at `at`, against r1's line."""
    conn.execute("insert into purchase_line_losses (request_id, line_no, position, location, sku, units, recorded_at)"
                 " values ('r1', 0, %s, 'coil', 'COKE20', %s, %s)", (position, units, at))


def slot_15(conn, now):
    return by_slot(get_machine_state(conn, now=now).data)["15"]


def test_an_unsettled_counter_drop_proves_no_silence(conn):
    # Round 3, case (a). Slot 15 reads 100 every evening to Thursday; a good poll early on Saturday reads 5, a drop
    # the next poll hasn't settled (a misread or a restart). Not 5. Round 3 had the clock stay at 3 (the days the
    # agreeing readings watched); the redesign's review round 2, item 1, says a drop at the slot's latest reading
    # is evidence of a possible sale (a restart loses what sold before it), so the clock is 0 until a poll settles it.
    coke(conn, [evening(d) for d in (2, 3, 4, 5)] + [("2026-11-07T06:00:00+00:00", 5)])
    assert slot_15(conn, datetime(2026, 11, 7, 7, tzinfo=UTC)).class_days_since_sale == 0


def test_an_empty_count_before_a_refill_is_a_break(conn):
    # Round 3, case (b). A door count finds slot 15 empty on Tue 3 Nov (the 5 Coke went with no sale or recorded
    # loss), and 5 more go in on Thursday. The clock counts from the refill: Fri 6 and Mon 9, read just after
    # midnight on Tuesday, so 2, not 6. The rate's in-stock days break there too: Mon 2 and Tue 3, then Fri 6 to
    # Tue 10. Slot 16, known only from a count, has no clock.
    five_coke(conn, days=(2, 3, 4, 5, 6, 9), late=("2026-11-10T06:00:00+00:00",))
    conn.execute("insert into slot_counts (count_id, counted_at, counted_by, source, device_id, selection, product,"
                 " units, exact) values ('c1', %s, 'kevin', 'door', 'vmc1', '15', null, 0, true),"
                 " ('c1', %s, 'kevin', 'door', 'vmc1', '16', 'PEPSI20', 4, true)",
                 (datetime(2026, 11, 3, 15, tzinfo=UTC),) * 2)
    fulfilled(conn, "r2", datetime(2026, 11, 5, 19, tzinfo=UTC), ("15", "COKE20", "primary", "5.00", 5, 0, 0))
    states = by_slot(get_machine_state(conn, now=datetime(2026, 11, 10, 6, 30, tzinfo=UTC)).data)
    s = states["15"]
    assert (s.stock_on_hand, s.class_days_in_stock, s.class_days_since_sale) == (10, 5, 2)
    assert (states["16"].counted_on_hand, states["16"].class_days_since_sale) == (4, None)


def test_a_loss_recorded_after_now_doesnt_change_the_rate(conn):
    # Round 3, case (c). Read on Tue 10 Nov at 6 PM Eastern; the 5 Coke are recorded lost on Fri 13 Nov, after
    # that. As of Tuesday the slot held stock from Mon 2 to Tue 10: 7 in-stock class days, not 10. The clock has
    # watched 6 of them to their end.
    five_coke(conn, days=(2, 3, 4, 5, 6, 9, 10))
    lose(conn, 5, datetime(2026, 11, 13, 15, tzinfo=UTC))
    s = slot_15(conn, datetime(2026, 11, 10, 23, tzinfo=UTC))
    assert (s.stock_on_hand, s.class_days_in_stock, s.class_days_since_sale) == (5, 7, 6)


def test_a_pull_of_another_sku_that_the_replay_ignores_leaves_the_stock(conn):
    # Round 3, case (d). On Wednesday a receipt line pulls 5 PEPSI20 from slot 15, which holds 5 Coke: the replay
    # warns and leaves slot 15's count alone, so neither the rate nor the clock stops there. The clock is 6, not 0.
    five_coke(conn, days=(2, 3, 4, 5, 6, 9), late=("2026-11-10T06:00:00+00:00",))
    fulfilled(conn, "r2", datetime(2026, 11, 4, 19, tzinfo=UTC), ("15", None, "unavailable", None, 0, 0, 0))
    conn.execute("update purchase_lines set units_pulled = 5, pulled_sku = 'PEPSI20' where request_id = 'r2'")
    s = slot_15(conn, datetime(2026, 11, 10, 6, 30, tzinfo=UTC))
    assert (s.stock_on_hand, s.class_days_in_stock, s.class_days_since_sale) == (5, 7, 6)


def test_an_oversized_later_loss_keeps_the_runs_first_loss(conn):
    # Round 3, case (e). 4 of the 5 Coke are recorded lost at the load's own moment, and the 1 left sells on
    # Monday, so the slot is empty from then. On Friday 10 more are recorded lost, though none are there. The
    # slot held stock on Mon 2 only: 1 in-stock class day, not 5.
    coke(conn, [evening(d, 101) for d in (2, 3, 4, 5, 6)])
    lose(conn, 4, datetime(2026, 10, 30, 19, 30, tzinfo=UTC))
    lose(conn, 10, datetime(2026, 11, 6, 15, tzinfo=UTC), position=1)
    s = slot_15(conn, datetime(2026, 11, 6, 23, tzinfo=UTC))
    assert (s.stock_on_hand, s.class_days_in_stock) == (0, 1)


def test_a_loss_at_the_moment_asked_about_leaves_the_clock_at_zero(conn):
    # Round 3, case (f). The 5 Coke are recorded lost at the very moment the state is read: the slot is empty.
    five_coke(conn, days=(2, 3, 4, 5, 6, 9), late=("2026-11-10T06:00:00+00:00",))
    now = datetime(2026, 11, 10, 6, 30, tzinfo=UTC)
    lose(conn, 5, now)
    s = slot_15(conn, now)
    assert (s.stock_on_hand, s.class_days_since_sale) == (0, 0)


def test_the_first_reading_of_a_slot_proves_nothing_before_it(conn):
    # Round 3, case (g). Slot 15 is loaded on Fri 30 Oct, but no poll reads its counter until Thu 5 Nov at 5 PM
    # Eastern. Only Fri 6 and Mon 9 are watched end to end: 2, not 6.
    store_poll(conn, "p0", "2026-10-30T18:00:00+00:00", {"16": (300, 50)})
    fulfilled(conn, "r1", datetime(2026, 10, 30, 19, 30, tzinfo=UTC), ("15", "COKE20", "primary", "5.00", 5, 0, 0))
    readings_of_15(conn, [(f"q{n}", at, (250, 100)) for n, at in enumerate(
        (evening(5)[0], evening(6)[0], evening(9)[0], "2026-11-10T06:00:00+00:00"))])
    assert slot_15(conn, datetime(2026, 11, 10, 6, 30, tzinfo=UTC)).class_days_since_sale == 2


def test_a_counter_restart_proves_nothing_before_the_reading_after_it(conn):
    # Round 3, case (g). Slot 15's counter reads 100 to Tuesday, then 3 from Wednesday on: a restart the next poll
    # confirms. What sold between Tuesday's and Wednesday's polls went with the old count, so a sale there can't be
    # ruled out, and the clock counts only what was watched after it: Thu 5, Fri 6 and Mon 9, so 3, not 6.
    coke(conn, [evening(2), evening(3)] + [evening(d, 3) for d in (4, 5, 6, 9)] + [("2026-11-10T06:00:00+00:00", 3)])
    assert slot_15(conn, datetime(2026, 11, 10, 6, 30, tzinfo=UTC)).class_days_since_sale == 3


# --- Redesign, review round 2 ------------------------------------------------------------------------------

LATE = "2026-11-10T06:00:00+00:00"            # 1 AM Eastern on Tuesday: Monday has been watched to its end
TUESDAY = datetime(2026, 11, 10, 6, 30, tzinfo=UTC)


def count_15(conn, at, product, units, source="door", count_id="c1", slot="15"):
    """A person's count of `slot`: `units` of `product` (None: seen empty). A door count is exact; a photo isn't."""
    conn.execute("insert into slot_counts (count_id, counted_at, counted_by, source, device_id, selection, product,"
                 " units, exact) values (%s, %s, 'kevin', %s, 'vmc1', %s, %s, %s, %s)",
                 (count_id, at, source, slot, product, units, source == "door"))


def test_the_clock_needs_a_fresh_reading_of_the_slot_itself(conn):
    # Item 1. Slot 15 is watched to Monday evening (5 silent class days), then the polls read only slot 16 for three
    # days. The machine isn't stale, but nothing has read slot 15 since Monday: what it sold since is unknown.
    five_coke(conn, days=(2, 3, 4, 5, 6, 9))
    for day in (10, 11, 12):
        store_poll(conn, f"o{day}", f"2026-11-{day:02d}T22:00:00+00:00", {"16": (300, 50)})
    reply = get_machine_state(conn, now=datetime(2026, 11, 12, 23, tzinfo=UTC))
    assert not reply.stale
    assert by_slot(reply.data)["15"].class_days_since_sale == 0


def test_a_takings_rise_with_no_unit_restarts_the_clock(conn):
    # Item 1. Slot 15's units stay at 100, but its takings rise $2.50 between the Tuesday and Wednesday polls: a
    # sale the units counter may have missed. Only Thu 5, Fri 6 and Mon 9 are silent on both counters: 3, not 6.
    coke(conn, [evening(2, 100, 25000), evening(3, 100, 25000)] + [evening(d, 100, 25250) for d in (4, 5, 6, 9)]
         + [(LATE, 100, 25250)])
    assert slot_15(conn, TUESDAY).class_days_since_sale == 3


def test_a_slot_that_comes_back_to_our_assignment_starts_again(conn):
    # Item 2. Slot 15 holds 5 Coke from Fri 8 Jan. A person records an assignment without it on Mon 11 Jan and
    # puts it back on Tue 19 Jan. Its polls from in between, and from before, aren't evidence of ours: demand and
    # the clock count only from 19 Jan. Wed 20 Jan began after it, in stock, but hasn't been watched to its end.
    # A sale on Thu 14 Jan, while it wasn't ours, is in the stock (the accounting replay) but not in
    # units_sold_since_load.
    from vending import slots
    from tests.conftest import TEST_SLOTS

    def assign(selections, at, **kwargs):
        slots.record(conn, {"team": slots.DEFAULT_TEAM, "device_id": "vmc1", "slots": list(selections)}, now=at,
                     **kwargs)

    store_poll(conn, "p0", "2027-01-08T18:00:00+00:00", {"15": (250, 100)})
    fulfilled(conn, "r1", datetime(2027, 1, 8, 19, 30, tzinfo=UTC), ("15", "COKE20", "primary", "5.00", 5, 0, 0))
    for day in (8, 11, 12, 13, 14, 15, 18, 19):
        store_poll(conn, f"j{day}", f"2027-01-{day:02d}T22:00:00+00:00", {"15": (250, 100 if day < 14 else 101)})
    store_poll(conn, "j20", "2027-01-20T06:00:00+00:00", {"15": (250, 101)})
    assign([s for s in TEST_SLOTS if s != "15"] + ["11"], datetime(2027, 1, 11, 15, tzinfo=UTC))
    assign(TEST_SLOTS, datetime(2027, 1, 19, 15, tzinfo=UTC), replace=True)
    s = slot_15(conn, datetime(2027, 1, 20, 6, 30, tzinfo=UTC))
    assert (s.stock_on_hand, s.units_sold_since_load, s.class_days_in_stock, s.class_days_since_sale) == (4, 0, 1, 0)


def test_an_exact_count_of_another_product_breaks_the_run(conn):
    # Item 3. After the Coke load, a door count on Wednesday finds 5 Pepsi in slot 15. Whatever the receipts say,
    # Coke wasn't surely there from then: in stock Mon 2 to Wed 4 only, and no clock. PEPSI20 is in the approved
    # catalog, so the count names a SKU we know (a name that matches none says nothing: Kevin, 25 Nov).
    five_coke(conn, days=(2, 3, 4, 5, 6, 9), late=(LATE,))
    conn.execute("insert into catalog_products (sku, name, last_response) values ('PEPSI20', 'Pepsi 20 oz', '{}')")
    count_15(conn, datetime(2026, 11, 4, 15, tzinfo=UTC), "PEPSI20", 5)
    s = slot_15(conn, TUESDAY)
    assert (s.stock_on_hand, s.class_days_in_stock, s.class_days_since_sale) == (5, 3, 0)


def test_an_empty_count_at_the_loads_own_moment_is_no_proof_of_stock(conn):
    # Item 3. A door count timestamped at the very moment of the load found slot 15 empty. Which came first can't
    # be told, so the load doesn't prove the slot held stock after it: no in-stock days, no clock.
    five_coke(conn, days=(2, 3, 4, 5, 6, 9), late=(LATE,))
    count_15(conn, datetime(2026, 10, 30, 19, 30, tzinfo=UTC), None, 0)
    s = slot_15(conn, TUESDAY)
    assert (s.class_days_in_stock, s.class_days_since_sale) == (0, 0)


def test_a_count_inside_a_sale_window_proves_no_stock_after_it(conn):
    # Item 4, reversed by the PR #256 review, item 5. 5 Coke in; a door count of 2 on Monday afternoon falls inside
    # the window the Monday evening poll books 3 sales in (the poll before it is at 2 PM Eastern, `FILL_EVERY`).
    # The receipts say 2 are left, but those sales may have come after the count, so nothing proves stock after
    # the poll: in stock Monday only, and no clock.
    coke(conn, [evening(d, 103) for d in (2, 3, 4, 5, 6, 9, 10)] + [("2026-11-11T06:00:00+00:00", 103)])
    count_15(conn, datetime(2026, 11, 2, 20, tzinfo=UTC), "COKE20", 2)
    s = slot_15(conn, datetime(2026, 11, 11, 6, 30, tzinfo=UTC))
    assert (s.stock_on_hand, s.class_days_in_stock, s.class_days_since_sale) == (2, 1, 0)


def test_a_photo_count_is_no_cap_on_the_stock(conn):
    # Item 4. A photo on Monday shows 1 Coke at the front of a coil the receipts say holds 5; one sells on Tuesday.
    # A photo's units are an estimate, so the slot still surely holds stock: silent Wed 4 to Mon 9.
    coke(conn, [evening(2), evening(3, 101)] + [evening(d, 101) for d in (4, 5, 6, 9)] + [(LATE, 101)])
    count_15(conn, datetime(2026, 11, 2, 15, tzinfo=UTC), "COKE20", 1, source="photo")
    assert slot_15(conn, TUESDAY).class_days_since_sale == 4


def test_an_exact_count_of_the_product_starts_a_new_stretch(conn):
    # Item 4. The swap puts in 1 Pepsi and Monday's poll books 1 unit the counters can't place, so the receipts
    # leave 0 for sure. A door count on Tuesday finds 1 Pepsi: the slot holds stock from then, silent Wed 4 to Mon 9.
    def door_count(conn):
        count_15(conn, datetime(2026, 11, 3, 15, tzinfo=UTC), "PEPSI20", 1)

    s = swap(conn, "PEPSI20", [("2026-11-02T14:00:00+00:00", (200, 107, 21400))]
             + [(f"2026-11-{d:02d}T22:00:00+00:00", (200, 107, 21400)) for d in (3, 4, 5, 6, 9)]
             + [(LATE, (200, 107, 21400))], now=TUESDAY, units=1, then=door_count, fill=True)
    assert (s.stock_on_hand, s.counted_on_hand, s.class_days_since_sale) == (0, 1, 4)


def test_a_sale_in_the_window_over_a_slots_first_receipt_is_not_the_new_products(conn):
    # Item 5. Slots 15 and 16 get their first receipts on Fri 30 Oct: $2.50 drinks where $2.00 ones stood. In the
    # window over the load, 15's counters took $2.00 for its unit, the old drink's price, so nothing places it on
    # the new drink. 16's took $2.50, the new price: the counters place that one on the new drink.
    store_poll(conn, "p0", "2026-10-30T18:00:00+00:00", {"15": (200, 100, 20000), "16": (200, 50, 10000)})
    fulfilled(conn, "r1", datetime(2026, 10, 30, 19, 30, tzinfo=UTC), ("15", "COKE20", "primary", "5.00", 5, 0, 0),
              ("16", "PEPSI20", "primary", "5.00", 5, 0, 0))
    for n, at in enumerate(["2026-10-30T22:00:00+00:00"] + [evening(d)[0] for d in (2, 3, 4, 5, 6, 9)] + [LATE]):
        store_poll(conn, f"q{n}", at, {"15": (250, 101, 20200), "16": (250, 51, 10250)})
    states = by_slot(get_machine_state(conn, now=TUESDAY).data)
    assert [(states[s].stock_on_hand, states[s].units_sold_since_load, states[s].rate_basis) for s in ("15", "16")] == [
        (4, 0, "own: 0 sold over 7 in-stock class days"), (4, 1, "own: 1 sold over 7 in-stock class days")]


def test_a_sale_settled_at_the_swaps_own_moment_counts_since_the_load(conn):
    # Item 5. A poll lands at the swap's own moment (no door times): 2 units for $6.00, a Coke at $2.00 and a Red
    # Bull at $4.00. The replay books the Red Bull after its load at that moment, so it sold since the load.
    s = swap(conn, "REDBULL", [("2026-10-30T19:30:00+00:00", (400, 108, 21800)),
                               ("2026-11-02T22:00:00+00:00", (400, 108, 21800))],
             now=datetime(2026, 11, 2, 23, tzinfo=UTC))
    assert (s.product, s.stock_on_hand, s.units_sold_since_load) == ("REDBULL", 19, 1)


def test_a_poll_after_the_cutoff_doesnt_change_the_report_at_it(conn):
    # Item 6. Coke has sold nothing since Fri 30 Oct. On Mon 9 Nov the door opens at 19:00 UTC to swap it for Red
    # Bull and closes at 19:40. Asked at 19:20 with the door open, then asked again at 19:20 once a poll at 20:00
    # has settled one Coke sold before the swap: the same cutoff gives the same report, since that sale was booked
    # after it (`Stock.sales` at 19:20 doesn't have it either).
    five_coke(conn, days=(2, 3, 4, 5, 6), late=("2026-11-09T18:00:00+00:00",))
    fulfilled(conn, "r2", datetime(2026, 11, 9, 19, 45, tzinfo=UTC), ("15", "REDBULL", "primary", "20.00", 10, 0, 0))
    conn.execute("update purchase_lines set slot_reassigned = true where request_id = 'r2'")
    conn.execute("update purchase_requests set door_opened_at = %s, door_closed_at = %s where request_id = 'r2'",
                 (datetime(2026, 11, 9, 19, tzinfo=UTC), datetime(2026, 11, 9, 19, 40, tzinfo=UTC)))
    cutoff = datetime(2026, 11, 9, 19, 20, tzinfo=UTC)
    before = slot_15(conn, cutoff)
    store_poll(conn, "m9b", "2026-11-09T20:00:00+00:00", {"15": (400, 101, 25250)})
    after = slot_15(conn, cutoff)
    assert [s for s in machine_state.stock.stock(conn, until=cutoff).sales if s.slot == "15"] == []
    assert (before.product, before.rate_basis, before.class_days_since_sale) == (
        "COKE20", "own: 0 sold over 6 in-stock class days", 5)
    assert (after.product, after.rate_basis, after.class_days_since_sale) == (
        before.product, before.rate_basis, before.class_days_since_sale)


def test_a_day_watched_from_its_own_midnight_counts_for_the_clock(conn):
    # Item 7. 5 Coke go in at 00:00 Eastern on Monday 2 Nov, as a poll reads the slot; polls watch it to 00:00 on
    # Saturday. Monday to Friday were in stock and watched end to end: 5 in-stock class days, and a clock of 5.
    monday = datetime(2026, 11, 2, 5, tzinfo=UTC)
    fulfilled(conn, "r1", monday, ("15", "COKE20", "primary", "5.00", 5, 0, 0))
    readings_of_15(conn, [("p0", monday.isoformat(), (250, 100))] + [
        (f"q{n}", at, (250, 100)) for n, at in enumerate([evening(d)[0] for d in (2, 3, 4, 5, 6)]
                                                         + ["2026-11-07T05:00:00+00:00"])])
    s = slot_15(conn, datetime(2026, 11, 7, 5, 30, tzinfo=UTC))
    assert (s.class_days_in_stock, s.class_days_since_sale) == (5, 5)


def test_a_loss_at_midnight_leaves_that_day_out_of_stock(conn):
    # Item 7. All 5 Coke are recorded lost at 00:00 Eastern on Monday 2 Nov: the slot was empty as Monday began.
    five_coke(conn, days=(2,))
    lose(conn, 5, datetime(2026, 11, 2, 5, tzinfo=UTC))
    assert slot_15(conn, datetime(2026, 11, 2, 23, tzinfo=UTC)).class_days_in_stock == 0


# --- Kevin's decisions, 25 Nov (#172 item 3) ---------------------------------------------------------------
# Nobody counts slots or confirms a swap after go-live, so the clock must not advance on unclear evidence.

def hourly_coke(conn, until, skip=(), step=timedelta(hours=1), units=100):
    """Slot 15: 5 Coke in on Fri 30 Oct (3:30 PM Eastern), and a poll every `step` from 2 PM that day to `until`,
    reading its units counter at `units` (no sale), except in each `(begin, end)` of `skip`."""
    fulfilled(conn, "r1", datetime(2026, 10, 30, 19, 30, tzinfo=UTC), ("15", "COKE20", "primary", "5.00", 5, 0, 0))
    at, n = datetime(2026, 10, 30, 18, tzinfo=UTC), 0
    while at <= until:
        if not any(begin <= at < end for begin, end in skip):
            store_poll(conn, f"h{n}", at.isoformat(), {"15": (250, units)})
        at, n = at + step, n + 1


def test_hourly_readings_watch_every_class_day(conn):
    # The base case for the tests below: read every hour, Mon 2 to Mon 9 Nov are six silent class days.
    hourly_coke(conn, datetime(2026, 11, 10, 6, tzinfo=UTC))
    s = slot_15(conn, TUESDAY)
    assert (s.class_days_in_stock, s.class_days_since_sale) == (7, 6)


def test_a_gap_of_more_than_six_hours_between_readings_starts_the_clock_again(conn):
    # Decision 2. No reading of slot 15 from 8 AM to 3 PM Eastern on Wednesday 4 Nov: 7 hours. The counter is flat
    # on both sides, but a restart and a sale inside the gap can't be ruled out. Only Thu 5, Fri 6 and Mon 9 count.
    gap = (datetime(2026, 11, 4, 13, tzinfo=UTC), datetime(2026, 11, 4, 20, tzinfo=UTC))
    hourly_coke(conn, datetime(2026, 11, 10, 6, tzinfo=UTC), skip=(gap,))
    assert slot_15(conn, TUESDAY).class_days_since_sale == 3


def test_a_gap_of_six_hours_is_not_a_break(conn):
    # Decision 2. Readings every 6 hours, exactly: no gap of more than 6 hours, so all six days count.
    hourly_coke(conn, datetime(2026, 11, 10, 6, tzinfo=UTC), step=timedelta(hours=6))
    assert slot_15(conn, TUESDAY).class_days_since_sale == 6


def test_a_count_whose_name_matches_no_sku_says_nothing_about_the_product(conn):
    # Decision 1. A door count on Wednesday writes "Coca-Cola 20oz bottle", 5 units. That text is no SKU we know,
    # so it says nothing about which product is there: the run isn't broken (the clock still needs the counters).
    hourly_coke(conn, datetime(2026, 11, 10, 6, tzinfo=UTC))
    count_15(conn, datetime(2026, 11, 4, 15, tzinfo=UTC), "Coca-Cola 20oz bottle", 5)
    s = slot_15(conn, TUESDAY)
    assert (s.class_days_in_stock, s.class_days_since_sale) == (7, 6)


def test_a_count_with_no_known_sku_that_found_the_slot_empty_still_empties_it(conn):
    # Decision 1. Whatever its name, a count of 0 found the slot empty: no stock from then, so no clock.
    hourly_coke(conn, datetime(2026, 11, 10, 6, tzinfo=UTC))
    count_15(conn, datetime(2026, 11, 4, 15, tzinfo=UTC), "Coca-Cola 20oz bottle", 0)
    s = slot_15(conn, TUESDAY)
    assert (s.class_days_in_stock, s.class_days_since_sale) == (3, 0)


def test_the_day_one_fill_starts_the_clock_even_after_a_test_load_of_the_same_drink(conn):
    # A test purchase put 5 Coke in slot 15 on Fri 30 Oct. The ledger opens on Fri 6 Nov, and the first fill after it
    # (the day-one fill's place here) loads Coke again on Mon 9 Nov at 3 PM Eastern. The test load is left out of
    # the stock, so the run and its clock start at the fill: Tue 10 to Fri 13 are four silent class days, not ten.
    hourly_coke(conn, datetime(2026, 11, 14, 6, tzinfo=UTC), step=timedelta(hours=3))
    conn.execute("insert into ledger_entries (id, at, kind, amount_cents, detail, recorded_by)"
                 " values (1, %s, 'opening', 60000, 'opening', 'kevin')", (datetime(2026, 11, 6, 20, tzinfo=UTC),))
    fulfilled(conn, "fill", datetime(2026, 11, 9, 20, tzinfo=UTC), ("15", "COKE20", "primary", "5.00", 5, 0, 0))
    s = slot_15(conn, datetime(2026, 11, 14, 6, 30, tzinfo=UTC))
    assert (s.stock_on_hand, s.class_days_since_sale) == (5, 4)


# --- PR #256 review (GPT-6.2 Sol, 25 Nov) ------------------------------------------------------------------
# Each is a way unclear evidence let the clock reach the swap threshold, or a demand regression against main.

def at_utc(value):
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


def test_a_visit_with_no_receipt_lines_leaves_the_stock_unproven(conn):
    # Item 1. A request is fulfilled on Wednesday with no lines: what that visit loaded or pulled is unknown, so the
    # stretch of stock that runs through it proves nothing (the wildcard's silence check refuses it the same way).
    hourly_coke(conn, at_utc("2026-11-10T06:00"))
    fulfilled(conn, "no-lines", at_utc("2026-11-04T20:00"))
    assert slot_15(conn, TUESDAY).class_days_since_sale == 0


def test_a_blank_takings_field_is_no_flat_money_counter(conn):
    # Item 2. Checksum-good polls report slot 15's units but leave its lifetime takings blank, stored as 0. A
    # takings counter at 0 with units above 0 never read the money: nothing proves silence on it.
    from tests.test_evadts import build
    from vending.sync_history import store

    first = at_utc("2026-10-30T18:00")
    fulfilled(conn, "r1", at_utc("2026-10-30T19:30"), ("15", "COKE20", "primary", "5.00", 5, 0, 0))
    raw = build(["ID1*TEST-MACHINE*0000**EVS3.0*", "ID4*2*1840*5", "VA1*0*0*0*0", "PA1*15*250*", "PA2*100**0*0*0*0"])
    for i in range(89):
        at = first + timedelta(hours=3 * i)
        assert store(conn, {"session_id": f"blank-{i}", "device_id": "vmc1", "started_at": at.isoformat()}, raw) == "ok"
    assert slot_15(conn, TUESDAY).class_days_since_sale == 0


def test_a_lone_drop_between_two_equal_readings_breaks_the_count(conn):
    # Item 3. Slot 15 reads (1 unit, $2.50) hourly, but (0, $0) at 1 PM Eastern on Wednesday, then (1, $2.50) again:
    # a misread, or a reset followed by one sale. Unclear, so the count starts again after it: Thu, Fri, Mon.
    at = at_utc("2026-11-04T18:00")
    hourly_coke(conn, at_utc("2026-11-10T06:00"), units=1, skip=((at, at + timedelta(hours=1)),))
    readings_of_15(conn, [("lone-drop", at.isoformat(), (250, 0, 0))])
    assert slot_15(conn, TUESDAY).class_days_since_sale == 3


def test_a_count_at_the_moment_of_a_loss_of_everything_cant_revive_the_stock(conn):
    # Item 4. A count of 5 at the very moment all 5 are recorded lost: the count may have come first. The replay
    # says 0 on hand, and the clock mustn't take the count as stock after the loss.
    hourly_coke(conn, at_utc("2026-11-10T06:00"))
    at = at_utc("2026-11-02T20:00")
    lose(conn, 5, at)
    count_15(conn, at, "COKE20", 5)
    s = slot_15(conn, TUESDAY)
    assert (s.stock_on_hand, s.class_days_since_sale) == (0, 0)


def test_sales_in_the_window_holding_a_count_may_have_come_after_it(conn):
    # Item 5. 5 Coke in; a door count finds 2 inside the window that books 2 sales. Both may have sold after the
    # count, leaving 0 (the 3 missing at the count are unexplained). Not surely in stock: no clock.
    coke(conn, [evening(d, 102) for d in (2, 3, 4, 5, 6, 9)] + [(LATE, 102)])
    count_15(conn, at_utc("2026-11-02T20:00"), "COKE20", 2)
    assert slot_15(conn, TUESDAY).class_days_since_sale == 0


def test_the_six_hour_gap_is_elapsed_time_across_the_fall_back_hour(conn):
    # Item 6. No poll from 2 AM EDT to 7 AM EST on Sun 1 Nov: 6 wall-clock hours, 7 elapsed. With the session in
    # Eastern time the readings come back in that zone, and the gap must still be the elapsed 7 hours.
    first, end = at_utc("2026-10-29T18:00"), at_utc("2026-11-06T06:00")
    fulfilled(conn, "fill", first, ("15", "COKE20", "primary", "5.00", 5, 0, 0))
    at = first
    while at <= end:
        if not at_utc("2026-11-01T06:00") <= at < at_utc("2026-11-01T12:00"):
            store_poll(conn, f"p{int(at.timestamp())}", at.isoformat(), {"15": (250, 100)})
        at += timedelta(hours=1)
    assert slot_15(conn, end).class_days_since_sale == 4
    conn.execute("set time zone 'America/New_York'")
    assert slot_15(conn, end).class_days_since_sale == 4


def test_a_first_fill_sale_after_a_poll_inside_the_visit_is_the_new_products(conn):
    # Item 7. The door opens at 19:10 UTC, a poll reads slot 15 at 19:20, the door closes at 19:30 and the 20:00
    # poll shows one sale at the same price. Nothing sells with the door open, so the sale came after the load:
    # the new product's, as on main.
    close = at_utc("2026-10-30T19:30")
    hourly_coke(conn, close, skip=((at_utc("2026-10-30T18:00"), close),))
    conn.execute("update purchase_requests set door_opened_at = %s, door_closed_at = %s where request_id = 'r1'",
                 (at_utc("2026-10-30T19:10"), close))
    readings_of_15(conn, [("inside", at_utc("2026-10-30T19:20").isoformat(), (250, 100)),
                          ("after", at_utc("2026-10-30T20:00").isoformat(), (250, 101))])
    assert slot_15(conn, at_utc("2026-10-30T21:00")).units_sold_since_load == 1
