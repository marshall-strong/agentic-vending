"""Regressions from the second review of the fixed wildcard (#166 at c991f53), one section per finding.

Each test here failed on c991f53. When data can't settle prices, timing, contents or sales, the code
reports unknown or refuses; it never promotes, moves or buys on a guess.
"""

from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import open_in, store_poll
from tests.test_machine_state import fulfilled
from vending import config

UTC = timezone.utc


# --- 7. The setter checks the stored assignment before any success ---------------------------------------

def test_7_asking_again_for_a_slot_that_left_our_assignment_is_refused(conn):
    from vending.config import set_halted
    from vending.tools.wildcard_slot import set_wildcard_slot
    from vending.write_gate import Refused, run_write

    set_halted(conn, False, "test")
    config.set_value(conn, "wildcard_slot", "37", "kevin")
    conn.execute("update slot_assignment set slots = array_remove(slots, '37')")
    with pytest.raises(Refused) as caught:
        run_write(conn, "set_wildcard_slot", {"slot": "37", "reason": "again"}, set_wildcard_slot("37", "again"))
    assert "isn't one of ours" in " ".join(r.message for r in caught.value.refusals)


def test_7_asking_again_still_needs_a_reason(conn):
    from vending.config import set_halted
    from vending.tools.wildcard_slot import set_wildcard_slot
    from vending.write_gate import Refused, run_write

    set_halted(conn, False, "test")
    config.set_value(conn, "wildcard_slot", "37", "kevin")
    with pytest.raises(Refused) as caught:
        run_write(conn, "set_wildcard_slot", {"slot": "37", "reason": " "}, set_wildcard_slot("37", " "))
    assert "give a reason" in " ".join(r.message for r in caught.value.refusals)


def test_7_the_config_path_checks_the_stored_assignment(conn):
    conn.execute("update slot_assignment set slots = array_remove(slots, '37')")
    with pytest.raises(ValueError, match="isn't one of ours in the stored slot assignment"):
        config.set_value(conn, "wildcard_slot", "37", "kevin")
    assert config.get_value(conn, "wildcard_slot") is None


def test_7_the_config_path_refuses_the_same_value_once_it_left_the_assignment(conn):
    config.set_value(conn, "wildcard_slot", "37", "kevin")
    conn.execute("update slot_assignment set slots = array_remove(slots, '37')")
    with pytest.raises(ValueError, match="isn't one of ours"):
        config.set_value(conn, "wildcard_slot", "37", "kevin")


def test_7_the_cli_checks_the_stored_assignment(schema_url, monkeypatch, capsys):
    from vending import db as db_module

    monkeypatch.setattr(db_module, "connect", lambda: open_in(*schema_url))
    with open_in(*schema_url) as c:
        c.execute("update slot_assignment set slots = array_remove(slots, '37')")
    assert config.main(["set", "wildcard_slot", '"37"', "--by", "kevin"]) == 1
    assert "isn't one of ours" in capsys.readouterr().err


# --- 8. Config can't raise the agreed limits ------------------------------------------------------------

@pytest.mark.parametrize("key, value", [
    ("price_ceiling_cents", 700),             # agreed: $6.50
    ("wildcard_ceiling_cents", 850),          # agreed: $8.00
    ("wildcard_purchase_cap_cents", 8300),    # agreed: $82.00
])
def test_8_config_refuses_a_limit_above_the_agreed_one(conn, key, value):
    with pytest.raises(ValueError, match="agreed"):
        config.set_value(conn, key, value, "kevin")
    assert config.get_value(conn, key) is None


@pytest.mark.parametrize("key, value", [
    ("price_ceiling_cents", 600), ("wildcard_ceiling_cents", 750), ("wildcard_purchase_cap_cents", 6000),
    ("price_ceiling_cents", 650), ("wildcard_ceiling_cents", 800), ("wildcard_purchase_cap_cents", 8200),
])
def test_8_config_allows_the_agreed_limit_or_lower(conn, key, value):
    config.set_value(conn, key, value, "kevin")
    assert config.get_value(conn, key) == value


@pytest.mark.parametrize("kwargs", [
    {"price_ceiling_cents": 700}, {"wildcard_ceiling_cents": 850}, {"wildcard_purchase_cap_cents": 8300},
])
def test_8_the_planner_refuses_a_limit_above_the_agreed_one(kwargs):
    from vending.planner import Config

    with pytest.raises(ValueError, match="agreed"):
        Config(**kwargs)


def test_8_the_planner_allows_lower_limits():
    from vending.planner import Config

    c = Config(price_ceiling_cents=600, wildcard_ceiling_cents=700, wildcard_purchase_cap_cents=5000)
    assert (c.price_ceiling_cents, c.wildcard_ceiling_cents, c.wildcard_purchase_cap_cents) == (600, 700, 5000)


@pytest.mark.parametrize("key, value", [
    ("price_ceiling_cents", 700), ("wildcard_ceiling_cents", 850), ("wildcard_purchase_cap_cents", 8300),
])
def test_8_a_hand_edited_limit_above_the_agreed_one_stops_planning(conn, key, value):
    from psycopg.types.json import Jsonb

    from vending import planning

    conn.execute("insert into config (key, value, updated_by) values (%s, %s, 'hand')", (key, Jsonb(value)))
    with pytest.raises(planning.NotReady, match=key):
        planning.planner_config(conn)
