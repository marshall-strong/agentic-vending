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
    [checked] = submissions.check(conn, bridge, NOW)
    assert (checked.status, state(conn, id)) == ("accepted", ("accepted", "pr-1", "auto-check"))
    assert conn.execute("select count(*) from purchase_requests where request_id = 'pr-1'").fetchone() == (1,)
    assert submissions.unresolved(conn) is None
    # Nothing is left to check, so a second check can't record the request again.
    assert submissions.check(conn, bridge, NOW) == [] and bridge.asked == ["pr-1"]


def test_a_request_the_bridge_doesnt_have_is_settled_not_filed(conn):
    ours(conn)
    id = row(conn, response={"id": "pr-1"})
    [checked] = submissions.check(conn, Lookup({"pr-old": {"id": "pr-old"}}), NOW)
    assert checked.status == "not_filed" and "pr-old" in checked.why
    assert state(conn, id) == ("not_filed", None, "auto-check")
    assert submissions.unresolved(conn) is None


def _too_young(conn):
    ours(conn)
    return row(conn, response={"id": "pr-1"}, age=timedelta(minutes=5))


def _with_control(conn):
    ours(conn)
    return row(conn, response={"id": "pr-1"})


@pytest.mark.parametrize("setup, known", [
    pytest.param(_too_young, {"pr-old": {"id": "pr-old"}}, id="a 404 for a row under the safe wait"),
    pytest.param(lambda c: row(c, response={"id": "pr-1"}), {}, id="a 404 with no other request to compare"),
    pytest.param(_with_control, {"pr-old": BridgeNotFound("404")}, id="a 404 for the control request too"),
    pytest.param(_with_control, {"pr-old": BridgeError("503")}, id="the control read fails"),
    pytest.param(lambda c: row(c, response={"id": "pr-1"}), {"pr-1": BridgeError("503")}, id="bridge down"),
    pytest.param(lambda c: row(c, response={"id": "pr-1"}), {"pr-1": {"id": "pr-2"}}, id="answer names another"),
    pytest.param(lambda c: row(c), {}, id="no request ID"),
    pytest.param(lambda c: row(c, status="sending"), {}, id="still sending, no ID"),
])
def test_anything_unsure_stays_open(conn, setup, known):
    id = setup(conn)
    before = state(conn, id)
    [checked] = submissions.check(conn, Lookup(known), NOW)
    assert checked.status == before[0] and checked.status in submissions.OPEN and state(conn, id) == before
    assert submissions.unresolved(conn).code == "submission_unresolved"


def test_no_bridge_is_built_when_no_row_holds_an_id(conn):
    row(conn)

    def bridge():
        raise AssertionError("built")
    assert [c.status for c in submissions.check(conn, bridge, NOW)] == ["unknown"]


def test_a_bridge_that_cant_be_built_leaves_the_row_open(conn):
    id = row(conn, response={"id": "pr-1"})

    def bridge():
        raise RuntimeError("DEX_BASE_URL isn't set")
    [checked] = submissions.check(conn, bridge, NOW)
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
