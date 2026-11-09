"""`get_machine_state` (tool #1): what each of our slots holds, charges and sells.

Built from our database only; it never reads the machine.

- Price and sales come from the history sync's stored polls. Lifetime counters
  never reset, so sales over the window are the latest poll minus the last poll
  at or before the window's start.
- Product and stock come from the purchase-request receipts, through
  `vending.stock`, which replays every receipt and every poll. `stock_on_hand`
  counts from when the slot's current product went in (loaded − sold − lost),
  so a top-up of the same product adds to it.
- `days_since_loaded` and `units_sold_since_load` count from the slot's most
  recent load: the latest fulfilled receipt line that put units in it.
- The closet (back stock) is listed per product, with its value at cost.

A slot with no receipts yet has `null` for every receipt field: null, not
zero, so the agent can't mistake "unknown" for "empty". The message names
those slots.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction

import psycopg
from pydantic import BaseModel

from vending import stock
from vending.slots import DEVICE_ID, SLOTS
from vending.tools import Reply

WINDOW = timedelta(days=7)
# David polls hourly; older than this means the history sync has likely stalled.
STALE_AFTER = timedelta(hours=3)
RECEIPT_FIELDS = "product, stock_on_hand, units_sold_since_load, days_since_loaded and days_of_stock_left"


class SlotState(BaseModel):
    slot: str
    price: str | None
    lifetime_units: int | None
    units_sold_in_window: int | None
    units_per_day: str | None
    product: str | None = None
    stock_on_hand: int | None = None
    units_sold_since_load: int | None = None
    days_since_loaded: int | None = None
    days_of_stock_left: str | None = None


class ClosetItem(BaseModel):
    product: str
    units: int
    unit_cost: str | None        # dollars; null when some units' cost is unknown
    value_at_cost: str | None    # dollars


class MachineState(Reply):
    as_of: datetime | None = None
    stale: bool = False
    window_start: datetime | None = None
    data: list[SlotState] = []
    closet: list[ClosetItem] = []
    closet_value_at_cost: str | None = None
    warnings: list[str] = []


# selection -> (price_cents, lifetime_units)
Counters = dict[str, tuple[int | None, int]]


def build(as_of: datetime, latest: Counters, start: datetime | None, baseline: Counters,
          counts: dict[str, stock.SlotCount] | None = None, last_loaded: dict[str, datetime] | None = None,
          sold_since_load: dict[str, int] | None = None, now: datetime | None = None) -> list[SlotState]:
    """Per-slot state from two polls' counters and the receipts. Pure: no database, no clock."""
    counts, last_loaded, sold_since_load = counts or {}, last_loaded or {}, sold_since_load or {}
    days = Decimal((as_of - start).total_seconds()) / 86400 if start else Decimal(0)
    states = []
    for slot in SLOTS:
        price, units = latest.get(slot, (None, None))
        sold = per_day = None
        if units is not None and slot in baseline and days > 0:
            diff = units - baseline[slot][1]
            if diff >= 0:  # a lower count means a replaced board, not negative sales
                sold = diff
                per_day = (diff / days).quantize(Decimal("0.01"), ROUND_HALF_UP)
        count = counts.get(slot)
        loaded_at = last_loaded.get(slot)
        states.append(SlotState(
            slot=slot,
            price=None if price is None else f"{price // 100}.{price % 100:02d}",
            lifetime_units=units,
            units_sold_in_window=sold,
            units_per_day=None if per_day is None else str(per_day),
            product=count.product if count else None,
            stock_on_hand=count.units if count else None,
            units_sold_since_load=sold_since_load.get(slot) if count else None,
            days_since_loaded=((now or as_of) - loaded_at).days if count and loaded_at else None,
            days_of_stock_left=_days_left(count.units, per_day) if count else None,
        ))
    return states


def _days_left(on_hand: int, per_day: Decimal | None) -> str | None:
    """On hand ÷ units per day. Null when the rate is unknown, or zero with stock left (no end in sight)."""
    if on_hand == 0:
        return "0.0"
    if not per_day:
        return None
    return str((Decimal(on_hand) / per_day).quantize(Decimal("0.1"), ROUND_HALF_UP))


def closet_items(closet: tuple[stock.ClosetCount, ...]) -> tuple[list[ClosetItem], str | None]:
    """The closet per product, and its total value at cost (null if any product's cost is unknown)."""
    items, total = [], Fraction(0)
    for c in closet:
        value = None if c.unit_cost is None else c.unit_cost * c.units
        items.append(ClosetItem(product=c.product, units=c.units, unit_cost=_dollars(c.unit_cost),
                                value_at_cost=_dollars(value)))
        total = None if value is None or total is None else total + value
    return items, _dollars(total)


def _dollars(cents: Fraction | None) -> str | None:
    if cents is None:
        return None
    return str((Decimal(cents.numerator) / Decimal(cents.denominator) / 100).quantize(Decimal("0.01"), ROUND_HALF_UP))


