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


# --- the analysis's run list (resolve_run) and the labs' scope check (labkit.scope_of) ---

NOW = "2026-11-12T13:00:00+00:00"
OURS = tuple(sorted(SLOTS))


def manifest(slots=OURS, source=None, device=slots_config.DEVICE_ID):
    return {"device": device, "slots": list(slots), "source": source or slots_config.SOURCE_DEFAULT,
            "resolved_at": "2026-11-12T12:00:00+00:00"}


def test_run_with_no_manifest_uses_the_default_list():
    assert slots_config.resolve_run(None, None, "slots.json", NOW) == (
        slots_config.DEVICE_ID, OURS, slots_config.SOURCE_DEFAULT, None)


def test_run_follows_the_manifest_and_drops_the_old_server_wording():
    old = slots_config.SOURCE_DEFAULT + " (matches the server's answer of 12 Nov 2026, see #69)"
    _, slots, source, at = slots_config.resolve_run(None, manifest(source=old), "slots.json", NOW)
    assert (slots, source, at) == (OURS, slots_config.SOURCE_DEFAULT, "2026-11-12T12:00:00+00:00")


def test_run_refuses_a_manifest_with_a_slot_that_is_not_ours():
    with pytest.raises(SystemExit, match="not one of our slots"):
        slots_config.resolve_run(None, manifest([*OURS, "11"]), "slots.json", NOW)


def test_run_refuses_a_manifest_that_claims_the_server_before_the_code_can_read_it(monkeypatch):
    monkeypatch.setattr(slots_config, "server_assignment_supported", lambda: False)
    with pytest.raises(SystemExit, match="can't read one yet"):
        slots_config.resolve_run(None, manifest(OURS, slots_config.SOURCE_SERVER), "slots.json", NOW)


def test_a_server_manifest_never_reaches_beyond_our_slots(monkeypatch):
    monkeypatch.setattr(slots_config, "server_assignment_supported", lambda: True)
    got = slots_config.resolve_run(None, manifest(("15", "16"), slots_config.SOURCE_SERVER), "slots.json", NOW)
    assert got[1:3] == (("15", "16"), slots_config.SOURCE_SERVER)
    for bad in (("11", "12", "15"), [f"{r}{c}" for r in "12345" for c in "12345678"]):
        with pytest.raises(SystemExit, match="not one of our slots"):
            slots_config.resolve_run(None, manifest(bad, slots_config.SOURCE_SERVER), "slots.json", NOW)
    with pytest.raises(SystemExit, match="not one of our slots"):
        slots_config.resolve_run("11", manifest(("15", "16"), slots_config.SOURCE_SERVER), "slots.json", NOW)


def test_run_refuses_an_unknown_source():
    with pytest.raises(SystemExit, match="unknown source"):
        slots_config.resolve_run(None, manifest(source="Default list"), "slots.json", NOW)


def test_run_refuses_a_manifest_for_another_device():
    with pytest.raises(SystemExit, match="recorded for device"):
        slots_config.resolve_run(None, manifest(device="vmc2"), "slots.json", NOW)


def test_run_command_line_must_match_the_export():
    with pytest.raises(SystemExit, match="Re-export first"):
        slots_config.resolve_run("15,16", manifest(), "slots.json", NOW)
    _, slots, source, at = slots_config.resolve_run(",".join(OURS), manifest(), "slots.json", NOW)
    assert (slots, source, at) == (OURS, slots_config.SOURCE_DEFAULT, "2026-11-12T12:00:00+00:00")
    _, slots, source, at = slots_config.resolve_run("16,15", None, "slots.json", NOW)
    assert (slots, source, at) == (("15", "16"), slots_config.SOURCE_CLI, NOW)


def lab_data(slots=OURS, source=slots_config.SOURCE_CLI, device=slots_config.DEVICE_ID):
    return {"scope": {"device": device, "our_slots": list(slots), "source": source,
                      "source_line": "Slots: ...", "machine": {"shelves": 5, "columns": 8}}}


def test_labs_refuse_a_scope_with_a_slot_that_is_not_ours():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "analysis" / "labs"))
    import labkit
    assert labkit.scope_of(lab_data(("15", "16")))["our_slots"] == ["15", "16"]
    with pytest.raises(SystemExit, match="not one of our slots"):
        labkit.scope_of(lab_data([*OURS, "14"]))
    with pytest.raises(SystemExit, match="recorded for device"):
        labkit.scope_of(lab_data(device="vmc2"))
    with pytest.raises(SystemExit, match="unknown source"):
        labkit.scope_of(lab_data(source="somewhere"))


def test_labs_refuse_a_server_scope_beyond_our_slots(monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "analysis" / "labs"))
    import labkit
    monkeypatch.setattr(slots_config, "server_assignment_supported", lambda: True)
    with pytest.raises(SystemExit, match="not one of our slots"):
        labkit.scope_of(lab_data(("11", "12", "15"), slots_config.SOURCE_SERVER))
