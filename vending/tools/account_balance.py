"""`get_account_balance` (tool #4): the balance from the ledger, never a stored figure.

Opening capital plus later capital and takings (tax included), minus receipts,
midweek visit fees, token spend and sales tax remitted, plus or minus
adjustments. The sales tax inside the takings is set aside; what is still owed
is the set-aside less what has been remitted, and the spendable balance is
what is left after it. That is the figure the planner's balance gate checks.
See `vending.ledger`.
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
    capital: str | None = None
    revenue: str | None = None
    receipts: str | None = None
    visit_fees: str | None = None
    visits: int | None = None
    token_spend: str | None = None
    tax_remitted: str | None = None
    adjustments: str | None = None
    balance: str | None = None
    tax_set_aside: str | None = None
    tax_owed: str | None = None
    spendable: str | None = None
    notes: list[str] = []


def get_account_balance(conn: psycopg.Connection, as_of: datetime | None = None) -> AccountBalance:
    """The balance now, or at `as_of` when given."""
    b = ledger.balance(conn, as_of)
    if b is None:
        return AccountBalance(success=False, message=ledger.OPEN_HINT)
    return AccountBalance(
        success=True,
        message=(f"Spendable {ledger.money(b.spendable_cents)} as of {b.as_of:%Y-%m-%d %H:%M} UTC: balance "
                 f"{ledger.money(b.balance_cents)} less {ledger.money(b.tax_owed_cents)} of sales tax still owed "
                 f"({ledger.money(b.takings.tax_cents)} set aside, {ledger.money(b.tax_remitted_cents)} remitted). "
                 f"Revenue includes tax; it is counted from the opening on {b.opening_at:%Y-%m-%d}."),
        count=1,
        as_of=b.as_of,
        opening_at=b.opening_at,
        opening=ledger.money(b.opening_cents),
        capital=ledger.money(b.capital_cents),
        revenue=ledger.money(b.takings.cents),
        receipts=ledger.money(b.receipts_cents),
        visit_fees=ledger.money(b.visit_fees_cents),
        visits=b.visits,
        token_spend=ledger.money(b.token_spend_cents),
        tax_remitted=ledger.money(b.tax_remitted_cents),
        adjustments=ledger.money(b.adjustments_cents),
        balance=ledger.money(b.balance_cents),
        tax_set_aside=ledger.money(b.takings.tax_cents),
        tax_owed=ledger.money(b.tax_owed_cents),
        spendable=ledger.money(b.spendable_cents),
        notes=list(b.notes),
    )
