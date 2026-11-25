"""The "7 vs 7b" section of compare7.md: plan 7 ($366 cap) against plan 7b ($400 cap, Kevin 22 Nov).

    python analysis/dayone/round7/compare7b.py [--out analysis/dayone/round7/local/compare7b_tables.md]

The tables have per-slot rows, so the script writes them itself, to round7/local/compare7b_tables.md (git-ignored),
and refuses an --out outside round7/local/ or one git doesn't ignore. Stdout gets a summary with no slot in it.
"""
from __future__ import annotations

import argparse
import functools
import json
from collections import Counter
from pathlib import Path

import sys as _sys
_sys.stdout.reconfigure(encoding="utf-8")   # the tables use arrows and minus signs; a Windows console would refuse them

HERE = Path(__file__).resolve().parent
_sys.path.insert(0, str(HERE.parent))
import project as P  # noqa: E402


def load(name: str) -> dict:
    """catalog7.json is committed here; the plans and projections map our slots to products, so they are read from
    the git-ignored local folder (README.md)."""
    folder = HERE if name.startswith("catalog") else P.LOCAL / "round7"
    return json.load(open(folder / name, encoding="utf-8"))


_ap = argparse.ArgumentParser(description="Write compare7b's per-slot tables into the git-ignored round7/local/.")
_ap.add_argument("--out", help="the tables file, under analysis/dayone/round7/local/")
OUT = P.tables_path(_ap.parse_args().out, "compare7b_tables.md")
cat = {p["sku"]: p for p in load("catalog7.json")["products"]}
p7, j7, p7b, j7b = load("plan7.json"), load("projection7.json"), load("plan7b.json"), load("projection7b.json")
BRAND = {"REDBULL": "Red Bull", "C4": "C4", "BUBLY": "bubly", "ARIZONA": "Arizona", "CELSIUS": "Celsius",
         "SNAPPLE": "Snapple", "HINT": "Hint", "GATORADE": "Gatorade", "POWERADE": "Powerade", "ROCKSTAR": "Rockstar"}


def brand(sku: str) -> str:
    b = sku.split("-")[0].split("_")[0]
    return BRAND.get(b, b.title())


def name(sku: str) -> str:
    n, size = cat[sku]["name"], cat[sku]["size"]
    return n if size in n else f"{n} {size}"


def m(x: float) -> str:
    return f"${x:,.2f}"


def d(x: float) -> str:
    return ("+" if x >= 0 else "−") + m(abs(x))


_fh = open(OUT, "w", encoding="utf-8")
emit = functools.partial(print, file=_fh)   # every table line goes to the file, never to stdout
a = {s["slot"]: s for s in p7["day_one"]}
b = {s["slot"]: s for s in p7b["day_one"]}
gp7 = {r["slot"]: r["gross_profit"] for r in j7["scenarios"]["base"]["per_slot"]}
gp7b = {r["slot"]: r["gross_profit"] for r in j7b["scenarios"]["base"]["per_slot"]}
emit("## 7 vs 7b: the fill cap raised from $366 to $400\n")
emit("Plan 7b is round 7 re-run with a $400 most-we-pay cap (Kevin, 22 Nov). Everything else is the same: the duplicate "
      "rule, the test slot, the brand lean, the catalog file and the $53 spendable floor.\n")
emit("### Each slot's drink\n")
emit("| Slot | Plan 7 ($366) | Base GP | Plan 7b ($400) | Base GP |")
emit("|---|---|---|---|---|")
for slot in sorted(a):
    x, y = a[slot], b[slot]
    tx = " **(test)**" if x.get("role") == "test" else ""
    ty = " **(test)**" if y.get("role") == "test" else ""
    emit(f"| {slot} | {name(x['sku'])}{tx} | {m(gp7[slot])} | {name(y['sku'])}{ty} | {m(gp7b[slot])} |")

s7, s7b = {s["sku"] for s in p7["day_one"]}, {s["sku"] for s in p7b["day_one"]}
emit(f"\nOut in 7b: {', '.join(name(s) for s in sorted(s7 - s7b))}.  ")
emit(f"In for 7b: {', '.join(name(s) for s in sorted(s7b - s7))}.  ")
emit("The rest are the same drinks; most moved slot because placement ranks drinks by value.\n")

emit("### Brands\n")
c7, c7b = Counter(brand(s) for s in s7), Counter(brand(s) for s in s7b)
emit("| Brand | Plan 7 | Plan 7b |")
emit("|---|---|---|")
for br in sorted(set(c7) | set(c7b), key=lambda k: (-(c7b[k] + c7[k]), k)):
    emit(f"| {br} | {c7[br]} | {c7b[br]} |")
emit(f"| **Brands** | **{len(c7)}** | **{len(c7b)}** |")
cc7, cc7b = Counter(cat[s]["cat"] for s in s7), Counter(cat[s]["cat"] for s in s7b)
emit("\nCategories: " + "; ".join(f"{k} {cc7[k]} → {cc7b[k]}" for k in sorted(set(cc7) | set(cc7b))) + ".\n")

emit("### Money\n")
emit("| | Plan 7 ($366) | Plan 7b ($400) | Change |")
emit("|---|---|---|---|")
emit(f"| Fill cost, expected | {m(j7['fill_expected'])} | {m(j7b['fill_expected'])} | {d(j7b['fill_expected'] - j7['fill_expected'])} |")
emit(f"| Fill cost, most we pay | {m(j7['fill_most'])} | {m(j7b['fill_most'])} | {d(j7b['fill_most'] - j7['fill_most'])} |")
emit(f"| Spendable after the fill, worst case ($600 less most we pay; floor $53) | {m(600 - j7['fill_most'])} | "
      f"{m(600 - j7b['fill_most'])} | {d(j7['fill_most'] - j7b['fill_most'])} |")
for sc in ("low", "base", "high"):
    x, y = j7["scenarios"][sc]["profit"], j7b["scenarios"][sc]["profit"]
    emit(f"| Projected profit, {sc} | {m(x)} | {m(y)} | {d(y - x)} |")
cl7, cl7b = sum(p["closet"] for p in j7["purchase"]), sum(p["closet"] for p in j7b["purchase"])
emit(f"| Closet stock after the fill (units, valued at cost) | {cl7} | {cl7b} | {cl7b - cl7:+d} |")

emit("\n### Drinks in 7b that need a checked-price sourcing row\n")
need = [s for s in sorted(s7b) if not cat[s]["src"].startswith("sourcing row")]
emit(f"{len(need)} drinks: " + ", ".join(name(s) + (" (new in 7b)" if s not in s7 else "") for s in need) + ".  ")
emit("Snapple Fruit Punch, new in 7b, already has one (Amazon, 15 Nov). Plan 7's list was also 14; 7b drops bubly "
      "Cherry and Snapple Peach Tea from it and adds C4 and Snapple Kiwi Strawberry.")
_fh.close()
print(f"wrote the compare7b tables to {OUT}")
print(f"base profit: plan 7 {m(j7['scenarios']['base']['profit'])}, plan 7b {m(j7b['scenarios']['base']['profit'])}")
