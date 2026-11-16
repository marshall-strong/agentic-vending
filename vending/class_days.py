"""Which days the Institute holds classes, for demand counted per class day.

Most of our sales come on class mornings (`analysis/REPORT.md`: 66% of machine
units sold 8-11 AM on weekdays), and a weekday without classes sells little.
So demand is measured per class day, never per calendar day.

A class day is a weekday inside a term, outside every break or closure that
cancels classes. The dates are copied from the official calendar in
`analysis/golisano_calendar_2026_27.json`, because the container image ships
`vending/` but not `analysis/`. `tests/test_class_days.py` checks the two agree,
so a change to the calendar file that isn't copied here fails the tests.

Days are Eastern calendar dates, the Institute's own.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")

# (first day, last day) of each term: classes run on weekdays between them.
TERMS = (
    (date(2026, 9, 9), date(2026, 11, 17)),    # Fall Quarter 2026
    (date(2026, 12, 3), date(2027, 2, 19)),    # Winter Quarter 2026-2027: the competition
    (date(2027, 3, 8), date(2027, 5, 14)),     # Spring Quarter 2027
)

# (first day, last day) of every stretch inside a term with no classes.
NO_CLASSES = (
    (date(2026, 10, 9), date(2026, 10, 9)),    # Mid-quarter break
    (date(2026, 10, 12), date(2026, 10, 12)),  # Indigenous Peoples' Day, Institute closed
    (date(2026, 12, 22), date(2027, 1, 1)),    # Winter Holiday Break (and its closures)
    (date(2027, 2, 15), date(2027, 2, 15)),    # Presidents Day, Institute closed
)


def is_class_day(day: date) -> bool:
    """True for a weekday in a term with classes scheduled."""
    if day.weekday() >= 5:
        return False
    if not any(first <= day <= last for first, last in TERMS):
        return False
    return not any(first <= day <= last for first, last in NO_CLASSES)


def eastern_date(moment: datetime) -> date:
    """The Eastern calendar date of an aware moment."""
    return moment.astimezone(EASTERN).date()


def class_days_after(start: datetime, end: datetime) -> list[date]:
    """Class days after `start`'s date, up to and including `end`'s date (both Eastern).

    The day of `start` itself never counts: a slot loaded on a Friday afternoon
    missed that morning's sales, so its first selling day is the next class day.
    The day of `end` counts, because the slot held stock when it began.
    """
    first, last = eastern_date(start) + timedelta(days=1), eastern_date(end)
    days, day = [], first
    while day <= last:
        if is_class_day(day):
            days.append(day)
        day += timedelta(days=1)
    return days
