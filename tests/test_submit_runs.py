"""Live agent runs in a row, through the real agent, the MCP server, the write gate and Postgres, against the
loopback fake bridge from `tests/test_desired_state.py` (#217). Only the model is scripted.

Only the bridge's documented `201`, whole, settles a submission by itself. Every other answer to the POST leaves
the row `unknown`, so the bridge sees one POST across two runs: the second run's preview is refused
`submission_unresolved` and it sends nothing. A person's `python -m vending.submissions resolve` then unblocks
the next run. Each run checks open submissions before its model starts, as a real live run does."""

import gzip
import http.client
import json
import sys
from contextlib import asynccontextmanager

import pytest

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("anthropic")

import anyio  # noqa: E402
from mcp.shared.memory import create_connected_server_and_client_session  # noqa: E402

from tests.conftest import open_in, store_poll  # noqa: E402
from tests.test_agent import Script, answer, tool_turn, use  # noqa: E402
from tests.test_desired_state import NOW, coke_sourcing, desired, fake, restock_15  # noqa: E402,F401
from tests.test_server import db  # noqa: E402,F401
from tests.test_submit import monday, prepared  # noqa: E402,F401
from vending import agent, server, submissions  # noqa: E402
from vending import bridge as bridge_module  # noqa: E402
from vending import db as db_module  # noqa: E402

BODY = desired(restock_15(fill_to=15), sourcing=[coke_sourcing()]).model_dump(mode="json")
PENDING = {"id": "pr-1", "status": "pending", "created_at": "2026-11-09T12:00:00Z"}


def mcp_tools():
    """The real MCP server, in memory, as the agent's tools. The run's context (`agent.run_context`) goes into the
    environment the server reads, as it does for the real server's own process, and is taken out after."""
    @asynccontextmanager
    async def opened(context=None):
        import os

        saved = {k: os.environ.get(k) for k in context or {}}
        os.environ.update(context or {})
        try:
            async with create_connected_server_and_client_session(server.mcp._mcp_server) as session:
                yield (await session.list_tools()).tools, session.call_tool
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    return opened


def live_run(schema_url, fake, *turns):  # noqa: F811
    """One live run. By default its model previews the restock, submits it in the next turn, then answers."""
    turns = turns or (tool_turn(use("preview_desired_state", {"desired": BODY}, id="p1")),
                      tool_turn(use("submit_desired_state", {"desired": BODY}, id="s1")),
                      answer("submit"))
    return anyio.run(lambda: agent.run(live=True, connect=lambda: open_in(*schema_url), client=Script(*turns),
                                       now=lambda: NOW, open_tools=mcp_tools(), bridge=fake.client))


def retry_run(schema_url, fake):  # noqa: F811
    """A live run that previews and submits, then previews and submits once more if it is allowed to."""
    return live_run(schema_url, fake,
                    tool_turn(use("preview_desired_state", {"desired": BODY}, id="p1")),
                    tool_turn(use("submit_desired_state", {"desired": BODY}, id="s1")),
                    tool_turn(use("preview_desired_state", {"desired": BODY}, id="p2")),
                    tool_turn(use("submit_desired_state", {"desired": BODY}, id="s2")),
                    answer("submit"))


def rows(db):  # noqa: F811
    return db.execute("select status, request_id from purchase_submissions order by id").fetchall()


def results(db, run_id, tool):  # noqa: F811
    """What `tool` gave back to the model in run `run_id`, in order."""
    return [content for (content,) in db.execute(
        "select content from agent_steps where run_id = %s and kind = 'tool_result' and tool = %s order by id",
        (run_id, tool)).fetchall()]


def assert_blocked(db, run_id):  # noqa: F811
    """The run got as far as its preview, which was refused for the open submission, and it sent nothing."""
    [previewed] = results(db, run_id, "preview_desired_state")
    assert "submission_unresolved" in previewed
    [submitted] = results(db, run_id, "submit_desired_state")
    assert submitted.startswith("Not sent")


def resolve_by_person(schema_url, monkeypatch, row, *how):
    """`python -m vending.submissions resolve ROW ... --by kevin`, as a person types it."""
    monkeypatch.setattr(db_module, "connect", lambda: open_in(*schema_url))
    assert submissions.main(["resolve", str(row), *how, "--by", "kevin"]) == 0


def answer_with_201(fake):  # noqa: F811
    fake.answer, fake.raw, fake.cut, fake.drop, fake.redirect, fake.chunked = (201, PENDING), None, False, False, None, False
    fake.content_type, fake.headers, fake.drip = "application/json", {}, 0.0


