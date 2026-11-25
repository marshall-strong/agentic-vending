"""The made-up part of the simulation: calendar, products, demand, the machine and its polls.

Every number here is either a repo fact (cited beside it) or an assumption the write-up names.
Randomness comes from one seed, through `rng(seed, *keys)`, so each draw is the same on a rerun
whatever order the events happen in.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from fractions import Fraction
from pathlib import Path
from zoneinfo import ZoneInfo

from vending import capacity, demand
from vending.evadts import crc16_arc
from vending.slots import DEVICE_ID

EASTERN = ZoneInfo("America/New_York")
REPO = Path(__file__).resolve().parents[2]
CALENDAR_FILE = REPO / "analysis" / "golisano_calendar_2026_27.json"

# --- Timeline (docs/opening-capital-plan.md, docs/planning/2026-11-12-david-answers.md) ----------------
# The ledger opens before the day-one fill; the machine goes live Thu 3 Dec; the run is 9 weeks,
# Thu 3 Dec to Wed 3 Feb if break weeks count (docs/inventory-demand.md, gap 7).
SIM_START = datetime(2026, 11, 30, 0, 0, tzinfo=EASTERN)
LEDGER_OPENS = datetime(2026, 11, 30, 9, 0, tzinfo=EASTERN)
DAY_ONE_RUN = datetime(2026, 11, 30, 10, 0, tzinfo=EASTERN)
DAY_ONE_FILL = datetime(2026, 12, 2, 15, 0, tzinfo=EASTERN)     # before the machine goes live
COMPETITION_START = datetime(2026, 12, 3, 0, 0, tzinfo=EASTERN)
COMPETITION_END = datetime(2027, 2, 4, 0, 0, tzinfo=EASTERN)    # end of Wed 3 Feb
WEEKLY_RUN_AT = time(19, 0)          # the run day's evening (Thursday by default, docs/agent.md; or Wednesday)
CHECK_IN_AT = time(7, 30)            # Monday morning, for a midweek visit (docs/restock-policy.md, rule 2)
VISIT_AT = time(15, 0)               # Friday 3 PM target, "likely later" (david-answers row 14)
OPENING_CAPITAL_CENTS = 60000


class Calendar:
    """Class days and closures from the Institute's calendar (analysis/golisano_calendar_2026_27.json)."""

    def __init__(self, path: Path = CALENDAR_FILE):
        events = json.loads(path.read_text(encoding="utf-8"))["events"]
        self.no_class: set[date] = set()
        self.closed: set[date] = set()
        for e in events:
            day, last = date.fromisoformat(e["start"]), date.fromisoformat(e["end"])
            while day <= last:
                if e.get("classes") == "none":
                    self.no_class.add(day)
                if e.get("institute") == "closed":
                    self.closed.add(day)
                day += timedelta(days=1)

    def is_class_day(self, day: date) -> bool:
        return day.weekday() < 5 and day not in self.no_class and day not in self.closed

    def is_open_weekday(self, day: date) -> bool:
        """A day the worker can visit: a weekday the Institute is open."""
        return day.weekday() < 5 and day not in self.closed

    def next_open_weekday(self, day: date) -> date:
        while not self.is_open_weekday(day):
            day += timedelta(days=1)
        return day

    def class_days(self, start: date, end: date) -> int:
        """Class days in [start, end)."""
        n, day = 0, start
        while day < end:
            n += self.is_class_day(day)
            day += timedelta(days=1)
        return n


# --- Products --------------------------------------------------------------------------------------
# Rates are units per class day for one stocked slot at the reference price: the mean of the fall
# segment rates (analysis/REPORT.md) over the selections the product line sold from that fall
# (REPORT.md, "By product line"). This is fall data, from before the fall cut, when no selection was
# either team's (docs/integrity.md); the selection names below say where a drink sold in the fall,
# not which selections are ours. A season's own slots come from its recorded assignment.
# Reference prices are from docs/agent/price-history.md, else its category row. Pack prices are the team's 11 Nov Amazon list (db/sourcing/2026-11-11-amazon.csv for
# the five with a sourcing row; docs/opening-capital-plan.md section 1 for the rest).
FAST, MID, SLOW_LOW, SLOW_ERRATIC = 1.701, 1.354, 0.767, 0.722
FALL_SEGMENT = {"15": MID, "16": MID, "17": MID, "18": MID, "25": MID, "26": MID, "28": FAST, "38": FAST,
                "45": FAST, "46": FAST, "35": SLOW_LOW, "36": SLOW_LOW, "37": SLOW_LOW, "55": SLOW_LOW,
                "58": SLOW_LOW, "27": SLOW_ERRATIC, "47": SLOW_ERRATIC, "48": SLOW_ERRATIC, "56": SLOW_ERRATIC,
                "57": SLOW_ERRATIC}


