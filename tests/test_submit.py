"""`submit_desired_state` through the MCP client, the write gate and Postgres, against the
local fake bridge from `tests/test_desired_state.py`."""

import http.client
import json
import socket
import threading

import pytest

psycopg = pytest.importorskip("psycopg")

from datetime import datetime, timezone  # noqa: E402

from tests.conftest import open_in, store_poll  # noqa: E402
from tests.test_desired_state import NOW, coke_sourcing, desired, fake, ready, restock_15, stocked  # noqa: E402,F401
from tests.test_server import call, db  # noqa: E402,F401
from tests.test_sync_requests import fulfilled, receipt  # noqa: E402
from vending import ledger, submissions  # noqa: E402
from vending.config import set_value  # noqa: E402
from vending.purchase_requests import store  # noqa: E402



@pytest.fixture(autouse=True)
def monday(monkeypatch):
    """The database clock pinned to `NOW`, a Monday before every cutoff. These tests go through the MCP
    server, which reads "now" from the database (`ledger.db_now`), so on the real clock they all failed from
    Thursday 23:59 to the week's turn with `past_cutoff`."""
    monkeypatch.setattr(ledger, "db_now", lambda conn: NOW)

def submit(expected_plan_hash=None, **arguments):
    body = desired(restock_15(fill_to=15), sourcing=[coke_sourcing()], **arguments).model_dump()
    args = {"desired": body}
    if expected_plan_hash:
        args["expected_plan_hash"] = expected_plan_hash
    result = call("submit_desired_state", args)
    assert not result.isError, result.content[0].text
    return result.structuredContent


def preview():
    body = desired(restock_15(fill_to=15), sourcing=[coke_sourcing()]).model_dump()
    return call("preview_desired_state", {"desired": body}).structuredContent


def codes(reply):
    return sorted(r["code"] for r in reply["refusals"])


def prepared(db):  # noqa: F811
    ready(db, capacity=15)
    stocked(db)


def test_an_accepted_request_is_recorded_everywhere(db, fake):  # noqa: F811
    prepared(db)
    reply = submit()
    assert reply["success"] and (reply["request_id"], reply["status"], reply["lines"]) == ("pr-1", "pending", 1)
    assert reply["max_cost"] == "13.00" and "Shopping list" in reply["directive"]
    [posted] = fake.posts
    assert posted["off_cycle"] is False and posted["lines"][0]["slot"] == "15"
    assert posted["lines"][0]["primary"]["max_price_per_pack"] == "13.00"
    # The fixture's fulfilled request r1 is there too; the new one is the pending row.
    assert db.execute("select request_id, status, plan_hash, request_body from purchase_requests"
                      " where status = 'pending'").fetchall() == [("pr-1", "pending", reply["plan_hash"], posted)]
    assert db.execute("select status, request_id from purchase_submissions").fetchall() == [("accepted", "pr-1")]
    assert db.execute("select outcome from action_log where tool = 'submit_desired_state'").fetchall() == [("done",)]
    # Not off-cycle, so no visit fee travels with it.
    assert db.execute("select visit_fee_cents from purchase_requests where request_id = 'pr-1'").fetchone() == (None,)
    # The request is pending now, so the same plan is refused until the worker is done.
    again = submit()
    assert codes(again) == ["request_pending"] and len(fake.posts) == 1


def test_an_off_cycle_request_carries_the_fee_it_was_quoted(db, fake):  # noqa: F811
    prepared(db)
    set_value(db, "midweek_fee_cents", 3000, "test")
    reply = submit(off_cycle=True)
    assert reply["success"] and "including the midweek visit fee" in reply["message"]
    assert db.execute("select visit_fee_cents from purchase_submissions").fetchone() == (3000,)
    assert db.execute("select off_cycle, visit_fee_cents from purchase_requests where request_id = 'pr-1'"
                      ).fetchone() == (True, 3000)
    # The fee changes afterwards; once the worker is done, the ledger still charges what was quoted.
    set_value(db, "midweek_fee_cents", 9900, "test")
    store(db, "pr-1", fulfilled("pr-1", receipt(resolved_at="2026-11-10T16:00:00Z")),
          datetime(2026, 11, 10, 17, tzinfo=timezone.utc))
    assert ledger.balance(db, datetime(2026, 11, 11, tzinfo=timezone.utc)).visit_fees_cents == 3000


