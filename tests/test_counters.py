"""Tests for reading rises out of a lifetime counter, including a reading that goes down."""

from datetime import datetime, timedelta, timezone

from vending.counters import rises, windows

T0 = datetime(2026, 11, 7, 15, 0, tzinfo=timezone.utc)


def h(n):
    return T0 + timedelta(hours=n)


def test_rises_are_the_differences_between_polls_in_time_order():
    assert rises([(h(2), 12), (h(0), 10), (h(1), 10), (h(3), 15)]) == [(h(2), 2), (h(3), 3)]


def test_one_low_reading_between_good_polls_is_a_misread_not_sales():
    # Kevin's case: 100000 -> 500 -> 100250 is 250 sold, not 99750.
    assert rises([(h(0), 100000), (h(1), 500), (h(2), 100250)]) == [(h(2), 250)]


def test_two_low_readings_in_a_row_are_a_counter_that_restarted():
    # A board swap: the counter starts again at 200 and sales are measured from there.
    assert rises([(h(0), 1000), (h(1), 200), (h(2), 450), (h(3), 460)]) == [(h(2), 250), (h(3), 10)]


def test_a_counter_that_keeps_falling_counts_nothing_until_it_rises():
    assert rises([(h(0), 1000), (h(1), 200), (h(2), 150), (h(3), 300)]) == [(h(3), 150)]


def test_a_lone_drop_at_the_end_counts_nothing_yet():
    assert rises([(h(0), 1000), (h(1), 1010), (h(2), 3)]) == [(h(1), 10)]


def test_fewer_than_two_readings_is_no_sales():
    assert rises([]) == [] and rises([(h(0), 5)]) == []


def test_windows_start_at_the_reading_each_rise_is_measured_from():
    # A misread is skipped, so that rise's window starts at the good poll before it;
    # after a restart, the window starts at the first low reading.
    assert windows([(h(0), 100000), (h(1), 500), (h(2), 100250), (h(3), 100260)]) == [
        (h(0), h(2), 250), (h(2), h(3), 10)]
    assert windows([(h(0), 1000), (h(1), 200), (h(2), 450)]) == [(h(1), h(2), 250)]
