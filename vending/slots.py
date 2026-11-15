"""Which selections are ours: read from the inventory server, then from our database.

    python -m vending.slots              # show the stored assignment
    python -m vending.slots record       # read GET /api/v1/slots and store it (or check it)

The inventory server assigns each team its selections (brief, section 1).
`record` reads the bridge's `GET /api/v1/slots` and stores the answer in
`slot_assignment`. The worker runs it every round (hourly), and a person can run
it any time.

The instructors may still change our assignment until the competition starts
(Thu 3 Dec 2026). After that it is fixed (assumption A16). So `record` follows
the server until the lock time, `SLOTS_LOCK_AT`, and holds what it has from then:

 - Before the lock time, a well-formed answer that differs from the stored one
   replaces it, and `action_log` says what it was and what it is now.
 - From the lock time, a different answer is refused (exit 1) and nothing
   changes, so a person looks before anything switches.
 - At any time, the same answer only moves `last_checked_at`, and a new team
   label alone is stored.

`SLOTS_LOCK_AT` is an ISO 8601 time with an offset. It defaults to the start,
midnight Eastern on 3 Dec 2026 (`DEFAULT_LOCK_AT`).

A malformed answer is refused at any time, and so is a malformed stored row (one
written by hand, say). An assignment must name `DEVICE_ID` and exactly
`SLOT_COUNT` (20) different selections, each a shelf 1-5 then a column 1-8 (11
to 58, never x0 or x9). The machine is split evenly between the two teams, so a
garbled answer can't widen what the tools read to the whole machine.

Every tool that reads the database takes our slots from `our_slots(conn)`. With
nothing stored it refuses and says to run `record`, rather than guessing.

`SLOTS` is the assignment the bridge returned on 12 Nov 2026 (team `group-b`:
columns 5-8 of every row on `vmc1`). Code with no database uses it directly: the
planner's default, the week replayer's synthetic test cases and the pure
helpers' defaults. It no longer bounds what `record` stores or what `our_slots`
returns. Some readers still check against it (the dashboard, the DB explorer and
the sales analysis; issue #75). `DEVICE_ID` is the machine we read polls from.

`record` never migrates. On a database that is behind it refuses (exit 2), like
the syncs.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # the constants are imported by pure code that runs without psycopg
    import psycopg

DEVICE_ID = "vmc1"

SLOTS: tuple[str, ...] = tuple(f"{row}{col}" for row in range(1, 6) for col in range(5, 9))

# Half of the machine's 5 shelves of 8: every assignment names exactly this many selections.
SLOT_COUNT = 20
SHELVES, COLUMNS = "12345", "12345678"

LOCK_SETTING = "SLOTS_LOCK_AT"
# The competition starts on Thu 3 Dec 2026. From then the assignment is fixed.
DEFAULT_LOCK_AT = "2026-12-03T00:00:00-05:00"

RECORD_HINT = ("Our slot assignment hasn't been read from the inventory server yet, so nothing can tell which "
               "selections are ours. A person runs: python -m vending.slots record")


class SlotsNotRecorded(RuntimeError):
    """No usable assignment is stored: none yet, or a malformed one.
    The message says what to do, and every tool that reads slots refuses with it."""

    def __init__(self, message: str = RECORD_HINT) -> None:
        super().__init__(message)


class BadAssignment(ValueError):
    """The bridge's answer can't be used as our assignment, or differs from the stored one after the lock."""


@dataclass(frozen=True)
class Assignment:
    team: str | None
    device_id: str
    slots: tuple[str, ...]


def problems(device_id: Any, slots: Any) -> list[str]:
    """Every reason `device_id` and `slots` can't be our assignment. Empty when they can."""
    found = []
    if device_id != DEVICE_ID:
        found.append(f"it puts our slots on {device_id!r}, but our polls are read from {DEVICE_ID!r} "
                     "(vending.slots.DEVICE_ID); change that first if the machine really changed")
    if not isinstance(slots, (list, tuple)) or not slots:
        return found + ["it lists no slots"]
    if not all(isinstance(s, str) and s.strip() and s == s.strip() for s in slots):
        return found + [f"every slot must be a non-empty name: {list(slots)!r}"[:300]]
    twice = sorted({s for s in slots if slots.count(s) > 1})
    if twice:
        found.append(f"a slot is listed twice: {', '.join(twice)}")
    bad = [s for s in slots if not (len(s) == 2 and s[0] in SHELVES and s[1] in COLUMNS)]
    if bad:
        found.append(f"not a selection (shelf 1-5 then column 1-8, like 15): {', '.join(bad)}"[:300])
    if len(set(slots)) != SLOT_COUNT:
        found.append(f"it names {len(set(slots))} different selections, not {SLOT_COUNT} (half the machine)")
    return found


def parse(answer: Any) -> Assignment:
    """Our assignment from a `GET /api/v1/slots` answer. Refuses anything it can't trust."""
    if not isinstance(answer, dict):
        raise BadAssignment(f"the answer isn't a JSON object: {str(answer)[:200]}")
    device, team, slots = answer.get("device_id"), answer.get("team"), answer.get("slots")
    if not isinstance(device, str) or not device.strip():
        raise BadAssignment("the answer names no device_id")
    wrong = problems(device, slots)
    if wrong:
        raise BadAssignment("the answer can't be our assignment: " + "; ".join(wrong) + ". Nothing was changed")
    return Assignment(team if isinstance(team, str) else None, device, tuple(slots))


