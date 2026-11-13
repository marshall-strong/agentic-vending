"""Score a plan: hard checks first, then the outcome against the following week.

Hard checks pass, fail or skip. A skip means the replay doesn't have what the
check needs (no unit costs yet, no balance yet); it never counts as a pass.

The outcome score treats what actually sold in each slot the following week as
that slot's demand, then asks how the plan would have done against it. That
rests on the assumptions in `DEMAND_ASSUMPTIONS`; they're printed under every
score table and listed in docs/week-replayer.md.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from fractions import Fraction

from vending.pricing import PRICE_STEP_CENTS, _dollars, check_price, floor_cents
from vending.replay.deciders import Plan, SlotPlan, check_plan
from vending.replay.state import SlotWeek, WeekState
from vending.slots import SLOTS
from vending.tools.sales_history import tax_cents

# The team's ceiling, decided 12 Nov ("Decide as a team" in tool-list.md); the planner's default too.
PRICE_CEILING_CENTS = 650
MIDWEEK_FEE_CENTS = 2500

PASS, FAIL, SKIP = "pass", "fail", "skip"

DEMAND_ASSUMPTIONS = (
    "Demand in a slot = units it actually sold the following week. Sales are a floor on demand: "
    "if the slot ran out, more would have sold, and without stock counts we can't see that.",
    "Demand doesn't depend on price. We only see sales at the prices that were really set, so a "
    "plan that changes a price is scored as if the same number would have sold. Those slots are noted.",
    "A new product in a slot has no sales to score against, so that slot is left unscored.",
    "Where stock is unknown, the rail holds exactly fill_to after the visit, and every unit is bought.",
    "Margin = revenue ex tax - units sold x actual unit cost. Unsold units keep their cost "
    "(the closet is valued at what we paid), so they cost nothing in the score; they're counted instead.",
)


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


@dataclass(frozen=True)
class SlotOutcome:
    slot: str
    price_cents: int | None
    available: int | None       # on the rail during the scoring week
    demand: int | None
    sold: int | None = None
    stockout: int | None = None  # demand the rail couldn't meet (a floor: see the assumptions)
    unsold: int | None = None
    revenue_ex_tax_cents: int | None = None
    margin_cents: Fraction | None = None
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Totals:
    scored: int
    stockout_units: int
    stockout_slots: int
    unsold_units: int
    revenue_ex_tax_cents: int
    margin_cents: Fraction | None   # None unless every scored slot has a unit cost
    fee_cents: int


@dataclass(frozen=True)
class Score:
    checks: tuple[Check, ...]
    plan: Plan | None
    slots: tuple[SlotOutcome, ...] = ()
    totals: Totals | None = None
    notes: tuple[str, ...] = field(default=())

    @property
    def passed(self) -> bool:
        return all(c.status != FAIL for c in self.checks)


def score(state: WeekState, answer: object, next_week: WeekState | None,
          writes: int | None = None) -> Score:
    """Check `answer` (a Plan or raw JSON), then score it against `next_week`.

    `writes` is how many write tool calls were logged while the decider ran
    (None if not measured).
    """
    plan, refusals = check_plan(answer)
    valid = Check("valid plan", FAIL if refusals else PASS,
                  "; ".join(_refusal(r) for r in refusals) or "All 20 slots once, every field well formed.")
    writes_check = _no_writes(writes)
    if plan is None:
        skipped = tuple(Check(name, SKIP, "No valid plan to check.") for name, _ in CHECKS)
        return Score(checks=(valid, *skipped, writes_check), plan=None)
    checks = (valid, *(fn(state, plan) for _, fn in CHECKS), writes_check)
    if next_week is None:
        return Score(checks=checks, plan=plan,
                     notes=("No polls for the following week, so the outcome can't be scored.",))
    outcomes = tuple(_outcome(state, plan, plan.slot(slot), next_week.slot(slot)) for slot in SLOTS)
    return Score(checks=checks, plan=plan, slots=outcomes, totals=_totals(plan, outcomes))


# --- Hard checks -------------------------------------------------------------

def _loads(state: WeekState, plan: Plan, sp: SlotPlan) -> bool:
    """Whether the plan puts units on this rail inside the scoring window."""
    return not sp.keep and not (state.midweek and plan.visit == "scheduled")


def _switches(sp: SlotPlan, s: SlotWeek) -> bool:
    return not sp.keep and sp.product is not None and sp.product != s.product


def _unit_cost(sp: SlotPlan, s: SlotWeek) -> Fraction | None:
    """What a unit for this slot costs, if we know. A new product's cost is unknown."""
    return None if _switches(sp, s) or s.unit_cost_cents is None else Fraction(s.unit_cost_cents)


def check_catalog(state: WeekState, plan: Plan) -> Check:
    named = {sp.product for sp in plan.slots if not sp.keep and sp.product is not None}
    if not named:
        return Check("catalog", PASS, "No products named; every slot keeps what it has.")
    rejected = sorted(named & state.rejected)
    if rejected:
        return Check("catalog", FAIL, f"Catalog request was refused for: {', '.join(rejected)}.")
    if state.catalog is None:
        return Check("catalog", SKIP, "Catalog unknown for this week.")
    unknown = sorted(named - state.catalog)
    if unknown:
        return Check("catalog", FAIL, f"Not in the active catalog: {', '.join(unknown)}. Request it and wait.")
    return Check("catalog", PASS, f"{len(named)} named products, all in the catalog.")


