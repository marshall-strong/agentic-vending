"""Which selections are ours: read from the inventory server, then from our database.

    python -m vending.slots                    # show the stored assignment
    python -m vending.slots record             # a person: read GET /api/v1/slots and store it (or check it)
    python -m vending.slots record --replace   # a person: replace an assignment already confirmed at the start
    python -m vending.slots check              # the worker: confirm the server still gives the stored one
    python -m vending.slots confirm            # a person, after the start: confirm the stored one without the server

The inventory server assigns each team its selections (brief, section 1).
`record` reads the bridge's `GET /api/v1/slots` and stores the answer in
`slot_assignment`. Every tool that reads the database takes our slots from that row.

The instructors change the assignment exactly once, through the server, and
that change is the assignment the competition uses (David, 16 Nov; assumption
A16). Nobody knows when, so our slots follow the server until the start, the
lock time `SLOTS_LOCK_AT`, and hold from then. No program takes in a change on
its own:

 - **Only a person stores or changes the assignment** (`record`). Before the
   lock, a well-formed answer that differs from the stored one replaces it, and
   `action_log` says what it was and what it is now. A person who runs `record`
   at or after the lock, while nothing has confirmed the stored one since the
   lock, stores the server's answer then, and that is the assignment from then on.
 - **The worker only confirms** (`check`, every round). The same answer moves
   `last_checked_at`; the first such check at or after the lock confirms the
   assignment for the competition. A different answer, or nothing stored,
   changes nothing and exits 1, so the worker's round fails (`/health` 503)
   until a person looks and runs `record`.
 - Once an assignment is confirmed at or after the lock, `record` refuses a
   different answer (exit 1). If the confirmed one turns out to be wrong, a
   person who has checked with the instructors runs `record --replace`.
 - If the bridge is down at the start, a person runs `confirm`: it confirms the
   stored assignment without the server. Until something confirms it, every
   reader refuses (`our_slots`), so nothing acts on a pre-start answer after
   the start. The worker also runs a round just after the lock, so the readers
   don't wait for the next hourly round.

A check counts from when its request to the server started, so an answer asked
for before the lock and delivered after it is still a pre-start answer and
confirms nothing. Times come from the database clock (`clock`), not the host's,
as in `vending.ledger.db_now`.

`SLOTS_LOCK_AT` is an ISO 8601 time with an offset. It defaults to the start,
midnight Eastern on Thu 3 Dec 2026 (`DEFAULT_LOCK_AT`): the first day of Winter
Quarter on the Institute's calendar, and the start David gave on 12 Nov.

Every answer must name our team, `SLOTS_TEAM` (default `group-b`). An answer
with no team, or another team's, is refused at any time, so a swapped key or a
misconfigured bridge can't hand us the other half of the machine. A stored row
for another team is refused by every reader.

A malformed answer is refused at any time, and so is a malformed stored row (one
written by hand, say). An assignment must name `DEVICE_ID` and exactly
`SLOT_COUNT` (20) different selections, each a shelf 1-5 then a column 1-8 (11
to 58, never x0 or x9). The machine is split evenly between the two teams, so a
garbled answer can't widen what the tools read to the whole machine.

With nothing stored, `our_slots` refuses and says to run `record`, rather than
guessing. There is no fixed list of our slots in the code: the assignment may
change until the lock time, so a list written down would go stale. Code that
takes slots as an argument is handed them from one call to `our_slots(conn)`.
Only the week replayer's made-up cases name selections of their own
(`vending.replay.cases.CASE_SLOTS`), and those are examples, not ours.

Migration 013's header says a differing read is refused; that was the first
design, and the rules above replace it (an applied migration isn't edited).
`last_checked_at` is `not null`, so every stored row has one.

`DEVICE_ID` is the machine we read polls from.

`record`, `check` and `confirm` never migrate. On a database that is behind they
refuse (exit 2), like the syncs.
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

# Half of the machine's 5 shelves of 8: every assignment names exactly this many selections.
SLOT_COUNT = 20
SHELVES, COLUMNS = "12345", "12345678"

LOCK_SETTING = "SLOTS_LOCK_AT"
# The competition starts on Thu 3 Dec 2026, the first day of Winter Quarter. From then the assignment is fixed.
DEFAULT_LOCK_AT = "2026-12-03T00:00:00-05:00"

TEAM_SETTING = "SLOTS_TEAM"
# Our team's name in the inventory server's answers. A name, not selections: those always come from the server.
DEFAULT_TEAM = "group-b"

RECORD_HINT = ("Our slot assignment hasn't been read from the inventory server yet, so nothing can tell which "
               "selections are ours. A person runs: python -m vending.slots record")
CHANGE_HINT = ("Nothing was changed. A person checks with the instructors which is right, then runs: "
               "python -m vending.slots record")


class SlotsNotRecorded(RuntimeError):
    """No usable assignment is stored: none yet, a malformed one, or one not confirmed since the start.
    The message says what to do, and every tool that reads slots refuses with it."""

    def __init__(self, message: str = RECORD_HINT) -> None:
        super().__init__(message)


class BadAssignment(ValueError):
    """The bridge's answer can't be used as our assignment, or can't replace the stored one without a person."""


