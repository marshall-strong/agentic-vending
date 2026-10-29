"""Tests for `get_sales_history`, on synthetic polls."""

from datetime import datetime, timezone

from tests.conftest import store_poll
from vending.tools.sales_history import build, get_sales_history, tax_cents


def at(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 10, day, hour, tzinfo=timezone.utc)


def test_tax_inside_tax_inclusive_takings():
    assert tax_cents(108) == 8
    assert tax_cents(13500) == 1000
    assert tax_cents(300) == 22   # 22.22 rounds down
    assert tax_cents(10) == 1     # 0.74 rounds up
    assert tax_cents(0) == 0


def test_build_differences_counters():
    rows, total = build({"15": (100, 25000), "16": (5, 1500)},
                        {"15": (110, 27700), "16": (3, 900), "17": (1, 100)},
                        ["15", "16", "17"])
    by = {r.slot: r for r in rows}
    assert (by["15"].units, by["15"].revenue, by["15"].revenue_ex_tax) == (10, "27.00", "25.00")
    assert by["16"].units is None   # counter went down: unknown, not negative
    assert by["17"].units is None   # not in the first poll
    assert total == 2700


def test_window_spans_the_polls_around_it(conn):
    store_poll(conn, "a", "2026-10-20T12:00:00+00:00", {"15": (250, 100), "16": (300, 10)})
    store_poll(conn, "b", "2026-10-22T12:00:00+00:00", {"15": (250, 104), "16": (300, 12)})
    store_poll(conn, "c", "2026-10-27T12:00:00+00:00", {"15": (250, 110), "16": (300, 12)})
    store_poll(conn, "x", "2026-10-25T12:00:00+00:00", {"15": (1, 999)}, device_id="vmc2")
    reply = get_sales_history(conn, at(21), at(28), None)
    assert reply.success and reply.count == 20
    assert (reply.start, reply.end) == (at(20), at(27))  # last poll at or before each end
    by = {r.slot: r for r in reply.data}
    assert (by["15"].units, by["15"].revenue) == (10, "25.00")
    assert (by["16"].units, by["16"].revenue) == (2, "6.00")
    assert reply.revenue == "31.00" and reply.revenue_ex_tax == "28.70"  # tax 3100 x 8/108 = 229.6 -> 230


def test_window_before_the_first_poll_starts_at_it(conn):
    store_poll(conn, "a", "2026-10-20T12:00:00+00:00", {"15": (250, 100)})
    store_poll(conn, "b", "2026-10-22T12:00:00+00:00", {"15": (250, 104)})
    reply = get_sales_history(conn, at(1), at(28), ["15"])
    assert reply.success and reply.count == 1 and reply.data[0].units == 4
    assert "starts at the first one" in reply.message


def test_refusals_say_why(conn):
    assert "Not our slots: 11" in get_sales_history(conn, at(1), at(2), ["15", "11"]).message
    assert "before end" in get_sales_history(conn, at(2), at(1), None).message
    store_poll(conn, "a", "2026-10-20T12:00:00+00:00", {"15": (250, 100)})
    assert "Fewer than two" in get_sales_history(conn, at(1), at(28), None).message


def test_naive_times_are_utc(conn):
    store_poll(conn, "a", "2026-10-20T12:00:00+00:00", {"15": (250, 100)})
    store_poll(conn, "b", "2026-10-22T12:00:00+00:00", {"15": (250, 104)})
    assert get_sales_history(conn, datetime(2026, 10, 20, 12), datetime(2026, 10, 22, 12), ["15"]).data[0].units == 4
