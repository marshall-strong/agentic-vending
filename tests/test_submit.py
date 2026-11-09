"""`submit_desired_state` through the MCP client, the write gate and Postgres, against the
local fake bridge from `tests/test_desired_state.py`."""

import threading

import pytest

psycopg = pytest.importorskip("psycopg")

from datetime import datetime, timezone  # noqa: E402

from tests.conftest import open_in, store_poll  # noqa: E402
from tests.test_desired_state import coke_sourcing, desired, fake, ready, restock_15, stocked  # noqa: E402,F401
from tests.test_server import call, db  # noqa: E402,F401
from tests.test_sync_requests import fulfilled, receipt  # noqa: E402
from vending import ledger, submissions  # noqa: E402
from vending.config import set_value  # noqa: E402
from vending.purchase_requests import store  # noqa: E402


def submit(expected_plan_hash=None, **arguments):
    body = desired(restock_15(fill_to=20), sourcing=[coke_sourcing()], **arguments).model_dump()
    args = {"desired": body}
    if expected_plan_hash:
        args["expected_plan_hash"] = expected_plan_hash
    result = call("submit_desired_state", args)
    assert not result.isError, result.content[0].text
    return result.structuredContent


def preview():
    body = desired(restock_15(fill_to=20), sourcing=[coke_sourcing()]).model_dump()
    return call("preview_desired_state", {"desired": body}).structuredContent


def codes(reply):
    return sorted(r["code"] for r in reply["refusals"])


def prepared(db):  # noqa: F811
    ready(db, capacity=20)
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
    body = desired(restock_15(price="1.20", fill_to=20), sourcing=[coke_sourcing()]).model_dump()
    reply = call("submit_desired_state", {"desired": body}).structuredContent
    assert not reply["success"] and codes(reply) == ["below_floor"]
    assert fake.posts == [] and db.execute("select count(*) from purchase_submissions").fetchone() == (0,)
    assert db.execute("select outcome from action_log where tool = 'submit_desired_state'").fetchall() == [("refused",)]


def test_not_ready_is_a_failure_not_a_refusal(db, fake):  # noqa: F811
    ready(db, capacity=20)
    fake.catalog_status = 503
    reply = submit()
    assert not reply["success"] and "Can't submit yet" in reply["message"] and "couldn't read the catalog" in reply["message"]
    assert fake.posts == []
    assert db.execute("select outcome from action_log where tool = 'submit_desired_state'").fetchall() == [("failed",)]


def test_a_4xx_from_the_bridge_is_refused_with_its_reason(db, fake):  # noqa: F811
    prepared(db)
    fake.answer = (422, {"detail": {"error_type": "invalid_lines", "problems": ["alternate_same_as_primary"]}})
    reply = submit()
    assert not reply["success"] and "422" in reply["message"] and "alternate_same_as_primary" in reply["message"]
    assert db.execute("select status from purchase_submissions").fetchall() == [("refused",)]
    assert db.execute("select count(*) from purchase_requests where status = 'pending'").fetchone() == (0,)
    # Certainly not filed, so a corrected plan may be sent.
    fake.answer = (201, {"id": "pr-2", "status": "pending"})
    assert submit()["success"] and len(fake.posts) == 2


def test_too_many_open_requests_is_a_refusal(db, fake):  # noqa: F811
    prepared(db)
    fake.answer = (429, {"detail": "too_many_open_requests"})
    assert codes(submit()) == ["too_many_open_requests"]
    assert db.execute("select status from purchase_submissions").fetchall() == [("refused",)]


@pytest.mark.parametrize("answer", [(503, {"detail": "down"}), (201, {"status": "pending"})])
def test_no_usable_answer_blocks_every_submit_until_a_person_settles_it(db, fake, answer):  # noqa: F811
    prepared(db)
    fake.answer = answer
    reply = submit()
    assert not reply["success"] and reply["status"] == "unknown" and "resolve 1" in reply["message"]
    assert db.execute("select status from purchase_submissions").fetchall() == [("unknown",)]
    assert db.execute("select count(*) from purchase_requests where status = 'pending'").fetchone() == (0,)
    fake.answer = (201, {"id": "pr-9", "status": "pending"})
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
    fake.answer = (201, {"id": "pr-2", "status": "pending"})
    assert submit()["success"] and len(fake.posts) == 2
    assert db.execute("select tool from action_log where tool = 'submissions.resolve'").fetchall() == [("submissions.resolve",)]


def test_a_settle_failure_after_the_bridge_accepted_still_names_the_request_id(db, fake, monkeypatch, capsys):  # noqa: F811
    prepared(db)

    def crash(*args, **kwargs):
        raise RuntimeError("simulated: the database dropped after the POST")

    original = submissions.settle
    monkeypatch.setattr(submissions, "settle", crash)
    body = desired(restock_15(fill_to=20), sourcing=[coke_sourcing()]).model_dump()
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

    body = desired(restock_15(fill_to=20), sourcing=[coke_sourcing()]).model_dump()
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
    # Three more sold since the preview: the rail holds 5, so the plan now buys 15, not 12.
    store_poll(db, "b", "2026-11-09T10:00:00+00:00", {"15": (250, 103)})
    reply = submit(expected_plan_hash=previewed["plan_hash"])
    assert codes(reply) == ["plan_changed"] and previewed["plan_hash"] in reply["refusals"][0]["message"]
    assert fake.posts == []
    again = preview()
    assert again["plan_hash"] != previewed["plan_hash"]
    assert submit(expected_plan_hash=again["plan_hash"])["success"] and len(fake.posts) == 1


def test_a_new_visit_fee_since_the_preview_is_a_changed_plan(db, fake):  # noqa: F811
    prepared(db)
    body = desired(restock_15(fill_to=20), off_cycle=True, sourcing=[coke_sourcing()]).model_dump()
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


@pytest.mark.parametrize(("status", "settled"), [(422, "refused"), (503, "unknown")])
def test_an_error_answer_cut_off_part_way_is_still_that_answer(db, fake, status, settled):  # noqa: F811
    prepared(db)
    fake.answer, fake.cut = (status, {}), True
    reply = submit()
    assert not reply["success"] and str(status) in reply["message"]
    assert db.execute("select status from purchase_submissions").fetchall() == [(settled,)]


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
    body = desired(restock_15(price="1.20", fill_to=20), sourcing=[coke_sourcing()]).model_dump()
    reply = call("submit_desired_state", {"desired": body}).structuredContent
    assert codes(reply) == ["below_floor", "submission_unresolved"]


def test_two_concurrent_submits_file_one_request(db, fake, schema_url):  # noqa: F811
    prepared(db)
    from vending.tools import submit as submit_tool
    from vending.write_gate import Refused, run_write
    plan = desired(restock_15(fill_to=20), sourcing=[coke_sourcing()])
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
        def post(self, path, body):
            return Bridge("http://127.0.0.1:9", "test-key").post(path, body)

    monkeypatch.setattr(server, "bridge", lambda: HalfDead(real.base_url, "test-key"))
    reply = submit()
    assert not reply["success"] and "Couldn't reach the bridge" in reply["message"]
    assert db.execute("select status from purchase_submissions").fetchall() == [("not_filed",)]


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
