"""Day-one planning rounds, read from a folder of round files, for the planning page.

A *round* is one answer from our agent to the day-one planning brief: which product goes in each of
our 20 slots, at what price, and which catalog requests it wants. Round files live outside the repo:

    round1/plan1.json        + round1/projection1.json
    round2/plan2a.json       + round2/projection2a.json
    round2/plan2b.json       + round2/projection2b.json
    round4/r4.plan.json      + round4/projection4.json

A plan is paired with the projection that has the same suffix. Other files are ignored, including
`plan1_sku.json` (round 1 re-keyed to SKUs, which is the projection's input) and the raw answers.

What this module computes, and what it only reads:

- **Computed here, from the plan and the projection's pack list:** each slot's floor
  (`pricing.pack_floor_cents` on the most-we-pay pack price), whether its price is legal
  (`pricing.check_price`), whether its units fit a column (`capacity.from_size`), and the fill's
  packs, expected and most-we-pay cost and closet leftovers. This is the same check as the round's
  `validate.py`, so the page never trusts a model's own arithmetic.
- **Read, never computed:** the projection: units per class day, class days of cover, the sell-out
  date, margin, gross profit, and the weekly figures for the low, base and high scenarios, with the
  `assumptions` that produced them. This module has no demand model. The projection script owns it.

Money is integer cents or an exact Fraction. A round-1 plan stored prices as JSON floats such as
3.5; those are read through their decimal text and refused unless they are a whole number of cents.

Only the selections in our current assignment (the inventory server's split, `vending.slots.our_slots`,
passed in by the caller) may appear in a round; a file naming any other selection is refused, so the page
can't show a value for it. The split was redrawn once, on 20 Nov, and is final for the quarter. `load_rounds`
sets aside a round placed in other slots (one made before the redraw), with only its projection's totals, so
the rounds placed again for our slots still show. Standard library only.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import Any

from vending import capacity, pricing

# Proposed in #113 ("Opening capital plan"), not yet agreed: the day-one fill's most-we-pay cap and the
# starting money. Shown as reference lines only; nothing here enforces them.
FILL_CAP_CENTS = 36_600
START_CENTS = 60_000
CEILING_CENTS = 650           # config.price_ceiling_cents on main (12 Nov decision)
SCENARIOS = ("low", "base", "high")

_PLAN = re.compile(r"^(?:plan(?P<legacy>\d+[a-z]?)|r(?P<new>\d+[a-z]?)\.plan)\.json$")
_PROJECTION = re.compile(r"^projection(?P<id>\d+[a-z]?)\.json$")
_ROUND_DIR = re.compile(r"^round\d+$")
_SELECTION = re.compile(r"[1-5][1-8]")      # shelf 1-5, then column 1-8


class RoundError(ValueError):
    """A round file the page can't use. The message names the file and the problem."""


class OtherSlots(RoundError):
    """A round that places products in slots outside our assignment: one made before the redraw, say.
    Raised only once the whole round has passed every other check, so it carries the checked round, the
    projection it was read from, and every selection it names that isn't ours, shelf then column."""

    def __init__(self, message: str, checked: Round, projection: dict, outside: tuple[str, ...]) -> None:
        super().__init__(message)
        self.checked = checked
        self.projection = projection
        self.outside = outside


@dataclass(frozen=True)
class Pack:
    sku: str
    name: str
    size: str | None
    category: str | None
    units_per_pack: int
    expected_cents: int
    max_cents: int
    column_capacity: int | None
    capacity_assumed: bool
    store: str | None
    real_sku: str | None


@dataclass(frozen=True)
class Projected:
    """One slot's projection under one scenario, exactly as the projection file has it."""
    units_per_class_day: float | None
    class_days_to_sellout: float | None
    sellout_date: date | None
    margin_cents: int | None
    gross_profit_cents: int | None
    weekly: tuple[tuple[date, float, int], ...]   # (week_start, units, gp_cents)


@dataclass(frozen=True)
class Slot:
    slot: str
    product: str
    size: str | None
    sku: str | None
    category: str | None
    units: int
    price_cents: int
    why: str | None
    needs_request: bool | None
    interloper: bool | None              # the projection's flag: the Interloper sells this drink
    floor_cents: int | None              # None when the round has no pack for this product
    legal_price: bool | None              # None when the floor is unknown
    capacity: int | None                 # units per column: the chart, else the projection's assumed figure
    capacity_assumed: bool
    most_cents: Fraction | None          # this slot's share of the fill at the most-we-pay pack price
    expected_cents: Fraction | None
    refusals: tuple[str, ...]            # check_price and capacity problems, in words
    projected: dict[str, Projected] = field(default_factory=dict)
    fill_check: str | None = None