def _mean_rate(slots: str) -> float:
    names = slots.split()
    return round(sum(FALL_SEGMENT[s] for s in names) / len(names), 3)


@dataclass(frozen=True)
class Product:
    sku: str
    name: str
    line: str
    flavor: str
    size: str
    units_per_pack: int
    pack_cents: int             # 11 Nov list price for that pack on Amazon
    url: str | None             # Amazon page, where the repo has one
    sourced: bool               # had a row in the first sourcing file, 2026-11-11 (informational)
    reference_cents: int        # docs/agent/price-history.md
    rate: float                 # units per class day per stocked slot at the reference price
    rate_basis: str
    counted_column: int | None = None   # a person's count (`capacity_overrides`), for a size the chart lacks

    @property
    def column(self) -> int:
        units = self.counted_column or capacity.from_size(self.size)
        if units is None:
            raise ValueError(f"{self.sku}: the loading chart has no figure for {self.size!r}")
        return units

    def catalog_entry(self) -> dict:
        return {"sku": self.sku, "name": self.name, "line": self.line, "flavor": self.flavor, "size": self.size,
                "active": True}


PRODUCTS: tuple[Product, ...] = (
    Product("REDBULL-ORIGINAL-8_4CAN", "Red Bull Original 8.4 oz can", "Red Bull", "Original", "8.4 oz can",
            24, 3499, "https://www.amazon.com/dp/B003PFPFIE", True, 400, _mean_rate("16 26 27 38"),
            "Red Bull's fall slots 16, 26, 27, 38"),
    Product("CELSIUS-SPARKLING_ORANGE-12CAN", "Celsius Sparkling Orange 12 oz can", "Celsius", "Sparkling Orange",
            "12 oz can", 12, 2157, "https://www.amazon.com/dp/B007R8XGJA", True, 350, _mean_rate("25 26 27 28"),
            "Celsius's fall slots 25-28"),
    Product("MOUNTAIN-ORIGINAL-20BOT", "Mountain Dew 20 oz bottle", "Mountain Dew", "Original", "20 oz bottle",
            24, 4924, "https://www.amazon.com/dp/B00ODVQ8U8", True, 230, _mean_rate("45 46 47 55 58"),
            "soda's fall slots 45-47, 55, 58 (no Mountain Dew history)"),
    Product("COCA-ORIGINAL-20BOT", "Coca-Cola 20 oz bottle", "Coca-Cola", "Original", "20 oz bottle",
            24, 5495, "https://www.amazon.com/dp/B004I405P4", True, 200, _mean_rate("45 46"),
            "Coca-Cola's fall slots 45, 46"),
    Product("COCA-DIET-20BOT", "Diet Coke 20 oz bottle", "Diet Coke", "Diet", "20 oz bottle",
            24, 5099, "https://www.amazon.com/dp/B008CAB1OE", True, 200, _mean_rate("45 47 55"),
            "Diet Coke's fall slots 45, 47, 55"),
    # In the catalog but with no sourcing row: the agent must give its own sourcing to buy them.
    Product("PEPSI-ORIGINAL-12CAN", "Pepsi 12 oz can", "Pepsi", "Original", "12 oz can",
            12, 697, None, False, 230, _mean_rate("45 46 47 55 58"), "soda's fall slots"),
    Product("SPINDRIFT-GRAPEFRUIT-12CAN", "Spindrift Grapefruit 12 oz can", "Spindrift", "Grapefruit", "12 oz can",
            24, 2097, None, False, 160, _mean_rate("36 55 56 57"), "sparkling water's fall slots 36, 55-57"),
    Product("REDBULL-RED-8_4CAN", "Red Bull Red Edition 8.4 oz can", "Red Bull", "Red Edition", "8.4 oz can",
            24, 4799, None, False, 410, _mean_rate("16 26 27 38"), "Red Bull's fall slots; energy reference"),
    Product("PEPSI-ORIGINAL-20BOT", "Pepsi 20 oz bottle", "Pepsi", "Original", "20 oz bottle",
            12, 2999, None, False, 230, _mean_rate("45 46 47 55 58"), "soda's fall slots"),
    # The team's 15 Nov sourcing file (db/sourcing/2026-11-15-catalog.csv, #137). Rates and references
    # follow the same rule: the product line's fall slots and row in price-history.md, else its
    # category's. Left out: Pepsi Real Sugar 7.5 oz cans and Premier Protein cartons, whose sizes
    # the loading chart (vending/capacity.py) doesn't cover.
    Product("REDBULL-SUGARFRE-8_4CAN", "Red Bull Sugarfree 8.4 oz can", "Red Bull", "Sugarfree", "8.4 oz can",
            24, 3499, None, False, 400, _mean_rate("16 26 27 38"), "Red Bull's fall slots"),
    Product("GORGIE-MANGO-12CAN", "Gorgie Mango Burst 12 oz can", "Gorgie", "Mango Burst", "12 oz can",
            12, 3108, None, False, 450, _mean_rate("15 16 17"), "Gorgie's fall slots 15-17"),
    Product("GORGIE-PEACH-12CAN", "Gorgie Peach Tea 12 oz can", "Gorgie", "Peach Tea", "12 oz can",
            12, 3199, None, False, 450, _mean_rate("15 16 17"), "Gorgie's fall slots 15-17"),
    Product("STARBUCKS-BLUE_LEMON-12CAN", "Starbucks Blueberry Lemonade 12 oz can", "Starbucks",
            "Blueberry Lemonade", "12 oz can", 12, 2398, None, False, 350, _mean_rate("38"),
            "Starbucks's fall slot 38"),
    Product("SNAPPLE-FRUIT_PUNCH-16BOT", "Snapple Fruit Punch 16 oz bottle", "Snapple", "Fruit Punch",
            "16 oz bottle", 12, 1884, None, False, 300, _mean_rate("27 38 47 48 55"), "Snapple's fall slots"),
    Product("SNAPPLE-MANGO_MADNESS-16BOT", "Snapple Mango Madness 16 oz bottle", "Snapple", "Mango Madness",
            "16 oz bottle", 12, 2145, None, False, 300, _mean_rate("27 38 47 48 55"), "Snapple's fall slots"),
    Product("HINT-GEORGIA_PEACH-16BOT", "Hint Georgia Peach 16 oz bottle", "Hint", "Georgia Peach",
            "16 oz bottle", 12, 1346, None, False, 265, _mean_rate("37 46 47 48 57 58"),
            "water's fall slots (vitaminwater's); water reference"),
    Product("HINT-GRAPE-16BOT", "Hint Grape 16 oz bottle", "Hint", "Grape", "16 oz bottle",
            12, 1346, None, False, 265, _mean_rate("37 46 47 48 57 58"),
            "water's fall slots (vitaminwater's); water reference"),
    Product("ARIZONA-ICED_LEMON-16BOT", "AriZona Iced Tea with Lemon 16 oz bottle", "AriZona", "Iced Tea Lemon",
            "16 oz bottle", 12, 819, None, False, 235, _mean_rate("35 36"), "tea's fall slots (Pure Leaf's)"),
    Product("COCA-ORIGINAL-12CAN", "Coca-Cola 12 oz can", "Coca-Cola", "Original", "12 oz can",
            12, 768, None, False, 200, _mean_rate("45 46"), "Coca-Cola's fall slots 45, 46"),
    Product("PEPSI-DIET-20BOT", "Diet Pepsi 20 oz bottle", "Pepsi", "Diet", "20 oz bottle",
            24, 5745, None, False, 230, _mean_rate("45 46 47 55 58"), "soda's fall slots"),
    Product("MOUNTAIN-CODE_RED-20BOT", "Mountain Dew Code Red 20 oz bottle", "Mountain Dew", "Code Red",
            "20 oz bottle", 24, 5745, None, False, 230, _mean_rate("45 46 47 55 58"), "soda's fall slots"),
    Product("MOUNTAIN-DIET-20BOT", "Diet Mountain Dew 20 oz bottle", "Mountain Dew", "Diet", "20 oz bottle",
            24, 5521, None, False, 230, _mean_rate("45 46 47 55 58"), "soda's fall slots"),
    Product("MONSTER-WHITE-16CAN", "Monster Zero Ultra 16 oz can", "Monster Energy", "Zero Ultra", "16 oz can",
            15, 2989, None, False, 450, _mean_rate("15 16 17 18"), "Monster's fall slots 15-18"),
)
BY_SKU = {p.sku: p for p in PRODUCTS}

