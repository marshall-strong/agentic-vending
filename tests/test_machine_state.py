"""Tests for `get_machine_state`, on synthetic polls."""

from datetime import datetime, timedelta, timezone

from tests.conftest import store_poll
from vending.slots import SLOTS
from vending.tools.machine_state import build, get_machine_state

NOW = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)


def by_slot(states):
    return {s.slot: s for s in states}


def test_build_sales_over_the_window():
    states = by_slot(build(NOW, {"15": (250, 110)}, NOW - timedelta(days=7), {"15": (250, 100)}))
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


def test_build_ignores_a_counter_that_went_down():
    states = by_slot(build(NOW, {"15": (250, 3)}, NOW - timedelta(days=7), {"15": (250, 100)}))
    assert states["15"].units_sold_in_window is None


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
