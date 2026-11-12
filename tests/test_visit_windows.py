"""analysis/visit_windows.py: visits from door-counter rises, the unseen window, and our sales inside it."""

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "analysis"))

import visit_windows as vw  # noqa: E402

T0 = datetime.fromisoformat("2026-10-05T07:00:00-04:00")     # a Monday, in the hourly era


def at(h):
    return (T0 + timedelta(hours=h)).isoformat()


def polls(hours):
    return [{"at_eastern": at(h)} for h in hours]


def door(rises):
    """rises: {hour: rise} on an hourly poll grid from 0 to 12."""
    return [{"at_eastern": at(h), "rise": str(rises.get(h, 0))} for h in range(0, 13)]


def sale(h, units=1, slot="15"):
    return {"slot": slot, "at_eastern": at(h), "units": str(units)}


def test_one_rise_is_one_visit_with_a_one_poll_window():
    r = vw.analyse(door({6: 1}), polls(range(13)), [])
    (v,) = r["visits"]
    assert v["window_min"] == 60 and v["openings"] == 1 and v["weekday"] == "Mon"
    assert v["from"].startswith("2026-10-05T12:00") and v["to"].startswith("2026-10-05T13:00")


def test_rises_within_three_hours_merge_and_widen_the_window():
    r = vw.analyse(door({4: 2, 6: 3}), polls(range(13)), [])
    (v,) = r["visits"]
    assert v["openings"] == 5 and v["window_min"] == 180          # 10:00 (poll before) to 13:00


def test_rises_more_than_three_hours_apart_are_two_visits():
    r = vw.analyse(door({2: 1, 8: 1}), polls(range(13)), [])
    assert len(r["visits"]) == 2


def test_our_sales_at_a_rise_poll_count_as_inside_the_window():
    r = vw.analyse(door({6: 1}), polls(range(13)), [sale(6, 2), sale(5, 4), sale(9, 1, "16")])
    (v,) = r["visits"]
    assert v["our_units_inside"] == 2                               # only the poll that also saw the door open
    assert v["minutes_to_next_sale"] == 180                         # 13:00 to the 16:00 sale
    assert r["summary"]["our_units_inside"] == 2 and r["summary"]["our_units"] == 7


def test_visits_before_hourly_polling_are_left_out():
    early = vw.HOURLY_FROM - timedelta(days=7)
    r = vw.analyse(door({6: 1}), polls(range(13)), [], since=early + timedelta(days=30))
    assert r["visits"] == []


def test_poll_gaps_report_the_long_ones():
    g = vw.poll_gaps([T0 + timedelta(hours=h) for h in (0, 1, 2, 5, 6)], T0)
    assert g["gaps"] == 4 and g["over_65_min"] == 1 and g["over_125_min"] == 1
    assert g["longest"][0]["minutes"] == 180


def test_grades_come_from_the_lab_data_by_window_start():
    lab = {"visits": [{"start": at(5)[:16], "evidence": "likely refill"}], "calendar": {"days": []}}
    r = vw.analyse(door({6: 1}), polls(range(13)), [], lab)
    assert r["visits"][0]["grade"] == "likely refill"
    assert r["summary"]["restock_visits"] == 1 and r["summary"]["restock_weekdays"] == {"Mon": 1}


def test_without_lab_data_visits_are_ungraded():
    r = vw.analyse(door({6: 1}), polls(range(13)), [])
    assert r["visits"][0]["grade"] is None and r["summary"]["graded"] is False
