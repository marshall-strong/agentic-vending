"""Place round 7's 20 products on our slots (the server's assignment round 6 used) by fall position data, as round 6 did.

    python analysis/dayone/round7/place7.py analysis/dayone/round7/catalog7.json analysis/dayone/round7/opt7.json
        analysis/dayone/round7/why7.json analysis/dayone/round7/plan7.json

Same method as place6.py:
- Positions, best first: shelf 1, shelf 2, then shelves 3-4 together, then shelf 5. Within that, the selection that
  sold more units in the fall (FALL40/sales_events.csv, all 40 selections, polls before the cut) comes first.
- Products, best first: by the base-scenario gross profit project.py gives each.
New in round 7:
- The test slot (role "test") keeps its place in that order, but never on a spot the wildcard may use, so the agent's
  wildcard pick and the weekly test never fight over one slot. If its turn lands on a wildcard spot, it takes the next
  position down the order that is not one, and the products after it move up one.
- The wildcard spots are read from plan 6's wildcard check, not typed here.
why7.json holds the one-line reason per slot (keyed by SKU, since the slot is only known after placement), the
test slot's reason, and the plan's note, checks and catalog_requests.
"""
from __future__ import annotations

import csv
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import project as P  # noqa: E402

PLAN6 = json.load(open(HERE.parent / "round6" / "plan6.json", encoding="utf-8"))
SLOTS = P.slots_from(HERE.parent / "round6" / "plan6.json")
# The fall export of all 40 selections, polls before the fall cut only (docs/integrity.md). Not committed:
# regenerate it with `python analysis/export_sales.py --fall-all analysis/data/fall40` (README.md).
FALL40 = Path(os.environ.get("FALL40_DIR", P.REPO / "analysis" / "data" / "fall40"))
SHELF_RANK = {1: 0, 2: 1, 3: 2, 4: 2, 5: 3}


def wildcard_spots() -> list[int]:
    text = next(c for c in PLAN6["checks"] if c.startswith("Wildcard"))
    spots = re.search(r"Allowed spots[^:]*:\s*([\d,\s]+)", text).group(1)
    return [int(x) for x in re.findall(r"\d+", spots)]


def positions() -> list[int]:
    sold = Counter()
    for r in csv.DictReader(open(FALL40 / "sales_events.csv", encoding="utf-8")):
        sold[int(r["slot"])] += int(r["units"])
    return sorted(SLOTS, key=lambda s: (SHELF_RANK[s // 10], -sold[s], s))


def main() -> None:
    cat_path, opt_path, why_path, out_path = sys.argv[1:5]
    cat = {p["sku"]: p for p in json.load(open(cat_path, encoding="utf-8"))["products"]}
    opt = json.load(open(opt_path, encoding="utf-8"))
    cal = P.load_calendar()
    repo = os.environ.get("VENDING_REPO", P.DEFAULT_REPO)
    price, skus, test = opt["price"], opt["skus"], opt["test_sku"]
    trial = {"day_one": [dict(slot=SLOTS[i], sku=s, units=cat[s]["cap"], price=price[s]) for i, s in enumerate(skus)]}
    base = {row["slot"]: row["gross_profit"] for row in P.project(trial, cat, cal, "", repo)["scenarios"]["base"]["per_slot"]}
    cols = [s for _, s in sorted(((base[SLOTS[i]], s) for i, s in enumerate(skus)), key=lambda t: -t[0])]
    pos, wild = positions(), set(wildcard_spots())
    k = cols.index(test)
    test_pos = next((p for p in pos[k:] if p not in wild), None) or next(p for p in reversed(pos[:k]) if p not in wild)
    others = [p for p in pos if p != test_pos]
    placed = sorted(list(zip(others, (s for s in cols if s != test))) + [(test_pos, test)])
    why = json.load(open(why_path, encoding="utf-8"))
    rows = []
    for slot, sku in placed:
        row = dict(slot=slot, sku=sku, units=cat[sku]["cap"], price=price[sku], price_cents=round(price[sku] * 100),
                   why=why["why"].get(sku, ""))
        if sku == test:
            row.update(role="test", role_reason=why["test_reason"].replace("{test_slot}", str(test_pos)))
        rows.append(row)
    checks = [c.replace("{test_slot}", str(test_pos)).replace("{wild_spots}", ", ".join(str(s) for s in sorted(wild)))
              for c in why["checks"]]
    plan = {"note": why["note"], "day_one": rows, "catalog_requests": why["catalog_requests"], "checks": checks}
    # opt7.json predates the cap option: plan 7 was made at $366.
    res = P.project(plan, cat, cal, "round7/" + Path(out_path).name, repo, opt.get("cap_cents", 36_600) / 100)
    plan["expect"] = round(res["scenarios"]["base"]["gross_profit"])
    json.dump(plan, open(out_path, "w", encoding="utf-8"), indent=1)
    for r in rows:
        print(r["slot"], r["sku"], r["price"], r.get("role", ""), "|", r["why"])
    print("base", res["scenarios"]["base"]["gross_profit"], "problems", res["problems"], "test slot", test_pos)


if __name__ == "__main__":
    main()
