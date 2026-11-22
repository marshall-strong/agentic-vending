"""`vending.submissions.check`: settling an open submission whose POST got a validated `201` (#217).
The bridge is faked; rows land in a throwaway schema."""

from datetime import datetime, timedelta, timezone

import pytest

psycopg = pytest.importorskip("psycopg")

from psycopg.types.json import Jsonb  # noqa: E402

from vending import submissions  # noqa: E402
from vending.bridge import BridgeError, BridgeNotFound  # noqa: E402

NOW = datetime(2026, 12, 7, 15, 0, tzinfo=timezone.utc)
BODY = {"lines": [], "off_cycle": False}


def found(request_id, status="pending", **fields):
    """The bridge's `GET` answer for a request, in its documented shape."""
    return {"id": request_id, "status": status, "created_at": "2026-12-07T13:00:00Z", **fields}


class Lookup:
    """A fake bridge that answers only `purchase_request`, from `known`; any other ID is a 404."""

    def __init__(self, known):
        self.known, self.asked = known, []

    def __call__(self):
        return self

    def purchase_request(self, request_id):
        self.asked.append(request_id)
        answer = self.known.get(request_id, BridgeNotFound(f"404 Not Found from /{request_id}"))
        if isinstance(answer, Exception):
            raise answer
        return answer


def row(conn, *, status="sending", created_id=None, response=None, age=timedelta(hours=2)):
    """A submission left open. `created_id` is set only when its POST got a validated 201."""
    return conn.execute(
        "insert into purchase_submissions (at, plan_hash, body, status, response, created_id)"
        " values (%s, 'h', %s, %s, %s, %s) returning id",
        (NOW - age, Jsonb(BODY), status, None if response is None else Jsonb(response), created_id)).fetchone()[0]


def ours(conn, request_id="pr-old"):
    """An earlier request of ours the bridge filed."""
    submissions.resolve(conn, row(conn, age=timedelta(days=3)), request_id, "kevin")


def state(conn, id):
    return conn.execute("select status, request_id, resolved_by from purchase_submissions where id = %s",
                        (id,)).fetchone()


def test_a_validated_201_never_recorded_is_settled_accepted_once(conn):
    id = row(conn, created_id="pr-1", response=found("pr-1"))
    bridge = Lookup({"pr-1": found("pr-1")})
    [checked] = submissions.check(conn, bridge)
    assert (checked.status, state(conn, id)) == ("accepted", ("accepted", "pr-1", "auto-check"))
    assert conn.execute("select count(*) from purchase_requests where request_id = 'pr-1'").fetchone() == (1,)
    assert submissions.unresolved(conn) is None
    # Nothing is left to check, so a second check can't record the request again.
    assert submissions.check(conn, bridge) == [] and bridge.asked == ["pr-1"]


@pytest.mark.parametrize("response", [
    pytest.param({"id": "pr-old-untracked", "status": "pending", "created_at": "2026-12-01T12:00:00Z"},
                 id="an ambiguous answer naming a request"),
    pytest.param(None, id="no answer"),
])
def test_a_row_without_a_validated_201_is_never_looked_up(conn, response):
    """Sol round 3, finding 3: an answer that wasn't the documented acceptance may name an unrelated request,
    and the bridge confirming that request exists doesn't make it this attempt's. Only a person settles it."""
    id = row(conn, status="unknown", response=response)
    bridge = Lookup({"pr-old-untracked": found("pr-old-untracked")})
    [checked] = submissions.check(conn, bridge)
    assert checked.status == "unknown" and "no validated 201" in checked.why
    assert state(conn, id) == ("unknown", None, None) and bridge.asked == []
    assert conn.execute("select count(*) from purchase_requests").fetchone() == (0,)


