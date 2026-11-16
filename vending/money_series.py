"""Daily money from the ledger, booked to Eastern calendar days.

Sales and price changes are observed at polls, not necessarily when they happened.
Tax is the change in the period's weekly-rounded tax, so the days add to its P&L.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from fractions import Fraction
from zoneinfo import ZoneInfo

import psycopg

from vending import db, ledger
from vending.slots import DEVICE_ID, SLOTS

EASTERN = ZoneInfo(ledger.TIMEZONE)


@dataclass(frozen=True)
class Event:
    at: datetime
    kind: str
    detail: str
    reference: str | None = None
    amount_cents: Fraction | None = None


@dataclass(frozen=True)
class Day:
    date: date
    revenue_ex_tax_cents: int
    takings_cents: int
    sales_tax_cents: int
    units_sold: int
    units_costed: int
    cost_of_sales_cents: Fraction
    cost_of_sales_complete: bool
    shrinkage_cents: Fraction
    shrinkage_complete: bool
    visit_fees_cents: int
    token_spend_cents: Fraction
    adjustments_cents: Fraction
    gross_profit_cents: Fraction
    net_cents: Fraction
    purchases_cents: Fraction
    cash_balance_cents: Fraction | None
    inventory_cents: Fraction | None
    inventory_complete: bool
    net_worth_cents: Fraction | None
    events: tuple[Event, ...]
    notes: tuple[str, ...]


def _events(conn: psycopg.Connection, start: datetime, end: datetime) -> list[Event]:
    events = [Event(e.at, "tokens", e.detail, e.reference, -e.amount_cents)
              for e in ledger.entries(conn, start, end) if e.kind == "tokens"]
    rows = conn.execute(
        "select r.request_id, r.off_cycle, coalesce(min(l.resolved_at), r.fulfilled_at) as at"
        " from purchase_requests r left join purchase_lines l using (request_id)"
        " where r.status = 'fulfilled' group by r.request_id, r.off_cycle, r.fulfilled_at"
    ).fetchall()
    for ref, off_cycle, at in rows:
        if at is not None and start <= at <= end:
            events.append(Event(at, "midweek_visit" if off_cycle else "restock_visit",
                                "Fulfilled request", ref))
    rows = conn.execute(
        "select l.resolved_at, l.request_id, l.line_no, l.total_paid from purchase_lines l"
        " join purchase_requests r using (request_id) where r.status = 'fulfilled'"
        " and l.resolved_at between %s and %s", (start, end),
    ).fetchall()
    for at, ref, line, paid in rows:
        events.append(Event(at, "receipt", f"Receipt line {line}", ref,
                            None if paid is None else Fraction(paid) * 100))
    # A price is only observed at a poll; do not infer the time of the physical change.
    rows = conn.execute(
        "select p.started_at, s.selection, s.price_cents from poll_selections s"
        " join polls p using (session_id) where p.device_id = %s and p.crc_ok"
        " and s.selection = any(%s) and p.started_at <= %s"
        " order by p.started_at, p.session_id, s.selection", (DEVICE_ID, list(SLOTS), end),
    ).fetchall()
    previous = {}
    for at, slot, price in rows:
        if price is None:
            continue
        if slot in previous and previous[slot] != price and start <= at:
            events.append(Event(at, "price_change", f"Slot {slot}: {previous[slot]} to {price} cents (observed)"))
        previous[slot] = price
    return sorted(events, key=lambda event: (event.at, event.kind, event.reference or ""))


def daily(conn: psycopg.Connection, start: date, end: date) -> list[Day]:
    """Return inclusive Eastern days; amounts are exact cents and notes preserve gaps.

    A cumulative P&L avoids rounding tax separately each day. The differences
    also retain the ledger's treatment of counter corrections and receipt clocks.
    """
    if end < start:
        raise ValueError("end must be on or after start")
    first = datetime.combine(start, time.min, EASTERN) - timedelta(microseconds=1)
    last = datetime.combine(end + timedelta(days=1), time.min, EASTERN) - timedelta(microseconds=1)
    opened = ledger.opening(conn)
    # This is competition money, so it counts from the ledger's opening, as the balance does.
    # Anything earlier (development model calls, the staff's sales) is left out, so a chart or
    # briefing built on it can't show the agent our testing spend.
    if opened is not None and opened.at - timedelta(microseconds=1) > first:
        first = opened.at - timedelta(microseconds=1)
    unopened = () if opened is not None else (
        "the ledger has no opening entry, so none of this is competition money yet",)
    events = _events(conn, first + timedelta(microseconds=1), last)
    result = []
    # P&L includes entries and receipts at its start; subtract those in the
    # preceding microsecond while still counting polls exactly at midnight.
    previous = ledger.pnl(conn, first, first)
    day = start
    while day <= end:
        finish = datetime.combine(day + timedelta(days=1), time.min, EASTERN) - timedelta(microseconds=1)
        if finish <= first:
            result.append(Day(day, 0, 0, 0, 0, 0, Fraction(0), True, Fraction(0), True, 0, Fraction(0),
                              Fraction(0), Fraction(0), Fraction(0), Fraction(0), None, None, False, None, (),
                              (f"before the ledger's opening on {opened.at.astimezone(EASTERN):%Y-%m-%d}, "
                               "so not competition money",)))
            day += timedelta(days=1)
            continue
        p = ledger.pnl(conn, first, finish)
        def diff(value, old):
            return value - (old if previous is not None else 0)
        takings = diff(p.takings.cents, previous.takings.cents if previous else 0)
        tax = diff(p.takings.tax_cents, previous.takings.tax_cents if previous else 0)
        units = diff(p.units_sold, previous.units_sold if previous else 0)
        costed = diff(p.units_costed, previous.units_costed if previous else 0)
        cost = diff(p.cost_of_sales_cents, previous.cost_of_sales_cents if previous else 0)
        shrink = diff(p.shrinkage_cents, previous.shrinkage_cents if previous else 0)
        fees = diff(p.visit_fees_cents, previous.visit_fees_cents if previous else 0)
        tokens = diff(p.token_spend_cents, previous.token_spend_cents if previous else 0)
        adjustments = diff(p.adjustments_cents, previous.adjustments_cents if previous else 0)
        gross = takings - tax - cost
        balance = ledger.balance(conn, finish) if opened is not None and opened.at <= finish else None
        cash = balance.balance_cents if balance else None
        inventory = p.inventory_cents
        # Partial inventory is useful with its flag, but a partial net worth is misleading.
        worth = cash + inventory if cash is not None and inventory is not None and p.inventory_complete else None
        daily_pnl = ledger.pnl(conn, max(first, datetime.combine(day, time.min, EASTERN) - timedelta(microseconds=1)),
                               finish)
        result.append(Day(day, takings - tax, takings, tax, units, costed, cost, units == costed,
                          shrink, daily_pnl.shrinkage_complete, fees, tokens, adjustments, gross,
                          gross - shrink - fees - tokens + adjustments,
                          diff(p.receipts_cents, previous.receipts_cents if previous else 0),
                          cash, inventory, p.inventory_complete, worth,
                          tuple(e for e in events if e.at.astimezone(EASTERN).date() == day),
                          tuple(dict.fromkeys(unopened + p.notes + (balance.notes if balance else ())))))
        previous = p
        day += timedelta(days=1)
    return result


def cumulative(days: list[Day], field: str) -> list[Fraction]:
    """Running total of a flow field, without rounding between days."""
    flows = {"revenue_ex_tax_cents", "takings_cents", "sales_tax_cents", "units_sold", "units_costed",
             "cost_of_sales_cents", "shrinkage_cents", "visit_fees_cents", "token_spend_cents",
             "adjustments_cents", "gross_profit_cents", "net_cents", "purchases_cents"}
    if field not in flows:
        raise ValueError(f"not a flow field: {field}")
    total = Fraction(0)
    out = []
    for day in days:
        total += getattr(day, field)
        out.append(total)
    return out


def _json(value):
    if isinstance(value, Fraction):
        return {"numerator": value.numerator, "denominator": value.denominator}
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.end < args.start:
        parser.error("end must be on or after start")
    with db.connect() as conn:
        behind = db.pending(conn)
        if behind:
            print(f"The database is missing migrations: {', '.join(behind)}. Run: python -m vending.db migrate", file=sys.stderr)
            return 2
        days = daily(conn, args.start, args.end)
    if args.json:
        print(json.dumps([asdict(day) for day in days], default=_json, indent=2))
    else:
        print("Date        Revenue   Takings       Cost        Net       Cash  Notes")
        for day in days:
            values = [ledger.money(v) or "?" for v in (day.revenue_ex_tax_cents, day.takings_cents,
                      day.cost_of_sales_cents, day.net_cents, day.cash_balance_cents)]
            print(f"{day.date} " + " ".join(f"{v:>10}" for v in values) +
                  ("  incomplete" if not day.cost_of_sales_complete or not day.shrinkage_complete else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
