"""Decision-makers: anything that turns a replayed week into a plan for each slot.

A decider has a `name` and a `decide(state)` method. It returns a `Plan`, or a
plain dict in the JSON shape below (what a model or a hand-written file gives).
Either way the plan goes through `check_plan` before it is scored, so a
nonsense answer fails the "valid plan" check instead of crashing the replay.

    {"visit": "scheduled",
     "reason": "one line for the whole plan",
     "slots": [
       {"slot": "15", "fill_to": 7, "sale_price": "2.50", "reason": "sold 7 last week"},
       {"slot": "16", "keep": true, "reason": "no sales figure, leave it"},
       {"slot": "17", "product": "MTDEW-20OZ", "fill_to": 6, "sale_price": "2.25", "reason": "..."}
     ]}

The fields follow the desired state in docs/planning/tool-list.md: `fill_to`
is units on the rail after the visit, `sale_price` includes tax, `product`
left out means whatever is in the slot now, and `keep` means no change.

The restock planner (PR #13, `vending.planner.plan_restock`) is not a decider:
it checks and prices a desired state, it doesn't choose one. Once it merges it
plugs into `scoring.CHECKS` as one more hard check, converting a `Plan` to its
`DesiredState`. A decider that wants the planner's help can call it itself.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from vending.pricing import Refusal, to_cents
from vending.replay.state import WeekState
from vending.slots import SLOTS

VISITS = ("scheduled", "midweek")
# The baseline never plans an empty rail: leaving a slot empty waits on David.
MIN_FILL = 1
# Placeholder sanity limit, not a capacity: rails hold "as many as physically fit"
# (build-plan.md). The planner now takes capacity from each product's package
# (`vending.capacity`, 7 to 11 a column), but the replay cases carry no package
# sizes, so this stays a sanity limit. It stops a plan like
# fill_to: 10**30 from being scored as if it made sense.
MAX_FILL = 50


@dataclass(frozen=True)
class SlotPlan:
    slot: str
    reason: str
    keep: bool = False
    fill_to: int | None = None          # units on the rail after the visit
    price_cents: int | None = None      # shelf price, tax included
    product: str | None = None          # None: whatever is in the slot now


@dataclass(frozen=True)
class Plan:
    slots: tuple[SlotPlan, ...]
    visit: str = "scheduled"
    reason: str = ""
    # Passed through unchecked for the restock planner, which validates sourcing.
    sourcing: tuple = ()

    def slot(self, slot: str) -> SlotPlan | None:
        return next((s for s in self.slots if s.slot == slot), None)


class Decider(Protocol):
    name: str
    # True if it can reach the database or the MCP tools (a model harness will).
    # Only then is the "no writes" check measured; otherwise it's skipped.
    uses_database: bool

    def decide(self, state: WeekState) -> Plan | Mapping: ...


class Baseline:
    """Restock each slot to last week's sales, at the same price. Never moves a product.

    Deliberately simple: it's the bar any other decider should clear.
    """
    name = "baseline"
    uses_database = False

    def decide(self, state: WeekState) -> Plan:
        slots = []
        for s in state.slots:
            if s.units_sold is None or s.price_cents is None:
                slots.append(SlotPlan(s.slot, keep=True,
                                      reason="No sales figure or price for last week, so leave it as it is."))
                continue
            fill = max(s.units_sold, MIN_FILL)
            why = f"Sold {s.units_sold} last week; restock to {fill} at the same price."
            if s.units_sold < MIN_FILL:
                why = f"Sold none last week; keep {MIN_FILL} on the rail rather than leave it empty."
            slots.append(SlotPlan(s.slot, fill_to=fill, price_cents=s.price_cents, reason=why))
        return Plan(tuple(slots), reason="Restock to last week's sales; no price or product changes.")


class FromFile:
    """A fixed plan from a JSON file, for a hand-written plan or a model's saved answer."""

    def __init__(self, path: str):
        self.path = Path(path)
        self.name = f"file:{path}"
        self.uses_database = False

    def decide(self, state: WeekState) -> Mapping | str:
        """The parsed plan. Text that isn't JSON comes back as text, so it fails "valid plan"."""
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as error:
            raise ValueError(f"Can't read the plan file {self.path}: {error.strerror}.") from None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text


def get_decider(name: str) -> Decider:
    if name == "baseline":
        return Baseline()
    if name.startswith("file:"):
        return FromFile(name[len("file:"):])
    raise ValueError(f"Unknown decider {name!r}. Use baseline, or file:PATH for a JSON plan.")