def error_body(error_type, **fields):
    return {"error": error_type.replace("_", " "), "error_type": error_type, **fields}


LINE = {"index": 0, "slot": "15", "choice": "primary", "sku": "COKE20", "error_type": "alternate_same_as_primary",
        "reason": "the alternate is the primary"}


def raw(fake, status, body, **settings):  # noqa: F811
    fake.answer = (status, {})
    fake.raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    for name, value in settings.items():
        setattr(fake, name, value)


# --- 1. No answer but a validated 201 settles anything: two runs, one POST, then a person -----------

REFUSAL_SHAPED = [
    pytest.param(lambda f: (setattr(f, "answer", (422, {})), setattr(f, "cut", True)), id="truncated 422"),
    pytest.param(lambda f: raw(f, 422, error_body("invalid_lines", items=[LINE]), chunked=True), id="chunked 422"),
    pytest.param(lambda f: raw(f, 422, error_body("invalid_lines", items=[LINE]), content_type="text/plain"),
                 id="text/plain 422"),
    pytest.param(lambda f: raw(f, 429, b'{"error": "x", "error_type": "too_many_open_requests", '
                                       b'"error_type": "too_many_open_requests"}'), id="duplicate keys 429"),
    pytest.param(lambda f: raw(f, 422, {"detail": [{"loc": "body", "msg": "Field required", "type": "missing"}]}),
                 id="bad loc 422"),
    pytest.param(lambda f: raw(f, 422, gzip.compress(json.dumps(error_body("invalid_lines", items=[LINE])).encode()),
                               headers={"Content-Encoding": "gzip"}), id="gzip 422"),
    pytest.param(lambda f: raw(f, 422, error_body("invalid_lines", items=[LINE], detail=None)),
                 id="documented 422 invalid lines"),
    pytest.param(lambda f: raw(f, 422, error_body("insufficient_funds", id="pr-rejected")),
                 id="documented 422 insufficient funds"),
    pytest.param(lambda f: raw(f, 422, {"detail": [{"loc": ["body", "lines"], "msg": "Field required",
                                                    "type": "missing"}]}), id="documented 422 validation"),
    pytest.param(lambda f: raw(f, 429, error_body("too_many_open_requests")), id="documented 429"),
    pytest.param(lambda f: raw(f, 429, error_body("submissions_locked", next_submission_from="2026-11-10T05:00:00Z")),
                 id="documented 429 locked"),
    pytest.param(lambda f: raw(f, 403, error_body("forbidden")), id="documented 403"),
    pytest.param(lambda f: raw(f, 503, {"detail": "down"}), id="503"),
    pytest.param(lambda f: setattr(f, "redirect", "/api/v1/purchase-requests/pr-1"), id="303 redirect"),
    pytest.param(lambda f: setattr(f, "drop", True), id="closed with no reply"),
    pytest.param(lambda f: raw(f, 201, PENDING, content_type="text/plain"), id="201 as text/plain"),
    pytest.param(lambda f: raw(f, 201, gzip.compress(json.dumps(PENDING).encode()),
                               headers={"Content-Encoding": "gzip"}), id="201 gzip"),
    pytest.param(lambda f: raw(f, 201, b'{"id": "pr-1", "id": "pr-2", "status": "pending", '
                                       b'"created_at": "2026-11-09T12:00:00Z"}'), id="201 duplicate keys"),
    pytest.param(lambda f: raw(f, 200, PENDING), id="200, not 201"),
    pytest.param(lambda f: raw(f, 201, {"id": "pr-1"}), id="201 with no status or time"),
    pytest.param(lambda f: raw(f, 201, {**PENDING, "status": "rejected"}), id="201 rejected"),
]


@pytest.mark.parametrize("answer_", REFUSAL_SHAPED)
def test_only_a_validated_201_settles_so_two_runs_post_once_until_a_person_resolves(
        db, fake, schema_url, monkeypatch, answer_):  # noqa: F811
    prepared(db)
    answer_(fake)
    first_run, _ = live_run(schema_url, fake)
    assert rows(db) == [("unknown", None)]
    [submitted] = results(db, first_run, "submit_desired_state")
    assert "submission_unresolved" not in submitted and len(fake.posts) == 1     # the first run did send
    second_run, second = live_run(schema_url, fake)
    assert_blocked(db, second_run)
    assert len(fake.posts) == 1 and rows(db) == [("unknown", None)] and fake.looked_up == []
    assert submissions.unresolved(db).code == "submission_unresolved"
    # A person finds out the bridge never filed it, and says so: the next run sends again.
    resolve_by_person(schema_url, monkeypatch, 1, "--not-filed")
    answer_with_201(fake)
    live_run(schema_url, fake)
    assert len(fake.posts) == 2 and rows(db) == [("not_filed", None), ("accepted", "pr-1")]