# A drink from an export (`vending.simulate.export`) whose product line has no fall history and no product above:
# the mean of the mid segments, at the plan's own price (assumed). A drink of a line above takes that line's rate.
UNKNOWN_LINE_RATE = MID


def product_for(entry: dict, units_per_pack: int, pack_cents: int, price_cents: int | None,
                column: int | None = None) -> Product | None:
    """A world product for a catalog drink the world doesn't have (`entry`: the bridge's product fields), bought
    in `units_per_pack` for `pack_cents`. Its rate and reference price are its product line's, where a product
    above has that line, else `UNKNOWN_LINE_RATE` at `price_cents` (the plan's price; $3.00 without one). None
    when neither the loading chart nor `column` (a person's count, `capacity_overrides`) sizes it."""
    if column is None and capacity.from_size(entry.get("size") or "") is None:
        return None
    kin = next((p for p in PRODUCTS if p.line.casefold() == str(entry.get("line") or "").casefold()), None)
    if kin is not None:
        rate, reference, basis = kin.rate, kin.reference_cents, f"{kin.line}'s rate ({kin.rate_basis})"
    else:
        rate, reference = UNKNOWN_LINE_RATE, price_cents or 300
        basis = "no fall line: the mid segments' mean, at the plan's price (assumed)"
    return Product(entry["sku"], entry.get("name") or entry["sku"], entry.get("line") or "", entry.get("flavor") or "",
                   entry["size"], units_per_pack, pack_cents, None, True, reference, rate, basis,
                   column if column else None)