def check_plan(answer: object) -> tuple[Plan | None, list[Refusal]]:
    """The plan, or every reason it isn't a valid one. Never raises on bad input."""
    if isinstance(answer, Plan):
        plan, refusals = answer, []
    elif isinstance(answer, Mapping):
        plan, refusals = _parse(answer)
    elif isinstance(answer, str):
        start = " ".join(answer.split())[:160] or "(empty)"
        return None, [Refusal("not_a_plan", f"Expected a JSON plan, got text: {start}")]
    else:
        return None, [Refusal("not_a_plan", f"Expected a plan or a JSON object, got {type(answer).__name__}.")]
    if plan is None:
        return None, refusals
    refusals += _structure(plan)
    return (None, refusals) if refusals else (plan, [])


def _parse(raw: Mapping) -> tuple[Plan | None, list[Refusal]]:
    refusals: list[Refusal] = []
    rows = raw.get("slots")
    if not isinstance(rows, list):
        return None, [Refusal("not_a_plan", "`slots` must be a list with one entry per slot.")]
    slots = []
    for i, row in enumerate(rows):
        if not isinstance(row, Mapping):
            refusals.append(Refusal("not_a_plan", f"slots[{i}] is not an object."))
            continue
        slot = row.get("slot")
        if not isinstance(slot, str):
            refusals.append(Refusal("slot_list", f"slots[{i}] has no slot name."))
            continue
        price, raw_price = None, row.get("sale_price")
        if raw_price is not None:
            # Text only: a number is ambiguous (to_cents reads an int as cents) and a float inexact.
            try:
                if not isinstance(raw_price, str):
                    raise ValueError(f"sale_price must be text, not {raw_price!r}")
                price = to_cents(raw_price)
            except (ValueError, ArithmeticError) as error:
                refusals.append(Refusal("bad_price", f"{error}. Give a price as text, such as \"2.50\".", slot))
                continue
        if "product" in row and row["product"] is None and not row.get("keep"):
            refusals.append(Refusal("empty_slot", "product: null empties the slot, which waits on David. "
                                    "Leave product out to keep the current one.", slot))
            continue
        fill = row.get("fill_to")
        if fill is not None and (isinstance(fill, bool) or not isinstance(fill, int)):
            refusals.append(Refusal("bad_fill", f"fill_to must be a whole number, not {fill!r}.", slot))
            continue
        keep = row.get("keep", False)
        reason, product = row.get("reason", ""), row.get("product")
        if not isinstance(keep, bool) or not isinstance(reason, str) or not (product is None or isinstance(product, str)):
            refusals.append(Refusal("not_a_plan", "keep must be true or false; reason and product must be text.", slot))
            continue
        slots.append(SlotPlan(slot, reason=reason, keep=keep, fill_to=fill, price_cents=price, product=product))
    visit, reason, sourcing = raw.get("visit", "scheduled"), raw.get("reason", ""), raw.get("sourcing", [])
    if not isinstance(reason, str):
        refusals.append(Refusal("not_a_plan", "reason must be text."))
        reason = ""
    if not isinstance(sourcing, list):
        refusals.append(Refusal("not_a_plan", "sourcing must be a list."))
        sourcing = []
    return Plan(tuple(slots), visit=visit, reason=reason, sourcing=tuple(sourcing)), refusals


def _structure(plan: Plan) -> list[Refusal]:
    refusals = []
    if plan.visit not in VISITS:
        refusals.append(Refusal("bad_visit", f"visit must be one of {', '.join(VISITS)}, not {plan.visit!r}."))
    names = [s.slot for s in plan.slots]
    missing = [s for s in SLOTS if s not in names]
    twice = sorted({s for s in names if names.count(s) > 1})
    foreign = sorted(set(names) - set(SLOTS))
    for label, found in (("missing", missing), ("listed twice", twice), ("not ours", foreign)):
        if found:
            refusals.append(Refusal("slot_list", f"Slots {label}: {', '.join(found)}. List all 20 once."))
    for s in plan.slots:
        if not s.reason.strip():
            refusals.append(Refusal("no_reason", "Every slot needs a short reason.", s.slot))
        if s.keep:
            continue
        if s.fill_to is None or s.price_cents is None:
            refusals.append(Refusal("not_a_plan", "Give fill_to and sale_price, or keep: true. "
                                    "(fill_to can't default to capacity: there is no slot capacity yet.)", s.slot))
        elif not 0 <= s.fill_to <= MAX_FILL:
            refusals.append(Refusal("bad_fill", f"fill_to must be 0 to {MAX_FILL}, not {s.fill_to}.", s.slot))
        if s.product is not None and not s.product.strip():
            refusals.append(Refusal("bad_product", "product is blank; leave it out to keep the current one.", s.slot))
    return refusals
