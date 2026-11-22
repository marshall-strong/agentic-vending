"""Project a day-one plan forward: units, sell-out, gross profit, per slot, per week, under three scenarios.

    python analysis/dayone/project.py <plan.json> <catalog.json> <out.json>

A planning tool for the day-one fill, not package code (see README.md here). Every rate below is an ASSUMPTION, written into
the output so a reader sees what the numbers rest on. Floors and capacities are checked with the
repo's own vending.pricing / vending.capacity.

Model:
- Each slot sells TIER_RATE[category] units per class day (our slots' in-stock segment rates,
  analysis/REPORT.md "Our slots": fast 1.701, mid 1.354, slow 0.767 / 0.722), times
  (price / REF_PRICE[category]) ** (-2 above the reference, -0.5 below; clamped), times the scenario's demand multiplier and
  Interloper haircut (an extra haircut for products the Interloper also sells).
- Duplicates add capacity only, never demand (round 7; Kevin, 22 Nov, issue #230). A product's demand is modelled
  once, however many slots hold it: its slots pool their stock, the product sells min(demand, stock), and the
  sales are split across its slots by capacity. A second slot only adds sales when the product's demand between
  Friday refills is more than one slot holds. Until round 6 a second slot of the same SKU was credited with
  DUP_DECAY = 0.75 of the first slot's sales as new demand; that credit is gone (project_v4.py keeps the old rule).
- A category past the product count history shows (energy 9, others 4) loses CAT_DECAY per extra product (flavours
  cannibalise). Counted per distinct product since round 7: a duplicate slot adds no flavour, so it doesn't count.
- Non-class days sell nothing (weekends, breaks; analysis/golisano_calendar_2026_27.json).
- The machine is loaded to fill_to before Thu 3 Dec. Every Friday from FIRST_RESTOCK the worker
  refills to fill_to at ~3 PM; half of Friday's demand comes before the visit.
- Gross profit = units sold x (price / 1.08 - expected unit cost). Unsold stock is valued at cost
  (confirmed 14 Nov), so leftover stock is neither profit nor loss. Minus model spend.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date, timedelta
from pathlib import Path

TIER_RATE = {"energy": 1.354, "soda20": 1.701, "sodacan": 0.767, "sparkling": 0.722, "water": 1.0,
             "juice": 1.0, "protein": 0.722, "coffee": 0.767, "tea": 0.767, "sports": 0.767}
REF_PRICE = {"energy": 4.09, "soda20": 2.35, "sodacan": 1.50, "sparkling": 1.62, "water": 2.67,
             "juice": 3.15, "protein": 3.68, "coffee": 3.31, "tea": 2.33, "sports": 3.21}
ELASTICITY_UP = -2.0     # above the reference price shoppers walk away fast
ELASTICITY_DOWN = -0.5   # below it they buy only a little more
CLAMP = (0.1, 1.6)      # was 0.4: that floor made $6.50 sodas look profitable (flagged by the round 2a model)
# Category saturation: history only shows a category up to this many of our slots (energy held 15-18, 25-28, 38).
# Beyond that, each extra slot in the category sells CAT_DECAY ** (slots past the limit) as much. ASSUMPTION.
CAT_FREE = {"energy": 9}
CAT_FREE_DEFAULT = 4
CAT_DECAY = 0.6
# DUP_DECAY (0.75) was removed in round 7: a duplicate slot adds capacity, not demand.
SCENARIOS = {"low": (0.6, 0.30, 0.30), "base": (1.0, 0.15, 0.15), "high": (1.25, 0.05, 0.05)}  # demand x, haircut, extra haircut on overlap
START, END = date(2026, 12, 3), date(2027, 1, 29)
FIRST_RESTOCK = date(2026, 12, 4)
MODEL_SPEND_PER_WEEK = 1.00


def most(p: dict) -> float:
    return p.get("max") or math.ceil(p["price"] * 1.15 * 2 - 1e-9) / 2


# This repo: its `vending` package checks floors and capacities, and its calendar sets the class days.
REPO = Path(__file__).resolve().parents[2]
DEFAULT_REPO = str(REPO)
CALENDAR = REPO / "analysis" / "golisano_calendar_2026_27.json"
# The day-one fill's most-we-pay cap, for all 20 slots: the figure proposed in #113 (docs/opening-capital-plan.md).
# That doc now splits it as $360 for 19 regular slots plus the wildcard's own $82 line (#110); see README.md.
FILL_CAP = 366.00
FILL_CAP_CENTS = 36_600


def load_calendar(path: Path | str = CALENDAR) -> dict[str, bool]:
    """ISO date -> whether it is a class day."""
    return {d["date"]: d["classes"] == "normal" for d in json.load(open(path, encoding="utf-8"))["days"]}


def slots_from(plan_path: Path | str) -> list[int]:
    """Our slots, as a plan file placed them. The plan was made for the server's assignment
    (`vending.slots.our_slots`); `vending.plan_rounds` refuses a plan naming any other selection."""
    return sorted(int(d["slot"]) for d in json.load(open(plan_path, encoding="utf-8"))["day_one"])


def project(plan: dict, cat: dict, cal: dict, plan_name: str = "", repo: str = DEFAULT_REPO) -> dict:
    """Project a plan in memory. cat: sku -> product; cal: ISO date -> is a class day."""
    if repo not in sys.path:
        sys.path.insert(0, repo)
    from vending.capacity import from_size
    from vending.pricing import check_price, pack_floor_cents
    a = argparse.Namespace(plan=plan_name)
    problems, slots, seen = [], [], {}
    for s in plan["day_one"]:
        p = cat.get(s["sku"])
        if p is None:
            problems.append(f"{s['slot']}: {s['sku']} is not in the sim catalog"); continue
        chart = from_size(p["size"])
        if chart is not None and chart != p["cap"]:
            problems.append(f"{p['sku']}: catalog cap {p['cap']} but chart says {chart}")
        if s["units"] != p["cap"]:
            problems.append(f"{s['slot']}: units {s['units']} but the slot holds {p['cap']} (fill to capacity)")
        floor = pack_floor_cents(round(most(p) * 100), p["pack"])
        bad = check_price(round(s["price"] * 100), floor, 650)
        if bad:
            problems.append(f"{s['slot']}: price {s['price']:.2f} refused: {bad}")
        k = seen.get(p["sku"], 0); seen[p["sku"]] = k + 1
        if k == 0:   # category saturation counts distinct products, not slots
            kc = seen.get("cat:" + p["cat"], 0); seen["cat:" + p["cat"]] = kc + 1
            seen["past:" + p["sku"]] = max(0, kc + 1 - CAT_FREE.get(p["cat"], CAT_FREE_DEFAULT))
        past = seen["past:" + p["sku"]]
        slots.append(dict(slot=s["slot"], sku=p["sku"], name=p["name"], size=p["size"], cat=p["cat"], fill=p["cap"],
                          price=s["price"], floor=floor / 100, unit_exp=p["price"] / p["pack"], unit_max=most(p) / p["pack"],
                          dup=k, cat_past=past, interloper=bool(p.get("interloper")), assumed_cap=bool(p.get("capacity_assumed")),
                          orderable_now=p.get("orderable_now", True), store=p["store"]))
    if len({s["slot"] for s in slots}) != len(slots):
        problems.append("a slot appears twice")
    groups: dict[str, list[dict]] = {}
    for s in slots:
        groups.setdefault(s["sku"], []).append(s)
    for sku, g in groups.items():
        if len({x["price"] for x in g}) > 1:
            problems.append(f"{sku}: its slots have different prices; one product has one demand and one price")

    # day-one purchase: whole packs per SKU
    need = {}
    for s in slots:
        need[s["sku"]] = need.get(s["sku"], 0) + s["fill"]
    buy = []
    for sku, u in need.items():
        p = cat[sku]; packs = math.ceil(u / p["pack"])
        buy.append(dict(sku=sku, units=u, packs=packs, bought=packs * p["pack"], closet=packs * p["pack"] - u,
                        expected=round(packs * p["price"], 2), most=round(packs * most(p), 2)))
    fill_exp = round(sum(b["expected"] for b in buy), 2); fill_most = round(sum(b["most"] for b in buy), 2)

    days = [START + timedelta(n) for n in range((END - START).days + 1)]
    out_sc = {}
    for name, (mult, hair, extra) in SCENARIOS.items():
        rows, weeks = {}, {}
        for sku, g in groups.items():
            s0 = g[0]
            ratio = s0["price"] / REF_PRICE[s0["cat"]]
            m = min(max(ratio ** (ELASTICITY_UP if ratio > 1 else ELASTICITY_DOWN), CLAMP[0]), CLAMP[1])
            # One demand per product, whatever its slot count; its slots pool their stock.
            r = TIER_RATE[s0["cat"]] * m * mult * (1 - hair) * ((1 - extra) if s0["interloper"] else 1) * CAT_DECAY ** s0["cat_past"]
            cap_total = sum(x["fill"] for x in g)
            stock, sold, lost, sellouts = float(cap_total), 0.0, 0.0, []
            wk_sold, cyc_sold, cyc = {}, {}, 0
            for d in days:
                wk = (d - START).days // 7 + 1
                dem = r if cal.get(d.isoformat()) else 0.0
                visit = d.weekday() == 4 and d >= FIRST_RESTOCK
                parts = [dem / 2, dem / 2] if visit else [dem]
                for i, part in enumerate(parts):
                    if i == 1:   # Friday visit between the halves
                        stock = float(cap_total); cyc += 1
                    take = min(stock, part)
                    if part > 0 and take < part and stock > 0:
                        sellouts.append(d.isoformat())
                    stock -= take; sold += take; lost += part - take
                    wk_sold[wk] = wk_sold.get(wk, 0) + take
                    cyc_sold[cyc] = cyc_sold.get(cyc, 0) + take
                if visit and dem == 0:   # visit on a no-class Friday
                    stock = float(cap_total); cyc += 1
            first_out = sellouts[0] if sellouts else None
            for s in g:
                share = s["fill"] / cap_total   # the product's sales split across its slots by capacity
                margin = s["price"] / 1.08 - s["unit_exp"]
                ws = {w: u * share for w, u in wk_sold.items()}
                wk_list = [dict(week_start=str(START + timedelta(7 * (w - 1))), units=round(u, 2), gp_cents=round(u * margin * 100)) for w, u in sorted(ws.items())]
                ss, sl, rs = sold * share, lost * share, r * share
                rows[s["slot"]] = dict(slot=s["slot"], sku=s["sku"], price_cents=round(s["price"] * 100), weekly=wk_list, units_per_class_day=round(rs, 3),
                                       class_days_to_sellout=round(cap_total / r, 1) if r else None, sellout_date=first_out,
                                       margin_cents=round(margin * 100), gross_profit_cents=round(ss * margin * 100), rate_per_class_day=round(rs, 3), class_days_of_cover=round(cap_total / r, 1) if r else None,
                                       first_sellout=first_out, sellout_days=len(set(sellouts)), units_sold=round(ss, 1), units_lost=round(sl, 1),
                                       margin_per_unit=round(margin, 2), gross_profit=round(ss * margin, 2),
                                       week1_units=round(ws.get(1, 0), 1), week1_gp=round(ws.get(1, 0) * margin, 2),
                                       week2_units=round(ws.get(2, 0), 1), week2_gp=round(ws.get(2, 0) * margin, 2),
                                       peak_week_units=round(max(ws.values(), default=0), 2),
                                       peak_cycle_units=round(max(cyc_sold.values(), default=0) * share, 2), capacity=s["fill"],
                                       product_slots=len(g), product_capacity=cap_total, product_units_per_class_day=round(r, 3),
                                       product_peak_cycle_units=round(max(cyc_sold.values(), default=0), 2))
                for w, u in ws.items():
                    weeks[w] = weeks.get(w, 0) + u * margin
        per_slot = [rows[s["slot"]] for s in slots]
        n_weeks = math.ceil(len(days) / 7)
        gp = sum(p["gross_profit"] for p in per_slot)
        totals = dict(units_sold=round(sum(p["units_sold"] for p in per_slot), 1), gross_profit_cents=round(gp * 100),
                      model_spend_cents=round(n_weeks * MODEL_SPEND_PER_WEEK * 100), profit_cents=round((gp - n_weeks * MODEL_SPEND_PER_WEEK) * 100),
                      week1_gp_cents=round(sum(p["week1_gp"] for p in per_slot) * 100))
        out_sc[name] = dict(totals=totals, per_slot=per_slot, weekly_gp={w: round(v, 2) for w, v in sorted(weeks.items())},
                            units_sold=round(sum(p["units_sold"] for p in per_slot), 1), units_lost=round(sum(p["units_lost"] for p in per_slot), 1),
                            gross_profit=round(gp, 2), model_spend=round(n_weeks * MODEL_SPEND_PER_WEEK, 2),
                            profit=round(gp - n_weeks * MODEL_SPEND_PER_WEEK, 2),
                            week1_gp=round(sum(p["week1_gp"] for p in per_slot), 2), week2_gp=round(sum(p["week2_gp"] for p in per_slot), 2),
                            slots_selling_out_week1=sorted(p["slot"] for p in per_slot if p["first_sellout"] and p["first_sellout"] < "2026-12-10"))
    packs = [dict(sku=p["sku"], real_sku=p.get("real_sku"), name=p["name"], size=p["size"], category=p["cat"], units_per_pack=p["pack"],
                  expected_cents=round(p["price"] * 100), max_cents=round(most(p) * 100), column_capacity=p["cap"],
                  capacity_assumed=bool(p.get("capacity_assumed")), store=p["store"]) for p in cat.values() if p["sku"] in need]
    res = dict(schema="vending-day1/projection/v1", money="integer cents in *_cents fields; float dollar fields are kept for older readers",
               plan=a.plan, packs=packs, fill_expected_cents=round(fill_exp * 100), fill_most_cents=round(fill_most * 100), window=f"{START} to {END} (calendar file ends 31 Jan)", slots=slots, purchase=buy,
               fill_expected=fill_exp, fill_most=fill_most, under_cap=fill_most <= FILL_CAP, problems=problems,
               needs_hand_count=sorted({s["sku"] for s in slots if s["assumed_cap"]}),
               needs_new_source=sorted({s["sku"] for s in slots if not s["orderable_now"]}),
               assumptions=dict(TIER_RATE=TIER_RATE, REF_PRICE=REF_PRICE, ELASTICITY_UP=ELASTICITY_UP, ELASTICITY_DOWN=ELASTICITY_DOWN, CLAMP=CLAMP, CAT_FREE=CAT_FREE, CAT_FREE_DEFAULT=CAT_FREE_DEFAULT, CAT_DECAY=CAT_DECAY,
                                SCENARIOS={k: dict(demand=v[0], interloper_haircut=v[1], overlap_extra=v[2]) for k, v in SCENARIOS.items()},
                                DUPLICATES="capacity only, never demand (round 7, #230): a product's demand is modelled once; its slots pool "
                                           "their stock and split its sales by capacity, so a second slot adds sales only when the product's "
                                           "demand between Friday refills is more than one slot holds. Replaces DUP_DECAY=0.75 (rounds 2-6)",
                                CATEGORY_COUNT="CAT_FREE/CAT_DECAY count distinct products in a category, not slots (round 7)",
                                FIRST_RESTOCK=str(FIRST_RESTOCK), refill="to fill_to every Friday ~3 PM; half of Friday's demand before",
                                profit="units sold x (price/1.08 - expected unit cost); unsold stock at cost; minus $1/week model spend; no midweek visits"),
               scenarios=out_sc)
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("plan"); ap.add_argument("catalog"); ap.add_argument("out")
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--calendar", default=str(CALENDAR))
    a = ap.parse_args()
    cat = {p["sku"]: p for p in json.load(open(a.catalog, encoding="utf-8"))["products"]}
    plan = json.load(open(a.plan, encoding="utf-8"))
    cal = load_calendar(a.calendar)
    res = project(plan, cat, cal, a.plan, a.repo)
    out_sc, fill_exp, fill_most, problems = res["scenarios"], res["fill_expected"], res["fill_most"], res["problems"]
    json.dump(res, open(a.out, "w", encoding="utf-8"), indent=1)
    b = out_sc["base"]
    print(f"fill ${fill_exp:.2f} expected / ${fill_most:.2f} most (cap ${FILL_CAP:.0f}: {'ok' if fill_most <= FILL_CAP else 'OVER'}); problems: {problems or 'none'}")
    for n, sc in out_sc.items():
        print(f"{n:5s}: units {sc['units_sold']:7.1f} lost {sc['units_lost']:6.1f}  GP ${sc['gross_profit']:8.2f}  profit ${sc['profit']:8.2f}  "
              f"wk1 ${sc['week1_gp']:6.2f} wk2 ${sc['week2_gp']:6.2f}  sell out in wk1: {len(sc['slots_selling_out_week1'])}")
    print("hand count needed:", res["needs_hand_count"] or "none", "| new source needed:", res["needs_new_source"] or "none")


if __name__ == "__main__":
    main()
