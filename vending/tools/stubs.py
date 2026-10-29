"""Tools that are registered but not built yet. Each says what it is waiting on.

When you build one, give it its own module like `machine_state.py`, then point
its registration in `vending.server` at it. Write tools return an action that
`vending.server` runs through the write gate; keep that shape.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

import psycopg

from vending.tools import NotBuilt, Reply, WriteReply
from vending.tools.desired_state import DesiredState

Action = Callable[[psycopg.Connection], WriteReply]


def get_catalog(conn: psycopg.Connection, search: str | None) -> Reply:
    raise NotBuilt("get_catalog", "needs the catalog sync from /products and our sourcing table")


def get_account_balance(conn: psycopg.Connection) -> Reply:
    raise NotBuilt("get_account_balance", "needs the ledger")


def preview_desired_state(conn: psycopg.Connection, desired: DesiredState) -> Reply:
    raise NotBuilt("preview_desired_state", "needs the restock planner, plan_restock")


def get_purchase_requests(conn: psycopg.Connection, pending_only: bool) -> Reply:
    raise NotBuilt("get_purchase_requests", "needs the purchase-request sync")


def get_pnl(conn: psycopg.Connection, start: date | None, end: date | None) -> Reply:
    raise NotBuilt("get_pnl", "needs the ledger")


def submit_desired_state(desired: DesiredState) -> Action:
    def action(conn: psycopg.Connection) -> WriteReply:
        raise NotBuilt("submit_desired_state", "waiting on David's purchase-request schema")
    return action


def set_price(slot: str, sale_price: str) -> Action:
    def action(conn: psycopg.Connection) -> WriteReply:
        raise NotBuilt("set_price", "no price-change route; waiting on the Cantaloupe check")
    return action


def request_catalog_product(name: str, size: str, line: str | None, flavor: str | None,
                            note: str | None) -> Action:
    def action(conn: psycopg.Connection) -> WriteReply:
        raise NotBuilt("request_catalog_product", "waiting on the catalog-request routes")
    return action
