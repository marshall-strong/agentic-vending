"""The record of every purchase request we tried to send, and how to settle one left open.

    python -m vending.submissions list
    python -m vending.submissions resolve 7 --request-id pr_abc123 --by kevin   # the bridge filed it
    python -m vending.submissions resolve 7 --not-filed --by kevin              # it never did
    python -m vending.submissions check                                         # see below

`submit_desired_state` commits a `sending` row here before it POSTs, keeps
the bridge's answer on it the moment it arrives (`save_answer`), and settles
it after, on the same connection. Only the bridge's documented `201`, whole,
settles a row by itself (`accepted`); a failure to connect settles it as
`not_filed`. Every other answer leaves it `unknown`: a `4xx` or `5xx`, even one
shaped like the bridge's refusal, a redirect, a body cut off, slow, encoded or
not plain JSON, or no answer. A row left `sending` (a crash in between) or
`unknown` may or may not exist on the bridge, and both the tool and
`preview_desired_state` refuse (`submission_unresolved`) until a person settles
it with `resolve`.

`check`, which a live agent run also does before it starts, settles only a row
whose POST got that validated `201` (`created_id`, saved before settling) but
was never settled (a crash or a database error in between). It reads
`GET /api/v1/purchase-requests/{created_id}`, and only an answer for that ID,
in the documented shape, settles the row as `accepted`. Anything else leaves
the row open, a `404` included. A request ID is only ever bound to one attempt:
one that another submission or a request we already recorded holds (`IdTaken`)
settles nothing, by `settle`, `check` or `resolve`. A row without `created_id`
always needs a person, even if its answer names a request: that answer wasn't
the bridge's acceptance, so the request it names may not be this attempt's.

To settle one, find out whether the bridge filed it. If a request ID is known
(the server log has a `LOST ANSWER` line, or the row's `response` holds one),
the bridge's Swagger page has `GET /api/v1/purchase-requests/{id}`; check that
its lines are this row's `body`. If no ID is known, the POST got a refusal, or
the lines don't match, **ask the instructors first**: the bridge has no route that lists our requests,
and a resend of a request that was filed would order twice. The request would
carry the lines in the row's `body`.

Resolving with a request ID also records the request in `purchase_requests`,
with the visit fee the plan quoted, so the request sync reads it back from then
on. `--by` is required: every change is logged in `action_log` with your name.
This never migrates, and the halt switch doesn't block it, because it's a
person correcting our records.

It does wait for a submit in flight. A submit holds the halt row's lock from
planning until it has settled its row, and `resolve` takes the same lock, so a
person's answer and the bridge's can't overwrite each other. `settle` also
refuses a row that is no longer `sending` (`AlreadySettled`), in case anything
else changed it.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from vending import db
from vending.config import is_halted
from vending.halt import WAIT_NOTICE_MS
from vending.pricing import Refusal
from vending.purchase_requests import record
from vending.write_gate import log

OPEN = ("sending", "unknown")
RESOLVE_HINT = ("a person must find out whether the bridge filed it (ask the instructors if no request ID is "
                "known), then run: python -m vending.submissions resolve {row} --request-id ID --by NAME, or "
                "--not-filed --by NAME")
AUTO_BY = "auto-check"     # `resolved_by` for a row `check` settled from the bridge's own answer
# The statuses `GET /api/v1/purchase-requests/{id}` documents (`PurchaseRequestDetail`).
REQUEST_STATUSES = frozenset({"pending", "rejected", "fulfilled"})


class IdTaken(ValueError):
    """The request ID is already another submission's, or a request we recorded: it can't be this attempt's."""

    def __init__(self, row: int, request_id: str, holder: str):
        super().__init__(
            f"the bridge named request {request_id} for submission {row}, but {holder} already holds that ID, so "
            "it isn't this attempt's request")
        self.row, self.request_id = row, request_id


def id_holder(conn: psycopg.Connection, row: int, request_id: str) -> str | None:
    """What holds `request_id` besides submission `row` ("submission 4", say), or None: another submission
    settled under it or whose POST got a validated `201` for it (`created_id`), or a request we recorded."""
    other = conn.execute("select id from purchase_submissions where (request_id = %s or created_id = %s)"
                         " and id <> %s order by id limit 1", (request_id, request_id, row)).fetchone()
    if other is not None:
        return f"submission {other[0]}"
    known = conn.execute("select status from purchase_requests where request_id = %s", (request_id,)).fetchone()
    return None if known is None else f"our recorded request ({known[0]})"


