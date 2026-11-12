"""Which selections are ours: read once from the inventory server, then from our database.

    python -m vending.slots              # show the stored assignment
    python -m vending.slots record       # read GET /api/v1/slots and store it (or check it)

The project brief (section 1) says the assignment comes from the inventory
server, once, and is fixed for both quarters. `record` reads the bridge's
`GET /api/v1/slots` and stores the answer in `slot_assignment`. Run it again any
time: a matching answer only updates when it was last checked, and a different
answer is refused and changes nothing, because the assignment isn't supposed to
change and a person should look before anything switches.

Every tool that reads the database takes our slots from `our_slots(conn)`. With
nothing stored it refuses and says to run `record`, rather than guessing.

`SLOTS` is the assignment the bridge returned on 12 Nov 2026 (team `group-b`:
columns 5-8 of every row on `vmc1`). Only code with no database uses it: the
planner's default, the week replayer's synthetic test cases and the pure
helpers' defaults. `DEVICE_ID` is the machine we read polls from; `record`
refuses an answer that names another one.

`record` never migrates. On a database that is behind it refuses (exit 2), like
the syncs.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # the constants are imported by pure code that runs without psycopg
    import psycopg

DEVICE_ID = "vmc1"

SLOTS: tuple[str, ...] = tuple(f"{row}{col}" for row in range(1, 6) for col in range(5, 9))

RECORD_HINT = ("Our slot assignment hasn't been read from the inventory server yet, so nothing can tell which "
               "selections are ours. A person runs: python -m vending.slots record")


class SlotsNotRecorded(RuntimeError):
    """No assignment is stored yet. The message says what to run."""

    def __init__(self) -> None:
        super().__init__(RECORD_HINT)


class BadAssignment(ValueError):
    """The bridge's answer can't be used as our assignment, or differs from the one stored."""


@dataclass(frozen=True)
class Assignment:
    team: str | None
    device_id: str
    slots: tuple[str, ...]


def parse(answer: Any) -> Assignment:
    """Our assignment from a `GET /api/v1/slots` answer. Refuses anything it can't trust."""
    if not isinstance(answer, dict):
        raise BadAssignment(f"the answer isn't a JSON object: {str(answer)[:200]}")
    device, team, slots = answer.get("device_id"), answer.get("team"), answer.get("slots")
    if not isinstance(device, str) or not device.strip():
        raise BadAssignment("the answer names no device_id")
    if device != DEVICE_ID:
        raise BadAssignment(f"the answer puts our slots on {device!r}, but our polls are read from {DEVICE_ID!r} "
                            "(vending.slots.DEVICE_ID); change that first if the machine really changed")
    if not isinstance(slots, list) or not slots:
        raise BadAssignment("the answer lists no slots")
    if not all(isinstance(s, str) and s.strip() and s == s.strip() for s in slots):
        raise BadAssignment(f"every slot must be a non-empty name: {slots!r}"[:300])
    if len(set(slots)) != len(slots):
        raise BadAssignment(f"a slot is listed twice: {slots!r}"[:300])
    return Assignment(team if isinstance(team, str) else None, device, tuple(slots))


def stored(conn: psycopg.Connection) -> Assignment | None:
    row = conn.execute("select team, device_id, slots from slot_assignment where id = 1").fetchone()
    return None if row is None else Assignment(row[0], row[1], tuple(row[2]))


def our_slots(conn: psycopg.Connection) -> tuple[str, ...]:
    """Our selections, from the stored assignment, in the order the server listed them.

    Raises `SlotsNotRecorded` when nothing is stored: a guess here would point
    every tool, the planner and the ledger at the wrong selections.
    """
    found = stored(conn)
    if found is None:
        raise SlotsNotRecorded()
    return found.slots


def record(conn: psycopg.Connection, answer: Any, now: datetime | None = None, by: str = "vending.slots") -> str:
    """Store the bridge's answer as our assignment, or check it against the one stored.

    Returns "recorded", "unchanged" or "team renamed". Raises `BadAssignment` for
    an answer that can't be used or whose device or slots differ from what is
    stored; nothing changes then. Only the device and the slots are the
    assignment: nothing reads `team`, so a new label alone is stored, not refused.
    """
    from psycopg.types.json import Jsonb

    new = parse(answer)
    with conn.transaction():
        old = conn.execute("select team, device_id, slots from slot_assignment where id = 1 for update").fetchone()
        if old is None:
            conn.execute(
                "insert into slot_assignment (team, device_id, slots, first_read_at, last_checked_at, response)"
                " values (%s, %s, %s, coalesce(%s, now()), coalesce(%s, now()), %s)",
                (new.team, new.device_id, list(new.slots), now, now, Jsonb(answer)))
            outcome = "recorded"
        else:
            before = Assignment(old[0], old[1], tuple(old[2]))
            if (before.device_id, set(before.slots)) != (new.device_id, set(new.slots)):
                raise BadAssignment(
                    "the inventory server now gives a different assignment, and the brief says it doesn't "
                    f"change. Stored: team {before.team}, {before.device_id} {', '.join(before.slots)}. "
                    f"Now: team {new.team}, {new.device_id} {', '.join(new.slots)}. Nothing was changed; "
                    "ask the instructors which is right before anyone acts on it")
            if before.team == new.team:
                conn.execute("update slot_assignment set last_checked_at = coalesce(%s, now()) where id = 1", (now,))
                outcome = "unchanged"
            else:
                conn.execute("update slot_assignment set team = %s, response = %s, last_checked_at = coalesce(%s, now())"
                             " where id = 1", (new.team, Jsonb(answer), now))
                outcome = "team renamed"
        conn.execute("insert into action_log (kind, tool, arguments, outcome, detail) values ('write', %s, %s, 'done', %s)",
                     ("slots.record", Jsonb({"by": by}),
                      f"slot assignment {outcome}: {new.device_id} {', '.join(new.slots)}"
                      + (f" (team {before.team} -> {new.team})" if outcome == "team renamed" else "")))
    return outcome


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vending.slots", description=__doc__.splitlines()[0])
    parser.add_argument("action", nargs="?", choices=["show", "record"], default="show")
    args = parser.parse_args(argv)

    from vending import db
    from vending.bridge import Bridge, BridgeError

    with db.connect() as conn:
        behind = db.pending(conn)
        if behind:
            print(f"The database is missing migrations: {', '.join(behind)}. Check DATABASE_URL points where you "
                  "mean, then run: python -m vending.db migrate", file=sys.stderr)
            return 2
        if args.action == "show":
            found = stored(conn)
            if found is None:
                print(RECORD_HINT)
                return 1
            print(f"team {found.team}, device {found.device_id}, {len(found.slots)} slots: {', '.join(found.slots)}")
            return 0
        try:
            answer = Bridge().slots()
        except BridgeError as error:
            print(f"bridge error: {error}", file=sys.stderr)
            return 3
        try:
            outcome = record(conn, answer)
        except BadAssignment as error:
            print(f"refused: {error}", file=sys.stderr)
            return 1
        found = stored(conn)
        print(f"{outcome}: team {found.team}, device {found.device_id}, {len(found.slots)} slots: "
              f"{', '.join(found.slots)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
