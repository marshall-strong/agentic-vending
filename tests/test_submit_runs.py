"""Two live agent runs in a row, through the real agent, the MCP server, the write gate and Postgres, against the
loopback fake bridge from `tests/test_desired_state.py` (#217). Only the model is scripted.

Whatever the first run's POST got back, the bridge must see one purchase POST across both runs unless the bridge
itself said, in full and in its documented words, that nothing was ordered. When in doubt the row stays open and
the second run is blocked. Each run checks open submissions before its model starts, as a real live run does."""

import json
from contextlib import asynccontextmanager

import pytest

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("anthropic")

import anyio  # noqa: E402
from mcp.shared.memory import create_connected_server_and_client_session  # noqa: E402

from tests.conftest import open_in  # noqa: E402
from tests.test_agent import Script, answer, tool_turn, use  # noqa: E402
from tests.test_desired_state import NOW, coke_sourcing, desired, fake, restock_15  # noqa: E402,F401
from tests.test_server import db  # noqa: E402,F401
from tests.test_submit import monday, prepared  # noqa: E402,F401
from vending import agent, server, submissions  # noqa: E402

BODY = desired(restock_15(fill_to=15), sourcing=[coke_sourcing()]).model_dump(mode="json")
PENDING = {"id": "pr-1", "status": "pending", "created_at": "2026-11-09T12:00:00Z"}


def mcp_tools():
    """The real MCP server, in memory, as the agent's tools."""
    @asynccontextmanager
    async def opened():
        async with create_connected_server_and_client_session(server.mcp._mcp_server) as session:
            yield (await session.list_tools()).tools, session.call_tool
    return opened


def live_run(schema_url, fake):  # noqa: F811
    """One live run whose model previews the restock, then submits it, then answers."""
    client = Script(tool_turn(use("preview_desired_state", {"desired": BODY}, id="p1")),
                    tool_turn(use("submit_desired_state", {"desired": BODY}, id="s1")),
                    answer("submit"), answer("submit"))
    return anyio.run(lambda: agent.run(live=True, connect=lambda: open_in(*schema_url), client=client,
                                       now=lambda: NOW, open_tools=mcp_tools(), bridge=fake.client))


def rows(db):  # noqa: F811
    return db.execute("select status, request_id from purchase_submissions order by id").fetchall()


def two_runs(db, fake, schema_url):  # noqa: F811
    prepared(db)
    live_run(schema_url, fake)
    first = rows(db)
    live_run(schema_url, fake)
    return first


def error_body(error_type, **fields):
    return {"error": error_type.replace("_", " "), "error_type": error_type, **fields}


LINE = {"index": 0, "slot": "15", "choice": "primary", "sku": "COKE20", "error_type": "alternate_same_as_primary",
        "reason": "the alternate is the primary"}


# --- 1. Only a complete, documented refusal closes the row as refused --------------------------------

@pytest.mark.parametrize("status, raw", [
    pytest.param(422, None, id="422 cut off part way"),
    pytest.param(422, b"", id="422 with no body"),
    pytest.param(429, b"", id="429 with no body"),
    pytest.param(403, b"<html>Forbidden</html>", id="403 from a proxy"),
    pytest.param(429, json.dumps(error_body("too_many_open_requests", id="pr-9")).encode(),
                 id="429 naming a request"),
    pytest.param(429, json.dumps(error_body("too_many_open_requests", status="pending")).encode(),
                 id="429 shaped like a filed request"),
    pytest.param(429, json.dumps(error_body("something_new")).encode(), id="429 of a type it doesn't document"),
    pytest.param(422, json.dumps({"detail": {"error_type": "invalid_lines"}}).encode(), id="422 not in its schema"),
    pytest.param(422, json.dumps(error_body("invalid_lines", items=[LINE], id="pr-9")).encode(),
                 id="invalid lines naming a request"),
    pytest.param(422, json.dumps(error_body("too_many_open_requests")).encode(), id="422 with a 429's type"),
])
def test_a_refusal_the_bridge_doesnt_document_in_full_never_lets_a_later_run_send_again(
        db, fake, schema_url, status, raw):  # noqa: F811
    if raw is None:
        fake.answer, fake.cut = (status, {}), True
    else:
        fake.answer, fake.raw = (status, {}), raw
    first = two_runs(db, fake, schema_url)
    assert first == [("unknown", None)]
    assert len(fake.posts) == 1 and rows(db) == [("unknown", None)]
    assert submissions.unresolved(db).code == "submission_unresolved"


