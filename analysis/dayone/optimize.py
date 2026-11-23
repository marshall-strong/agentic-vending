"""Best day-one plan under project.py's model:

    python analysis/dayone/optimize.py <catalog.json> <out_plan.json> --slots-from PLAN.json [--exclude SKU ...] [--only-checked]

Prices: per SKU, the 5-cent price from the floor to $6.50 that maximises margin x demand multiplier.
Slots: our slots as PLAN.json placed them (no list is typed here), filled greedily by marginal base gross
profit, then single-slot swaps until no gain, always keeping the most-we-pay fill at or under the cap
(project.FILL_CAP). Every slot gets a different drink (day one: Kevin, 22 Nov, #230). Only products with a price
estimate are candidates; --only-checked keeps those with a checked-price sourcing row. A planning aid, not package
code. Round 7 imports candidates and prices from here.
"""
from __future__ import annotations
import argparse, json, math, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import project as P  # noqa: E402

CAP_CENTS = P.FILL_CAP_CENTS
CEILING_CENTS = 650     # every regular slot's price ceiling (config.price_ceiling_cents, 12 Nov)


def best_price(p: dict, floor_cents: int) -> float | None:
    """The 5-cent price from the floor to the ceiling that maximises margin x demand; None if the floor is above it."""
    unit = p["price"] / p["pack"]; ref = P.REF_PRICE[p["cat"]]; best = None
    for c in range(max(floor_cents, 5), CEILING_CENTS + 1, 5):
        x = c / 100; ratio = x / ref
        m = min(max(ratio ** (P.ELASTICITY_UP if ratio > 1 else P.ELASTICITY_DOWN), P.CLAMP[0]), P.CLAMP[1])
        v = (x / 1.08 - unit) * m
        if best is None or v > best[0]:
            best = (v, x)
    return None if best is None else best[1]


def prices(cat: dict, pool: list[str]) -> tuple[dict[str, float], dict[str, str]]:
    """Each candidate's best price, and the ones that can't be priced legally, with why: a floor (most-we-pay unit
    cost with tax) above the ceiling leaves no allowed price, so the optimisers leave the product out."""
    sys.path.insert(0, P.DEFAULT_REPO)
    from vending.pricing import pack_floor_cents
    price, skipped = {}, {}
    for s in pool:
        floor = pack_floor_cents(round(P.most(cat[s]) * 100), cat[s]["pack"])
        best = best_price(cat[s], floor)
        if best is None:
            skipped[s] = (f"its floor is ${floor / 100:.2f} (most-we-pay unit cost with tax), above the "
                          f"${CEILING_CENTS / 100:.2f} ceiling, so no price is allowed")
        else:
            price[s] = best
    return price, skipped


def candidates(cat: dict, exclude=(), only_checked: bool = False) -> list[str]:
    """SKUs an optimiser may place, in catalog order: a counted capacity, a price estimate (an unpriced pack would
    look free), a category the model rates (one without has no demand rate or reference price), not left out on
    purpose (plannable), and with only_checked a checked-price sourcing row too."""
    return [s for s, p in cat.items() if p.get("cap") and not p.get("capacity_assumed") and s not in exclude
            and p.get("cat") in P.TIER_RATE
            and P.priced(p) and p.get("plannable", True) and (not only_checked or P.checked_source(p))]


def main() -> None:
    ap = argparse.ArgumentParser(); ap.add_argument("catalog"); ap.add_argument("out")
    ap.add_argument("--exclude", nargs="*", default=[]); ap.add_argument("--only-checked", action="store_true")
    ap.add_argument("--slots-from", required=True, help="a plan JSON for our current slots (its day_one list)")
    ap.add_argument("--quiet", action="store_true"); ap.add_argument("--seed", nargs="*", default=[], help="plan JSONs to start swaps from")
    a = ap.parse_args()
    SLOTS = P.slots_from(a.slots_from)
    cat = {p["sku"]: p for p in json.load(open(a.catalog, encoding="utf-8"))["products"]}
    cal = P.load_calendar()
    price, skipped = prices(cat, candidates(cat, a.exclude, a.only_checked))
    for s, why in skipped.items():
        print(f"left out {s}: {why}")
    pool = list(price)

    def score(skus: list[str]) -> tuple[float, dict]:
        plan = {"day_one": [dict(slot=int(SLOTS[i]), sku=s, units=cat[s]["cap"], price=price[s]) for i, s in enumerate(skus)]}
        r = P.project(plan, cat, cal)
        if r["problems"] or round(r["fill_most"] * 100) > CAP_CENTS or len(set(skus)) < len(skus):
            return -1e9, r
        return r["scenarios"]["base"]["gross_profit"], r

    chosen: list[str] = []
    for _ in SLOTS:
        best = max(((score(chosen + [s])[0], s) for s in pool), default=(-1e9, None))
        if best[0] <= -1e8:
            break
        chosen.append(best[1])
    def polish(chosen):
        cur = score(chosen)[0]; improved = True
        while improved:
            improved = False
            for i in range(len(chosen)):
                for s in pool:
                    if s == chosen[i]:
                        continue
                    trial = chosen[:i] + [s] + chosen[i + 1:]
                    trial.sort(key=lambda x: -(price[x] / 1.08 - cat[x]["price"] / cat[x]["pack"]) * P.TIER_RATE[cat[x]["cat"]])
                    v = score(trial)[0]
                    if v > cur + 0.01:
                        chosen, cur, improved = trial, v, True
        return chosen, cur

    starts = [chosen]
    for f in a.seed:
        sk = [d["sku"] for d in json.load(open(f, encoding="utf-8"))["day_one"] if d["sku"] in price]
        if len(sk) == len(SLOTS):
            starts.append(sk)
    chosen = max((polish(list(x)) for x in starts), key=lambda t: t[1])[0]
    if len(chosen) < len(SLOTS):
        sys.exit(f"Only {len(chosen)} different drinks fit under the ${CAP_CENTS / 100:.2f} cap for {len(SLOTS)} slots. "
                 "Raise the cap or add cheaper products; no plan was written.")
    v, r = score(chosen)
    plan = {"day_one": [dict(slot=int(SLOTS[i]), sku=s, units=cat[s]["cap"], price=price[s], price_cents=round(price[s] * 100)) for i, s in enumerate(chosen)],
            "source": "optimize.py", "base_gp": round(v, 2), "low_gp": r["scenarios"]["low"]["gross_profit"],
            "fill_most": r["fill_most"], "fill_expected": r["fill_expected"]}
    json.dump(plan, open(a.out, "w", encoding="utf-8"), indent=1)
    if not a.quiet:
        print(f"base GP ${v:.2f} low ${r['scenarios']['low']['gross_profit']:.2f} fill ${r['fill_expected']:.2f}/${r['fill_most']:.2f}")
        from collections import Counter
        for s, n in Counter(chosen).items():
            print(f"  {n} x {s} @ ${price[s]:.2f}")
    else:
        print(json.dumps({"base_gp": round(v, 2), "low_gp": r["scenarios"]["low"]["gross_profit"]}))


if __name__ == "__main__":
    main()
