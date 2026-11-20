"""How fast a slot sells: units per class day while it held stock (#110 item 6, #93).

The rule (`docs/inventory-demand.md`, "The simpler rule, per slot"):

1. **In-stock rate** = units sold since the slot's product went in ÷ the class
   days it held stock in that time. A day the slot sat empty is left out, not
   counted as a day nobody wanted it, and so are weekends, breaks and closures
   (`vending.class_days`).
2. **Until a slot has `OWN_RATE_AFTER` in-stock class days of its own**, use the
   fall rate of the segment whose average price is nearest the slot's price
   (`SEGMENTS`, `docs/restock-policy.md` R3).

Stock comes from the receipts' loads and the counters' sales, cycle by cycle:
the slot holds stock from a load that takes it above zero until the sale that
takes it back to zero. A class day counts when the slot held stock as it began
(`class_days.class_days_after`), so a Friday-afternoon load starts counting on
Monday, and the day a slot sold out counts.

Everything here is pure: no database, no clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from vending.class_days import class_days_after

OWN_RATE_AFTER = 5   # in-stock class days before a slot's own rate replaces the prior (Proposed, #110 item 6)


@dataclass(frozen=True)
class Segment:
    name: str
    units_per_class_day: Decimal
    average_price_cents: int


# Fall 2026, our 20 slots: units per in-stock class day and the average price paid
# (`analysis/REPORT.md`, "Our slots"; `docs/restock-policy.md`, "Sales rates"). The all-40 fall rates by
# price band (`analysis/fall_rates.py`, the report's last section) are advice for people setting starting
# rates; nothing in the code reads them yet.
SEGMENTS = (
    Segment("fast seller", Decimal("1.701"), 263),
    Segment("mid seller, premium-priced", Decimal("1.354"), 433),
    Segment("slow seller, low-priced", Decimal("0.767"), 217),
    Segment("slow seller, erratic", Decimal("0.722"), 301),
)


@dataclass(frozen=True)
class Demand:
    units_per_class_day: Decimal | None   # null when there is neither an own rate nor a price for the prior
    basis: str                            # where the rate came from, in words the agent can quote
    class_days_in_stock: int              # since the product went in
    units_sold: int                       # since the product went in
    class_days_since_sale: int | None = None   # in-stock class days since its last sale; set by the machine state


def segment_for(price_cents: int) -> Segment:
    """The fall segment whose average price is nearest `price_cents` (the first one on a tie)."""
    return min(SEGMENTS, key=lambda s: abs(s.average_price_cents - price_cents))


def in_stock_spans(loads: list[tuple[datetime, int]], sales: list[tuple[datetime, int]],
                   now: datetime, units_now: int | None = None) -> list[tuple[datetime, datetime]]:
    """When the slot held stock: from each load that took it above zero to the sale that emptied it.

    `loads` and `sales` are `(at, units)` for the slot's current product only. A load
    and a sale at the same moment apply the load first. A sale past zero (more sold
    than the receipts loaded) changes nothing here. A span still open runs to `now`,
    unless `units_now` (the stock replay's count, which also takes off losses) says
    the slot is empty: then it ends at the last sale.
    """
    events = sorted([(at, 0, units) for at, units in loads] + [(at, 1, -units) for at, units in sales])
    spans, on_hand, start, last_sale = [], 0, None, None
    for at, kind, change in events:
        if kind == 1:
            last_sale = at
        before, on_hand = on_hand, max(on_hand + change, 0)
        if before == 0 and on_hand > 0:
            start = at
        elif before > 0 and on_hand == 0:
            spans.append((start, at))
            start = None
    if start is not None:
        empty = units_now == 0 and last_sale is not None and last_sale >= start
        spans.append((start, last_sale if empty else now))
    return spans


def run_events(since: datetime, total: int, events: list[tuple[datetime, int]]) -> list[tuple[datetime, int]]:
    """One product run's loads, or its decreases, as `(at, units)`: those after `since`, when the run began, and
    at `since` itself what is left of `total`, the stock replay's own count of them for the run
    (`stock.SlotCount`), kept in its movement order. A timestamp can't say whether something at `since` itself
    was this run's or the one before (A, B, then A again at one visit; a poll at the swap's own moment)."""
    later = [(at, units) for at, units in events if at > since]
    first = total - sum(units for _, units in later)
    return ([(since, first)] if first > 0 else []) + later


def class_days_in_stock(spans: list[tuple[datetime, datetime]]) -> list[date]:
    """The class days the slot held stock as they began, over all spans, each day once."""
    return sorted({day for start, end in spans for day in class_days_after(start, end)})


def estimate(units_sold: int, days_in_stock: int, price_cents: int | None) -> Demand:
    """The slot's own in-stock rate once it has `OWN_RATE_AFTER` class days, else the segment prior."""
    if days_in_stock >= OWN_RATE_AFTER:
        rate = (Decimal(units_sold) / days_in_stock).quantize(Decimal("0.01"), ROUND_HALF_UP)
        return Demand(rate, f"own: {units_sold} sold over {days_in_stock} in-stock class days",
                      days_in_stock, units_sold)
    so_far = f"{days_in_stock} in-stock class day{'' if days_in_stock == 1 else 's'} of our own so far"
    if price_cents is None:
        return Demand(None, f"no price to pick a fall segment, and {so_far}", days_in_stock, units_sold)
    segment = segment_for(price_cents)
    return Demand(segment.units_per_class_day,
                  f"fall prior: {segment.name} (average ${segment.average_price_cents / 100:.2f}); "
                  f"{so_far}, own rate after {OWN_RATE_AFTER}", days_in_stock, units_sold)


def class_days_left(on_hand: int | None, rate: Decimal | None) -> str | None:
    """On hand ÷ units per class day. Null when either is unknown, or the rate is zero."""
    if on_hand is None:
        return None
    if on_hand == 0:
        return "0.0"
    if not rate:
        return None
    return str((Decimal(on_hand) / rate).quantize(Decimal("0.1"), ROUND_HALF_UP))