def lock_at() -> datetime:
    """When the assignment locks: `SLOTS_LOCK_AT`, else the competition's start."""
    from vending import settings

    value = settings.get(LOCK_SETTING) or DEFAULT_LOCK_AT
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        moment = None
    if moment is None or moment.utcoffset() is None:
        raise settings.SettingsError(f"{LOCK_SETTING} is {value!r}; give a time with its offset, like {DEFAULT_LOCK_AT}")
    return moment


def stored(conn: psycopg.Connection) -> Assignment | None:
    row = conn.execute("select team, device_id, slots from slot_assignment where id = 1").fetchone()
    return None if row is None else Assignment(row[0], row[1], tuple(row[2]))


def our_slots(conn: psycopg.Connection) -> tuple[str, ...]:
    """Our selections, from the stored assignment, in the order the server listed them.

    Raises `SlotsNotRecorded` when nothing is stored: a guess here would point
    every tool, the planner and the ledger at the wrong selections. It also
    refuses a stored row that isn't a well-formed assignment (the checks `record`
    makes), so a row written by hand can't point the tools at the whole machine
    or at a selection that doesn't exist.
    """
    found = stored(conn)
    if found is None:
        raise SlotsNotRecorded()
    wrong = problems(found.device_id, list(found.slots))
    if wrong:
        raise SlotsNotRecorded(
            "The stored slot assignment can't be ours: " + "; ".join(wrong) + ". Nothing reads it until a person "
            "checks. Clear slot_assignment, then run: python -m vending.slots record")
    return found.slots


def _describe(a: Assignment) -> str:
    return f"team {a.team}, {a.device_id} {', '.join(a.slots)}"


def record(conn: psycopg.Connection, answer: Any, now: datetime | None = None, by: str = "vending.slots",
           lock: datetime | None = None) -> str:
    """Store the bridge's answer as our assignment, or check it against the one stored.

    Returns "recorded", "unchanged", "team renamed" or "changed". Before `lock`
    (default `lock_at()`), an answer whose device or slots differ from the stored
    ones replaces them ("changed"), and the log says old -> new. From `lock` on
    it raises `BadAssignment` and nothing changes. A malformed answer raises
    `BadAssignment` at any time. Only the device and the slots are the
    assignment: nothing reads `team`, so a new label alone is stored, not refused.
    """
    from psycopg.types.json import Jsonb

    new = parse(answer)
    lock = lock or lock_at()
    at = now or datetime.now(timezone.utc)
    note = ""
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
                if at >= lock:
                    raise BadAssignment(
                        f"the inventory server now gives a different assignment, but ours locked at "
                        f"{lock.isoformat()} ({LOCK_SETTING}). Stored: {_describe(before)}. Now: {_describe(new)}. "
                        "Nothing was changed; ask the instructors which is right before anyone acts on it")
                # first_read_at becomes when this assignment was first read; the log keeps the old one.
                conn.execute("update slot_assignment set team = %s, device_id = %s, slots = %s, response = %s,"
                             " first_read_at = coalesce(%s, now()), last_checked_at = coalesce(%s, now())"
                             " where id = 1",
                             (new.team, new.device_id, list(new.slots), Jsonb(answer), now, now))
                outcome = "changed"
                note = f" (was {_describe(before)}; locks at {lock.isoformat()})"
            elif before.team == new.team:
                conn.execute("update slot_assignment set last_checked_at = coalesce(%s, now()) where id = 1", (now,))
                outcome = "unchanged"
            else:
                conn.execute("update slot_assignment set team = %s, response = %s, last_checked_at = coalesce(%s, now())"
                             " where id = 1", (new.team, Jsonb(answer), now))
                outcome = "team renamed"
                note = f" (team {before.team} -> {new.team})"
        conn.execute("insert into action_log (kind, tool, arguments, outcome, detail) values ('write', %s, %s, 'done', %s)",
                     ("slots.record", Jsonb({"by": by}),
                      f"slot assignment {outcome}: {new.device_id} {', '.join(new.slots)}{note}"))
    return outcome


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vending.slots", description=__doc__.splitlines()[0])
    parser.add_argument("action", nargs="?", choices=["show", "record"], default="show")
    args = parser.parse_args(argv)

    from vending import db, settings
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
            lock = lock_at()
        except settings.SettingsError as error:
            print(f"refused: {error}", file=sys.stderr)
            return 1
        try:
            answer = Bridge().slots()
        except BridgeError as error:
            print(f"bridge error: {error}", file=sys.stderr)
            return 3
        try:
            outcome = record(conn, answer, lock=lock)
        except BadAssignment as error:
            print(f"refused: {error}", file=sys.stderr)
            return 1
        found = stored(conn)
        print(f"{outcome}: team {found.team}, device {found.device_id}, {len(found.slots)} slots: "
              f"{', '.join(found.slots)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
