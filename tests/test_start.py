"""The competition's start is defined once, in `vending.start`, and everything that needs it uses it."""

from datetime import date, datetime, timedelta, timezone

import pytest

from vending import class_days
from vending.start import START, on_or_after_start


def test_start_is_midnight_eastern_on_3_dec_2026():
    assert START.utcoffset() is not None
    assert START == datetime(2026, 12, 3, 5, 0, tzinfo=timezone.utc)   # EST is UTC-5


def test_on_or_after_start():
    assert on_or_after_start(START)
    assert not on_or_after_start(START - timedelta(microseconds=1))
    assert on_or_after_start(datetime(2026, 12, 3, 5, 0, tzinfo=timezone.utc))
    with pytest.raises(ValueError):
        on_or_after_start(datetime(2026, 12, 3, 12, 0))


def test_every_copy_of_the_start_is_this_one():
    pytest.importorskip("psycopg")   # the catalog request tool imports it
    from vending.tools import catalog_request

    assert catalog_request.MACHINE_LIVE is START
    assert class_days.TERMS[1][0] == START.date() == date(2026, 12, 3)
    assert class_days.EASTERN is START.tzinfo
