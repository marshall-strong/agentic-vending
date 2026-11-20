"""Tests for turning receipts and polls into stock. Synthetic receipts only; no database."""

import pytest
from datetime import datetime, timedelta, timezone
from fractions import Fraction

from vending.stock import closet_from_receipts, movements_from_receipts, replay, sales_from_counters, first_in_first_out

T0 = datetime(2026, 11, 7, 15, 0, tzinfo=timezone.utc)


def hours(n):
    return T0 + timedelta(hours=n)


def line(slot="15", sku="COKE20", outcome="primary", total_paid="12.00", units_loaded=0, units_lost=0,
         units_to_back_stock=0, units_from_back_stock=0, units_pulled=0, pulled_sku=None,
         slot_reassigned=False, at=0, request_id="r1", line_no=0):
    """One fulfilled line as `stock()` reads it from the database."""
    return {
        "request_id": request_id, "line_no": line_no, "slot": slot, "outcome": outcome, "sku": sku,
        "total_paid": total_paid, "units_loaded": units_loaded, "units_lost": units_lost,
        "units_to_back_stock": units_to_back_stock, "units_from_back_stock": units_from_back_stock,
        "units_pulled": units_pulled, "pulled_sku": pulled_sku, "slot_reassigned": slot_reassigned,
        "resolved_at": hours(at),
    }


def loss(location, units, sku="COKE20", slot="15", at=1, request_id="r1", line_no=0):
    return {"request_id": request_id, "line_no": line_no, "slot": slot, "location": location,
            "sku": sku, "units": units, "recorded_at": hours(at)}


def book(lines, losses=(), counters=()):
    return first_in_first_out(movements_from_receipts(list(lines), list(losses)) + sales_from_counters(list(counters)))


def test_one_broken_unit_spreads_the_pack_over_the_rest():
    # A 12-pack at $12.00, one broken on the way: the other 11 cost $12.00 between them.
    stock = book([line(units_loaded=8, units_lost=1, units_to_back_stock=3)])
    [slot] = stock.slots
    [closet] = stock.closet
    assert (slot.units, slot.unit_cost) == (8, Fraction(1200, 11))
    assert (closet.product, closet.units, closet.unit_cost) == ("COKE20", 3, Fraction(1200, 11))
    assert stock.warnings == ()


def test_back_stock_load_carries_closet_cost_and_its_own_losses():
    stock = book([
        line(units_loaded=12, units_to_back_stock=12, total_paid="12.00"),          # 50 cents each
        line(slot="16", outcome="loaded", total_paid=None, units_loaded=5,
             units_from_back_stock=6, at=2, request_id="r2"),                       # 1 dropped
    ])
    assert [(c.units, c.unit_cost) for c in stock.closet] == [(6, 50)]
    slot16 = next(s for s in stock.slots if s.slot == "16")
    assert (slot16.units, slot16.unit_cost) == (5, 60)   # six units' cost on the five that made it


def test_pulled_units_go_to_the_closet_at_their_slot_cost():
    stock = book(
        [
            line(units_loaded=10, total_paid="5.00"),                               # 50 cents each
            line(sku="PEPSI20", units_loaded=10, total_paid="8.00", units_pulled=6, pulled_sku="COKE20",
                 slot_reassigned=True, at=48, request_id="r2"),
        ],
        counters=[("15", hours(-1), 100), ("15", hours(24), 104)],                  # 4 sold
    )
    [slot] = stock.slots
    assert (slot.product, slot.units, slot.unit_cost, slot.loaded, slot.sold) == ("PEPSI20", 10, 80, 10, 0)
    assert slot.since == hours(48)
    assert [(c.product, c.units, c.unit_cost) for c in stock.closet] == [("COKE20", 6, 50)]
    assert stock.warnings == ()


def test_the_closet_from_receipts_alone_has_every_unit_but_no_guessed_pull_cost():
    """Without counters (no assignment to read them by) the closet's units are exact. A pulled product's cost
    depended on the slot's sales, so it is unknown; a product only bought into the closet keeps its cost."""
    lines = [
        line(units_loaded=10, total_paid="5.00"),                                   # 50 cents each
        line(slot="16", sku="WATER", units_loaded=0, units_to_back_stock=4, total_paid="2.00", request_id="r3"),
        line(sku="PEPSI20", units_loaded=10, total_paid="8.00", units_pulled=6, pulled_sku="COKE20",
             slot_reassigned=True, at=48, request_id="r2"),
    ]
    sold = book(lines, counters=[("15", hours(-1), 100), ("15", hours(24), 104)])
    alone = closet_from_receipts(lines, [])
    assert [(c.product, c.units) for c in alone] == [(c.product, c.units) for c in sold.closet]
    assert [(c.product, c.units, c.unit_cost) for c in alone] == [("COKE20", 6, None), ("WATER", 4, 50)]


def test_a_top_up_keeps_old_and_new_cost_apart_and_averages_them_for_unit_cost():
    stock = book(
        [line(units_loaded=10, total_paid="5.00"),                                  # 10 at 50
         line(units_loaded=10, total_paid="8.00", at=48, request_id="r2")],         # 10 at 80
        counters=[("15", hours(-1), 0), ("15", hours(24), 6)],                      # 6 sold before the top-up
    )
    [slot] = stock.slots
    # 4 left at 50 (200) + 10 at 80 (800) = 1000 over 14.
    assert (slot.units, slot.unit_cost, slot.loaded, slot.sold) == (14, Fraction(1000, 14), 20, 6)
    assert slot.layers == ((4, 50), (10, 80))


def test_a_sale_in_the_window_of_a_top_up_is_costed_at_the_stock_that_was_there():
    """#172 item 2. 4 on hand at $1, 4 more loaded at $2, and the next poll shows 4 sold: the 4 that
    sold are the $1 ones (first in, first out), not 4 at the $1.50 average."""
    stock = book(
        [line(units_loaded=4, total_paid="4.00"),                                   # 4 at 100
         line(units_loaded=4, total_paid="8.00", at=24, request_id="r2")],          # 4 at 200
        counters=[("15", hours(1), 100), ("15", hours(30), 104)],                   # the window holds the top-up
    )
    [sale] = stock.sales
    assert (sale.units, sale.costed_units, sale.cost_cents) == (4, 4, 400)
    [slot] = stock.slots
    assert (slot.units, slot.unit_cost, slot.loaded, slot.sold) == (4, 200, 8, 4)


def test_sales_drain_the_oldest_units_first():
    stock = book(
        [line(units_loaded=4, total_paid="4.00"),                                   # 4 at 100
         line(units_loaded=4, total_paid="8.00", at=24, request_id="r2")],          # 4 at 200
        counters=[("15", hours(1), 100), ("15", hours(30), 102), ("15", hours(40), 105)],
    )
    # 2 of the $1 units, then the last 2 of them and 1 at $2.
    assert [(s.units, s.cost_cents) for s in stock.sales] == [(2, 200), (3, 400)]
    [slot] = stock.slots
    assert (slot.units, slot.unit_cost) == (3, 200)


def test_a_loss_takes_the_oldest_units():
    stock = book(
        [line(units_loaded=4, total_paid="4.00"),                                   # 4 at 100
         line(units_loaded=4, total_paid="8.00", at=24, request_id="r2")],          # 4 at 200
        [loss("coil", 2, at=30)],
    )
    [x] = stock.shrinkage
    assert (x.units, x.cost_cents) == (2, 200)
    [slot] = stock.slots
    assert (slot.units, slot.unit_cost) == (6, Fraction(1000, 6))


def test_pulled_units_keep_their_layers_and_a_later_load_takes_the_oldest():
    stock = book([
        line(units_loaded=4, total_paid="4.00"),                                    # 4 at 100
        line(units_loaded=4, total_paid="8.00", at=24, request_id="r2"),            # 4 at 200
        line(sku="PEPSI20", units_loaded=10, total_paid="8.00", units_pulled=8, pulled_sku="COKE20",
             slot_reassigned=True, at=48, request_id="r3"),
        line(slot="16", outcome="loaded", total_paid=None, units_loaded=4,
             units_from_back_stock=4, at=50, request_id="r4"),
    ])
    slot16 = next(s for s in stock.slots if s.slot == "16")
    assert (slot16.units, slot16.unit_cost) == (4, 100)
    assert [(c.product, c.units, c.unit_cost) for c in stock.closet] == [("COKE20", 4, 200)]


