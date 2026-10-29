"""Tests for the restock planner. Run from the repo root:

    python -m pytest

Every fixture is synthetic: made-up products, prices and stock, chosen so the
arithmetic is easy to check by hand. Times use a fixed Eastern offset so the
tests don't need the IANA time zone database (Windows lacks it).
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from fractions import Fraction

from vending.planner import (
    MIDWEEK,
    NO_CHANGE,
    OUR_SLOTS,
    PRICE_ONLY,
    REPOSITION,
    RESTOCK,
    SCHEDULED,
    SWITCH,
    CatalogProduct,
    ClosetStock,
    Config,
    CurrentState,
    DesiredSlot,
    DesiredState,
    PendingRequest,
    PurchaseOption,
    SlotState,
    Sourcing,
    plan_restock,
)

EDT = timezone(timedelta(hours=-4), "EDT")
TUESDAY = datetime(2026, 10, 20, 12, 0, tzinfo=EDT)         # before every cutoff
WEDNESDAY_LATE = datetime(2026, 10, 21, 23, 59, tzinfo=EDT)
THURSDAY_NOON = datetime(2026, 10, 22, 12, 0, tzinfo=EDT)    # past Amazon only
THURSDAY_LAST_MINUTE = datetime(2026, 10, 22, 23, 59, 30, tzinfo=EDT)
FRIDAY_MIDNIGHT = datetime(2026, 10, 23, 0, 0, tzinfo=EDT)   # past everything
SATURDAY = datetime(2026, 10, 24, 9, 0, tzinfo=EDT)          # a new week

CONFIG = Config(slot_capacity=10, timezone=EDT)

MTDEW = "MTDEW-20OZ"
COKE = "COKE-20OZ"
PICKLE = "PICKLE-POTION-12OZ"
SPRITE = "SPRITE-20OZ"

CATALOG = (
    CatalogProduct(MTDEW),
    CatalogProduct(COKE),
    CatalogProduct(PICKLE),
    CatalogProduct(SPRITE),
    CatalogProduct("OLD-SODA", active=False),
)

# The worked examples from "The pricing gate": floors 55c / 70c, 160c, 270c.
TABLE = (
    Sourcing(MTDEW,
             PurchaseOption("BJS", 24, "12.00", expected_pack_price="10.00"),
             PurchaseOption("AMAZON", 12, "7.50", link="https://www.amazon.com/dp/MTDEW",
                            expected_pack_price="6.50")),
    Sourcing(COKE, PurchaseOption("WALMART", 24, "34.99", expected_pack_price="29.99")),
    Sourcing(PICKLE, PurchaseOption("AMAZON", 12, "29.99", link="https://www.amazon.com/dp/PICKLE",
                                    expected_pack_price="27.99")),
)

SPRITE_SOURCING = Sourcing(SPRITE, PurchaseOption("WALMART", 12, "6.00"))


def current(slots=None, closet=(), pending=(), balance=60000, table=TABLE) -> CurrentState:
    """Slot 15 holds 4 Mountain Dew at $2.50; every other slot is full of Coke at $2.00."""
    base = {s: SlotState(s, COKE, 200, 10, 125) for s in OUR_SLOTS}
    base["15"] = SlotState("15", MTDEW, 250, 4, 50)
    base.update(slots or {})
    return CurrentState(tuple(base.values()), tuple(closet), tuple(pending),
                        CATALOG, tuple(table), balance)


def desired(*changes: DesiredSlot, visit=SCHEDULED, sourcing=()) -> DesiredState:
    """Every slot kept, except the ones given."""
    by_slot = {c.slot: c for c in changes}
    return DesiredState(visit, tuple(by_slot.get(s, DesiredSlot(s, keep=True)) for s in OUR_SLOTS),
                        tuple(sourcing))


def plan(*changes, now=TUESDAY, config=CONFIG, visit=SCHEDULED, sourcing=(), **state):
    return plan_restock(current(**state), desired(*changes, visit=visit, sourcing=sourcing),
                        config, now)


def restock_15(price="2.50", fill_to=10) -> DesiredSlot:
    return DesiredSlot("15", product=MTDEW, sale_price=price, fill_to=fill_to)


def codes(p) -> list[str]:
    return [r.code for r in p.refusals]


def line(p, slot):
    return next(ln for ln in p.lines if ln.slot == slot)


def floor_of(p, slot):
    return next(f for f in p.floors if f.slot == slot)


class SlotCases(unittest.TestCase):
    """David's cases: no change, restock, price only, reposition, switch."""

    def test_all_keep_is_no_change(self):
        p = plan()
        self.assertTrue(p.ok)
        self.assertEqual({ln.action for ln in p.lines}, {NO_CHANGE})
        self.assertEqual(len(p.lines), 20)
        self.assertEqual(p.cost.max_cents, 0)
        self.assertEqual(p.floors, ())
        self.assertIsNotNone(p.plan_hash)

    def test_identical_target_is_no_change(self):
        p = plan(restock_15(fill_to=4))
        self.assertEqual(line(p, "15").action, NO_CHANGE)

    def test_fill_below_on_hand_leaves_units_in_place(self):
        p = plan(restock_15(fill_to=2))
        ln = line(p, "15")
        self.assertEqual(ln.action, NO_CHANGE)
        self.assertEqual(ln.units_staying, 4)
        self.assertEqual(ln.units_pulled, 0)

    def test_restock_buys_the_shortfall(self):
        ln = line(plan(restock_15()), "15")
        self.assertEqual(ln.action, RESTOCK)
        self.assertFalse(ln.change_product)
        self.assertEqual((ln.units_staying, ln.units_to_load), (4, 6))
        self.assertEqual(ln.purchase.units_short, 6)
        self.assertEqual((ln.purchase.primary.store, ln.purchase.primary.packs), ("BJS", 1))
        self.assertEqual(ln.purchase.primary.units_to_closet, 18)
        self.assertEqual((ln.purchase.alternate.store, ln.purchase.alternate.packs), ("AMAZON", 1))
        self.assertEqual(ln.purchase.alternate.link, "https://www.amazon.com/dp/MTDEW")

    def test_fill_to_defaults_to_capacity(self):
        ln = line(plan(DesiredSlot("15", product=MTDEW, sale_price="2.50")), "15")
        self.assertEqual((ln.fill_to, ln.units_to_load), (10, 6))

    def test_restock_draws_on_the_closet_before_buying(self):
        ln = line(plan(restock_15(), closet=[ClosetStock(MTDEW, 4, 50)]), "15")
        self.assertEqual(ln.from_back_stock, 4)
        self.assertEqual(ln.purchase.units_short, 2)

    def test_closet_can_cover_the_whole_restock(self):
        p = plan(restock_15(), closet=[ClosetStock(MTDEW, 9, 50)])
        ln = line(p, "15")
        self.assertEqual((ln.action, ln.from_back_stock, ln.purchase), (RESTOCK, 6, None))
        self.assertEqual(p.cost.max_cents, 0)

    def test_price_only(self):
        p = plan(restock_15(price="3.00", fill_to=4))
        ln = line(p, "15")
        self.assertEqual((ln.action, ln.sale_price_cents, ln.units_to_load), (PRICE_ONLY, 300, 0))
        self.assertIsNone(ln.purchase)
        # Only the stock on hand sets the floor: 50c x 1.08 = 54c -> 55c.
        self.assertEqual(floor_of(p, "15").floor_cents, 55)
        self.assertEqual([part.source for part in floor_of(p, "15").parts], ["on hand"])

    def test_reposition_moves_units_between_slots(self):
        # Slot 16's Coke moves to slot 25, which pulls its Pickle Potion;
        # slot 16 switches to Mountain Dew, bought new.
        p = plan(
            DesiredSlot("16", product=MTDEW, sale_price="2.50", fill_to=10),
            DesiredSlot("25", product=COKE, sale_price="2.00", fill_to=10),
            slots={"25": SlotState("25", PICKLE, 300, 5, 250)})
        self.assertTrue(p.ok, p.refusals)
        to = line(p, "25")
        self.assertEqual(to.action, REPOSITION)
        self.assertTrue(to.change_product)
        self.assertEqual(to.from_slots, (("16", 10),))
        self.assertEqual((to.pulled_product, to.units_pulled), (PICKLE, 5))
        self.assertIsNone(to.purchase)
        frm = line(p, "16")
        self.assertEqual(frm.action, SWITCH)
        self.assertEqual((frm.pulled_product, frm.units_pulled), (COKE, 10))
        self.assertEqual(frm.purchase.units_short, 10)
        # The moved Coke cost 125c: x 1.08 = 135c.
        self.assertEqual(floor_of(p, "25").floor_cents, 135)

    def test_switch_pulls_old_units_and_loads_new(self):
        p = plan(DesiredSlot("17", product=PICKLE, sale_price="3.00", fill_to=6),
                 closet=[ClosetStock(PICKLE, 2, 250)])
        self.assertTrue(p.ok, p.refusals)
        ln = line(p, "17")
        self.assertEqual(ln.action, SWITCH)
        self.assertTrue(ln.change_product)
        self.assertEqual((ln.pulled_product, ln.units_pulled), (COKE, 10))
        self.assertEqual((ln.units_staying, ln.from_back_stock), (0, 2))
        self.assertEqual((ln.purchase.units_short, ln.purchase.primary.packs), (4, 1))

    def test_switch_into_an_empty_slot_pulls_nothing(self):
        p = plan(DesiredSlot("18", product=PICKLE, sale_price="3.00", fill_to=6),
                 slots={"18": SlotState("18", None, None, 0, None)})
        ln = line(p, "18")
        self.assertEqual((ln.action, ln.change_product, ln.units_pulled), (SWITCH, True, 0))
        self.assertIsNone(ln.pulled_product)


