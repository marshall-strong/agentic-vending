"""The day-one plan model in analysis/dayone (#230, #231), on a synthetic catalog and the tests' made-up assignment.

No database and no real plan: the real round files map our slots to products, and our assignment is private
(docs/integrity.md), so they live outside git (analysis/dayone/README.md). Every SKU, price and slot here is made up."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from tests.conftest import TEST_SLOTS
from vending import plan_rounds

DAYONE = Path(__file__).resolve().parent.parent / "analysis" / "dayone"


def module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def P():
    return module("dayone_project", DAYONE / "project.py")


def product(sku, cat="tea", size="16 oz can", pack=12, price=9.00, checked=True, priced=True, interloper=False,
            most=None):
    """A made-up catalog product in catalog7.json's shape. `price` and `most` are dollars per pack (expected, and
    most-we-pay; None: the projection's x1.15 rule)."""
    caps = {"16 oz can": 8, "12 oz can": 8, "20 oz bottle": 7, "8.4 oz can": 11}
    return dict(sku=sku, name=sku.title(), size=size, cat=cat, cap=caps[size], capacity_assumed=False, pack=pack,
                price=price if priced else 0.0, max=most, store="Costco", priced=priced, checked_source=checked,
                plannable=priced, interloper=interloper)


def catalog(*products) -> dict:
    return {p["sku"]: p for p in products}


def plan(*rows) -> dict:
    """rows: (sku, price) or (sku, price, slot); slots default to the tests' assignment, in order."""
    out = []
    for i, row in enumerate(rows):
        sku, price, *slot = row
        out.append(dict(slot=int(slot[0] if slot else TEST_SLOTS[i]), sku=sku, units=None, price=price))
    return {"day_one": out}


def filled(p: dict, cat: dict) -> dict:
    for row in p["day_one"]:
        row["units"] = cat[row["sku"]]["cap"]
    return p


def test_the_cap_is_kevins_400(P):
    """Kevin, 22 Nov: the day-one fill's most-we-pay cap is $400, up from #113's proposed $366."""
    assert (P.FILL_CAP, P.FILL_CAP_CENTS) == (400.00, 40_000)


# --- Duplicates and rounding (finding 5) -------------------------------------------------------------------------

@pytest.mark.parametrize("price", [2.30, 2.95, 3.45, 4.10, 5.25, 6.50])
def test_a_second_slot_of_one_drink_adds_no_demand_to_the_totals(P, price):
    """Kevin, 22 Nov: a duplicate adds capacity only. With nothing selling out, the totals are the same with one slot
    or two, to the cent: they add the product's sales before rounding, not each slot's rounded share."""
    cat = catalog(product("TEA-A"))
    one = P.project(filled(plan(("TEA-A", price)), cat), cat, P.load_calendar())
    two = P.project(filled(plan(("TEA-A", price), ("TEA-A", price)), cat), cat, P.load_calendar())
    for name in P.SCENARIOS:
        a, b = one["scenarios"][name], two["scenarios"][name]
        assert a["units_lost"] == b["units_lost"] == 0, name
        for key in ("units_sold", "gross_profit", "profit", "week1_gp"):
            assert a[key] == b[key], (name, key)
        assert a["totals"] == b["totals"], name


def test_a_slot_that_runs_out_exactly_records_its_sellout(P, monkeypatch):
    """Demand of exactly 2 a class day: after a Friday refill and Friday's second half, a 7-unit slot holds 6, and
    Monday to Wednesday empty it exactly. Thursday finds it empty. That is a sellout, though no sale came up short
    while units were left."""
    monkeypatch.setattr(P, "TIER_RATE", {**P.TIER_RATE, "tea": 2.0})
    monkeypatch.setattr(P, "REF_PRICE", {**P.REF_PRICE, "tea": 3.00})
    monkeypatch.setattr(P, "SCENARIOS", {"low": (1.0, 0.0, 0.0), "base": (1.0, 0.0, 0.0), "high": (1.0, 0.0, 0.0)})
    cat = catalog(product("TEA-A", size="20 oz bottle"))
    base = P.project(filled(plan(("TEA-A", 3.00)), cat), cat, P.load_calendar())["scenarios"]["base"]
    [slot] = base["per_slot"]
    assert slot["units_lost"] > 0
    assert slot["first_sellout"] == slot["sellout_date"] == "2026-12-09"     # Wednesday, its last unit sold
    assert slot["sellout_days"] >= 2                                         # Wednesday, then Thursday's lost sales


# --- Category limits (finding 3) ----------------------------------------------------------------------------------

def test_the_category_limit_doesnt_depend_on_the_plans_order(P):
    """Five teas against a limit of four: the penalty falls on a product chosen the same way whatever the order."""
    cat = catalog(*(product(f"TEA-{c}", price=p) for c, p in zip("ABCDE", (6.0, 7.0, 8.0, 9.0, 10.0))))
    rows = [(f"TEA-{c}", 2.50) for c in "ABCDE"]
    forward = P.project(filled(plan(*rows), cat), cat, P.load_calendar())
    backward = P.project(filled(plan(*reversed(rows)), cat), cat, P.load_calendar())
    for name in P.SCENARIOS:
        assert forward["scenarios"][name]["profit"] == backward["scenarios"][name]["profit"], name
    past = {s["sku"]: s["cat_past"] for s in forward["slots"]}
    assert past == {s["sku"]: s["cat_past"] for s in backward["slots"]}
    assert sum(1 for v in past.values() if v) == 1