def test_a_back_stock_load_takes_the_oldest_closet_units():
    stock = book([
        line(units_loaded=0, units_to_back_stock=4, total_paid="2.00"),             # 4 at 50 in the closet
        line(units_loaded=0, units_to_back_stock=4, total_paid="4.00", at=2, request_id="r2"),   # 4 at 100
        line(slot="16", outcome="loaded", total_paid=None, units_loaded=3,
             units_from_back_stock=3, at=4, request_id="r3"),
    ])
    slot16 = next(s for s in stock.slots if s.slot == "16")
    assert (slot16.units, slot16.unit_cost) == (3, 50)
    [closet] = stock.closet
    assert (closet.units, closet.unit_cost) == (5, Fraction(450, 5))   # 1 at 50 and 4 at 100


def test_later_losses_come_out_where_they_were_found_as_shrinkage():
    stock = book(
        [line(units_loaded=10, units_to_back_stock=2, total_paid="6.00")],          # 50 cents each
        [loss("coil", 2, at=5), loss("back_stock", 1, at=6)],
    )
    [slot] = stock.slots
    [closet] = stock.closet
    # The lost units take their own cost with them; the rest still cost 50 each.
    assert (slot.units, slot.lost, slot.unit_cost) == (8, 2, 50)
    assert (closet.units, closet.unit_cost) == (1, 50)
    assert [(x.at, x.where, x.product, x.units, x.cost_cents) for x in stock.shrinkage] == [
        (hours(5), "slot 15", "COKE20", 2, 100), (hours(6), "the closet", "COKE20", 1, 50)]
    # Every cent of the $6.00 is accounted for: on hand, or written off.
    assert slot.units * slot.unit_cost + closet.units * closet.unit_cost + sum(x.cost_cents for x in stock.shrinkage) == 600


def test_units_still_counted_when_the_product_changes_are_shrinkage():
    stock = book([line(units_loaded=10, total_paid="5.00"),
                  line(sku="PEPSI20", units_loaded=10, total_paid="8.00", slot_reassigned=True, at=48, request_id="r2")],
                 counters=[("15", hours(-1), 0), ("15", hours(24), 7)])
    [x] = stock.shrinkage
    assert (x.at, x.where, x.product, x.units, x.cost_cents) == (hours(48), "slot 15", "COKE20", 3, 150)
    assert any("written off as shrinkage" in w for w in stock.warnings)


def test_a_line_that_lost_every_unit_is_shrinkage_and_leaves_the_closet_cost_known():
    stock = book([line(units_loaded=0, units_to_back_stock=6, total_paid="3.00"),              # 50 each
                  line(units_loaded=0, units_lost=12, total_paid="6.00", at=2, request_id="r2")])  # all broken
    assert [(c.units, c.unit_cost) for c in stock.closet] == [(6, 50)]
    [x] = stock.shrinkage
    assert (x.units, x.cost_cents) == (12, 600)


def test_rereading_the_same_losses_gives_the_same_count():
    lines = [line(units_loaded=10, total_paid="5.00")]
    losses = [loss("coil", 2)]
    assert book(lines, losses) == book(lines, losses)


def test_coil_loss_after_the_product_changed_is_ignored_with_a_warning():
    stock = book(
        [line(units_loaded=10, total_paid="5.00"),
         line(sku="PEPSI20", units_loaded=10, total_paid="8.00", slot_reassigned=True, at=48, request_id="r2")],
        [loss("coil", 1, sku="COKE20", at=50)],
    )
    assert stock.slots[0].units == 10
    assert any("no longer holds it" in w for w in stock.warnings)


def test_sales_from_counters_skip_a_counter_that_goes_down():
    moves = sales_from_counters([
        ("15", hours(0), 10), ("15", hours(1), 12), ("15", hours(2), 3), ("15", hours(3), 5), ("16", hours(1), 7),
    ])
    assert [(m.slot, m.at, m.units) for m in moves] == [("15", hours(1), 2), ("15", hours(3), 2)]


def test_sales_from_counters_ignore_one_misread_between_good_polls():
    moves = sales_from_counters([("15", hours(0), 100000), ("15", hours(1), 500), ("15", hours(2), 100250)])
    assert [(m.at, m.units) for m in moves] == [(hours(2), 250)]


def test_sales_before_we_first_load_a_slot_are_not_ours():
    stock = book([line(units_loaded=10, total_paid="5.00", at=5)],
                 counters=[("15", hours(0), 0), ("15", hours(1), 3), ("15", hours(6), 5)])
    # Only the 2 sold after the load (between hours 1 and 6) count.
    assert (stock.slots[0].units, stock.slots[0].sold) == (8, 2)


def test_more_sales_than_loaded_count_as_zero_on_hand_with_a_warning():
    stock = book([line(units_loaded=3, total_paid="1.50")],
                 counters=[("15", hours(-1), 0), ("15", hours(5), 5)])
    assert (stock.slots[0].units, stock.slots[0].unit_cost, stock.slots[0].sold) == (0, None, 5)
    assert any("2 more sales" in w for w in stock.warnings)


def test_pulling_a_product_we_never_loaded_leaves_its_cost_unknown():
    stock = book([line(sku="PEPSI20", units_loaded=10, total_paid="8.00", units_pulled=4,
                       pulled_sku="OLDCOLA", slot_reassigned=True)])
    assert [(c.product, c.units, c.unit_cost) for c in stock.closet] == [("OLDCOLA", 4, None)]
    assert any("cost is unknown" in w for w in stock.warnings)


def test_unknown_cost_clears_once_those_units_are_gone():
    stock = book(
        [line(sku="PEPSI20", units_loaded=10, total_paid="8.00", units_pulled=4,
              pulled_sku="OLDCOLA", slot_reassigned=True),
         line(slot="16", sku="OLDCOLA", outcome="loaded", total_paid=None, units_loaded=4,
              units_from_back_stock=4, at=24, request_id="r2"),
         line(slot="17", sku="OLDCOLA", units_loaded=0, units_to_back_stock=6, total_paid="3.00",
              at=48, request_id="r3")],
    )
    assert [(c.product, c.units, c.unit_cost) for c in stock.closet] == [("OLDCOLA", 6, 50)]


def test_unavailable_moves_nothing():
    stock = book([line(outcome="unavailable", sku=None, total_paid=None)])
    assert stock.slots == () and stock.closet == ()


def test_loading_more_than_the_closet_holds_warns_and_leaves_cost_unknown():
    stock = book([line(outcome="loaded", total_paid=None, units_loaded=5, units_from_back_stock=5)])
    assert (stock.slots[0].units, stock.slots[0].unit_cost) == (5, None)
    assert any("counted only 0 there" in w for w in stock.warnings)


def test_a_product_change_the_receipt_does_not_flag_is_warned_about():
    stock = book([line(units_loaded=10, total_paid="5.00"),
                  line(sku="PEPSI20", units_loaded=10, total_paid="8.00", at=48, request_id="r2")])
    assert stock.slots[0].product == "PEPSI20"
    assert any("slot_reassigned is false" in w for w in stock.warnings)


# --- A poll window that spans a product change (#97) ------------------------------------
# David's case: slot 15 sells an Arizona at $2.00 in the morning, the worker swaps in
# Red Bull at $4.00, and it sells one in the afternoon. The next poll shows 2 units and
# 600 cents more, at today's $4.00 price. Rows are (slot, polled_at, units, cents, price).

def swap(sku="REDBULL", total_paid="15.00", at=3):
    """Arizona (10 at 50 cents) at hour 0; at `at` the worker pulls what is left and loads 10 of `sku`."""
    return [line(sku="ARIZONA", units_loaded=10, total_paid="5.00"),
            line(sku=sku, units_loaded=10, total_paid=total_paid, units_pulled=9, pulled_sku="ARIZONA",
                 slot_reassigned=True, at=at, request_id="r2")]


def test_sales_across_a_product_change_are_split_by_the_cents_counter():
    stock = book(swap(), counters=[("15", hours(1), 100, 20000, 200), ("15", hours(5), 102, 20600, 400)])
    # One Arizona, sold before the swap at its own cost; one Red Bull after it at its own.
    # Both are booked to the hour-5 poll that measured them; the Arizona left its count at the swap.
    assert [(s.at, s.taken_at, s.product, s.units, s.cost_cents) for s in stock.sales] == [
        (hours(5), hours(3), "ARIZONA", 1, 50), (hours(5), None, "REDBULL", 1, 150)]
    assert [s.unattributed for s in stock.sales] == ["", ""]
    [slot] = stock.slots
    assert (slot.product, slot.units, slot.sold, slot.unattributed) == ("REDBULL", 9, 1, 0)
    # The 9 pulled are all that was left, so nothing is written off.
    assert [(c.product, c.units, c.unit_cost) for c in stock.closet] == [("ARIZONA", 9, 50)]
    assert stock.shrinkage == () and stock.warnings == ()