class Purchases(unittest.TestCase):
    def test_packs_round_up_per_option(self):
        config = Config(slot_capacity=30, timezone=EDT)
        ln = line(plan(restock_15(fill_to=29), config=config), "15")    # 25 short
        self.assertEqual((ln.purchase.primary.packs, ln.purchase.primary.units_to_closet), (2, 23))
        self.assertEqual((ln.purchase.alternate.packs, ln.purchase.alternate.units_to_closet), (3, 11))

    def test_exact_multiple_buys_no_extra_pack(self):
        config = Config(slot_capacity=30, timezone=EDT)
        ln = line(plan(restock_15(fill_to=28), config=config), "15")    # 24 short
        self.assertEqual((ln.purchase.primary.packs, ln.purchase.primary.units_to_closet), (1, 0))
        self.assertEqual(ln.purchase.alternate.packs, 2)

    def test_two_slots_buy_separately(self):
        # Until David confirms one line may load another line's purchase.
        p = plan(restock_15(), DesiredSlot("16", product=MTDEW, sale_price="2.50", fill_to=6))
        self.assertEqual(line(p, "15").purchase.primary.packs, 1)
        self.assertEqual(line(p, "16").purchase.primary.packs, 1)
        self.assertEqual(p.cost.max_cents, 2 * 1200)

    def test_given_sourcing_beats_the_table(self):
        given = Sourcing(MTDEW, PurchaseOption("WALMART", 6, "4.49"))
        ln = line(plan(restock_15(), sourcing=[given]), "15")
        self.assertEqual((ln.purchase.primary.store, ln.purchase.primary.packs), ("WALMART", 1))
        self.assertIsNone(ln.purchase.alternate)

    def test_extra_units_ride_on_the_first_line_only(self):
        given = Sourcing(MTDEW, TABLE[0].primary, extra_units_to_closet=20)
        p = plan(restock_15(), DesiredSlot("16", product=MTDEW, sale_price="2.50", fill_to=6),
                 sourcing=[given])
        first = line(p, "15").purchase.primary
        self.assertEqual((first.packs, first.units_to_closet), (2, 42))    # 6 + 20 -> 2 x 24
        self.assertEqual(line(p, "16").purchase.primary.packs, 1)

    def test_extra_units_with_nothing_bought_is_a_warning(self):
        given = Sourcing(PICKLE, TABLE[2].primary, extra_units_to_closet=12)
        p = plan(sourcing=[given])
        self.assertTrue(p.ok)
        self.assertTrue(any("extra_units_to_closet" in w for w in p.warnings))


