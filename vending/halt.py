"""The halt switch. Works without the agent: it only touches the database.

    python -m vending.halt status
    python -m vending.halt on --by kevin     # every write tool refuses from now on
    python -m vending.halt off --by kevin

`--by` is required to flip it: on a shared VM everyone is the same OS user, so
the name is what makes the log say who did it.

A second, harder stop needs nothing at all: stop the MCP server process, or
remove it from the agent's MCP config.
"""

from __future__ import annotations

import argparse
import sys

import psycopg

from vending import db
from vending.config import is_halted, set_halted

WAIT_NOTICE_MS = 2000


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m vending.halt", description="The halt switch.")
    parser.add_argument("action", choices=["status", "on", "off"])
    parser.add_argument("--by", help="your name, for the log (required for on and off)")
    args = parser.parse_args(argv)
    if args.action != "status" and not (args.by or "").strip():
        parser.error("--by NAME is required to turn the halt switch on or off")
    with db.connect() as conn:
        db.migrate(conn)
        if args.action != "status":
            flip(conn, args.action == "on", args.by.strip())
        print("halted" if is_halted(conn) else "running: writes allowed")
    return 0


def flip(conn: psycopg.Connection, halted: bool, who: str) -> None:
    """Flip the flag. If a write is in flight, say so instead of hanging silently."""
    try:
        set_halted(conn, halted, who, lock_timeout_ms=WAIT_NOTICE_MS)
    except psycopg.errors.LockNotAvailable:
        print("waiting for an in-flight write to finish...", flush=True)
        set_halted(conn, halted, who)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
