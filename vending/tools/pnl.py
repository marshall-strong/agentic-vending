"""`get_pnl` (tool #10): profit and loss over a window, and inventory at cost at its end.

Revenue is the takings from the polls with the sales tax taken out. Cost of
sales is what the units sold actually cost, at weighted-average cost from the
receipts (`vending.stock`), so it is known only for units sold from slots we
have loaded; `units_sold` counts every sale from our 20 slots and the reply
says how many of them could not be costed. Shrinkage is the cost of units
lost without a sale. Visit fees, token spend and adjustments come from the
ledger; capital and tax remittances are shown but are not profit or loss.
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
    weeks: list[dict] = []
    notes: list[str] = []


def get_pnl(conn: psycopg.Connection, start: date | None, end: date | None) -> Pnl:
    """Both dates are Eastern calendar days, inclusive. Defaults: the opening moment to today."""
    today = datetime.now(EASTERN).date()
    if start is None:
        opened = ledger.opening(conn)
        if opened is None:
            return Pnl(success=False, message=ledger.OPEN_HINT + " Or give start and end dates.")
        first = opened.at
    else:
        first = datetime.combine(start, time.min, EASTERN)
    last_day = end or today
    if last_day < first.astimezone(EASTERN).date():
        return Pnl(success=False, message=f"end ({last_day}) is before start ({first.astimezone(EASTERN).date()}).")
    last = datetime.combine(last_day + timedelta(days=1), time.min, EASTERN) - timedelta(microseconds=1)
    try:
        p = ledger.pnl(conn, first, last)
    except SlotsNotRecorded as error:
        return Pnl(success=False, message=str(error))
    net = ledger.money(p.net_cents)
    caveat = "" if p.cost_of_sales_complete else f" ({p.units_sold - p.units_costed} of {p.units_sold} units sold not costed)"
    return Pnl(
        success=True,
        message=(f"{first.astimezone(EASTERN):%Y-%m-%d} to {last_day} (Eastern days): revenue "
                 f"{ledger.money(p.revenue_ex_tax_cents)} ex tax, net {net}{caveat}, "
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
        notes=list(p.notes),
    )
