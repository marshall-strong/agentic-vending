"""Tests for `get_machine_state`, on synthetic polls and receipts."""

from datetime import datetime, timedelta, timezone
from fractions import Fraction

from tests.conftest import store_poll
from vending.slots import SLOTS
from vending.stock import ClosetCount, SlotCount
from vending.tools.machine_state import build, closet_items, get_machine_state, sold_in_window

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
    store_poll(conn, "other-device", "2026-10-30T12:00:00+00:00", {"15": (1, 1)}, device_id="vmc2")
    reply = get_machine_state(conn, now=NOW + timedelta(hours=1))
    assert reply.success and reply.count == 20 and not reply.stale
    assert [s.slot for s in reply.data] == list(SLOTS)  # slot 11 isn't ours
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

    def sold(*readings):
        polls = [("15", t0 + (n if n < 2 else n + 1) * h, v) for n, v in enumerate(readings)]  # no poll at the load
        replay = stock.weighted_average(stock.movements_from_receipts([line], []) + stock.sales_from_counters(polls))
        return _sold_since_load(replay.sales, {"15": t0 + 2 * h})["15"]

    assert sold(1000, 5, 1010) == 10      # a misread just before the load is not 1005 sold
    assert sold(1000, 1000, 3, 8) == 5    # a board swap after the load still counts
    assert sold(1000, 1002, 1010) == 8


def test_a_misread_poll_moves_no_sales(conn):
    store_poll(conn, "p0", "2026-10-22T12:00:00+00:00", {"15": (250, 1000)})
    store_poll(conn, "p1", "2026-10-24T12:00:00+00:00", {"15": (250, 5)})      # misread, just before the load
    fulfilled(conn, "r1", datetime(2026, 10, 24, 15, tzinfo=timezone.utc), ("15", "COKE20", "primary", "12.00", 20, 0, 0))
    store_poll(conn, "p2", "2026-10-26T12:00:00+00:00", {"15": (250, 1010)})
    store_poll(conn, "p3", "2026-10-29T12:00:00+00:00", {"15": (250, 1014)})
    s = by_slot(get_machine_state(conn, now=NOW).data)["15"]
    assert (s.units_sold_in_window, s.units_per_day) == (14, "2.00")
    assert (s.stock_on_hand, s.units_sold_since_load) == (6, 14)   # not 1009 sold since the load
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
    assert "A person's count (2026-10-27, from a photo" in reply.message
    assert "Slots it saw holding stock: 47." in reply.message


def test_no_count_table_is_no_count(conn):
    store_poll(conn, "a", "2026-10-29T12:00:00+00:00", {"15": (250, 100)})
    conn.execute("drop table slot_counts")
    reply = get_machine_state(conn, now=NOW)
    assert reply.success and all(s.counted_at is None for s in reply.data) and "person's count" not in reply.message
