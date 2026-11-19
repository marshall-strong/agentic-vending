"""The fixed wildcard slot: config `wildcard_slot`, set once by a person or the agent, never moved."""

import pytest

from tests.conftest import open_in
from vending import config
from vending.config import set_halted
from vending.tools.wildcard_slot import set_wildcard_slot
from vending.write_gate import Refused, run_write


def pick(conn, slot, reason="middle shelf, an ordinary spot"):
    """The agent's tool, through the write gate as the server runs it."""
    return run_write(conn, "set_wildcard_slot", {"slot": slot, "reason": reason}, set_wildcard_slot(slot, reason))


def refusal(conn, slot, reason="middle shelf"):
    with pytest.raises(Refused) as caught:
        pick(conn, slot, reason)
    return " ".join(r.message for r in caught.value.refusals)


# --- Write-once ---------------------------------------------------------------------------------------

def test_the_wildcard_slot_is_set_once_and_never_changes(conn):
    config.set_value(conn, "wildcard_slot", "37", "kevin")
    config.set_value(conn, "wildcard_slot", "37", "kevin")          # the same slot again changes nothing
    with pytest.raises(ValueError, match="already \"37\" and never changes"):
        config.set_value(conn, "wildcard_slot", "26", "kevin")
    assert config.get_value(conn, "wildcard_slot") == "37"
    assert conn.execute("select count(*) from action_log where tool = 'config.set'").fetchone() == (1,)


@pytest.mark.parametrize("value", [37, "99", "", None, ["37"]])
def test_the_wildcard_slot_must_name_one_of_our_slots_as_text(conn, value):
    with pytest.raises(ValueError, match="wildcard_slot"):
        config.set_value(conn, "wildcard_slot", value, "kevin")
    assert config.get_value(conn, "wildcard_slot") is None


def test_a_person_sets_it_with_the_config_cli(schema_url, monkeypatch, capsys):
    from vending import db as db_module
    monkeypatch.setattr(db_module, "connect", lambda: open_in(*schema_url))
    assert config.main(["set", "wildcard_slot", "37", "--by", "kevin"]) == 1          # a bare 37 is a number
    assert "in quotes" in capsys.readouterr().err
    assert config.main(["set", "wildcard_slot", '"37"', "--by", "kevin"]) == 0
    assert config.main(["set", "wildcard_slot", '"26"', "--by", "kevin"]) == 1
    assert "never changes" in capsys.readouterr().err


# --- The agent's tool ---------------------------------------------------------------------------------

def test_the_agent_picks_an_ordinary_middle_shelf_slot_once(conn):
    set_halted(conn, False, "test")
    assert pick(conn, "37").success
    assert config.get_value(conn, "wildcard_slot") == "37"
    assert pick(conn, "37").success                                     # asking again for the same slot is fine
    assert "never moves" in refusal(conn, "26")
    assert config.get_value(conn, "wildcard_slot") == "37"
    logged = conn.execute("select tool, outcome, arguments->>'reason' from action_log"
                          " where tool = 'set_wildcard_slot' order by id").fetchall()
    assert logged[0] == ("set_wildcard_slot", "done", "middle shelf, an ordinary spot")
    assert logged[-1][1] == "refused"


@pytest.mark.parametrize("slot, why", [
    ("17", "shelf 1"),          # the top shelf
    ("57", "shelf 5"),          # the bottom shelf
    ("35", "column 5"),         # beside the machine's centre line
    ("31", "isn't one of ours"),
])
def test_the_agent_tool_keeps_to_the_rule(conn, slot, why):
    set_halted(conn, False, "test")
    assert why in refusal(conn, slot)
    assert config.get_value(conn, "wildcard_slot") is None


def test_the_agent_gives_a_reason(conn):
    set_halted(conn, False, "test")
    assert "reason" in refusal(conn, "37", reason="  ")


def test_the_agent_tool_refuses_when_halted(conn):
    with pytest.raises(Refused) as caught:
        pick(conn, "37")
    assert caught.value.refusals[0].code == "halted"
    assert config.get_value(conn, "wildcard_slot") is None


def test_the_agent_tool_needs_our_slot_assignment(conn):
    set_halted(conn, False, "test")
    conn.execute("delete from slot_assignment")
    assert "python -m vending.slots record" in refusal(conn, "37")


def test_the_slot_must_still_be_ours_when_planning(conn):
    from vending import planning

    config.set_value(conn, "wildcard_slot", "37", "kevin")
    assert planning.planner_config(conn).wildcard_slot == "37"
    conn.execute("update slot_assignment set slots = array_replace(slots, '37', '11')")
    with pytest.raises(planning.NotReady, match="isn't one of ours"):
        planning.planner_config(conn)
    # A hand edit that isn't a slot name is refused too.
    conn.execute("update config set value = '37' where key = 'wildcard_slot'")
    with pytest.raises(planning.NotReady, match="in quotes"):
        planning.planner_config(conn)
