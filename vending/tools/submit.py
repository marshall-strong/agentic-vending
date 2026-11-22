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

The reply carries the plan's `warnings` and `notes`, as preview's does.

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
import sys
from collections.abc import Callable
from datetime import datetime

import psycopg

from vending import planner, planning, submissions
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
                         expected_plan_hash: str | None = None,
                         ) -> Callable[[psycopg.Connection], SubmitReply]:
    """The write action for the gate. `connect` opens the one side connection that records the
    attempt; it must reach the same database as the gate's."""

    def action(conn: psycopg.Connection) -> SubmitReply:
        client = bridge()
        try:
            plan, gathered = planning.plan(conn, desired, client, now)
        except planning.NotReady as blocked:
            return SubmitReply(success=False, message=f"Can't submit yet: {blocked}")
        refusals = list(plan.refusals)
        # A plan with refusals has no hash; those refusals already say what changed.
        if expected_plan_hash and plan.plan_hash and plan.plan_hash != expected_plan_hash:
            refusals.append(Refusal("plan_changed", (
                f"the plan now hashes to {plan.plan_hash}, not {expected_plan_hash} as previewed: the lines, their "
                "worst-case cost or the visit fee changed since. Preview again and submit what it shows.")))
        if unresolved := submissions.unresolved(conn):
            refusals.append(unresolved)
        if refusals:
            raise Refused(refusals)
        body = plan.request()
        # The fee the plan quoted goes with the request, so the ledger charges what was
        # agreed now even if config.midweek_fee_cents changes later.
        fee = plan.cost.visit_fee_cents if plan.off_cycle else None
        # Opened before anything is sent, and reused to settle: a connect that would fail
        # fails now, while nothing has left, not after the bridge has the request.
        with connect() as side:
            row = submissions.save_sending(side, plan.plan_hash, body, plan.directive, fee)
            reply = _send(client, plan, body, row, side, gathered.now)
        # What preview would have said about this plan, so a changed warning since the preview isn't missed.
        return reply.model_copy(update={"warnings": list(plan.warnings), "notes": list(gathered.notes)})

    return action


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
          now: datetime) -> SubmitReply:
    try:
        answer = client.post_answer(ROUTE, body)
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
