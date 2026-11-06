"""Tests for turning receipts and polls into stock. Synthetic receipts only; no database."""

from datetime import datetime, timedelta, timezone
from fractions import Fraction

from vending.stock import movements_from_receipts, sales_from_counters, weighted_average

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


def test_later_losses_come_out_where_they_were_found():
    stock = book(
        [line(units_loaded=10, units_to_back_stock=2, total_paid="6.00")],          # 50 cents each
        [loss("coil", 2, at=5), loss("back_stock", 1, at=6)],
    )
    [slot] = stock.slots
    [closet] = stock.closet
    assert (slot.units, slot.lost, slot.unit_cost) == (8, 2, Fraction(500, 8))
    assert (closet.units, closet.unit_cost) == (1, 100)


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