def id_taken(conn: psycopg.Connection, row: int, request_id: str) -> None:
    """Raise `IdTaken` if `request_id` is bound to anything but submission `row`."""
    holder = id_holder(conn, row, request_id)
    if holder is not None:
        raise IdTaken(row, request_id, holder)


class AlreadySettled(RuntimeError):
    """`settle` found its row no longer `sending`: something else settled it while the request was in flight."""

    def __init__(self, row: int, status: str, by: str | None):
        super().__init__(
            f"submission {row} was already settled as {status}{f' by {by}' if by else ''} while its request was "
            "in flight, so the bridge's answer was not recorded over it")
        self.row, self.status, self.by = row, status, by


def save_sending(side: psycopg.Connection, plan_hash: str, body: dict,
                 directive: str | None, visit_fee_cents: int | None = None, purpose: str | None = None,
                 schedule_due=None) -> int:
    """Commit a `sending` row on `side`, an autocommit connection of its own, so a crash after this can't lose it.

    `visit_fee_cents` is the fee the plan quoted for an off-cycle request (None
    otherwise). It is saved here, before the POST, so even a request settled
    later by `resolve` carries the fee that was agreed when it was sent.
    `purpose` is `fill` for the day-one fill (`vending.fill`), None for the
    agent's; `settle` and `resolve` keep it. `schedule_due` is the scheduled time whose run sends it
    (`vending.agent_schedule`), None for a run by hand or the fill.
    """
    if not side.autocommit:
        # In a transaction the row would only commit after the POST, which is what it guards against.
        raise ValueError("save_sending needs an autocommit connection")
    (row,) = side.execute(
        "insert into purchase_submissions (plan_hash, body, directive, visit_fee_cents, purpose, schedule_due)"
        " values (%s, %s, %s, %s, %s, %s) returning id",
        (plan_hash, Jsonb(body), directive, visit_fee_cents, purpose, schedule_due),
    ).fetchone()
    return row


def save_answer(side: psycopg.Connection, row: int, answer: object, *, created_id: str | None = None) -> None:
    """Keep the bridge's answer to the POST on a row still `sending`, before anything else is done with it.

    `created_id` is the request ID of a validated `201` (`submit.accepted`), and None for any other answer.
    It commits at once (`side` is autocommit), on its own, so the ID is on the row even if settling fails
    after: `check` looks up `created_id`, and only that. It never settles the row.
    """
    side.execute("update purchase_submissions set response = %s, created_id = %s where id = %s"
                 " and status = 'sending'", (None if answer is None else Jsonb(answer), created_id, row))


def settle(side: psycopg.Connection, row: int, status: str, *, request_id: str | None = None,
           response: object = None, detail: str | None = None, submitted_at: datetime | None = None) -> None:
    """Record how a submission ended, on the connection that saved it.

    `accepted` also records the request in `purchase_requests`, in the same
    transaction, so the ID can't be stored in one place and not the other.

    Only a row still `sending` is settled. Anything else raises `AlreadySettled`
    and leaves the row as it is, so a person's `resolve` is never overwritten.
    `accepted` under an ID already bound elsewhere raises `IdTaken` and changes nothing.
    """
    with side.transaction():
        current, by, body, plan_hash, fee = side.execute(
            "select status, resolved_by, body, plan_hash, visit_fee_cents from purchase_submissions"
            " where id = %s for update", (row,)).fetchone()
        if current != "sending":
            raise AlreadySettled(row, current, by)
        if status == "accepted":
            id_taken(side, row, request_id)
        side.execute(
            "update purchase_submissions set status = %s, request_id = %s, response = %s, detail = %s"
            " where id = %s and status = 'sending'",
            (status, request_id, None if response is None else Jsonb(response), detail, row),
        )
        if status == "accepted":
            record(side, request_id, submitted_at or datetime.now(timezone.utc), bool(body.get("off_cycle")),
                   body, plan_hash=plan_hash, visit_fee_cents=fee)


def open_rows(conn: psycopg.Connection, except_row: int | None = None) -> list[tuple]:
    """(id, at, status, line count, detail, created_id) for every submission a person still has to settle.
    `created_id` is the request the bridge accepted with a validated `201`, or None. `except_row` leaves out
    one row: the fill's own attempt, when it checks everything again before its POST."""
    return conn.execute(
        "select id, at, status, jsonb_array_length(body->'lines'), detail, created_id from purchase_submissions"
        " where status = any(%s) and id is distinct from %s order by id",
        (list(OPEN), except_row),
    ).fetchall()


