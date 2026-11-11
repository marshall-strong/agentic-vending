"""Tools that are registered but not built yet. Each says what it is waiting on.

When you build one, give it its own module like `machine_state.py`, then point
its registration in `vending.server` at it. Write tools return an action that
`vending.server` runs through the write gate; keep that shape.
"""

from __future__ import annotations

from collections.abc import Callable

import psycopg

from vending.tools import Dropped, NotBuilt, Reply, WriteReply
from vending.tools.desired_state import DesiredState

Action = Callable[[psycopg.Connection], WriteReply]


def submit_desired_state(desired: DesiredState) -> Action:
    def action(conn: psycopg.Connection) -> WriteReply:
        raise NotBuilt("submit_desired_state", "needs plan_restock wired to the ledger and our request records")
    return action


def set_price(slot: str, sale_price: str) -> Action:
    def action(conn: psycopg.Connection) -> WriteReply:
        raise Dropped("set_price", "prices can't change remotely. Set sale_price on that slot in submit_desired_state; the worker changes the tag at the Friday visit (free), or at a midweek visit if you set off_cycle: true ($25)")
    return action