class Costs(unittest.TestCase):
    def test_expected_uses_primary_expected_max_uses_dearer_option(self):
        p = plan(restock_15())
        self.assertEqual(p.cost.expected_cents, 1000)   # 1 x $10.00 at BJS
        self.assertEqual(p.cost.max_cents, 1200)        # BJS $12.00 beats Amazon $7.50
        self.assertEqual(p.cost.balance_left_at_max_cents, 60000 - 1200)

    def test_alternate_can_be_the_worst_case(self):
        # 13 short: one 24-pack at $12.00, or two 12-packs at $7.50 = $15.00.
        config = Config(slot_capacity=30, timezone=EDT)
        p = plan(restock_15(fill_to=17), config=config)
        self.assertEqual(p.cost.max_cents, 1500)
        self.assertEqual(p.cost.expected_cents, 1000)

    def test_expected_price_is_borrowed_from_the_table(self):
        same = Sourcing(MTDEW, PurchaseOption("BJS", 24, "13.00"))
        other = Sourcing(MTDEW, PurchaseOption("BJS", 12, "8.00"))
        self.assertEqual(plan(restock_15(), sourcing=[same]).cost.expected_cents, 1000)
        self.assertEqual(plan(restock_15(), sourcing=[other]).cost.expected_cents, 800)

    def test_midweek_adds_the_visit_fee(self):
        p = plan(restock_15(), visit=MIDWEEK)
        self.assertEqual(p.cost.visit_fee_cents, 2500)
        self.assertEqual((p.cost.expected_cents, p.cost.max_cents), (3500, 3700))

    def test_balance_check_uses_max_plus_fee(self):
        self.assertIn("insufficient_balance", codes(plan(restock_15(), visit=MIDWEEK, balance=3699)))
        self.assertTrue(plan(restock_15(), visit=MIDWEEK, balance=3700).ok)