def test_sales_across_a_change_the_counters_cannot_split_keep_their_cost_unattributed():
    # Same price both sides: the counters can't say which product sold.
    same = book(swap(sku="COKE20", total_paid="8.00"),
                counters=[("15", hours(1), 100, 20000, 200), ("15", hours(5), 102, 20400, 200)])
    # Cents that fit no whole split (2 units, 500 cents, at $2.00 and $4.00).
    odd = book(swap(), counters=[("15", hours(1), 100, 20000, 200), ("15", hours(5), 102, 20500, 400)])
    # Their product is uncertain, so no cost_cents, but what they took from the new count stays booked (#112).
    for stock, taken in ((same, 160), (odd, 300)):
        [sale] = stock.sales
        assert (sale.at, sale.units, sale.cost_cents, sale.unattributed_cost_cents) == (hours(5), 2, None, taken)
        assert any("can't split" in w and "may be the wrong product's" in w for w in stock.warnings)
        # #172: carried as unknown, the new product's neither, with the reason.
        assert (sale.product, sale.costed_units) == (None, 2) and "can't split" in sale.unattributed
    [slot] = same.slots
    assert (slot.product, slot.loaded, slot.sold, slot.unattributed, slot.units) == ("COKE20", 10, 0, 2, 8)


def test_an_unsplit_window_keeps_the_books_whole():
    # Marshall's check on #112: 10 Arizona at 50c and 10 Red Bull at 150c bought (2000c);
    # cost of sales + shrinkage + slot + closet must add back to 2000, as on main.
    stock = book(swap(), counters=[("15", hours(1), 100, 20000, 200), ("15", hours(5), 102, 20500, 400)])
    sold = sum((s.cost_cents if s.cost_cents is not None else s.unattributed_cost_cents) for s in stock.sales)
    lost = sum(x.cost_cents for x in stock.shrinkage)
    on_hand = sum(c.units * c.unit_cost for c in stock.slots) + sum(c.units * c.unit_cost for c in stock.closet)
    assert (sold, lost, on_hand) == (300, 50, 1650)
    assert sold + lost + on_hand == 2000


def test_an_unsplit_window_is_estimated_at_the_midpoint_only_when_both_candidate_costs_are_known():
    # #172 item 5: the 2 units are an Arizona (the 50c unit written off at the swap) or a
    # Red Bull (the 150c units they were taken from), so the estimate is the midpoint, 100c each.
    odd = [("15", hours(1), 100, 20000, 200), ("15", hours(5), 102, 20500, 400)]
    [sale] = book(swap(), counters=odd).sales
    assert (sale.product, sale.cost_cents, sale.estimated_cost_cents) == (None, None, (50 + 150) / 2 * 2)
    # The new product's cost unknown (no total_paid): no estimate.
    [sale] = book(swap(total_paid=None), counters=odd).sales
    assert (sale.cost_cents, sale.estimated_cost_cents) == (None, None)
    # The old product's cost unknown: no estimate either.
    old_unknown = [line(sku="ARIZONA", units_loaded=10, total_paid=None), swap()[1]]
    [sale] = book(old_unknown, counters=odd).sales
    assert (sale.cost_cents, sale.estimated_cost_cents) == (None, None)
    # A sale the counters split has an exact cost and no estimate.
    split = book(swap(), counters=[("15", hours(1), 100, 20000, 200), ("15", hours(5), 102, 20600, 400)])
    assert [(s.cost_cents, s.estimated_cost_cents) for s in split.sales] == [(50, None), (150, None)]


def test_a_poll_during_the_visit_starts_a_window_of_only_the_new_product():
    # Marshall's case: Arizona at $2.50 swapped for Red Bull at $3.50. The price is set during
    # the visit, a poll lands at 2h45m with the new price, and the receipt resolves at 3h.
    # The next poll shows 2 units and 700 cents: both Red Bull, from the new count.
    counters = [("15", hours(1), 100, 20000, 250), ("15", hours(2.75), 100, 20000, 350),
                ("15", hours(5), 102, 20700, 350)]
    stock = book(swap(), counters=counters)
    assert [(s.product, s.units, s.cost_cents) for s in stock.sales] == [("REDBULL", 2, 300)]
    # No Arizona sold, so the 1 of 10 not pulled is a real write-off, and nothing is unsplit.
    assert not any("can't split" in w for w in stock.warnings)
    assert [(x.product, x.units) for x in stock.shrinkage] == [("ARIZONA", 1)]


@pytest.mark.parametrize("counters", [
    # No price change before the window: a same-price swap, still unsplittable (#110 item 7).
    [("15", hours(1), 100, 20000, 350), ("15", hours(2.75), 100, 20000, 350), ("15", hours(5), 102, 20700, 350)],
    # The price changed before the window, but the swap came long after its first poll.
    [("15", hours(-2), 100, 20000, 250), ("15", hours(1), 100, 20000, 350), ("15", hours(5), 102, 20700, 350)],
])
def test_a_same_price_window_is_read_as_new_only_when_its_first_poll_was_in_the_visit(counters):
    stock = book(swap(), counters=counters)
    [sale] = [s for s in stock.sales if s.units]
    assert sale.cost_cents is None and any("can't split" in w for w in stock.warnings)


def test_sales_in_a_window_with_no_change_are_not_split():
    stock = book(swap(), counters=[("15", hours(4), 100, 20000, 400), ("15", hours(5), 102, 20800, 400)])
    assert [(s.product, s.units, s.cost_cents) for s in stock.sales] == [("REDBULL", 2, 300)]


def test_stock_as_of_a_moment_between_the_swap_and_the_next_poll_splits_before_it_cuts():
    # The ledger asks for stock as of a period's end. If that end falls between the swap
    # (hour 3) and the poll that measures the window (hour 5), the old product's share
    # still comes out of its count at the swap instead of being written off as shrinkage.
    moves = movements_from_receipts(swap(), []) + sales_from_counters(
        [("15", hours(1), 100, 20000, 200), ("15", hours(5), 102, 20600, 400)])
    cut = replay(moves, until=hours(4))
    assert cut.shrinkage == () and cut.warnings == ()
    [slot] = cut.slots
    assert (slot.product, slot.units) == ("REDBULL", 10)
    # Both shares are booked to the poll that measured them, like the takings, so no sale
    # is booked before hour 5, and the replay to hour 5 books both.
    assert cut.sales == ()
    full = replay(moves)
    assert [(s.at, s.taken_at, s.product, s.units, s.cost_cents) for s in full.sales] == [
        (hours(5), hours(3), "ARIZONA", 1, 50), (hours(5), None, "REDBULL", 1, 150)]


def test_a_cents_counter_that_did_not_rise_with_the_units_cannot_split_them():
    # The cents counter misread low at hour 5, so there is no cents rise to split with.
    # At an old price of $0 a missing rise read as 0 cents would book both units old.
    stock = book(swap(), counters=[("15", hours(1), 100, 20000, 0), ("15", hours(5), 102, 100, 400)])
    [sale] = stock.sales
    assert (sale.product, sale.units, sale.cost_cents) == (None, 2, None)
    assert any("can't split" in w for w in stock.warnings)


def test_a_window_the_counters_cannot_split_says_the_write_off_may_be_those_sales():
    stock = book(swap(), counters=[("15", hours(1), 100, 20000, 200), ("15", hours(5), 102, 20500, 400)])
    # 10 Arizona counted, 9 pulled: the 1 written off may be one of the 2 sold in the window.
    assert [(x.product, x.units) for x in stock.shrinkage] == [("ARIZONA", 1)]
    assert any("can't split" in w and "1 units of ARIZONA" in w and "may be" in w for w in stock.warnings)


def test_a_split_that_finds_more_old_units_sold_than_counted_is_warned_about():
    # 1 Arizona loaded and sold by hour 2; the counters then split 1 more Arizona to before the swap.
    lines = [line(sku="ARIZONA", units_loaded=1, total_paid="0.50"),
             line(sku="REDBULL", units_loaded=10, total_paid="15.00", slot_reassigned=True, at=3, request_id="r2")]
    stock = book(lines, counters=[("15", hours(1), 100, 20000, 200), ("15", hours(2), 101, 20200, 200),
                                  ("15", hours(5), 103, 20800, 400)])
    assert any("1 units of ARIZONA" in w and "counted only 0" in w for w in stock.warnings)


