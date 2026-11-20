"""The round loader: pairing files, recomputing floors and fill cost, refusing other slots, reading
the projection as written. Pure: rounds are written to a temp folder, no database."""

from __future__ import annotations

import json
from fractions import Fraction

import pytest

from vending import plan_rounds as pr
from tests.conftest import TEST_SLOTS

RB = {"sku": "SIM-RB", "real_sku": "REDBULL-ORIGINAL-8_4CAN", "name": "Red Bull Original", "size": "8.4 oz can",
      "category": "energy", "units_per_pack": 24, "expected_cents": 3499, "max_cents": 4050, "column_capacity": 11,
      "capacity_assumed": False, "store": "Amazon"}
COKE = {"sku": "SIM-COKE", "real_sku": None, "name": "Coca-Cola", "size": "12 oz can", "category": "sodacan",
        "units_per_pack": 12, "expected_cents": 768, "max_cents": 900, "column_capacity": 8,
        "capacity_assumed": False, "store": "Amazon"}


def day_one(price_key: str = "price_cents", **over) -> list[dict]:
    """20 slots: Red Bull in the first 10, Coke in the rest."""
    rows = []
    for i, slot in enumerate(TEST_SLOTS):
        rb = i < 10
        row = {"slot": int(slot), "sku": "SIM-RB" if rb else "SIM-COKE", "units": 11 if rb else 8,
               "why": f"reason {slot}"}
        row[price_key] = (350 if rb else 200) if price_key == "price_cents" else (3.5 if rb else 2.0)
        row.update(over.get(slot, {}))
        rows.append(row)
    return rows


def projection(per_slot_gp: int = 1000, assumptions: dict | None = None) -> dict:
    def scen(mult: float) -> dict:
        per = [{"slot": int(s), "price_cents": 350 if i < 10 else 200, "units_per_class_day": 1.2 * mult,
                "class_days_to_sellout": 9.2, "sellout_date": None if mult < 1.2 else "2026-12-08",
                "margin_cents": 180, "gross_profit_cents": int(per_slot_gp * mult),
                "weekly": [{"week_start": "2026-12-03", "units": 5.0, "gp_cents": int(600 * mult)},
                           {"week_start": "2026-12-10", "units": 3.0, "gp_cents": int(400 * mult)}]}
               for i, s in enumerate(TEST_SLOTS)]
        return {"totals": {"units_sold": 100.0, "gross_profit_cents": int(20 * per_slot_gp * mult),
                           "model_spend_cents": 900, "profit_cents": int(20 * per_slot_gp * mult) - 900,
                           "week1_gp_cents": int(20 * 600 * mult)}, "per_slot": per}
    return {"schema": "vending-day1/projection/v1", "packs": [dict(RB), dict(COKE)],
            "slots": [{"slot": int(s), "sku": "SIM-RB" if i < 10 else "SIM-COKE", "interloper": i >= 10} for i, s in enumerate(TEST_SLOTS)],
            "fill_expected_cents": 4 * 3499 + 3 * 768, "fill_most_cents": 4 * 4050 + 3 * 900,
            "window": "2026-12-03 to 2027-01-29", "assumptions": assumptions or {"TIER_RATE": {"energy": 1.354}},
            "scenarios": {"low": scen(0.5), "base": scen(1.0), "high": scen(1.25)}}


def write(root, folder, rid, plan, proj=None):
    d = root / folder
    d.mkdir(exist_ok=True)
    (d / f"plan{rid}.json").write_text(json.dumps(plan), encoding="utf-8")
    if proj is not None:
        (d / f"projection{rid}.json").write_text(json.dumps(proj), encoding="utf-8")


def test_rounds_pair_by_suffix_in_round_order_and_ignore_other_files(tmp_path):
    write(tmp_path, "round2", "2b", {"day_one": day_one()}, projection())
    write(tmp_path, "round2", "2a", {"day_one": day_one()})
    write(tmp_path, "round10", "10", {"day_one": day_one()})
    write(tmp_path, "round1", "1", {"day_one": day_one()}, projection())
    (tmp_path / "round1" / "plan1_sku.json").write_text("{}", encoding="utf-8")
    (tmp_path / "round2" / "r2a.answer.json").write_text("{}", encoding="utf-8")
    write(tmp_path, "round4", "4", {"day_one": day_one()}, projection())
    folder = tmp_path / "round4"
    (folder / "plan4.json").rename(folder / "r4.plan.json")
    found = pr.discover(tmp_path)
    assert [rid for rid, _, _ in found] == ["1", "2a", "2b", "4", "10"]
    assert found[1][2] is None and found[2][2].name == "projection2b.json"
    assert found[3] == ("4", folder / "r4.plan.json", folder / "projection4.json")