@pytest.mark.parametrize("known", [
    pytest.param({"pr-1": {"id": "pr-1"}}, id="only the id"),
    pytest.param({"pr-1": found("pr-1", status="lost")}, id="an undocumented status"),
    pytest.param({"pr-1": {"id": "pr-1", "status": "pending"}}, id="no creation time"),
    pytest.param({"pr-1": found("pr-2")}, id="another request"),
    pytest.param({"pr-1": ["pr-1"]}, id="not an object"),
    pytest.param({"pr-1": BridgeError("503")}, id="bridge down"),
    pytest.param({}, id="404"),
])
def test_a_get_that_doesnt_answer_for_the_id_leaves_the_row_open(conn, known):
    """Sol round 3, finding 3: a `GET` with only the ID cleared the row. Only the documented record of that
    request settles it; a `404` doesn't prove it was never filed."""
    id = row(conn, created_id="pr-1")
    [checked] = submissions.check(conn, Lookup(known))
    assert checked.status == "sending" and state(conn, id) == ("sending", None, None)
    assert submissions.unresolved(conn).code == "submission_unresolved"


def test_no_bridge_is_built_when_no_row_has_a_validated_201(conn):
    row(conn, status="unknown", response={"id": "pr-1"})

    def bridge():
        raise AssertionError("built")
    assert [c.status for c in submissions.check(conn, bridge)] == ["unknown"]


def test_a_bridge_that_cant_be_built_leaves_the_row_open(conn):
    id = row(conn, created_id="pr-1")

    def bridge():
        raise RuntimeError("DEX_BASE_URL isn't set")
    [checked] = submissions.check(conn, bridge)
    assert checked.status == "sending" and state(conn, id)[0] == "sending"


def test_cli_check_says_what_it_settled_and_what_still_needs_a_person(conn, schema_url, monkeypatch, capsys):
    from tests.conftest import open_in
    from vending import bridge as bridge_module
    from vending import db as db_module

    accepted, blocked = row(conn, created_id="pr-1"), row(conn, status="unknown")
    monkeypatch.setattr(db_module, "connect", lambda: open_in(*schema_url))
    monkeypatch.setattr(bridge_module, "Bridge", Lookup({"pr-1": found("pr-1")}))
    assert submissions.main(["check"]) == 1
    out = capsys.readouterr().out
    assert f"{accepted}  settled as accepted" in out and f"{blocked}  still unknown" in out
    assert f"resolve {blocked}" in out
    assert submissions.main(["resolve", str(blocked), "--not-filed", "--by", "kevin"]) == 0
    assert submissions.main(["check"]) == 0
    assert "Nothing to settle" in capsys.readouterr().out


def test_a_request_id_already_ours_never_settles_another_row(conn):
    """#217: the bridge naming a request we already hold (another submission's, or a request recorded) doesn't
    make it this attempt's. Settling the row onto it would leave no new pending request, and the next run would
    send the same order again, so the row stays open for a person, even when the bridge confirms the ID."""
    ours(conn, "pr-old")
    id = row(conn, created_id="pr-old")
    [checked] = submissions.check(conn, Lookup({"pr-old": found("pr-old")}))
    assert checked.status == "sending" and "IdTaken" in checked.why
    assert state(conn, id) == ("sending", None, None)
    with pytest.raises(submissions.IdTaken, match="submission 1 already holds"):
        submissions.resolve(conn, id, "pr-old", "kevin")
    assert submissions.unresolved(conn).code == "submission_unresolved"
    # Not this attempt's 201, so `--not-filed` isn't refused for it (Sol round 5): a person checks and settles.
    assert submissions.resolve(conn, id, None, "kevin").startswith(f"Submission {id} marked never filed")


def test_a_request_id_another_row_was_accepted_under_cant_settle_this_one(conn):
    """A row whose POST got a validated 201 for pr-1 (`created_id`, never settled) holds pr-1: another row
    can't be resolved onto it, so the request is never bound to two attempts."""
    accepted, other = row(conn, created_id="pr-1"), row(conn, status="unknown")
    with pytest.raises(submissions.IdTaken, match=f"submission {accepted} already holds"):
        submissions.resolve(conn, other, "pr-1", "kevin")
    assert state(conn, other) == ("unknown", None, None)