def test_a_window_across_two_changes_says_so():
    lines = swap() + [line(sku="PEPSI20", units_loaded=10, total_paid="8.00", units_pulled=10, pulled_sku="REDBULL",
                           slot_reassigned=True, at=4, request_id="r3")]
    stock = book(lines, counters=[("15", hours(1), 100, 20000, 200), ("15", hours(5), 102, 20600, 250)])
    assert any("can't split" in w and "2 product changes" in w for w in stock.warnings)


# Door times (17 Nov): the receipt says when the worker opened and closed the door. The old
# product ends at door open and the new one starts at door close. Without them, the line's
# resolved_at stands in, as above.

def with_door(lines, opened=2.5, closed=3.5, request_id="r2"):
    """The same lines, with door times on `request_id`'s lines (the swap), as `stock()` reads them."""
    return [{**l, "door_opened_at": hours(opened), "door_closed_at": hours(closed)} if l["request_id"] == request_id
            else l for l in lines]


def test_with_door_times_a_window_across_the_visit_is_split_at_open_and_close():
    stock = book(with_door(swap()), counters=[("15", hours(1), 100, 20000, 200), ("15", hours(5), 102, 20600, 400)])
    # The Arizona left its count at door open, the Red Bull went in at door close.
    assert [(s.at, s.taken_at, s.product, s.units, s.cost_cents) for s in stock.sales] == [
        (hours(5), hours(2.5), "ARIZONA", 1, 50), (hours(5), None, "REDBULL", 1, 150)]
    assert [s.unattributed for s in stock.sales] == ["", ""]
    [slot] = stock.slots
    assert (slot.product, slot.units, slot.since, slot.sold, slot.unattributed) == ("REDBULL", 9, hours(3.5), 1, 0)
    assert stock.shrinkage == () and stock.warnings == ()


def test_with_door_times_a_window_that_starts_during_the_visit_is_all_new_with_no_guess():
    # The same-price counters that can't be split without door times (above): a poll at 2h45m,
    # inside the visit, with $3.50 at both ends of the next window and no price change before it.
    counters = [("15", hours(1), 100, 20000, 350), ("15", hours(2.75), 100, 20000, 350),
                ("15", hours(5), 102, 20700, 350)]
    stock = book(with_door(swap()), counters=counters)
    assert [(s.product, s.units, s.cost_cents) for s in stock.sales if s.units] == [("REDBULL", 2, 300)]
    assert not any("can't split" in w for w in stock.warnings)


def test_with_door_times_a_window_that_ends_during_the_visit_is_all_old():
    # A poll at 3h, between door open and close, after 1 sale at $2.00 and before any new price.
    counters = [("15", hours(1), 100, 20000, 200), ("15", hours(3), 101, 20200, 200),
                ("15", hours(5), 101, 20200, 400)]
    stock = book(with_door(swap()), counters=counters)
    assert [(s.at, s.taken_at, s.product, s.units, s.cost_cents) for s in stock.sales if s.units] == [
        (hours(3), hours(2.5), "ARIZONA", 1, 50)]
    # 10 loaded, 1 sold, 9 pulled: nothing written off.
    assert stock.shrinkage == () and stock.warnings == ()


def test_with_door_times_a_same_price_window_across_the_whole_visit_still_cannot_be_split():
    stock = book(with_door(swap(sku="COKE20", total_paid="8.00")),
                 counters=[("15", hours(1), 100, 20000, 200), ("15", hours(5), 102, 20400, 200)])
    [sale] = stock.sales
    assert (sale.units, sale.cost_cents, sale.unattributed_cost_cents) == (2, None, 160)
    assert any("can't split" in w for w in stock.warnings)
    assert (sale.product, sale.costed_units) == (None, 2) and "can't split" in sale.unattributed
    [slot] = stock.slots
    assert (slot.product, slot.loaded, slot.sold, slot.unattributed, slot.units) == ("COKE20", 10, 0, 2, 8)


def test_a_line_resolved_before_the_door_opened_is_timed_by_the_door():
    # On pr_v8hB_h3gQatc one line was filled in 2 minutes before the door opened. Here the
    # worker fills in the swap at 2h, opens the door at 2.5h and closes it at 3.5h. A poll at
    # 2h15m shows 1 more sale at $2.00: an Arizona, sold before the door opened.
    counters = [("15", hours(1), 100, 20000, 200), ("15", hours(2.25), 101, 20200, 200),
                ("15", hours(5), 102, 20600, 400)]
    stock = book(with_door(swap(at=2)), counters=counters)
    assert [(s.product, s.units, s.cost_cents) for s in stock.sales] == [("ARIZONA", 1, 50), ("REDBULL", 1, 150)]
    assert stock.warnings == ()
    # Without door times the swap is at 2h, so that Arizona sale lands after it.
    guessed = book(swap(at=2), counters=counters)
    assert any("can't split" in w for w in guessed.warnings)


def summary(stock):
    """A Stock as plain values, times as hours after T0, so main's numbers can be written down."""
    h = lambda t: None if t is None else (t - T0) / timedelta(hours=1)
    return {
        "sales": [(h(s.at), h(s.taken_at), s.product, s.units, s.costed_units, s.cost_cents, s.unattributed_cost_cents)
                  for s in stock.sales],
        "slots": [(s.slot, s.product, s.units, s.unit_cost, s.loaded, s.sold, s.lost, h(s.since), h(s.last_moved))
                  for s in stock.slots],
        "closet": [(c.product, c.units, c.unit_cost) for c in stock.closet],
        "shrinkage": [(h(x.at), x.where, x.product, x.units, x.cost_cents) for x in stock.shrinkage],
        "warnings": list(stock.warnings),
    }


# What main (9523764, before door times) gives for `swap()` with these counters, worked out
# by running main's vending/stock.py on them. Null door times must give exactly this.
_WRITTEN_OFF = "r2 line 0: 1 units of ARIZONA still counted in slot 15 when it changed to REDBULL; written off as shrinkage"
_UNSPLIT = ("slot 15: 2 units sold between the polls at {} and 2026-11-07T20:00:00+00:00, across the change from "
            "ARIZONA to REDBULL; the counters can't split them, so they are costed at the oldest units taken from "
            "REDBULL's count, which may be the wrong product's; the 1 units of ARIZONA written off as shrinkage at the "
            "change may be some of these sales, not losses")
_MAIN_SLOT_AFTER_2_SOLD = [("15", "REDBULL", 8, 150, 10, 2, 0, 3.0, 3.0)]
# Where main can't split the window, #172 changes one thing: its 2 units are unattributed, in neither
# product's sales and not in REDBULL's sold. Every other figure is main's.
_MAIN_UNSPLIT_SALE = [(5.0, None, None, 2, 2, None, 300)]
_MAIN_UNSPLIT_SLOT = [("15", "REDBULL", 8, 150, 10, 0, 0, 3.0, 3.0)]
_MAIN_SHRINKAGE = [(3.0, "slot 15", "ARIZONA", 1, 50)]


@pytest.mark.parametrize("counters, main", [
    ([("15", hours(1), 100, 20000, 200), ("15", hours(5), 102, 20600, 400)],
     {"sales": [(5.0, 3.0, "ARIZONA", 1, 1, 50, None), (5.0, None, "REDBULL", 1, 1, 150, None)],
      "slots": [("15", "REDBULL", 9, 150, 10, 1, 0, 3.0, 3.0)], "closet": [("ARIZONA", 9, 50)],
      "shrinkage": [], "warnings": []}),
    ([("15", hours(1), 100, 20000, 250), ("15", hours(2.75), 100, 20000, 350), ("15", hours(5), 102, 20700, 350)],
     {"sales": [(5.0, None, "REDBULL", 2, 2, 300, None)], "slots": _MAIN_SLOT_AFTER_2_SOLD,
      "closet": [("ARIZONA", 9, 50)], "shrinkage": _MAIN_SHRINKAGE, "warnings": [_WRITTEN_OFF]}),
    ([("15", hours(1), 100, 20000, 350), ("15", hours(2.75), 100, 20000, 350), ("15", hours(5), 102, 20700, 350)],
     {"sales": _MAIN_UNSPLIT_SALE, "slots": _MAIN_UNSPLIT_SLOT,
      "closet": [("ARIZONA", 9, 50)], "shrinkage": _MAIN_SHRINKAGE,
      "warnings": [_WRITTEN_OFF, _UNSPLIT.format("2026-11-07T17:45:00+00:00")]}),
    ([("15", hours(1), 100, 20000, 200), ("15", hours(5), 102, 20500, 400)],
     {"sales": _MAIN_UNSPLIT_SALE, "slots": _MAIN_UNSPLIT_SLOT,
      "closet": [("ARIZONA", 9, 50)], "shrinkage": _MAIN_SHRINKAGE,
      "warnings": [_WRITTEN_OFF, _UNSPLIT.format("2026-11-07T16:00:00+00:00")]}),
])
def test_null_door_times_give_mains_answer(counters, main):
    # Older requests, or a worker who left them out: exactly what main gave before door times.
    # Submitted in the opposite order to the visits: without door times that must change nothing.
    sent = {"r1": hours(1), "r2": hours(-1)}
    nulls = [{**l, "door_opened_at": None, "door_closed_at": None, "submitted_at": sent[l["request_id"]]}
             for l in swap()]
    assert summary(book(nulls, counters=counters)) == main