def test_a_checked_plan_wins_over_the_raw_answer_beside_it(tmp_path):
    # Rounds 2 and 3 on the laptop keep both: planN.json (checked) and rN.plan.json (the model's answer).
    write(tmp_path, "round2", "2a", {"day_one": day_one()}, projection())
    (tmp_path / "round2" / "r2a.plan.json").write_text("{}", encoding="utf-8")
    write(tmp_path, "round3", "3", {"day_one": day_one()}, projection())
    (tmp_path / "round3" / "r3.plan.json").write_text("{}", encoding="utf-8")
    found = {rid: plan.name for rid, plan, _ in pr.discover(tmp_path)}
    assert found == {"2a": "plan2a.json", "3": "plan3.json"}


@pytest.mark.parametrize("first, second", [
    ("plan3.json", "r3.plan.json"),
    ("r3.plan.json", "plan3.json"),
], ids=["checked-first", "raw-first"])
def test_discover_refuses_the_same_round_in_different_folders(tmp_path, first, second):
    for folder, filename in (("round2", first), ("round3", second)):
        directory = tmp_path / folder
        directory.mkdir()
        (directory / filename).write_text("{}", encoding="utf-8")

    with pytest.raises(pr.RoundError, match="round 3 has two plan files") as error:
        pr.discover(tmp_path)
    assert f"round2/{first}" in str(error.value) and f"round3/{second}" in str(error.value)


def test_floor_and_fill_cost_are_recomputed_from_the_pack_list(tmp_path):
    write(tmp_path, "round1", "1", {"day_one": day_one()}, projection())
    r = pr.load_all(tmp_path, TEST_SLOTS)[0]
    rb, coke = r.slots["15"], r.slots["58"]
    assert rb.floor_cents == 185          # $40.50 / 24 x 1.08 = $1.8225, up to $1.85
    assert coke.floor_cents == 85         # $9.00 / 12 x 1.08 = $0.81, up to $0.85
    assert rb.most_cents == Fraction(11, 110) * 5 * 4050     # its share of 5 packs, closet included
    # 110 Red Bull = 5 packs of 24 (10 to the closet); 80 Coke = 7 packs of 12 (4 to the closet).
    buys = {p.sku: p for p in r.purchases}
    assert (buys["SIM-RB"].packs, buys["SIM-RB"].closet) == (5, 10)
    assert (buys["SIM-COKE"].packs, buys["SIM-COKE"].closet) == (7, 4)
    assert r.fill_most_cents == 5 * 4050 + 7 * 900
    assert r.fill_expected_cents == 5 * 3499 + 7 * 768
    # The projection's own totals disagree with that count, and the page says so.
    assert any("fill most-we-pay" in w for w in r.warnings)


def test_projection_figures_are_read_not_computed(tmp_path):
    write(tmp_path, "round1", "1", {"day_one": day_one()}, projection(per_slot_gp=1234))
    r = pr.load_all(tmp_path, TEST_SLOTS)[0]
    base = r.slots["15"].projected["base"]
    assert base.gross_profit_cents == 1234 and base.units_per_class_day == pytest.approx(1.2)
    assert r.slots["15"].projected["high"].sellout_date.isoformat() == "2026-12-08"
    assert r.totals["base"].gross_profit_cents == 20 * 1234
    assert r.weeks["base"][0][1] == 20 * 600       # the week's gp summed over our slots
    assert r.slots["58"].interloper is True and r.assumptions == {"TIER_RATE": {"energy": 1.354}}


def test_float_prices_are_read_as_exact_cents_and_bad_ones_refused(tmp_path):
    write(tmp_path, "round1", "1", {"day_one": day_one(price_key="price")}, projection())
    assert pr.load_all(tmp_path, TEST_SLOTS)[0].slots["15"].price_cents == 350
    write(tmp_path, "round1", "1", {"day_one": day_one(price_key="price", **{"15": {"price": 3.505}})})
    with pytest.raises(pr.RoundError, match="whole number of cents"):
        pr.load_all(tmp_path, TEST_SLOTS)


