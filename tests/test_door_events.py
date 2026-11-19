"""analysis/door_events.py: the door-open counter in a raw poll, and the rises export_sales.py writes to door_events.csv."""

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "analysis"))

from door_events import door_counter, door_rows  # noqa: E402

T0 = datetime.fromisoformat("2026-10-05T07:00:00-04:00")


def readings(*lifetimes):
    """(at, since_reset, lifetime) on an hourly grid; since_reset is just a marker here."""
    return [(T0 + timedelta(hours=i), i, life) for i, life in enumerate(lifetimes)]


def rise_column(rows):
    return [rise for _, _, _, rise in rows]


def test_a_lone_low_reading_is_a_misread_and_rises_by_nothing():
    assert rise_column(door_rows(readings(1000, 5, 1000))) == [0, 0, 0]


def test_after_a_misread_the_rise_is_measured_from_the_old_high():
    assert rise_column(door_rows(readings(1000, 5, 1003))) == [0, 0, 3]


def test_two_low_readings_in_a_row_are_a_restart():
    assert rise_column(door_rows(readings(1000, 5, 7))) == [0, 0, 2]


def test_every_reading_gets_a_row_in_time_order_with_its_counts():
    rows = door_rows(list(reversed(readings(10, 12, 12, 13))))
    assert [at for at, _, _, _ in rows] == [T0 + timedelta(hours=i) for i in range(4)]
    assert [(life, since) for _, life, since, _ in rows] == [(10, 0), (12, 1), (12, 2), (13, 3)]
    assert rise_column(rows) == [0, 2, 0, 1]


def test_the_door_counter_is_read_from_the_ea2_do_record():
    raw = b"DXS*X*VA*V1/1\r\nEA2*DO*4*1234\r\nEA2*CR*1*2\r\nDXE*1*1\r\n"
    assert door_counter(raw) == (4, 1234)
    assert door_counter(b"DXS*X\r\nEA2*CR*1*2\r\n") is None
    assert door_counter(b"EA2*DO*x*1\r\n") is None


# --- is_our_rise and read_receipts: which rises are ours from the start (3 Dec) ---

def test_before_the_start_every_rise_is_ours():
    from door_events import is_our_rise
    assert is_our_rise(T0, T0 + timedelta(hours=1), [])


def test_from_the_start_only_a_rise_with_a_receipt_is_ours(tmp_path):
    from door_events import is_our_rise, read_receipts
    from slots_config import START
    prev, at = START + timedelta(hours=10), START + timedelta(hours=11)
    assert not is_our_rise(prev, at, [])
    assert is_our_rise(prev, at, [(at - timedelta(minutes=20), at + timedelta(minutes=10))])
    # a receipt that ended before the poll before, or began more than a visit's length after the rise: not this rise
    assert not is_our_rise(prev, at, [(prev - timedelta(hours=2), prev - timedelta(hours=1))])
    assert not is_our_rise(prev, at, [(at + timedelta(hours=2), at + timedelta(hours=3))])
    f = tmp_path / "receipts.csv"
    f.write_text("request_id,first_at_eastern,fulfilled_at_eastern\nr1,2026-12-04T15:00:00-05:00,2026-12-04T15:20:00-05:00\n")
    assert read_receipts(f) == [(datetime.fromisoformat("2026-12-04T15:00:00-05:00"), datetime.fromisoformat("2026-12-04T15:20:00-05:00"))]
    assert read_receipts(tmp_path / "none.csv") == []