def test_a_resolved_submission_carries_the_fee_too(db, fake, schema_url, monkeypatch):  # noqa: F811
    prepared(db)
    set_value(db, "midweek_fee_cents", 3000, "test")
    fake.answer = (503, {"detail": "down"})
    assert submit(off_cycle=True)["status"] == "unknown"
    assert submissions.resolve(db, 1, "pr-x", "kevin").startswith("Submission 1 recorded as filed")
    assert db.execute("select off_cycle, visit_fee_cents from purchase_requests where request_id = 'pr-x'"
                      ).fetchone() == (True, 3000)


def test_a_refused_plan_sends_nothing_and_saves_nothing(db, fake):  # noqa: F811
    prepared(db)
    body = desired(restock_15(price="1.20", fill_to=15), sourcing=[coke_sourcing()]).model_dump()
    reply = call("submit_desired_state", {"desired": body}).structuredContent
    assert not reply["success"] and codes(reply) == ["below_floor"]
    assert fake.posts == [] and db.execute("select count(*) from purchase_submissions").fetchone() == (0,)
    assert db.execute("select outcome from action_log where tool = 'submit_desired_state'").fetchall() == [("refused",)]


def test_not_ready_is_a_failure_not_a_refusal(db, fake):  # noqa: F811
    ready(db, capacity=15)
    fake.catalog_status = 503
    reply = submit()
    assert not reply["success"] and "Can't submit yet" in reply["message"] and "couldn't read the catalog" in reply["message"]
    assert fake.posts == []
    assert db.execute("select outcome from action_log where tool = 'submit_desired_state'").fetchall() == [("failed",)]


@pytest.mark.parametrize("answer", [
    (422, {"error": "invalid lines", "error_type": "invalid_lines", "items": [
        {"index": 0, "slot": "15", "choice": "alternate", "sku": "COKE20", "error_type": "alternate_same_as_primary",
         "reason": "the alternate is the primary"}]}),
    (429, {"error": "too many open requests", "error_type": "too_many_open_requests"}),
    (403, {"error": "forbidden", "error_type": "instructor_key"}),
], ids=["422", "429", "403"])
def test_the_bridges_documented_refusal_is_unknown_with_its_words_until_a_person_resolves(db, fake, answer):  # noqa: F811
    """#217: even the bridge's documented refusal settles nothing by itself. Its words reach the agent and the
    record, but the row stays `unknown` and every submit is refused until a person resolves it."""
    prepared(db)
    fake.answer = answer
    reply = submit()
    assert not reply["success"] and reply["status"] == "unknown" and reply["submission"] == 1
    assert str(answer[0]) in reply["message"] and answer[1]["error_type"] in reply["message"]
    assert "resolve 1" in reply["message"] and not reply["refusals"]
    assert db.execute("select status, response from purchase_submissions").fetchall() == [("unknown", answer[1])]
    assert db.execute("select count(*) from purchase_requests where status = 'pending'").fetchone() == (0,)
    fake.answer = (201, {"id": "pr-2", "status": "pending", "created_at": "2026-11-09T12:00:00Z"})
    assert codes(submit()) == ["submission_unresolved"] and len(fake.posts) == 1
    # A person finds it was never filed: a corrected plan may be sent.
    submissions.resolve(db, 1, None, "kevin")
    assert submit()["success"] and len(fake.posts) == 2


@pytest.mark.parametrize("answer", [(503, {"detail": "down"}), (201, {"status": "pending"})])
def test_no_usable_answer_blocks_every_submit_until_a_person_settles_it(db, fake, answer):  # noqa: F811
    prepared(db)
    fake.answer = answer
    reply = submit()
    assert not reply["success"] and reply["status"] == "unknown" and "resolve 1" in reply["message"]
    assert db.execute("select status from purchase_submissions").fetchall() == [("unknown",)]
    assert db.execute("select count(*) from purchase_requests where status = 'pending'").fetchone() == (0,)
    fake.answer = (201, {"id": "pr-9", "status": "pending", "created_at": "2026-11-09T12:00:00Z"})
    blocked = submit()
    assert codes(blocked) == ["submission_unresolved"] and len(fake.posts) == 1
    # The person finds it on the bridge: now it is a pending request like any other.
    submissions.resolve(db, 1, "pr-found", "kevin")
    assert db.execute("select request_id, status from purchase_requests where status = 'pending'").fetchall() == [
        ("pr-found", "pending")]
    assert codes(submit()) == ["request_pending"]


def test_a_submission_the_bridge_never_filed_can_be_sent_again(db, fake):  # noqa: F811
    prepared(db)
    fake.answer = (503, {"detail": "down"})
    submit()
    submissions.resolve(db, 1, None, "kevin")
    assert db.execute("select status, resolved_by from purchase_submissions").fetchall() == [("not_filed", "kevin")]
    fake.answer = (201, {"id": "pr-2", "status": "pending", "created_at": "2026-11-09T12:00:00Z"})
    assert submit()["success"] and len(fake.posts) == 2
    assert db.execute("select tool from action_log where tool = 'submissions.resolve'").fetchall() == [("submissions.resolve",)]


