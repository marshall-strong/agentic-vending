"""`submit_desired_state` (tool #6): the only way the agent spends money.

Inside the write gate (halt lock held), it runs the planner again on live data,
so the plan that is sent is the plan that was checked. Given `expected_plan_hash`
(the `plan_hash` that `preview_desired_state` returned) it also refuses,
`plan_changed`, when stock, prices, the balance or the catalog moved since the
preview. Every refusal is reported at once, including an earlier submission a
person still has to settle. Then, on one side connection opened before
anything is sent:

1. commits a `sending` row in `purchase_submissions`, so a crash after the POST
   can't lose the fact that we sent something;
2. POSTs the plan's request body once, never retried: a retry after a lost
   answer could order twice;
3. settles the row on that same connection: `accepted` (and the request goes
   into `purchase_requests` for the sync to read back), `refused` (a 4xx: the
   bridge said no, and says why), `not_filed` (it never left), or `unknown`
   (sent, no usable answer; a 5xx keeps the body the bridge sent).

If the bridge accepted the request but recording that fails anyway, the answer
is written to stderr and the error the agent sees names the request ID and the
`resolve` command, so the ID is never only in memory. While a row is `sending`
or `unknown`, the request may or may not exist on the bridge, so the tool
refuses to submit anything until a person settles it with
`python -m vending.submissions resolve`.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from datetime import datetime

import psycopg

from vending import planner, planning, submissions
from vending.bridge import Bridge, BridgeAnswered, BridgeNoAnswer, BridgeUnreachable
from vending.ledger import money
from vending.pricing import Refusal
from vending.tools import WriteReply
from vending.tools.desired_state import DesiredState
from vending.write_gate import Refused

ROUTE = "/api/v1/purchase-requests"


class SubmitReply(WriteReply):
    request_id: str | None = None
    status: str | None = None
    plan_hash: str | None = None
    submission: int | None = None
    lines: int = 0
    max_cost: str | None = None
    directive: str | None = None


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
        if expected_plan_hash and plan.plan_hash != expected_plan_hash:
            refusals.append(Refusal("plan_changed", (
                f"the plan now hashes to {plan.plan_hash}, not {expected_plan_hash} as previewed: stock, prices, "
                "the balance or the catalog changed since. Preview again and submit what it shows.")))
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
            return _send(client, plan, body, row, side, gathered.now)

    return action


def _send(client: Bridge, plan: planner.Plan, body: dict, row: int, side: psycopg.Connection,
          now: datetime) -> SubmitReply:
    try:
        answer = client.post(ROUTE, body)
    except BridgeAnswered as error:
        if error.status == 429:
            submissions.settle(side, row, "refused", detail=str(error))
            raise Refused([Refusal("too_many_open_requests",
                                   "the bridge has too many of our requests open; wait for earlier ones to be decided")]) from None
        if error.status >= 500:
            return _unknown(side, row, plan, f"the bridge answered {error.status}: {error.detail}")
        submissions.settle(side, row, "refused", detail=str(error))
        return SubmitReply(success=False, submission=row, plan_hash=plan.plan_hash,
                           message=f"The bridge refused the request ({error}); nothing was ordered. "
                                   "Fix what it names and preview again.")
    except BridgeUnreachable as error:
        submissions.settle(side, row, "not_filed", detail=str(error))
        return SubmitReply(success=False, submission=row, plan_hash=plan.plan_hash,
                           message=f"Couldn't reach the bridge ({error}); nothing was sent.")
    except BridgeNoAnswer as error:
        return _unknown(side, row, plan, str(error))
    request_id = answer.get("id") if isinstance(answer, dict) else None
    if not request_id:
        return _unknown(side, row, plan, "the bridge accepted it, but its answer has no request ID", answer)
    status = answer.get("status") or "pending"
    try:
        submissions.settle(side, row, "accepted", request_id=request_id, response=answer, submitted_at=now)
    except Exception as error:
        _keep(row, request_id, answer, error)
        raise AnswerNotStored(row, request_id, answer, error) from error
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
        message=(f"Sent, but no usable answer came back ({why}), so the request may or may not exist. "
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
