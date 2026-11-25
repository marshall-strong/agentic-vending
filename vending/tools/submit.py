"""`submit_desired_state` (tool #6): the only way the agent spends money.

Inside the write gate (halt lock held), it runs the planner again on live data,
so the plan that is sent is the plan that was checked. Given `expected_plan_hash`
(the `plan_hash` that `preview_desired_state` returned) it also refuses,
`plan_changed`, when the lines, their worst-case cost or the visit fee changed
since the preview. Every refusal is reported at once, including an earlier submission a
person still has to settle. Then, on one side connection opened before
anything is sent:

1. commits a `sending` row in `purchase_submissions`, so a crash after the POST
   can't lose the fact that we sent something;
2. POSTs the plan's request body once, never retried and never redirected
   (`Bridge.post_answer`): a retry after a lost answer could order twice;
3. keeps the bridge's answer on the row the moment it is read (`save_answer`),
   with `created_id` set only for a validated `201`, so the ID is on the row even
   if settling fails, and the next run's check can look it up;
4. settles the row on that same connection, one of three ways:
   - `accepted`: only the documented acceptance, whole (`accepted`): a `201`,
     `application/json`, read in full within the deadline and size cap, strict
     JSON, the `PurchaseRequestPending` body with status `pending`, under an ID
     no other attempt or request holds. The request goes into
     `purchase_requests` for the sync to read back.
   - `not_filed`: connecting failed, so no byte of the request left.
   - `unknown`: everything else. A `4xx` or `5xx` (even the bridge's documented
     refusal), a redirect, any other success, a body cut off, too slow, too
     long, encoded or not plain JSON, or a lost answer. The reason is kept, with
     the body the bridge sent.

An `unknown` row blocks every later submit (`submission_unresolved`) until a
person settles it with `python -m vending.submissions resolve`. Nothing settles
a POST's answer as "nothing was ordered" by itself: a reply shaped like a refusal
can't prove that, so a person checks.

Only `plan_changed` (our own preview, before step 1) and `not_filed` (connecting
failed) let the agent try once more in the same run. A refusal before step 1
comes back with no `submission` number.

The day-one fill (`vending.fill`) adds checks of its own through hooks the agent
never passes: `before_send` before step 1, with the inputs the planner was given,
and `before_post` between steps 1 and 2, given the row and the body, which settles
the row `not_filed` if it refuses. It also marks its rows `purpose = 'fill'`.

The reply carries the plan's `warnings` and `notes`, as preview's does.

**The run's own context (`RunContext`).** The agent that starts this server hands it its run lease's token and,
for a scheduled run, the scheduled time (`VENDING_RUN_LEASE`, `VENDING_SCHEDULE_DUE`, in the child's real
environment: `vending.agent.server_env`). The submit checks that exact lease, never whichever lease is current, so
a lease that expired or was taken over after the agent handed the call on can't turn a scheduled submit into a
manual one: it is refused (`lease_lost`). A scheduled time with no usable lease is refused too (`run_context`).
A weekly scheduled run never buys an off-cycle visit (`scheduled_off_cycle`): its job is the Friday order. A
midweek run, started by a midweek wake-up (#219), buys only an off-cycle visit (`midweek_on_cycle`). A person's
run by hand has no scheduled time and keeps the off-cycle choice.

**Locks first, then the clock.** Between steps 1 and 2 the submit takes its locks on the gate's connection: the
run lease's row (`for key share`, so no takeover until the POST is answered), and for a scheduled run the
database switch's row and the schedule's row (`for share`, so an `off` waits until after the send). Only once
the bridge connection is open, right before the request is written (`Bridge.post_answer`'s `ready`), does it
read the database's clock and run the final checks: the store cutoffs, the lease, and for a scheduled run both
switches, its row, the start, open submissions, the slot check and one submission per visit
(`agent_schedule.send_blocker`). So neither a lock wait nor a slow connect can carry a send past a cutoff. A
failed check sends nothing and settles the attempt `not_filed`.

If the bridge accepted the request but recording that fails anyway, the answer
is written to stderr and the error the agent sees names the request ID and the
`resolve` command, so the ID is never only in memory. That includes finding the
row already settled by something else (`submissions.AlreadySettled`), which
`settle` refuses to overwrite. A row left `sending` that way holds `created_id`,
so `python -m vending.submissions check` (which a live agent run also does first)
can settle it from the bridge.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime

import psycopg

from vending import ledger, planner, planning, submissions
from vending.bridge import Bridge, BridgeNoAnswer, BridgeUnreachable, PostAnswer
from vending.ledger import money
from vending.pricing import Refusal
from vending.tools import WriteReply
from vending.tools.desired_state import DesiredState
from vending.write_gate import Refused

ROUTE = "/api/v1/purchase-requests"
ACCEPTED_STATUS = 201
# `PurchaseRequestPending`'s fields (`docs/bridge-openapi-2026-11-20.json`): an acceptance with any other isn't
# the documented one, so it settles nothing.
PENDING_FIELDS = frozenset({"id", "status", "created_at", "decision_note"})
# Set by the agent on the server it starts (`vending.agent.server_env`), in the real environment only, never `.env`.
RUN_LEASE_ENV = "VENDING_RUN_LEASE"
SCHEDULE_DUE_ENV = "VENDING_SCHEDULE_DUE"
RUN_ENV = (RUN_LEASE_ENV, SCHEDULE_DUE_ENV)


@dataclass(frozen=True)
class RunContext:
    """The agent run this server works for: its run lease's token, and its scheduled time (None by hand).
    `problem` says why the values the agent passed can't be used; a submit is then refused."""
    lease: uuid.UUID | None = None
    due: datetime | None = None
    problem: str | None = None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> RunContext:
        environ = os.environ if environ is None else environ
        raw_lease = (environ.get(RUN_LEASE_ENV) or "").strip()
        raw_due = (environ.get(SCHEDULE_DUE_ENV) or "").strip()
        lease = due = None
        problems = []
        if raw_lease:
            try:
                lease = uuid.UUID(raw_lease)
            except ValueError:
                problems.append(f"{RUN_LEASE_ENV} isn't a run lease token")
        if raw_due:
            try:
                due = datetime.fromisoformat(raw_due)
                if due.tzinfo is None:
                    raise ValueError
            except ValueError:
                problems.append(f"{SCHEDULE_DUE_ENV} isn't a time with its offset")
                due = None
            if not raw_lease:
                problems.append(f"{SCHEDULE_DUE_ENV} names a scheduled time but {RUN_LEASE_ENV} gives no run lease")
        return cls(lease, due, "; ".join(problems) or None)


