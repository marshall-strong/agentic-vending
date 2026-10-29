"""`get_machine_state` (tool #1): what each of our slots charges and how fast it sells.

Built from the history sync's stored polls only; it never reads the machine.
Lifetime counters never reset, so sales over the window are the latest poll
minus the last poll at or before the window's start.

The fields that need restock receipts (stock on hand, units sold since the last
load, days since loaded, days of stock left) are `null` until the receipts sync
exists. They are null, not zero, so the agent can't mistake "unknown" for "empty".
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

import psycopg
from pydantic import BaseModel

from vending.slots import DEVICE_ID, SLOTS
from vending.tools import Reply

WINDOW = timedelta(days=7)
NEEDS_RECEIPTS = "stock_on_hand, units_sold_since_load, days_since_loaded and days_of_stock_left need restock receipts, which aren't synced yet"


class SlotState(BaseModel):
    slot: str
    price: str | None
    lifetime_units: int | None
    units_sold_in_window: int | None
    units_per_day: str | None
    stock_on_hand: int | None = None
    units_sold_since_load: int | None = None
    days_since_loaded: int | None = None
    days_of_stock_left: str | None = None


class MachineState(Reply):
    as_of: datetime | None = None
    window_start: datetime | None = None
    data: list[SlotState] = []


# selection -> (price_cents, lifetime_units)
Counters = dict[str, tuple[int | None, int]]


def build(as_of: datetime, latest: Counters, start: datetime | None, baseline: Counters) -> list[SlotState]:
    """Per-slot state from two polls' counters. Pure: no database, no clock."""
    days = Decimal((as_of - start).total_seconds()) / 86400 if start else Decimal(0)
    states = []
    for slot in SLOTS:
        price, units = latest.get(slot, (None, None))
        sold = per_day = None
        if units is not None and slot in baseline and days > 0:
            diff = units - baseline[slot][1]
            if diff >= 0:  # a lower count means a replaced board, not negative sales
                sold = diff
                per_day = str((diff / days).quantize(Decimal("0.01"), ROUND_HALF_UP))
        states.append(SlotState(
            slot=slot,
            price=None if price is None else f"{price // 100}.{price % 100:02d}",
            lifetime_units=units,
            units_sold_in_window=sold,
            units_per_day=per_day,
        ))
    return states


def get_machine_state(conn: psycopg.Connection) -> MachineState:
    latest = _poll(conn, "true")
    if latest is None:
        return MachineState(success=False, message=(
            f"No valid polls for {DEVICE_ID} are stored yet. "
            "Run the history sync: python -m vending.sync_history"))
    as_of = latest[1]
    baseline = _poll(conn, "started_at <= %s", as_of - WINDOW) or _first_poll(conn)
    if baseline and baseline[0] == latest[0]:
        baseline = None
    states = build(as_of, _counters(conn, latest[0]),
                   baseline[1] if baseline else None,
                   _counters(conn, baseline[0]) if baseline else {})
    window = f"since {baseline[1]:%Y-%m-%d %H:%M} UTC" if baseline else "unavailable (only one poll stored)"
    return MachineState(
        success=True,
        message=f"{len(states)} slots as of the poll at {as_of:%Y-%m-%d %H:%M} UTC; sales window {window}. {NEEDS_RECEIPTS}.",
        count=len(states),
        as_of=as_of,
        window_start=baseline[1] if baseline else None,
        data=states,
    )


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