def register(products: list[dict]) -> list[str]:
    """Add products (a `Product`'s fields, as a season keeps them) to the world, for this process. A SKU the world
    already has keeps its own. Returns the SKUs added."""
    added = []
    for fields in products:
        if fields["sku"] not in BY_SKU:
            BY_SKU[fields["sku"]] = Product(**fields)
            added.append(fields["sku"])
    return added

# The tag on an empty slot before its first load (assumed). The machine starts empty, and the worker sets
# each slot's tag to the sale price of what it loads, so this only shows in the polls of empty slots.
OPENING_PRICE_CENTS = 250


# --- Demand ----------------------------------------------------------------------------------------
# Weekday index Mon 1.19 / Tue 1.74 / Wed 1.10 / Thu 1.61 / Fri 1.33 (REPORT.md finding 6), scaled so
# a class day averages 1. 66% of units sell 8 to 11 AM (finding 7); the rest of the day is assumed.
_DOW = (1.19, 1.74, 1.10, 1.61, 1.33)
DOW = tuple(v * 5 / sum(_DOW) for v in _DOW)
# The hourly shares live in `vending.demand`, which the midweek wake check reads too; floats here,
# as before, so the simulator's arithmetic is unchanged.
HOUR_SHARE = {hour: float(share) for hour, share in demand.HOUR_SHARE.items()}
OFF_DAY = 0.05      # a weekend or no-class weekday sells "about nothing" (inventory-demand.md, assumptions)


# The Interloper (#116, docs/agent/interloper.md): the outside machine beside ours, as Sean saw it on
# 14 Nov. A product line it sells loses a share of our demand to it; an energy drink loses a smaller
# share to its Reign. Its other drinks (protein shakes) aren't products here. Nobody can see its sales,
# so the strength is an assumption, swept in the write-up.
INTERLOPER_LINES = {"Pepsi": 150, "Mountain Dew": 150, "Spindrift": 150}    # its price, cents (12 oz cans)
INTERLOPER_ENERGY = 400                                                    # Reign, 16 oz can
ENERGY_LINES = frozenset({"Red Bull", "Celsius", "Gorgie", "Monster Energy"})
ENERGY_WEIGHT = 0.4             # a different energy brand competes less than the same drink
INTERLOPER_MAX_LOSS = 0.9


