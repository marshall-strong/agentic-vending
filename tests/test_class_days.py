"""Tests for `vending.class_days`: it must agree with the official calendar file."""

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from vending.class_days import TERMS, class_days_after, is_class_day

CALENDAR = Path(__file__).resolve().parents[1] / "analysis" / "golisano_calendar_2026_27.json"


def calendar_says_class_day(day: date, calendar: dict) -> bool:
    """The calendar file's rule: a class weekday inside a term, with no event cancelling classes."""
    if day.strftime("%a") not in calendar["class_weekdays"]:
        return False
    if not any(date.fromisoformat(t["start"]) <= day <= date.fromisoformat(t["end"]) for t in calendar["terms"]):
        return False
    return not any(e.get("classes") == "none"
                   and date.fromisoformat(e["start"]) <= day <= date.fromisoformat(e["end"])
                   for e in calendar["events"])


def test_agrees_with_the_calendar_file_every_day_of_our_terms():
    calendar = json.loads(CALENDAR.read_text(encoding="utf-8"))
    day, last = TERMS[0][0] - timedelta(days=10), TERMS[-1][1] + timedelta(days=10)
    wrong = []
    while day <= last:
        if is_class_day(day) != calendar_says_class_day(day, calendar):
            wrong.append(day.isoformat())
        day += timedelta(days=1)
    assert wrong == [], f"vending/class_days.py disagrees with {CALENDAR.name} on {wrong}"


def test_the_competition_calendar():
    assert is_class_day(date(2026, 12, 3)) and is_class_day(date(2026, 12, 4))   # start, first Friday
    assert not is_class_day(date(2026, 12, 5))                                     # Saturday
    assert not is_class_day(date(2026, 12, 22)) and not is_class_day(date(2027, 1, 1))
    assert is_class_day(date(2027, 1, 4))                                          # classes resume
    assert is_class_day(date(2027, 1, 18))                                         # MLK Day: classes in session
    assert not is_class_day(date(2027, 2, 15))                                     # Presidents Day
    assert not is_class_day(date(2026, 11, 30))                                    # quarter break


def test_a_friday_afternoon_load_starts_counting_on_monday():
    friday_3pm = datetime(2026, 12, 4, 20, tzinfo=timezone.utc)                    # 3 PM Eastern
    thursday = datetime(2026, 12, 10, 17, tzinfo=timezone.utc)
    assert class_days_after(friday_3pm, thursday) == [date(2026, 12, d) for d in (7, 8, 9, 10)]


def test_days_are_eastern_not_utc():
    late_friday = datetime(2026, 12, 5, 2, tzinfo=timezone.utc)                    # Fri 9 PM Eastern
    monday = datetime(2026, 12, 7, 15, tzinfo=timezone.utc)
    assert class_days_after(late_friday, monday) == [date(2026, 12, 7)]


def test_the_holiday_break_counts_nothing():
    friday = datetime(2026, 12, 18, 20, tzinfo=timezone.utc)
    after = datetime(2027, 1, 5, 15, tzinfo=timezone.utc)
    assert class_days_after(friday, after) == [date(2026, 12, 21), date(2027, 1, 4), date(2027, 1, 5)]