@dataclass(frozen=True)
class Assignment:
    team: str | None
    device_id: str
    slots: tuple[str, ...]
    last_checked_at: datetime | None = None


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


def our_team() -> str:
    """Our team's name in the server's answers: `SLOTS_TEAM`, else `DEFAULT_TEAM`."""
    from vending import settings

    return settings.get(TEAM_SETTING) or DEFAULT_TEAM


def _team_problem(team: Any) -> str | None:
    ours = our_team()
    if not isinstance(team, str) or not team:
        return f"it names no team; ours is {ours!r} ({TEAM_SETTING})"
    if team != ours:
        return (f"it is for team {team!r}, but ours is {ours!r} ({TEAM_SETTING}); a swapped key or a "
                "misconfigured bridge can do this")
    return None


def parse(answer: Any) -> Assignment:
    """Our assignment from a `GET /api/v1/slots` answer. Refuses anything it can't trust, or another team's."""
    if not isinstance(answer, dict):
        raise BadAssignment(f"the answer isn't a JSON object: {str(answer)[:200]}")
    device, team, slots = answer.get("device_id"), answer.get("team"), answer.get("slots")
    if not isinstance(device, str) or not device.strip():
        raise BadAssignment("the answer names no device_id")
    wrong = problems(device, slots)
    team_wrong = _team_problem(team)
    if team_wrong:
        wrong.append(team_wrong)
    if wrong:
        raise BadAssignment("the answer can't be our assignment: " + "; ".join(wrong) + ". Nothing was changed")
    return Assignment(team, device, tuple(slots))


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


def clock(conn: psycopg.Connection) -> datetime:
    """Now by the database clock, in UTC, as `vending.ledger.db_now` (which imports this module, so can't be used).

    The lock is judged on this clock, not the host's, which can drift (a container, a laptop)."""
    return conn.execute("select clock_timestamp()").fetchone()[0].astimezone(timezone.utc)


def stored(conn: psycopg.Connection) -> Assignment | None:
    row = conn.execute("select team, device_id, slots, last_checked_at from slot_assignment where id = 1").fetchone()
    return None if row is None else Assignment(row[0], row[1], tuple(row[2]), row[3])


def _usable(found: Assignment | None) -> Assignment:
    """`found`, or `SlotsNotRecorded` when nothing is stored or the stored row can't be our assignment."""
    if found is None:
        raise SlotsNotRecorded()
    wrong = problems(found.device_id, list(found.slots))
    team_wrong = _team_problem(found.team)
    if team_wrong:
        wrong.append(team_wrong)
    if wrong:
        raise SlotsNotRecorded(
            "The stored slot assignment can't be ours: " + "; ".join(wrong) + ". Nothing reads it until a person "
            "checks. Clear slot_assignment, then run: python -m vending.slots record")
    return found


def our_slots(conn: psycopg.Connection, now: datetime | None = None) -> tuple[str, ...]:
    """Our selections, from the stored assignment, in the order the server listed them.

    Raises `SlotsNotRecorded` when nothing is stored: a guess here would point
    every tool, the planner and the ledger at the wrong selections. It also
    refuses a stored row that isn't a well-formed assignment for our team (the
    checks `record` makes), so a row written by hand can't point the tools at the
    whole machine, the other half or a selection that doesn't exist. From the
    lock on (`now`, default the database clock), it refuses until a check at or
    after the lock has confirmed the stored assignment.
    """
    from vending import settings

    found = _usable(stored(conn))
    try:
        lock = lock_at()
    except settings.SettingsError as error:
        raise SlotsNotRecorded(f"Our slots can't be read: {error}") from error
    if (now or clock(conn)) >= lock and found.last_checked_at < lock:
        raise SlotsNotRecorded(
            "The competition has started, but the final server assignment has not been confirmed. "
            "Before reading selections, run: python -m vending.slots check (or, if the bridge is down, "
            "a person runs: python -m vending.slots confirm)")
    return found.slots


