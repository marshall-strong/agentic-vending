"""Tables for compare7.md: plan 6 (old rule, its own projection6.json), plan 6 re-scored under round 7's rule, and plan 7.

    python analysis/dayone/round7/compare7.py [--out analysis/dayone/round7/local/compare7_tables.md]

The tables have per-slot rows, so the script writes them itself, to round7/local/compare7_tables.md (git-ignored), and
refuses an --out outside round7/local/ or one git doesn't ignore. Stdout gets a summary with no slot in it.

Also writes projection6_rescored.json (plan 6 under the new project.py) so the re-score can be checked. The plans and
projections it reads and writes map our slots to products, so they live in the git-ignored local folder (README.md).
"""
from __future__ import annotations

import argparse
import functools
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import project as P  # noqa: E402

R6, R7 = P.LOCAL / "round6", P.LOCAL / "round7"   # the real round files: git-ignored
_ap = argparse.ArgumentParser(description="Write compare7's per-slot tables into the git-ignored round7/local/.")
_ap.add_argument("--out", help="the tables file, under analysis/dayone/round7/local/")
OUT = P.tables_path(_ap.parse_args().out, "compare7_tables.md")
_fh = open(OUT, "w", encoding="utf-8")
emit = functools.partial(print, file=_fh)   # every table line goes to the file, never to stdout


def load(p: Path) -> dict:
    return json.load(open(p, encoding="utf-8"))


# Round 6's catalog6.json is byte for byte the same as catalog7.json (same team snapshot, same script), so it isn't kept.
cat6 = {p["sku"]: p for p in load(HERE / "catalog7.json")["products"]}
cat7 = {p["sku"]: p for p in load(HERE / "catalog7.json")["products"]}
cal = P.load_calendar()
repo = os.environ.get("VENDING_REPO", P.DEFAULT_REPO)
plan6, proj6, plan7, proj7 = load(R6 / "plan6.json"), load(R6 / "projection6.json"), load(R7 / "plan7.json"), load(R7 / "projection7.json")
re6 = P.project(plan6, cat6, cal, "round6/plan6.json (re-scored, round 7 rule)", repo)
json.dump(re6, open(R7 / "projection6_rescored.json", "w", encoding="utf-8"), indent=1)


def money(x: float) -> str:
    return f"${x:,.2f}"


def name(cat: dict, sku: str) -> str:
    n, size = cat[sku]["name"], cat[sku]["size"]
    return n if size in n else f"{n} {size}"


s6 = {d["slot"]: d for d in plan6["day_one"]}
s7 = {d["slot"]: d for d in plan7["day_one"]}
b6 = {r["slot"]: r for r in proj6["scenarios"]["base"]["per_slot"]}
b6n = {r["slot"]: r for r in re6["scenarios"]["base"]["per_slot"]}
b7 = {r["slot"]: r for r in proj7["scenarios"]["base"]["per_slot"]}
skus6 = {d["sku"] for d in plan6["day_one"]}

emit("## Each slot's drink, and peak sales against capacity (base scenario)\n")
emit("Peak = the most units the slot sells between two Friday refills. Plan 6 is shown under both rules: the old rule "
      "gave each duplicate slot its own extra demand; the new rule splits the product's one demand across its slots. "
      "No slot in any of the three reaches its capacity in the base case.\n")
emit("| Slot | Plan 6 | Plan 6 peak (old rule) | Plan 6 peak (new rule) | Plan 7 | Plan 7 peak | Capacity (6 / 7) |")
emit("|---|---|---|---|---|---|---|")


# Class-day equivalents in the busiest refill cycle: the same for every slot that never sells out.
one = next(r for r in b6n.values() if r["product_slots"] == 1 and r["units_lost"] == 0)
F = one["product_peak_cycle_units"] / one["product_units_per_class_day"]


def cyc(row: dict) -> float:
    # projection6.json predates peak_cycle_units; nothing sold out there, so peak = its rate x F
    return row["units_per_class_day"] * F


