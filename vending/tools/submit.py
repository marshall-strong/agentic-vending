"""`submit_desired_state` (tool #6): the only way the agent spends money.

Inside the write gate (halt lock held), it runs the planner again on live data,
so the plan that is sent is the plan that was checked, and refuses with every
problem at once if anything has changed since the preview. Then:

1. commits a `sending` row in `purchase_submissions` on its own connection,
   so a crash after the POST can't lose the fact that we sent something;
2. POSTs the plan's request body once, never retried: a retry after a lost
   answer could order twice;
3. settles the row: `accepted` (and the request goes into `purchase_requests`
   for the sync to read back), `refused` (a 4xx: the bridge said no, and says
   why), `not_filed` (it never left), or `unknown` (sent, no usable answer).

While a row is `sending` or `unknown`, the request may or may not exist on the
bridge, so the tool refuses to submit anything until a person settles it with
`python -m vending.submissions resolve`.
"""

from __future__ import annotations

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


def submit_desired_state(desired: DesiredState, bridge: Callable[[], Bridge],
                         connect: Callable[[], psycopg.Connection], now: datetime | None = None,
                         ) -> Callable[[psycopg.Connection], SubmitReply]:
    """The write action for the gate. `connect` opens the short, separate connections that
    record each step; they must reach the same database as the gate's."""

    def action(conn: psycopg.Connection) -> SubmitReply:
        client = bridge()
        try:
            plan, gathered = planning.plan(conn, desired, client, now)
        except planning.NotReady as blocked:
            return SubmitReply(success=False, message=f"Can't submit yet: {blocked}")
        if plan.refusals:
            raise Refused(list(plan.refusals))
        if open_rows := submissions.open_rows(conn):
            row, at, status, _, _ = open_rows[0]
            raise Refused([Refusal("submission_unresolved", (
                f"submission {row} from {at:%Y-%m-%d %H:%M} UTC is still {status}, so a request may already "
                f"exist on the bridge; {submissions.RESOLVE_HINT.format(row=row)}"))])
        body = plan.request()
        row = submissions.save_sending(connect, plan.plan_hash, body, plan.directive)
        return _send(client, plan, body, row, connect, gathered.now)

    return action


def _send(client: Bridge, plan: planner.Plan, body: dict, row: int,
          connect: Callable[[], psycopg.Connection], now: datetime) -> SubmitReply:
    try:
        answer = client.post(ROUTE, body)
    except BridgeAnswered as error:
        if error.status == 429:
            submissions.settle(connect, row, "refused", detail=str(error))
            raise Refused([Refusal("too_many_open_requests",
                                   "the bridge has too many of our requests open; wait for earlier ones to be decided")]) from None
        if error.status >= 500:
            return _unknown(connect, row, plan, f"the bridge answered {error.status}")
        submissions.settle(connect, row, "refused", detail=str(error))
        return SubmitReply(success=False, submission=row, plan_hash=plan.plan_hash,
                           message=f"The bridge refused the request ({error}); nothing was ordered. "
                                   "Fix what it names and preview again.")
    except BridgeUnreachable as error:
        submissions.settle(connect, row, "not_filed", detail=str(error))
        return SubmitReply(success=False, submission=row, plan_hash=plan.plan_hash,
                           message=f"Couldn't reach the bridge ({error}); nothing was sent.")
    except BridgeNoAnswer as error:
        return _unknown(connect, row, plan, str(error))
    request_id = answer.get("id") if isinstance(answer, dict) else None
    if not request_id:
        return _unknown(connect, row, plan, "the bridge accepted it, but its answer has no request ID", answer)
    status = answer.get("status") or "pending"
    submissions.settle(connect, row, "accepted", request_id=request_id, response=answer, submitted_at=now)
    return SubmitReply(
        success=True, count=len(plan.lines), request_id=request_id, status=status, plan_hash=plan.plan_hash,
        submission=row, lines=len(plan.lines), max_cost=money(plan.cost.max_cents), directive=plan.directive,
        message=(f"Submitted purchase request {request_id} ({status}): {len(plan.lines)} lines, at most "
                 f"{money(plan.cost.max_cents)}"
                 + (" including the midweek visit fee" if plan.off_cycle else "")
                 + ". Follow it with get_purchase_requests; the receipts arrive once the worker is done."),
    )


def _unknown(connect: Callable[[], psycopg.Connection], row: int, plan: planner.Plan, why: str,
             answer: object = None) -> SubmitReply:
    submissions.settle(connect, row, "unknown", response=answer, detail=why)
    return SubmitReply(
        success=False, status="unknown", submission=row, plan_hash=plan.plan_hash,
        message=(f"Sent, but no usable answer came back ({why}), so the request may or may not exist. "
                 f"Don't submit again; {submissions.RESOLVE_HINT.format(row=row)}"),
    )
