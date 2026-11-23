"""Build round7/catalog7.json: the REAL catalog in the team DB on 20 Nov (team_catalog_2026-11-20.json, the same file round 6 used), priced.

Price sources, in order:
  1. our sourcing_options row (expected and most-we-pay per pack), where one exists;
  2. the approved request's own note (15 Nov Amazon/Costco checks, "N-pack ... $X");
  3. the 11 Nov list round 3/4 used (Red Bull Red/Yellow, Celsius Kiwi, Spindrift, Baja Blast), and round 3's
     15 Nov bubly check.
Most-we-pay without a sourcing row: the projection's rule, expected x 1.15 rounded up to 50 cents. A sourcing row
with no expected price uses its most-we-pay as the expected price too, as the planner does (vending.planner._Sources).
A product with no price, a package size with no column figure, or no category, is kept but not plannable.

Three flags per product, kept apart (#231 review):
  priced          it has a price estimate, from any of the sources above;
  checked_source  it has a checked-price sourcing row, which the fill needs to buy it (an estimate is not one);
  plannable       priced, with a column figure, and not left out on purpose: the optimisers' candidates.

    python analysis/dayone/round7/build_catalog7.py

It needs the team DB snapshot team_catalog_2026-11-20.json in the git-ignored local folder (README.md says how to
make one). catalog7.json, its output, holds no slot and is committed as round 7's input.
"""
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import project as P  # noqa: E402

sys.path.insert(0, P.DEFAULT_REPO)
from vending.capacity import for_product  # noqa: E402

SNAPSHOT = P.LOCAL / "team_catalog_2026-11-20.json"

CAT = {"ARIZONA": "tea", "PURELEAF": "tea", "BUBLY": "sparkling", "SPINDRIFT_SPARK": "sparkling", "HINT": "water",
       "VITAMINWATER": "water", "ESSENTIA": "water", "SNAPPLE": "juice", "PREMIER": "protein", "CORE": "protein",
       "GATORADE": "sports", "POWERADE": "sports", "BODYARMOR_LYTE": "sports", "STARBUCKS_FRAP": "coffee"}
ENERGY = {"CELSIUS", "GORGIE", "REDBULL", "STARBUCKS", "STARBUCKS_DOUBLE", "MONSTER", "GHOST", "GHOST_ZERO_SUGAR",
          "ALANI", "C4_PERF", "ROCKSTAR", "REIGN"}
TEA_BY_NAME = {"SNAPPLE-PEACH-16BOT"}   # Snapple Peach Tea is a tea; round 4 called it juice (its own check flagged this)

# 11 Nov list figures round 4 used for approved requests whose note carries no price: (pack, expected $)
ELEVEN_NOV = {"REDBULL-RE_WATERMELON-8_4CAN": (24, 47.99), "REDBULL-YE_TROPICAL-8_4CAN": (24, 42.48),
              "CELSIUS-SPARKLING_KIWI-12CAN": (12, 19.98), "SPINDRIFT_SPARK-GRAPEFRUIT-12CAN": (24, 20.97),
              "SPINDRIFT_SPARK-RASPBERRY_LIME-12CAN": (24, 20.91), "SPINDRIFT_SPARK-LEMON-12CAN": (24, 20.91),
              "MOUNTAIN-BAJA-20BOT": (24, 58.80)}
# round 3's 15 Nov check: "Amazon 8-packs $3.97-4.27 by flavour (top of range used)"
for _f in ("BLACKBERRY", "CHERRY", "GRAPEFRUIT", "LIME", "MANGO", "STRAWBERRY"):
    ELEVEN_NOV[f"BUBLY-{_f}-12CAN"] = (8, 4.27)
# Left out on purpose, with the reason shown in the output
LEAVE_OUT = {
    "CELSIUS-SPARKLING_PEACH-12CAN": "only price is the unconfirmed 11 Nov $13.99 per 12 that round 4 flagged; "
                                     "at the $19.98 of the other Celsius flavours it adds nothing they don't",
    # Round 7's snapshot predates config product_aliases; the team DB records this pair there now.
    "REDBULL-SUGARFREE-8_4CAN": "same product as REDBULL-SUGARFRE-8_4CAN (our request 1 got a second SKU); "
                                "the older SKU has our sourcing row",
    "PEPSI-ORIGINAL-20BOT": "15 Nov: no in-stock 20 oz Pepsi at an allowed store",
}
INTERLOPER = {"PEPSI-ORIGINAL-12CAN", "REIGN-RAZZLE-16CAN", "BODYARMOR_LYTE-STRAWBERRY-16BOT", "CORE-CHOCOLATE-14BOT",
              "SPINDRIFT_SPARK-GRAPEFRUIT-12CAN", "SPINDRIFT_SPARK-RASPBERRY_LIME-12CAN", "SPINDRIFT_SPARK-LEMON-12CAN"}
