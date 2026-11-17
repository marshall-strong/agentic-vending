"""The sales analysis's slot list (analysis/slots_config.py) can never name a slot that isn't ours.

The export writes per-slot prices and sales for every slot on the list, so a code outside our
assignment would put another team's figures in our files (docs/integrity.md).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "analysis"))

import slots_config  # noqa: E402
from tests.conftest import TEST_SLOTS  # noqa: E402

OURS = tuple(sorted(TEST_SLOTS))      # the stored assignment these tests stand in (test data)
OTHER = sorted({f"{r}{c}" for r in "12345" for c in "12345678"} - set(OURS))
ALL_40 = ",".join(f"{r}{c}" for r in "12345" for c in "12345678")
# Another possible assignment, the other half of the machine: the server may change ours before the start.
REDRAWN = tuple(f"{r}{c}" for r in "12345" for c in "1234")


@pytest.fixture(autouse=True)
def _fall_cut_at_the_start(monkeypatch):
    """Most tests here were written for a fall that ends at the start, so they cut there. The tests of the default
    cut (before the day-one fill) unset it."""
    monkeypatch.setenv(slots_config.FALL_CUTOFF_SETTING, slots_config.START.isoformat())


@pytest.fixture
def stored(monkeypatch):
    """The stored server assignment the analysis reads (OURS unless a test sets another)."""
    def store(slots=OURS):
        monkeypatch.setattr(slots_config, "from_server", lambda conn, **kwargs: slots)
    store()
    return store


def test_every_code_outside_ours_is_refused():
    for code in OTHER:
        with pytest.raises(SystemExit, match="not one of our slots"):
            slots_config.parse_slots(code, OURS)


def test_a_wrong_guess_and_the_whole_machine_are_refused():
    with pytest.raises(SystemExit, match="11, 12, 13, 14"):
        slots_config.parse_slots("11,12,13,14,15", OURS)
    with pytest.raises(SystemExit, match="not one of our slots"):
        slots_config.parse_slots(ALL_40, OURS)


def test_check_refuses_a_code_outside_ours():
    with pytest.raises(SystemExit, match="not one of our slots"):
        slots_config.check([*OURS, "11"], "a list", OURS)


def test_part_of_our_slots_is_accepted_in_shelf_order():
    assert slots_config.parse_slots("25 15,16", OURS) == ("15", "16", "25")
    assert slots_config.check(tuple(reversed(OURS)), "a list", OURS) == OURS


def test_resolve_refuses_a_command_line_code_outside_ours(stored):
    with pytest.raises(SystemExit, match="not one of our slots"):
        slots_config.resolve_slots("15,11")


def test_resolve_checks_the_command_line_against_the_stored_server_assignment(stored):
    stored(REDRAWN)
    assert slots_config.resolve_slots("11,12,21")[1:3] == (("11", "12", "21"), slots_config.SOURCE_CLI)
    with pytest.raises(SystemExit, match="not one of our slots"):
        slots_config.resolve_slots("15,16,25")       # ours in another assignment, not in the stored one
    assert slots_config.resolve_slots(None)[1:3] == (REDRAWN, slots_config.SOURCE_SERVER)


def test_with_nothing_stored_every_run_stops_and_says_to_record(monkeypatch):
    monkeypatch.setattr(slots_config, "from_server", lambda conn, **kwargs: None)
    for cli, fall in ((None, False), ("15,16,25,26", False), (None, True), ("11,12,21,22", True)):
        with pytest.raises(SystemExit, match="python -m vending.slots record"):
            slots_config.resolve_slots(cli, fall=fall)


def test_a_stored_assignment_is_the_server_source():
    assert not slots_config.is_old_default(slots_config.SOURCE_SERVER)
    assert not slots_config.is_old_default(slots_config.SOURCE_CLI)
    assert slots_config.is_old_default(slots_config.SOURCE_OLD_DEFAULT + " (matches the server's answer of 12 Nov 2026, see #69)")


# --- the analysis's run list (resolve_run) and the labs' scope check (labkit.scope_of) ---

NOW = "2026-11-12T13:00:00+00:00"


def manifest(slots=OURS, source=None, device=slots_config.DEVICE_ID, assigned=OURS):
    return {"device": device, "slots": list(slots), "source": source or slots_config.SOURCE_SERVER,
            "assigned_slots": list(assigned), "resolved_at": "2026-11-12T12:00:00+00:00"}


def test_run_with_no_manifest_stops():
    for cli in (None, "15,16,25,26"):
        with pytest.raises(SystemExit, match="not found, so nothing says which slots are ours"):
            slots_config.resolve_run(cli, None, "slots.json", NOW)


def test_run_follows_the_manifest():
    assert slots_config.resolve_run(None, manifest(), "slots.json", NOW) == (
        slots_config.DEVICE_ID, OURS, slots_config.SOURCE_SERVER, "2026-11-12T12:00:00+00:00", OURS)


def test_run_refuses_a_scope_recorded_from_the_old_fixed_list():
    for old in (slots_config.SOURCE_OLD_DEFAULT,
                slots_config.SOURCE_OLD_DEFAULT + " (matches the server's answer of 12 Nov 2026, see #69)"):
        with pytest.raises(SystemExit, match="fixed list the code used to carry"):
            slots_config.resolve_run(None, manifest(source=old), "slots.json", NOW)


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
    # bounded by the assignment recorded with it
    with pytest.raises(SystemExit, match="not one of our slots"):
        slots_config.resolve_run(None, manifest(("15", "16"), slots_config.SOURCE_SERVER, assigned=REDRAWN), "slots.json", NOW)


def test_run_refuses_an_unknown_source():
    with pytest.raises(SystemExit, match="unknown source"):
        slots_config.resolve_run(None, manifest(source="Default list"), "slots.json", NOW)


def test_run_refuses_a_manifest_for_another_device():
    with pytest.raises(SystemExit, match="recorded for device"):
        slots_config.resolve_run(None, manifest(device="vmc2"), "slots.json", NOW)


def test_run_command_line_must_match_the_export():
    with pytest.raises(SystemExit, match="Re-export first"):
        slots_config.resolve_run("15,16,25,26", manifest(), "slots.json", NOW)
    _, slots, source, at, _ = slots_config.resolve_run(",".join(OURS), manifest(), "slots.json", NOW)
    assert (slots, source, at) == (OURS, slots_config.SOURCE_SERVER, "2026-11-12T12:00:00+00:00")
    narrowed = manifest(("15", "16", "25", "26"), slots_config.SOURCE_CLI)
    _, slots, source, at, _ = slots_config.resolve_run("26,25,16,15", narrowed, "slots.json", NOW)
    assert (slots, source, at) == (("15", "16", "25", "26"), slots_config.SOURCE_CLI, "2026-11-12T12:00:00+00:00")


def lab_data(slots=OURS, source=slots_config.SOURCE_CLI, device=slots_config.DEVICE_ID, assigned=OURS):
    return {"scope": {"device": device, "our_slots": list(slots), "assigned_slots": list(assigned), "source": source,
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


def test_a_malformed_stored_server_answer_is_refused(monkeypatch):
    for bad in (("11", "15"), REDRAWN[:-1], (*REDRAWN[:-1], "11"), (*REDRAWN[:-1], "19"), (*REDRAWN[:-1], " 11"),
                tuple(slots_config.ALL_SLOTS)):
        monkeypatch.setattr(slots_config, "from_server", lambda conn, bad=bad, **kwargs: bad)
        for fall in (False, True):
            with pytest.raises(SystemExit, match="malformed"):
                slots_config.resolve_slots(None, fall=fall)
        with pytest.raises(SystemExit, match="malformed"):
            slots_config.resolve_slots("15,16,25,26")


def test_a_narrowed_run_labels_the_rest_not_in_this_run():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "analysis" / "labs"))
    import labkit
    scope = {"device": slots_config.DEVICE_ID, "machine": {"shelves": 5, "columns": 8}, "slots_text": "x", "source_line": "x",
             "assigned_slots": list(OURS)}
    full = labkit.tokens({**scope, "our_slots": list(OURS)})
    part = labkit.tokens({**scope, "our_slots": ["15", "16"]})
    assert (full["TURF_LABEL"], full["OTHER_PHRASE"]) == ("Not our turf", "not ours")
    assert (part["TURF_LABEL"], part["OTHER_PHRASE"]) == ("Not in this run", "left out of this run")
    for v in (*full.values(), *part.values()):
        assert labkit.SAFE.fullmatch(v), v


def test_a_narrowed_run_needs_two_shelves_and_two_columns():
    with pytest.raises(SystemExit, match="one shelf or one column"):
        slots_config.parse_slots("15,16,17,18", OURS)         # one shelf
    with pytest.raises(SystemExit, match="one shelf or one column"):
        slots_config.parse_slots("15,25,35", OURS)            # one column
    assert slots_config.parse_slots("15,16,25,26", OURS) == ("15", "16", "25", "26")


# --- the fall run: all 40 selections, but only before the start (docs/integrity.md, "Before the start") ---

import datetime as dt  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

EASTERN = ZoneInfo("America/New_York")
BEFORE = dt.datetime(2026, 12, 2, 23, 59, 59, tzinfo=EASTERN)


def test_the_start_is_3_december_midnight_eastern():
    assert slots_config.START == dt.datetime(2026, 12, 3, 0, 0, tzinfo=EASTERN)
    assert slots_config.START.utcoffset() == dt.timedelta(hours=-5)
    assert len(slots_config.ALL_SLOTS) == 40 and set(OURS) < set(slots_config.ALL_SLOTS)


def test_the_guard_allows_every_selection_before_the_start():
    rows = [(s, BEFORE) for s in slots_config.ALL_SLOTS]
    assert slots_config.check_before_start(rows) == 40


def test_the_guard_refuses_another_teams_selection_from_the_start_on():
    for at in (slots_config.START, slots_config.START + dt.timedelta(seconds=1),
               dt.datetime(2026, 12, 3, 5, 0, tzinfo=dt.timezone.utc),       # the same instant, in UTC
               dt.datetime(2027, 2, 1, 12, 0, tzinfo=EASTERN)):
        for code in ("11", "24", "54"):
            with pytest.raises(SystemExit, match="not in our assignment"):
                slots_config.check_before_start([("15", BEFORE), (code, at)])
    # one second before the start, in UTC, is still the fall
    assert slots_config.check_before_start([("11", dt.datetime(2026, 12, 3, 4, 59, 59, tzinfo=dt.timezone.utc))]) == 1


def test_the_guard_lets_our_own_selections_through_after_the_start():
    later = dt.datetime(2026, 12, 10, 9, 0, tzinfo=EASTERN)
    assert slots_config.check_before_start([(s, later) for s in OURS], assigned=OURS) == 20
    # with no assignment given, nothing is exempt
    with pytest.raises(SystemExit, match="not in our assignment"):
        slots_config.check_before_start([("15", later)])
    # follows the assignment it is given: after a redraw, 15 may be theirs
    with pytest.raises(SystemExit, match="not in our assignment"):
        slots_config.check_before_start([("15", later)], assigned=("11", "12"))


def test_the_guard_refuses_a_time_without_a_zone():
    with pytest.raises(SystemExit, match="no time zone"):
        slots_config.check_before_start([("11", dt.datetime(2026, 11, 1, 12, 0))])


def test_the_fall_run_covers_all_40_and_a_normal_run_still_cannot(stored):
    assert slots_config.resolve_slots(None, fall=True)[1:3] == (slots_config.ALL_SLOTS, slots_config.SOURCE_FALL)
    assert slots_config.resolve_slots("11,12,21,22", fall=True)[1] == ("11", "12", "21", "22")
    with pytest.raises(SystemExit, match="not one of our slots"):
        slots_config.resolve_slots(ALL_40)


def test_a_fall_manifest_is_accepted_for_all_40():
    fall = manifest(slots_config.ALL_SLOTS, slots_config.SOURCE_FALL)
    got = slots_config.resolve_run(None, fall, "slots.json", NOW)
    assert got[1:3] == (slots_config.ALL_SLOTS, slots_config.SOURCE_FALL)


def test_sales_analysis_refuses_fall_rows_from_the_start_on(tmp_path):
    pd = pytest.importorskip("pandas")
    pytest.importorskip("statsmodels")
    pytest.importorskip("sklearn")
    import argparse

    import sales_analysis
    rows = [("15", "1", "5", "2026-11-02T09:00:00-05:00", 1, 250), ("11", "1", "1", "2026-12-03T00:00:00-05:00", 1, 250)]
    pd.DataFrame(rows, columns=["slot", "row", "col", "at_eastern", "units", "cents"]).to_csv(tmp_path / "s.csv", index=False)
    pd.DataFrame([(s, r, c, at, u, ct, 250) for s, r, c, at, u, ct in rows],
                 columns=["slot", "row", "col", "at_eastern", "lifetime_units", "lifetime_sales_cents", "price_cents"]
                 ).to_csv(tmp_path / "r.csv", index=False)
    sales_analysis.configure(slots_config.DEVICE_ID, slots_config.ALL_SLOTS, slots_config.SOURCE_FALL)
    args = argparse.Namespace(sales=tmp_path / "s.csv", readings=tmp_path / "r.csv")
    with pytest.raises(SystemExit, match="not in our assignment"):
        sales_analysis.load(args)


# --- our assignment is the stored server answer, checked by shape; there is no fixed list ---

def test_a_redrawn_stored_assignment_is_ours(stored):
    stored(tuple(reversed(REDRAWN)))
    assert slots_config.resolve_slots(None) == (slots_config.DEVICE_ID, REDRAWN, slots_config.SOURCE_SERVER, REDRAWN)
    assert slots_config.resolve_slots(None, fall=True) == (
        slots_config.DEVICE_ID, slots_config.ALL_SLOTS, slots_config.SOURCE_FALL, REDRAWN)


def test_from_server_requires_confirmation_for_live_reads_but_not_historical_fall(monkeypatch):
    import vending.slots as vs

    def refuse(conn):
        raise vs.SlotsNotRecorded("final assignment not confirmed")
    monkeypatch.setattr(vs, "our_slots", refuse)
    monkeypatch.setattr(vs, "stored", lambda conn: vs.Assignment("group-b", slots_config.DEVICE_ID, REDRAWN))
    assert slots_config.from_server(object(), historical=True) == REDRAWN
    with pytest.raises(SystemExit, match="not confirmed"):
        slots_config.from_server(object())
    with pytest.raises(SystemExit, match="not confirmed"):
        slots_config.resolve_slots(None, object())
    assert slots_config.resolve_slots(None, object(), fall=True)[1:] == (slots_config.ALL_SLOTS, slots_config.SOURCE_FALL, REDRAWN)
    monkeypatch.setattr(vs, "stored", lambda conn: None)
    assert slots_config.from_server(object()) is None
    monkeypatch.setattr(vs, "stored", lambda conn: vs.Assignment("group-b", "vmc2", REDRAWN))
    with pytest.raises(SystemExit, match="recorded for device"):
        slots_config.from_server(object())


def test_a_scope_recorded_for_a_redrawn_assignment_is_accepted(monkeypatch):
    monkeypatch.setattr(slots_config, "server_assignment_supported", lambda: True)
    got = slots_config.resolve_run(None, manifest(REDRAWN, slots_config.SOURCE_SERVER, assigned=REDRAWN), "slots.json", NOW)
    assert got[1:3] == (REDRAWN, slots_config.SOURCE_SERVER) and got[4] == REDRAWN
    fall = manifest(slots_config.ALL_SLOTS, slots_config.SOURCE_FALL, assigned=REDRAWN)
    assert slots_config.resolve_run(None, fall, "slots.json", NOW)[4] == REDRAWN
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "analysis" / "labs"))
    import labkit
    scope = labkit.scope_of(lab_data(REDRAWN, slots_config.SOURCE_SERVER, assigned=REDRAWN))
    assert scope["assigned_slots"] == list(REDRAWN)
    assert labkit.tokens({**scope, "slots_text": "x"})["TURF_LABEL"] == "Not our turf"


def test_a_scope_without_a_well_formed_assignment_is_refused(monkeypatch):
    monkeypatch.setattr(slots_config, "server_assignment_supported", lambda: True)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "analysis" / "labs"))
    import labkit
    for source in (slots_config.SOURCE_CLI, slots_config.SOURCE_SERVER, slots_config.SOURCE_FALL):
        slots = slots_config.ALL_SLOTS if source == slots_config.SOURCE_FALL else OURS
        missing = manifest(slots, source)
        del missing["assigned_slots"]
        with pytest.raises(SystemExit, match="no slot assignment recorded"):
            slots_config.resolve_run(None, missing, "slots.json", NOW)
        for bad in (None, ",".join(OURS), list(OURS[:-1]), [*OURS[:-1], "61"], [*OURS[:-1], OURS[0]], list(slots_config.ALL_SLOTS)):
            m = {**manifest(slots, source), "assigned_slots": bad}
            with pytest.raises(SystemExit, match="assigned_slots"):
                slots_config.resolve_run(None, m, "slots.json", NOW)
            d = lab_data(slots, source)
            d["scope"]["assigned_slots"] = bad
            with pytest.raises(SystemExit, match="assigned_slots"):
                labkit.scope_of(d)
        d = lab_data(slots, source)
        del d["scope"]["assigned_slots"]
        with pytest.raises(SystemExit, match="no scope.assigned_slots"):
            labkit.scope_of(d)
    # a list recorded from the old fixed list is refused even with a well-formed assignment
    for assigned in (OURS, REDRAWN):
        with pytest.raises(SystemExit, match="fixed list the code used to carry"):
            slots_config.resolve_run(None, manifest(OURS, slots_config.SOURCE_OLD_DEFAULT, assigned=assigned), "slots.json", NOW)
        d = lab_data(OURS, slots_config.SOURCE_OLD_DEFAULT, assigned=assigned)
        with pytest.raises(SystemExit, match="fixed list the code used to carry"):
            labkit.scope_of(d)


# --- sales_analysis.load: the date guard and the times it reads ---

def _load(tmp_path, rows, source, slots, assigned=None):
    """sales_analysis.load on per-selection rows [(slot, at_eastern text)], with one poll and one machine row.
    assigned=None calls configure() without an assignment."""
    pd = pytest.importorskip("pandas")
    pytest.importorskip("statsmodels")
    pytest.importorskip("sklearn")
    import argparse

    import sales_analysis
    pd.DataFrame([(s, s[0], s[1], at, 1, 250) for s, at in rows],
                 columns=["slot", "row", "col", "at_eastern", "units", "cents"]).to_csv(tmp_path / "s.csv", index=False)
    pd.DataFrame([(s, s[0], s[1], at, 1, 250, 250) for s, at in rows],
                 columns=["slot", "row", "col", "at_eastern", "lifetime_units", "lifetime_sales_cents", "price_cents"]
                 ).to_csv(tmp_path / "r.csv", index=False)
    pd.DataFrame({"at_eastern": ["2026-11-02T09:00:00-05:00"]}).to_csv(tmp_path / "p.csv", index=False)
    pd.DataFrame({"at_eastern": ["2026-11-02T09:00:00-05:00"], "units": [1], "cents": [250]}).to_csv(tmp_path / "m.csv", index=False)
    (tmp_path / "cal.json").write_text("{}", encoding="utf-8")
    if assigned is None:
        sales_analysis.configure(slots_config.DEVICE_ID, slots, source)
    else:
        sales_analysis.configure(slots_config.DEVICE_ID, slots, source, assigned=assigned)
    args = argparse.Namespace(sales=tmp_path / "s.csv", readings=tmp_path / "r.csv", polls=tmp_path / "p.csv",
                              machine=tmp_path / "m.csv", calendar=tmp_path / "cal.json", doors=None,
                              photos=tmp_path / "none.json", planogram=tmp_path / "none.json")
    return sales_analysis.load(args)


FALL_ROW = ("11", "2026-11-02T09:00:00-05:00")


def test_a_fall_run_refuses_even_our_own_rows_from_the_start_on(tmp_path):
    # 15 is in our assignment, but a fall run is bounded by date, not by slot
    rows = [FALL_ROW, ("15", "2026-12-04T09:00:00-05:00")]
    with pytest.raises(SystemExit, match="not in our assignment"):
        _load(tmp_path, rows, slots_config.SOURCE_FALL, slots_config.ALL_SLOTS)
    with pytest.raises(SystemExit, match="not in our assignment"):
        _load(tmp_path, rows, slots_config.SOURCE_FALL, slots_config.ALL_SLOTS, assigned=OURS)


def test_a_normal_run_follows_the_recorded_assignment_after_the_start(tmp_path):
    after = "2026-12-10T09:00:00-05:00"
    sales = _load(tmp_path, [("11", after), ("24", after)], slots_config.SOURCE_SERVER, REDRAWN, REDRAWN)[0]
    assert sorted(sales.slot) == ["11", "24"]
    with pytest.raises(SystemExit, match="outside ours|not in our assignment"):
        _load(tmp_path, [("15", after)], slots_config.SOURCE_SERVER, REDRAWN, REDRAWN)
    # no assignment given: nothing is exempt, so no row from the start on gets through
    with pytest.raises(SystemExit, match="not in our assignment"):
        _load(tmp_path, [("11", after)], slots_config.SOURCE_SERVER, REDRAWN)


@pytest.mark.parametrize("at, before_start", [
    ("2026-12-02T23:59:59-05:00", True),
    ("2026-12-03T04:59:59+00:00", True),
    ("2026-12-03T04:59:59Z", True),
    ("2026-12-02T21:59:59-07:00", True),
    ("2026-12-03T00:00:00-05:00", False),
    ("2026-12-03T05:00:00+00:00", False),
    ("2026-12-03T05:00:00Z", False),
    ("2026-12-02T22:00:00-07:00", False),
    ("2026-12-03T00:00:01-05:00", False),
])
def test_the_fall_load_cuts_at_the_start_in_any_offset(tmp_path, at, before_start):
    if before_start:
        sales = _load(tmp_path, [FALL_ROW, ("11", at)], slots_config.SOURCE_FALL, slots_config.ALL_SLOTS)[0]
        assert sales.t.max() < slots_config.START and str(sales.t.dt.tz) == "America/New_York"
    else:
        with pytest.raises(SystemExit, match="not in our assignment"):
            _load(tmp_path, [FALL_ROW, ("11", at)], slots_config.SOURCE_FALL, slots_config.ALL_SLOTS)


@pytest.mark.parametrize("at", ["2026-12-03T00:30:00", "2026-12-02T12:00:00", "2026-12-03 00:30", "",
                                "not a time", "2026-13-40T00:00:00-05:00"])
def test_load_refuses_a_time_with_no_offset_or_one_it_cannot_read(tmp_path, at):
    # pd.to_datetime(..., utc=True) would read 2026-12-03T00:30:00 as UTC: 19:30 Eastern on 2 Dec, before the start.
    # Alone in its file it would pass; next to a time with an offset, pandas would stop with its own ValueError.
    for slots, source, assigned in ((slots_config.ALL_SLOTS, slots_config.SOURCE_FALL, None),
                                    (REDRAWN, slots_config.SOURCE_SERVER, REDRAWN)):
        for rows in ([("11", at)], [FALL_ROW, ("11", at)]):
            with pytest.raises(SystemExit, match="no UTC offset|can't be read"):
                _load(tmp_path, rows, source, slots, assigned)


def _store(conn, session_id, started_at, selections):
    conn.execute("insert into polls (session_id, device_id, started_at, raw, byte_count, crc_ok)"
                 " values (%s, 'vmc1', %s, '', 0, true)", (session_id, started_at))
    for sel, units in selections.items():
        conn.execute("insert into poll_selections (session_id, selection, price_cents, lifetime_units, lifetime_sales_cents)"
                     " values (%s, %s, 250, %s, %s)", (session_id, sel, units, units * 250))


def _run_export(schema_url, out, *flags):
    import os
    import subprocess

    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    url, schema = schema_url
    params = conninfo_to_dict(url)
    params["options"] = " ".join(o for o in (params.get("options"), f"-c search_path={schema}") if o)
    # The lock far off: conftest's assignment was last checked before the start, and the export reads the real
    # database clock, so after 3 Dec it would refuse (the run's own slot clock isn't patched in a subprocess).
    env = {**os.environ, "DATABASE_URL": make_conninfo(**params), "SLOTS_LOCK_AT": "2999-01-01T00:00:00+00:00"}
    repo = Path(__file__).resolve().parent.parent
    return subprocess.run([sys.executable, str(repo / "analysis" / "export_sales.py"), *flags, str(out)],
                          cwd=repo, env=env, capture_output=True, text=True)


def _export(schema_url, out, *flags):
    import csv
    import json

    p = _run_export(schema_url, out, *flags)
    assert p.returncode == 0, p.stdout + p.stderr
    with open(out / "poll_readings.csv", newline="") as f:
        readings = list(csv.DictReader(f))
    with open(out / "polls.csv", newline="") as f:
        polls = [r["at_eastern"] for r in csv.DictReader(f)]
    return readings, polls, json.loads((out / "slots.json").read_text(encoding="utf-8"))


def test_the_fall_export_stops_at_the_start(conn, schema_url, tmp_path):
    _store(conn, "p1", "2026-11-20T15:00:00Z", {"11": 5, "15": 7})
    _store(conn, "p2", "2026-12-03T04:59:59Z", {"11": 6, "15": 8})      # 23:59:59 Eastern on 2 Dec: still the fall
    _store(conn, "p3", "2026-12-03T05:00:00Z", {"11": 9, "15": 9})      # midnight Eastern on 3 Dec: the start
    _store(conn, "p4", "2026-12-10T15:00:00Z", {"11": 12, "15": 11})
    readings, polls, manifest_ = _export(schema_url, tmp_path / "fall", "--fall-all")
    assert manifest_["source"] == slots_config.SOURCE_FALL and manifest_["before"] == slots_config.START.isoformat()
    assert sorted(manifest_["slots"]) == sorted(slots_config.ALL_SLOTS)
    assert polls == ["2026-11-20T10:00:00-05:00", "2026-12-02T23:59:59-05:00"]
    assert sorted((r["slot"], r["lifetime_units"]) for r in readings) == [("11", "5"), ("11", "6"), ("15", "7"), ("15", "8")]
    # a normal export still writes no row for a selection that isn't ours, and keeps our later polls
    readings, polls, _ = _export(schema_url, tmp_path / "ours")
    assert {r["slot"] for r in readings} == {"15"} and len(polls) == 4


def test_the_export_follows_a_redrawn_stored_assignment(conn, schema_url, tmp_path):
    # the assignment changed before the start: the export follows what is stored, and --fall-all still runs
    conn.execute("update slot_assignment set slots = %s where id = 1", (list(REDRAWN),))
    _store(conn, "p1", "2026-11-20T15:00:00Z", {"11": 5, "15": 7})
    _store(conn, "p2", "2026-12-10T15:00:00Z", {"11": 9, "15": 9})
    readings, polls, manifest_ = _export(schema_url, tmp_path / "ours")
    assert sorted((r["slot"], r["lifetime_units"]) for r in readings) == [("11", "5"), ("11", "9")] and len(polls) == 2
    assert (manifest_["source"], manifest_["assigned_slots"]) == (slots_config.SOURCE_SERVER, list(REDRAWN))
    readings, polls, manifest_ = _export(schema_url, tmp_path / "fall", "--fall-all")
    assert (manifest_["source"], manifest_["assigned_slots"]) == (slots_config.SOURCE_FALL, list(REDRAWN))
    assert sorted((r["slot"], r["lifetime_units"]) for r in readings) == [("11", "5"), ("15", "7")] and len(polls) == 1


def test_the_export_stops_with_nothing_stored(conn, schema_url, tmp_path):
    conn.execute("delete from slot_assignment")
    _store(conn, "p1", "2026-11-20T15:00:00Z", {"11": 5, "15": 7})
    for flags in ((), ("--fall-all",)):
        p = _run_export(schema_url, tmp_path / "out", *flags)
        assert p.returncode != 0 and "python -m vending.slots record" in p.stdout + p.stderr
        assert not (tmp_path / "out" / "slots.json").exists()


# --- the fall cut: the fall data ends before the day-one fill, not at the start ---

CUT = dt.datetime(2026, 11, 18, 0, 0, tzinfo=EASTERN)


def test_the_fall_cut_defaults_to_18_november_and_is_never_after_the_start(monkeypatch):
    monkeypatch.delenv(slots_config.FALL_CUTOFF_SETTING)
    from vending import settings
    monkeypatch.setattr(settings, "_FILE", {})
    assert slots_config.fall_end() == CUT
    monkeypatch.setenv(slots_config.FALL_CUTOFF_SETTING, "2026-11-30T08:00:00-05:00")
    assert slots_config.fall_end() == dt.datetime(2026, 11, 30, 8, tzinfo=EASTERN)
    monkeypatch.setenv(slots_config.FALL_CUTOFF_SETTING, "2026-12-20T00:00:00-05:00")
    assert slots_config.fall_end() == slots_config.START                    # never later than the start
    for bad in ("18 Nov", "2026-11-18", "2026-11-18T00:00:00"):
        monkeypatch.setenv(slots_config.FALL_CUTOFF_SETTING, bad)
        with pytest.raises(SystemExit, match="FALL_CUTOFF"):
            slots_config.fall_end()


def test_the_guard_refuses_a_poll_between_the_cut_and_the_start(monkeypatch):
    """The review's case on #146: the day-one fill goes in before the start, so its prices are the teams' own."""
    monkeypatch.delenv(slots_config.FALL_CUTOFF_SETTING)
    between = dt.datetime(2026, 12, 1, 12, 0, tzinfo=EASTERN)
    assert slots_config.check_before_start([("11", CUT - dt.timedelta(seconds=1))]) == 1
    for at in (CUT, between):
        with pytest.raises(SystemExit, match="not in our assignment"):
            slots_config.check_before_start([("11", at)])
    assert slots_config.check_before_start([("15", between)], assigned=OURS) == 1        # our own, any time


def test_the_exports_stop_at_the_fall_cut(conn, schema_url, tmp_path, monkeypatch):
    import csv

    monkeypatch.delenv(slots_config.FALL_CUTOFF_SETTING)
    _store(conn, "p1", "2026-11-15T15:00:00Z", {"11": 5, "15": 7})
    _store(conn, "p2", "2026-11-17T15:00:00Z", {"11": 6, "15": 8})      # before the cut: the whole machine
    _store(conn, "p3", "2026-12-01T15:00:00Z", {"11": 9, "15": 9})      # between the cut and the start
    _store(conn, "p4", "2026-12-10T15:00:00Z", {"11": 12, "15": 11})
    readings, polls, manifest_ = _export(schema_url, tmp_path / "fall", "--fall-all")
    assert manifest_["before"] == CUT.isoformat()
    assert polls == ["2026-11-15T10:00:00-05:00", "2026-11-17T10:00:00-05:00"]
    assert sorted((r["slot"], r["lifetime_units"]) for r in readings) == [("11", "5"), ("11", "6"), ("15", "7"), ("15", "8")]
    # a normal export: whole-machine totals before the cut, only ours from it on
    readings, polls, _ = _export(schema_url, tmp_path / "ours")
    assert {r["slot"] for r in readings} == {"15"} and len(polls) == 4
    with open(tmp_path / "ours" / "machine_hourly.csv", newline="") as f:
        units = [int(r["units"]) for r in csv.DictReader(f)]
    assert units == [0, 2, 1, 2]           # p2: 11 and 15; p3 and p4: 15 only


def test_the_fall_section_shows_groups_never_one_selection_outside_ours():
    """The committed report is public: the fall section gives bands, shelves and halves, not one selection's rates."""
    pytest.importorskip("pandas")
    pytest.importorskip("statsmodels")
    import fall_rates

    def group(level, n=10):
        return dict(level=level, n=n, mean_rate=1.0, ci=[0.5, 1.5], pooled_rate=1.0, pooled_ci=[0.5, 1.5],
                    avg_price=2.5, instock_days=100)

    other = [s for s in slots_config.ALL_SLOTS if s not in OURS]
    per = [dict(slot=s, row=int(s[0]), col=int(s[1]), half="left" if int(s[1]) <= 4 else "right", band="$2.50",
                rate=0.1 if s in ("31", "34") else 1.0, instock_class_days=5 if s == "12" else 60, avg_price=2.5)
           for s in slots_config.ALL_SLOTS]
    ols = dict(coef=0.1, ci=[-0.2, 0.4], p=0.5, n=40)
    result = dict(
        scope=dict(source=slots_config.SOURCE_FALL, device="vmc1", first_poll="2026-08-27T00:00", last_poll="2026-11-11T18:00",
                   before="2026-11-18T00:00:00-05:00", selections=38, dropped=["21", "44"], seed=1, bootstrap=100,
                   permutations=100),
        by_band={h: [group("$2.50")] for h in ("all", "left", "right")}, by_row=[group(r) for r in range(1, 6)],
        by_half=[group("left"), group("right")],
        left_right=dict(mean_diff=0.1, mean_ci=[-0.2, 0.4], pooled_diff=0.1, pooled_ci=[-0.2, 0.4], p_perm_within_shelf=0.5,
                        ols=dict(shelf=ols, shelf_price=ols), by_row=[dict(row=r, left=1.0, right=1.1) for r in range(1, 6)],
                        avg_price=dict(left=2.4, right=2.6)),
        our_rates_match_normal_run=True,
        segments_ours=[dict(name="Steady", slots=list(OURS[:5]), rate=1.0, avg_price=2.5)],
        segments_all_40=[dict(name="Steady", slots=other[:6] + list(OURS[:4]), left=6, right=4, rate=1.0, avg_price=2.5)],
        per_selection=per)
    text = fall_rates.report(result)
    assert "## All 40 selections, fall (before the fall cut)" in text and "2026-11-18T00:00" in text
    assert ", ".join(other[:6]) not in text                     # the all-40 segments give counts
    for listed in ("21, 44", "31, 34", "(12)", "(12,", ": 12", "(31", "(21"):
        assert listed not in text, listed
    assert "2 left out" in text
    assert "1 selections were in stock fewer than 11 class days (1 in the left half, 0 in the right)" in text
    assert "(2 selections, 2 in the left half, 0 in the right:" in text


def test_no_fall_writes_the_report_without_its_fall_section(tmp_path):
    """A21's rollback: --no-fall must take the fall section out of REPORT.md, not leave the old one in place."""
    import rebuild

    (tmp_path / "fall40").mkdir()
    (tmp_path / "analysis_report.md").write_text("# Report\n\nbody\n", encoding="utf-8")
    (tmp_path / "fall40" / "fall_rates.md").write_text("## All 40 selections, fall\n\nrates\n", encoding="utf-8")
    assert rebuild.report_text(True, tmp_path) == "# Report\n\nbody\n"
    assert rebuild.report_text(False, tmp_path) == "# Report\n\nbody\n\n## All 40 selections, fall\n\nrates\n"
