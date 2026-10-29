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
    BACK_STOCK,
    OUR_SLOTS,
    PURCHASE,
    RESTOCK,
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

MTDEW_URL = "https://www.amazon.com/dp/MTDEW"
PICKLE_URL = "https://www.amazon.com/dp/PICKLE"

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
             PurchaseOption("BJs", 24, "12.00", expected_price_per_pack="10.00"),
             PurchaseOption("Amazon", 12, "7.50", url=MTDEW_URL, expected_price_per_pack="6.50")),
    Sourcing(COKE, PurchaseOption("Walmart", 24, "34.99", expected_price_per_pack="29.99")),
    Sourcing(PICKLE, PurchaseOption("Amazon", 12, "29.99", url=PICKLE_URL,
                                    expected_price_per_pack="27.99")),
)

SPRITE_SOURCING = Sourcing(SPRITE, PurchaseOption("Walmart", 12, "6.00"))


def current(slots=None, closet=(), pending=(), balance=60000, table=TABLE) -> CurrentState:
    """Slot 15 holds 4 Mountain Dew at $2.50; every other slot is full of Coke at $2.00."""
    base = {s: SlotState(s, COKE, 200, 10, 125) for s in OUR_SLOTS}
    base["15"] = SlotState("15", MTDEW, 250, 4, 50)
    base.update(slots or {})
    return CurrentState(tuple(base.values()), tuple(closet), tuple(pending),
                        CATALOG, tuple(table), balance)


def desired(*changes: DesiredSlot, off_cycle=False, sourcing=()) -> DesiredState:
    """Every slot kept, except the ones given."""
    by_slot = {c.slot: c for c in changes}
    return DesiredState(tuple(by_slot.get(s, DesiredSlot(s, keep=True)) for s in OUR_SLOTS),
                        off_cycle, tuple(sourcing))


def plan(*changes, now=TUESDAY, config=CONFIG, off_cycle=False, sourcing=(), **state):
    return plan_restock(current(**state), desired(*changes, off_cycle=off_cycle, sourcing=sourcing),
                        config, now)


def restock_15(price="2.50", fill_to=10) -> DesiredSlot:
    return DesiredSlot("15", product=MTDEW, sale_price=price, fill_to=fill_to)


def pickle_17(price="3.00", fill_to=6) -> DesiredSlot:
    return DesiredSlot("17", product=PICKLE, sale_price=price, fill_to=fill_to)


def codes(p) -> list[str]:
    return [r.code for r in p.refusals]


def line(p, slot):
    return next(ln for ln in p.lines if ln.slot == slot)


def floor_of(p, slot):
    return next(f for f in p.floors if f.slot == slot)