@dataclass(frozen=True)
class Purchase:
    sku: str
    product: str
    units_loaded: int
    packs: int
    units_per_pack: int
    closet: int
    expected_cents: int
    most_cents: int


@dataclass(frozen=True)
class Totals:
    units_sold: float | None
    gross_profit_cents: int | None
    model_spend_cents: int | None
    profit_cents: int | None
    week1_gp_cents: int | None


@dataclass(frozen=True)
class Round:
    id: str                               # "1", "2a", "2b"
    folder: str
    plan_file: str
    projection_file: str | None
    saved_at: datetime                    # the plan file's modified time
    slots: dict[str, Slot]
    catalog_requests: list[dict]          # rank, name, size, why (as the plan has them)
    purchases: list[Purchase]
    fill_expected_cents: int | None       # recomputed here
    fill_most_cents: int | None
    totals: dict[str, Totals]             # by scenario, from the projection
    weeks: dict[str, list[tuple[date, int]]]   # by scenario: week_start, summed gp_cents over our slots
    window: str | None
    assumptions: dict
    notes: dict[str, Any]                 # fallbacks, asks, checks, expect, note, problems, needs_* as given
    warnings: list[str]                   # underfills and disagreements with the projection
    ours: tuple[str, ...]                 # our slots it was checked against, shelf then column

    @property
    def projected(self) -> bool:
        return bool(self.totals)


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RoundError(f"{path.name}: can't read it as JSON ({error})") from None


def _cents_field(value: Any, what: str) -> int:
    """A `*_cents` field: whole cents, as an int (or a float or digit string with no fraction).
    Anything else is refused rather than guessed at, so 405.0 is 405 cents, never $405."""
    if isinstance(value, bool) or value is None:
        raise RoundError(f"{what}: no amount")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value)
    raise RoundError(f"{what}: {value!r} isn't a whole number of cents")


def _cents(value: Any, what: str) -> int:
    """Integer cents from a dollar price: a price string, or a float that is a whole number of
    cents (round 1's `price`). An int is taken as cents."""
    if isinstance(value, bool) or value is None:
        raise RoundError(f"{what}: no price")
    if isinstance(value, int):
        return value
    try:
        dollars = Decimal(repr(value)) if isinstance(value, float) else Decimal(str(value))
    except InvalidOperation:
        raise RoundError(f"{what}: not a price: {value!r}") from None
    cents = dollars * 100
    if not cents.is_finite() or cents != cents.to_integral_value():
        raise RoundError(f"{what}: {value!r} isn't a whole number of cents")
    return int(cents)


def _opt_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _opt_num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _opt_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _round_key(round_id: str) -> tuple[int, str]:
    m = re.match(r"(\d+)(.*)", round_id)
    return (int(m.group(1)), m.group(2)) if m else (0, round_id)


def _slot_key(slot: str) -> tuple[int, int]:
    """Shelf, then column: the order the machine's face reads in."""
    return int(slot[0]), int(slot[1:])


def discover(root: Path) -> list[tuple[str, Path, Path | None]]:
    """(round id, plan file, projection file or None) for every round under `root`, oldest first."""
    found: dict[str, tuple[Path, Path | None]] = {}
    if not root.is_dir():
        raise RoundError(f"{root}: not a folder")
    folders = [root, *sorted(p for p in root.iterdir() if p.is_dir() and _ROUND_DIR.fullmatch(p.name))]
    for folder in folders:
        files = sorted(p for p in folder.iterdir() if p.is_file())
        projections = {m["id"]: p for p in files if (m := _PROJECTION.fullmatch(p.name))}
        for plan in files:
            match = _PLAN.fullmatch(plan.name)
            if match is None:
                continue
            rid = match["legacy"] or match["new"]
            # Prefer the checked planN.json to an rN.plan.json beside it.
            legacy = match["legacy"] is not None
            if rid in found:
                old = found[rid][0]
                old_legacy = _PLAN.fullmatch(old.name)["legacy"] is not None
                if old.parent != folder or legacy == old_legacy:
                    raise RoundError(f"round {rid} has two plan files: {old.parent.name}/{old.name} and "
                                     f"{folder.name}/{plan.name}")
                if old_legacy:
                    continue
            found[rid] = (plan, projections.get(rid))
    return [(rid, *found[rid]) for rid in sorted(found, key=_round_key)]