def test_without_door_times_every_pull_at_one_moment_goes_before_any_load_as_on_main():
    # r3, submitted before r2, loads 5 Arizona from the closet at the same resolved_at as r2's
    # pull of 9 Arizona. As on main, the pull goes first: the load takes the pulled units at 50c.
    sent = {"r1": hours(-2), "r3": hours(-1), "r2": hours(0)}
    lines = swap() + [line(slot="16", sku="ARIZONA", outcome="loaded", total_paid=None, units_loaded=5,
                           units_from_back_stock=5, at=3, request_id="r3")]
    stock = book([{**l, "door_opened_at": None, "door_closed_at": None, "submitted_at": sent[l["request_id"]]}
                  for l in lines])
    assert [(c.product, c.units, c.unit_cost) for c in stock.closet] == [("ARIZONA", 4, 50)]
    slot16 = next(s for s in stock.slots if s.slot == "16")
    assert (slot16.product, slot16.units, slot16.unit_cost) == ("ARIZONA", 5, 50)


def test_a_door_timed_pull_goes_before_a_closet_load_without_door_times_at_the_same_moment():
    # r2 pulls 9 Arizona at its door open, 17:30; r3, with no door times, loads 5 Arizona from
    # the closet into slot 16 at the same instant. As on main, the pull goes first.
    sent = {"r1": hours(-2), "r2": hours(-1), "r3": hours(0)}
    lines = with_door(swap()) + [line(slot="16", sku="ARIZONA", outcome="loaded", total_paid=None, units_loaded=5,
                                      units_from_back_stock=5, at=2.5, request_id="r3")]
    stock = book([{**l, "submitted_at": sent[l["request_id"]]} for l in lines])
    assert [(c.product, c.units, c.unit_cost) for c in stock.closet] == [("ARIZONA", 4, 50)]
    slot16 = next(s for s in stock.slots if s.slot == "16")
    assert (slot16.product, slot16.units, slot16.unit_cost) == ("ARIZONA", 5, 50)


def test_a_pull_goes_before_a_load_at_one_minute_even_from_a_later_submitted_visit():
    # r3, submitted first, loads 5 Arizona from the closet into slot 16, its door closing at
    # 17:30. r2, submitted later, opens slot 15 at 17:30 and pulls 9 Arizona. Different slots, so
    # submission order doesn't decide: the pull goes first and the load takes the pulled units.
    sent = {"r1": hours(-2), "r3": hours(-1), "r2": hours(0)}
    load = with_door([line(slot="16", sku="ARIZONA", outcome="loaded", total_paid=None, units_loaded=5,
                           units_from_back_stock=5, at=2.6, request_id="r3")], opened=2, closed=2.5, request_id="r3")
    stock = book([{**l, "submitted_at": sent[l["request_id"]]} for l in with_door(swap()) + load])
    assert [(c.product, c.units, c.unit_cost) for c in stock.closet] == [("ARIZONA", 4, 50)]
    slot16 = next(s for s in stock.slots if s.slot == "16")
    assert (slot16.product, slot16.units, slot16.unit_cost) == ("ARIZONA", 5, 50)


def test_visits_sharing_a_minute_are_found_by_scanning_in_door_open_order():
    from itertools import combinations

    from vending.stock import _MINUTE, _sharing_a_minute

    # Nested, chained and separate visits: the scan finds exactly the pairs every-pair-against-every-pair finds.
    visits = {"a": (hours(1), hours(4), None), "b": (hours(1.5), hours(2), None), "c": (hours(3), hours(3.5), None),
              "d": (hours(4), hours(4.5), None), "e": (hours(6), hours(7), None), "f": (hours(7), hours(7), None)}
    every = [((p, pv), (q, qv)) for (p, pv), (q, qv) in combinations(sorted(visits.items()), 2)
             if pv[0] < qv[1] + _MINUTE and qv[0] < pv[1] + _MINUTE]
    assert _sharing_a_minute(visits) == every
    assert [(p, q) for (p, _), (q, _) in every] == [("a", "b"), ("a", "c"), ("a", "d"), ("e", "f")]


def test_door_times_split_once_however_often_the_replay_splits():
    moves = movements_from_receipts(with_door(swap()), []) + sales_from_counters(
        [("15", hours(1), 100, 20000, 200), ("15", hours(5), 102, 20600, 400)])
    assert replay(moves) == first_in_first_out(moves)
    cut = replay(moves, until=hours(4))
    assert cut.shrinkage == () and cut.warnings == () and cut.sales == ()


# The door times are to the minute, and we don't know whether they are truncated or rounded.
# Each is read as a minute: the door opened in [17:30, 17:31) and closed in [18:30, 18:31) for
# `with_door(swap())`. A poll inside either minute can't say which side of the door it was.

def seconds(h, s):
    return hours(h) + timedelta(seconds=s)


def test_a_window_starting_in_the_open_minute_is_solved_by_the_counters_not_read_as_all_new():
    # A poll at 17:30:20 may have come before the door opened at, say, 17:30:50, and an
    # Arizona can sell in between. The counters say it did: 1 unit at $2.00, 1 at $4.00.
    counters = [("15", hours(1), 100, 20000, 200), ("15", seconds(2.5, 20), 100, 20000, 200),
                ("15", hours(5), 102, 20600, 400)]
    stock = book(with_door(swap()), counters=counters)
    assert [(s.taken_at, s.product, s.units, s.cost_cents) for s in stock.sales] == [
        (hours(2.5), "ARIZONA", 1, 50), (None, "REDBULL", 1, 150)]
    assert stock.warnings == ()
    # With the same price on both sides the counters can't say, so it is left unattributed, as on main.
    same = book(with_door(swap(sku="COKE20", total_paid="8.00")),
                counters=[(s, t, u, c, 200) for s, t, u, c, _ in counters[:2]] + [("15", hours(5), 102, 20400, 200)])
    [sale] = same.sales
    assert (sale.units, sale.cost_cents, sale.unattributed_cost_cents) == (2, None, 160)
    assert any("can't split" in w for w in same.warnings)


def test_a_window_starting_after_the_open_minute_is_all_new():
    # At 17:31 the door had opened, so nothing after it is the Arizona, whatever the counters say.
    counters = [("15", hours(1), 100, 20000, 200), ("15", seconds(2.5, 60), 100, 20000, 200),
                ("15", hours(5), 102, 20400, 200)]
    stock = book(with_door(swap()), counters=counters)
    assert [(s.product, s.units, s.cost_cents) for s in stock.sales] == [("REDBULL", 2, 300)]
    assert not any("can't split" in w for w in stock.warnings)


def test_a_window_ending_in_the_close_minute_is_solved_by_the_counters():
    # A poll at 18:30:30 may have come before or after the door closed in that minute, so a
    # window from before the visit to it may hold either product. The counters decide.
    counters = [("15", hours(1), 100, 20000, 200), ("15", seconds(3.5, 30), 102, 20600, 400),
                ("15", hours(5), 102, 20600, 400)]
    stock = book(with_door(swap()), counters=counters)
    assert [(s.taken_at, s.product, s.units) for s in stock.sales] == [(hours(2.5), "ARIZONA", 1), (None, "REDBULL", 1)]
    same = book(with_door(swap(sku="COKE20", total_paid="8.00")),
                counters=[("15", hours(1), 100, 20000, 200), ("15", seconds(3.5, 30), 102, 20400, 200)])
    [sale] = same.sales
    assert sale.cost_cents is None and any("can't split" in w for w in same.warnings)