def _describe(a: Assignment) -> str:
    return f"team {a.team}, {a.device_id} {', '.join(a.slots)}"


def _log(conn: psycopg.Connection, by: str, detail: str) -> None:
    from psycopg.types.json import Jsonb

    conn.execute("insert into action_log (kind, tool, arguments, outcome, detail) values ('write', %s, %s, 'done', %s)",
                 ("slots.record", Jsonb({"by": by}), detail))


def _store(conn: psycopg.Connection, answer: Any, at: datetime, lock: datetime, by: str, *,
           person: bool, replace: bool = False) -> str:
    from psycopg.types.json import Jsonb

    new = parse(answer)
    with conn.transaction():
        # Serializes recorders even while the table is empty, when FOR UPDATE has no row to lock.
        conn.execute("select pg_advisory_xact_lock(hashtext('vending.slot_assignment'))")
        old = conn.execute("select team, device_id, slots, last_checked_at from slot_assignment"
                           " where id = 1 for update").fetchone()
        if old is None:
            if not person:
                raise BadAssignment(f"no assignment is stored, and the worker only confirms one. {RECORD_HINT}")
            conn.execute(
                "insert into slot_assignment (team, device_id, slots, first_read_at, last_checked_at, response)"
                " values (%s, %s, %s, %s, %s, %s)",
                (new.team, new.device_id, list(new.slots), at, at, Jsonb(answer)))
            note = f" (the first check since the start at {lock.isoformat()})" if at >= lock else ""
            _log(conn, by, f"slot assignment recorded: {new.device_id} {', '.join(new.slots)}{note}")
            return "recorded"
        before = Assignment(old[0], old[1], tuple(old[2]), old[3])
        confirmed = before.last_checked_at >= lock
        if (before.team, before.device_id, set(before.slots)) == (new.team, new.device_id, set(new.slots)):
            conn.execute("update slot_assignment set last_checked_at = greatest(last_checked_at, %s) where id = 1",
                         (at,))
            # The worker checks every hour; logging each one would push the agent's own tool calls off the
            # dashboard. last_checked_at records the check. The first check at or after the start confirms the
            # assignment for good, so that one is logged.
            if not confirmed and at >= lock:
                _log(conn, by, f"slot assignment unchanged: {new.device_id} {', '.join(new.slots)} (confirmed by "
                               f"the first check since the start at {lock.isoformat()})")
            return "unchanged"
        if not person:
            raise BadAssignment(f"the inventory server now gives a different assignment. Stored: "
                                f"{_describe(before)}. Now: {_describe(new)}. The worker only confirms an "
                                f"unchanged one. {CHANGE_HINT}")
        if confirmed and not replace:
            raise BadAssignment(
                f"the inventory server now gives a different assignment, but ours locked at {lock.isoformat()} "
                f"({LOCK_SETTING}). Stored: {_describe(before)}. Now: {_describe(new)}. Nothing was changed. If "
                "the instructors say the new one is right, run: python -m vending.slots record --replace")
        if at < before.last_checked_at:
            raise BadAssignment("This server observation is older than the recorded assignment. Nothing was changed")
        # first_read_at becomes when this assignment was first read; the log keeps the old one.
        conn.execute("update slot_assignment set team = %s, device_id = %s, slots = %s, response = %s,"
                     " first_read_at = %s, last_checked_at = %s where id = 1",
                     (new.team, new.device_id, list(new.slots), Jsonb(answer), at, at))
        if confirmed:
            outcome, note = "replaced", (f" (was {_describe(before)}, confirmed since the start at "
                                         f"{lock.isoformat()}; replaced by a person with --replace)")
        elif at >= lock:
            outcome, note = "changed", (f" (was {_describe(before)}; the first check since the start at "
                                        f"{lock.isoformat()}, so this is the assignment from now on)")
        else:
            outcome, note = "changed", f" (was {_describe(before)}; locks at {lock.isoformat()})"
        _log(conn, by, f"slot assignment {outcome}: {new.device_id} {', '.join(new.slots)}{note}")
        return outcome


