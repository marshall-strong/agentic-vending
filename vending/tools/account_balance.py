"""`get_account_balance` (tool #4): the balance from the ledger, never a stored figure.

Opening capital plus takings (tax included), minus receipts, midweek visit fees
and token spend, plus or minus adjustments. The sales tax inside the takings is
set aside and the spendable balance is what is left after it; that is the
figure the planner's balance gate checks. See `vending.ledger`.
"""

from __future__ import annotations

from datetime import datetime

import psycopg

from vending import ledger
from vending.tools import Reply


class AccountBalance(Reply):
    as_of: datetime | None = None
    opening_at: datetime | None = None
    opening: str | None = None
    revenue: str | None = None
    receipts: str | None = None
    visit_fees: str | None = None
    visits: int | None = None
    token_spend: str | None = None
    adjustments: str | None = None
    balance: str | None = None
    tax_set_aside: str | None = None
    spendable: str | None = None
    notes: list[str] = []


def get_account_balance(conn: psycopg.Connection) -> AccountBalance:
    b = ledger.balance(conn)
    if b is None:
        return AccountBalance(success=False, message=ledger.OPEN_HINT)
    return AccountBalance(
        success=True,
        message=(f"Spendable {ledger.money(b.spendable_cents)} as of {b.as_of:%Y-%m-%d %H:%M} UTC: balance "
                 f"{ledger.money(b.balance_cents)} less {ledger.money(b.takings.tax_cents)} of sales tax set aside. "
                 f"Revenue includes tax; it is counted from the opening on {b.opening_at:%Y-%m-%d}."),
        count=1,
        as_of=b.as_of,
        opening_at=b.opening_at,
        opening=ledger.money(b.opening_cents),
        revenue=ledger.money(b.takings.cents),
        receipts=ledger.money(b.receipts_cents),
        visit_fees=ledger.money(b.visit_fees_cents),
        visits=b.visits,
        token_spend=ledger.money(b.token_spend_cents),
        adjustments=ledger.money(b.adjustments_cents),
        balance=ledger.money(b.balance_cents),
        tax_set_aside=ledger.money(b.takings.tax_cents),
        spendable=ledger.money(b.spendable_cents),
        notes=list(b.notes),
    )
