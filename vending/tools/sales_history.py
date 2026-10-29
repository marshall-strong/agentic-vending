"""`get_sales_history` (tool #2): units and ex-tax revenue per slot over a time window.

Built from the history sync's stored polls only; it never reads the machine.
Lifetime counters never reset, so sales over a window are the last poll at or
before `end` minus the last poll at or before `start`. Counters only move
between polls, so the reply says which two polls the window really spans.

Shelf prices include the 8% sales tax, so the tax inside takings T is
T x 8/108, rounded half up to the cent (the team's 19 Oct decision; see
"Ask David" in docs/planning/tool-list.md). Ex-tax revenue is T minus that.
"""

from __future__ import annotations

from datetime import datetime, timezone
from fractions import Fraction

import psycopg
from pydantic import BaseModel

from vending.pricing import DEFAULT_TAX_PERCENT
from vending.slots import DEVICE_ID, SLOTS
from vending.tools import Reply


class SlotSales(BaseModel):
    slot: str
    units: int | None
    revenue: str | None
    revenue_ex_tax: str | None


class SalesHistory(Reply):
    start: datetime | None = None
    end: datetime | None = None
    revenue: str | None = None
    revenue_ex_tax: str | None = None
    data: list[SlotSales] = []


# selection -> (lifetime_units, lifetime_sales_cents)
Counters = dict[str, tuple[int, int]]


def tax_cents(takings_cents: int, tax_percent: int = DEFAULT_TAX_PERCENT) -> int:
    """The tax inside tax-inclusive takings, rounded half up to the cent."""
    return int(Fraction(takings_cents * tax_percent, 100 + tax_percent) + Fraction(1, 2))


def dollars(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def build(first: Counters, last: Counters, slots: list[str]) -> tuple[list[SlotSales], int]:
    """Per-slot sales between two polls' counters, and total takings in cents. Pure: no database."""
    rows, total = [], 0
    for slot in slots:
        units = revenue = ex_tax = None
        if slot in first and slot in last:
            du = last[slot][0] - first[slot][0]
            dc = last[slot][1] - first[slot][1]
            if du >= 0 and dc >= 0:  # a lower count means a replaced board, not negative sales
                units, revenue, ex_tax = du, dollars(dc), dollars(dc - tax_cents(dc))
                total += dc
        rows.append(SlotSales(slot=slot, units=units, revenue=revenue, revenue_ex_tax=ex_tax))
    return rows, total


def get_sales_history(conn: psycopg.Connection, start: datetime, end: datetime,
                      slots: list[str] | None) -> SalesHistory:
    start, end = _utc(start), _utc(end)
    wanted = list(SLOTS) if not slots else slots
    unknown = sorted(set(wanted) - set(SLOTS))
    if unknown:
        return SalesHistory(success=False, message=(
            f"Not our slots: {', '.join(unknown)}. Ours are {', '.join(SLOTS)}; omit slots for all 20."))
    if start >= end:
        return SalesHistory(success=False, message="start must be before end.")
    last = _poll(conn, "started_at <= %s order by started_at desc", end)
    first = _poll(conn, "started_at <= %s order by started_at desc", start) \
        or _poll(conn, "started_at >= %s order by started_at", start)
    if last is None or first is None or first[1] >= last[1]:
        return SalesHistory(success=False, message=(
            "Fewer than two valid polls fall in that window, so there are no sales to measure. "
            "Widen the window, or run the history sync: python -m vending.sync_history"))
    rows, total = build(_counters(conn, first[0]), _counters(conn, last[0]), wanted)
    note = "" if first[1] <= start else f" No poll before {start:%Y-%m-%d %H:%M}, so it starts at the first one."
    return SalesHistory(
        success=True,
        message=(f"{len(rows)} slots, from the poll at {first[1]:%Y-%m-%d %H:%M} UTC to the poll at "
                 f"{last[1]:%Y-%m-%d %H:%M} UTC. Revenue includes 8% tax; revenue_ex_tax doesn't.{note}"),
        count=len(rows),
        start=first[1],
        end=last[1],
        revenue=dollars(total),
        revenue_ex_tax=dollars(total - tax_cents(total)),
        data=rows,
    )


def _utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment


def _poll(conn: psycopg.Connection, where: str, moment: datetime) -> tuple[str, datetime] | None:
    return conn.execute(
        f"select session_id, started_at from polls where device_id = %s and crc_ok and {where} limit 1",
        (DEVICE_ID, moment)).fetchone()


def _counters(conn: psycopg.Connection, session_id: str) -> Counters:
    rows = conn.execute(
        "select selection, lifetime_units, lifetime_sales_cents from poll_selections"
        " where session_id = %s and selection = any(%s)", (session_id, list(SLOTS)))
    return {selection: (units, cents) for selection, units, cents in rows}