def check_prices(state: WeekState, plan: Plan) -> Check:
    problems, unchecked = [], 0
    for sp in plan.slots:
        if sp.keep:
            continue
        cost = _unit_cost(sp, state.slot(sp.slot))
        unchecked += cost is None
        # With no unit cost, a price must still be above zero: free drinks never clear a floor.
        floor = PRICE_STEP_CENTS if cost is None else floor_cents(cost)
        problems += check_price(sp.price_cents, floor, PRICE_CEILING_CENTS, sp.slot)
    if problems:
        return Check("prices", FAIL, "; ".join(_refusal(r) for r in problems))
    detail = f"5-cent steps, above zero, at most the ceiling of {_dollars(PRICE_CEILING_CENTS)}"
    if unchecked:
        return Check("prices", SKIP, f"{detail}; but the floor isn't checked on {unchecked} slots (unit cost unknown).")
    return Check("prices", PASS, f"{detail}; above every floor.")


def check_balance(state: WeekState, plan: Plan) -> Check:
    if state.balance_cents is None:
        return Check("balance", SKIP, "Balance unknown: it needs the ledger.")
    cost, unknown = Fraction(MIDWEEK_FEE_CENTS if plan.visit == "midweek" else 0), []
    for sp in plan.slots:
        s = state.slot(sp.slot)
        if sp.keep:
            continue
        bought = sp.fill_to if s.stock is None or _switches(sp, s) else max(sp.fill_to - s.stock, 0)
        unit = _unit_cost(sp, s)
        if bought and unit is None:
            unknown.append(sp.slot)
        elif bought:
            cost += bought * unit
    if unknown:
        return Check("balance", SKIP, f"Unit cost unknown for slots {', '.join(unknown)}.")
    spend = _dollars(int(cost + Fraction(1, 2)))
    if cost > state.balance_cents:
        return Check("balance", FAIL, f"Costs {spend} at actual unit cost, over the {_dollars(state.balance_cents)} balance.")
    return Check("balance", PASS, f"Costs {spend} of {_dollars(state.balance_cents)} at actual unit cost.")


# Each check takes the state and a valid plan. The restock planner joins here once #13 merges.
CHECKS: tuple[tuple[str, Callable[[WeekState, Plan], Check]], ...] = (
    ("catalog", check_catalog),
    ("prices", check_prices),
    ("balance", check_balance),
)


def _no_writes(writes: int | None) -> Check:
    if writes is None:
        return Check("no writes", SKIP, "Not measured: this decider has no database access.")
    if writes:
        return Check("no writes", FAIL, f"{writes} write tool calls were logged during the decision "
                     "(by anything: run replays when nothing else is writing).")
    return Check("no writes", PASS, "No write tool calls were logged during the decision.")


def _refusal(r) -> str:
    return f"{r.slot}: {r.message}" if r.slot else r.message


# --- Outcome -----------------------------------------------------------------

def _outcome(state: WeekState, plan: Plan, sp: SlotPlan, actual: SlotWeek | None) -> SlotOutcome:
    s = state.slot(sp.slot)
    price = s.price_cents if sp.keep else sp.price_cents
    if _loads(state, plan, sp):
        available = sp.fill_to if s.stock is None or _switches(sp, s) else max(sp.fill_to, s.stock)
    else:
        available = s.stock
    notes = []
    if not sp.keep and not _loads(state, plan, sp):
        notes.append("the scheduled visit lands after this window")
    demand = None if actual is None else actual.units_sold
    if _switches(sp, s) and s.product is None:
        demand = None
        notes.append(f"product unknown this week, so {sp.product} may be a switch: unscored")
    elif _switches(sp, s):
        demand = None
        notes.append("new product: no sales to score it against")
    elif demand is None:
        notes.append("no sales figure for the following week")
    if available is None:
        notes.append("stock unknown")
    if actual is not None and actual.price_cents is not None and price is not None and price != actual.price_cents:
        notes.append(f"priced {_dollars(price)}, {_dollars(actual.price_cents)} was charged: same demand assumed")
    if actual is not None and actual.price_changed:
        notes.append("price changed during the following week")
    if demand is None or available is None or price is None:
        return SlotOutcome(sp.slot, price, available, demand, notes=tuple(notes))
    sold = min(demand, available)
    revenue = sold * price
    ex_tax = revenue - tax_cents(revenue)
    cost = _unit_cost(sp, s)
    return SlotOutcome(
        sp.slot, price, available, demand,
        sold=sold,
        stockout=demand - sold,
        unsold=available - sold,
        revenue_ex_tax_cents=ex_tax,
        margin_cents=None if cost is None else ex_tax - sold * cost,
        notes=tuple(notes),
    )


def _totals(plan: Plan, outcomes: tuple[SlotOutcome, ...]) -> Totals:
    scored = [o for o in outcomes if o.sold is not None]
    fee = MIDWEEK_FEE_CENTS if plan.visit == "midweek" else 0
    margins = [o.margin_cents for o in scored]
    return Totals(
        scored=len(scored),
        stockout_units=sum(o.stockout for o in scored),
        stockout_slots=sum(1 for o in scored if o.stockout),
        unsold_units=sum(o.unsold for o in scored),
        revenue_ex_tax_cents=sum(o.revenue_ex_tax_cents for o in scored),
        margin_cents=None if not scored or None in margins else sum(margins) - fee,
        fee_cents=fee,
    )
