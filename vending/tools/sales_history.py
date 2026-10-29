"""`get_sales_history` (tool #2): units and ex-tax revenue per slot over a time window.

Registration point only. The `vending-tools` work owns this tool's logic:
replace the body of `get_sales_history` below, keep its signature and return
type, and `vending.server` serves it with no other change. `SalesHistory`'s
fields are a starting point; change them with the logic.
"""

from __future__ import annotations

from datetime import datetime

import psycopg
from pydantic import BaseModel

from vending.tools import NotBuilt, Reply


class SlotSales(BaseModel):
    slot: str
    units: int
    revenue_ex_tax: str


class SalesHistory(Reply):
    start: datetime | None = None
    end: datetime | None = None
    data: list[SlotSales] = []


def get_sales_history(conn: psycopg.Connection, start: datetime, end: datetime,
                      slots: list[str] | None) -> SalesHistory:
    raise NotBuilt("get_sales_history", "being built separately on top of the history sync")