def _packs(projection: dict, name: str) -> dict[str, Pack]:
    packs = {}
    for p in projection.get("packs") or []:
        sku = p.get("sku")
        if not sku:
            raise RoundError(f"{name}: a pack has no sku")
        upp = _opt_int(p.get("units_per_pack"))
        if not upp or upp <= 0:
            raise RoundError(f"{name}: pack {sku} has no units_per_pack")
        packs[sku] = Pack(
            sku=sku, name=p.get("name") or sku, size=p.get("size"), category=p.get("category"),
            units_per_pack=upp,
            expected_cents=_cents_field(p.get("expected_cents"), f"{name}: pack {sku} expected_cents"),
            max_cents=_cents_field(p.get("max_cents"), f"{name}: pack {sku} max_cents"),
            column_capacity=_opt_int(p.get("column_capacity")),
            capacity_assumed=bool(p.get("capacity_assumed")),
            store=p.get("store"), real_sku=p.get("real_sku"))
    return packs


def _projected(row: dict) -> Projected:
    weekly = []
    for w in row.get("weekly") or []:
        start = _opt_date(w.get("week_start"))
        gp = _opt_int(w.get("gp_cents"))
        if start is not None and gp is not None:
            weekly.append((start, _opt_num(w.get("units")) or 0.0, gp))
    return Projected(
        units_per_class_day=_opt_num(row.get("units_per_class_day")),
        class_days_to_sellout=_opt_num(row.get("class_days_to_sellout")),
        sellout_date=_opt_date(row.get("sellout_date")),
        margin_cents=_opt_int(row.get("margin_cents")),
        gross_profit_cents=_opt_int(row.get("gross_profit_cents")),
        weekly=tuple(weekly))


def _check_slot(raw: Any, name: str, ours: tuple[str, ...], outside: dict[str, str]) -> str:
    """The slot as text. One that is a selection but not ours goes in `outside` (slot -> file), so the
    round is still checked in full; one that isn't a selection at all (missing, say) is refused."""
    slot = str(raw)
    if slot in ours:
        return slot
    if isinstance(raw, bool) or not _SELECTION.fullmatch(slot):
        raise RoundError(f"{name}: slot {raw!r} isn't a selection (shelf 1-5 then column 1-8, like 15)")
    outside.setdefault(slot, name)
    return slot


def _check_note_slots(value: Any, name: str, ours: tuple[str, ...], outside: dict[str, str]) -> None:
    """Notes name slots in free text ("38, 45, 46", "15-17", "15 to 17"); every two-digit selection named must be
    ours, including each one inside a range."""
    for item in value if isinstance(value, list) else [value]:
        text = str(item.get("slot", "")) if isinstance(item, dict) else ""
        for low, high in re.findall(r"\b(\d{2})\s*(?:-|–|to)\s*(\d{2})\b", text):
            for n in range(min(int(low), int(high)), max(int(low), int(high)) + 1):
                if str(n)[1] in "12345678":           # 19 and 20 aren't selections, so "18-21" is 18 and 21
                    _check_slot(str(n), name, ours, outside)
        for slot in re.findall(r"\b\d{2}\b", text):
            _check_slot(slot, name, ours, outside)


def load_round(round_id: str, plan_path: Path, projection_path: Path | None, ours: tuple[str, ...]) -> Round:
    """One round, checked against `ours` (our slots, from `vending.slots.our_slots`), or RoundError
    naming the file when a plan or projection has an unexpected shape."""
    return read_round(round_id, plan_path, projection_path, ours)[0]


def read_round(round_id: str, plan_path: Path, projection_path: Path | None,
               ours: tuple[str, ...]) -> tuple[Round, dict, dict]:
    """Read once and return the checked round and the two JSON objects used to check it."""
    plan = _load(plan_path)
    projection = _load(projection_path) if projection_path else {}
    checked = parse_round(round_id, plan, projection, ours,
                          saved_at=datetime.fromtimestamp(plan_path.stat().st_mtime).astimezone(),
                          folder=plan_path.parent.name, plan_file=f"{plan_path.parent.name}/{plan_path.name}",
                          projection_file=(f"{projection_path.parent.name}/{projection_path.name}"
                                           if projection_path else None))
    return checked, plan, projection


