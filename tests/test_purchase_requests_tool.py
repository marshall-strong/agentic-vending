"""Tests for the `get_purchase_requests` tool's envelope: counts, messages and the stale flag.

The query underneath is tested against a database in `test_sync_requests.py`;
here it is replaced with fixed rows, so these run without one."""

from datetime import datetime, timezone

import pytest

from vending import purchase_requests as query
from vending.tools.purchase_requests import get_purchase_requests

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


def row(request_id, status="pending", last_checked="2026-11-09T10:00:00+00:00"):
    return {"request_id": request_id, "status": status, "submitted_at": "2026-11-07T14:00:00+00:00",
            "off_cycle": False, "plan_hash": "abc123", "decision_note": None, "total_paid": None,
            "fulfilled_at": None, "last_checked": last_checked, "plan": {"off_cycle": False, "lines": []},
            "lines": []}


class Stored(list):
    """The rows the fake query returns, and every `pending_only` it was asked with."""
    asked: list[bool]


@pytest.fixture
def rows(monkeypatch):
    stored = Stored()
    stored.asked = []

    def fake(conn, pending_only=False):
        stored.asked.append(pending_only)
        return [r for r in stored if not pending_only or r["status"] == "pending"]

    monkeypatch.setattr(query, "get_purchase_requests", fake)
    return stored


def test_no_requests_is_a_success_not_a_failure(rows):
    reply = get_purchase_requests(None, pending_only=False, now=NOW)
    assert (reply.success, reply.count, reply.message) == (True, 0, "No purchase requests yet.")
    assert get_purchase_requests(None, pending_only=True, now=NOW).message == "No pending purchase requests."


def test_counts_requests_and_pending_ones(rows):
    rows += [row("r2"), row("r1", status="fulfilled")]
    reply = get_purchase_requests(None, pending_only=False, now=NOW)
    assert (reply.count, reply.pending, reply.not_checked_recently) == (2, 1, [])
    assert [r.request_id for r in reply.data] == ["r2", "r1"]
    assert reply.message == "2 requests, newest first; 1 pending."


def test_pending_only_is_passed_through(rows):
    rows += [row("r2"), row("r1", status="fulfilled")]
    reply = get_purchase_requests(None, pending_only=True, now=NOW)
    assert [r.request_id for r in reply.data] == ["r2"]
    assert rows.asked == [True]


def test_flags_pending_requests_the_sync_has_not_read_lately(rows):
    rows += [
        row("never", last_checked=None),
        row("old", last_checked="2026-11-09T05:59:00+00:00"),
        row("fresh", last_checked="2026-11-09T06:00:00+00:00"),
        row("done", status="fulfilled", last_checked="2026-11-01T00:00:00+00:00"),
    ]
    reply = get_purchase_requests(None, pending_only=False, now=NOW)
    assert reply.not_checked_recently == ["never", "old"]
    assert "in the last 6 hours: never, old" in reply.message
    assert "python -m vending.sync_requests" in reply.message