class _NotSent(Exception):
    """The final checks, once the bridge connection is open, said no (`refusals`) or failed (`error`): nothing
    of the request was written."""

    def __init__(self, refusals: list[Refusal], error: BaseException | None = None):
        super().__init__("; ".join(f"{r.code}: {r.message}" for r in refusals) or repr(error))
        self.refusals, self.error = refusals, error


class SubmitReply(WriteReply):
    request_id: str | None = None
    # The request's status when accepted; otherwise how the record was settled: `not_filed` (it never left)
    # means nothing was ordered, `unknown` that it may have been.
    status: str | None = None
    plan_hash: str | None = None
    submission: int | None = None
    lines: int = 0
    max_cost: str | None = None
    directive: str | None = None
    # The same as preview_desired_state's, from the plan that was sent.
    warnings: list[str] = []
    notes: list[str] = []


class AnswerNotStored(RuntimeError):
    """The bridge accepted a request, but recording its ID failed. The message carries the ID."""

    def __init__(self, row: int, request_id: str, answer: object, cause: BaseException):
        super().__init__(
            f"the bridge ACCEPTED purchase request {request_id} (submission {row}) but storing that failed "
            f"({type(cause).__name__}: {cause}). Money may be spent on it. Don't submit again: a person must run "
            f"python -m vending.submissions resolve {row} --request-id {request_id} --by NAME")
        self.row, self.request_id, self.answer = row, request_id, answer


