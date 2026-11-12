"""The one gate every write tool goes through.

    result = run_write(conn, "submit_desired_state", arguments, action)

In one transaction it checks the halt flag (holding a lock on it), runs
`action(conn)`, and logs the attempt to `action_log`. The write and its log row
commit together or not at all. A refusal or a crash rolls the write back and is
then logged on its own, so every attempt leaves a row, including refused ones.

`action` refuses by raising `Refused` with every reason at once, so the agent
can fix a plan in one turn. Anything else it raises is logged as `failed` and
re-raised; if that log row can't be written either, the original error still
reaches the agent.

A write that calls the bridge can't be rolled back, and the bridge has no
route that lists our requests, so a transaction around the call isn't enough.
`submit_desired_state` commits a `purchase_submissions` row on its own
connection before it sends, and settles that row after (`vending.submissions`).
"""

from __future__ import annotations

import sys
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
        _log_after(conn, tool, arguments, "refused", str(refusal))
        raise
    except Exception as error:
        _log_after(conn, tool, arguments, "failed", f"{type(error).__name__}: {error}")
        raise


def _log_after(conn: psycopg.Connection, tool: str, arguments: dict[str, Any], outcome: str, detail: str) -> None:
    """Log a write that didn't go through, without hiding why it didn't.

    If the database dropped, logging fails too, and that error must not replace
    the one the agent needs to see (an accepted request's ID, say). It goes to
    stderr instead, which the server's log keeps.
    """
    try:
        with conn.transaction():
            log(conn, "write", tool, arguments, outcome, detail)
    except psycopg.Error as error:
        print(f"ACTION LOG LOST {tool} {outcome}: {detail} ({type(error).__name__}: {error})",
              file=sys.stderr, flush=True)


def log(conn: psycopg.Connection, kind: str, tool: str, arguments: dict[str, Any], outcome: str,
        detail: str | None, result: BaseModel | None = None) -> None:
    """One `action_log` row. Reads use this directly; writes only through `run_write`."""
    conn.execute(
        "insert into action_log (kind, tool, arguments, outcome, detail, result)"
        " values (%s, %s, %s, %s, %s, %s)",
        (kind, tool, Jsonb(_no_nul(arguments)), outcome, _no_nul(detail),
         None if result is None else Jsonb(_no_nul(result.model_dump(mode="json")))),
    )


def _no_nul(value: Any) -> Any:
    """Write a NUL character as the text `\\x00`. Postgres text and jsonb refuse NUL,
    and an argument the agent sends must not stop its own log row."""
    if isinstance(value, str):
        return value.replace("\x00", "\\x00")
    if isinstance(value, dict):
        return {_no_nul(k): _no_nul(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_no_nul(v) for v in value]
    return value