def test_a_documented_201_is_accepted_and_the_next_run_waits_for_it(db, fake, schema_url):  # noqa: F811
    prepared(db)
    live_run(schema_url, fake)
    assert rows(db) == [("accepted", "pr-1")]
    second_run, _ = live_run(schema_url, fake)
    [previewed] = results(db, second_run, "preview_desired_state")
    assert "pending" in previewed           # the request just filed, still open, refuses a new plan
    assert len(fake.posts) == 1 and rows(db) == [("accepted", "pr-1")]


def test_a_chunked_201_is_still_the_documented_acceptance(db, fake, schema_url):  # noqa: F811
    prepared(db)
    raw(fake, 201, PENDING, chunked=True)
    live_run(schema_url, fake)
    assert rows(db) == [("accepted", "pr-1")]


def test_a_request_id_the_bridge_names_twice_is_never_settled_onto_a_new_attempt(db, fake, schema_url):  # noqa: F811
    """r1 is the fixture's fulfilled request. A 201 naming it isn't this attempt's, even when the bridge confirms
    r1 on the next run's check: the row stays open for a person."""
    prepared(db)
    fake.answer = (201, {**PENDING, "id": "r1"})
    fake.lookups = {"r1": {**PENDING, "id": "r1"}}
    live_run(schema_url, fake)
    second_run, _ = live_run(schema_url, fake)
    assert_blocked(db, second_run)
    assert len(fake.posts) == 1 and [status for status, _ in rows(db)] == ["unknown"]
    assert db.execute("select status from purchase_requests where request_id = 'r1'").fetchall() == [("fulfilled",)]


# --- 2. Startup recovery settles only a validated 201 it never recorded (Sol round 3, finding 3) ------

def test_an_id_from_an_ambiguous_answer_is_never_looked_up_or_attached(db, fake, schema_url):  # noqa: F811
    """Sol's case: a 200 naming an unrelated older request. The bridge confirms that request exists, but the
    answer wasn't the documented acceptance, so the request may not be this attempt's: the next run's check
    never looks it up, never marks the row accepted, and the row waits for a person."""
    prepared(db)
    raw(fake, 200, {"id": "pr-old-untracked", "status": "pending", "created_at": "2026-11-01T12:00:00Z"})
    fake.lookups = {"pr-old-untracked": {"id": "pr-old-untracked", "status": "pending",
                                         "created_at": "2026-11-01T12:00:00Z"}}
    live_run(schema_url, fake)
    second_run, _ = live_run(schema_url, fake)
    assert fake.looked_up == []
    assert_blocked(db, second_run)
    assert len(fake.posts) == 1 and rows(db) == [("unknown", None)]
    assert db.execute("select created_id from purchase_submissions").fetchone() == (None,)
    assert db.execute("select count(*) from purchase_requests where request_id = 'pr-old-untracked'").fetchone() == (0,)


def fail_first_record(monkeypatch):
    real, failed = submissions.record, []

    def fails_once(*args, **kwargs):
        if not failed:
            failed.append(True)
            raise psycopg.OperationalError("simulated: the database went away mid-settle")
        return real(*args, **kwargs)

    monkeypatch.setattr(submissions, "record", fails_once)
    return failed


def test_a_validated_201_whose_recording_failed_is_settled_by_the_next_runs_check(
        db, fake, schema_url, monkeypatch):  # noqa: F811
    failed = fail_first_record(monkeypatch)
    fake.lookups = {"pr-1": PENDING}
    prepared(db)
    live_run(schema_url, fake)
    # The settle failed, but the validated 201's ID is on the row, as `created_id`.
    assert failed and rows(db) == [("sending", None)]
    assert db.execute("select created_id from purchase_submissions").fetchone() == ("pr-1",)
    live_run(schema_url, fake)
    assert fake.looked_up == ["pr-1"]
    assert len(fake.posts) == 1 and rows(db) == [("accepted", "pr-1")]
    assert db.execute("select resolved_by from purchase_submissions").fetchone() == (submissions.AUTO_BY,)
    assert db.execute("select status from purchase_requests where request_id = 'pr-1'").fetchone() == ("pending",)