def test_save_answer_keeps_the_answer_and_created_id_on_a_row_still_sending_and_never_settles_it(conn):
    id = row(conn)
    submissions.save_answer(conn, id, found("pr-7"), created_id="pr-7")
    assert state(conn, id) == ("sending", None, None)
    assert conn.execute("select created_id, response->>'id' from purchase_submissions where id = %s",
                        (id,)).fetchone() == ("pr-7", "pr-7")
    # An answer that wasn't a validated 201 is kept without an ID to look up.
    other = row(conn)
    submissions.save_answer(conn, other, {"id": "pr-8"})
    assert conn.execute("select created_id from purchase_submissions where id = %s", (other,)).fetchone() == (None,)
    # A row already settled is left as it is.
    done = row(conn, status="not_filed")
    submissions.save_answer(conn, done, {"id": "pr-9"}, created_id="pr-9")
    assert conn.execute("select response, created_id from purchase_submissions where id = %s",
                        (done,)).fetchone() == (None, None)


@pytest.mark.parametrize("status", [{}, [], ["pending"], 1, None])
def test_a_lookup_whose_status_isnt_a_string_leaves_that_row_open_and_checks_the_rest(conn, status):
    """Sol round 4, finding 5: `status: {}` can't be looked up in a set of strings. It used to raise
    `TypeError` and end the whole check; that row now stays open, and the next row is still settled."""
    odd = row(conn, created_id="pr-odd")
    good = row(conn, created_id="pr-good")
    bridge = Lookup({"pr-odd": found("pr-odd", status=status), "pr-good": found("pr-good")})
    first, second = submissions.check(conn, bridge)
    assert (first.row, first.status) == (odd, "sending") and "no documented status" in first.why
    assert (second.row, second.status) == (good, "accepted")
    assert state(conn, odd) == ("sending", None, None) and bridge.asked == ["pr-odd", "pr-good"]


def test_a_row_the_bridge_accepted_cant_be_marked_never_filed(conn):
    """Sol round 5, finding 3: a row holding `created_id` got the bridge's validated `201`, so it was filed.
    `--not-filed` would unblock the next submit and order the same plan twice; it is refused, naming the ID."""
    id = row(conn, created_id="pr-1")
    with pytest.raises(ValueError, match="accepted request pr-1.*--request-id pr-1"):
        submissions.resolve(conn, id, None, "kevin")
    assert state(conn, id) == ("sending", None, None)
    assert conn.execute("select count(*) from action_log where tool = 'submissions.resolve'").fetchone() == (0,)


def test_a_request_id_that_isnt_the_one_the_bridge_accepted_is_refused(conn):
    """The bridge accepted pr-1 for this row; a person naming pr-2 is told to check with the instructors."""
    id = row(conn, status="unknown", created_id="pr-1")
    with pytest.raises(ValueError, match="accepted request pr-1 for submission [0-9]+, not pr-2.*instructors"):
        submissions.resolve(conn, id, "pr-2", "kevin")
    assert state(conn, id) == ("unknown", None, None)
    assert submissions.resolve(conn, id, "pr-1", "kevin").startswith(f"Submission {id} recorded as filed under pr-1")


def test_cli_list_shows_the_request_id_the_bridge_accepted(conn, schema_url, monkeypatch, capsys):
    from tests.conftest import open_in
    from vending import db as db_module

    accepted, blocked = row(conn, created_id="pr-1"), row(conn, status="unknown")
    monkeypatch.setattr(db_module, "connect", lambda: open_in(*schema_url))
    assert submissions.main(["list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "accepted as pr-1" in lines[0] and lines[0].startswith(f"{accepted}  ")
    assert "accepted as" not in lines[1] and lines[1].startswith(f"{blocked}  ")
    assert submissions.main(["resolve", str(accepted), "--not-filed", "--by", "kevin"]) == 1
    assert "--request-id pr-1" in capsys.readouterr().err
    assert state(conn, accepted) == ("sending", None, None)