def test_a_settle_failure_after_the_bridge_accepted_still_names_the_request_id(db, fake, monkeypatch, capsys):  # noqa: F811
    prepared(db)

    def crash(*args, **kwargs):
        raise RuntimeError("simulated: the database dropped after the POST")

    original = submissions.settle
    monkeypatch.setattr(submissions, "settle", crash)
    body = desired(restock_15(fill_to=15), sourcing=[coke_sourcing()]).model_dump()
    result = call("submit_desired_state", {"desired": body})
    assert result.isError
    text = result.content[0].text
    # The agent is told the ID and what a person must do; nothing says "try again".
    assert "ACCEPTED purchase request pr-1" in text and "resolve 1 --request-id pr-1" in text
    assert "Don't submit again" in text
    # The answer is kept on stderr, where the server log has it even if the database stays down.
    err = capsys.readouterr().err
    assert "LOST ANSWER" in err and '"request_id": "pr-1"' in err
    assert len(fake.posts) == 1
    assert db.execute("select status from purchase_submissions").fetchall() == [("sending",)]
    [(detail,)] = db.execute("select detail from action_log where tool = 'submit_desired_state'").fetchall()
    assert "pr-1" in detail
    monkeypatch.setattr(submissions, "settle", original)
    assert codes(submit()) == ["submission_unresolved"] and len(fake.posts) == 1
    submissions.resolve(db, 1, "pr-1", "kevin")
    assert db.execute("select request_id from purchase_requests where status = 'pending'").fetchall() == [("pr-1",)]


def test_a_side_connection_that_fails_before_the_post_sends_nothing(db, fake, schema_url, monkeypatch):  # noqa: F811
    prepared(db)
    from vending import server

    def refuse():
        raise psycopg.OperationalError("too many connections")

    body = desired(restock_15(fill_to=15), sourcing=[coke_sourcing()]).model_dump()
    # The gate keeps its own connection; only the side connection the submit opens fails.
    with open_in(*schema_url) as gate:
        monkeypatch.setattr(server, "_conn", gate)
        monkeypatch.setattr(server, "connect", refuse)
        result = call("submit_desired_state", {"desired": body})
    assert result.isError and "too many connections" in result.content[0].text
    assert fake.posts == [] and db.execute("select count(*) from purchase_submissions").fetchone() == (0,)


def test_a_cut_off_answer_is_unknown_not_a_crash(db, fake):  # noqa: F811
    prepared(db)
    fake.cut = True
    reply = submit()
    assert not reply["success"] and reply["status"] == "unknown"
    assert "IncompleteRead" in reply["message"] and "Don't submit again" in reply["message"]
    assert db.execute("select status from purchase_submissions").fetchall() == [("unknown",)]


def test_a_5xx_keeps_the_body_the_bridge_sent(db, fake):  # noqa: F811
    prepared(db)
    fake.answer = (503, {"detail": "worker portal is being redeployed"})
    reply = submit()
    assert reply["status"] == "unknown" and "503" in reply["message"] and "redeployed" in reply["message"]
    assert "redeployed" in db.execute("select detail from purchase_submissions").fetchone()[0]


def test_a_plan_that_changed_since_the_preview_is_refused_when_its_hash_is_given(db, fake):  # noqa: F811
    prepared(db)
    previewed = preview()
    assert previewed["success"] and previewed["plan_hash"]
    # Six more sold since the preview: the rail holds 2, so the plan now loads 13, not 7,
    # which takes a second pack of 12.
    store_poll(db, "b", "2026-11-09T10:00:00+00:00", {"15": (250, 106)})
    reply = submit(expected_plan_hash=previewed["plan_hash"])
    assert codes(reply) == ["plan_changed"] and previewed["plan_hash"] in reply["refusals"][0]["message"]
    # Refused before any POST: no submission number, which is what lets the agent try once more.
    assert fake.posts == [] and reply["submission"] is None
    again = preview()
    assert again["plan_hash"] != previewed["plan_hash"]
    assert submit(expected_plan_hash=again["plan_hash"])["success"] and len(fake.posts) == 1