def unresolved(conn: psycopg.Connection, except_row: int | None = None) -> Refusal | None:
    """The refusal both preview and submit raise while any submission is still open, naming every one."""
    rows = open_rows(conn, except_row)
    if not rows:
        return None
    named = ", ".join(f"{row} ({status} since {at:%Y-%m-%d %H:%M} UTC)" for row, at, status, *_ in rows)
    return Refusal("submission_unresolved", (
        f"submission{'s' if len(rows) > 1 else ''} {named} may already exist on the bridge, so nothing can be "
        f"submitted; {RESOLVE_HINT.format(row=rows[0][0])}"))


def resolve(conn: psycopg.Connection, row: int, request_id: str | None, who: str, *,
            lock_timeout_ms: int | None = None) -> str:
    """Settle one open row as filed under `request_id`, or as never filed (`request_id` None). Returns what
    was done. A `request_id` that is given but empty or only spaces raises `ValueError` before anything is
    written: it is a typo, not "never filed", and the row stays open.

    A row holding `created_id` got the bridge's validated `201` for that request, so it was filed: never filed
    is refused, and so is any other request ID (a person checks with the instructors). Both raise `ValueError`
    and leave the row open. A `created_id` something else already holds (`id_holder`) isn't this attempt's,
    so it settles nothing and refuses nothing: the row is like one with no `created_id`.

    Waits for a submit in flight to finish first. With `lock_timeout_ms`, it gives
    up after that long and raises `psycopg.errors.LockNotAvailable`, so a caller
    can say it's waiting.
    """
    if not who.strip():
        raise ValueError("--by needs your name")
    if request_id is not None:
        request_id = request_id.strip()
        if not request_id:
            raise ValueError(f"--request-id is empty, so submission {row} was left open; give the bridge's "
                             "request ID, or --not-filed if the bridge never filed it")
    with conn.transaction():
        if lock_timeout_ms is not None:
            conn.execute(f"set local lock_timeout = {int(lock_timeout_ms)}")
        # The lock every write takes; a submit holds it until its row is settled.
        is_halted(conn, lock=True)
        found = conn.execute(
            "select at, status, body, plan_hash, visit_fee_cents, created_id from purchase_submissions"
            " where id = %s for update", (row,)).fetchone()
        if found is None:
            raise ValueError(f"There is no submission {row}.")
        at, status, body, plan_hash, fee, created_id = found
        if status not in OPEN:
            raise ValueError(f"Submission {row} is {status}, not sending or unknown; nothing to settle.")
        if created_id is not None and id_holder(conn, row, created_id) is not None:
            created_id = None
        if created_id is not None and request_id is None:
            raise ValueError(f"The bridge accepted request {created_id} for submission {row} (a validated 201), so "
                             f"it was filed and can't be marked never filed; use --request-id {created_id}")
        if created_id is not None and request_id != created_id:
            raise ValueError(f"The bridge accepted request {created_id} for submission {row}, not {request_id}; "
                             "check with the instructors which request is this submission's before settling it. "
                             f"Submission {row} was left open")
        if request_id is not None:
            id_taken(conn, row, request_id)     # an IdTaken is a ValueError: `check` leaves the row open
            conn.execute("update purchase_submissions set status = 'accepted', request_id = %s, resolved_by = %s,"
                         " resolved_at = now() where id = %s", (request_id, who.strip(), row))
            record(conn, request_id, at, bool(body.get("off_cycle")), body, plan_hash=plan_hash, visit_fee_cents=fee)
            done = f"submission {row} recorded as filed under {request_id}, pending, by {who.strip()}"
        else:
            conn.execute("update purchase_submissions set status = 'not_filed', resolved_by = %s, resolved_at = now()"
                         " where id = %s", (who.strip(), row))
            done = f"submission {row} marked never filed by {who.strip()}; the plan may be submitted again"
        log(conn, "write", "submissions.resolve", {"row": row, "request_id": request_id, "was": status}, "done", done)
    return done[0].upper() + done[1:]


@dataclass
class Checked:
    row: int
    status: str        # `accepted` if settled; otherwise the row's status, still open
    why: str


def answers_for(answer: Any, request_id: str) -> str:
    """Why `answer` (a `GET` of `request_id`) isn't the bridge's record of that request; "" if it is:
    a JSON object naming `request_id`, with a documented status and a creation time."""
    if not isinstance(answer, dict):
        return "the bridge's answer isn't a JSON object"
    if answer.get("id") != request_id:
        return f"the bridge's answer doesn't name {request_id}"
    status = answer.get("status")
    if not isinstance(status, str) or status not in REQUEST_STATUSES:     # `{}` can't even be looked up
        return f"the bridge's answer for {request_id} has no documented status"
    if not (isinstance(answer.get("created_at"), str) and answer["created_at"].strip()):
        return f"the bridge's answer for {request_id} has no creation time"
    return ""