def submit_desired_state(desired: DesiredState, bridge: Callable[[], Bridge],
                         connect: Callable[[], psycopg.Connection], now: datetime | None = None,
                         expected_plan_hash: str | None = None, *, pool: bool = False,
                         before_send: Callable[[psycopg.Connection, planner.Plan, planning.Gathered],
                                               list[Refusal]] | None = None,
                         before_post: Callable[[psycopg.Connection, int, dict], list[Refusal]] | None = None,
                         purpose: str | None = None, count_takings: bool = True,
                         run: RunContext | None = None,
                         ) -> Callable[[psycopg.Connection], SubmitReply]:
    """The write action for the gate. `connect` opens the one side connection that records the
    attempt; it must reach the same database as the gate's. The rest are the fill's (`vending.fill`), never
    the agent's:

    - `pool`: see "Pooled purchases" in the planner;
    - `before_send` runs last before anything is recorded or sent, in the gate's transaction, with the plan and
      the inputs the planner was given; any refusal it returns stops the send;
    - `before_post` runs after the attempt is saved, right before the POST, with the side connection, the
      attempt's row and the exact body about to be sent; any refusal it returns settles the attempt as
      `not_filed` (nothing was sent) and stops the send;
    - `purpose` is saved on the attempt (`purchase_submissions.purpose`);
    - `count_takings=False` checks the balance without takings from the polls (`planning.gather`).

    `run` is the agent run's own context (the MCP server reads it from its environment): its lease, checked at the
    send, and for a scheduled run its time. Never the fill's."""

    def action(conn: psycopg.Connection) -> SubmitReply:
        client = bridge()
        try:
            plan, gathered = planning.plan(conn, desired, client, now, pool=pool, count_takings=count_takings)
        except planning.NotReady as blocked:
            return SubmitReply(success=False, message=f"Can't submit yet: {blocked}")
        refusals = list(plan.refusals)
        # A plan with refusals has no hash; those refusals already say what changed.
        if expected_plan_hash and plan.plan_hash and plan.plan_hash != expected_plan_hash:
            refusals.append(Refusal("plan_changed", (
                f"the plan now hashes to {plan.plan_hash}, not {expected_plan_hash} as previewed: the lines, their "
                "worst-case cost or the visit fee changed since. Preview again and submit what it shows.")))
        if before_send is not None:
            refusals.extend(before_send(conn, plan, gathered))
        if unresolved := submissions.unresolved(conn):
            refusals.append(unresolved)
        mine = run if purpose is None else None
        if mine is not None and mine.problem:
            refusals.append(Refusal("run_context", (
                f"this server can't tell which agent run it works for ({mine.problem}), so nothing is sent")))
        # A scheduled run (the agent passed its time): checked again at the send. Never the fill's.
        due = mine.due if mine is not None else None
        # A weekly run orders for Friday and never buys an off-cycle visit; a midweek run (#219) buys only one.
        kind = None
        if due is not None:
            from vending import agent_schedule

            kind = agent_schedule.kind_of(conn, due)
        if kind == "weekly" and plan.off_cycle:
            refusals.append(Refusal("scheduled_off_cycle", (
                "a scheduled run orders for the Friday visit and never buys an off-cycle visit (its fee, and the "
                "cutoffs it skips). Submit with off_cycle false; a person who means an off-cycle visit sends it by "
                "hand: python -m vending.agent --live.")))
        if kind == "midweek" and not plan.off_cycle:
            refusals.append(Refusal("midweek_on_cycle", (
                "a midweek run was started for a paid midweek visit and sends only that: submit with off_cycle true, "
                "or submit nothing if no visit is worth its fee. The Friday order is the weekly run's.")))
        if refusals:
            raise Refused(refusals)
        body = plan.request()
        # The fee the plan quoted goes with the request, so the ledger charges what was
        # agreed now even if config.midweek_fee_cents changes later.
        fee = plan.cost.visit_fee_cents if plan.off_cycle else None
        # Opened before anything is sent, and reused to settle: a connect that would fail
        # fails now, while nothing has left, not after the bridge has the request.
        with connect() as side:
            row = submissions.save_sending(side, plan.plan_hash, body, plan.directive, fee, purpose, due)
            if before_post is not None:
                _last_check(side, row, body, before_post)
            # The locks first, held until the POST is answered; the clock is read only after them, at the send.
            _last_check(side, row, body, lambda _side, _r, _body: _lock_for_send(conn, mine))
            reply = _send(client, plan, body, row, side, gathered.now,
                          ready=lambda: _at_the_send(conn, plan, gathered, now, mine, row))
        # What preview would have said about this plan, so a changed warning since the preview isn't missed.
        return reply.model_copy(update={"warnings": list(plan.warnings), "notes": list(gathered.notes)})

    return action