class SlotCases(unittest.TestCase):
    """David's cases: no change, restock, price only, switch."""

    def test_nothing_to_load(self):
        # The API refuses a request with no lines, so there is nothing to send.
        p = plan()
        self.assertEqual(codes(p), ["nothing_to_load"])
        self.assertEqual(p.lines, ())
        self.assertEqual(p.floors, ())

    def test_identical_target_is_no_change(self):
        p = plan(restock_15(fill_to=4), pickle_17(), closet=[ClosetStock(PICKLE, 6, 250)])
        self.assertTrue(p.ok, p.refusals)
        self.assertEqual([ln.slot for ln in p.lines], ["17"])

    def test_fill_below_on_hand_leaves_units_in_place(self):
        p = plan(restock_15(fill_to=2))
        self.assertEqual(codes(p), ["nothing_to_load"])

    def test_restock_buys_the_shortfall(self):
        ln = line(plan(restock_15()), "15")
        self.assertEqual((ln.action, ln.source), (RESTOCK, PURCHASE))
        self.assertFalse(ln.change_product)
        self.assertEqual((ln.units_staying, ln.units_to_load), (4, 6))
        self.assertEqual(ln.purchase.units_short, 6)
        self.assertEqual((ln.purchase.primary.store, ln.purchase.primary.packs), ("BJs", 1))
        self.assertEqual(ln.purchase.primary.units_to_closet, 18)
        self.assertEqual((ln.purchase.alternate.store, ln.purchase.alternate.packs), ("Amazon", 1))
        self.assertEqual(ln.purchase.alternate.url, MTDEW_URL)

    def test_fill_to_defaults_to_capacity(self):
        ln = line(plan(DesiredSlot("15", product=MTDEW, sale_price="2.50")), "15")
        self.assertEqual((ln.fill_to, ln.units_to_load), (10, 6))

    def test_closet_that_covers_the_load_is_used(self):
        p = plan(restock_15(), closet=[ClosetStock(MTDEW, 9, 50)])
        ln = line(p, "15")
        self.assertEqual((ln.source, ln.units_to_load, ln.purchase), (BACK_STOCK, 6, None))
        self.assertEqual(p.cost.max_cents, 0)

    def test_closet_that_falls_short_is_left_alone(self):
        # A line has one source, so 4 in the closet can't top up a purchase.
        p = plan(restock_15(), closet=[ClosetStock(MTDEW, 4, 50)])
        ln = line(p, "15")
        self.assertEqual((ln.source, ln.purchase.units_short), (PURCHASE, 6))
        self.assertTrue(any("fill_to 8" in w for w in p.warnings))

    def test_closet_is_shared_out_in_slot_order(self):
        p = plan(restock_15(), DesiredSlot("16", product=MTDEW, sale_price="2.50", fill_to=6),
                 closet=[ClosetStock(MTDEW, 8, 50)])
        self.assertEqual(line(p, "15").source, BACK_STOCK)
        self.assertEqual(line(p, "16").source, PURCHASE)    # only 2 left

    def test_price_only_is_refused(self):
        p = plan(restock_15(price="3.00", fill_to=4))
        self.assertEqual(codes(p), ["price_only_unsupported"])
        self.assertEqual(p.lines, ())
        # The floor still shows: 50c x 1.08 = 54c -> 55c.
        self.assertEqual(floor_of(p, "15").floor_cents, 55)

    def test_price_change_rides_on_a_restock(self):
        p = plan(restock_15(price="3.00", fill_to=5))
        self.assertTrue(p.ok, p.refusals)
        self.assertEqual(line(p, "15").sale_price_cents, 300)

    def test_switch_from_the_closet(self):
        p = plan(pickle_17(), closet=[ClosetStock(PICKLE, 6, 250)])
        self.assertTrue(p.ok, p.refusals)
        ln = line(p, "17")
        self.assertEqual((ln.action, ln.source, ln.change_product), (SWITCH, BACK_STOCK, True))
        self.assertEqual((ln.pulled_product, ln.units_pulled), (COKE, 10))
        self.assertEqual((ln.units_staying, ln.units_to_load), (0, 6))

    def test_switch_by_buying(self):
        ln = line(plan(pickle_17()), "17")
        self.assertEqual((ln.action, ln.source, ln.purchase.primary.packs), (SWITCH, PURCHASE, 1))

    def test_pulled_units_are_not_loaded_at_the_same_visit(self):
        # Slot 16's Coke goes to the closet; slot 25 buys its Coke instead,
        # until David confirms a line may load what another line pulls.
        p = plan(
            DesiredSlot("16", product=MTDEW, sale_price="2.50", fill_to=10),
            DesiredSlot("25", product=COKE, sale_price="2.00", fill_to=10),
            slots={"25": SlotState("25", PICKLE, 300, 5, 250)})
        self.assertTrue(p.ok, p.refusals)
        self.assertEqual((line(p, "16").pulled_product, line(p, "16").units_pulled), (COKE, 10))
        self.assertEqual(line(p, "25").source, PURCHASE)
        self.assertEqual(floor_of(p, "25").floor_cents, 160)

    def test_swapping_products_buys_both(self):
        p = plan(DesiredSlot("15", product=COKE, sale_price="2.00", fill_to=4),
                 DesiredSlot("16", product=MTDEW, sale_price="2.50", fill_to=10))
        self.assertTrue(p.ok, p.refusals)
        self.assertEqual({line(p, s).source for s in ("15", "16")}, {PURCHASE})

    def test_switch_into_an_empty_slot_pulls_nothing(self):
        p = plan(pickle_17(), slots={"17": SlotState("17", None, None, 0, None)})
        ln = line(p, "17")
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
        given = Sourcing(MTDEW, PurchaseOption("Walmart", 6, "4.49"))
        ln = line(plan(restock_15(), sourcing=[given]), "15")
        self.assertEqual((ln.purchase.primary.store, ln.purchase.primary.packs), ("Walmart", 1))
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
        p = plan(restock_15(), sourcing=[given])
        self.assertTrue(p.ok, p.refusals)
        self.assertTrue(any("extra_units_to_closet" in w for w in p.warnings))

    def test_too_many_packs(self):
        # 6 short + 20 extra in single cans is 26 packs; a line takes 20.
        given = Sourcing(MTDEW, PurchaseOption("Walmart", 1, "1.00"), extra_units_to_closet=20)
        p = plan(restock_15(), sourcing=[given])
        self.assertEqual(codes(p), ["too_many_packs"])
        self.assertEqual(line(p, "15").purchase.primary.packs, 26)