@pytest.mark.parametrize("lookup", [
    pytest.param({"id": "pr-1"}, id="only the id"),
    pytest.param({**PENDING, "status": "lost"}, id="an undocumented status"),
    pytest.param({**PENDING, "id": "pr-2"}, id="another request"),
    pytest.param(None, id="404"),
])
def test_a_get_that_doesnt_answer_for_the_id_leaves_the_row_open(db, fake, schema_url, monkeypatch, lookup):  # noqa: F811
    fail_first_record(monkeypatch)
    fake.lookups = {} if lookup is None else {"pr-1": lookup}
    prepared(db)
    live_run(schema_url, fake)
    second_run, _ = live_run(schema_url, fake)
    assert fake.looked_up == ["pr-1"]
    assert_blocked(db, second_run)
    assert len(fake.posts) == 1 and rows(db) == [("sending", None)]


# --- 3. A slow or huge answer is no answer, the success path included (Sol round 3, finding 5) --------

def test_a_201_dripped_past_the_deadline_is_unknown(db, fake, schema_url, monkeypatch):  # noqa: F811
    monkeypatch.setattr(bridge_module, "PURCHASE_DEADLINE_SECONDS", 1)
    prepared(db)
    raw(fake, 201, PENDING, drip=0.1)        # each byte well inside the read timeout; all of them, about 7 s
    first_run, _ = live_run(schema_url, fake)
    [submitted] = results(db, first_run, "submit_desired_state")
    assert "within 1 s" in submitted and rows(db) == [("unknown", None)]
    second_run, _ = live_run(schema_url, fake)
    assert_blocked(db, second_run)
    assert len(fake.posts) == 1


def test_a_201_past_the_size_cap_is_unknown(db, fake, schema_url, monkeypatch):  # noqa: F811
    monkeypatch.setattr(bridge_module, "PURCHASE_ANSWER_BYTES", 40)
    prepared(db)
    raw(fake, 201, PENDING)
    first_run, _ = live_run(schema_url, fake)
    assert rows(db) == [("unknown", None)]
    second_run, _ = live_run(schema_url, fake)
    assert_blocked(db, second_run)
    assert len(fake.posts) == 1


# --- 4. The only two retries: proven before any byte left -------------------------------------------

def test_a_failed_connect_allows_one_retry_in_the_run(db, fake, schema_url, monkeypatch):  # noqa: F811
    """Connecting for the first purchase POST fails. Nothing left, so the run previews again and sends once."""
    real, failed = http.client.HTTPConnection.connect, []

    def connect(self):
        caller = sys._getframe(1)       # the purchase POST's connect, not the catalog's (both go through `_exchange`)
        if not failed and caller.f_code.co_name == "_exchange" and caller.f_locals.get("method") == "POST":
            failed.append(True)
            raise ConnectionRefusedError("simulated: refused while connecting")
        return real(self)

    monkeypatch.setattr(http.client.HTTPConnection, "connect", connect)
    prepared(db)
    run_id, out = retry_run(schema_url, fake)
    assert failed and len(fake.posts) == 1
    assert rows(db) == [("not_filed", None), ("accepted", "pr-1")]
    assert out.status == "submitted" and "may submit once more" in results(db, run_id, "submit_desired_state")[0]


def test_plan_changed_allows_one_retry_in_the_run(db, fake, schema_url, monkeypatch):  # noqa: F811
    """Six more sold between the preview and the submit: our own check refuses `plan_changed` before any POST."""
    from vending.tools import submit as submit_module

    real, moved = submit_module.planning.plan, []

    def plan(conn, *args, **kwargs):
        if not moved and sys._getframe(1).f_code.co_filename == submit_module.__file__:    # the submit's own plan
            moved.append(True)
            store_poll(conn, "b", "2026-11-09T10:00:00+00:00", {"15": (250, 106)})
        return real(conn, *args, **kwargs)

    monkeypatch.setattr(submit_module.planning, "plan", plan)
    prepared(db)
    run_id, out = retry_run(schema_url, fake)
    first = results(db, run_id, "submit_desired_state")[0]
    assert moved and "plan_changed" in first and "may submit once more" in first
    assert len(fake.posts) == 1 and rows(db) == [("accepted", "pr-1")] and out.status == "submitted"


@pytest.mark.parametrize("answer_", [REFUSAL_SHAPED[6], REFUSAL_SHAPED[9]])
def test_a_documented_refusal_allows_no_retry_in_the_run(db, fake, schema_url, answer_):  # noqa: F811
    prepared(db)
    answer_(fake)
    run_id, out = retry_run(schema_url, fake)
    assert len(fake.posts) == 1 and rows(db) == [("unknown", None)] and out.status == "failed"
    assert results(db, run_id, "submit_desired_state")[1].startswith("Not sent")
