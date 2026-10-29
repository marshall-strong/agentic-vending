"""The halt switch. Works without the agent: it only touches the database.

    python -m vending.halt status
    python -m vending.halt on      # every write tool refuses from now on
    python -m vending.halt off

A second, harder stop needs nothing at all: stop the MCP server process, or
remove it from the agent's MCP config.
"""

from __future__ import annotations

import getpass
import sys

from vending import db
from vending.config import is_halted, set_halted


def main(argv: list[str]) -> int:
    if argv not in (["status"], ["on"], ["off"]):
        print("usage: python -m vending.halt status|on|off", file=sys.stderr)
        return 2
    with db.connect() as conn:
        db.migrate(conn)
        if argv[0] != "status":
            set_halted(conn, argv[0] == "on", getpass.getuser())
        print("halted" if is_halted(conn) else "running: writes allowed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
