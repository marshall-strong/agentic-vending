"""Our slot assignment: read from the inventory server, stored once, and used by every tool that reads
the database. The parsing tests need no database."""

from datetime import date, datetime, timedelta, timezone

import pytest

from vending import slots
from vending.slots import BadAssignment, SlotsNotRecorded, parse

# The bridge's answer on 12 Nov 2026.
ANSWER = {"team": "group-b", "device_id": "vmc1", "slots": list(slots.SLOTS)}
NOW = datetime(2026, 11, 12, 15, tzinfo=timezone.utc)


# --- Parsing (no database) ------------------------------------------------------------------------

def test_the_live_answer_parses_in_the_order_it_came():
    found = parse(ANSWER)
    assert (found.team, found.device_id, found.slots) == ("group-b", "vmc1", slots.SLOTS)


@pytest.mark.parametrize("answer, why", [
    (["15", "16"], "isn't a JSON object"),
    ({"team": "group-b", "slots": ["15"]}, "no device_id"),
    ({**ANSWER, "device_id": "vmc2"}, "'vmc2'"),
    ({**ANSWER, "slots": []}, "lists no slots"),
    ({**ANSWER, "slots": "15,16"}, "lists no slots"),
    ({**ANSWER, "slots": ["15", ""]}, "non-empty name"),
    ({**ANSWER, "slots": ["15", " 16"]}, "non-empty name"),
    ({**ANSWER, "slots": ["15", 16]}, "non-empty name"),
    ({**ANSWER, "slots": ["15", "15"]}, "listed twice"),
])
def test_an_answer_it_cant_trust_is_refused(answer, why):
    with pytest.raises(BadAssignment, match=why):
        parse(answer)


# --- Storing it -----------------------------------------------------------------------------------

@pytest.fixture
def empty(conn):
    """A database with no assignment stored (conftest stores one by default)."""
    conn.execute("delete from slot_assignment")
    return conn


def test_nothing_stored_refuses_and_says_what_to_run(empty):
    with pytest.raises(SlotsNotRecorded, match="python -m vending.slots record"):
        slots.our_slots(empty)


def test_the_first_read_is_recorded(empty):
    assert slots.record(empty, ANSWER, NOW) == "recorded"
    assert slots.our_slots(empty) == slots.SLOTS
    row = empty.execute("select team, first_read_at, last_checked_at, response from slot_assignment").fetchone()
    assert row == ("group-b", NOW, NOW, ANSWER)
    assert empty.execute("select tool, outcome from action_log").fetchall() == [("slots.record", "done")]


def test_the_same_answer_again_only_moves_last_checked(empty):
    slots.record(empty, ANSWER, NOW)
    later = NOW + timedelta(days=3)
    reordered = {**ANSWER, "slots": list(reversed(slots.SLOTS))}   # same set: not a change
    assert slots.record(empty, reordered, later) == "unchanged"
    assert empty.execute("select first_read_at, last_checked_at from slot_assignment").fetchone() == (NOW, later)
    assert slots.our_slots(empty) == slots.SLOTS                    # the first order stays


@pytest.mark.parametrize("changed", [
    {**ANSWER, "slots": ["11", "12", "13", "14"]},
    {**ANSWER, "slots": list(slots.SLOTS[:-1])},
    {**ANSWER, "team": "group-a"},
])
def test_a_different_answer_is_refused_and_changes_nothing(empty, changed):
    slots.record(empty, ANSWER, NOW)
    with pytest.raises(BadAssignment, match="different assignment"):
        slots.record(empty, changed, NOW + timedelta(days=1))
    assert slots.our_slots(empty) == slots.SLOTS
    assert empty.execute("select last_checked_at from slot_assignment").fetchone() == (NOW,)


def test_a_bad_answer_stores_nothing(empty):
    with pytest.raises(BadAssignment):
        slots.record(empty, {**ANSWER, "device_id": "vmc2"}, NOW)
    assert empty.execute("select count(*) from slot_assignment").fetchone() == (0,)


def test_only_one_assignment_can_be_stored(conn):
    import psycopg
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute("insert into slot_assignment (device_id, slots, response) values ('vmc1', '{15}', '{}')")


# --- The tools use it -----------------------------------------------------------------------------

def test_every_tool_that_reads_slots_refuses_without_an_assignment(empty):
    from vending.planning import NotReady, planner_config
    from vending.tools.account_balance import get_account_balance
    from vending.tools.machine_state import get_machine_state
    from vending.tools.pnl import get_pnl
    from vending.tools.sales_history import get_sales_history

    from tests.test_ledger import open_ledger

    open_ledger(empty)
    replies = [get_machine_state(empty, now=NOW), get_sales_history(empty, NOW - timedelta(days=7), NOW, None),
               get_account_balance(empty), get_pnl(empty, date(2026, 11, 1), date(2026, 11, 7))]
    for reply in replies:
        assert not reply.success and "python -m vending.slots record" in reply.message
    from vending import config
    config.set_value(empty, "slot_capacity", 10, "test")
    with pytest.raises(NotReady, match="python -m vending.slots record"):
        planner_config(empty)