class PricingGate(unittest.TestCase):
    def test_floor_shows_its_arithmetic(self):
        f = floor_of(plan(restock_15()), "15")
        self.assertEqual(f.floor_cents, 70)
        self.assertEqual([(part.unit_cost, part.floor_cents) for part in f.parts],
                         [(Fraction(50), 55), (Fraction(50), 55), (Fraction(125, 2), 70)])

    def test_unchanged_price_is_rechecked_when_units_are_bought(self):
        # 60c clears the 55c floor of the stock on hand, but not the 70c
        # floor of the Amazon alternate the worker might buy.
        slots = {"15": SlotState("15", MTDEW, 60, 4, 50)}
        p = plan(restock_15(price="0.60"), slots=slots)
        self.assertEqual(codes(p), ["below_floor"])
        self.assertIn("$0.70", p.refusals[0].message)
        self.assertEqual(p.refusals[0].slot, "15")
        # Without new units the same price is no change, and not refused.
        self.assertTrue(plan(restock_15(price="0.60", fill_to=4), slots=slots).ok)

    def test_costlier_stock_on_hand_lifts_the_floor(self):
        slots = {"15": SlotState("15", MTDEW, 250, 4, 90)}     # 90c x 1.08 -> $1.00
        p = plan(restock_15(price="0.95"), slots=slots)
        self.assertEqual(codes(p), ["below_floor"])
        self.assertEqual(floor_of(p, "15").floor_cents, 100)

    def test_unknown_on_hand_cost_is_a_warning(self):
        slots = {"15": SlotState("15", MTDEW, 250, 4, None)}
        p = plan(restock_15(price="3.00", fill_to=4), slots=slots)
        self.assertTrue(p.ok)
        self.assertEqual(floor_of(p, "15").floor_cents, 0)
        self.assertTrue(any("unknown" in w for w in p.warnings))