def interloper_loss(p: "Product", price_cents: int, strength: float) -> float:
    """The share of `p`'s demand the Interloper takes at our price: strength x weight x price ratio.

    At the same price, the same line loses `strength`; dearer than theirs loses more (the ratio is
    capped at 2), cheaper loses less. Per unit, not per ounce: a shopper sees two price tags.
    """
    if strength <= 0:
        return 0.0
    if p.line in INTERLOPER_LINES:
        theirs, weight = INTERLOPER_LINES[p.line], 1.0
    elif p.line in ENERGY_LINES:
        theirs, weight = INTERLOPER_ENERGY, ENERGY_WEIGHT
    else:
        return 0.0
    return min(INTERLOPER_MAX_LOSS, strength * weight * min(2.0, price_cents / theirs))


@dataclass(frozen=True)
class Demand:
    """How customers respond. The knobs are assumptions; the write-up tests other values offline."""
    elasticity: float = 0.46        # ln(1.354/1.701) / ln(4.326/2.626): fall mid vs fast segment
    facing_exponent: float = 0.5    # product demand grows with the square root of its stocked slots
    interloper: float = 0.0         # share of a same-line drink's demand lost to the Interloper at equal price

    def day_factor(self, cal: Calendar, day: date) -> float:
        if day in cal.closed:
            return 0.0
        if cal.is_class_day(day):
            return DOW[day.weekday()]
        return OFF_DAY

    def slot_rates(self, cal: Calendar, hour_start: datetime,
                   slots: dict[str, "SlotState"]) -> tuple[dict[str, float], dict[str, float]]:
        """(expected sales, expected lost demand) per slot in the hour starting at `hour_start`.

        A product in n slots, k of them stocked, draws rate x k ** facing_exponent, shared equally by the
        stocked slots, each at its own price. What the empty slots lose is the difference to all n stocked,
        so an empty slot whose product is stocked next door loses less than one that is the last of it.
        """
        local = hour_start.astimezone(EASTERN)
        share = HOUR_SHARE.get(local.hour, 0.0) * self.day_factor(cal, local.date())
        sales = {s: 0.0 for s in slots}
        lost = {s: 0.0 for s in slots}
        if share == 0:
            return sales, lost
        by_product: dict[str, list[str]] = {}
        for name, st in slots.items():
            if st.sku:
                by_product.setdefault(st.sku, []).append(name)
        for sku, names in by_product.items():
            p = BY_SKU.get(sku)
            if p is None:
                continue
            stocked = [n for n in names if slots[n].units > 0]
            empty = [n for n in names if slots[n].units <= 0]
            n, k = len(names), len(stocked)

            def unit(name: str) -> float:
                price = slots[name].price_cents or p.reference_cents
                kept = 1.0 - interloper_loss(p, price, self.interloper)
                return p.rate * (price / p.reference_cents) ** (-self.elasticity) * share * kept

            for name in stocked:
                sales[name] = unit(name) * k ** self.facing_exponent / k
            if empty:
                gap = (n ** self.facing_exponent - k ** self.facing_exponent) / len(empty)
                for name in empty:
                    lost[name] = unit(name) * gap
        return sales, lost


def poisson(r: random.Random, mean: float) -> int:
    if mean <= 0:
        return 0
    limit, k, prod = math.exp(-mean), 0, r.random()
    while prod > limit:
        k += 1
        prod *= r.random()
    return k


