"""Tools that are registered but not built, or dropped. Each says what it is waiting on, or why.

When you build one, give it its own module like `machine_state.py`, then point
its registration in `vending.server` at it. Write tools return an action that
`vending.server` runs through the write gate; keep that shape.
"""

from __future__ import annotations

from collections.abc import Callable

import psycopg

from vending.tools import Dropped, WriteReply

Action = Callable[[psycopg.Connection], WriteReply]


def set_price(slot: str, sale_price: str) -> Action:
    def action(conn: psycopg.Connection) -> WriteReply:
        raise Dropped("set_price", "prices can't change remotely. Set sale_price on that slot in submit_desired_state; the worker changes the tag at the Friday visit (free), or at a midweek visit if you set off_cycle: true ($25)")
    return action

