"""The sales analysis's slot list (analysis/slots_config.py) can never name a slot that isn't ours.

The export writes per-slot prices and sales for every slot on the list, so a code outside our
assignment would put another team's figures in our files (docs/integrity.md).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "analysis"))

import slots_config  # noqa: E402
from vending.slots import SLOTS  # noqa: E402

OTHER = sorted({f"{r}{c}" for r in "12345" for c in "12345678"} - set(SLOTS))
ALL_40 = ",".join(f"{r}{c}" for r in "12345" for c in "12345678")


def test_every_code_outside_ours_is_refused():
    for code in OTHER:
        with pytest.raises(SystemExit, match="not one of our slots"):
            slots_config.parse_slots(code)


def test_a_wrong_guess_and_the_whole_machine_are_refused():
    with pytest.raises(SystemExit, match="11, 12, 13, 14"):
        slots_config.parse_slots("11,12,13,14,15")
    with pytest.raises(SystemExit, match="not one of our slots"):
        slots_config.parse_slots(ALL_40)


def test_check_refuses_a_code_outside_ours():
    with pytest.raises(SystemExit, match="not one of our slots"):
        slots_config.check([*SLOTS, "11"], "a list")


def test_part_of_our_slots_is_accepted_in_shelf_order():
    assert slots_config.parse_slots("25 15,16") == ("15", "16", "25")
    assert slots_config.check(SLOTS, "default") == tuple(sorted(SLOTS))


def test_resolve_refuses_a_command_line_code_outside_ours():
    with pytest.raises(SystemExit, match="not one of our slots"):
        slots_config.resolve_slots("15,11")


def test_resolve_checks_the_command_line_against_the_stored_server_assignment(monkeypatch):
    monkeypatch.setattr(slots_config, "from_server", lambda conn: ("15", "16"))
    assert slots_config.resolve_slots("16")[1:] == (("16",), slots_config.SOURCE_CLI)
    with pytest.raises(SystemExit, match="not one of our slots"):
        slots_config.resolve_slots("17")             # in SLOTS, but not in the server's answer
    assert slots_config.resolve_slots(None)[1:] == (("15", "16"), slots_config.SOURCE_SERVER)


def test_the_default_source_claims_no_server_check():
    _, slots, source = slots_config.resolve_slots(None)
    assert slots == tuple(sorted(SLOTS))
    assert source == slots_config.SOURCE_DEFAULT
    assert "server" not in source
    assert slots_config.is_default(source)
    assert slots_config.is_default(source + " (matches the server's answer of 12 Nov 2026, see #69)")
    assert not slots_config.is_default(slots_config.SOURCE_CLI)
