"""Test cases from the team's own lists, as built weeks a decider can be scored on.

Each case is a question from the 8 Oct whiteboard or "Decide as a team" in
docs/planning/tool-list.md, or a stress case from issue #26. Every number in
them is made up to pose the question; none is a real sale, cost or balance.
Unlike a replayed week, a case can know stock, unit cost, the balance and the
catalog, so every hard check gets exercised.

`good_answer` is what a person grading the plan should look for. The outcome
score can't judge everything: a new product has no sales to score against.

    python -m vending.replay --cases --decider baseline
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from vending.replay.state import EASTERN, SlotWeek, WeekState
from vending.slots import SLOTS

WHITEBOARD = "8 Oct whiteboard"
TEAM = "Decide as a team (tool-list.md)"
STRESS = "Stress case (issue #26)"

FRIDAY = datetime(2026, 10, 2, tzinfo=EASTERN)
CATALOG = frozenset({"COKE-20OZ", "DIET-COKE-20OZ", "MTDEW-20OZ", "DASANI-20OZ", "GATORADE-20OZ", "CELSIUS-12OZ"})
# A steady seller: $2.50, 7 a week, 2 left on the rail, $1.00 a unit.
STEADY = dict(price_cents=250, units_sold=7, stock=2, unit_cost_cents=100)


@dataclass(frozen=True)
class Case:
    name: str
    source: str
    question: str
    state: WeekState
    next_week: WeekState | None
    good_answer: str


def _week(start: datetime = FRIDAY, days: int = 7, default: dict | None = None,
          slots: dict[str, dict] | None = None, **fields) -> WeekState:
    """A week where every slot is `default`, except the ones in `slots`."""
    default = STEADY if default is None else default
    rows = []
    for slot in SLOTS:
        row = {**default, **(slots or {}).get(slot, {})}
        if row.get("units_sold") is not None:
            row["units_per_day"] = (Decimal(row["units_sold"]) / days).quantize(Decimal("0.01"), ROUND_HALF_UP)
        rows.append(SlotWeek(slot, **row))
    return WeekState(start=start, end=start + timedelta(days=days), slots=tuple(rows),
                     **{"balance_cents": 60000, "catalog": CATALOG, **fields})


def _next(state: WeekState, days: int = 7, default: dict | None = None,
          slots: dict[str, dict] | None = None) -> WeekState:
    """The week after `state`: what each slot actually sold, at what price."""
    default = {"price_cents": 250, "units_sold": 7} if default is None else default
    return _week(state.end, days, default, slots, balance_cents=None, catalog=None)


def all_cases() -> list[Case]:
    cases = []

    s = _week(slots={"15": dict(units_sold=0, stock=9)},
              notes=("Slot 15 also sold none the week before.",))
    cases.append(Case(
        "slow-seller", f"{TEAM}; {WHITEBOARD}: move product if not selling well",
        "When do we pull a slow seller? Slot 15 sold nothing two weeks running and has 9 on the rail.",
        s, _next(s, slots={"15": dict(units_sold=0)}),
        "Switches slot 15 to a product that sells, or cuts its price, and loads nothing more into it. "
        "The reason names the threshold used."))

    empty = dict(price_cents=None, units_sold=None, stock=0, unit_cost_cents=None)
    s = _week(default=empty, notes=("A cold start: nothing loaded yet, no sales history.",))
    cases.append(Case(
        "fills-first", TEAM,
        "What goes in our 20 slots first once the catalog has products?",
        s, _next(s, default=dict(price_cents=None, units_sold=None)),
        "Picks catalog products for every slot with a price above each floor, within the $600. "
        "Not scoreable on outcome: there are no sales yet. Grade the reasons."))

    s = _week(days=4, midweek=True,
              default=dict(price_cents=250, units_sold=4, stock=8, unit_cost_cents=100),
              slots={"15": dict(units_sold=18, stock=0), "16": dict(units_sold=18, stock=0)},
              notes=("Taken Tuesday: slots 15 and 16 sold out today. The next free visit is Friday.",))
    cases.append(Case(
        "midweek-visit", f"{WHITEBOARD}: request service visit ($25)",
        "Is a $25 midweek visit worth it? Two fast sellers sold out on Tuesday.",
        s, _next(s, days=3, default=dict(price_cents=250, units_sold=3),
                 slots={"15": dict(units_sold=13), "16": dict(units_sold=13)}),
        "Asks for a midweek visit only if the margin it saves beats $25. Here refilling 15 and 16 "
        "saves about $34 before the fee, so a visit that refills them and keeps the rest is right."))

    s = _week(notes=("We asked for PICKLE-POTION-12OZ to be added; the request is still pending.",))
    cases.append(Case(
        "catalog-request", f"{WHITEBOARD}: new product research, request product",
        "Can we stock a product whose catalog request is still pending?",
        s, _next(s),
        "Doesn't plan PICKLE-POTION-12OZ until it's approved; plans the rest from the catalog."))

    late_nov = datetime(2026, 11, 20, tzinfo=EASTERN)
    s = _week(start=late_nov, catalog=CATALOG | {"SEASONAL-CIDER-12OZ"},
              slots={"18": dict(units_sold=1, stock=6)},
              notes=("SEASONAL-CIDER-12OZ is new in the catalog. Slot 18 is the slowest seller.",))
    cases.append(Case(
        "holiday-drinks", f"{TEAM}; {WHITEBOARD}",
        "Do holiday drinks get a slot, and which one gives way?",
        s, _next(s, slots={"18": dict(units_sold=1)}),
        "If it tries the seasonal drink, it puts it in the slowest slot (18), prices it above its "
        "floor and loads few. Not scoreable on outcome: the drink has no sales yet."))

    s = _week(slots={"15": dict(units_sold=10, stock=0)},
              notes=("Slot 15 sold out on Tuesday; it sold 10 in 4 days, then nothing.",))
    cases.append(Case(
        "tuesday-sellout", STRESS,
        "A slot sold out on Tuesday. Last week's 10 sales hide the real demand.",
        s, _next(s, slots={"15": dict(units_sold=17)}),
        "Reads the sell-out: plans slot 15 at its rate before it ran out (2.5 a day, about 17 a week), "
        "not at the 10 it happened to sell."))

    s = _week(balance_cents=800)
    cases.append(Case(
        "low-balance", STRESS,
        "Only $8.00 is left. A full restock costs $100.",
        s, _next(s),
        "Stays within $8.00: restocks only the best sellers, or keeps everything."))

    s = _week(slots={"15": dict(unit_cost_cents=240)},
              notes=("Slot 15's last pack cost $2.40 a unit, so its floor is $2.60. It's priced $2.50.",))
    cases.append(Case(
        "price-below-floor", STRESS,
        "Keeping slot 15 at $2.50 would break the floor.",
        s, _next(s),
        "Raises slot 15 to at least $2.60, or keeps it without buying more at that cost."))

    s = _week(rejected=frozenset({"MONSTER-ULTRA-16OZ"}),
              notes=("Our catalog request for MONSTER-ULTRA-16OZ was refused.",))
    cases.append(Case(
        "rejected-request", STRESS,
        "A catalog request was refused. Does the plan still stock it?",
        s, _next(s),
        "Doesn't plan MONSTER-ULTRA-16OZ anywhere."))

    unknown = dict(price_cents=None, units_sold=None, stock=None, unit_cost_cents=None)
    odd = {slot: unknown for slot in SLOTS[::2]}
    odd["15"] = dict(price_cents=250, units_sold=None, stock=None, unit_cost_cents=None, price_changed=None)
    s = _week(slots=odd, balance_cents=None, catalog=None,
              notes=("Half the slots have no figures: a replaced board, a gap in the polls.",))
    cases.append(Case(
        "nonsense-input", STRESS,
        "Half the inputs are unknown. Does the decider invent numbers?",
        s, _next(s, slots={slot: dict(units_sold=None) for slot in SLOTS[::3]}),
        "Keeps every slot it has no figures for, and says so; never treats unknown as zero."))
    return cases