def _last_check(side: psycopg.Connection, row: int, body: dict,
                before_post: Callable[[psycopg.Connection, int, dict], list[Refusal]]) -> None:
    """Run `before_post` once the attempt is saved. Any refusal, or an error, settles the attempt as never filed
    (nothing has been sent), so it can't be left open; a refusal is then raised."""
    try:
        refusals = before_post(side, row, body)
    except BaseException as error:
        submissions.settle(side, row, "not_filed", detail=f"nothing sent: {type(error).__name__} before the POST")
        raise
    if refusals:
        submissions.settle(side, row, "not_filed", detail="nothing sent: " + "; ".join(
            f"{r.code}: {r.message}" for r in refusals))
        raise Refused(refusals)


def _lock_for_send(conn: psycopg.Connection, run: RunContext | None) -> list[Refusal]:
    """Take the send's locks on the gate's connection, before the clock is read: the run lease's row `for key
    share` (a takeover waits until the POST is answered; the run's own renewals don't), and for a scheduled run
    the database switch's row and the schedule's row (`agent_schedule.lock_for_send`). Refuses nothing itself."""
    if run is None or run.lease is None:
        return []
    conn.execute("select from agent_run_lease where one for key share")
    if run.due is not None:
        from vending import agent_schedule

        agent_schedule.lock_for_send(conn, run.due)
    return []


def lease_blocker(conn: psycopg.Connection, run: RunContext) -> str | None:
    """Why the run named by `run` can't send now: it no longer holds its own run lease, or that lease is for
    another scheduled time. None when it holds it."""
    from vending import run_lease

    found = conn.execute("select schedule_due from agent_run_lease"
                         " where token = %s and expires_at > clock_timestamp()", (run.lease,)).fetchone()
    if found is None:
        return run_lease.LOST
    if found[0] != run.due:
        return "this run's lease is held for a different scheduled time than the one it was started for"
    return None


def _at_the_send(conn: psycopg.Connection, plan: planner.Plan, gathered: planning.Gathered, given: datetime | None,
                 run: RunContext | None, row: int) -> None:
    """The final checks, on the gate's connection, once the bridge connection is open and before any byte is
    written, with the clock read now, after the locks (the database's, `ledger.db_now`; a `now` the caller gave
    stays as given): the plan's store cutoffs again, so a plan made just before Thursday 23:59 isn't sent after it;
    the run's own lease; and for a scheduled run, both switches, its row, the start and every check before a send
    (`agent_schedule.send_blocker`). Raises `_NotSent` if any says no, or if a check fails."""
    try:
        now = given if given is not None else ledger.db_now(conn)
        refusals = planner.cutoff_refusals(plan, gathered.config, now)
        if run is not None and run.lease is not None and (why := lease_blocker(conn, run)) is not None:
            refusals.append(Refusal("lease_lost", f"checked at the send: {why}"))
        if run is not None and run.due is not None:
            from vending import agent_schedule

            if (why := agent_schedule.send_blocker(conn, run.due, now, row)) is not None:
                refusals.append(Refusal("schedule_not_live", f"scheduled live run, checked again at the send: {why}"))
    except BaseException as error:
        raise _NotSent([], error) from error
    if refusals:
        raise _NotSent(refusals)


