"""`preview_desired_state` (tool #5): plan a desired state without submitting it.

Runs `plan_restock` on live data (`vending.planning`) and returns the lines the
request would carry, the directive the worker would read, the cost and the
balance left, every slot's price floor and how it was worked out, warnings,
and every refusal at once, so the agent can fix the whole plan in one turn.

`success` is whether the plan could be submitted as it stands. A plan with
refusals still comes back with everything that could be worked out.
"""

from __future__ import annotations

from datetime import datetime

import psycopg
from pydantic import BaseModel

from vending import planner, planning
from vending.bridge import Bridge
from vending.ledger import money
from vending.tools import RefusalOut, Reply
from vending.tools.desired_state import DesiredState


class PlannedLine(BaseModel):
    slot: str
    action: str                     # restock | switch
    product: str
    sale_price: str
    fill_to: int
    units_staying: int
    units_to_load: int
    source: str                     # purchase | back_stock
    pulled_product: str | None = None
    units_pulled: int = 0
    api: dict                       # exactly what the request would carry for this slot


class LineCost(BaseModel):
    slot: str
    expected: str
    max: str


class Cost(BaseModel):
    expected: str
    max: str
    visit_fee: str
    balance: str
    balance_left_at_max: str
    lines: list[LineCost]


class FloorPart(BaseModel):
    source: str
    unit_cost: str
    floor: str


class SlotFloor(BaseModel):
    slot: str
    floor: str
    parts: list[FloorPart]


class PlanReply(Reply):
    ok: bool = False
    plan_hash: str | None = None
    off_cycle: bool = False
    as_of: datetime | None = None
    prices_as_of: datetime | None = None
    lines: list[PlannedLine] = []
    directive: str | None = None
    cost: Cost | None = None
    floors: list[SlotFloor] = []
    warnings: list[str] = []
    refusals: list[RefusalOut] = []
    notes: list[str] = []


def describe(plan: planner.Plan, gathered: planning.Gathered) -> PlanReply:
    """A `Plan` as the agent sees it. Pure: no database."""
    refusals = [RefusalOut(code=r.code, message=r.message, slot=r.slot) for r in plan.refusals]
    if plan.ok:
        message = (f"Plan OK: {len(plan.lines)} lines, max cost {money(plan.cost.max_cents)}, leaving "
                   f"{money(plan.cost.balance_left_at_max_cents)} of the spendable balance. "
                   "Submit it unchanged with submit_desired_state.")
    else:
        message = (f"Plan refused: {len(refusals)} problem{'s' if len(refusals) != 1 else ''} "
                   f"({', '.join(sorted({r.code for r in refusals}))}). Fix them all and preview again.")
    return PlanReply(
        success=plan.ok,
        message=message,
        count=len(plan.lines),
        ok=plan.ok,
        plan_hash=plan.plan_hash,
        off_cycle=plan.off_cycle,
        as_of=gathered.now,
        prices_as_of=gathered.price_poll_at,
        lines=[_line(ln) for ln in plan.lines],
        directive=plan.directive or None,
        cost=None if plan.cost is None else _cost(plan.cost),
        floors=[SlotFloor(slot=f.slot, floor=money(f.floor_cents),
                          parts=[FloorPart(source=p.source, unit_cost=_fraction(p.unit_cost), floor=money(p.floor_cents))
                                 for p in f.parts])
                for f in plan.floors],
        warnings=list(plan.warnings),
        refusals=refusals,
        notes=list(gathered.notes),
    )


def preview_desired_state(conn: psycopg.Connection, desired: DesiredState, bridge: Bridge,
                          now: datetime | None = None) -> PlanReply:
    try:
        plan, gathered = planning.plan(conn, desired, bridge, now)
    except planning.NotReady as blocked:
        return PlanReply(success=False, message=f"Can't plan yet: {blocked}")
    return describe(plan, gathered)


def _line(ln: planner.Line) -> PlannedLine:
    return PlannedLine(
        slot=ln.slot, action=ln.action, product=ln.product, sale_price=money(ln.sale_price_cents),
        fill_to=ln.fill_to, units_staying=ln.units_staying, units_to_load=ln.units_to_load,
        source=ln.source, pulled_product=ln.pulled_product, units_pulled=ln.units_pulled, api=ln.to_api(),
    )


def _cost(c: planner.Cost) -> Cost:
    return Cost(
        expected=money(c.expected_cents), max=money(c.max_cents), visit_fee=money(c.visit_fee_cents),
        balance=money(c.balance_cents), balance_left_at_max=money(c.balance_left_at_max_cents),
        lines=[LineCost(slot=lc.slot, expected=money(lc.expected_cents), max=money(lc.max_cents)) for lc in c.lines],
    )


def _fraction(cents) -> str:
    """A unit cost that may be a fraction of a cent, to four places."""
    return f"{float(cents) / 100:.4f}"