class Costs(unittest.TestCase):
    def test_expected_uses_primary_expected_max_uses_dearer_option(self):
        p = plan(restock_15())
        self.assertEqual(p.cost.expected_cents, 1000)   # 1 x $10.00 at BJs
        self.assertEqual(p.cost.max_cents, 1200)        # BJs $12.00 beats Amazon $7.50
        self.assertEqual(p.cost.balance_left_at_max_cents, 60000 - 1200)

    def test_alternate_can_be_the_worst_case(self):
        # 13 short: one 24-pack at $12.00, or two 12-packs at $7.50 = $15.00.
        config = Config(slot_capacity=30, timezone=EDT)
        p = plan(restock_15(fill_to=17), config=config)
        self.assertEqual(p.cost.max_cents, 1500)
        self.assertEqual(p.cost.expected_cents, 1000)

    def test_expected_price_is_borrowed_from_the_table(self):
        same = Sourcing(MTDEW, PurchaseOption("BJs", 24, "13.00"))
        other = Sourcing(MTDEW, PurchaseOption("BJs", 12, "8.00"))
        self.assertEqual(plan(restock_15(), sourcing=[same]).cost.expected_cents, 1000)
        self.assertEqual(plan(restock_15(), sourcing=[other]).cost.expected_cents, 800)

    def test_midweek_adds_the_visit_fee(self):
        p = plan(restock_15(), off_cycle=True)
        self.assertEqual(p.cost.visit_fee_cents, 2500)
        self.assertEqual((p.cost.expected_cents, p.cost.max_cents), (3500, 3700))

    def test_balance_check_uses_max_plus_fee(self):
        self.assertIn("insufficient_balance", codes(plan(restock_15(), off_cycle=True, balance=3699)))
        self.assertTrue(plan(restock_15(), off_cycle=True, balance=3700).ok)


class PricingGate(unittest.TestCase):
    def test_floor_shows_its_arithmetic(self):
        f = floor_of(plan(restock_15()), "15")
        self.assertEqual(f.floor_cents, 70)
        self.assertEqual([(part.unit_cost, part.floor_cents) for part in f.parts],
                         [(Fraction(50), 55), (Fraction(50), 55), (Fraction(125, 2), 70)])

    def test_closet_cost_counts_toward_the_floor(self):
        p = plan(restock_15(price="1.00"), closet=[ClosetStock(MTDEW, 9, 95)])   # 95c -> $1.05
        self.assertEqual(codes(p), ["below_floor"])
        self.assertEqual(floor_of(p, "15").floor_cents, 105)

    def test_unchanged_price_is_rechecked_when_units_are_bought(self):
        # 60c clears the 55c floor of the stock on hand, but not the 70c
        # floor of the Amazon alternate the worker might buy.
        slots = {"15": SlotState("15", MTDEW, 60, 4, 50)}
        p = plan(restock_15(price="0.60"), slots=slots)
        self.assertEqual(codes(p), ["below_floor"])
        self.assertIn("$0.70", p.refusals[0].message)
        self.assertEqual(p.refusals[0].slot, "15")

    def test_costlier_stock_on_hand_lifts_the_floor(self):
        slots = {"15": SlotState("15", MTDEW, 250, 4, 90)}     # 90c x 1.08 -> $1.00
        p = plan(restock_15(price="0.95"), slots=slots)
        self.assertEqual(codes(p), ["below_floor"])
        self.assertEqual(floor_of(p, "15").floor_cents, 100)

    def test_unknown_on_hand_cost_is_a_warning(self):
        slots = {"15": SlotState("15", MTDEW, 250, 4, None)}
        p = plan(restock_15(), slots=slots)
        self.assertTrue(p.ok, p.refusals)
        self.assertEqual([part.source for part in floor_of(p, "15").parts][0], "primary BJs 24-pack, max $12.00")
        self.assertTrue(any("unknown" in w for w in p.warnings))