# --- Prices and sourcing (finding 2) ------------------------------------------------------------------------------

def test_an_unpriced_product_is_a_problem_and_a_checked_row_decides_needs_new_source(P):
    cat = catalog(product("TEA-A"), product("TEA-B", checked=False), product("TEA-C", priced=False, checked=False))
    res = P.project(filled(plan(("TEA-A", 2.50), ("TEA-B", 2.50), ("TEA-C", 2.50)), cat), cat, P.load_calendar())
    assert any("TEA-C" in p and "no price" in p for p in res["problems"]), res["problems"]
    assert res["needs_new_source"] == ["TEA-B", "TEA-C"]


def test_the_optimisers_never_consider_an_unpriced_product():
    opt = module("optimize", DAYONE / "optimize.py")
    left_out = {**product("TEA-OUT"), "plannable": False}       # priced, but left out on purpose (a duplicate SKU)
    cat = catalog(product("TEA-A"), product("TEA-FREE", priced=False, checked=False), product("TEA-B", checked=False),
                  left_out)
    assert opt.candidates(cat) == ["TEA-A", "TEA-B"]
    assert opt.candidates(cat, only_checked=True) == ["TEA-A"]


# --- Day one: distinct drinks (finding 4) -------------------------------------------------------------------------

def opt7():
    module("dayone_project", DAYONE / "project.py")
    sys.modules["project"] = sys.modules["dayone_project"]
    module("optimize", DAYONE / "optimize.py")
    return module("dayone_optimize7", DAYONE / "round7" / "optimize7.py")


# One 24-pack of NRG-A fills two 11-can slots, so a second slot of it costs nothing more. NRG-B earns the most as a
# second drink, but after A and B ($57.50 most) no third pack fits under $60, so a loose search doubles A. Two cheap
# sodas with A ($43.50) are the mix of different drinks that fits.
SPREAD = (product("NRG-A", cat="energy", size="8.4 oz can", pack=24, price=30.00),
          product("NRG-B", cat="energy", size="16 oz can", pack=12, price=20.00),
          product("SODA-C", cat="sodacan", size="12 oz can", pack=8, price=3.50),
          product("SODA-D", cat="sodacan", size="12 oz can", pack=8, price=3.50))


def test_the_day_one_mix_has_no_duplicates_even_where_a_double_scores_higher():
    o = opt7()
    cat = catalog(*SPREAD)
    slots = [int(s) for s in TEST_SLOTS[:3]]
    loose = o.optimise(cat, slots, 6_000, distinct=False, test_slot=False, say=lambda m: None)
    assert sorted(loose["skus"]) == ["NRG-A", "NRG-A", "NRG-B"], "the case should tempt a loose search to double up"
    strict = o.optimise(cat, slots, 6_000, test_slot=False, say=lambda m: None)
    assert sorted(strict["skus"]) == ["NRG-A", "SODA-C", "SODA-D"] and strict["fill_most"] <= 60.00


def test_a_cap_no_distinct_mix_fits_under_is_reported_plainly():
    o = opt7()
    cat = catalog(*SPREAD)
    with pytest.raises(o.NoMix, match=r"3 different drinks.*\$20\.00"):
        o.optimise(cat, [int(s) for s in TEST_SLOTS[:3]], 2_000, test_slot=False, say=lambda m: None)


# --- Fall data stops at the cut (finding 1) -----------------------------------------------------------------------

def test_placement_refuses_fall_rows_from_the_cut_on(tmp_path):
    sys.path.insert(0, str(DAYONE.parent))
    module("dayone_project", DAYONE / "project.py")
    sys.modules["project"] = sys.modules["dayone_project"]
    place = module("dayone_place7", DAYONE / "round7" / "place7.py")
    slots = [int(s) for s in TEST_SLOTS[:2]]
    header = "slot,row,col,at_eastern,units,cents\n"
    early = f"{slots[0]},1,5,2026-10-01T12:00:00-04:00,3,600\n{slots[1]},1,6,2026-10-02T12:00:00-04:00,1,200\n"
    (tmp_path / "sales_events.csv").write_text(header + early, encoding="utf-8")
    assert place.positions(slots, tmp_path) == [slots[0], slots[1]]
    late = f"{slots[1]},1,6,2026-12-05T12:00:00-05:00,9,1800\n"
    (tmp_path / "sales_events.csv").write_text(header + early + late, encoding="utf-8")
    with pytest.raises(SystemExit, match="fall cut"):
        place.positions(slots, tmp_path)


# --- The round reader (how plan_store loads a round) ----------------------------------------------------------------