@pytest.mark.parametrize("status, body", [
    pytest.param(422, error_body("invalid_lines", items=[LINE], detail=None), id="invalid lines"),
    pytest.param(422, error_body("insufficient_funds", id="pr-rejected"), id="insufficient funds"),
    pytest.param(422, {"detail": [{"loc": ["body", "lines"], "msg": "Field required", "type": "missing"}]},
                 id="FastAPI validation"),
    pytest.param(429, error_body("too_many_open_requests"), id="too many open requests"),
    pytest.param(429, error_body("submissions_locked", next_submission_from="2026-11-10T05:00:00Z"),
                 id="submissions locked"),
    pytest.param(403, error_body("forbidden"), id="403"),
])
def test_a_documented_refusal_is_refused_and_a_later_run_may_send(db, fake, schema_url, status, body):  # noqa: F811
    """The control: the bridge's own documented refusal does close the row, so the second run sends again,
    and the bridge refuses it again. The test above can tell the two apart."""
    fake.answer = (status, body)
    first = two_runs(db, fake, schema_url)
    assert first == [("refused", None)]
    assert len(fake.posts) == 2 and rows(db) == [("refused", None), ("refused", None)]


# --- 2. Only a documented 201 with a pending request, under an ID not used yet, is accepted -------------

@pytest.mark.parametrize("answer_", [
    pytest.param((200, {"id": "r1"}), id="200 naming a request already fulfilled"),
    pytest.param((201, {"id": "r1", "status": "pending", "created_at": "2026-11-09T12:00:00Z"}),
                 id="201 naming a request already fulfilled"),
    pytest.param((200, PENDING), id="200, not 201"),
    pytest.param((201, {"id": "pr-1"}), id="201 with no status or time"),
    pytest.param((201, {**PENDING, "status": "rejected"}), id="201 rejected"),
    pytest.param((201, {**PENDING, "id": 7}), id="201 with a number for an ID"),
])
def test_a_success_answer_out_of_its_schema_never_lets_a_later_run_send_again(
        db, fake, schema_url, answer_):  # noqa: F811
    fake.answer = answer_
    first = two_runs(db, fake, schema_url)
    assert [status for status, _ in first] == ["unknown"]
    assert len(fake.posts) == 1 and [status for status, _ in rows(db)] == ["unknown"]
    # r1 is the fixture's own fulfilled request: still recorded once, still fulfilled.
    assert db.execute("select status from purchase_requests where request_id = 'r1'").fetchall() == [("fulfilled",)]
    assert db.execute("select count(*) from purchase_requests where status = 'pending'").fetchone() == (0,)


def test_a_request_id_the_bridge_names_twice_is_never_settled_onto_a_new_attempt(db, fake, schema_url):  # noqa: F811
    """Even when the bridge confirms r1 on the second run's check, r1 is the fulfilled request it already was,
    not this attempt: the row stays open for a person."""
    fake.answer = (201, {"id": "r1", "status": "pending", "created_at": "2026-11-09T12:00:00Z"})
    fake.lookups = {"r1": {"id": "r1", "status": "pending", "created_at": "2026-11-09T12:00:00Z"}}
    two_runs(db, fake, schema_url)
    assert fake.looked_up == ["r1"]
    assert len(fake.posts) == 1 and [status for status, _ in rows(db)] == ["unknown"]


def test_a_documented_201_is_accepted_and_the_next_run_waits_for_it(db, fake, schema_url):  # noqa: F811
    first = two_runs(db, fake, schema_url)
    assert first == [("accepted", "pr-1")]
    assert len(fake.posts) == 1 and rows(db) == [("accepted", "pr-1")]


# --- 3. The ID of an accepted request is on the row before anything else can fail ------------------

def test_a_201_whose_recording_fails_is_settled_by_the_next_runs_check(db, fake, schema_url, monkeypatch):  # noqa: F811
    real, failed = submissions.record, []

    def fails_once(*args, **kwargs):
        if not failed:
            failed.append(True)
            raise psycopg.OperationalError("simulated: the database went away mid-settle")
        return real(*args, **kwargs)

    monkeypatch.setattr(submissions, "record", fails_once)
    fake.lookups = {"pr-1": PENDING}
    prepared(db)
    live_run(schema_url, fake)
    # The settle failed, but the bridge's answer, with its ID, is on the row.
    assert failed and rows(db) == [("sending", None)]
    assert db.execute("select response->>'id' from purchase_submissions").fetchone() == ("pr-1",)
    live_run(schema_url, fake)
    # The second run's check looked it up and settled it; its submit then saw the request pending.
    assert fake.looked_up == ["pr-1"]
    assert len(fake.posts) == 1 and rows(db) == [("accepted", "pr-1")]
    assert db.execute("select status from purchase_requests where request_id = 'pr-1'").fetchone() == ("pending",)


def test_a_reply_lost_whole_still_needs_a_person(db, fake, schema_url):  # noqa: F811
    """No ID came back at all: nothing to look up, so the row stays open and the second run is blocked."""
    fake.drop = True
    first = two_runs(db, fake, schema_url)
    assert first == [("unknown", None)]
    assert len(fake.posts) == 1 and fake.looked_up == [] and rows(db) == [("unknown", None)]