class Refusals(unittest.TestCase):
    """One test per refusal code."""

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
        p = plan(pickle_17(), now=THURSDAY_NOON)
        self.assertTrue(p.ok, p.refusals)
        self.assertTrue(any("Amazon cutoff" in w for w in p.warnings))
        self.assertTrue(plan(pickle_17(), now=WEDNESDAY_LATE).ok)

    def test_in_store_alternate_counts_too(self):
        given = Sourcing(PICKLE, TABLE[2].primary, PurchaseOption("BJs", 12, "29.99"))
        p = plan(pickle_17(), now=FRIDAY_MIDNIGHT, sourcing=[given])
        self.assertEqual(codes(p), ["past_cutoff"])

    def test_request_pending(self):
        p = plan(restock_15(), pending=[PendingRequest("pr-41")])
        self.assertEqual(codes(p), ["request_pending"])
        self.assertIn("pr-41", p.refusals[0].message)

    def test_slot_list(self):
        targets = [DesiredSlot(s, keep=True) for s in OUR_SLOTS if s != "58"]
        targets += [DesiredSlot("15", keep=True), DesiredSlot("15", keep=True), DesiredSlot("11", keep=True)]
        p = plan_restock(current(), DesiredState(tuple(targets)), CONFIG, TUESDAY)
        self.assertEqual(codes(p), ["slot_list"] * 3)
        self.assertEqual(sorted(r.slot for r in p.refusals), ["11", "15", "58"])

    def test_product_unknown_or_inactive(self):
        p = plan(DesiredSlot("16", product="NOPE", sale_price="2.00"),
                 DesiredSlot("17", product="OLD-SODA", sale_price="2.00"))
        self.assertEqual(codes(p), ["product_unknown", "product_unknown"])

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

    def test_bad_sourcing(self):
        bad = [
            Sourcing("A", PurchaseOption("Target", 12, "6.00")),
            Sourcing("B", PurchaseOption("BJS", 12, "6.00")),           # old spelling
            Sourcing("C", PurchaseOption("Amazon", 12, "7.50")),
            Sourcing("D", PurchaseOption("Amazon", 12, "7.50", url="https://example.com/x")),
            Sourcing("E", PurchaseOption("Walmart", 0, "6.00")),
            Sourcing("F", PurchaseOption("Walmart", 61, "6.00")),
            Sourcing("G", PurchaseOption("Walmart", 24, 34.99)),
            Sourcing("H", PurchaseOption("Walmart", 24, "0.00")),
            Sourcing("I", PurchaseOption("Walmart", 24, "200.05")),
            Sourcing("J", PurchaseOption("Walmart", 24, "6.00", pack_description="x" * 101)),
        ]
        p = plan(restock_15(), sourcing=bad)
        self.assertEqual(codes(p), ["bad_sourcing"] * len(bad))

    def test_sourcing_given_twice(self):
        p = plan(restock_15(), sourcing=[SPRITE_SOURCING, SPRITE_SOURCING])
        self.assertEqual(codes(p), ["bad_sourcing"])

    def test_bad_sourcing_is_not_also_no_sourcing(self):
        bad = Sourcing(SPRITE, PurchaseOption("Target", 12, "6.00"))
        p = plan(DesiredSlot("17", product=SPRITE, sale_price="2.00"), sourcing=[bad])
        self.assertEqual(codes(p), ["bad_sourcing"])

    def test_bad_table_row_counts_only_when_used(self):
        table = TABLE + (Sourcing(SPRITE, PurchaseOption("Amazon", 12, "6.00")),)
        self.assertTrue(plan(restock_15(), table=table).ok)
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

    def test_zero_price_is_below_floor_even_with_no_known_cost(self):
        self.assertEqual(codes(plan(restock_15(price="0.00"))), ["below_floor"])

    def test_above_ceiling(self):
        self.assertEqual(codes(plan(restock_15(price="6.05"))), ["above_ceiling"])

    def test_insufficient_balance(self):
        self.assertEqual(codes(plan(restock_15(), balance=1199)), ["insufficient_balance"])
        self.assertTrue(plan(restock_15(), balance=1200).ok)

    def test_empty_slot(self):
        self.assertEqual(codes(plan(DesiredSlot("15", product=None, sale_price="2.50"))), ["empty_slot"])

    def test_midweek_no_op(self):
        self.assertEqual(codes(plan(off_cycle=True)), ["midweek_no_op"])

    def test_every_refusal_at_once(self):
        p = plan(
            DesiredSlot("15", product=None, sale_price="2.50"),
            DesiredSlot("16", product="NOPE", sale_price="2.00"),
            DesiredSlot("17", product=PICKLE, sale_price="3.03", fill_to=6),
            DesiredSlot("18", product=MTDEW, sale_price="2.50", fill_to=12),
            DesiredSlot("25", product=SPRITE, sale_price="2.00"),
            DesiredSlot("26", product=PICKLE, sale_price="3.00", fill_to=6),
            DesiredSlot("27", product=COKE, sale_price="2.10", fill_to=10),
            pending=[PendingRequest("pr-41")])
        self.assertEqual(sorted(codes(p)), sorted([
            "empty_slot", "product_unknown", "over_capacity", "no_sourcing",
            "request_pending", "price_off_step", "price_only_unsupported"]))
        self.assertIsNone(p.plan_hash)
        # Lines, costs and floors still come back for the preview.
        self.assertEqual(line(p, "26").purchase.primary.packs, 1)
        self.assertEqual(floor_of(p, "26").floor_cents, 270)