class Refusals(unittest.TestCase):
    """One test per code in the refusals table."""

    def test_halted(self):
        p = plan(restock_15(), config=Config(slot_capacity=10, timezone=EDT, halted=True))
        self.assertEqual(codes(p), ["halted"])
        self.assertEqual(p.lines, ())

    def test_past_cutoff_for_in_store(self):
        self.assertEqual(codes(plan(restock_15(), now=FRIDAY_MIDNIGHT)), ["past_cutoff"])

    def test_cutoff_minute_itself_is_not_past(self):
        self.assertTrue(plan(restock_15(), now=THURSDAY_LAST_MINUTE).ok)

    def test_cutoff_resets_after_the_friday_visit(self):
        self.assertTrue(plan(restock_15(), now=SATURDAY).ok)

    def test_cutoff_is_in_eastern_time(self):
        # 03:30 UTC Friday is 23:30 Thursday in Eastern Daylight Time.
        now = datetime(2026, 10, 23, 3, 30, tzinfo=timezone.utc)
        self.assertTrue(plan(restock_15(), now=now).ok)

    def test_past_amazon_cutoff_is_only_a_warning(self):
        p = plan(DesiredSlot("17", product=PICKLE, sale_price="3.00", fill_to=6), now=THURSDAY_NOON)
        self.assertTrue(p.ok)
        self.assertTrue(any("AMAZON cutoff" in w for w in p.warnings))
        self.assertTrue(plan(DesiredSlot("17", product=PICKLE, sale_price="3.00", fill_to=6),
                             now=WEDNESDAY_LATE).ok)

    def test_in_store_alternate_counts_too(self):
        given = Sourcing(PICKLE, TABLE[2].primary, PurchaseOption("BJS", 12, "29.99"))
        p = plan(DesiredSlot("17", product=PICKLE, sale_price="3.00", fill_to=6),
                 now=FRIDAY_MIDNIGHT, sourcing=[given])
        self.assertEqual(codes(p), ["past_cutoff"])

    def test_request_pending(self):
        p = plan(restock_15(), pending=[PendingRequest("pr-41")])
        self.assertEqual(codes(p), ["request_pending"])
        self.assertIn("pr-41", p.refusals[0].message)

    def test_slot_list(self):
        targets = [DesiredSlot(s, keep=True) for s in OUR_SLOTS if s != "58"]
        targets += [DesiredSlot("15", keep=True), DesiredSlot("15", keep=True), DesiredSlot("11", keep=True)]
        p = plan_restock(current(), DesiredState(SCHEDULED, tuple(targets)), CONFIG, TUESDAY)
        self.assertEqual(codes(p), ["slot_list"] * 3)
        self.assertEqual(sorted(r.slot for r in p.refusals), ["11", "15", "58"])

    def test_product_unknown_or_inactive(self):
        p = plan(DesiredSlot("16", product="NOPE", sale_price="2.00"),
                 DesiredSlot("17", product="OLD-SODA", sale_price="2.00"))
        self.assertEqual(codes(p), ["product_unknown", "product_unknown"])
        self.assertEqual(line(p, "16").action, NO_CHANGE)

    def test_no_sourcing(self):
        p = plan(DesiredSlot("17", product=SPRITE, sale_price="2.00"))
        self.assertEqual(codes(p), ["no_sourcing"])

    def test_given_sourcing_fills_the_gap(self):
        p = plan(DesiredSlot("17", product=SPRITE, sale_price="2.00"), sourcing=[SPRITE_SOURCING])
        self.assertTrue(p.ok, p.refusals)

    def test_insufficient_stock(self):
        p = plan(DesiredSlot("17", product=SPRITE, sale_price="2.00"),
                 closet=[ClosetStock(SPRITE, 3, 40)])
        self.assertEqual(codes(p), ["insufficient_stock"])
        self.assertEqual(line(p, "17").from_back_stock, 3)

    def test_bad_sourcing(self):
        bad = [
            Sourcing(SPRITE, PurchaseOption("TARGET", 12, "6.00")),
            Sourcing(MTDEW, PurchaseOption("AMAZON", 12, "7.50")),
            Sourcing(PICKLE, PurchaseOption("WALMART", 0, "6.00")),
            Sourcing(COKE, PurchaseOption("WALMART", 24, 34.99)),
        ]
        p = plan(sourcing=bad)
        self.assertEqual(codes(p), ["bad_sourcing"] * 4)

    def test_sourcing_given_twice(self):
        p = plan(sourcing=[SPRITE_SOURCING, SPRITE_SOURCING])
        self.assertEqual(codes(p), ["bad_sourcing"])

    def test_bad_sourcing_is_not_also_no_sourcing(self):
        bad = Sourcing(SPRITE, PurchaseOption("TARGET", 12, "6.00"))
        p = plan(DesiredSlot("17", product=SPRITE, sale_price="2.00"), sourcing=[bad])
        self.assertEqual(codes(p), ["bad_sourcing"])

    def test_bad_table_row_counts_only_when_used(self):
        table = TABLE + (Sourcing(SPRITE, PurchaseOption("AMAZON", 12, "6.00")),)
        self.assertTrue(plan(table=table).ok)
        p = plan(DesiredSlot("17", product=SPRITE, sale_price="2.00"), table=table)
        self.assertEqual(codes(p), ["bad_sourcing"])

    def test_over_capacity(self):
        self.assertEqual(codes(plan(restock_15(fill_to=11))), ["over_capacity"])

    def test_price_off_step(self):
        self.assertEqual(codes(plan(restock_15(price="2.53"))), ["price_off_step"])

    def test_missing_or_unparseable_price(self):
        p = plan(DesiredSlot("15", product=MTDEW), DesiredSlot("16", product=MTDEW, sale_price="abc"))
        self.assertEqual(codes(p), ["price_off_step", "price_off_step"])

    def test_below_floor(self):
        self.assertEqual(codes(plan(restock_15(price="0.65"))), ["below_floor"])

    def test_above_ceiling(self):
        self.assertEqual(codes(plan(restock_15(price="6.05"))), ["above_ceiling"])

    def test_insufficient_balance(self):
        self.assertEqual(codes(plan(restock_15(), balance=1199)), ["insufficient_balance"])
        self.assertTrue(plan(restock_15(), balance=1200).ok)

    def test_empty_slot(self):
        self.assertEqual(codes(plan(DesiredSlot("15", product=None, sale_price="2.50"))), ["empty_slot"])

    def test_midweek_no_op(self):
        self.assertEqual(codes(plan(visit=MIDWEEK)), ["midweek_no_op"])

    def test_swap_unconfirmed(self):
        p = plan(DesiredSlot("15", product=COKE, sale_price="2.00", fill_to=4),
                 DesiredSlot("16", product=MTDEW, sale_price="2.50", fill_to=10))
        self.assertEqual(codes(p), ["swap_unconfirmed"])
        self.assertIn("15, 16", p.refusals[0].message)

    def test_every_refusal_at_once(self):
        p = plan(
            DesiredSlot("15", product=None, sale_price="2.50"),
            DesiredSlot("16", product="NOPE", sale_price="2.00"),
            DesiredSlot("17", product=PICKLE, sale_price="3.03", fill_to=6),
            DesiredSlot("18", product=MTDEW, sale_price="2.50", fill_to=12),
            DesiredSlot("25", product=SPRITE, sale_price="2.00"),
            DesiredSlot("26", product=PICKLE, sale_price="3.00", fill_to=6),
            pending=[PendingRequest("pr-41")])
        self.assertEqual(sorted(codes(p)), sorted([
            "empty_slot", "product_unknown", "over_capacity", "no_sourcing",
            "request_pending", "price_off_step"]))
        self.assertIsNone(p.plan_hash)
        # Lines, costs and floors still come back for the preview.
        self.assertEqual(len(p.lines), 20)
        self.assertEqual(line(p, "26").purchase.primary.packs, 1)
        self.assertEqual(floor_of(p, "26").floor_cents, 270)