def _str(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def accepted(answer: PostAnswer) -> str:
    """Why the answer to the POST isn't the documented acceptance; "" if it is: a `201` whose body is plain
    JSON (`PostAnswer.problem` empty) in the `PurchaseRequestPending` shape, status `pending`."""
    if answer.status != ACCEPTED_STATUS:
        return f"the bridge answered {answer.status}, and only the {ACCEPTED_STATUS} it documents settles a request"
    if answer.problem:
        return answer.problem
    body = answer.body
    if not isinstance(body, dict):
        return "its answer isn't a JSON object"
    extra = set(body) - PENDING_FIELDS
    if extra:
        return f"its answer has fields a new request doesn't document ({', '.join(sorted(extra))})"
    if not _str(body.get("id")):
        return "its answer has no request ID"
    if body.get("status") != "pending":
        return f"its answer's status is {body.get('status')!r}, not pending"
    if not _str(body.get("created_at")) or not (body.get("decision_note") is None
                                                or isinstance(body.get("decision_note"), str)):
        return "its answer isn't the documented new request (created_at, decision_note)"
    return ""


def _short(text: str, size: int = 300) -> str:
    return text if len(text) <= size else text[:size] + f"... ({len(text)} characters)"


def _send(client: Bridge, plan: planner.Plan, body: dict, row: int, side: psycopg.Connection,
          now: datetime, ready: Callable[[], None] | None = None) -> SubmitReply:
    try:
        answer = client.post_answer(ROUTE, body, ready=ready)
    except _NotSent as stopped:
        if stopped.error is not None:
            submissions.settle(side, row, "not_filed",
                               detail=f"nothing sent: {type(stopped.error).__name__} in the checks before the POST")
            raise stopped.error from None
        submissions.settle(side, row, "not_filed", detail="nothing sent: " + "; ".join(
            f"{r.code}: {r.message}" for r in stopped.refusals))
        raise Refused(stopped.refusals) from None
    except BridgeUnreachable as error:
        submissions.settle(side, row, "not_filed", detail=str(error))
        return SubmitReply(success=False, status="not_filed", submission=row, plan_hash=plan.plan_hash,
                           message=f"Couldn't connect to the bridge ({error}); nothing was sent.")
    except BridgeNoAnswer as error:
        return _unknown(side, row, plan, str(error))
    why = accepted(answer)
    created_id = None if why else answer.body["id"]
    # Kept before anything else can fail: the next run's check looks up `created_id`.
    try:
        submissions.save_answer(side, row, answer.body, created_id=created_id)
    except Exception as error:
        if created_id is not None:
            _keep(row, created_id, answer.body, error)
            raise AnswerNotStored(row, created_id, answer.body, error) from error
        # Not an acceptance, so nothing to lose: settle it `unknown` without the body (a NUL character in it, say).
        return _unknown(side, row, plan, f"{why}; its answer couldn't be stored ({type(error).__name__})")
    if why:
        said = f" It said: {_short(answer.text)}" if answer.text else ""
        return _unknown(side, row, plan, f"{why}.{said}", answer.body)
    request_id, status = created_id, answer.body["status"]
    try:
        submissions.settle(side, row, "accepted", request_id=request_id, response=answer.body, submitted_at=now)
    except submissions.IdTaken as taken:
        # Not this attempt's request after all, so not an acceptance: the row keeps the answer and no `created_id`.
        submissions.save_answer(side, row, answer.body)
        return _unknown(side, row, plan, str(taken), answer.body)
    except Exception as error:
        _keep(row, request_id, answer.body, error)
        raise AnswerNotStored(row, request_id, answer.body, error) from error
    return SubmitReply(
        success=True, count=len(plan.lines), request_id=request_id, status=status, plan_hash=plan.plan_hash,
        submission=row, lines=len(plan.lines), max_cost=money(plan.cost.max_cents), directive=plan.directive,
        message=(f"Submitted purchase request {request_id} ({status}): {len(plan.lines)} lines, at most "
                 f"{money(plan.cost.max_cents)}"
                 + (" including the midweek visit fee" if plan.off_cycle else "")
                 + ". Follow it with get_purchase_requests; the receipts arrive once the worker is done."),
    )


def _unknown(side: psycopg.Connection, row: int, plan: planner.Plan, why: str,
             answer: object = None) -> SubmitReply:
    submissions.settle(side, row, "unknown", response=answer, detail=why)
    return SubmitReply(
        success=False, status="unknown", submission=row, plan_hash=plan.plan_hash,
        message=(f"Sent, and no answer settles it ({why}), so the request may or may not exist. "
                 f"Don't submit again; {submissions.RESOLVE_HINT.format(row=row)}"),
    )


def _keep(row: int, request_id: str, answer: object, error: BaseException) -> None:
    """The last resort for an accepted request whose ID couldn't be stored: write it where it will be kept.

    stderr goes to the server's log (the Container App keeps it), so a person
    can still settle the row with the ID even if the database stays down.
    """
    print("LOST ANSWER " + json.dumps({"submission": row, "request_id": request_id, "answer": answer,
                                       "error": f"{type(error).__name__}: {error}"}, default=str),
          file=sys.stderr, flush=True)