def test_a_new_visit_fee_since_the_preview_is_a_changed_plan(db, fake):  # noqa: F811
    prepared(db)
    body = desired(restock_15(fill_to=15), off_cycle=True, sourcing=[coke_sourcing()]).model_dump()
    previewed = call("preview_desired_state", {"desired": body}).structuredContent
    assert previewed["success"]
    # The request body is the same; only what the visit costs moved.
    set_value(db, "midweek_fee_cents", 3000, "test")
    reply = call("submit_desired_state", {"desired": body, "expected_plan_hash": previewed["plan_hash"]}).structuredContent
    assert codes(reply) == ["plan_changed"] and fake.posts == []


def test_a_plan_refused_for_other_reasons_is_not_also_called_changed(db, fake):  # noqa: F811
    prepared(db)
    body = desired(restock_15(fill_to=999), sourcing=[coke_sourcing()]).model_dump()
    reply = call("submit_desired_state", {"desired": body, "expected_plan_hash": "previewed"}).structuredContent
    assert reply["refusals"] and "plan_changed" not in codes(reply)


@pytest.mark.parametrize("status", [403, 422, 429, 503])
def test_an_error_answer_cut_off_part_way_is_unknown_and_blocks_the_next_submit(db, fake, status):  # noqa: F811
    """A cut-off answer is no answer, whatever its status: `unknown`, and nothing more is sent until a person
    settles it. `tests/test_submit_runs.py` runs the same through two live agent runs."""
    prepared(db)
    fake.answer, fake.cut = (status, {}), True
    reply = submit()
    assert not reply["success"] and reply["status"] == "unknown" and str(status) in reply["message"]
    assert db.execute("select status from purchase_submissions").fetchall() == [("unknown",)]
    fake.cut = False
    assert codes(submit(preview().get("plan_hash"))) == ["submission_unresolved"] and len(fake.posts) == 1


def test_preview_refuses_too_while_a_submission_is_unresolved(db, fake):  # noqa: F811
    prepared(db)
    fake.answer = (503, {"detail": "down"})
    assert submit()["status"] == "unknown"
    previewed = preview()
    assert not previewed["success"] and [r["code"] for r in previewed["refusals"]] == ["submission_unresolved"]
    assert "ask the instructors" in previewed["refusals"][0]["message"]


def test_every_refusal_is_shown_at_once_including_an_unresolved_submission(db, fake):  # noqa: F811
    prepared(db)
    fake.answer = (503, {"detail": "down"})
    submit()
    body = desired(restock_15(price="1.20", fill_to=15), sourcing=[coke_sourcing()]).model_dump()
    reply = call("submit_desired_state", {"desired": body}).structuredContent
    assert codes(reply) == ["below_floor", "submission_unresolved"]


