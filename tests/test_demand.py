"""Tests for `vending.demand`: the in-stock rate per class day, and the fall prior before it."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from vending.demand import (OWN_RATE_AFTER, class_days_in_stock, class_days_left, estimate, in_stock_since,
                            in_stock_spans, segment_for)
from vending.stock import Applied

UTC = timezone.utc
FRI = datetime(2026, 12, 4, 20, tzinfo=UTC)        # first Friday visit, 3 PM Eastern


def day(n, hour=16):
    """`n` days after the Friday visit, at `hour` UTC (11 AM Eastern by default)."""
    return (FRI + timedelta(days=n)).replace(hour=hour)


def history(loads, sales, *more):
    """A run's history as the stock replay applies it (`stock.SlotCount.history`): `loads` and `sales` as
    `(at, units)`, plus any other `Applied`, in time order, a load first at one moment."""
    applied = [Applied(at, "load", units) for at, units in loads] + [Applied(at, "sale", units) for at, units in sales]
    return sorted(applied + list(more), key=lambda a: (a.at, a.kind != "load"))


def test_a_slot_that_sells_out_counts_only_the_days_it_held_stock():
    # 8 in on Friday; 3 sold Monday, 3 Tuesday, 2 Wednesday: empty from Wednesday on.
    loads = [(FRI, 8)]
    sales = [(day(3), 3), (day(4), 3), (day(5), 2)]
    spans = in_stock_spans(history(loads, sales), now=day(7))
    assert spans == [(FRI, day(5))]
    days = class_days_in_stock(spans)
    assert len(days) == 3                                           # Mon, Tue, Wed; not Thu or Fri
    rate = estimate(8, len(days), 250)
    assert rate.units_per_class_day == segment_for(250).units_per_class_day   # 3 days: still the prior
    # The old rule would say 8 / 7 = 1.14 a day; the in-stock rate is 8 / 3 = 2.67 once it counts.
    assert estimate(8, 5, 250).units_per_class_day == Decimal("1.60")


def test_a_top_up_after_selling_out_starts_a_new_span():
    loads = [(FRI, 8), (day(7), 8)]                                  # sold out Wednesday, topped up next Friday
    sales = [(day(3), 4), (day(5), 4), (day(10), 2)]
    spans = in_stock_spans(history(loads, sales), now=day(11))
    assert spans == [(FRI, day(5)), (day(7), day(11))]
    days = class_days_in_stock(spans)
    assert len(days) == 3 + 2                                        # Mon-Wed, then Mon-Tue of the next week
    rate = estimate(10, len(days), 250)
    assert (rate.units_per_class_day, rate.class_days_in_stock) == (Decimal("2.00"), 5)
    assert rate.basis == "own: 10 sold over 5 in-stock class days"


def test_a_slot_still_holding_stock_runs_to_now():
    spans = in_stock_spans(history([(FRI, 11)], [(day(3), 2)]), now=day(6))
    assert spans == [(FRI, day(6))]
    assert len(class_days_in_stock(spans)) == 4                      # Mon to Thu
    assert in_stock_since(history([(FRI, 11)], [(day(3), 2)])) == FRI


def test_a_loss_that_empties_the_slot_ends_the_span_when_the_replay_applied_it():
    # 6 sold Monday, the last 2 lost Tuesday. The replay's history has the loss itself, so the span ends there,
    # not at the last sale as when only the count left at the end said the slot was empty.
    run = history([(FRI, 8)], [(day(3), 6)], Applied(day(4), "loss", 2))
    assert in_stock_spans(run, now=day(7)) == [(FRI, day(4))]
    assert in_stock_since(run) is None
    # A pull empties it the same way; a loss at the last moment still leaves no stretch running.
    assert in_stock_spans(history([(FRI, 8)], [], Applied(day(7), "pull", 8)), now=day(7)) == [(FRI, day(7))]
    assert in_stock_since(history([(FRI, 8)], [], Applied(day(7), "pull", 8))) is None


def test_a_count_lowers_the_stock_and_an_empty_one_ends_the_span():
    # A person counts 3 on Monday where the replay says 8: the 3 sold on Tuesday empty it. One that finds it
    # empty ends the span then, and a refill starts a new one.
    assert in_stock_spans(history([(FRI, 8)], [(day(4), 3)], Applied(day(3), "count", 3)), now=day(6)) == [
        (FRI, day(4))]
    run = history([(FRI, 8), (day(6), 4)], [], Applied(day(3), "count", 0))
    assert in_stock_spans(run, now=day(10)) == [(FRI, day(3)), (day(6), day(10))]
    assert in_stock_since(run) == day(6)


def test_overselling_does_not_open_a_span():
    spans = in_stock_spans(history([(FRI, 2)], [(day(3), 2), (day(4), 1)]), now=day(6))
    assert spans == [(FRI, day(3))]


def test_the_prior_is_the_segment_priced_nearest():
    assert segment_for(450).name == "mid seller, premium-priced"
    assert segment_for(250).name == "fast seller"
    assert segment_for(200).name == "slow seller, low-priced"
    assert segment_for(325).name == "slow seller, erratic"


def test_the_prior_says_where_it_came_from():
    rate = estimate(3, 2, 450)
    assert rate.units_per_class_day == Decimal("1.354")
    assert rate.basis.startswith("fall prior: mid seller, premium-priced (average $4.33); 2 in-stock class days")
    assert f"own rate after {OWN_RATE_AFTER}" in rate.basis
    assert estimate(0, 0, None).units_per_class_day is None


def test_the_own_rate_takes_over_at_five_days():
    assert estimate(0, OWN_RATE_AFTER, 450).units_per_class_day == Decimal("0.00")   # really sold nothing
    assert estimate(7, OWN_RATE_AFTER - 1, 450).units_per_class_day == Decimal("1.354")


def test_class_days_left():
    assert class_days_left(8, Decimal("2.00")) == "4.0"
    assert class_days_left(0, Decimal("2.00")) == "0.0"
    assert class_days_left(8, Decimal("0.00")) is None
    assert class_days_left(None, Decimal("2.00")) is None
