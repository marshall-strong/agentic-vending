"""`get_machine_state` (tool #1): what each of our slots holds, charges and sells.

Built from our database only; it never reads the machine.

- Price and sales come from the history sync's stored polls. Sales over the
  window are the counters' rises from the last poll at or before the window's
  start to the latest poll, read through `vending.counters`, so a misread poll
  or a board swap counts the same here as in `vending.stock` and the ledger.
- Product and stock come from the purchase-request receipts, through
  `vending.stock`, which replays every receipt and every poll. `stock_on_hand`
  counts from when the slot's current product went in (loaded − sold − lost),
  so a top-up of the same product adds to it.
- `days_since_loaded` and `units_sold_since_load` count from the slot's most
  recent load: the latest fulfilled receipt line that put units in it.
- The closet (back stock) is listed per product, with its value at cost.
- What a person saw, from the latest count in `slot_counts` (`vending.slot_counts`),
  fills `counted_*` for a slot the receipts don't cover, or whose count is newer
  than its latest receipt movement (the dashboard's rule). `counted_product` is
  free text as seen, not a SKU; `counted_on_hand` is the units counted less what
  the polls say sold since, an estimate for a photo count. `counted_days_ago`
  is its age: nothing ages a count except sales, so after `COUNT_STALE_DAYS` it
  is weak evidence (the slot may have been restocked or emptied since). When a
  slot has both `stock_on_hand` and `counted_on_hand`, the count is the later
  observation: trust it over the receipts' arithmetic.

A slot with no receipts yet has `null` for every receipt field: null, not
zero, so the agent can't mistake "unknown" for "empty". The message names
those slots. A count that saw a slot empty gives `counted_on_hand` 0 and
`counted_product` null; a slot nobody counted has `counted_at` null.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction

import psycopg
from pydantic import BaseModel

from vending import counters, slot_counts, stock
from vending.slots import DEVICE_ID, SLOTS
from vending.tools import Reply

WINDOW = timedelta(days=7)
# David polls hourly; older than this means the history sync has likely stalled.
STALE_AFTER = timedelta(hours=3)
RECEIPT_FIELDS = "product, stock_on_hand, units_sold_since_load, days_since_loaded and days_of_stock_left"


COUNT_STALE_DAYS = 7   # a person's count older than this is called weak evidence in the message


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
    # From the latest person's count, when receipts don't cover the slot or the count is newer.
    counted_product: str | None = None   # as seen, free text, not a SKU; null when seen empty
    counted_on_hand: int | None = None   # counted less sold since; an estimate for a photo count
    counted_at: datetime | None = None   # null: nobody counted this slot (or receipts are newer)
    count_source: str | None = None      # "photo" (an estimate) or "door" (counted with the door open)
    counted_days_ago: int | None = None  # whole days since the count; an old count is weak evidence


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


def build(as_of: datetime, latest: Counters, start: datetime | None, window_sold: dict[str, int],
          counts: dict[str, stock.SlotCount] | None = None, last_loaded: dict[str, datetime] | None = None,
          sold_since_load: dict[str, int] | None = None, now: datetime | None = None,
          seen: dict[str, dict] | None = None) -> list[SlotState]:
    """Per-slot state from the latest poll, the window's sales, the receipts and the latest person's
    count (`seen`, from `vending.slot_counts.latest`). Pure: no database, no clock."""
    counts, last_loaded, sold_since_load, seen = counts or {}, last_loaded or {}, sold_since_load or {}, seen or {}
    days = Decimal((as_of - start).total_seconds()) / 86400 if start else Decimal(0)
    states = []
    for slot in SLOTS:
        price, units = latest.get(slot, (None, None))
        sold = per_day = None
        if units is not None and slot in window_sold and days > 0:
            sold = window_sold[slot]
            per_day = (sold / days).quantize(Decimal("0.01"), ROUND_HALF_UP)
        count = counts.get(slot)
        loaded_at = last_loaded.get(slot)
        looked = _newer_count(seen.get(slot), count)
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
            counted_product=looked["product"] if looked else None,
            counted_on_hand=looked["on_hand"] if looked else None,
            counted_at=looked["counted_at"] if looked else None,
            count_source=looked["source"] if looked else None,
            counted_days_ago=((now or as_of) - looked["counted_at"]).days if looked else None,
        ))
    return states


def _newer_count(looked: dict | None, count: stock.SlotCount | None) -> dict | None:
    """The person's count, unless the receipts know the slot and moved it since (the dashboard's rule)."""
    if looked is None:
        return None
    moved = (count.last_moved or count.since) if count else None
    return looked if moved is None or looked["counted_at"] > moved else None


def sold_in_window(rows: list[tuple[str, datetime, int]], start: datetime) -> dict[str, int]:
    """Units sold per slot after `start`, from `(selection, polled_at, lifetime_units)` rows. Pure.

    The rows should begin a poll before `start`, so a misread at the baseline
    poll itself is caught. Only slots read at or before `start` get a number,
    so a slot missing from the baseline poll stays unknown rather than counting
    from a later reading.
    """
    readings: dict[str, list[tuple[datetime, int]]] = {}
    for selection, at, units in rows:
        readings.setdefault(selection, []).append((at, units))
    return {slot: sum(rise for at, rise in counters.rises(r) if at > start)
            for slot, r in readings.items() if min(r)[0] <= start}


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
    start = baseline[1] if baseline else None
    window_sold = sold_in_window(_readings(conn, start, as_of), start) if start else {}

    counted = stock.stock(conn, until=now)
    counts = {c.slot: c for c in counted.slots if c.slot in SLOTS}
    last_loaded = _last_loaded(conn)
    sold_since_load = _sold_since_load(counted.sales, last_loaded)
    seen = _seen(conn)
    states = build(as_of, _counters(conn, latest[0]), start, window_sold,
                   counts, last_loaded, sold_since_load, now, seen)
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
                 f"sales window {window}. {_receipts_note(counts, closet, counted.warnings)}{_count_note(states)}"),
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


def _count_note(states: list[SlotState]) -> str:
    """Sentences on the person's count, when it fills in any slot: its dates, its sources, its age."""
    looked = [s for s in states if s.counted_at is not None]
    if not looked:
        return ""
    first, last = min(s.counted_at for s in looked), max(s.counted_at for s in looked)
    dates = f"{last:%Y-%m-%d}" if f"{first:%Y-%m-%d}" == f"{last:%Y-%m-%d}" else f"{first:%Y-%m-%d} to {last:%Y-%m-%d}"
    photos = sum(s.count_source == "photo" for s in looked)
    doors = len(looked) - photos
    kinds = [f"{n} from {what}" for n, what in ((photos, "a photo, so units are estimates"), (doors, "the door open")) if n]
    holding = [s.slot for s in looked if s.counted_on_hand]
    note = (f" A person's count ({dates}; {', '.join(kinds)}) shows what is in {len(looked)} slots: see "
            f"counted_product and counted_on_hand. Slots it saw holding stock: {', '.join(holding) or 'none'}.")
    old = [s for s in looked if (s.counted_days_ago or 0) > COUNT_STALE_DAYS]
    if old:
        note += (f" Counts over {COUNT_STALE_DAYS} days old are weak evidence (the slot may have been restocked or emptied "
                 f"since): {', '.join(f'{s.slot} ({s.counted_days_ago} days)' for s in old)}.")
    both = [s.slot for s in looked if s.stock_on_hand is not None]
    if both:
        note += f" Slots {', '.join(both)} have both stock_on_hand and counted_on_hand: the count is newer, so trust it."
    return note