def record(conn: psycopg.Connection, answer: Any, now: datetime | None = None, by: str = "vending.slots",
           lock: datetime | None = None, *, replace: bool = False) -> str:
    """A person stores the bridge's answer as our assignment, or checks it against the one stored.

    Returns "recorded", "unchanged", "changed" or "replaced". `now` is when the
    request for `answer` started (default: the database clock now). Before `lock`
    (default `lock_at()`, the start), a different answer replaces the stored one
    ("changed"), and the log says old -> new. At or after the lock, while nothing
    has confirmed the stored one since the lock, a different answer replaces it
    too: that is the assignment at the start. Once one is confirmed, a different
    answer raises `BadAssignment` unless `replace`. A malformed answer, or one for
    another team, raises `BadAssignment` at any time, and so does an observation
    older than the stored one.
    """
    at = now or clock(conn)
    return _store(conn, answer, at, lock or lock_at(), by, person=True, replace=replace)


def check(conn: psycopg.Connection, answer: Any, now: datetime | None = None, by: str = "vending.worker",
          lock: datetime | None = None) -> str:
    """The worker confirms the stored assignment: "unchanged", or `BadAssignment` and nothing changes.

    The same answer moves `last_checked_at`, and the first time at or after the lock confirms the assignment.
    A different answer, or nothing stored, raises `BadAssignment`: only a person changes our slots (`record`)."""
    at = now or clock(conn)
    return _store(conn, answer, at, lock or lock_at(), by, person=False)


def confirm(conn: psycopg.Connection, now: datetime | None = None, by: str = "vending.slots",
            lock: datetime | None = None) -> str:
    """A person confirms the stored assignment after the start without asking the server (the bridge is down).

    Returns "confirmed" or "already confirmed". Refuses before the lock (there is nothing to confirm yet), and when
    nothing usable is stored."""
    at = now or clock(conn)
    lock = lock or lock_at()
    if at < lock:
        raise BadAssignment(f"the assignment locks at {lock.isoformat()} ({LOCK_SETTING}); before then there is "
                            "nothing to confirm. Nothing was changed")
    with conn.transaction():
        conn.execute("select pg_advisory_xact_lock(hashtext('vending.slot_assignment'))")
        try:
            found = _usable(stored(conn))
        except SlotsNotRecorded as error:
            raise BadAssignment(str(error)) from error
        if found.last_checked_at >= lock:
            return "already confirmed"
        conn.execute("update slot_assignment set last_checked_at = %s where id = 1", (at,))
        _log(conn, by, f"slot assignment confirmed without the server: {found.device_id} {', '.join(found.slots)} "
                       f"(by a person, after the start at {lock.isoformat()})")
    return "confirmed"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vending.slots", description=__doc__.splitlines()[0])
    parser.add_argument("action", nargs="?", choices=["show", "record", "check", "confirm"], default="show")
    parser.add_argument("--replace", action="store_true",
                        help="record only: replace an assignment already confirmed at the start (a person, after "
                             "checking with the instructors)")
    args = parser.parse_args(argv)
    if args.replace and args.action != "record":
        parser.error("--replace goes with record only")

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
            try:
                our_slots(conn)
            except SlotsNotRecorded as error:
                print(str(error), file=sys.stderr)
                return 1
            print(f"team {found.team}, device {found.device_id}, {len(found.slots)} slots: {', '.join(found.slots)}")
            return 0
        try:
            lock = lock_at()
        except settings.SettingsError as error:
            print(f"refused: {error}", file=sys.stderr)
            return 1
        if args.action == "confirm":
            try:
                outcome = confirm(conn, lock=lock)
            except BadAssignment as error:
                print(f"refused: {error}", file=sys.stderr)
                return 1
        else:
            # The answer is what the server said when we asked, so it is observed when the request starts. Taken
            # after it returns, an answer asked for before the lock and delivered after it would confirm a
            # pre-start assignment for good.
            asked_at = clock(conn)
            try:
                answer = Bridge().slots()
            except BridgeError as error:
                print(f"bridge error: {error}", file=sys.stderr)
                return 3
            try:
                if args.action == "check":
                    outcome = check(conn, answer, now=asked_at, lock=lock)
                else:
                    outcome = record(conn, answer, now=asked_at, lock=lock, replace=args.replace)
            except BadAssignment as error:
                print(f"refused: {error}", file=sys.stderr)
                return 1
        found = stored(conn)
        print(f"{outcome}: team {found.team}, device {found.device_id}, {len(found.slots)} slots: "
              f"{', '.join(found.slots)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
