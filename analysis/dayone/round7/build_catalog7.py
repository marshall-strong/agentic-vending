"""Build round7/catalog7.json: the REAL catalog in the team DB on 20 Nov (team_catalog_2026-11-20.json, the same file round 6 used), priced.

Price sources, in order:
  1. our sourcing_options row (expected and most-we-pay per pack), where one exists;
  2. the approved request's own note (15 Nov Amazon/Costco checks, "N-pack ... $X");
  3. the 11 Nov list round 3/4 used (Red Bull Red/Yellow, Celsius Kiwi, Spindrift, Baja Blast), and round 3's
     15 Nov bubly check.
Most-we-pay without a sourcing row: the projection's rule, expected x 1.15 rounded up to 50 cents.
A product with no price, or a package size with no column figure, is kept but not plannable.
    python analysis/dayone/round7/build_catalog7.py

It needs the team DB snapshot round7/team_catalog_2026-11-20.json, which is not committed: README.md says how to
make one. catalog7.json, its output, is committed as round 7's input.
"""
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))
from vending.capacity import for_product  # noqa: E402

SNAPSHOT = HERE / "team_catalog_2026-11-20.json"
if not SNAPSHOT.exists():
    sys.exit(f"{SNAPSHOT} is missing: it is a read-only snapshot of the team DB, not committed (README.md, 'Data').")
snap = json.load(open(SNAPSHOT, encoding="utf-8"))
over = snap["capacity_overrides"]
src = {s["sku"]: s for s in snap["sourcing"] if s["rank"] == "primary"}
req = {r["sku"]: r for r in snap["requests"] if r["sku"]}

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


out = []
for c in snap["catalog"]:
    sku, size = c["sku"], c["size"]
    cap = for_product(sku, size, over)
    s, r = src.get(sku), req.get(sku)
    pack = price = mx = store = None
    if s:
        pack, price, mx, store = s["units_per_pack"], s["expected_cents"] / 100, s["max_cents"] / 100, s["store"]
        source = f"sourcing row ({s['store']}, checked {s['checked_at'][:10]})"
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
    reason = LEAVE_OUT.get(sku) or (None if price else source) or (None if cap else f"no column figure for '{size}'")
    out.append(dict(sku=sku, name=c["name"], size=size, cat=category(sku, size), cap=cap, capacity_assumed=cap is None,
                    pack=pack or 1, price=price or 0.0, max=mx, store=store, src=source,
                    request=(r["id"] if r else None), orderable_now=reason is None, not_plannable=reason,
                    interloper=sku in INTERLOPER, in_catalog=True))
json.dump({"about": "Real team catalog on 20 Nov 2026 (83 live products), priced; see build_catalog6.py for sources.",
           "products": out}, open(HERE / "catalog7.json", "w", encoding="utf-8"), indent=1)
ok = [p for p in out if p["orderable_now"]]
print(len(ok), "plannable of", len(out))
for p in out:
    if not p["orderable_now"]:
        print("  not plannable:", p["sku"], "-", p["not_plannable"])