class PlanHash(unittest.TestCase):
    def test_same_plan_same_hash_whenever_computed(self):
        a = plan(restock_15(), now=TUESDAY)
        b = plan(restock_15(), now=WEDNESDAY_LATE)
        self.assertEqual(a.plan_hash, b.plan_hash)

    def test_different_plan_different_hash(self):
        self.assertNotEqual(plan(restock_15()).plan_hash, plan(restock_15(price="2.55")).plan_hash)
        self.assertNotEqual(plan(restock_15()).plan_hash,
                            plan(restock_15(), visit=MIDWEEK).plan_hash)


class Directive(unittest.TestCase):
    def test_worker_table_and_shopping_list(self):
        d = plan(restock_15(), closet=[ClosetStock(MTDEW, 2, 50)]).directive
        self.assertIn("Visit: scheduled", d)
        self.assertIn("back stock 2, buy 4", d)
        shopping = d.split("Shopping list", 1)[1]
        self.assertIn("BJS\n  MTDEW-20OZ: 1 x 24-pack, max $12.00 a pack, for slot 15 (20 to closet)",
                      shopping)
        self.assertIn("only if BJS is unavailable or over its max", shopping)
        self.assertIn("https://www.amazon.com/dp/MTDEW", shopping)


class Inputs(unittest.TestCase):
    def test_naive_now_is_an_error(self):
        with self.assertRaises(ValueError):
            plan(now=datetime(2026, 10, 20, 12, 0))

    def test_unknown_visit_is_an_error(self):
        with self.assertRaises(ValueError):
            DesiredState("tuesday", ())

    def test_our_slots(self):
        self.assertEqual(OUR_SLOTS, ("15", "16", "17", "18", "25", "26", "27", "28", "35", "36",
                                     "37", "38", "45", "46", "47", "48", "55", "56", "57", "58"))


if __name__ == "__main__":
    unittest.main()
