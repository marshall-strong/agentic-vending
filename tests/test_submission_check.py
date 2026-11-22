"""`vending.submissions.check`: settling an open submission from the bridge's own answer (#217).
The bridge is faked; rows land in a throwaway schema."""

from datetime import datetime, timedelta, timezone

import pytest

psycopg = pytest.importorskip("psycopg")

from psycopg.types.json import Jsonb  # noqa: E402

from vending import submissions  # noqa: E402
from vending.bridge import BridgeError, BridgeNotFound  # noqa: E402

NOW = datetime(2026, 12, 7, 15, 0, tzinfo=timezone.utc)
BODY = {"lines": [], "off_cycle": False}


class Lookup:
    """A fake bridge that answers only `purchase_request`, from `known`; any other ID is a 404."""

    def __init__(self, known):
        self.known, self.asked = known, []

    def __call__(self):
        return self

    def purchase_request(self, request_id):
        self.asked.append(request_id)
        found = self.known.get(request_id, BridgeNotFound(f"404 Not Found from /{request_id}"))
        if isinstance(found, Exception):
            raise found
        return found


def row(conn, *, status="unknown", response=None, age=timedelta(hours=2)):
    return conn.execute(
        "insert into purchase_submissions (at, plan_hash, body, status, response)"
        " values (%s, 'h', %s, %s, %s) returning id",
        (NOW - age, Jsonb(BODY), status, None if response is None else Jsonb(response))).fetchone()[0]


def ours(conn, request_id="pr-old"):
    """An earlier request of ours the bridge filed: the control that shows the lookup route works."""
    submissions.resolve(conn, row(conn, status="sending", age=timedelta(days=3)), request_id, "kevin")


def state(conn, id):
    return conn.execute("select status, request_id, resolved_by from purchase_submissions where id = %s",
                        (id,)).fetchone()


def test_a_request_the_bridge_has_is_settled_accepted_once(conn):
    id = row(conn, response={"id": "pr-1"})
    bridge = Lookup({"pr-1": {"id": "pr-1", "status": "pending"}})
    [checked] = submissions.check(conn, bridge)
    assert (checked.status, state(conn, id)) == ("accepted", ("accepted", "pr-1", "auto-check"))
    assert conn.execute("select count(*) from purchase_requests where request_id = 'pr-1'").fetchone() == (1,)
    assert submissions.unresolved(conn) is None
    # Nothing is left to check, so a second check can't record the request again.
    assert submissions.check(conn, bridge) == [] and bridge.asked == ["pr-1"]


def test_a_404_never_settles_a_row_as_not_filed(conn):
    """A `404` for the ID a row holds isn't proof the request was never filed: another request reading fine
    doesn't show this one would be visible, and the bridge has no answer that says "never filed". So the row
    stays open for a person, however old it is."""
    ours(conn)
    id = row(conn, response={"id": "pr-1"}, age=timedelta(days=2))
    [checked] = submissions.check(conn, Lookup({"pr-old": {"id": "pr-old"}}))
    assert checked.status == "unknown" and "404" in checked.why
    assert state(conn, id) == ("unknown", None, None)
    assert submissions.unresolved(conn).code == "submission_unresolved"


@pytest.mark.parametrize("setup, known", [
    pytest.param(lambda c: row(c, response={"id": "pr-1"}, age=timedelta(minutes=5)), {},
                 id="a 404 for a young row"),
    pytest.param(lambda c: row(c, response={"id": "pr-1"}), {}, id="a 404 with no other request to compare"),
    pytest.param(lambda c: row(c, response={"id": "pr-1"}), {"pr-1": BridgeError("503")}, id="bridge down"),
    pytest.param(lambda c: row(c, response={"id": "pr-1"}), {"pr-1": {"id": "pr-2"}}, id="answer names another"),
    pytest.param(lambda c: row(c), {}, id="no request ID"),
    pytest.param(lambda c: row(c, status="sending"), {}, id="still sending, no ID"),
])
def test_anything_unsure_stays_open(conn, setup, known):
    id = setup(conn)
    before = state(conn, id)
    [checked] = submissions.check(conn, Lookup(known))
    assert checked.status == before[0] and checked.status in submissions.OPEN and state(conn, id) == before
    assert submissions.unresolved(conn).code == "submission_unresolved"


def test_no_bridge_is_built_when_no_row_holds_an_id(conn):
    row(conn)

    def bridge():
        raise AssertionError("built")
    assert [c.status for c in submissions.check(conn, bridge)] == ["unknown"]


def test_a_bridge_that_cant_be_built_leaves_the_row_open(conn):
    id = row(conn, response={"id": "pr-1"})

    def bridge():
        raise RuntimeError("DEX_BASE_URL isn't set")
    [checked] = submissions.check(conn, bridge)
    assert checked.status == "unknown" and state(conn, id)[0] == "unknown"


def test_cli_check_says_what_it_settled_and_what_still_needs_a_person(conn, schema_url, monkeypatch, capsys):
    from tests.conftest import open_in
    from vending import bridge as bridge_module
    from vending import db as db_module

    accepted, blocked = row(conn, response={"id": "pr-1"}), row(conn)
    monkeypatch.setattr(db_module, "connect", lambda: open_in(*schema_url))
    monkeypatch.setattr(bridge_module, "Bridge", Lookup({"pr-1": {"id": "pr-1"}}))
    assert submissions.main(["check"]) == 1
    out = capsys.readouterr().out
    assert f"{accepted}  settled as accepted" in out and f"{blocked}  still unknown" in out
    assert f"resolve {blocked}" in out
    submissions.resolve(conn, blocked, None, "kevin")
    assert submissions.main(["check"]) == 0
    assert "Nothing to settle" in capsys.readouterr().out


def test_a_request_id_already_ours_never_settles_another_row(conn):
    """#217: the bridge naming a request we already hold (another submission's, or a request recorded) doesn't
    make it this attempt's. Settling the row onto it would leave no new pending request, and the next run would
    send the same order again, so the row stays open for a person, even when the bridge confirms the ID."""
    ours(conn, "pr-old")
    id = row(conn, response={"id": "pr-old"})
    bridge = Lookup({"pr-old": {"id": "pr-old", "status": "pending"}})
    [checked] = submissions.check(conn, bridge)
    assert checked.status == "unknown" and "IdTaken" in checked.why
    assert state(conn, id) == ("unknown", None, None)
    with pytest.raises(submissions.IdTaken, match="submission 1 already holds"):
        submissions.resolve(conn, id, "pr-old", "kevin")
    assert submissions.unresolved(conn).code == "submission_unresolved"


def test_save_answer_keeps_the_id_on_a_row_still_sending_and_never_settles_it(conn):
    id = row(conn, status="sending")
    submissions.save_answer(conn, id, {"id": "pr-7", "status": "pending"})
    assert state(conn, id) == ("sending", None, None)
    assert submissions.known_request_id(None, conn.execute(
        "select response from purchase_submissions where id = %s", (id,)).fetchone()[0]) == "pr-7"
    # A row already settled is left as it is.
    done = row(conn, status="refused")
    submissions.save_answer(conn, done, {"id": "pr-8"})
    assert conn.execute("select response from purchase_submissions where id = %s", (done,)).fetchone() == (None,)