for slot in sorted(s6):
    a, b = s6[slot], s7[slot]
    tag = " **(test slot)**" if b.get("role") == "test" else ""
    emit(f"| {slot} | {name(cat6, a['sku'])} | {cyc(b6[slot]):.1f} | {b6n[slot]['peak_cycle_units']:.1f} | "
          f"{name(cat7, b['sku'])}{tag} | {b7[slot]['peak_cycle_units']:.1f} | {a['units']} / {b['units']} |")

emit(f"\nThe busiest refill cycle holds {F:.1f} class days of demand. In the high scenario the busiest 8-can energy "
      "drinks (Celsius, Rockstar) sell exactly their 8 between refills, but none runs out.")
emit("\n## Totals\n")
d6, d7 = len({d['sku'] for d in plan6['day_one']}), len({d['sku'] for d in plan7['day_one']})
emit("| | Plan 6, old rule | Plan 6, re-scored | Plan 7 |")
emit("|---|---|---|---|")
emit(f"| Distinct drinks | {d6} | {d6} | {d7} |")
emit(f"| Fill cost, expected | {money(proj6['fill_expected'])} | {money(re6['fill_expected'])} | {money(proj7['fill_expected'])} |")
emit(f"| Fill cost, most we pay (cap $366) | {money(proj6['fill_most'])} | {money(re6['fill_most'])} | {money(proj7['fill_most'])} |")
emit(f"| Spendable after the fill, worst case ($600 less most we pay; floor $53) | {money(600 - proj6['fill_most'])} | "
      f"{money(600 - re6['fill_most'])} | {money(600 - proj7['fill_most'])} |")
for sc in ("low", "base", "high"):
    emit(f"| Projected profit, {sc} | {money(proj6['scenarios'][sc]['profit'])} | {money(re6['scenarios'][sc]['profit'])} | "
          f"{money(proj7['scenarios'][sc]['profit'])} |")
for sc in ("low", "base", "high"):
    emit(f"| Units lost to empty slots, {sc} | {proj6['scenarios'][sc]['units_lost']:.1f} | {re6['scenarios'][sc]['units_lost']:.1f} | "
          f"{proj7['scenarios'][sc]['units_lost']:.1f} |")
emit(f"| Closet stock after the fill (units) | {sum(p['closet'] for p in proj6['purchase'])} | {sum(p['closet'] for p in re6['purchase'])} | "
      f"{sum(p['closet'] for p in proj7['purchase'])} |")

emit("\n## How much of plan 6's profit was the DUP_DECAY credit\n")
emit("| Scenario | Plan 6, old rule | Plan 6, re-scored | Credit | Share |")
emit("|---|---|---|---|---|")
for sc in ("low", "base", "high"):
    o, n = proj6["scenarios"][sc]["profit"], re6["scenarios"][sc]["profit"]
    emit(f"| {sc} | {money(o)} | {money(n)} | {money(o - n)} | {100 * (o - n) / o:.0f}% |")

emit("\n## Drinks new to plan 7, and whether they have a checked-price sourcing row\n")
emit("| Drink | Slot | Price source in catalog7.json | Needs a sourcing row |")
emit("|---|---|---|---|")
for d in plan7["day_one"]:
    if d["sku"] in skus6:
        continue
    p = cat7[d["sku"]]
    has = p["src"].startswith("sourcing row")
    emit(f"| {name(cat7, d['sku'])} (`{d['sku']}`) | {d['slot']} | {p['src']} | {'no' if has else '**yes**'} |")
carried = [d["sku"] for d in plan7["day_one"] if d["sku"] in skus6 and not cat7[d["sku"]]["src"].startswith("sourcing row")]
if carried:
    emit(f"\nKept from plan 6 and still without a sourcing row: {', '.join(sorted(set(carried)))}.")
_fh.close()
print(f"wrote the compare7 tables to {OUT}")
print("base profit: plan 6 (old rule) " + money(proj6["scenarios"]["base"]["profit"]) + ", plan 6 re-scored "
      + money(re6["scenarios"]["base"]["profit"]) + ", plan 7 " + money(proj7["scenarios"]["base"]["profit"]))