def test_a_window_ending_by_the_start_of_the_close_minute_is_all_old():
    # By 18:30:00 the door had not closed, so no Red Bull could have sold yet.
    counters = [("15", hours(1), 100, 20000, 200), ("15", hours(3.5), 101, 20200, 200),
                ("15", hours(5), 101, 20200, 400)]
    stock = book(with_door(swap()), counters=counters)
    assert [(s.at, s.taken_at, s.product, s.units) for s in stock.sales] == [(hours(3.5), hours(2.5), "ARIZONA", 1)]
    assert stock.warnings == ()


# Ties (17 Nov review): a visit inside one recorded minute, and two visits sharing a minute.

def test_a_visit_inside_one_minute_pulls_before_it_loads_and_the_counters_settle_a_poll_in_it():
    # The door opened and closed at 17:30. A poll at 17:30:30 already shows the new $4.00 price
    # and one more sale for $2.00: an Arizona, before the visit. The next window starts with the
    # new price at both ends, after the price was set, so it is all Red Bull.
    counters = [("15", hours(1), 100, 20000, 200), ("15", seconds(2.5, 30), 101, 20200, 400),
                ("15", hours(5), 102, 20600, 400)]
    stock = book(with_door(swap(), opened=2.5, closed=2.5), counters=counters)
    assert [(s.at, s.taken_at, s.product, s.units, s.cost_cents) for s in stock.sales] == [
        (seconds(2.5, 30), hours(2.5), "ARIZONA", 1, 50), (hours(5), None, "REDBULL", 1, 150)]
    # The 9 pulled are what the Arizona sale left: the pull came after it and before the load.
    assert [(c.product, c.units) for c in stock.closet] == [("ARIZONA", 9)]
    assert stock.shrinkage == () and stock.warnings == ()


def two_visits(first=(2.5, 3), second=(3, 3.5), sent=(1, 2)):
    """Arizona at hour 0; r2 swaps it for Red Bull, then r3 swaps that for Pepsi, each with door times.

    `sent` is when each of r2 and r3 was submitted, in hours (None: unknown). Their
    resolved_at, 2.9h and 3.4h, are in the same order as the visits.
    """
    lines = [line(sku="ARIZONA", units_loaded=10, total_paid="5.00"),
             line(sku="REDBULL", units_loaded=10, total_paid="15.00", units_pulled=10, pulled_sku="ARIZONA",
                  slot_reassigned=True, at=2.9, request_id="r2"),
             line(sku="PEPSI20", units_loaded=10, total_paid="10.00", units_pulled=10, pulled_sku="REDBULL",
                  slot_reassigned=True, at=3.4, request_id="r3")]
    for request_id, (opened, closed), at in (("r2", first, sent[0]), ("r3", second, sent[1])):
        lines = [{**l, "submitted_at": None if at is None else hours(at)} if l["request_id"] == request_id else l
                 for l in with_door(lines, opened, closed, request_id)]
    return lines


def test_two_visits_sharing_a_minute_go_in_the_order_they_were_submitted():
    # r2's door closed at 18:00 and r3's opened at 18:00. r2 was submitted first, so its Red Bull
    # goes in before r3 pulls it out, though r3's pull is timed at the same minute as r2's load.
    stock = book(two_visits())
    assert [(c.product, c.units, c.unit_cost) for c in stock.closet] == [("ARIZONA", 10, 50), ("REDBULL", 10, 150)]
    [slot] = stock.slots
    assert (slot.product, slot.units, slot.since) == ("PEPSI20", 10, hours(3.5))
    assert stock.shrinkage == () and stock.warnings == ()


@pytest.mark.parametrize("sent", [(None, None), (1, 1), (2, 1)])
def test_two_visits_sharing_a_minute_whose_order_cant_be_told_fall_back_to_resolved_at(sent):
    # Submitted together, unknown, or in the opposite order to the door times: the door times
    # don't place these visits, so both are timed by resolved_at as before, with a warning.
    stock = book(two_visits(sent=sent))
    assert [(c.product, c.units) for c in stock.closet] == [("ARIZONA", 10), ("REDBULL", 10)]
    [slot] = stock.slots
    assert slot.since == hours(3.4)   # r3's resolved_at, not its door close
    assert sorted(w.split(":")[0] for w in stock.warnings if "can't be told" in w) == ["r2", "r3"]
    assert not any("never loaded" in w for w in stock.warnings)


def test_a_clash_warning_names_its_own_slot_when_the_request_first_moved_another_slot():
    # r3's first line loads slot 14 (say, a former slot); its clash with r2 is in slot 15. The
    # dashboard keeps a warning only for our slots, by its `slot`, so it must say 15.
    lines = two_visits(sent=(1, 1))
    r3 = next(l for l in lines if l["request_id"] == "r3")
    lines.insert(lines.index(r3), {**r3, "slot": "14", "line_no": 1, "sku": "COKE20", "total_paid": "2.00",
                                   "units_loaded": 2, "units_pulled": 0, "pulled_sku": None,
                                   "slot_reassigned": False})
    stock = book(lines)
    clashes = [w for w in stock.warnings if "can't be told" in w]
    assert sorted((w.split(":")[0], w.slot) for w in clashes) == [("r2", "15"), ("r3", "15")]


def test_two_visits_that_overlap_fall_back_to_resolved_at_even_in_submission_order():
    stock = book(two_visits(first=(2.5, 3.25), second=(3, 3.5)))
    assert sum("can't be told" in w for w in stock.warnings) == 2
    assert stock.slots[0].since == hours(3.4)


# Eligibility (17 Nov review): which products a window could hold, given the door was open.

def test_a_sale_while_the_door_was_open_is_flagged_and_left_unattributed():
    # Polls at 17:45 and 18:15 both fall while the receipt says the door was open (opened by
    # 17:31, closed no earlier than 18:30), yet the counter rose. That contradicts the receipt.
    counters = [("15", hours(1), 100, 20000, 200), ("15", hours(2.75), 100, 20000, 200),
                ("15", hours(3.25), 101, 20200, 200), ("15", hours(5), 101, 20200, 400)]
    stock = book(with_door(swap()), counters=counters)
    [sale] = [s for s in stock.sales if s.units]
    assert (sale.at, sale.units, sale.cost_cents) == (hours(3.25), 1, None)
    assert any("r2's door times say the door was open" in w and "contradicts the receipt" in w
               and "costed at the oldest units taken from" in w for w in stock.warnings)


def test_a_window_from_inside_one_visit_to_inside_the_next_is_the_first_visits_product():
    # r2 open 17:30-17:50 (Red Bull in), r3 open 18:10-18:30 (Pepsi in). A window from 17:40 to
    # 18:20 starts and ends with the door open, so only the Red Bull, in between, can have sold.
    lines = two_visits(first=(2.5, 2 + 50 / 60), second=(3 + 10 / 60, 3.5))
    lines = [{**l, "units_pulled": 9} if l["request_id"] == "r3" else l for l in lines]
    counters = [("15", hours(1), 100, 20000, 200), ("15", hours(2 + 40 / 60), 100, 20000, 200),
                ("15", hours(3 + 20 / 60), 101, 20300, 300), ("15", hours(5), 101, 20300, 300)]
    stock = book(lines, counters=counters)
    assert [(s.at, s.taken_at, s.product, s.units, s.cost_cents) for s in stock.sales] == [
        (hours(3 + 20 / 60), hours(3 + 10 / 60), "REDBULL", 1, 150)]
    assert stock.shrinkage == () and stock.warnings == ()
    # From inside r2's open minute instead, the Arizona may have sold too: two products, two
    # changes, so the window is left unattributed.
    counters[1] = ("15", seconds(2.5, 30), 100, 20000, 200)
    stock = book(lines, counters=counters)
    [sale] = [s for s in stock.sales if s.units]
    assert sale.cost_cents is None and any("can't split" in w and "2 product changes" in w for w in stock.warnings)


# Every slot at our visit (#172 item 1). A window between two polls that the counters and the door
# times can't split is carried as unknown: not booked as the new product's sales, nor the old one's.

