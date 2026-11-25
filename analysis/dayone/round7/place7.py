"""Place round 7's products on our slots (the server's assignment) by fall position data, as round 6 did.

    python analysis/dayone/round7/place7.py analysis/dayone/round7/catalog7.json analysis/dayone/local/round7/opt7b.json \
        analysis/dayone/local/round7/why7b.json analysis/dayone/local/round7/plan7b.json [--slots-from PLAN.json]

The opt, why and plan files map our slots to products, so they live in the git-ignored local folder (README.md).

Same method as place6.py:
- Positions, best first: shelf 1, shelf 2, then shelves 3-4 together, then shelf 5. Within that, the selection that
  sold more units in the fall (FALL40/sales_events.csv, all 40 selections, polls before the cut) comes first. Every
  row's time is checked against the fall cut first (analysis/slots_config.check_before_start): one row from the cut
  on stops the run, so no team's own sales can steer the placement (docs/integrity.md).
- Products, best first: by the base-scenario gross profit project.py gives each.
New in round 7:
- The test slot (role "test") keeps its place in that order, but never on a spot the wildcard may use, so the agent's
  wildcard pick and the weekly test never fight over one slot. If its turn lands on a wildcard spot, it takes the next
  position down the order that is not one, and the products after it move up one.
- The wildcard spots are read from the --slots-from plan's wildcard check (round 6's), not typed here.
The why file holds the one-line reason per product (keyed by SKU, since the slot is only known after placement), the
test slot's reason, and the plan's note, checks and catalog_requests.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import project as P  # noqa: E402

sys.path.insert(0, str(P.REPO / "analysis"))
from slots_config import check_before_start  # noqa: E402

# The fall export of all 40 selections, polls before the fall cut only (docs/integrity.md). Not committed:
# regenerate it with `python analysis/export_sales.py --fall-all analysis/data/fall40` (README.md).
FALL40 = Path(os.environ.get("FALL40_DIR") or P.REPO / "analysis" / "data" / "fall40")
SHELF_RANK = {1: 0, 2: 1, 3: 2, 4: 2, 5: 3}


def wildcard_spots(plan: dict) -> list[int]:
    text = next(c for c in plan["checks"] if c.startswith("Wildcard"))
    spots = re.search(r"Allowed spots[^:]*:\s*([\d,\s]+)", text).group(1)
    return [int(x) for x in re.findall(r"\d+", spots)]


def positions(slots: list[int], fall_dir: Path = FALL40) -> list[int]:
    """Our slots, best position first. Every row of the fall sales is checked against the fall cut before any is
    counted, with no exemptions: a row from the cut on stops the run (SystemExit)."""
    rows = list(csv.DictReader(open(Path(fall_dir) / "sales_events.csv", encoding="utf-8")))
    check_before_start(((r["slot"], datetime.fromisoformat(r["at_eastern"])) for r in rows), (),
                       "place7.py fall sales")
    sold = Counter()
    for r in rows:
        sold[int(r["slot"])] += int(r["units"])
    return sorted(slots, key=lambda s: (SHELF_RANK[s // 10], -sold[s], s))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("catalog"); ap.add_argument("opt"); ap.add_argument("why"); ap.add_argument("out")
    ap.add_argument("--slots-from", default=str(P.LOCAL / "round6" / "plan6.json"),
                    help="a plan made for our current assignment: its slots and its wildcard check are read")
    a = ap.parse_args()
    source = json.load(open(a.slots_from, encoding="utf-8"))
    slots = P.slots_from(a.slots_from)
    cat = {p["sku"]: p for p in json.load(open(a.catalog, encoding="utf-8"))["products"]}
    opt = json.load(open(a.opt, encoding="utf-8"))
    cal = P.load_calendar()
    repo = os.environ.get("VENDING_REPO", P.DEFAULT_REPO)
    price, skus, test = opt["price"], opt["skus"], opt["test_sku"]
    trial = {"day_one": [dict(slot=slots[i], sku=s, units=cat[s]["cap"], price=price[s]) for i, s in enumerate(skus)]}
    base = {row["slot"]: row["gross_profit"] for row in P.project(trial, cat, cal, "", repo)["scenarios"]["base"]["per_slot"]}
    cols = [s for _, s in sorted(((base[slots[i]], s) for i, s in enumerate(skus)), key=lambda t: -t[0])]
    pos, wild = positions(slots), set(wildcard_spots(source))
    k = cols.index(test)
    test_pos = next((p for p in pos[k:] if p not in wild), None) or next(p for p in reversed(pos[:k]) if p not in wild)
    others = [p for p in pos if p != test_pos]
    placed = sorted(list(zip(others, (s for s in cols if s != test))) + [(test_pos, test)])
    why = json.load(open(a.why, encoding="utf-8"))
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
    res = P.project(plan, cat, cal, "round7/" + Path(a.out).name, repo, opt.get("cap_cents", 36_600) / 100)
    plan["expect"] = round(res["scenarios"]["base"]["gross_profit"])
    json.dump(plan, open(a.out, "w", encoding="utf-8"), indent=1)
    for r in rows:
        print(r["slot"], r["sku"], r["price"], r.get("role", ""), "|", r["why"])
    print("base", res["scenarios"]["base"]["gross_profit"], "problems", res["problems"], "test slot", test_pos)


if __name__ == "__main__":
    main()