def test_two_concurrent_submits_file_one_request(db, fake, schema_url):  # noqa: F811
    prepared(db)
    from vending.tools import submit as submit_tool
    from vending.write_gate import Refused, run_write
    plan = desired(restock_15(fill_to=15), sourcing=[coke_sourcing()])
    outcomes = []

    def one():
        with open_in(*schema_url) as conn:
            action = submit_tool.submit_desired_state(plan, fake.client, lambda: open_in(*schema_url))
            try:
                outcomes.append(("ok", run_write(conn, "submit_desired_state", {}, action).request_id))
            except Refused as refused:
                outcomes.append(("refused", sorted(r.code for r in refused.refusals)))

    threads = [threading.Thread(target=one) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    # The halt-row lock serializes them: the second plans after the first's request is pending.
    assert sorted(outcomes) == [("ok", "pr-1"), ("refused", ["request_pending"])]
    assert len(fake.posts) == 1


def test_unreachable_bridge_is_not_filed(db, fake, monkeypatch):  # noqa: F811
    prepared(db)
    from vending import server
    from vending.bridge import Bridge
    # The catalog is read first; point only the POST at a closed port by swapping the client after planning.
    real = fake.client()

    class HalfDead(Bridge):
        def post_answer(self, path, body):
            return Bridge("http://127.0.0.1:9", "test-key").post_answer(path, body)

    monkeypatch.setattr(server, "bridge", lambda: HalfDead(real.base_url, "test-key"))
    reply = submit()
    assert not reply["success"] and reply["status"] == "not_filed" and reply["submission"] == 1
    assert "Couldn't connect to the bridge" in reply["message"] and "nothing was sent" in reply["message"]
    assert db.execute("select status from purchase_submissions").fetchall() == [("not_filed",)]


# --- Never a second order after a POST that may have arrived (#217) ---------------------------
# Each test sends the way the agent would retry: a fresh preview, then a submit with its hash. Whatever the
# first POST got back, the bridge must see one POST only.


def test_a_post_answered_with_a_redirect_is_never_sent_twice(db, fake):  # noqa: F811
    prepared(db)
    fake.redirect = "/api/v1/purchase-requests/pr-1"     # a GET there is a 404, which looked like a refusal
    first = submit(preview()["plan_hash"])
    second = submit(preview().get("plan_hash"))
    assert len(fake.posts) == 1
    assert fake.other_gets == []                            # the redirect isn't followed
    assert first["status"] == "unknown" and "303" in first["message"]
    assert codes(second) == ["submission_unresolved"]
    assert db.execute("select status from purchase_submissions").fetchall() == [("unknown",)]


@pytest.mark.parametrize("error", [ConnectionRefusedError, socket.gaierror, ConnectionResetError,
                                   BrokenPipeError, TimeoutError])
def test_a_send_error_after_the_post_arrived_is_never_sent_twice(db, fake, monkeypatch, error):  # noqa: F811
    """An error raised once connected is no proof the request didn't arrive, whatever its type: only a failed
    connect is."""
    prepared(db)
    real = http.client.HTTPConnection.send
    posting = []

    def send(self, data):
        real(self, data)
        if isinstance(data, bytes) and data.startswith(b"POST /api/v1/purchase-requests"):
            posting.append(True)
        elif posting:
            posting.clear()
            assert fake.received.wait(10)                   # the whole POST is at the bridge
            raise error("simulated: the socket failed once the body was written")

    monkeypatch.setattr(http.client.HTTPConnection, "send", send)
    first = submit(preview()["plan_hash"])
    second = submit(preview().get("plan_hash"))
    assert len(fake.posts) == 1
    assert first["status"] == "unknown" and "may have arrived" in first["message"]
    assert codes(second) == ["submission_unresolved"]
    assert db.execute("select status from purchase_submissions").fetchall() == [("unknown",)]


@pytest.mark.parametrize("status", [400, 404, 405, 409])
def test_a_4xx_the_bridge_doesnt_document_for_this_route_is_unknown(db, fake, status):  # noqa: F811
    """Like every answer but the documented 201, these leave the row `unknown`."""
    prepared(db)
    fake.answer = (status, {"detail": "?"})
    first = submit(preview()["plan_hash"])
    second = submit(preview().get("plan_hash"))
    assert len(fake.posts) == 1 and first["status"] == "unknown" and str(status) in first["message"]
    assert codes(second) == ["submission_unresolved"]


@pytest.mark.parametrize("lost", ["closed before any reply", "read timeout"])
def test_a_lost_reply_stays_blocked_for_later_runs(db, fake, monkeypatch, lost):  # noqa: F811
    """#217's lost reply: the bridge has the POST, and no answer comes back. Nothing we hold names the request
    on the bridge, and the bridge has no lookup by anything else, so neither the check nor a retry can settle it:
    a person must."""
    from vending import bridge as bridge_module

    prepared(db)
    if lost == "read timeout":
        monkeypatch.setattr(bridge_module, "TIMEOUT_SECONDS", 1)
        fake.hold = threading.Event()
    else:
        fake.drop = True
    try:
        first = submit(preview()["plan_hash"])
    finally:
        if fake.hold is not None:
            fake.hold.set()
    assert first["status"] == "unknown" and len(fake.posts) == 1
    assert db.execute("select status, request_id, response from purchase_submissions").fetchall() == [
        ("unknown", None, None)]
    # A later live run checks before its model starts; this row holds nothing to look up.
    [checked] = submissions.check(db, fake.client)
    assert checked.status == "unknown" and "no validated 201" in checked.why
    fake.drop, fake.hold = False, None
    second = submit(preview().get("plan_hash"))
    assert codes(second) == ["submission_unresolved"] and len(fake.posts) == 1


def test_halted_refuses_before_planning(db, fake):  # noqa: F811
    prepared(db)
    from vending.config import set_halted
    set_halted(db, True, "test")
    reply = submit()
    assert codes(reply) == ["halted"] and fake.gets == 0 and fake.posts == []


def test_cli_lists_and_resolves(db, fake, schema_url, monkeypatch, capsys):  # noqa: F811
    prepared(db)
    fake.answer = (503, {"detail": "down"})
    submit()
    from vending import db as db_module
    monkeypatch.setattr(db_module, "connect", lambda: open_in(*schema_url))
    assert submissions.main(["list"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("1  ") and "unknown" in out and "1 lines" in out
    assert submissions.main(["resolve", "1", "--request-id", "pr-x", "--by", "kevin"]) == 0
    assert "recorded as filed under pr-x" in capsys.readouterr().out
    assert submissions.main(["resolve", "1", "--not-filed", "--by", "kevin"]) == 1
    assert "not sending or unknown" in capsys.readouterr().err
    assert submissions.main(["list"]) == 0
    assert "Nothing to settle" in capsys.readouterr().out


@pytest.mark.parametrize("given", ["", "   "])
def test_an_empty_request_id_is_refused_and_the_row_stays_open(db, fake, schema_url, monkeypatch, capsys, given):  # noqa: F811
    """Sol round 4, finding 3: `--request-id ""` is a typo, not `--not-filed`. It used to mark the row never
    filed, which unblocks the next submit; now it exits nonzero and writes nothing."""
    prepared(db)
    fake.answer = (503, {"detail": "down"})
    submit()
    from vending import db as db_module
    monkeypatch.setattr(db_module, "connect", lambda: open_in(*schema_url))
    assert submissions.main(["resolve", "1", "--request-id", given, "--by", "kevin"]) == 1
    assert "--request-id is empty" in capsys.readouterr().err
    assert db.execute("select status, request_id, resolved_by from purchase_submissions").fetchall() == [
        ("unknown", None, None)]
    assert db.execute("select count(*) from action_log where tool = 'submissions.resolve'").fetchone() == (0,)
    with pytest.raises(ValueError, match="empty"):
        submissions.resolve(db, 1, given, "kevin")
    assert codes(submit()) == ["submission_unresolved"] and len(fake.posts) == 1


def test_a_fulfillment_committed_while_submit_reads_never_files_the_plan_twice(db, fake, monkeypatch):  # noqa: F811
    """Sol round 4, finding 1. The receipt sync fulfills pr-1 on another connection after the second submit
    has read the receipts but before it reads the pending requests (as the old order did). Read that way it
    saw neither the delivery nor a pending request, and sent the same plan again. Pending requests are now
    read first, so the request is still pending to it and the plan is refused."""
    from vending import planning
    prepared(db)
    assert submit()["success"] and len(fake.posts) == 1
    real = planning._latest_prices
    fired = []

    def fulfil_then_read(conn, slots):
        if not fired:
            fired.append(True)
            store(db, "pr-1", fulfilled("pr-1", receipt(units_loaded=7, units_to_back_stock=4,
                                                        resolved_at="2026-11-09T11:00:00Z")),
                  NOW)
        return real(conn, slots)

    monkeypatch.setattr(planning, "_latest_prices", fulfil_then_read)
    again = submit()
    assert fired and not again["success"] and codes(again) == ["request_pending"]
    assert len(fake.posts) == 1
    assert db.execute("select status from purchase_requests where request_id = 'pr-1'").fetchone() == ("fulfilled",)


def test_a_slow_body_after_connection_close_frees_the_write_lock_at_the_deadline(db, fake, schema_url, monkeypatch):  # noqa: F811
    """Sol round 4, finding 2: a `201` with `Connection: close` whose headers come at once and whose body
    drips. `http.client` drops `conn.sock`, so the deadline had no socket to shut and the submit held the
    write lock for as long as the body took. It now ends at the deadline (here 2 s, not 30, to keep the test
    short), the row is `unknown`, and the lock is free."""
    import time

    from vending import bridge as bridge_module
    from vending.config import is_halted
    from vending.tools import submit as submit_tool
    from vending.write_gate import run_write
    monkeypatch.setattr(bridge_module, "PURCHASE_DEADLINE_SECONDS", 2)
    prepared(db)
    fake.answer = (201, {})
    fake.raw = json.dumps({"id": "pr-1", "status": "pending", "created_at": "2026-11-09T12:00:00Z"}).encode()
    fake.headers, fake.drip = {"Connection": "close"}, 0.25     # the body alone would take about 18 s
    plan = desired(restock_15(fill_to=15), sourcing=[coke_sourcing()])
    replies = []

    def one():
        with open_in(*schema_url) as conn:
            action = submit_tool.submit_desired_state(plan, fake.client, lambda: open_in(*schema_url))
            replies.append(run_write(conn, "submit_desired_state", {}, action))

    started = time.monotonic()
    thread = threading.Thread(target=one)
    thread.start()
    assert fake.received.wait(10)
    time.sleep(max(0.0, started + 3.5 - time.monotonic()))      # the deadline plus a margin
    with db.transaction():
        db.execute("set local lock_timeout = '200ms'")
        is_halted(db, lock=True)        # raises LockNotAvailable while the submit still holds it
    thread.join(timeout=30)
    [reply] = replies
    assert reply.status == "unknown" and "within 2 s" in reply.message
    assert db.execute("select status from purchase_submissions").fetchall() == [("unknown",)]
    assert len(fake.posts) == 1


def test_settle_never_overwrites_a_row_a_person_already_settled(db, schema_url):  # noqa: F811
    with open_in(*schema_url) as side:
        row = submissions.save_sending(side, "h", {"lines": [], "off_cycle": False}, None)
        submissions.resolve(db, row, None, "kevin")
        with pytest.raises(submissions.AlreadySettled, match="not_filed by kevin"):
            submissions.settle(side, row, "accepted", request_id="pr-1", response={"id": "pr-1"},
                               submitted_at=datetime(2026, 11, 9, tzinfo=timezone.utc))
    assert db.execute("select status, request_id, resolved_by from purchase_submissions").fetchall() == [
        ("not_filed", None, "kevin")]
    assert db.execute("select count(*) from purchase_requests where request_id = 'pr-1'").fetchone() == (0,)


def test_resolve_waits_for_a_submit_in_flight(db, fake, schema_url):  # noqa: F811
    prepared(db)
    from vending.tools import submit as submit_tool
    from vending.write_gate import run_write
    plan = desired(restock_15(fill_to=15), sourcing=[coke_sourcing()])
    fake.hold = threading.Event()
    results = {}

    def sending():
        with open_in(*schema_url) as conn:
            action = submit_tool.submit_desired_state(plan, fake.client, lambda: open_in(*schema_url))
            results["submit"] = run_write(conn, "submit_desired_state", {}, action).request_id

    def resolving():
        with open_in(*schema_url) as conn:
            try:
                results["resolve"] = submissions.resolve(conn, 1, None, "kevin")
            except ValueError as error:
                results["resolve"] = str(error)

    submitter = threading.Thread(target=sending)
    submitter.start()
    # The POST has arrived and isn't answered yet, so submission 1 is `sending`.
    assert fake.received.wait(30)
    resolver = threading.Thread(target=resolving)
    resolver.start()
    resolver.join(timeout=1)
    assert resolver.is_alive(), "resolve settled the row while its request was still in flight"
    fake.hold.set()
    submitter.join(timeout=60)
    resolver.join(timeout=60)
    # The bridge's answer is recorded; the person's resolve then finds nothing left to settle.
    assert results["submit"] == "pr-1"
    assert "is accepted, not sending or unknown" in results["resolve"]
    assert db.execute("select status, request_id, resolved_by from purchase_submissions").fetchall() == [
        ("accepted", "pr-1", None)]


def test_resolve_gives_up_waiting_when_asked_to(db, schema_url):  # noqa: F811
    from vending.config import is_halted, set_halted
    set_halted(db, False, "test")
    with open_in(*schema_url) as holder:
        with holder.transaction():
            is_halted(holder, lock=True)
            with pytest.raises(psycopg.errors.LockNotAvailable):
                submissions.resolve(db, 1, None, "kevin", lock_timeout_ms=100)


def test_cli_says_it_is_waiting_for_a_submit_in_flight(db, fake, schema_url, monkeypatch, capsys):  # noqa: F811
    prepared(db)
    fake.answer = (503, {"detail": "down"})
    submit()
    from vending import db as db_module
    from vending.config import is_halted
    monkeypatch.setattr(db_module, "connect", lambda: open_in(*schema_url))
    monkeypatch.setattr(submissions, "WAIT_NOTICE_MS", 100)
    locked, release = threading.Event(), threading.Event()

    def hold_the_write_lock():
        with open_in(*schema_url) as holder:
            with holder.transaction():
                is_halted(holder, lock=True)
                locked.set()
                release.wait(30)

    holder = threading.Thread(target=hold_the_write_lock)
    holder.start()
    assert locked.wait(30)
    threading.Timer(1, release.set).start()
    assert submissions.main(["resolve", "1", "--not-filed", "--by", "kevin"]) == 0
    holder.join(timeout=30)
    out = capsys.readouterr().out
    assert "waiting for a submit in flight to finish" in out and "marked never filed by kevin" in out


def test_submit_returns_the_same_warnings_and_notes_as_preview(db, fake):  # noqa: F811
    prepared(db)
    previewed = preview()
    reply = submit()
    assert reply["success"]
    assert (reply["warnings"], reply["notes"]) == (previewed["warnings"], previewed["notes"])
    assert reply["warnings"] or reply["notes"], "the case should have something to show"


def test_a_balance_that_drops_under_the_floor_after_the_preview_refuses_the_submit(db, fake):  # noqa: F811
    """Submit plans again on live data, so the $53.00 spendable floor is checked against the balance at
    submit time, not the one the preview saw (Kevin, 19 Nov)."""
    prepared(db)
    previewed = preview()
    assert previewed["success"] and previewed["cost"]["spendable_left_at_max"] == "574.63"
    # 530.00 of model spend lands between preview and submit: 587.63 - 530.00 - 13.00 leaves 44.63.
    ledger.record_tokens(db, "530.00", "m", 1, 1, "live")
    reply = submit(expected_plan_hash=previewed["plan_hash"])
    assert codes(reply) == ["below_spendable_floor"] and fake.posts == []
    assert "$53.00 spendable floor" in reply["refusals"][0]["message"]


def test_a_fulfillment_committed_just_after_the_clock_is_read_never_files_the_plan_twice(db, fake, monkeypatch):  # noqa: F811
    """Sol round 5, finding 1. The receipt sync fulfills pr-1 right after the second submit reads the clock,
    and the receipt's `resolved_at` is after that reading. When the clock was read before the pending
    requests, the pending query saw nothing and the stock replay (up to the clock) missed the delivery, so
    the same plan went out again. The clock is now read after the pending requests: either pr-1 is still
    pending to it, or the clock is after the fulfillment and the delivery is counted."""
    from datetime import timedelta

    prepared(db)
    assert submit()["success"] and len(fake.posts) == 1
    clock, armed = [NOW], []

    def db_now(conn):
        reading = clock[0]
        if armed and armed.pop():
            store(db, "pr-1", fulfilled("pr-1", receipt(units_loaded=7, units_to_back_stock=4,
                                                        resolved_at="2026-11-09T13:00:00Z")),
                  NOW + timedelta(hours=1))
            clock[0] = NOW + timedelta(hours=2)     # the sync committed after this reading, so time moved on
        return reading

    monkeypatch.setattr(ledger, "db_now", db_now)
    armed.append(True)
    again = submit()
    assert not armed, "the fulfillment never happened"
    assert not again["success"], again["message"]
    assert len(fake.posts) == 1
    assert db.execute("select status from purchase_requests where request_id = 'pr-1'").fetchone() == ("fulfilled",)


def test_a_dripped_catalog_frees_the_write_lock_at_the_deadline(db, fake, schema_url, monkeypatch):  # noqa: F811
    """Sol round 5, finding 4. With no stored catalog, submit reads it from the bridge while it holds the
    halt lock. A catalog whose body drips used to hold the lock for as long as it took (each byte inside the
    per-read timeout). The read now has a total deadline (2 s here), so the submit gives up and the lock is
    free; nothing is sent."""
    import time

    from vending import bridge as bridge_module
    from vending.config import is_halted
    from vending.tools import submit as submit_tool
    from vending.write_gate import run_write
    monkeypatch.setattr(bridge_module, "CATALOG_DEADLINE_SECONDS", 2, raising=False)
    prepared(db)
    fake.catalog_drip = 0.25        # the catalog alone would take over a minute
    plan = desired(restock_15(fill_to=15), sourcing=[coke_sourcing()])
    replies = []

    def one():
        with open_in(*schema_url) as conn:
            action = submit_tool.submit_desired_state(plan, fake.client, lambda: open_in(*schema_url))
            replies.append(run_write(conn, "submit_desired_state", {}, action))

    started = time.monotonic()
    thread = threading.Thread(target=one)
    thread.start()
    time.sleep(max(0.0, started + 4 - time.monotonic()))        # the deadline plus a margin
    with db.transaction():
        db.execute("set local lock_timeout = '200ms'")
        is_halted(db, lock=True)        # raises LockNotAvailable while the submit still holds it
    thread.join(timeout=30)
    [reply] = replies
    assert not reply.success and "couldn't read the catalog" in reply.message and "within 2 s" in reply.message
    assert fake.gets == 1 and fake.posts == []


def test_a_201_naming_a_request_we_already_hold_keeps_no_created_id_so_a_person_can_settle_it(db, fake):  # noqa: F811
    """A validated-looking 201 under r1, a request we already recorded, isn't this attempt's: the row is
    `unknown` with the answer kept and no `created_id`, so `--not-filed` isn't refused for it."""
    prepared(db)
    fake.answer = (201, {"id": "r1", "status": "pending", "created_at": "2026-11-09T12:00:00Z"})
    reply = submit()
    assert reply["status"] == "unknown" and "already holds" in reply["message"]
    assert db.execute("select status, created_id, response->>'id' from purchase_submissions").fetchall() == [
        ("unknown", None, "r1")]
    assert submissions.resolve(db, 1, None, "kevin").startswith("Submission 1 marked never filed")
