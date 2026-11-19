"""analysis/boundary_units.py: units sold in a poll interval whose two ends show different prices."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "analysis"))

from boundary_units import split_boundary  # noqa: E402


def test_the_counters_settle_a_mixed_interval():
    # 3 units, 650 cents, price 150 -> 250: 150*old + 250*(3-old) = 650 -> old = 1
    assert split_boundary(3, 650, 150, 250) == (1, 2, 0)


def test_all_units_at_the_old_price_when_the_cents_say_so():
    assert split_boundary(2, 300, 150, 250) == (2, 0, 0)


def test_all_units_at_the_new_price_are_not_credited_to_the_opening_price():
    assert split_boundary(2, 500, 150, 250) == (0, 2, 0)


def test_units_the_counters_cannot_split_are_unknown_not_the_opening_price():
    # 2 units, 333 cents fits no whole split of 150 and 250 (a misread or a third price)
    assert split_boundary(2, 333, 150, 250) == (0, 0, 2)
    assert split_boundary(2, None, 150, 250) == (0, 0, 2)


def test_an_interval_with_one_price_is_all_that_price():
    assert split_boundary(4, 600, 150, 150) == (4, 0, 0)
    assert split_boundary(4, 600, None, 150) == (0, 4, 0)       # no reading before it: the one reading there is