def test_a_slot_that_isnt_ours_is_refused(tmp_path):
    rows = day_one()
    rows[0]["slot"] = 11        # the other team's selection
    write(tmp_path, "round1", "1", {"day_one": rows})
    with pytest.raises(pr.RoundError, match="isn't in our current assignment"):
        pr.load_all(tmp_path, TEST_SLOTS)
    proj = projection()
    proj["scenarios"]["base"]["per_slot"][0]["slot"] = 14
    write(tmp_path, "round1", "1", {"day_one": day_one()}, proj)
    with pytest.raises(pr.RoundError, match="isn't in our current assignment"):
        pr.load_all(tmp_path, TEST_SLOTS)


def test_illegal_prices_and_overfull_columns_are_flagged(tmp_path):
    write(tmp_path, "round1", "1", {"day_one": day_one(**{"15": {"price_cents": 180}, "16": {"price_cents": 352},
                                                          "17": {"units": 12}, "18": {"price_cents": 700}})}, projection())
    r = pr.load_all(tmp_path, TEST_SLOTS)[0]
    assert any("below the floor" in m for m in r.slots["15"].refusals)
    assert any("multiple of 5 cents" in m for m in r.slots["16"].refusals)
    assert any("a column holds 11" in m for m in r.slots["17"].refusals)
    assert any("above the ceiling" in m for m in r.slots["18"].refusals)
    assert any("projection used $3.50" in w for w in r.warnings)


def test_changes_name_product_price_and_units(tmp_path):
    write(tmp_path, "round1", "1", {"day_one": day_one()}, projection())
    write(tmp_path, "round2", "2", {"day_one": day_one(**{"15": {"price_cents": 375},
                                                          "16": {"sku": "SIM-COKE", "units": 8, "price_cents": 200}})}, projection())
    one, two = pr.load_all(tmp_path, TEST_SLOTS)
    ch = pr.changes(two, one)
    assert set(ch) == {"15", "16"}
    assert ch["15"].labels == ("price",)
    assert ch["16"].labels == ("new product", "price", "units")
    assert pr.changes(one, None) == {}
    assert pr.same_assumptions(two, one)


def test_a_round_without_a_projection_has_no_cost_or_forecast(tmp_path):
    write(tmp_path, "round1", "1", {"day_one": day_one()})
    r = pr.load_all(tmp_path, TEST_SLOTS)[0]
    assert r.fill_most_cents is None and not r.projected and r.slots["15"].floor_cents is None
    assert not r.slots["15"].projected


def test_cents_fields_are_cents_whatever_their_json_type(tmp_path):
    proj = projection()
    proj["packs"][0]["max_cents"] = 4050.0           # a float that is a whole number of cents
    write(tmp_path, "round1", "1", {"day_one": day_one(**{"15": {"price_cents": "350"}})}, proj)
    r = pr.load_all(tmp_path, TEST_SLOTS)[0]
    assert r.slots["15"].price_cents == 350 and r.slots["15"].floor_cents == 185
    proj["packs"][0]["max_cents"] = 4050.5
    write(tmp_path, "round1", "1", {"day_one": day_one()}, proj)
    with pytest.raises(pr.RoundError, match="whole number of cents"):
        pr.load_all(tmp_path, TEST_SLOTS)


def test_units_or_sku_that_drift_from_the_projection_are_warned_about(tmp_path):
    proj = projection()
    proj["slots"][0]["fill"] = 11
    proj["slots"][0]["name"] = "Red Bull Original"
    write(tmp_path, "round1", "1", {"day_one": day_one(**{"15": {"units": 9}, "16": {"sku": "SIM-COKE", "units": 8}})}, proj)
    r = pr.load_all(tmp_path, TEST_SLOTS)[0]
    assert any("slot 15: the plan loads 9 units but the projection assumed 11" in w for w in r.warnings)
    assert any("slot 16: the plan has SIM-COKE but the projection has SIM-RB" in w for w in r.warnings)
    assert r.slots["16"].product == "Coca-Cola"       # from its own pack, not the projection's slot


