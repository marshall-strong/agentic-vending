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
2. POSTs the plan's request body once, never retried: a retry after a lost
   answer could order twice;
3. keeps the bridge's answer on the row the moment it is read (`save_answer`),
   so a request ID the bridge sent is on the row even if settling fails, and
   the next run's check can look it up;
4. settles the row on that same connection: `accepted` (only the documented
   `201` with a pending request, `PurchaseRequestPending`, under an ID no other
   attempt or request holds; the request goes into `purchase_requests` for the
   sync to read back), `refused` (only a status the route documents as a
   refusal, `REFUSED_STATUSES`, with its whole body in the documented shape and
   nothing in it that names a request: `refusal`), `not_filed` (it failed while
   connecting, so it never left), or `unknown` (anything else: sent, no usable
   answer, a redirect, a 5xx, another 4xx, a body cut off, out of its schema or
   naming a request, a success other than `201`; the reason is kept, with the
   body the bridge sent).

Only a refusal from before step 1 comes back with no `submission` number. That
is how the agent tells "nothing was sent" (it may try once more) from a reply
to a POST, which never allows another one in the same run.

The reply carries the plan's `warnings` and `notes`, as preview's does.

If the bridge accepted the request but recording that fails anyway, the answer
is written to stderr and the error the agent sees names the request ID and the
`resolve` command, so the ID is never only in memory. That includes finding the
row already settled by something else (`submissions.AlreadySettled`), which
`settle` refuses to overwrite. While a row is `sending`
or `unknown`, the request may or may not exist on the bridge, so the tool
refuses to submit anything until it is settled: by `python -m vending.submissions check`
(which a live agent run also does first) when the bridge can answer for it, otherwise by a person
with `python -m vending.submissions resolve`.
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
from vending.tools import RefusalOut, WriteReply
from vending.tools.desired_state import DesiredState
from vending.write_gate import Refused

ROUTE = "/api/v1/purchase-requests"
# The statuses the route documents as refusals (`docs/bridge-openapi-2026-11-20.json`): a team key needed,
# lines refused or no funds, too many open requests. Any other answer to the POST may not be the bridge's own
# word on this request (a proxy, a redirect, a route that moved), so it is `unknown`, never `refused`.
REFUSED_STATUSES = (403, 422, 429)
# The `error_type`s each refusal status documents. The spec names none for 403 ("Needs a team key"), so any
# there; for 422 and 429, only these.
REFUSAL_TYPES: dict[int, frozenset[str] | None] = {
    403: None,
    422: frozenset({"invalid_lines", "insufficient_funds"}),
    429: frozenset({"too_many_open_requests", "submissions_locked"}),
}
# `PurchaseRequestError`'s fields, `PurchaseRequestPending`'s and `LineErrorItem`'s: an answer with any other
# field isn't the documented one, so it settles nothing.
ERROR_FIELDS = frozenset({"error", "error_type", "detail", "items", "id", "next_submission_from"})
PENDING_FIELDS = frozenset({"id", "status", "created_at", "decision_note"})
LINE_FIELDS = frozenset({"index", "slot", "choice", "sku", "error_type", "reason"})
ACCEPTED_STATUS = 201
# What each documented 429 tells the agent to wait for.
WAITS = {
    "too_many_open_requests": Refusal(
        "too_many_open_requests", "the bridge has too many of our requests open; wait for earlier ones to be decided"),
    "submissions_locked": Refusal(
        "submissions_locked", "the bridge refused two requests today for insufficient funds and takes no more until "
                              "the time it names (midnight Eastern)"),
}


class SubmitReply(WriteReply):
    request_id: str | None = None
    # The request's status when accepted; otherwise how the record was settled: `refused` (the bridge
    # said no) and `not_filed` (it never left) mean nothing was ordered, `unknown` that it may have been.
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


def _optional_str(value: object) -> bool:
    return value is None or isinstance(value, str)