def _seen(conn: psycopg.Connection) -> dict[str, dict]:
    """The latest count per selection, or nothing when `slot_counts` isn't there yet.

    A savepoint keeps the caller's transaction usable if the table is missing.
    """
    try:
        with conn.transaction():
            return slot_counts.latest(conn, DEVICE_ID)
    except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn):
        return {}


def _last_loaded(conn: psycopg.Connection) -> dict[str, datetime]:
    """When each slot last had units put in it, from fulfilled receipts."""
    rows = conn.execute(
        "select l.slot, max(l.resolved_at) from purchase_lines l join purchase_requests r using (request_id)"
        " where r.status = 'fulfilled' and l.outcome in ('primary', 'alternate', 'loaded') and l.units_loaded > 0"
        " group by l.slot")
    return dict(rows)


def _sold_since_load(sales: tuple[stock.SaleCost, ...], last_loaded: dict[str, datetime]) -> dict[str, int]:
    """Units sold in each slot since its last load: the replay's sales after it. Pure.

    The replay reads the counters through `vending.counters`, so a misread
    poll or a board swap counts the same here as in `stock_on_hand` and the ledger.
    The old product's share of a window split at a product change left the
    count at the change (`taken_at`), so it is not sold since that load.
    """
    sold = dict.fromkeys(last_loaded, 0)
    for sale in sales:
        loaded_at = last_loaded.get(sale.slot)
        if loaded_at is not None and (sale.taken_at or sale.at) > loaded_at:
            sold[sale.slot] += sale.units
    return sold


def _poll(conn: psycopg.Connection, where: str, *params) -> tuple[str, datetime] | None:
    return conn.execute(
        f"select session_id, started_at from polls where device_id = %s and crc_ok and {where}"
        " order by started_at desc limit 1", (DEVICE_ID, *params)).fetchone()


def _first_poll(conn: psycopg.Connection) -> tuple[str, datetime] | None:
    return conn.execute(
        "select session_id, started_at from polls where device_id = %s and crc_ok"
        " order by started_at limit 1", (DEVICE_ID,)).fetchone()


def _readings(conn: psycopg.Connection, start: datetime, end: datetime) -> list[tuple[str, datetime, int]]:
    """Every good poll's lifetime units for our slots from the poll before `start` to `end`, inclusive."""
    return conn.execute(
        "select s.selection, p.started_at, s.lifetime_units from poll_selections s join polls p using (session_id)"
        " where p.device_id = %s and p.crc_ok and s.selection = any(%s) and p.started_at <= %s"
        " and p.started_at >= coalesce((select max(started_at) from polls"
        "   where device_id = %s and crc_ok and started_at < %s), %s)",
        (DEVICE_ID, list(SLOTS), end, DEVICE_ID, start, start)).fetchall()


def _counters(conn: psycopg.Connection, session_id: str) -> Counters:
    rows = conn.execute(
        "select selection, price_cents, lifetime_units from poll_selections"
        " where session_id = %s and selection = any(%s)", (session_id, list(SLOTS)))
    return {selection: (price, units) for selection, price, units in rows}
