"""`get_purchase_requests` (tool #7): our purchase requests, the plan we sent, and every receipt.

The query is `vending.purchase_requests.get_purchase_requests`; this module
wraps it in the tools' reply envelope. Nothing here calls the bridge: requests
are only as fresh as the last run of `python -m vending.sync_requests`, so the
reply flags pending requests the sync hasn't read recently. Otherwise the agent
could keep waiting on a request the worker finished hours ago.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
from pydantic import BaseModel

from vending import purchase_requests
from vending.tools import Reply

# A pending request last read longer ago than this is flagged. A placeholder:
# the team hasn't set how often the request sync runs.
STALE_AFTER = timedelta(hours=6)


class PurchaseRequest(BaseModel):
    request_id: str
    status: str                     # pending, rejected or fulfilled
    submitted_at: str
    off_cycle: bool
    plan_hash: str | None
    decision_note: str | None
    total_paid: str | None          # exact decimal string, as the bridge sent it
    fulfilled_at: str | None
    last_checked: str | None        # when the sync last read it; null if never
    plan: dict[str, Any]            # the body we POSTed
    lines: list[dict[str, Any]]     # what we asked for per line, with its receipt and later losses


class PurchaseRequests(Reply):
    pending: int = 0
    not_checked_recently: list[str] = []
    data: list[PurchaseRequest] = []


def get_purchase_requests(conn: psycopg.Connection, pending_only: bool,
                          now: datetime | None = None) -> PurchaseRequests:
    now = now or datetime.now(timezone.utc)
    rows = [PurchaseRequest(**r) for r in purchase_requests.get_purchase_requests(conn, pending_only)]
    if not rows:
        return PurchaseRequests(success=True, message=(
            "No pending purchase requests." if pending_only else "No purchase requests yet."))
    open_ = [r for r in rows if r.status == "pending"]
    # A request the sync hasn't read yet counts from when it was submitted, so
    # one submitted a minute ago isn't flagged.
    stale = [r.request_id for r in open_
             if now - datetime.fromisoformat(r.last_checked or r.submitted_at) > STALE_AFTER]
    message = f"{len(rows)} request{'s' if len(rows) != 1 else ''}, newest first; {len(open_)} pending."
    if stale:
        message += (f" Not read from the bridge in the last {STALE_AFTER.total_seconds() / 3600:g} hours: "
                    f"{', '.join(stale)}. Their status may be out of date; run python -m vending.sync_requests")
    return PurchaseRequests(success=True, message=message, count=len(rows), pending=len(open_),
                            not_checked_recently=stale, data=rows)
