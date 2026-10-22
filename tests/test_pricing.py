"""Tests for the pricing gate. Run from the repo root:

    python -m unittest discover tests
"""

from __future__ import annotations

import unittest
from decimal import Decimal
from fractions import Fraction

from vending.pricing import (
    check_price,
    floor_cents,
    pack_floor_cents,
    round_up_to_step,
    slot_floor_cents,
    to_cents,
)


class FloorTable(unittest.TestCase):
    """The worked examples in tool-list.md, "The pricing gate"."""

    def test_documented_examples(self):
        cases = [
            # (max pack price in cents, pack size, floor in cents)
            (1200, 24, 55),    # Mountain Dew, 24-pack, max $12.00
            (750, 12, 70),     # its alternate, 12-pack, max $7.50
            (3499, 24, 160),   # Coca-Cola 20 oz, 24-pack, max $34.99
            (2999, 12, 270),   # Pickle Potion, 12-pack, max $29.99
            (449, 6, 85),      # a 6-pack, max $4.49
        ]
        for pack_cents, size, expected in cases:
            with self.subTest(pack_cents=pack_cents, size=size):
                self.assertEqual(pack_floor_cents(pack_cents, size), expected)

    def test_exact_multiple_is_not_bumped(self):
        # A cost that lands exactly on a 5-cent step must stay put. With 0% tax,
        # 50c costs 50c; with 8% tax, 50c costs 54c, which rounds up to 55c.
        self.assertEqual(floor_cents(50, tax_percent=0), 50)
        self.assertEqual(floor_cents(50), 55)

    def test_never_rounds_down(self):
        # 1.575 dollars must be 1.60, not 1.55 (the float trap).
        self.assertEqual(floor_cents(Fraction(3499, 24)), 160)

    def test_fraction_of_a_cent_cost(self):
        # 62.5c a unit x 1.08 = 67.5c -> 70c
        self.assertEqual(floor_cents(Fraction(125, 2)), 70)

    def test_zero_cost_gives_zero_floor(self):
        self.assertEqual(floor_cents(0), 0)

    def test_bad_inputs(self):
        with self.assertRaises(ValueError):
            pack_floor_cents(1200, 0)
        with self.assertRaises(ValueError):
            floor_cents(-1)


class SlotFloor(unittest.TestCase):
    def test_worst_case_wins(self):
        primary = pack_floor_cents(1200, 24)     # 55
        alternate = pack_floor_cents(750, 12)    # 70
        self.assertEqual(slot_floor_cents([primary, alternate]), 70)

    def test_stock_on_hand_can_raise_the_floor(self):
        buying = pack_floor_cents(1200, 24)      # 55
        on_hand = floor_cents(Fraction(90))      # units already cost 90c -> 100
        self.assertEqual(slot_floor_cents([buying, on_hand]), 100)

    def test_empty_list_is_an_error(self):
        with self.assertRaises(ValueError):
            slot_floor_cents([])


class CheckPrice(unittest.TestCase):
    def codes(self, price, floor=70, ceiling=600):
        return [r.code for r in check_price(price, floor, ceiling)]

    def test_legal_price(self):
        self.assertEqual(self.codes(250), [])

    def test_exactly_at_floor_and_ceiling_are_legal(self):
        self.assertEqual(self.codes(70), [])
        self.assertEqual(self.codes(600), [])

    def test_off_step_is_refused_not_rounded(self):
        self.assertEqual(self.codes(163), ["price_off_step"])

    def test_below_floor(self):
        self.assertEqual(self.codes(65), ["below_floor"])

    def test_above_ceiling(self):
        self.assertEqual(self.codes(605), ["above_ceiling"])

    def test_reports_every_problem_at_once(self):
        self.assertEqual(self.codes(63), ["price_off_step", "below_floor"])

    def test_message_names_the_limit_and_the_slot(self):
        (refusal,) = check_price(65, 70, 600, slot="15")
        self.assertIn("$0.70", refusal.message)
        self.assertEqual(refusal.slot, "15")


class ParsePrice(unittest.TestCase):
    def test_strings_and_decimals(self):
        self.assertEqual(to_cents("2.50"), 250)
        self.assertEqual(to_cents("3"), 300)
        self.assertEqual(to_cents(Decimal("0.05")), 5)

    def test_int_is_cents(self):
        self.assertEqual(to_cents(250), 250)

    def test_rejects_floats_and_fractions_of_a_cent(self):
        for bad in (2.5, True, "2.505", "abc", "NaN", "Infinity"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                to_cents(bad)


class RoundUp(unittest.TestCase):
    def test_round_up_to_step(self):
        self.assertEqual(round_up_to_step(Fraction(161)), 165)
        self.assertEqual(round_up_to_step(Fraction(165)), 165)
        self.assertEqual(round_up_to_step(Fraction(1)), 5)


if __name__ == "__main__":
    unittest.main()