def test_the_round_reader_reads_a_projected_round(tmp_path, P):
    """A made-up 20-drink round, projected by project.py, reads back through vending.plan_rounds as plan_store
    would load it, with no warnings."""
    cat = catalog(*(product(f"TEA-{i:02d}", price=9.00 + i / 4) for i in range(20)))
    p = filled(plan(*((f"TEA-{i:02d}", 2.50) for i in range(20))), cat)
    folder = tmp_path / "round9"
    folder.mkdir()
    (folder / "plan9.json").write_text(json.dumps(p), encoding="utf-8")
    (folder / "projection9.json").write_text(json.dumps(P.project(p, cat, P.load_calendar(), "plan9.json")),
                                             encoding="utf-8")
    [(rid, plan_file, projection_file)] = plan_rounds.discover(tmp_path)
    checked, _, _ = plan_rounds.read_round(rid, plan_file, projection_file, TEST_SLOTS)
    assert rid == "9" and len(checked.slots) == 20 and not checked.warnings


# --- Re-check of #231 (Sol, 22 Nov) -------------------------------------------------------------------------------

def test_a_sourcing_row_without_an_expected_price_falls_back_to_most_we_pay(monkeypatch):
    """The planner reads a missing expected price as the most-we-pay (vending.planner._Sources); the catalog builder
    used to crash on it."""
    build = module("dayone_build_catalog7", DAYONE / "round7" / "build_catalog7.py")
    snap = {"capacity_overrides": {},
            "catalog": [{"sku": "TEA-A", "name": "Tea A", "size": "16 oz can"}],
            "requests": [],
            "sourcing": [{"sku": "TEA-A", "rank": "primary", "store": "Costco", "units_per_pack": 12,
                          "expected_cents": None, "max_cents": 1299, "checked_at": "2026-11-20 12:00:00+00"}]}
    [p] = build.build(snap)
    assert (p["price"], p["max"], p["pack"]) == (12.99, 12.99, 12)
    assert p["priced"] and p["checked_source"] and p["plannable"]


def test_the_test_slot_tries_a_cheaper_drink_of_its_category_when_the_best_doesnt_fit():
    """$6 for three slots. TEA-RICH scores best on its own but costs $4.00, which leaves too little for two sodas;
    TEA-LEAN ($1.00) with both sodas ($1.50 each) fits. The search used to stop at TEA-RICH and report NoMix."""
    o = opt7()
    cat = catalog(product("TEA-RICH", price=3.50, most=4.00),
                  product("TEA-LEAN", price=0.90, most=1.00, interloper=True),
                  product("SODA-ONE", cat="sodacan", size="12 oz can", pack=8, price=1.30, most=1.50),
                  product("SODA-TWO", cat="sodacan", size="12 oz can", pack=8, price=1.30, most=1.50))
    res = o.optimise(cat, [int(s) for s in TEST_SLOTS[:3]], 600, say=lambda m: None)
    assert sorted(res["skus"]) == ["SODA-ONE", "SODA-TWO", "TEA-LEAN"]
    assert res["test_sku"] == "TEA-LEAN" and res["fill_most"] <= 6.00


def test_a_product_whose_floor_is_above_the_ceiling_is_left_out_with_a_reason():
    """A $7.50 unit cost needs at least $8.10 (with 8% tax), over the $6.50 ceiling: it can't be priced legally, so
    the optimisers leave it out and say why instead of crashing."""
    opt = module("optimize", DAYONE / "optimize.py")
    dear = product("TEA-DEAR", pack=1, price=7.00, most=7.50)
    cat = catalog(product("TEA-A"), dear)
    price, skipped = opt.prices(cat, ["TEA-A", "TEA-DEAR"])
    assert list(price) == ["TEA-A"]
    assert "TEA-DEAR" in skipped and "$8.10" in skipped["TEA-DEAR"] and "$6.50" in skipped["TEA-DEAR"]
    o = opt7()
    cat = catalog(product("TEA-A"), product("SODA-B", cat="sodacan", size="12 oz can", pack=8, price=4.00), dear)
    res = o.optimise(cat, [int(s) for s in TEST_SLOTS[:2]], 40_000, test_slot=False, say=lambda m: None)
    assert "TEA-DEAR" not in res["skus"] and "TEA-DEAR" in res["unpriceable"]


@pytest.mark.parametrize("script", ["compare7.py", "compare7b.py"])
def test_the_compare_scripts_document_writing_their_tables_to_the_ignored_folder(script):
    """Their output has per-slot rows: the usage line must send it into the git-ignored local folder."""
    import re
    import subprocess

    text = (DAYONE / "round7" / script).read_text(encoding="utf-8")
    usage = [line for line in text.split('"""')[1].splitlines() if script in line and ">" in line]
    assert usage, "a usage line with a redirect"
    for line in usage:
        target = re.search(r">\s*(\S+)", line).group(1)
        assert target.startswith("analysis/dayone/local/"), line
        ignored = subprocess.run(["git", "check-ignore", "-q", target], cwd=DAYONE.parents[1])
        assert ignored.returncode == 0, f"{target} is not git-ignored"