def check(conn: psycopg.Connection, bridge: Callable[[], Any], *,
          lock_timeout_ms: int | None = None) -> list[Checked]:
    """Settle each open row the bridge can answer for by itself. Returns one `Checked` per open row.

    Only a row with `created_id` (its POST got a validated `201`) is looked up, by that ID, and only a
    `GET` answer for it in the documented shape (`answers_for`) settles it, as `accepted`. When in doubt
    the row stays open. It never sends anything and never settles a row as `not_filed`. The bridge is
    read first, outside any lock; then each row is settled through `resolve`, which takes the halt lock
    and refuses a row someone settled meanwhile. `bridge` is called only if some open row has `created_id`.
    """
    from vending.bridge import BridgeError, BridgeNotFound

    rows = conn.execute("select id, status, created_id from purchase_submissions"
                        " where status = any(%s) order by id", (list(OPEN),)).fetchall()
    client = None
    results = []
    for row, status, found in rows:
        if found is None:
            results.append(Checked(row, status, "its POST got no validated 201, so only a person can tell "
                                                "whether the bridge filed it"))
            continue
        try:
            client = client or bridge()
            answer = client.purchase_request(found)
        except BridgeNotFound:
            settle_as, why = None, (f"the bridge has no {found} (404), which doesn't prove it was never filed: "
                                    "the bridge has no answer that means that")
        except BridgeError as error:
            settle_as, why = None, f"the bridge couldn't be asked about {found} ({type(error).__name__})"
        except Exception as error:      # no bridge address or key set, say: the row just stays open
            settle_as, why = None, f"the bridge couldn't be asked ({type(error).__name__})"
        else:
            if problem := answers_for(answer, found):
                settle_as, why = None, problem
            else:
                settle_as, why = found, f"the bridge has request {found} ({answer['status']})"
        if settle_as is None:
            results.append(Checked(row, status, why))
            continue
        try:
            resolve(conn, row, settle_as, AUTO_BY, lock_timeout_ms=lock_timeout_ms)
        except (ValueError, psycopg.errors.LockNotAvailable) as error:
            results.append(Checked(row, status, f"{why}, but it wasn't settled ({type(error).__name__})"))
            continue
        results.append(Checked(row, "accepted", why))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vending.submissions", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("list", help="submissions still waiting for a person to check the bridge")
    sub.add_parser("check", help="settle a row whose POST got a validated 201 but was never settled")
    resolver = sub.add_parser("resolve", help="settle one submission")
    resolver.add_argument("row", type=int, help="the submission number, from `list`")
    outcome = resolver.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--request-id", help="the bridge filed it, under this request ID")
    outcome.add_argument("--not-filed", action="store_true", help="the bridge never filed it")
    resolver.add_argument("--by", required=True, help="your name, for the log")
    args = parser.parse_args(argv)

    with db.connect() as conn:
        if args.action == "list":
            rows = open_rows(conn)
            if not rows:
                print("Nothing to settle: every submission was accepted, refused or never filed.")
            for row, at, status, lines, detail, created_id in rows:
                accepted = f"  accepted as {created_id} (resolve with --request-id {created_id})" if created_id else ""
                print(f"{row}  {at:%Y-%m-%d %H:%M}  {status:8}  {lines} lines{accepted}  {detail or ''}".rstrip())
            return 0
        if args.action == "check":
            from vending.bridge import Bridge

            results = check(conn, Bridge, lock_timeout_ms=WAIT_NOTICE_MS)
            if not results:
                print("Nothing to settle: every submission was accepted, refused or never filed.")
            for c in results:
                if c.status in OPEN:
                    print(f"{c.row}  still {c.status}: {c.why}. {RESOLVE_HINT.format(row=c.row)}")
                else:
                    print(f"{c.row}  settled as {c.status}: {c.why}.")
            return 1 if any(c.status in OPEN for c in results) else 0
        try:
            try:
                done = resolve(conn, args.row, args.request_id, args.by, lock_timeout_ms=WAIT_NOTICE_MS)
            except psycopg.errors.LockNotAvailable:
                print("waiting for a submit in flight to finish...", flush=True)
                done = resolve(conn, args.row, args.request_id, args.by)
            print(done)
        except ValueError as error:
            print(error, file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
