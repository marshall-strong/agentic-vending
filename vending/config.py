"""The `config` table: settings the code reads at run time, starting with the halt flag.

The halt flag fails closed: a missing row, or anything that isn't JSON
`false`, counts as halted.
"""

from __future__ import annotations

import psycopg
from psycopg.types.json import Jsonb


def is_halted(conn: psycopg.Connection, *, lock: bool = False) -> bool:
    """True unless the `halted` row holds exactly `false`.

    `lock=True` takes a row lock until the surrounding transaction ends, so a
    write that saw "not halted" finishes before a halt can land, and a halt
    that landed first blocks every write that starts after it.
    """
    sql = "select value from config where key = 'halted'"
    row = conn.execute(sql + (" for update" if lock else "")).fetchone()
    return row is None or row[0] is not False


def set_halted(conn: psycopg.Connection, halted: bool, who: str) -> None:
    """Flip the halt flag, and log the flip like any other write."""
    with conn.transaction():
        conn.execute(
            "insert into config (key, value, updated_by) values ('halted', %s, %s)"
            " on conflict (key) do update set value = excluded.value,"
            " updated_at = now(), updated_by = excluded.updated_by",
            (Jsonb(halted), who),
        )
        conn.execute(
            "insert into action_log (kind, tool, arguments, outcome, detail)"
            " values ('write', 'halt', %s, 'done', %s)",
            (Jsonb({"halted": halted}), f"halt turned {'on' if halted else 'off'} by {who}"),
        )