def parse_round(round_id: str, plan: dict, projection: dict, ours: tuple[str, ...], *,
                saved_at: datetime, folder: str = "", plan_file: str = "plan",
                projection_file: str | None = None) -> Round:
    """Check a round from JSON objects, with no dependency on where they were stored."""
    try:
        if not isinstance(plan, dict) or not isinstance(projection, dict):
            raise RoundError(f"round {round_id}: the plan and projection must be JSON objects")
        return _parse_round(round_id, plan, projection, ours, saved_at, folder, plan_file, projection_file)
    except RoundError:
        raise
    except (AttributeError, TypeError, KeyError, ValueError) as error:
        raise RoundError(f"round {round_id}: a plan or projection value has an unexpected shape "
                         f"({type(error).__name__}: {error})") from None


def _parse_round(round_id: str, plan: dict, projection: dict, ours: tuple[str, ...],
                 saved_at: datetime, folder: str, pname: str, projection_file: str | None) -> Round:
    ours = tuple(sorted(ours, key=_slot_key))
    jname = projection_file or "projection"
    warnings: list[str] = []
    outside: dict[str, str] = {}     # selections named that aren't ours: refused once the rest has been checked

    packs = _packs(projection, jname) if projection else {}
    # The projection's own view of each slot: sku, name, size, category, Interloper flag.
    proj_slots = {_check_slot(s.get("slot"), jname, ours, outside): s for s in projection.get("slots") or []}
    scen_rows: dict[str, dict[str, dict]] = {}
    totals: dict[str, Totals] = {}
    weeks: dict[str, list[tuple[date, int]]] = {}
    for scen in SCENARIOS:
        block = (projection.get("scenarios") or {}).get(scen)
        if not block:
            continue
        scen_rows[scen] = {_check_slot(r.get("slot"), jname, ours, outside): r for r in block.get("per_slot") or []}
        t = block.get("totals") or {}
        if not isinstance(t, dict):
            raise RoundError(f"{jname}: the {scen} scenario's totals aren't an object")
        totals[scen] = Totals(_opt_num(t.get("units_sold")), _opt_int(t.get("gross_profit_cents")),
                              _opt_int(t.get("model_spend_cents")), _opt_int(t.get("profit_cents")),
                              _opt_int(t.get("week1_gp_cents")))
        by_week: dict[date, int] = {}
        for row in scen_rows[scen].values():
            for start, _units, gp in _projected(row).weekly:
                by_week[start] = by_week.get(start, 0) + gp
        weeks[scen] = sorted(by_week.items())

    entries = plan.get("day_one")
    if not isinstance(entries, list) or not entries:
        raise RoundError(f"{pname}: no day_one list")
    slots: dict[str, Slot] = {}
    for entry in entries:
        slot = _check_slot(entry.get("slot"), pname, ours, outside)
        if slot in slots:
            raise RoundError(f"{pname}: slot {slot} is listed twice")
        ps = proj_slots.get(slot, {})
        sku = entry.get("sku") or ps.get("sku")
        pack = packs.get(sku) if sku else None
        # The projection's name and size describe its own SKU; use them only when it is this slot's.
        same = ps if ps and ps.get("sku") == sku else {}
        if ps and ps.get("sku") and entry.get("sku") and ps.get("sku") != entry.get("sku"):
            warnings.append(f"slot {slot}: the plan has {entry['sku']} but the projection has {ps['sku']}, "
                            "so its forecast is for a different product")
        product = entry.get("product") or same.get("name") or (pack.name if pack else None) or sku or "unknown"
        size = entry.get("size") or same.get("size") or (pack.size if pack else None)
        units = _opt_int(entry.get("units"))
        if units is None or units < 0:
            raise RoundError(f"{pname}: slot {slot} has no units")
        fill = _opt_int(ps.get("fill"))
        if fill is not None and fill != units:
            warnings.append(f"slot {slot}: the plan loads {units} units but the projection assumed {fill}")
        if "price_cents" in entry:
            price = _cents_field(entry["price_cents"], f"{pname}: slot {slot} price_cents")
        else:
            price = _cents(entry.get("price"), f"{pname}: slot {slot} price")
        problems: list[str] = []
        floor = most = expected = None
        legal_price = None
        if pack:
            floor = pricing.pack_floor_cents(pack.max_cents, pack.units_per_pack)
            price_problems = pricing.check_price(price, floor, CEILING_CENTS)
            legal_price = not price_problems
            problems.extend(r.message for r in price_problems)
            most = Fraction(units * pack.max_cents, pack.units_per_pack)
            expected = Fraction(units * pack.expected_cents, pack.units_per_pack)
        chart = capacity.from_size(size)
        cap = chart if chart is not None else (pack.column_capacity if pack else None)
        # Off the chart, the pack's figure is a count unless the projection says it was assumed.
        assumed = chart is None and cap is not None and (pack is None or pack.capacity_assumed)
        if cap is not None and units > cap:
            problems.append(f"{units} units but a column holds {cap}" + (" (assumed, not counted)" if assumed else ""))
        check = capacity.fill_check(units, None if assumed else cap)
        why = entry.get("why") or entry.get("reason")
        if check and check != capacity.NOT_COUNTED:
            warnings.append(f"slot {slot}: {check}" + (f". {why}" if why else ""))
        projected = {scen: _projected(rows[slot]) for scen, rows in scen_rows.items() if slot in rows}
        for scen, rows in scen_rows.items():
            pc = _opt_int(rows.get(slot, {}).get("price_cents"))
            if pc is not None and pc != price:
                warnings.append(f"slot {slot}: the plan prices it at {format_cents(price)} but the {scen} projection "
                                f"used {format_cents(pc)}")
        if projection and slot not in proj_slots and not projected:
            warnings.append(f"slot {slot}: not in the projection")
        slots[slot] = Slot(
            slot=slot, product=product, size=size, sku=sku,
            category=(pack.category if pack else None) or ps.get("cat"),
            units=units, price_cents=price, why=why,
            needs_request=entry.get("needs_request") if isinstance(entry.get("needs_request"), bool) else None,
            interloper=ps.get("interloper") if isinstance(ps.get("interloper"), bool) else None,
            floor_cents=floor, legal_price=legal_price, capacity=cap, capacity_assumed=assumed,
            most_cents=most, expected_cents=expected, refusals=tuple(problems), projected=projected,
            fill_check=check)
    missing = [s for s in ours if s not in slots]
    if missing:
        warnings.append(f"no product for slot{'s' if len(missing) > 1 else ''} {', '.join(missing)}")

    # The fill: whole packs per product, leftovers to the closet.
    purchases: list[Purchase] = []
    fill_expected = fill_most = None
    if packs:
        loaded: dict[str, int] = {}
        for s in slots.values():
            if s.sku in packs:
                loaded[s.sku] = loaded.get(s.sku, 0) + s.units
            else:
                warnings.append(f"slot {s.slot}: {s.product} has no pack price in the projection, so its cost is unknown")
        for sku, units in loaded.items():
            p = packs[sku]
            n = math.ceil(units / p.units_per_pack)
            purchases.append(Purchase(sku, p.name, units, n, p.units_per_pack, n * p.units_per_pack - units,
                                      n * p.expected_cents, n * p.max_cents))
        purchases.sort(key=lambda p: -p.most_cents)
        # Each slot's share of its product's packs, closet leftovers included, so the shares add up to the fill.
        bought = {p.sku: p for p in purchases}
        for s in list(slots.values()):
            if s.sku in bought:
                b = bought[s.sku]
                share = Fraction(s.units, b.units_loaded) if b.units_loaded else Fraction(0)
                slots[s.slot] = replace(s, most_cents=share * b.most_cents, expected_cents=share * b.expected_cents)
        if all(s.sku in packs for s in slots.values()):
            fill_expected = sum(p.expected_cents for p in purchases)
            fill_most = sum(p.most_cents for p in purchases)
            for label, mine, theirs in (("expected", fill_expected, projection.get("fill_expected_cents")),
                                        ("most-we-pay", fill_most, projection.get("fill_most_cents"))):
                if isinstance(theirs, int) and theirs != mine:
                    warnings.append(f"fill {label}: the projection says {format_cents(theirs)}, "
                                    f"this page counts {format_cents(mine)}")
        for p in projection.get("purchase") or []:
            mine = next((q for q in purchases if q.sku == p.get("sku")), None)
            if mine and isinstance(p.get("packs"), int) and p["packs"] != mine.packs:
                warnings.append(f"{mine.product}: the projection buys {p['packs']} packs, this page counts {mine.packs}")

    notes = {k: plan[k] for k in ("fallbacks", "asks", "checks", "expect", "note", "truncated") if plan.get(k)}
    _check_note_slots(notes.get("fallbacks"), pname, ours, outside)
    for k in ("problems", "needs_hand_count", "needs_new_source"):
        if projection.get(k):
            notes[k] = projection[k]
    if isinstance(plan.get("totals"), dict) and plan["totals"].get("cap_note"):
        notes["cap_note"] = plan["totals"]["cap_note"]
    checked = Round(
        id=round_id, folder=folder, plan_file=pname, projection_file=projection_file, saved_at=saved_at,
        slots=dict(sorted(slots.items(), key=lambda kv: _slot_key(kv[0]))),
        catalog_requests=[r for r in plan.get("catalog_requests") or [] if isinstance(r, dict)],
        purchases=purchases, fill_expected_cents=fill_expected, fill_most_cents=fill_most,
        totals=totals, weeks=weeks, window=projection.get("window"),
        assumptions=projection.get("assumptions") or {}, notes=notes, warnings=warnings, ours=ours)
    if outside:
        slot, name = next(iter(outside.items()))
        raise OtherSlots(f"{name}: slot {slot} isn't in our current assignment; "
                         f"a round may only place products in those {len(ours)} slots", checked, projection,
                         tuple(sorted(outside, key=_slot_key)))
    return checked