class Request(unittest.TestCase):
    """The body for POST /api/v1/purchase-requests, per the 29 Oct schema."""

    def test_purchase_line(self):
        self.assertEqual(plan(restock_15()).request(), {"off_cycle": False, "lines": [{
            "slot": "15", "source": "purchase",
            "primary": {"sku": MTDEW, "store": "BJs", "packs": 1, "units_per_pack": 24,
                        "max_price_per_pack": "12.00", "sale_price": "2.50",
                        "change_product": False},
            "alternate": {"sku": MTDEW, "store": "Amazon", "packs": 1, "units_per_pack": 12,
                          "max_price_per_pack": "7.50", "sale_price": "2.50",
                          "change_product": False, "url": MTDEW_URL},
        }]})

    def test_back_stock_line(self):
        p = plan(pickle_17(), closet=[ClosetStock(PICKLE, 6, 250)], off_cycle=True)
        self.assertEqual(p.request(), {"off_cycle": True, "lines": [{
            "slot": "17", "source": "back_stock",
            "load": {"sku": PICKLE, "units": 6, "sale_price": "3.00", "change_product": True},
        }]})

    def test_only_slots_that_load_get_a_line(self):
        p = plan(restock_15(), pickle_17())
        self.assertEqual([ln["slot"] for ln in p.request()["lines"]], ["15", "17"])

    def test_pack_description_is_passed_on(self):
        given = Sourcing(MTDEW, PurchaseOption("BJs", 24, "12.00", pack_description="24-pack, 20 oz bottles"))
        body = plan(restock_15(), sourcing=[given]).request()
        self.assertEqual(body["lines"][0]["primary"]["pack_description"], "24-pack, 20 oz bottles")


class PlanHash(unittest.TestCase):
    def test_same_plan_same_hash_whenever_computed(self):
        a = plan(restock_15(), now=TUESDAY)
        b = plan(restock_15(), now=WEDNESDAY_LATE)
        self.assertIsNotNone(a.plan_hash)
        self.assertEqual(a.plan_hash, b.plan_hash)

    def test_different_plan_different_hash(self):
        self.assertNotEqual(plan(restock_15()).plan_hash, plan(restock_15(price="2.55")).plan_hash)
        self.assertNotEqual(plan(restock_15()).plan_hash, plan(restock_15(), off_cycle=True).plan_hash)


class Directive(unittest.TestCase):
    def test_worker_table_and_shopping_list(self):
        d = plan(restock_15(), pickle_17(), closet=[ClosetStock(PICKLE, 6, 250)]).directive
        self.assertIn("Visit: scheduled", d)
        self.assertIn("back stock", d)
        self.assertIn("10 COKE-20OZ to closet", d)
        shopping = d.split("Shopping list", 1)[1]
        self.assertIn("BJs\n  MTDEW-20OZ: 1 x 24-pack, max $12.00 a pack, for slot 15 (18 to closet)",
                      shopping)
        self.assertIn("only if BJs is unavailable or over its max", shopping)
        self.assertIn(MTDEW_URL, shopping)


class Inputs(unittest.TestCase):
    def test_naive_now_is_an_error(self):
        with self.assertRaises(ValueError):
            plan(now=datetime(2026, 10, 20, 12, 0))

    def test_off_cycle_must_be_a_bool(self):
        with self.assertRaises(ValueError):
            DesiredState((), off_cycle="yes")

    def test_ceiling_cannot_pass_the_api_limit(self):
        with self.assertRaises(ValueError):
            Config(slot_capacity=10, price_ceiling_cents=1005)

    def test_our_slots(self):
        self.assertEqual(OUR_SLOTS, ("15", "16", "17", "18", "25", "26", "27", "28", "35", "36",
                                     "37", "38", "45", "46", "47", "48", "55", "56", "57", "58"))


if __name__ == "__main__":
    unittest.main()