def rng(seed: int, *keys: object) -> random.Random:
    """A generator for one draw site, independent of every other, from the seed and the keys."""
    digest = hashlib.sha256(json.dumps([seed, *[str(k) for k in keys]]).encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


# --- The machine -----------------------------------------------------------------------------------

@dataclass
class SlotState:
    sku: str | None
    units: int
    price_cents: int
    lifetime_units: int
    lifetime_cents: int


@dataclass
class Machine:
    """What is physically in our 20 slots and the closet. Only the simulated worker and customers see it;
    the agent sees polls and receipts, as in the real competition."""
    slots: dict[str, SlotState]
    closet: dict[str, int] = field(default_factory=dict)

    @classmethod
    def empty(cls, slots: tuple[str, ...] | list[str]) -> "Machine":
        """Our `slots` (the season's recorded assignment), empty. Staff empty the machine before 3 Dec
        (inventory-demand.md, "Day one"). Lifetime counters carry on from the fall; their start values
        are arbitrary, since only rises count."""
        if not slots:
            raise ValueError("a simulated machine needs the season's slot assignment")
        return cls({s: SlotState(None, 0, OPENING_PRICE_CENTS, 40 + 3 * i, (40 + 3 * i) * 300)
                    for i, s in enumerate(slots)})

    def sell(self, slot: str, units: int) -> int:
        st = self.slots[slot]
        sold = min(units, st.units)
        st.units -= sold
        st.lifetime_units += sold
        st.lifetime_cents += sold * st.price_cents
        return sold


def poll_document(machine: Machine, at: datetime, serial: str = "RVV_NG SIM0001") -> tuple[dict, bytes]:
    """One EVA-DTS poll of our selections, as the bridge stores it, with a correct G85 checksum.

    Only our selections are in it. The real polls from 3 Dec carry the whole machine (David, 17 Nov), and
    every reader filters them to our assignment; the simulated machine has no other selections to send.
    """
    total_cents = sum(s.lifetime_cents for s in machine.slots.values())
    total_units = sum(s.lifetime_units for s in machine.slots.values())
    body = [f"ST*001*0001", f"ID1*>*{serial}", "ID4*2", f"VA1*{total_cents}*{total_units}"]
    for name, st in machine.slots.items():
        body += [f"PA1*{name}*{st.price_cents}", f"PA2*{st.lifetime_units}*{st.lifetime_cents}"]
    covered = ("\r\n".join(body) + "\r\n").encode("ascii")
    crc = f"{crc16_arc(covered):04X}"
    raw = b"DXS*SIM0000001*VA*V1/1*1\r\n" + covered + f"G85*{crc}\r\nSE*{len(body) + 2}*0001\r\nDXE*1*1\r\n".encode()
    session = f"sim-{at.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}"
    return {"session_id": session, "started_at": at.astimezone(timezone.utc).isoformat(), "device_id": DEVICE_ID}, raw


# --- Shop prices ------------------------------------------------------------------------------------

DRIFT_SD = 0.03          # a pack's price moves about 3% week to week (assumed; the ~15% max covers drift)
DRIFT_LIMIT = (0.92, 1.12)
STORE_SOLD_OUT = 0.03    # chance a store has none of a product on a given visit (assumed)
BREAKAGE = 0.005         # chance each unit arrives broken (assumed)


def _store_unit_prices() -> dict[tuple[str, str], Fraction]:
    """Unit prices the team found at each store (db/sourcing/*.csv): a pack at BJ's prices from BJ's
    list, not Amazon's. The latest file wins."""
    import csv
    out: dict[tuple[str, str], Fraction] = {}
    for path in sorted((REPO / "db" / "sourcing").glob("*.csv")):
        with path.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                if row.get("price") and row.get("units_per_pack"):
                    cents = Fraction(row["price"]) * 100
                    out[(row["sku"], row["store"].casefold())] = cents / int(row["units_per_pack"])
    return out


STORE_UNIT_PRICE = _store_unit_prices()


def register_prices(rows: list[tuple[str, str, int, int]]) -> None:
    """Unit prices from an export, as (sku, store, pack cents, units per pack): they win over the sourcing files'."""
    for sku, store, cents, units in rows:
        STORE_UNIT_PRICE[(sku, store.casefold())] = Fraction(cents, units)


def market_pack_cents(seed: int, sku: str, units_per_pack: int, store: str, on: date) -> int:
    """What a pack costs at a store in the week of `on`: the 11 Nov list price per unit, times the pack,
    with a seeded weekly drift. Other stores are assumed to charge the Amazon unit price."""
    p = BY_SKU[sku]
    week = (on - date(2026, 11, 7)).days // 7
    r = rng(seed, "price", sku, week)
    drift = min(max(math.exp(r.gauss(0, DRIFT_SD)), DRIFT_LIMIT[0]), DRIFT_LIMIT[1])
    per_unit = STORE_UNIT_PRICE.get((sku, store.casefold()), Fraction(p.pack_cents, p.units_per_pack))
    return int(round(per_unit * units_per_pack * Fraction(drift)))
