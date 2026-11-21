"""`get_pnl` (tool #10): profit and loss over a window, and inventory at cost at its end.

Revenue is the takings from the polls with the sales tax taken out. Cost of
sales is what the units sold actually cost, first in, first out, from the
receipts (`vending.stock`), so it is known only for units sold from slots we
have loaded; `units_sold` counts every sale from our 20 slots and the reply
says how many of them could not be costed. Shrinkage is the cost of units
lost without a sale. Visit fees, token spend and adjustments come from the
ledger; capital and tax remittances are shown but are not profit or loss.
The sales tax is rounded per tax week, Sunday to Saturday Eastern, as David
calculates it (`tax_weeks`); `weeks` are Saturday to Friday, each with its
share of that tax.
While any cost is missing (`cost_of_sales_complete` or `shrinkage_complete`
false), `gross_margin` and `net` are over the known costs only, and the
summary says "net at known costs (incomplete...)", never plain "net". Units
sold across a swap the counters can't split have an estimate apart
(`estimated_cost_of_sales`), in no profit figure; the stock they took is
`unattributed_stock`, a reconciliation line, also in no profit figure.
Inventory is every unit on hand, in the machine and the closet, at the cost
we paid. See `vending.ledger`.

The window is given as calendar days in Eastern time, the team's and the
machine's clock, so a Friday evening sale lands in Friday.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg

from vending import ledger
from vending.slots import SlotsNotRecorded
from vending.tools import Reply

EASTERN = ZoneInfo(ledger.TIMEZONE)


class Pnl(Reply):
    start: datetime | None = None
    end: datetime | None = None
    revenue: str | None = None
    tax_set_aside: str | None = None
    revenue_ex_tax: str | None = None
    units_sold: int | None = None
    units_costed: int | None = None
    cost_of_sales: str | None = None
    cost_of_sales_complete: bool | None = None
    units_estimated: int | None = None              # sold across a swap the counters can't split
    estimated_cost_of_sales: str | None = None      # their estimate: not in cost_of_sales, margin or net
    unattributed_stock: str | None = None           # stock those sales took: a reconciliation line, not in net
    unattributed_stock_provisional: bool | None = None   # a write-off at the swap may be one of those sales
    gross_margin: str | None = None
    shrinkage: str | None = None
    shrinkage_units: int | None = None
    visit_fees: str | None = None
    visits: int | None = None
    token_spend: str | None = None
    adjustments: str | None = None
    net: str | None = None
    purchases: str | None = None
    capital: str | None = None
    tax_remitted: str | None = None
    inventory_machine: str | None = None
    inventory_closet: str | None = None
    inventory: str | None = None
    inventory_complete: bool | None = None
    weeks: list[dict] = []          # display weeks, Saturday to Friday; tax is each week's share
    tax_weeks: list[dict] = []      # tax weeks, Sunday to Saturday, as David calculates the tax
    notes: list[str] = []


def get_pnl(conn: psycopg.Connection, start: date | None, end: date | None) -> Pnl:
    """Both dates are Eastern calendar days, inclusive. Defaults: the opening moment to today."""
    today = datetime.now(EASTERN).date()
    opened = ledger.opening(conn)
    if start is None:
        if opened is None:
            return Pnl(success=False, message=ledger.OPEN_HINT + " Or give start and end dates.")
        first = since = opened.at
    else:
        first = datetime.combine(start, time.min, EASTERN)
        # The ledger counts the rises at polls after `since`, so a calendar start is counted from one microsecond
        # before midnight: a poll at midnight sharp is in the window (a tax week's Sunday 00:00 poll, say). An
        # opening at that midnight stays the baseline, so its poll's rise (sales before the opening) is not counted.
        since = first if opened is not None and opened.at == first else first - timedelta(microseconds=1)
    last_day = end or today
    if last_day < first.astimezone(EASTERN).date():
        return Pnl(success=False, message=f"end ({last_day}) is before start ({first.astimezone(EASTERN).date()}).")
    last = datetime.combine(last_day + timedelta(days=1), time.min, EASTERN) - timedelta(microseconds=1)
    try:
        p = ledger.pnl(conn, since, last)
    except SlotsNotRecorded as error:
        return Pnl(success=False, message=str(error))
    net = ledger.money(p.net_cents)
    # A net over known costs only is never headlined as net (#172 review).
    missing = []
    if not p.cost_of_sales_complete:
        missing.append(f"{p.units_sold - p.units_costed} of {p.units_sold} units sold not costed exactly")
    if not p.shrinkage_complete:
        missing.append("some units lost have no known cost")
    if p.units_estimated:
        missing.append(f"estimated cost of {p.units_estimated} units {ledger.money(p.estimated_cost_of_sales_cents)}, "
                       "an estimate not in net")
    headline = f"net at known costs {net} (incomplete: {'; '.join(missing)})" if missing else f"net {net}"
    return Pnl(
        success=True,
        message=(f"{first.astimezone(EASTERN):%Y-%m-%d} to {last_day} (Eastern days): revenue "
                 f"{ledger.money(p.revenue_ex_tax_cents)} ex tax, {headline}, "
                 f"inventory at cost {ledger.money(p.inventory_cents) or 'unknown'}."),
        count=len(p.takings.by_week),
        start=first.astimezone(timezone.utc),
        end=last.astimezone(timezone.utc),
        revenue=ledger.money(p.takings.cents),
        tax_set_aside=ledger.money(p.takings.tax_cents),
        revenue_ex_tax=ledger.money(p.revenue_ex_tax_cents),
        units_sold=p.units_sold,
        units_costed=p.units_costed,
        cost_of_sales=ledger.money(p.cost_of_sales_cents),
        cost_of_sales_complete=p.cost_of_sales_complete,
        units_estimated=p.units_estimated,
        estimated_cost_of_sales=ledger.money(p.estimated_cost_of_sales_cents),
        unattributed_stock=ledger.money(p.unattributed_stock_cents),
        unattributed_stock_provisional=p.unattributed_stock_provisional,
        gross_margin=ledger.money(p.gross_margin_cents),
        shrinkage=ledger.money(p.shrinkage_cents),
        shrinkage_units=p.shrinkage_units,
        visit_fees=ledger.money(p.visit_fees_cents),
        visits=p.visits,
        token_spend=ledger.money(p.token_spend_cents),
        adjustments=ledger.money(p.adjustments_cents),
        net=net,
        purchases=ledger.money(p.receipts_cents),
        capital=ledger.money(p.capital_cents),
        tax_remitted=ledger.money(p.tax_remitted_cents),
        inventory_machine=ledger.money(p.inventory_machine_cents),
        inventory_closet=ledger.money(p.inventory_closet_cents),
        inventory=ledger.money(p.inventory_cents),
        inventory_complete=p.inventory_complete,
        weeks=[{"week": w.week.isoformat(), "revenue": ledger.money(w.takings_cents),
                "tax_set_aside": ledger.money(w.tax_cents)} for w in p.takings.by_week],
        tax_weeks=[{"week": w.week.isoformat(), "ends": ledger.tax_week_end(w.week).isoformat(),
                    "revenue": ledger.money(w.takings_cents), "tax": ledger.money(w.tax_cents)}
                   for w in p.takings.by_tax_week],
        notes=list(p.notes),
    )

