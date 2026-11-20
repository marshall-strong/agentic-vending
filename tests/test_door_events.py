"""analysis/door_events.py: the door-open counter in a raw poll, and the rises export_sales.py writes to door_events.csv."""

import sys
from datetime import datetime, timedelta, timezone
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


# --- our_windows and read_receipts: which rises are ours from the start (3 Dec) ---

def _hourly(counts, t0):
    return [(t0 + timedelta(hours=h), c) for h, c in enumerate(counts)]


def test_before_the_start_every_rise_is_ours_and_starts_at_the_poll_before():
    from door_events import our_windows
    assert our_windows(_hourly([5, 6, 6, 8], T0), []) == [(T0, T0 + timedelta(hours=1), 1),
                                                          (T0 + timedelta(hours=2), T0 + timedelta(hours=3), 2)]


def test_from_the_start_only_a_rise_with_a_receipt_is_ours(tmp_path):
    from door_events import our_windows, read_receipts
    from slots_config import START
    t0 = START + timedelta(hours=10)
    readings = _hourly([5, 6, 6, 8], t0)                      # rises seen at +1 h and +3 h
    assert our_windows(readings, []) == []
    one = (t0 + timedelta(hours=2, minutes=15), t0 + timedelta(hours=2, minutes=30))      # inside the +3 h window
    assert our_windows(readings, [one]) == [(t0 + timedelta(hours=2), t0 + timedelta(hours=3), 2)]
    # fulfilled after the rise was seen: the bounded lookback finds it
    late = (t0 + timedelta(minutes=45), t0 + timedelta(hours=1, minutes=10))
    assert our_windows(readings, [late]) == [(t0, t0 + timedelta(hours=1), 1)]
    # a receipt that ended before the window opened, or began long after the rise: not this rise
    assert our_windows(readings, [(t0 - timedelta(hours=3), t0 - timedelta(hours=2))]) == []
    assert our_windows(readings, [(t0 + timedelta(hours=7), t0 + timedelta(hours=8))]) == []
    f = tmp_path / "receipts.csv"
    f.write_text("request_id,first_at_eastern,fulfilled_at_eastern\nr1,2026-12-04T15:00:00-05:00,2026-12-04T15:20:00-05:00\n")
    assert read_receipts(f) == [(datetime.fromisoformat("2026-12-04T15:00:00-05:00"), datetime.fromisoformat("2026-12-04T15:20:00-05:00"))]
    assert read_receipts(tmp_path / "none.csv") == []


# --- a receipt's candidate rises: the windows overlapping [fulfilled - 1 h, first line resolved] ---

def _from_start(counts):
    from slots_config import START
    t0 = START + timedelta(hours=10)                           # 3 Dec 10:00 Eastern
    return t0, _hourly(counts, t0)


def test_a_rise_seen_after_the_first_line_was_resolved_is_ruled_out():
    from door_events import our_visits
    t0, readings = _from_start([5, 5, 5, 6])                   # one rise, seen at +3 h (window +2 h to +3 h)
    # first line at +1:50, fulfilled +2:10: the door opened by +1:50, before that window began
    windows, unplaced = our_visits(readings, [(t0 + timedelta(hours=1, minutes=50), t0 + timedelta(hours=2, minutes=10))])
    assert windows == [] and unplaced == [(t0 + timedelta(hours=2, minutes=10), "unknown", ())]


def test_a_receipt_whose_first_line_came_over_an_hour_before_its_fulfilment_claims_no_rise():
    from door_events import our_visits
    t0, readings = _from_start([5, 6, 6, 6])                   # one rise, seen at +1 h (window 0 to +1 h)
    # first line at +0:06, fulfilled +1:30: the opening was by +0:06 and within an hour of +1:30, which can't both hold
    windows, unplaced = our_visits(readings, [(t0 + timedelta(minutes=6), t0 + timedelta(hours=1, minutes=30))])
    assert windows == [] and unplaced == [(t0 + timedelta(hours=1, minutes=30), "unknown", ())]


def test_one_candidate_is_claimed_and_two_are_an_ambiguous_visit_that_claims_neither():
    from door_events import our_visits
    t0, readings = _from_start([5, 6, 7, 7])                   # rises seen at +1 h and +2 h
    one = (t0 + timedelta(minutes=50), t0 + timedelta(hours=1, minutes=10))      # only the +1 h window can hold it
    assert our_visits(readings, [one]) == ([(t0, t0 + timedelta(hours=1), 1)], [])
    two = (t0 + timedelta(hours=1, minutes=15), t0 + timedelta(hours=1, minutes=30))   # either window can
    assert our_visits(readings, [two]) == ([], [(t0 + timedelta(hours=1, minutes=30), "ambiguous",
                                                 (t0 + timedelta(hours=1), t0 + timedelta(hours=2)))])
    # the same receipt twice, or another receipt of the same two candidates, is still one visit
    assert len(our_visits(readings, [two, two, (two[0], t0 + timedelta(hours=1, minutes=40))])[1]) == 1


def test_the_lab_listing_keeps_the_full_fulfilment_time_and_tells_receipts_of_one_minute_apart():
    import json

    from door_events import is_listed, unplaced_listing
    done = datetime.fromisoformat("2026-12-04T11:30:00-05:00")
    later = done + timedelta(seconds=15)
    rises = (done - timedelta(hours=1), done)
    listing = json.loads(json.dumps(unplaced_listing([(done, "unknown", ()), (later, "ambiguous", rises)])))     # as lab_data.json
    assert listing[0] == {"fulfilled_at": "2026-12-04T11:30:00-05:00", "window": "unknown", "candidate_rises": []}
    assert listing[1]["fulfilled_at"] == "2026-12-04T11:30:15-05:00"
    assert listing[1]["candidate_rises"] == ["2026-12-04T10:30-05:00", "2026-12-04T11:30-05:00"]
    assert is_listed(listing, done, "unknown") and is_listed(listing, later, "ambiguous")
    assert not is_listed(listing, later, "unknown") and not is_listed(listing, done, "ambiguous")      # kind must match
    assert not is_listed(listing, done + timedelta(seconds=1), "unknown")                               # to the second
    assert is_listed(listing, done.astimezone(timezone.utc), "unknown")                                 # the same instant
    # an entry from an older analysis (minutes only), a naive time, or no time lists nothing
    for odd in ({"fulfilled_at": "2026-12-04T11:30-05:00", "window": "unknown"},
                {"fulfilled_at": "2026-12-04T11:30:00", "window": "unknown"},
                {"fulfilled_at": None, "window": "unknown"}, {"window": "unknown"}):
        assert is_listed([odd], done, "unknown") == (odd.get("fulfilled_at") == "2026-12-04T11:30-05:00")
    assert not is_listed([], done, "unknown")
