"""First-fill plans through the person-run command and the dashboard snapshot."""

from datetime import timedelta

import pytest
from psycopg.errors import CheckViolation

from tests.conftest import TEST_SLOTS, open_in
from tests.test_dashboard import catalog
from tests.test_plan_rounds import day_one, projection, write
from vending import agent_runs, dashboard, plan_store, slots


def unassigned_slot():
    return next(r + c for r in slots.SHELVES for c in slots.COLUMNS if r + c not in TEST_SLOTS)


def test_cli_load_pick_and_list(conn, schema_url, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(plan_store.db, "connect", lambda: open_in(*schema_url))
    write(tmp_path, "round3", "3", {"day_one": day_one()}, projection())
    folder = tmp_path / "round3"
    (folder / "plan3.json").rename(folder / "r3.plan.json")
    assert plan_store.main(["load", str(folder), "--round", "3", "--by", "Test Loader"]) == 0
    assert plan_store.main(["load", str(tmp_path), "--round", "3", "--by", "Test Loader"]) == 0
    rows = conn.execute(
        "select id, loaded_by, label, source_note, plan, projection, slots, picked"
        " from first_fill_plans order by id").fetchall()
    first, second = (row[0] for row in rows)
    assert rows[0][1:] == ("Test Loader", "3", "round3/r3.plan.json + round3/projection3.json",
                           {"day_one": day_one()}, projection(), list(TEST_SLOTS), False)
    for plan_id in (second, first):
        assert plan_store.main(["pick", str(plan_id), "--by", "Test Picker"]) == 0
    assert plan_store.main(["pick", "99999", "--by", "Test Picker"]) != 0
    assert conn.execute("select id from first_fill_plans where picked").fetchall() == [(first,)]
    assert plan_store.main(["list"]) == 0
    out = capsys.readouterr().out
    assert f"{first}  picked" in out and f"{second}  unpicked" in out
    assert str(tmp_path) not in out
    assert conn.execute("select tool, arguments, outcome from action_log order by id").fetchall() == [
        ("plan_store.load", {"id": first, "by": "Test Loader"}, "done"),
        ("plan_store.load", {"id": second, "by": "Test Loader"}, "done"),
        ("plan_store.pick", {"id": second, "by": "Test Picker"}, "done"),
        ("plan_store.pick", {"id": first, "by": "Test Picker"}, "done"),
    ]


def test_load_refuses_a_plan_with_an_unassigned_slot(conn, tmp_path):
    outside = unassigned_slot()
    write(tmp_path, "round3", "3", {"day_one": [{"slot": outside, "units": 6, "price_cents": 200}]})
    with pytest.raises(ValueError, match="isn't in our current assignment"):
        plan_store.load(conn, tmp_path, "3", "Test Loader")
    assert conn.execute("select count(*) from first_fill_plans").fetchone() == (0,)
    assert conn.execute("select count(*) from action_log").fetchone() == (0,)


def test_first_fill_switches_to_weekly_plan_at_the_lock(conn, tmp_path, monkeypatch):
    lock = slots.lock_at()
    before = lock - timedelta(microseconds=1)
    slot = TEST_SLOTS[-1]
    write(tmp_path, "round3", "3", {"day_one": day_one(**{slot: {"units": 6, "why": "Early fill reason."}})}, projection())
    old = plan_store.load(conn, tmp_path, "3", "Test Loader")
    write(tmp_path, "round3", "3", {"day_one": day_one(**{"15": {"why": "New fill reason."}})}, projection())
    new = plan_store.load(conn, tmp_path, "3", "Test Loader")
    catalog(conn, ("WEEKLY", "Weekly drink", "Weekly", None, "20 oz bottle"))
    run = agent_runs.start(conn, mode="live")
    agent_runs.finish(conn, run, "skipped", plan={"slots": [
        {"slot": slot, "product": "WEEKLY", "fill_to": 5, "reason": "Weekly reason."}]})
    conn.execute("update agent_runs set started_at = %s, finished_at = %s where id = %s",
                 (before - timedelta(minutes=1), before, run))
    early = dashboard.snapshot(conn, before)
    assert early["plan"]["data"]["id"] == new
    assert "latest, not picked" in dashboard.render(early)
    plan_store.pick(conn, old, "Test Picker")
    early = dashboard.snapshot(conn, before)
    assert early["plan"]["data"]["id"] == old
    first_row = next(row for row in early["plan"]["data"]["slots"] if row["slot"] == slot)
    assert first_row["checks"]["legal_price"] is True and first_row["checks"]["capacity"] == 8
    assert first_row["fill_check"] == "fills 6 of 8: 2 spaces empty"
    page = dashboard.render(early)
    plan_html = page.split('<section id="plan">')[1].split("</section>")[0]
    assert "Early fill reason." in plan_html and "Weekly reason." not in plan_html
    summary = plan_html.split(f'id="slot-{slot}"', 1)[1].split("</summary>", 1)[0]
    assert "fills 6 of 8: 2 spaces empty" in summary and "Early fill reason." in summary
    assert "Code checks" in plan_html and "legal price" in plan_html
    assert [item["text"] for item in early["attention"] if " spaces empty" in item["text"]] == [
        f"Slot {slot} fills 6 of 8: 2 spaces empty. Early fill reason."]
    conn.execute("update slot_assignment set last_checked_at = %s", (lock,))
    monkeypatch.setattr(slots, "clock", lambda conn: lock)
    at_lock = dashboard.snapshot(conn, lock)
    assert at_lock["plan"]["data"]["run"]["id"] == run
    plan_html = dashboard.render(at_lock).split('<section id="plan">')[1].split("</section>")[0]
    assert "Weekly reason." in plan_html and "Plan for the first fill" not in plan_html
    assert "fills 5 of 7: 2 spaces empty" in plan_html
    assert [item["text"] for item in at_lock["attention"] if " spaces empty" in item["text"]] == [
        f"Slot {slot} fills 5 of 7: 2 spaces empty. Weekly reason."]


def test_redraw_hides_the_picked_plan_even_if_a_newer_one_matches(conn, tmp_path):
    old_slot = TEST_SLOTS[0]
    new_slot = unassigned_slot()
    write(tmp_path, "round3", "3", {"day_one": day_one()})
    old = plan_store.load(conn, tmp_path, "3", "Test Loader")
    plan_store.pick(conn, old, "Test Picker")
    # Reordering the same selection set is not a redraw.
    conn.execute("update slot_assignment set slots = %s", (list(reversed(TEST_SLOTS)),))
    now = slots.lock_at() - timedelta(days=1)
    assert len(dashboard.snapshot(conn, now)["plan"]["data"]["slots"]) == len(TEST_SLOTS)
    assignment = [new_slot, *TEST_SLOTS[1:]]
    conn.execute("update slot_assignment set slots = %s", (assignment,))
    write(tmp_path, "round3", "3", {"day_one": day_one(**{old_slot: {"slot": new_slot}})})
    new = plan_store.load(conn, tmp_path, "3", "Test Loader")
    snap = dashboard.snapshot(conn, now)
    p = snap["plan"]["data"]
    assert p["slots"] == []
    assert p["message"] == "planned for an earlier slot assignment: load a new plan"
    section = dashboard.render(snap).split('<section id="plan">')[1].split("</section>")[0]
    assert p["message"] in section and "<details" not in section
    plan_store.pick(conn, new, "Test Picker")
    p = dashboard.snapshot(conn, now)["plan"]["data"]
    assert {row["slot"] for row in p["slots"]} == set(assignment)


def test_pick_and_audit_rollback_together(conn, tmp_path):
    write(tmp_path, "round3", "3", {"day_one": day_one()})
    old = plan_store.load(conn, tmp_path, "3", "Test Loader")
    new = plan_store.load(conn, tmp_path, "3", "Test Loader")
    plan_store.pick(conn, old, "Test Picker")
    conn.execute("alter table action_log add check (tool <> 'plan_store.pick') not valid")
    with pytest.raises(CheckViolation):
        plan_store.pick(conn, new, "Test Picker")
    assert conn.execute("select id from first_fill_plans where picked").fetchall() == [(old,)]