def load_all(root: Path, ours: tuple[str, ...]) -> list[Round]:
    """Every round under `root`, each checked against `ours` (our slots, from `vending.slots.our_slots`)."""
    return [load_round(rid, plan, projection, ours) for rid, plan, projection in discover(root)]


@dataclass(frozen=True)
class SetAside:
    """A round placed in slots outside our assignment, so its slots aren't shown. Its totals are the
    projection file's own figures, read as written and never recomputed."""
    id: str
    plan_file: str
    saved_at: datetime
    outside: tuple[str, ...]             # the selections it names that aren't ours, shelf then column
    reason: str                          # the loader's message
    fill_most_cents: int | None
    gross_profit_cents: dict[str, int]   # by scenario, where the projection has one


def _set_aside(error: OtherSlots) -> SetAside:
    """From the round as the loader checked it: its totals came through the same parser as a shown round's,
    and its selections outside ours are the ones that set it aside."""
    r, projection = error.checked, error.projection
    gp = {scen: t.gross_profit_cents for scen, t in r.totals.items() if t.gross_profit_cents is not None}
    return SetAside(id=r.id, plan_file=r.plan_file, saved_at=r.saved_at, outside=error.outside, reason=str(error),
                    fill_most_cents=_opt_int(projection.get("fill_most_cents")), gross_profit_cents=gp)