def test_the_tools_use_the_stored_slots_not_the_list_in_the_code(conn):
    from tests.conftest import store_poll
    from vending.planning import planner_config
    from vending.tools.machine_state import get_machine_state
    from vending.tools.sales_history import get_sales_history
    from vending import config

    conn.execute("update slot_assignment set slots = '{11,12}'")
    store_poll(conn, "a", "2026-11-05T12:00:00+00:00", {"11": (200, 10), "12": (250, 5), "15": (300, 50)})
    store_poll(conn, "b", "2026-11-12T12:00:00+00:00", {"11": (200, 17), "12": (250, 5), "15": (300, 60)})

    state = get_machine_state(conn, now=NOW)
    assert [s.slot for s in state.data] == ["11", "12"]
    assert state.data[0].units_sold_in_window == 7

    sales = get_sales_history(conn, NOW - timedelta(days=8), NOW, None)
    assert [r.slot for r in sales.data] == ["11", "12"]
    refused = get_sales_history(conn, NOW - timedelta(days=8), NOW, ["15"])
    assert not refused.success and "Not our slots: 15. Ours are 11, 12" in refused.message

    config.set_value(conn, "slot_capacity", 10, "test")
    assert planner_config(conn).slots == ("11", "12")


def test_the_ledger_counts_only_the_stored_slots(conn):
    from tests.conftest import store_poll
    from vending import ledger

    conn.execute("update slot_assignment set slots = '{11}'")
    store_poll(conn, "a", "2026-11-05T12:00:00+00:00", {"11": (200, 10), "15": (300, 50)})
    store_poll(conn, "b", "2026-11-06T12:00:00+00:00", {"11": (200, 12), "15": (300, 59)})
    start, end = datetime(2026, 11, 5, tzinfo=timezone.utc), datetime(2026, 11, 7, tzinfo=timezone.utc)
    assert ledger.units_sold(conn, start, end) == 2           # slot 15's 9 aren't ours
    assert ledger.takings(conn, start, end).cents == 400


# --- The command ----------------------------------------------------------------------------------

class FakeBridge:
    def __init__(self, answer):
        self.answer = answer

    def slots(self):
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.fixture
def command(schema_url, monkeypatch):
    """`python -m vending.slots` against the test schema, with the bridge faked."""
    from tests.conftest import open_in
    from vending import bridge, db

    monkeypatch.setattr(db, "connect", lambda: open_in(*schema_url))
    answers = {"answer": ANSWER}
    monkeypatch.setattr(bridge, "Bridge", lambda: FakeBridge(answers["answer"]))
    with open_in(*schema_url) as conn:
        conn.execute("delete from slot_assignment")
    return answers


def test_record_then_show(command, capsys):
    assert slots.main(["show"]) == 1
    assert "python -m vending.slots record" in capsys.readouterr().out
    assert slots.main(["record"]) == 0
    assert capsys.readouterr().out.startswith("recorded: team group-b, device vmc1, 20 slots: 15, 16")
    assert slots.main(["record"]) == 0
    assert capsys.readouterr().out.startswith("unchanged:")
    assert slots.main([]) == 0
    assert "20 slots" in capsys.readouterr().out


def test_record_refuses_a_changed_answer_with_exit_1(command, capsys):
    slots.main(["record"])
    command["answer"] = {**ANSWER, "slots": ["11"]}
    assert slots.main(["record"]) == 1
    assert "different assignment" in capsys.readouterr().err


def test_record_reports_an_unreachable_bridge_with_exit_3(command, capsys):
    from vending.bridge import BridgeUnreachable

    command["answer"] = BridgeUnreachable("Is Tailscale connected?")
    assert slots.main(["record"]) == 3
    assert "Tailscale" in capsys.readouterr().err


def test_record_never_migrates(schema_url, monkeypatch, capsys):
    from tests.conftest import open_in
    from vending import db

    with open_in(*schema_url) as conn:
        conn.execute("delete from schema_migrations where version = '012_slot_assignment.sql'")
    monkeypatch.setattr(db, "connect", lambda: open_in(*schema_url))
    monkeypatch.setattr(db, "migrate", lambda conn: pytest.fail("record must never migrate"))
    assert slots.main(["record"]) == 2
    assert "python -m vending.db migrate" in capsys.readouterr().err
