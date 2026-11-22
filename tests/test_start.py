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


def test_the_fall_cut_defaults_to_18_november_and_is_never_after_the_start(monkeypatch):
    """The fall cut (`docs/integrity.md`, "Fall data") is defined once, in `vending.start`, clamped to the start."""
    from vending import start

    assert start.fall_cut() == datetime(2026, 11, 18, 5, 0, tzinfo=timezone.utc)   # conftest unsets FALL_CUTOFF
    monkeypatch.setenv("FALL_CUTOFF", "2026-11-30T08:00:00-05:00")
    assert start.fall_cut() == datetime(2026, 11, 30, 13, 0, tzinfo=timezone.utc)
    monkeypatch.setenv("FALL_CUTOFF", "2026-12-20T00:00:00-05:00")
    assert start.fall_cut() == START
    for bad in ("18 Nov", "2026-11-18T00:00:00"):
        monkeypatch.setenv("FALL_CUTOFF", bad)
        with pytest.raises(ValueError, match="FALL_CUTOFF"):
            start.fall_cut()


def test_the_analysis_uses_the_same_start_and_fall_cut(monkeypatch):
    import sys
    from pathlib import Path

    from vending import start

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "analysis"))
    import slots_config

    assert slots_config.START is START
    assert slots_config.DEFAULT_FALL_CUTOFF == start.DEFAULT_FALL_CUTOFF
    monkeypatch.setenv("FALL_CUTOFF", "2026-11-25T00:00:00-05:00")
    assert slots_config.fall_end() == start.fall_cut()