def load_rounds(root: Path, ours: tuple[str, ...]) -> tuple[list[Round], list[SetAside]]:
    """The rounds under `root` placed in our slots, and those set aside because they name other slots.

    Rounds made before the 20 Nov redraw name the slots we had then. They can't be shown slot by slot
    (the page shows values only in our slots), but they shouldn't stop the page either. Any other problem
    with a file still raises RoundError, in a set-aside round too: each round is checked in full first."""
    shown, aside = [], []
    for rid, plan, projection in discover(root):
        try:
            shown.append(load_round(rid, plan, projection, ours))
        except OtherSlots as error:
            aside.append(_set_aside(error))
    return shown, aside


@dataclass(frozen=True)
class Change:
    slot: str
    labels: tuple[str, ...]      # "new product", "price", "units"
    before: Slot | None
    after: Slot | None


def changes(after: Round, before: Round | None) -> dict[str, Change]:
    """What changed in each slot from `before` to `after`. Empty when there is nothing to compare."""
    if before is None:
        return {}
    out = {}
    for slot in after.ours:
        a, b = after.slots.get(slot), before.slots.get(slot)
        labels = []
        if (a is None) != (b is None):
            labels.append("added" if a else "dropped")
        elif a and b:
            if (a.sku != b.sku) if a.sku and b.sku else (_name(a.product) != _name(b.product)):
                labels.append("new product")
            if a.price_cents != b.price_cents:
                labels.append("price")
            if a.units != b.units:
                labels.append("units")
        if labels:
            out[slot] = Change(slot, tuple(labels), b, a)
    return out


def _name(product: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", product.lower()))


def same_assumptions(a: Round, b: Round) -> bool:
    return bool(a.assumptions) and a.assumptions == b.assumptions


def format_cents(cents: int) -> str:
    """Format an integer number of cents as exact dollars."""
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents // 100:,}.{cents % 100:02d}"
