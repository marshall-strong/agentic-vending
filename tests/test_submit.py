"""`submit_desired_state` through the MCP client, the write gate and Postgres, against the
local fake bridge from `tests/test_desired_state.py`."""

import pytest

psycopg = pytest.importorskip("psycopg")

from tests.conftest import open_in  # noqa: E402
from tests.test_desired_state import coke_sourcing, desired, fake, ready, restock_15, stocked  # noqa: E402,F401
from tests.test_server import call, db  # noqa: E402,F401
from vending import submissions  # noqa: E402
from vending.config import set_value  # noqa: E402


def submit(**arguments):
    body = desired(restock_15(fill_to=20), sourcing=[coke_sourcing()], **arguments).model_dump()
    result = call("submit_desired_state", {"desired": body})
    assert not result.isError, result.content[0].text
    return result.structuredContent


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
    # The request is pending now, so the same plan is refused until the worker is done.
    again = submit()
    assert codes(again) == ["request_pending"] and len(fake.posts) == 1


def test_a_refused_plan_sends_nothing_and_saves_nothing(db, fake):  # noqa: F811
    prepared(db)
    body = desired(restock_15(price="1.20", fill_to=20), sourcing=[coke_sourcing()]).model_dump()
    reply = call("submit_desired_state", {"desired": body}).structuredContent
    assert not reply["success"] and codes(reply) == ["below_floor"]
    assert fake.posts == [] and db.execute("select count(*) from purchase_submissions").fetchone() == (0,)
    assert db.execute("select outcome from action_log where tool = 'submit_desired_state'").fetchall() == [("refused",)]


def test_not_ready_is_a_failure_not_a_refusal(db, fake):  # noqa: F811
    ready(db, capacity=20)                                 # no ledger entry... but ready opens it; drop capacity instead
    db.execute("delete from config where key = 'slot_capacity'")
    reply = submit()
    assert not reply["success"] and "Can't submit yet" in reply["message"] and "slot_capacity" in reply["message"]
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


def test_a_crash_after_the_post_leaves_the_sending_row(db, fake, monkeypatch):  # noqa: F811
    prepared(db)

    def crash(*args, **kwargs):
        raise RuntimeError("simulated crash before the answer was stored")

    original = submissions.settle
    monkeypatch.setattr(submissions, "settle", crash)
    body = desired(restock_15(fill_to=20), sourcing=[coke_sourcing()]).model_dump()
    assert call("submit_desired_state", {"desired": body}).isError
    assert len(fake.posts) == 1
    assert db.execute("select status from purchase_submissions").fetchall() == [("sending",)]
    monkeypatch.setattr(submissions, "settle", original)
    assert codes(submit()) == ["submission_unresolved"] and len(fake.posts) == 1


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