def test_a_poll_in_the_open_minute_after_an_earlier_price_change_is_not_read_as_all_new():
    # r0 tops up the Arizona at 16:35-16:40 and drops its price from $2.50 to $2.00. r2 opens the
    # door in the 17:30 minute to swap in Coke at the same $2.00. The poll at 17:30:10 shows $2.00:
    # r0's price, not a sign that r2 had started, so the door may have opened at 17:30:50, and an
    # Arizona can sell in between. Same price on both sides: the counters can't say which sold.
    lines = with_door([line(sku="ARIZONA", units_loaded=2, total_paid="1.00", at=1.6, request_id="r0")],
                      opened=1 + 35 / 60, closed=1 + 40 / 60, request_id="r0")
    lines = lines + with_door(swap(sku="COKE20", total_paid="8.00"))
    counters = [("15", hours(1), 100, 25000, 250), ("15", seconds(2.5, 10), 100, 25000, 200),
                ("15", hours(4), 102, 25400, 200)]
    stock = book(lines, counters=counters)
    [sale] = [s for s in stock.sales if s.units]
    assert (sale.product, sale.units) == (None, 2)
    # Main read it as all Coke and costed it at 80 cents in cost_cents; that cost stays, so the money
    # series still counts it as the ledger does (#172 fix 5 reports such estimates apart).
    assert (sale.cost_cents, sale.unattributed_cost_cents) == (160, None)
    [slot] = stock.slots
    assert (slot.product, slot.sold, slot.unattributed) == ("COKE20", 0, 2)
    # Without r0 the price change at 17:30:10 can only be r2's, so the door was open: all Coke, as before.
    alone = book(with_door(swap(sku="COKE20", total_paid="8.00")), counters=counters)
    assert [(s.product, s.units, s.unattributed) for s in alone.sales if s.units] == [("COKE20", 2, "")]


def test_confirmed_sales_after_unattributed_ones_lower_the_most_that_can_be_on_hand():
    # 10 Coke loaded at a same-price swap; 8 sold in the window across it (unattributed), then 6 more,
    # all Coke. If the 8 were Arizonas, 10 - 6 = 4 Coke are left; if they were Coke, none are.
    stock = book(swap(sku="COKE20", total_paid="8.00"),
                 counters=[("15", hours(1), 100, 20000, 200), ("15", hours(5), 108, 21600, 200),
                           ("15", hours(8), 114, 22800, 200)])
    [slot] = stock.slots
    assert (slot.loaded, slot.sold, slot.unattributed, slot.units, slot.at_most) == (10, 6, 8, 0, 4)


def test_visits_whose_door_times_the_replay_drops_still_count_as_setting_the_price():
    # As the test above, but r0's top-up is two requests, r0 and r1, whose door times share the 16:40
    # minute and were submitted together. The replay times their lines by resolved_at (15:30, before
    # the 16:00 poll), but the doors were open between the 16:00 and 17:30:10 polls, so either may have
    # set the $2.00 price: the 17:30:10 poll doesn't show r2 had started.
    lines = [line(sku="ARIZONA", units_loaded=10, total_paid="5.00")]
    for request_id, opened, closed in (("r0", 1 + 35 / 60, 1 + 40 / 60), ("r1", 1 + 40 / 60, 1 + 42 / 60)):
        lines = with_door(lines + [line(sku="ARIZONA", units_loaded=1, total_paid="0.50", at=0.5,
                                        request_id=request_id)], opened=opened, closed=closed, request_id=request_id)
    lines = lines + with_door([swap(sku="COKE20", total_paid="8.00")[1]])
    counters = [("15", hours(1), 100, 25000, 250), ("15", seconds(2.5, 10), 100, 25000, 200),
                ("15", hours(4), 102, 25400, 200)]
    stock = book(lines, counters=counters)
    assert any("share a minute" in w for w in stock.warnings)
    [sale] = [s for s in stock.sales if s.units]
    assert (sale.product, sale.units) == (None, 2) and "can't split" in sale.unattributed


def _unknown_then_two_dollars(**loaded):
    """Slot 17 holds 2 OLDCOLA: 1 of unknown cost (pulled from a slot we never loaded with it), then 1 at $2."""
    return [
        line(sku="PEPSI20", units_loaded=10, total_paid="8.00", units_pulled=1, pulled_sku="OLDCOLA",
             slot_reassigned=True),
        line(slot="16", sku="OLDCOLA", units_loaded=0, units_to_back_stock=1, total_paid="2.00", at=1,
             request_id="r2"),
        line(slot="17", sku="OLDCOLA", outcome="loaded", total_paid=None, units_loaded=2,
             units_from_back_stock=2, at=2, request_id="r3"),
    ]


@pytest.mark.parametrize("counters", [
    [("17", hours(3), 0), ("17", hours(5), 2)],                          # both in one poll
    [("17", hours(3), 0), ("17", hours(4), 1), ("17", hours(5), 2)],     # one per poll
])
def test_a_sale_across_an_unknown_and_a_known_unit_books_the_known_cost_however_it_is_polled(counters):
    stock = book(_unknown_then_two_dollars(), counters=counters)
    sales = [s for s in stock.sales if s.slot == "17"]
    assert sum(s.cost_cents or 0 for s in sales) == 200
    assert sum(s.costed_units for s in sales if s.cost_cents is not None) == 1   # the other unit's cost is unknown


def test_mixed_cost_shrinkage_and_inventory_keep_the_known_amount_and_say_they_are_incomplete():
    from vending.ledger import _inventory
    held = book(_unknown_then_two_dollars())
    slot17 = next(s for s in held.slots if s.slot == "17")
    assert slot17.unit_cost is None                                    # no mean over a partly unknown pool
    assert _inventory(held) == (Fraction(800 + 200), None, False)   # slot 15's PEPSI20, and the known unit
    lost = book(_unknown_then_two_dollars(), [loss("coil", 2, sku="OLDCOLA", slot="17", at=3, request_id="r3")])
    assert sum(x.units for x in lost.shrinkage) == 2
    assert sum(x.cost_cents for x in lost.shrinkage if x.cost_cents is not None) == 200
    assert any(x.cost_cents is None for x in lost.shrinkage)


def test_a_back_stock_load_past_the_closet_count_keeps_the_known_units_cost():
    stock = book([
        line(units_loaded=0, units_to_back_stock=4, total_paid="4.00"),               # 4 at 100 in the closet
        line(slot="16", outcome="loaded", total_paid=None, units_loaded=5,
             units_from_back_stock=5, at=2, request_id="r2"),                         # 5 loaded from 4 counted
    ])
    slot16 = next(s for s in stock.slots if s.slot == "16")
    assert (slot16.units, slot16.unit_cost) == (5, None)
    assert slot16.layers == ((4, 100), (1, None))
    assert any("counted only 4 there" in w for w in stock.warnings)


def test_a_back_stock_load_with_a_loss_keeps_the_dearer_layer_apart():
    """2 at $1 then 2 at $3 drawn, 1 lost: the loss comes out of the oldest units, and its $1 is spread
    over the 3 left, so the $3 units stay the last and dearest, and the $8 is all still there."""
    stock = book([
        line(units_loaded=0, units_to_back_stock=2, total_paid="2.00"),               # 2 at 100
        line(units_loaded=0, units_to_back_stock=2, total_paid="6.00", at=1, request_id="r2"),   # 2 at 300
        line(slot="16", outcome="loaded", total_paid=None, units_loaded=3,
             units_from_back_stock=4, at=2, request_id="r3"),                         # 1 dropped
    ])
    slot16 = next(s for s in stock.slots if s.slot == "16")
    assert slot16.layers == ((1, Fraction(400, 3)), (2, Fraction(1000, 3)))
    assert sum(units * cost for units, cost in slot16.layers) == 800


def _known_cents(stock):
    """Known cost in inventory, shrinkage and cost of sales: what conservation compares with the known cost paid."""
    from vending.ledger import _inventory
    machine, closet, _ = _inventory(stock)
    return ((machine or 0) + (closet or 0) + sum(x.cost_cents or 0 for x in stock.shrinkage)
            + sum((s.cost_cents or 0) + (s.unattributed_cost_cents or 0) for s in stock.sales))


@pytest.mark.parametrize("loaded, drawn", [(1, 2), (0, 1)])
def test_a_load_loss_no_known_unit_survives_to_carry_is_shrinkage(loaded, drawn):
    """The closet holds 1 unit at $2; the load draws `drawn` (any past the count come in unknown) and the
    oldest, the $2 one, is dropped. No unit of known cost made it to carry the $2, so it is shrinkage."""
    stock = book([
        line(units_loaded=0, units_to_back_stock=1, total_paid="2.00"),              # 1 at 200 in the closet
        line(slot="16", outcome="loaded", total_paid=None, units_loaded=loaded,
             units_from_back_stock=drawn, at=2, request_id="r2"),
    ])
    assert [(x.units, x.cost_cents) for x in stock.shrinkage if x.cost_cents is not None] == [(1, 200)]
    assert _known_cents(stock) == 200


