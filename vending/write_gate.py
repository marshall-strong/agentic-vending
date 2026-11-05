"""The one gate every write tool goes through.

    result = run_write(conn, "submit_desired_state", arguments, action)

In one transaction it checks the halt flag (holding a lock on it), runs
`action(conn)`, and logs the attempt to `action_log`. The write and its log row
commit together or not at all. A refusal or a crash rolls the write back and is
then logged on its own, so every attempt leaves a row, including refused ones.

`action` refuses by raising `Refused` with every reason at once, so the agent
can fix a plan in one turn. Anything else it raises is logged as `failed` and
re-raised.

Not covered here yet: a write that calls the bridge cannot be rolled back, and
the bridge has no route that lists our requests. When `submit_desired_state` is
built, it must commit a row saying what it is about to send before the call,
then update that row with the request ID after it. A transaction around the
call isn't enough: a crash before the commit loses the ID, and a retry could
order twice.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from vending.config import is_halted
from vending.pricing import Refusal

T = TypeVar("T", bound=BaseModel)


class Refused(Exception):
    """A write that broke a rule. Carries every refusal found, not just the first."""

    def __init__(self, refusals: list[Refusal]):
        super().__init__("; ".join(f"{r.code}: {r.message}" for r in refusals))
        self.refusals = refusals


HALTED = Refusal("halted", "the halt switch is on; nothing can be written until a person turns it off")


def run_write(conn: psycopg.Connection, tool: str, arguments: dict[str, Any],
              action: Callable[[psycopg.Connection], T]) -> T:
    """Run one write behind the halt check, logged. Raises `Refused` on a refusal."""
    try:
        with conn.transaction():
            if is_halted(conn, lock=True):
                raise Refused([HALTED])
            result = action(conn)
            log(conn, "write", tool, arguments, "done" if result.success else "failed",
                None if result.success else result.message, result)
        return result
    except Refused as refusal:
        with conn.transaction():
            log(conn, "write", tool, arguments, "refused", str(refusal))
        raise
    except Exception as error:
        with conn.transaction():
            log(conn, "write", tool, arguments, "failed", f"{type(error).__name__}: {error}")
        raise


def log(conn: psycopg.Connection, kind: str, tool: str, arguments: dict[str, Any], outcome: str,
        detail: str | None, result: BaseModel | None = None) -> None:
    """One `action_log` row. Reads use this directly; writes only through `run_write`."""
    conn.execute(
        "insert into action_log (kind, tool, arguments, outcome, detail, result)"
        " values (%s, %s, %s, %s, %s, %s)",
        (kind, tool, Jsonb(arguments), outcome, detail,
         None if result is None else Jsonb(result.model_dump(mode="json"))),
    )