# #116: it sells Pepsi and Mountain Dew cans, Spindrift, Reign, BodyArmor, Core Power. A demand haircut only, never a price input.


def category(sku: str, size: str) -> str | None:
    line = sku.split("-")[0]
    if sku in TEA_BY_NAME:
        return "tea"
    if line in ENERGY:
        return "energy"
    if line in CAT:
        return CAT[line]
    if "20BOT" in sku:
        return "soda20"
    if "CAN" in sku:
        return "sodacan"
    return None


def note_price(note: str | None) -> tuple[int, float] | None:
    m = re.search(r"(\d+)-pack.*?\$(\d+\.\d\d)", note or "")
    return (int(m.group(1)), float(m.group(2))) if m else None


def build(snap: dict) -> list[dict]:
    """The priced catalog from a team DB snapshot (README.md, "Team DB snapshot")."""
    over = snap["capacity_overrides"]
    src = {s["sku"]: s for s in snap["sourcing"] if s["rank"] == "primary"}
    req = {r["sku"]: r for r in snap["requests"] if r["sku"]}
    # config product_aliases: SKUs that are the same product as another SKU. Only the SKU it maps to is plannable,
    # so the optimiser can't put one drink in two slots under two SKUs.
    aliases = snap.get("product_aliases") or {}
    return [product(c, src.get(c["sku"]), req.get(c["sku"]), over, aliases) for c in snap["catalog"]]


def product(c: dict, s: dict | None, r: dict | None, over: dict, aliases: dict | None = None) -> dict:
    sku, size = c["sku"], c["size"]
    cap = for_product(sku, size, over)
    pack = price = mx = store = None
    if s:
        expected = s["expected_cents"] if s.get("expected_cents") is not None else s["max_cents"]
        pack, price, mx, store = s["units_per_pack"], expected / 100, s["max_cents"] / 100, s["store"]
        source = f"sourcing row ({s['store']}, checked {s['checked_at'][:10]})"
        if s.get("expected_cents") is None:
            source += "; no expected price, so the most-we-pay is used for it"
    elif r and note_price(r["note"]):
        pack, price = note_price(r["note"])
        store = "Costco" if "Costco" in r["note"] and "Amazon" not in r["note"] else "Amazon"
        source = f"approved request {r['id']}: its 15 Nov note; no sourcing row yet"
    elif sku in ELEVEN_NOV:
        pack, price = ELEVEN_NOV[sku]
        store = "Amazon"
        source = ("15 Nov Amazon check (round 3)" if sku.startswith("BUBLY") else "11 Nov list (round 4)")             + (f"; approved request {r['id']}" if r else "") + "; no sourcing row yet"
    else:
        source = "not priced: no sourcing row, and no price in a request note"
    cat = category(sku, size)
    # A product with no category (a new brand: add its line to CAT or ENERGY) has no demand rate or reference price.
    same = (aliases or {}).get(sku)
    reason = (LEAVE_OUT.get(sku) or (same and f"same product as {same} (config product_aliases)")
              or (None if price else source) or (None if cap else f"no column figure for '{size}'")
              or (None if cat else f"no category for the line '{sku.split('-')[0]}': add it to CAT or ENERGY here"))
    # cap_counted: the cap is a person's count (capacity_overrides), which wins over the chart, as in the planner.
    return dict(sku=sku, name=c["name"], size=size, cat=cat, cap=cap, capacity_assumed=cap is None,
                cap_counted=sku in over,
                pack=pack or 1, price=price or 0.0, max=mx, store=store, src=source,
                request=(r["id"] if r else None), priced=bool(price), checked_source=s is not None,
                plannable=reason is None, not_plannable=reason,
                interloper=sku in INTERLOPER, in_catalog=True)


def main() -> None:
    if not SNAPSHOT.exists():
        sys.exit(f"{SNAPSHOT} is missing: it is a read-only snapshot of the team DB, not committed (README.md).")
    out = build(json.load(open(SNAPSHOT, encoding="utf-8")))
    json.dump({"about": "Real team catalog on 20 Nov 2026 (83 live products), priced; see build_catalog6.py for sources.",
               "products": out}, open(HERE / "catalog7.json", "w", encoding="utf-8"), indent=1)
    ok = [p for p in out if p["plannable"]]
    print(f"{len(ok)} plannable of {len(out)}; {sum(p['priced'] for p in out)} with a price estimate; "
          f"{sum(p['checked_source'] for p in out)} with a checked sourcing row")
    for p in out:
        if not p["plannable"]:
            print("  not plannable:", p["sku"], "-", p["not_plannable"])
    unchecked = [p["sku"] for p in ok if not p["checked_source"]]
    print(f"{len(unchecked)} plannable products have only an estimate, no checked sourcing row:", ", ".join(unchecked))


if __name__ == "__main__":
    main()