def test_a_name_only_round_and_a_sku_round_compare_by_name(tmp_path):
    named = [{"slot": int(s), "product": "Red Bull Original" if i < 10 else "Coca-Cola", "size": "8.4 oz can",
              "units": 11 if i < 10 else 8, "price": 3.5 if i < 10 else 2.0} for i, s in enumerate(TEST_SLOTS)]
    write(tmp_path, "round1", "1", {"day_one": named})
    proj = projection()
    proj["packs"][1]["name"] = "Coca-Cola"
    write(tmp_path, "round2", "2", {"day_one": day_one()}, proj)
    one, two = pr.load_all(tmp_path, TEST_SLOTS)
    assert pr.changes(two, one) == {}


def test_slot_cost_shares_add_up_to_the_fill(tmp_path):
    write(tmp_path, "round1", "1", {"day_one": day_one()}, projection())
    r = pr.load_all(tmp_path, TEST_SLOTS)[0]
    assert sum(s.most_cents for s in r.slots.values()) == r.fill_most_cents
    assert sum(s.expected_cents for s in r.slots.values()) == r.fill_expected_cents


def test_an_off_chart_size_is_assumed_only_when_the_projection_says_so(tmp_path):
    proj = projection()
    proj["packs"][1].update(size="16.9 oz bottle", column_capacity=8, capacity_assumed=False)
    write(tmp_path, "round1", "1", {"day_one": day_one()}, proj)
    assert pr.load_all(tmp_path, TEST_SLOTS)[0].slots["58"].capacity_assumed is False
    proj["packs"][1]["capacity_assumed"] = True
    write(tmp_path, "round1", "1", {"day_one": day_one()}, proj)
    assert pr.load_all(tmp_path, TEST_SLOTS)[0].slots["58"].capacity_assumed is True


def test_a_malformed_file_is_a_round_error_not_a_crash(tmp_path):
    proj = projection()
    proj["scenarios"]["base"]["per_slot"][0]["weekly"] = ["not a dict"]
    write(tmp_path, "round1", "1", {"day_one": day_one()}, proj)
    with pytest.raises(pr.RoundError, match="unexpected shape"):
        pr.load_all(tmp_path, TEST_SLOTS)
    write(tmp_path, "round1", "1", {"day_one": ["not a dict"]})
    with pytest.raises(pr.RoundError, match="unexpected shape"):
        pr.load_all(tmp_path, TEST_SLOTS)


def test_fallbacks_may_only_name_our_slots(tmp_path):
    write(tmp_path, "round1", "1", {"day_one": day_one(), "fallbacks": [{"slot": "38, 45, 46", "if": "x", "then": "y"},
                                                                       {"slot": "all requested slots", "if": "x", "then": "y"}]})
    pr.load_all(tmp_path, TEST_SLOTS)
    write(tmp_path, "round1", "1", {"day_one": day_one(), "fallbacks": [{"slot": "38, 44", "if": "x", "then": "y"}]})
    with pytest.raises(pr.RoundError, match="slot 44 isn't in our current assignment"):
        pr.load_all(tmp_path, TEST_SLOTS)


def test_a_fallback_range_checks_every_selection_inside_it(tmp_path):
    """"15-17" names 16 too, so a range whose ends are ours can't slip in a selection that isn't."""
    ours = tuple(s for s in TEST_SLOTS if s != "16") + ("11",)
    rows = [r for r in day_one() if str(r["slot"]) != "16"]
    for text in ("15-17", "15 to 17", "15\u201317", "17-15"):
        write(tmp_path, "round1", "1", {"day_one": rows, "fallbacks": [{"slot": text, "if": "x", "then": "y"}]})
        with pytest.raises(pr.RoundError, match="slot 16 isn't in our current assignment"):
            pr.load_all(tmp_path, ours)
    write(tmp_path, "round1", "1", {"day_one": day_one(), "fallbacks": [{"slot": "18-25", "if": "x", "then": "y"}]})
    with pytest.raises(pr.RoundError, match="slot 21 isn't"):     # 19 and 20 aren't selections; 21 is the first
        pr.load_all(tmp_path, TEST_SLOTS)
    write(tmp_path, "round1", "1", {"day_one": day_one(), "fallbacks": [{"slot": "15-18, 25 to 28", "if": "x", "then": "y"}]})
    pr.load_all(tmp_path, TEST_SLOTS)