def get_machine_state(conn: psycopg.Connection, now: datetime | None = None) -> MachineState:
    now = now or datetime.now(timezone.utc)
    latest = _poll(conn, "true")
    if latest is None:
        return MachineState(success=False, message=(
            f"No valid polls for {DEVICE_ID} are stored yet. "
            "Run the history sync: python -m vending.sync_history"))
    as_of = latest[1]
    baseline = _poll(conn, "started_at <= %s", as_of - WINDOW) or _first_poll(conn)
    if baseline and baseline[0] == latest[0]:
        baseline = None
    latest_counters = _counters(conn, latest[0])

    counted = stock.stock(conn)
    counts = {c.slot: c for c in counted.slots if c.slot in SLOTS}
    last_loaded = _last_loaded(conn)
    sold_since_load = _sold_since_load(conn, latest_counters, last_loaded)
    states = build(as_of, latest_counters,
                   baseline[1] if baseline else None,
                   _counters(conn, baseline[0]) if baseline else {},
                   counts, last_loaded, sold_since_load, now)
    closet, closet_value = closet_items(counted.closet)

    window = f"since {baseline[1]:%Y-%m-%d %H:%M} UTC" if baseline else "unavailable (only one poll stored)"
    age = now - as_of
    stale = age > STALE_AFTER
    warning = (f"STALE: the latest poll is {age.total_seconds() / 3600:.1f} hours old, so the history sync "
               "may have stopped; don't price on these rates until it catches up. ") if stale else ""
    return MachineState(
        success=True,
        stale=stale,
        message=(f"{warning}{len(states)} slots as of the poll at {as_of:%Y-%m-%d %H:%M} UTC; "
                 f"sales window {window}. {_receipts_note(counts, closet, counted.warnings)}"),
        count=len(states),
        as_of=as_of,
        window_start=baseline[1] if baseline else None,
        data=states,
        closet=closet,
        closet_value_at_cost=closet_value,
        warnings=list(counted.warnings),
    )


def _receipts_note(counts: dict, closet: list[ClosetItem], warnings: tuple[str, ...]) -> str:
    missing = [slot for slot in SLOTS if slot not in counts]
    if len(missing) == len(SLOTS):
        note = (f"No receipts are stored yet, so {RECEIPT_FIELDS} are null for every slot. "
                "If a request has been fulfilled, run: python -m vending.sync_requests.")
    elif missing:
        note = f"No receipts yet for slots {', '.join(missing)}, so their {RECEIPT_FIELDS} are null."
    else:
        note = "Stock is from receipts for all 20 slots."
    note += f" Closet: {sum(c.units for c in closet)} units of {len(closet)} products." if closet else " Closet: empty."
    if warnings:
        note += f" {len(warnings)} stock warning{'s' if len(warnings) != 1 else ''}; see warnings."
    return note


def _last_loaded(conn: psycopg.Connection) -> dict[str, datetime]:
    """When each slot last had units put in it, from fulfilled receipts."""
    rows = conn.execute(
        "select l.slot, max(l.resolved_at) from purchase_lines l join purchase_requests r using (request_id)"
        " where r.status = 'fulfilled' and l.outcome in ('primary', 'alternate', 'loaded') and l.units_loaded > 0"
        " group by l.slot")
    return dict(rows)


def _sold_since_load(conn: psycopg.Connection, latest: Counters, last_loaded: dict[str, datetime]) -> dict[str, int]:
    """Latest lifetime units minus the count at the last good poll at or before the load.

    Missing when no poll predates the load, or the counter went down since (a
    replaced board), rather than a wrong number.
    """
    sold = {}
    for slot, loaded_at in last_loaded.items():
        if slot not in latest:
            continue
        before = conn.execute(
            "select s.lifetime_units from poll_selections s join polls p using (session_id)"
            " where p.device_id = %s and p.crc_ok and s.selection = %s and p.started_at <= %s"
            " order by p.started_at desc limit 1", (DEVICE_ID, slot, loaded_at)).fetchone()
        if before is not None and latest[slot][1] >= before[0]:
            sold[slot] = latest[slot][1] - before[0]
    return sold


def _poll(conn: psycopg.Connection, where: str, *params) -> tuple[str, datetime] | None:
    return conn.execute(
        f"select session_id, started_at from polls where device_id = %s and crc_ok and {where}"
        " order by started_at desc limit 1", (DEVICE_ID, *params)).fetchone()


def _first_poll(conn: psycopg.Connection) -> tuple[str, datetime] | None:
    return conn.execute(
        "select session_id, started_at from polls where device_id = %s and crc_ok"
        " order by started_at limit 1", (DEVICE_ID,)).fetchone()


def _counters(conn: psycopg.Connection, session_id: str) -> Counters:
    rows = conn.execute(
        "select selection, price_cents, lifetime_units from poll_selections"
        " where session_id = %s and selection = any(%s)", (session_id, list(SLOTS)))
    return {selection: (price, units) for selection, price, units in rows}
