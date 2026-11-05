"""Rebuild one past week from the history sync's stored polls.

A week is 7 days from midnight Eastern on the given date. Sales come from
`get_sales_history`, so the units, revenue and the polls the week really spans
are worked out exactly as the agent's tool works them out. Prices are read from
the same two polls.

Polls carry price and lifetime counters only. Product, stock on the rail and
unit cost need restock receipts, which aren't synced yet, so a replayed week
has them as `None`. Anything else the polls can't tell us is `None` too, never
zero: a decider must be able to tell "unknown" from "none sold".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from zoneinfo import ZoneInfo

import psycopg

from vending.pricing import to_cents
from vending.slots import SLOTS
from vending.tools import machine_state
from vending.tools.sales_history import get_sales_history

EASTERN = ZoneInfo("America/New_York")
WEEK = timedelta(days=7)
# Below this much poll coverage, a week's rates say more about the gap than the week.
THIN_COVERAGE = timedelta(days=6)


class NoData(Exception):
    """The stored polls can't rebuild the week asked for. The message says why."""


@dataclass(frozen=True)
class SlotWeek:
    """One slot over one week. `None` means unknown, never zero."""
    slot: str
    price_cents: int | None             # at the end of the week (PA1-02)
    units_sold: int | None
    revenue_cents: int | None = None    # tax included
    units_per_day: Decimal | None = None
    price_changed: bool | None = None   # price at the start differs from the end
    product: str | None = None          # needs receipts
    stock: int | None = None            # on the rail at the end of the week; needs receipts
    unit_cost_cents: Fraction | int | None = None  # actual cost of a unit; needs receipts


@dataclass(frozen=True)
class WeekState:
    start: datetime                     # aware; the week is [start, end)
    end: datetime
    slots: tuple[SlotWeek, ...]
    polls: tuple[datetime, datetime] | None = None  # the two polls the week really spans
    balance_cents: int | None = None    # spendable; needs the ledger
    catalog: frozenset[str] | None = None   # active products; None = unknown
    rejected: frozenset[str] = frozenset()  # products whose catalog request was refused
    # Taken midweek: a scheduled (Friday) visit lands after the scoring window,
    # so only a $25 midweek visit can change what sells in it.
    midweek: bool = False
    notes: tuple[str, ...] = field(default=())

    def slot(self, slot: str) -> SlotWeek | None:
        return next((s for s in self.slots if s.slot == slot), None)


def week_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(0), EASTERN)
    return start, start + WEEK


def replay_week(conn: psycopg.Connection, day: date) -> WeekState:
    """The week starting at midnight Eastern on `day`, from stored polls only."""
    start, end = week_bounds(day)
    sales = get_sales_history(conn, start, end, None)
    if not sales.success:
        raise NoData(f"Week of {day:%Y-%m-%d}: {sales.message}")
    first, last = _prices(conn, sales.start), _prices(conn, sales.end)
    days = Decimal((sales.end - sales.start).total_seconds()) / 86400
    slots = []
    for row in sales.data:
        units = row.units
        before, after = first.get(row.slot), last.get(row.slot)
        slots.append(SlotWeek(
            slot=row.slot,
            price_cents=after,
            units_sold=units,
            revenue_cents=None if row.revenue is None else to_cents(row.revenue),
            units_per_day=None if units is None or days <= 0
            else (units / days).quantize(Decimal("0.01"), ROUND_HALF_UP),
            price_changed=None if before is None or after is None else before != after,
        ))
    notes = []
    if sales.end - sales.start < THIN_COVERAGE:
        notes.append(f"The polls cover only {days:.1f} days of this week, so rates are per day over that.")
    if sales.start > start:
        notes.append("No poll before the week starts, so it starts at the first one.")
    notes.append("Product, stock and unit cost are unknown: they need restock receipts, which aren't synced yet.")
    return WeekState(start=start, end=end, slots=tuple(slots), polls=(sales.start, sales.end),
                     notes=tuple(notes))


def _prices(conn: psycopg.Connection, moment: datetime) -> dict[str, int | None]:
    """Each of our slots' price at the last valid poll at or before `moment`."""
    poll = machine_state._poll(conn, "started_at <= %s", moment)
    if poll is None:
        return {}
    return {slot: price for slot, (price, _units) in machine_state._counters(conn, poll[0]).items()
            if slot in SLOTS}
