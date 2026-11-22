"""The day-one plan model in analysis/dayone (#230, #231): it runs from the repo and reproduces round 7's record.

No database. Our slots come from the plan files, as the round scripts read them; nothing here lists them."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from vending import plan_rounds

DAYONE = Path(__file__).resolve().parent.parent / "analysis" / "dayone"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def model():
    spec = importlib.util.spec_from_file_location("dayone_project", DAYONE / "project.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_plan_7_scores_as_its_recorded_projection():
    P = model()
    catalog = {p["sku"]: p for p in load(DAYONE / "round7" / "catalog7.json")["products"]}
    recorded = load(DAYONE / "round7" / "projection7.json")
    scored = P.project(load(DAYONE / "round7" / "plan7.json"), catalog, P.load_calendar(), "test")
    assert scored["problems"] == []
    assert scored["scenarios"]["base"]["profit"] == recorded["scenarios"]["base"]["profit"] == 1018.31
    assert json.loads(json.dumps(scored["scenarios"])) == recorded["scenarios"]   # as written: week keys become text
    assert scored["fill_most_cents"] == recorded["fill_most_cents"] <= P.FILL_CAP_CENTS


def test_a_second_slot_of_one_drink_adds_no_demand():
    """Kevin, 22 Nov: a duplicate adds capacity only. Plan 7 with one slot copied onto another sells no more of
    that drink in the base case, where nothing sells out between Friday refills."""
    P = model()
    catalog = {p["sku"]: p for p in load(DAYONE / "round7" / "catalog7.json")["products"]}
    plan = load(DAYONE / "round7" / "plan7.json")
    first, second = plan["day_one"][0], plan["day_one"][1]
    doubled = {"day_one": [first, {**first, "slot": second["slot"]}]}
    single = {"day_one": [first]}
    base = lambda p: P.project(p, catalog, P.load_calendar())["scenarios"]["base"]["units_sold"]  # noqa: E731
    assert base(doubled) == base(single)


def test_the_round_reader_reads_rounds_6_and_7_for_the_slots_they_were_made_for():
    ours = tuple(str(s) for s in model().slots_from(DAYONE / "round6" / "plan6.json"))
    found = {rid: (plan, projection) for rid, plan, projection in plan_rounds.discover(DAYONE)}
    assert set(found) == {"6", "7"}
    for rid, (plan, projection) in found.items():
        checked, _, _ = plan_rounds.read_round(rid, plan, projection, ours)
        assert len(checked.slots) == 20 and not checked.warnings, rid
    assert ours == tuple(str(s) for s in model().slots_from(DAYONE / "round7" / "plan7.json"))