def refusal(error: BridgeAnswered) -> tuple[dict | None, str]:
    """The bridge's refusal, if `error` is one in full: (its body, "") or (None, why it isn't).

    Only a status the route documents as a refusal, with its whole body (`complete`), in the documented shape
    for that status, counts: `PurchaseRequestError` with a documented `error_type`, or (a 422 only) FastAPI's
    validation error. Nothing in it may name a request (`id`, `status`), except the `id` an
    `insufficient_funds` refusal is kept as rejected under. Anything else may not be the bridge's own word, or
    may not mean nothing was ordered.
    """
    if error.status not in REFUSED_STATUSES:
        return None, "the route doesn't document that status as a refusal"
    if not getattr(error, "complete", False):
        return None, "its body was cut off or unreadable"
    try:
        body = json.loads(error.detail)
    except ValueError:
        return None, "its body isn't JSON"
    if not isinstance(body, dict):
        return None, "its body isn't a JSON object"
    if error.status == 422 and set(body) == {"detail"}:
        problems = body["detail"]
        if (isinstance(problems, list) and problems
                and all(isinstance(p, dict) and isinstance(p.get("loc"), list) and _str(p.get("msg"))
                        and _str(p.get("type")) for p in problems)):
            return body, ""
        return None, "its body isn't the documented validation error"
    extra = set(body) - ERROR_FIELDS
    if extra:
        return None, f"its body has fields a refusal doesn't document ({', '.join(sorted(extra))})"
    error_type = body.get("error_type")
    if not (_str(body.get("error")) and _str(error_type) and _optional_str(body.get("detail"))):
        return None, "its body isn't the documented refusal (error and error_type)"
    documented = REFUSAL_TYPES[error.status]
    if documented is not None and error_type not in documented:
        return None, f"{error.status} doesn't document the error type {error_type!r}"
    if body.get("id") is not None and not (error_type == "insufficient_funds" and _str(body["id"])):
        return None, "it names a request, which a refusal of that kind doesn't"
    locked = body.get("next_submission_from")
    if locked is not None and not (error_type == "submissions_locked" and _str(locked)):
        return None, "it carries next_submission_from, which only submissions_locked does"
    items = body.get("items", [])
    if not (isinstance(items, list) and all(_line_problem(item) for item in items)):
        return None, "its items aren't the documented line problems"
    if error_type == "invalid_lines" and not items:
        return None, "invalid_lines names no line"
    return body, ""


def _line_problem(item: object) -> bool:
    """One `LineErrorItem`: every documented field, and no other."""
    return (isinstance(item, dict) and set(item) == LINE_FIELDS
            and isinstance(item["index"], int) and not isinstance(item["index"], bool)
            and isinstance(item["slot"], str) and item["choice"] in ("primary", "alternate", "load", None)
            and _optional_str(item["sku"]) and _str(item["error_type"]) and isinstance(item["reason"], str))


def accepted(status: int | None, answer: object) -> str:
    """Why the answer to the POST isn't the documented acceptance; "" if it is: a `201` with a
    `PurchaseRequestPending` body whose status is `pending` (`rejected` would contradict the 201)."""
    if status != ACCEPTED_STATUS:
        return f"the bridge answered {status}, not the {ACCEPTED_STATUS} it documents for an accepted request"
    if not isinstance(answer, dict):
        return "its answer isn't a JSON object"
    extra = set(answer) - PENDING_FIELDS
    if extra:
        return f"its answer has fields a new request doesn't document ({', '.join(sorted(extra))})"
    if not _str(answer.get("id")):
        return "its answer has no request ID"
    if answer.get("status") != "pending":
        return f"its answer's status is {answer.get('status')!r}, not pending"
    if not _str(answer.get("created_at")) or not _optional_str(answer.get("decision_note")):
        return "its answer isn't the documented new request (created_at, decision_note)"
    return ""


def _short(text: str, size: int = 500) -> str:
    return text if len(text) <= size else text[:size] + f"... ({len(text)} characters)"


def _send(client: Bridge, plan: planner.Plan, body: dict, row: int, side: psycopg.Connection,
          now: datetime) -> SubmitReply:
    try:
        status, answer = client.post_answer(ROUTE, body)
    except BridgeAnswered as answered:
        error = _short(str(answered))
        refused, why = refusal(answered)
        if refused is None:
            return _unknown(side, row, plan, f"the bridge answered {answered.status}, but {why}: {error}")
        submissions.settle(side, row, "refused", response=refused, detail=error)
        if answered.status == 429:
            # Returned, not raised as `Refused`: it carries the submission number, so it reads as a reply to a POST.
            wait = WAITS[refused["error_type"]]
            return SubmitReply(success=False, status="refused", submission=row, plan_hash=plan.plan_hash, count=1,
                               refusals=[RefusalOut(code=wait.code, message=wait.message)],
                               message=f"submit_desired_state refused by the bridge ({error}); nothing was ordered.")
        return SubmitReply(success=False, status="refused", submission=row, plan_hash=plan.plan_hash,
                           message=f"The bridge refused the request ({error}); nothing was ordered. "
                                   "Fix what it names and preview again.")
    except BridgeUnreachable as error:
        submissions.settle(side, row, "not_filed", detail=str(error))
        return SubmitReply(success=False, status="not_filed", submission=row, plan_hash=plan.plan_hash,
                           message=f"Couldn't reach the bridge ({error}); nothing was sent.")
    except BridgeNoAnswer as error:
        return _unknown(side, row, plan, str(error))
    named = submissions.known_request_id(None, answer)
    # Kept before anything else can fail: the next run's check looks up the ID this holds.
    try:
        submissions.save_answer(side, row, answer)
    except Exception as error:
        if named is None:
            raise
        _keep(row, named, answer, error)
        raise AnswerNotStored(row, named, answer, error) from error
    if why := accepted(status, answer):
        return _unknown(side, row, plan, why, answer)
    request_id, status = answer["id"], answer["status"]
    try:
        submissions.settle(side, row, "accepted", request_id=request_id, response=answer, submitted_at=now)
    except submissions.IdTaken as taken:
        return _unknown(side, row, plan, str(taken), answer)
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
