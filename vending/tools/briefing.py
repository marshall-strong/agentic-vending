"""The startup briefing as an ordinary logged MCP read."""

from datetime import datetime

import psycopg

from vending import briefing
from vending.tools import Reply


class BriefingReply(Reply):
    computed_at: datetime
    snapshot_id: str
    text: str
    data: briefing.Briefing


def get_briefing(conn: psycopg.Connection, now: datetime | None = None) -> BriefingReply:
    data = briefing.build(conn, now)
    return BriefingReply(success=True, message=f"Startup snapshot {data.snapshot_id}: {data.status}.",
                         count=len(data.slots), computed_at=data.computed_at, snapshot_id=data.snapshot_id,
                         text=briefing.render(data), data=data)
