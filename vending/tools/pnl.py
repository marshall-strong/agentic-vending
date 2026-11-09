"""`get_pnl` (tool #10): profit and loss over a window, and inventory at cost at its end.

Revenue is the takings from the polls with the sales tax taken out. Cost of
sales is what the units sold actually cost, at weighted-average cost from the
receipts (`vending.stock`), so it is known only for slots we have loaded; the
reply says when some sold units could not be costed. Visit fees, token spend
and adjustments come from the ledger. Inventory is every unit on hand, in the
machine and the closet, at the cost we paid. See `vending.ledger`.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import psycopg

from vending import ledger
from vending.tools import Reply


class Pnl(Reply):
    start: datetime | None = None
    end: datetime | None = None
    revenue: str | None = None
    tax_set_aside: str | None = None
    revenue_ex_tax: str | None = None
    cost_of_sales: str | None = None
    cost_of_sales_complete: bool | None = None
    units_sold: int | None = None
    gross_margin: str | None = None
    visit_fees: str | None = None
    visits: int | None = None
    token_spend: str | None = None
    adjustments: str | None = None
    net: str | None = None
    purchases: str | None = None
    inventory_machine: str | None = None
    inventory_closet: str | None = None
    inventory: str | None = None
    inventory_complete: bool | None = None
    weeks: list[dict] = []
    notes: list[str] = []


def get_pnl(conn: psycopg.Connection, start: date | None, end: date | None) -> Pnl:
    """Both dates are UTC days and inclusive. Defaults: the opening day to today."""
    today = datetime.now(timezone.utc).date()
    if start is None:
        opened = ledger.opening(conn)
        if opened is None:
            return Pnl(success=False, message=ledger.OPEN_HINT + " Or give start and end dates.")
        first = opened.at
    else:
        first = datetime.combine(start, time.min, timezone.utc)
    last_day = end or today
    if last_day < first.date():
        return Pnl(success=False, message=f"end ({last_day}) is before start ({first.date()}).")
    last = datetime.combine(last_day + timedelta(days=1), time.min, timezone.utc) - timedelta(microseconds=1)
    p = ledger.pnl(conn, first, last)
    net = ledger.money(p.net_cents)
    return Pnl(
        success=True,
        message=(f"{first:%Y-%m-%d} to {last_day}: revenue {ledger.money(p.revenue_ex_tax_cents)} ex tax, "
                 + (f"net {net}" if net is not None else "net unknown (cost of sales could not be worked out)")
                 + f", inventory at cost {ledger.money(p.inventory_cents) or 'unknown'}."),
        count=len(p.takings.by_week),
        start=first,
        end=last,
        revenue=ledger.money(p.takings.cents),
        tax_set_aside=ledger.money(p.takings.tax_cents),
        revenue_ex_tax=ledger.money(p.revenue_ex_tax_cents),
        cost_of_sales=ledger.money(p.cost_of_sales_cents),
        cost_of_sales_complete=p.cost_of_sales_complete,
        units_sold=p.units_sold,
        gross_margin=ledger.money(p.gross_margin_cents),
        visit_fees=ledger.money(p.visit_fees_cents),
        visits=p.visits,
        token_spend=ledger.money(p.token_spend_cents),
        adjustments=ledger.money(p.adjustments_cents),
        net=net,
        purchases=ledger.money(p.receipts_cents),
        inventory_machine=ledger.money(p.inventory_machine_cents),
        inventory_closet=ledger.money(p.inventory_closet_cents),
        inventory=ledger.money(p.inventory_cents),
        inventory_complete=p.inventory_complete,
        weeks=[{"week": w.week.isoformat(), "revenue": ledger.money(w.takings_cents),
                "tax_set_aside": ledger.money(w.tax_cents)} for w in p.takings.by_week],
        notes=list(p.notes),
    )
