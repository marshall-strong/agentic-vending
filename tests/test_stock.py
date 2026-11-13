"""Tests for turning receipts and polls into stock. Synthetic receipts only; no database."""

from datetime import datetime, timedelta, timezone
from fractions import Fraction

from vending.stock import movements_from_receipts, replay, sales_from_counters, weighted_average

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
    return weighted_average(movements_from_receipts(list(lines), list(losses)) + sales_from_counters(list(counters)))


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


def test_a_top_up_blends_old_and_new_cost():
    stock = book(
        [line(units_loaded=10, total_paid="5.00"),                                  # 10 at 50
         line(units_loaded=10, total_paid="8.00", at=48, request_id="r2")],         # 10 at 80
        counters=[("15", hours(-1), 0), ("15", hours(24), 6)],                      # 6 sold before the top-up
    )
    [slot] = stock.slots
    # 4 left at 50 (200) + 10 at 80 (800) = 1000 over 14.
    assert (slot.units, slot.unit_cost, slot.loaded, slot.sold) == (14, Fraction(1000, 14), 20, 6)


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
    [slot] = stock.slots
    assert (slot.product, slot.units, slot.sold) == ("REDBULL", 9, 1)
    # The 9 pulled are all that was left, so nothing is written off.
    assert [(c.product, c.units, c.unit_cost) for c in stock.closet] == [("ARIZONA", 9, 50)]
    assert stock.shrinkage == () and stock.warnings == ()


def test_sales_across_a_change_the_counters_cannot_split_are_booked_with_no_cost():
    # Same price both sides: the counters can't say which product sold.
    same = book(swap(sku="COKE20", total_paid="8.00"),
                counters=[("15", hours(1), 100, 20000, 200), ("15", hours(5), 102, 20400, 200)])
    # Cents that fit no whole split (2 units, 500 cents, at $2.00 and $4.00).
    odd = book(swap(), counters=[("15", hours(1), 100, 20000, 200), ("15", hours(5), 102, 20500, 400)])
    for stock in (same, odd):
        [sale] = stock.sales
        assert (sale.at, sale.units, sale.cost_cents) == (hours(5), 2, None)
        assert any("can't split" in w for w in stock.warnings)


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
    assert (sale.product, sale.units, sale.cost_cents) == ("REDBULL", 2, None)
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
