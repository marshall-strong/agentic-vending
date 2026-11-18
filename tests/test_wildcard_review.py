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


# --- 1. Silence needs the counters to prove it end to end ------------------------------------------------

def _silent_report(conn, now=None, **machine_args):
    from tests.test_silent_slots import NOW, machine, report

    machine(conn, **machine_args)
    return report(conn, now=now or NOW)


def _why(r, slot):
    from tests.test_silent_slots import why

    return why(r, slot)


def test_1_the_control_slot_16_is_silent(conn):
    # The baseline the four tests below break one way each.
    assert [s.slot for s in _silent_report(conn).silent] == ["16"]


def test_1_no_reading_of_the_slot_at_or_before_fridays_midnight_is_unknown(conn):
    # 16 is missing from every poll from Thursday 6 PM to Friday 4 AM Eastern: a sale then can't be ruled out.
    def edit(t, units):
        if datetime(2026, 10, 22, 22, tzinfo=UTC) <= t <= datetime(2026, 10, 23, 8, tzinfo=UTC):
            del units["16"]
    r = _silent_report(conn, edit=edit)
    assert "16" not in [s.slot for s in r.silent]
    assert _why(r, "16").startswith("unknown: no reading of its counter at or before")


def test_1_a_confirmed_counter_restart_in_the_window_is_unknown(conn):
    # Saturday 24 Oct the board is swapped: 16 reads 3 from then on, so 3 may have sold before or after.
    def edit(t, units):
        if t >= datetime(2026, 10, 24, 10, tzinfo=UTC):
            units["16"] = 3
    r = _silent_report(conn, edit=edit)
    assert "16" not in [s.slot for s in r.silent]
    assert _why(r, "16").startswith("unknown: its counter restarted between the polls at 2026-10-24 06:00")


def test_1_a_24_hour_polling_outage_is_unknown_not_silent(conn):
    # No poll from Saturday morning to Sunday morning: no class day is lost, but the stretch wasn't watched.
    r = _silent_report(conn, gaps=[(datetime(2026, 10, 24, 2, tzinfo=UTC), datetime(2026, 10, 25, 3, tzinfo=UTC))])
    assert "16" not in [s.slot for s in r.silent]
    assert _why(r, "16").startswith("unknown: no poll read it from 2026-10-24 02:00 to 2026-10-25 06:00")


def test_1_a_stale_closing_reading_is_unknown(conn):
    from tests.test_silent_slots import THURSDAY_EVENING

    r = _silent_report(conn, now=THURSDAY_EVENING + timedelta(hours=8))
    assert "16" not in [s.slot for s in r.silent]
    assert _why(r, "16").startswith("unknown: its latest reading, at 2026-10-29 22:00 UTC, is over 6 hours old")
