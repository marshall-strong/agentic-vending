"""Tools that are registered but not built yet. Each says what it is waiting on.

When you build one, give it its own module like `machine_state.py`, then point
its registration in `vending.server` at it. Write tools return an action that
`vending.server` runs through the write gate; keep that shape.
"""

from __future__ import annotations

from collections.abc import Callable

import psycopg

from vending.tools import NotBuilt, WriteReply

Action = Callable[[psycopg.Connection], WriteReply]


def set_price(slot: str, sale_price: str) -> Action:
    def action(conn: psycopg.Connection) -> WriteReply:
        raise NotBuilt("set_price", "no price-change route; waiting on the Cantaloupe check")
    return action