def test_a_load_loss_of_a_zero_cost_unit_no_known_unit_survives_to_carry_is_still_shrinkage():
    """The closet holds 1 unit at $0 (free stock); the load drops it and nothing makes it into the slot.
    It is still a unit gone without a sale, so it is booked as shrinkage at $0, not lost from the books."""
    stock = book([
        line(units_loaded=0, units_to_back_stock=1, total_paid="0.00"),              # 1 at 0 in the closet
        line(slot="16", outcome="loaded", total_paid=None, units_loaded=0,
             units_from_back_stock=1, at=2, request_id="r2"),
    ])
    assert [(x.units, x.cost_cents) for x in stock.shrinkage] == [(1, 0)]


def test_a_load_loss_of_an_unknown_cost_unit_is_shrinkage_even_when_a_known_unit_survives():
    """The closet holds 1 unit of unknown cost, then 1 at $2; the load draws both and drops the oldest, the
    unknown one. The $2 unit makes it, but has no known loss to carry: the dropped unit is shrinkage of unknown
    cost, so the P&L says shrinkage is incomplete instead of leaving the unit out."""
    stock = book([
        line(units_loaded=0, units_to_back_stock=1, total_paid=None),                # 1 unknown in the closet
        line(units_loaded=0, units_to_back_stock=1, total_paid="2.00", at=1, request_id="r1b"),  # then 1 at 200
        line(slot="16", outcome="loaded", total_paid=None, units_loaded=1,
             units_from_back_stock=2, at=2, request_id="r2"),
    ])
    assert [(x.units, x.cost_cents) for x in stock.shrinkage] == [(1, None)]
    assert _known_cents(stock) == 200


def test_a_pull_past_the_count_brings_the_extra_units_in_at_unknown_cost():
    """Slot 17 counts 1 unknown OLDCOLA, then 1 at $2; 3 are pulled. The two counted move at their own cost;
    the third has no layer, so its cost is unknown rather than another $2."""
    stock = book(_unknown_then_two_dollars() + [
        line(slot="17", sku="WATER", units_loaded=1, total_paid="1.00", units_pulled=3, pulled_sku="OLDCOLA",
             slot_reassigned=True, at=3, request_id="r4"),
    ])
    [cola] = [c for c in stock.closet if c.product == "OLDCOLA"]
    assert cola.layers == ((1, None), (1, 200), (1, None))
    assert _known_cents(stock) == 800 + 200 + 100
    assert any("counted only 2" in w for w in stock.warnings)


def test_unattributed_sales_say_they_are_costed_at_the_oldest_units_taken():
    stock = book(swap(), counters=[("15", hours(1), 100, 20000, 200), ("15", hours(5), 102, 20600, 250)])
    unsplit = [w for w in stock.warnings if "can't split" in w]
    assert unsplit and all("oldest units taken from" in w and "average" not in w for w in unsplit)


# A read taken while the door is open (#172). The counters are cumulative and read only when polled, so
# when a product swap keeps the price, a unit sold between the last read before the visit and the first
# one after it can't be told apart. No unit sells with the door open, so a read inside the visit puts
# everything before it with the old product and everything after it with the new one. The receipt's
# door times prove the read fell inside the visit. 15:00 is T0, door open 15:12, close 15:31.

def minutes(m):
    return T0 + timedelta(minutes=m)


def same_price_swap():
    """ARIZONA (10 at 50c) loaded at 14:00. The worker opens the door at 15:12, pulls the 8 left, loads 10 REDBULL
    (150c each) and closes the door at 15:31. Both sell at $2.00."""
    return with_door([line(sku="ARIZONA", units_loaded=10, total_paid="5.00", at=-1),
                      line(sku="REDBULL", units_loaded=10, total_paid="15.00", units_pulled=8, pulled_sku="ARIZONA",
                           slot_reassigned=True, at=0.4, request_id="r2")], opened=12 / 60, closed=31 / 60)


# The lifetime units at each minute from T0. One unit sells at 15:05 and one at 15:11, before the door
# opens; one at 15:35 and one at 15:45, after it closes. None sells with the door open.
UNITS = {-60: 100, -10: 100, 0: 100, 10: 101, 20: 102, 30: 102, 40: 103, 50: 104, 60: 104}


def polled_at(*at_minutes):
    return [("15", minutes(m), UNITS[m], UNITS[m] * 200, 200) for m in at_minutes]


def attributed(stock):
    return sorted((s.product, s.units, s.cost_cents, s.unattributed_cost_cents) for s in stock.sales)


def test_a_read_inside_the_visit_attributes_every_unit_where_hourly_polls_cannot():
    every_10_minutes = book(same_price_swap(), counters=polled_at(-10, 0, 10, 20, 30, 40, 50, 60))
    # The 15:20 and 15:30 reads fall while the door is open: the two sales before it are the Arizona the worker
    # pulled, the two after it the Red Bull that was loaded. Nothing is unknown, and the books balance.
    assert attributed(every_10_minutes) == [("ARIZONA", 1, 50, None), ("ARIZONA", 1, 50, None),
                                            ("REDBULL", 1, 150, None), ("REDBULL", 1, 150, None)]
    assert every_10_minutes.warnings == () and every_10_minutes.shrinkage == ()
    [slot] = every_10_minutes.slots
    assert (slot.product, slot.units, slot.sold) == ("REDBULL", 8, 2)
    assert [(c.product, c.units) for c in every_10_minutes.closet] == [("ARIZONA", 8)]

    # The same sales, polled every 10 minutes but with no read while the door was open: the window from 15:10 to
    # 15:40 holds the whole visit, and at one price on both sides its 2 units can't be told apart.
    no_read_inside = book(same_price_swap(), counters=polled_at(-10, 0, 10, 40, 50, 60))
    # The hourly polls: all 4 units are one window across the visit.
    hourly = book(same_price_swap(), counters=polled_at(-60, 0, 60))
    for stock, unknown in ((no_read_inside, 2), (hourly, 4)):
        assert sum(s.units for s in stock.sales if s.cost_cents is None) == unknown
        assert any("the counters can't split them" in w for w in stock.warnings)


def test_a_sale_carries_the_poll_its_window_was_measured_from():
    """`SaleCost.since`: the window is (since, at], the counters' own, for each share of a split window and for
    a window left whole; after a misread it is the good poll before it, not the misread."""
    split = book(swap(), counters=[("15", hours(1), 100, 20000, 200), ("15", hours(5), 102, 20600, 400)])
    assert [(s.product, s.since, s.at) for s in split.sales] == [
        ("ARIZONA", hours(1), hours(5)), ("REDBULL", hours(1), hours(5))]
    whole = book(swap(sku="COKE20", total_paid="8.00"),
                 counters=[("15", hours(1), 100, 20000, 200), ("15", hours(2), 100, 20000, 200),
                           ("15", hours(5), 102, 20400, 200)])
    assert [(s.product, s.since, s.at) for s in whole.sales] == [(None, hours(2), hours(5))]
    misread = book(swap(sku="COKE20", total_paid="8.00"),
                   counters=[("15", hours(1), 100, 20000, 200), ("15", hours(4), 40, 8000, 200),
                             ("15", hours(5), 102, 20400, 200)])
    assert [(s.product, s.since, s.at) for s in misread.sales] == [(None, hours(1), hours(5))]


def test_a_hand_built_sale_keeps_its_window_start_through_a_split():
    """A sale `Movement` made with only `since` (no counters' `window_since`) still reports where its window began,
    for a window left whole and for both shares of a split one."""
    from vending.stock import Movement

    whole = replay(movements_from_receipts([line(units_loaded=10)], []) + [
        Movement(hours(5), "sale", "15", None, units=1, since=hours(2))])
    assert [(s.since, s.at) for s in whole.sales] == [(hours(2), hours(5))]
    sale = Movement(hours(5), "sale", "15", None, units=2, since=hours(1), cents=600, price_before=200, price_after=400)
    split = replay(movements_from_receipts(swap(), []) + [sale])
    assert [(s.product, s.since) for s in split.sales] == [("ARIZONA", hours(1)), ("REDBULL", hours(1))]



def test_movements_keep_their_positional_field_order():
    """`booked_at` is the 19th field: a caller that passes it positionally must not have it land in `window_since`,
    which was added later and comes last."""
    from vending.stock import Movement

    positional = Movement(hours(5), "sale_before_change", "15", "ARIZONA", 1, 0, 0, 0, None, None, False, "",
                          None, None, None, None, None, None, hours(6))
    assert positional.booked_at == hours(6) and positional.window_since is None
