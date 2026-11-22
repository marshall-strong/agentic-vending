"""The day-one plan model in analysis/dayone (#230, #231): it runs from the repo and reproduces round 7's record.

No database. Our slots come from the plan files, as the round scripts read them; nothing here lists them."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from vending import plan_rounds

DAYONE = Path(__file__).resolve().parent.parent / "analysis" / "dayone"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def model():
    spec = importlib.util.spec_from_file_location("dayone_project", DAYONE / "project.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_cap_is_kevins_400():
    """Kevin, 22 Nov: the day-one fill's most-we-pay cap is $400, up from #113's proposed $366."""
    P = model()
    assert (P.FILL_CAP, P.FILL_CAP_CENTS) == (400.00, 40_000)


@pytest.mark.parametrize("plan, cap, base_profit, expected, most", [
    ("7", 366, 1018.31, 315.16, 366.00),     # round 7 at #113's cap
    ("7b", 400, 1088.66, 343.50, 399.00),    # the same round at Kevin's $400: the picked plan
])
def test_a_plan_scores_as_its_recorded_projection(plan, cap, base_profit, expected, most):
    P = model()
    catalog = {p["sku"]: p for p in load(DAYONE / "round7" / "catalog7.json")["products"]}
    recorded = load(DAYONE / "round7" / f"projection{plan}.json")
    scored = P.project(load(DAYONE / "round7" / f"plan{plan}.json"), catalog, P.load_calendar(), "test", cap=cap)
    assert scored["problems"] == [] and scored["under_cap"] and recorded["fill_cap"] == cap
    assert scored["scenarios"]["base"]["profit"] == recorded["scenarios"]["base"]["profit"] == base_profit
    assert json.loads(json.dumps(scored["scenarios"])) == recorded["scenarios"]   # as written: week keys become text
    assert (scored["fill_expected"], scored["fill_most"]) == (recorded["fill_expected"], recorded["fill_most"]) == (expected, most)


def test_plan_7b_has_20_distinct_drinks_and_10_brands():
    skus = [d["sku"] for d in load(DAYONE / "round7" / "plan7b.json")["day_one"]]
    assert len(skus) == len(set(skus)) == 20
    assert len({s.split("-")[0].split("_")[0] for s in skus}) == 10


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


def test_the_round_reader_reads_rounds_6_7_and_7b_for_the_slots_they_were_made_for():
    ours = tuple(str(s) for s in model().slots_from(DAYONE / "round6" / "plan6.json"))
    found = {rid: (plan, projection) for rid, plan, projection in plan_rounds.discover(DAYONE)}
    assert set(found) == {"6", "7", "7b"}
    for rid, (plan, projection) in found.items():
        checked, _, _ = plan_rounds.read_round(rid, plan, projection, ours)
        assert len(checked.slots) == 20 and not checked.warnings, rid
    for plan in ("plan7.json", "plan7b.json"):
        assert ours == tuple(str(s) for s in model().slots_from(DAYONE / "round7" / plan)), plan
