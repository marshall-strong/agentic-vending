"""The restock planner: one desired state in, request lines or refusals out.

The agent says what each of our 20 slots should hold, charge and be filled to
after the next visit. `plan_restock` diffs that against what is in the slots
and the closet, decides where every unit comes from, sizes the purchases,
prices them, and runs every gate. The model never does this arithmetic, so it
spends no tokens on it and cannot get it wrong.

`preview_desired_state` and `submit_desired_state` both call this, and submit
calls it again inside the transaction that records the request, so the plan
that is sent is the plan that was checked. That only works if this function
has no side effects: it reads its arguments and returns a `Plan`, nothing else.
See "The restock planner" in docs/planning/tool-list.md for the eight steps.

The lines follow the bridge's live purchase-request schema (checked against
openapi.json on 29 Oct; see docs/dex-bridge.md). Its shape forces three things
the 19 Oct plan didn't expect:

- A line has one source, a purchase or a back-stock load, and a slot gets one
  line. So a slot loads from the closet only when the closet covers all of it.
- Units pulled at this visit can't be loaded at this visit until David
  confirms a line may load what another line pulls. They go to the closet.
- Every line loads at least one unit, so a price change alone can't be sent,
  and slots that don't change get no line.

`Plan.request()` is the body to POST, so the field mapping lives here too.

A plan with problems still comes back with its lines, costs and floors filled
in as far as they could be worked out, plus *every* refusal at once, so the
agent can fix the whole plan in one turn instead of one problem per call.

Prices and floors come from `vending.pricing`, the one place the price rule
lives. Money is integer cents (or an exact Fraction of a cent), never floats.

Standard library only.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, time, tzinfo
from decimal import Decimal
from fractions import Fraction
from urllib.parse import urlparse

from vending.pricing import (
    DEFAULT_TAX_PERCENT,
    Refusal,
    _dollars,
    check_price,
    floor_cents,
    pack_floor_cents,
    round_up_to_step,
    slot_floor_cents,
    to_cents,
)

# Columns 5-8 of every row on vmc1 (GET /api/v1/slots).
OUR_SLOTS: tuple[str, ...] = tuple(
    f"{row}{col}" for row in range(1, 6) for col in range(5, 9))

# Spelled exactly as the API wants them.
AMAZON = "Amazon"
STORES = frozenset({"Walmart", "Costco", AMAZON, "BJs"})

# Limits from the live schema. Going over any of them gets the request refused.
MAX_PACKS = 20
MAX_UNITS_PER_PACK = 60
MAX_PRICE_PER_PACK_CENTS = 20000
MAX_SALE_PRICE_CENTS = 1000
MAX_PACK_DESCRIPTION = 100

# David's cases, one per slot.
NO_CHANGE = "no_change"
RESTOCK = "restock"
PRICE_ONLY = "price_only"   # refused: the API has no way to send it yet
SWITCH = "switch"

# Where a line's units come from (the API's `source`).
PURCHASE = "purchase"
BACK_STOCK = "back_stock"

# A price as the agent or the database hands it over. See `to_cents`.
Price = str | Decimal | int


# --- Inputs ------------------------------------------------------------------
# Field names follow the API where it has one, and tool-list.md otherwise, so
# these can be filled from Postgres later. Prices that come from people (the
# agent, the team's sourcing table) are parsed here; prices that come from our
# own database are already integer cents and say so in the name.

@dataclass(frozen=True)
class SlotState:
    """One of our slots as our database sees it now."""
    slot: str
    product: str | None
    price_cents: int | None             # PA1-02
    units: int                          # estimated on hand: loaded - sold
    unit_cost: Fraction | int | None    # actual cents per unit of those units


@dataclass(frozen=True)
class ClosetStock:
    """Units of one product in the closet, at their actual cost after losses."""
    product: str
    units: int
    unit_cost: Fraction | int           # cents per unit


@dataclass(frozen=True)
class PendingRequest:
    """A request we submitted whose receipts have not come back yet."""
    request_id: str


@dataclass(frozen=True)
class CatalogProduct:
    """One single-item product from /products. Inactive products are refused."""
    product: str
    active: bool = True


@dataclass(frozen=True)
class PurchaseOption:
    """One way to buy a product: a store, a pack, and the most to pay for it.

    `expected_price_per_pack` is what we think the pack costs. The agent
    usually gives only the max; the planner then borrows the expected price
    from the sourcing table for the same store and pack, or falls back to the
    max. `pack_description` is passed to the worker as what to look for.
    """
    store: str
    units_per_pack: int
    max_price_per_pack: Price
    url: str | None = None
    expected_price_per_pack: Price | None = None
    pack_description: str | None = None


@dataclass(frozen=True)
class Sourcing:
    """How to buy one product: a primary, an optional alternate, and extras.

    The same shape serves the agent's `desired.sourcing` and the team's
    sourcing table. The agent's entry wins when both exist.
    """
    product: str
    primary: PurchaseOption
    alternate: PurchaseOption | None = None
    extra_units_to_closet: int = 0


@dataclass(frozen=True)
class CurrentState:
    """Everything the planner reads from our side, as of one moment.

    The spec lists the catalog, sourcing table and balance as separate inputs,
    but the signature is `plan_restock(current, desired, config, now)`, so
    they ride here with the database state they come from.
    """
    slots: tuple[SlotState, ...]
    closet: tuple[ClosetStock, ...] = ()
    pending: tuple[PendingRequest, ...] = ()
    catalog: tuple[CatalogProduct, ...] = ()
    sourcing_table: tuple[Sourcing, ...] = ()
    balance_cents: int = 0              # spendable, after the sales-tax set-aside


@dataclass(frozen=True)
class DesiredSlot:
    """What the agent wants one slot to look like after the visit.

    `keep=True` means no change and ignores the other fields. Otherwise the
    agent gives the product and the sale price; `fill_to` defaults to the
    slot's capacity. `product=None` asks for an empty slot, which the API
    can't express yet, so it is refused.
    """
    slot: str
    keep: bool = False
    product: str | None = None
    sale_price: Price | None = None
    fill_to: int | None = None


@dataclass(frozen=True)
class DesiredState:
    """The agent's whole request: all 20 slots, how to buy, and the visit.

    `off_cycle=True` asks for a midweek visit, which costs
    `config.midweek_fee_cents` on top of the purchases.
    """
    slots: tuple[DesiredSlot, ...]
    off_cycle: bool = False
    sourcing: tuple[Sourcing, ...] = ()

    def __post_init__(self):
        # The tool schema should already stop this; a truthy string here would
        # otherwise turn a free visit into a $25 one, or the reverse.
        if not isinstance(self.off_cycle, bool):
            raise ValueError(f"off_cycle must be true or false, not {self.off_cycle!r}")


@dataclass(frozen=True)
class Cutoff:
    """The last moment to order from a store for this week's Friday visit."""
    weekday: int        # Monday = 0, as datetime.weekday()
    at: time


IN_STORE_CUTOFF = Cutoff(3, time(23, 59))   # Thursday 23:59
AMAZON_CUTOFF = Cutoff(2, time(23, 59))     # Wednesday 23:59, two-day shipping


def _default_cutoffs() -> dict[str, Cutoff]:
    return {"Walmart": IN_STORE_CUTOFF, "BJs": IN_STORE_CUTOFF,
            "Costco": IN_STORE_CUTOFF, AMAZON: AMAZON_CUTOFF}


@dataclass(frozen=True)
class Config:
    """The `config` table. Most of it is still undecided, so all of it is data.

    `slot_capacity` has no default because nobody has measured a rail yet.
    The ceiling default is the team's $6.00 proposal; the API refuses any
    sale price over $10.00, so a higher ceiling is an error. Cutoffs are per
    store and in `timezone`; past a cutoff in `soft_cutoff_stores` is a
    warning, not a refusal, because David saw no hard Amazon deadline. The
    week turns over on `week_starts` (Saturday, after the Friday restock), so
    a cutoff passed on Thursday stops mattering once the visit is done.

    `timezone` may be a name or a tzinfo. A name needs the IANA database,
    which Windows lacks unless the `tzdata` package is installed.
    """
    slot_capacity: int
    price_ceiling_cents: int = 600
    tax_percent: int = DEFAULT_TAX_PERCENT
    midweek_fee_cents: int = 2500
    halted: bool = False
    slots: tuple[str, ...] = OUR_SLOTS
    stores: frozenset[str] = STORES
    cutoffs: Mapping[str, Cutoff] = field(default_factory=_default_cutoffs)
    soft_cutoff_stores: frozenset[str] = frozenset({AMAZON})
    week_starts: int = 5
    timezone: str | tzinfo = "America/New_York"

    def __post_init__(self):
        if self.price_ceiling_cents > MAX_SALE_PRICE_CENTS:
            raise ValueError(
                f"price ceiling {_dollars(self.price_ceiling_cents)} is over the API's "
                f"{_dollars(MAX_SALE_PRICE_CENTS)}")


# --- Outputs -----------------------------------------------------------------

@dataclass(frozen=True)
class PlannedPurchase:
    """One option the worker may buy for one line, sized for that line alone."""
    store: str
    url: str | None
    units_per_pack: int
    packs: int
    max_price_per_pack_cents: int
    expected_price_per_pack_cents: int
    pack_description: str | None
    units_to_closet: int        # bought beyond the slot's shortfall


@dataclass(frozen=True)
class Purchase:
    units_short: int            # what the slot needs loaded
    primary: PlannedPurchase
    alternate: PlannedPurchase | None


@dataclass(frozen=True)
class Line:
    """One slot that loads something. The API takes no other kind.

    `source` is `purchase` (then `purchase` is set) or `back_stock` (the
    units come from the closet). `change_product` is what the bridge checks:
    true exactly when the product differs from what the slot holds now.
    Pulled units go to the closet.
    """
    slot: str
    source: str
    action: str
    product: str
    sale_price_cents: int
    change_product: bool
    fill_to: int
    units_staying: int
    units_to_load: int
    purchase: Purchase | None
    pulled_product: str | None
    units_pulled: int

    def to_api(self) -> dict:
        """This line as the bridge's PurchaseLine or BackStockLine."""
        if self.source == BACK_STOCK:
            return {"slot": self.slot, "source": BACK_STOCK, "load": {
                "sku": self.product, "units": self.units_to_load,
                "sale_price": _money(self.sale_price_cents),
                "change_product": self.change_product}}
        body = {"slot": self.slot, "source": PURCHASE,
                "primary": self._choice(self.purchase.primary)}
        if self.purchase.alternate is not None:
            body["alternate"] = self._choice(self.purchase.alternate)
        return body

    def _choice(self, o: PlannedPurchase) -> dict:
        choice = {"sku": self.product, "store": o.store, "packs": o.packs,
                  "units_per_pack": o.units_per_pack,
                  "max_price_per_pack": _money(o.max_price_per_pack_cents),
                  "sale_price": _money(self.sale_price_cents),
                  "change_product": self.change_product}
        if o.url:
            choice["url"] = o.url
        if o.pack_description:
            choice["pack_description"] = o.pack_description
        return choice


@dataclass(frozen=True)
class LineCost:
    slot: str
    expected_cents: int
    max_cents: int


@dataclass(frozen=True)
class Cost:
    """Expected and worst-case spend. The balance gate uses the worst case."""
    lines: tuple[LineCost, ...]
    visit_fee_cents: int
    expected_cents: int         # purchases + visit fee
    max_cents: int
    balance_cents: int
    balance_left_at_max_cents: int


@dataclass(frozen=True)
class FloorPart:
    """One cost a slot might carry, and the floor it alone would set."""
    source: str
    unit_cost: Fraction
    floor_cents: int


@dataclass(frozen=True)
class SlotFloor:
    """A slot's floor and the arithmetic behind it, so the agent can see why."""
    slot: str
    floor_cents: int
    parts: tuple[FloorPart, ...]


@dataclass(frozen=True)
class Plan:
    """The planner's answer. Submittable only when `refusals` is empty.

    `plan_hash` is set only then. It hashes the request body, so #6 can tell
    the same plan submitted twice whenever it was computed.
    """
    lines: tuple[Line, ...]
    directive: str
    cost: Cost | None
    floors: tuple[SlotFloor, ...]
    warnings: tuple[str, ...]
    refusals: tuple[Refusal, ...]
    plan_hash: str | None
    off_cycle: bool = False

    @property
    def ok(self) -> bool:
        return not self.refusals

    def request(self) -> dict:
        """The body for POST /api/v1/purchase-requests."""
        return {"off_cycle": self.off_cycle, "lines": [line.to_api() for line in self.lines]}


# --- Working state -----------------------------------------------------------

@dataclass
class _Option:
    store: str
    url: str | None
    units_per_pack: int
    max_cents: int
    expected_cents: int | None
    pack_description: str | None


@dataclass
class _Source:
    primary: _Option
    alternate: _Option | None
    extras: int


@dataclass
class _Lot:
    """Units of one product in the closet, as the allocation draws them down."""
    units: int
    unit_cost: Fraction


@dataclass
class _Slot:
    slot: str
    state: SlotState
    on_hand: int
    product: str | None
    price_cents: int | None
    fill_to: int
    staying: int
    need: int = 0
    changes_product: bool = False
    from_back_stock: int = 0
    shortfall: int = 0
    purchase: Purchase | None = None
    parts: list[FloorPart] = field(default_factory=list)

    @property
    def price_changed(self) -> bool:
        return self.price_cents != self.state.price_cents

    @property
    def action(self) -> str:
        if self.changes_product:
            return SWITCH
        if self.need > 0:
            return RESTOCK
        if self.price_changed:
            return PRICE_ONLY
        return NO_CHANGE


# --- The planner -------------------------------------------------------------

def plan_restock(current: CurrentState, desired: DesiredState, config: Config,
                 now: datetime) -> Plan:
    """Turn a desired state into request lines, or every reason it can't be.

    `now` must be timezone-aware; it is compared with the store cutoffs.
    """
    if config.halted:
        # The agent's only move is to stop, so nothing else is worth saying.
        return Plan((), "", None, (), (), (Refusal(
            "halted", "the halt switch is on; stop and do nothing"),), None)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    refusals: list[Refusal] = []
    warnings: list[str] = []

    # 1. Shape. Slots that fail are planned as "keep" so the rest can still
    #    be checked and every refusal reported at once.
    targets = _check_slot_list(desired.slots, config.slots, refusals)
    active = {p.product for p in current.catalog if p.active}
    sources = _Sources(desired.sourcing, current.sourcing_table, config, refusals)

    # 2. Classify each slot and work out how many units it needs.
    states = {s.slot: s for s in current.slots}
    slots = [_intend(slot, states.get(slot), targets.get(slot), active, config, refusals)
             for slot in config.slots]

    # 3. Supply: only what the closet holds before the visit. Units pulled at
    #    this visit can't be loaded until the next request, until David
    #    confirms a line may load what another line pulls.
    closet: dict[str, list[_Lot]] = defaultdict(list)
    for c in current.closet:
        if c.units > 0:
            closet[c.product].append(_Lot(c.units, Fraction(c.unit_cost)))

    # 4. Allocate. A line has one source, so a slot loads from the closet only
    #    if the closet covers all of it; otherwise it buys all of it.
    for s in slots:
        if s.staying and (s.need or s.price_changed):
            # Units staying in the slot sell at its new price too.
            _add_part(s, "on hand", s.state.unit_cost, config, warnings, s.staying)
        if not s.need:
            continue
        lots = closet.get(s.product, [])
        available = sum(lot.units for lot in lots)
        if available < s.need:
            s.shortfall = s.need
            if available:
                warnings.append(
                    f"slot {s.slot}: {available} {s.product} in the closet go unused, because "
                    f"a line loads from one source; fill_to {s.staying + available} would "
                    f"load them instead of buying")
            continue
        remaining = s.need
        for lot in lots:
            take = min(remaining, lot.units)
            if take:
                lot.units -= take
                remaining -= take
                _add_part(s, "back stock", lot.unit_cost, config, warnings, take)
        s.from_back_stock = s.need

    # 5. Size each purchase, separately per slot until David confirms one
    #    line may load units bought on another.
    extras_placed: set[str] = set()
    for s in slots:
        if s.shortfall == 0:
            continue
        source = sources.get(s.product)
        if source is None:
            if not sources.is_bad(s.product):
                code = "insufficient_stock" if closet.get(s.product) else "no_sourcing"
                refusals.append(Refusal(code, (
                    f"slot {s.slot} needs {s.shortfall} {s.product}, the closet can't cover "
                    f"it, and there is no sourcing for it, given or in the table; "
                    f"add sourcing or lower fill_to"), s.slot))
            continue
        extra = 0
        if s.product not in extras_placed:
            extra = source.extras
            extras_placed.add(s.product)
        primary = _size(source.primary, s.shortfall, extra)
        alternate = source.alternate and _size(source.alternate, s.shortfall, extra)
        s.purchase = Purchase(s.shortfall, primary, alternate)
        for label, option, planned in (("primary", source.primary, primary),
                                       ("alternate", source.alternate, alternate)):
            if option is None:
                continue
            s.parts.append(FloorPart(
                f"{label} {option.store} {option.units_per_pack}-pack, max {_dollars(option.max_cents)}",
                Fraction(option.max_cents, option.units_per_pack),
                pack_floor_cents(option.max_cents, option.units_per_pack, config.tax_percent)))
            if planned.packs > MAX_PACKS:
                refusals.append(Refusal("too_many_packs", (
                    f"slot {s.slot} needs {planned.packs} {option.units_per_pack}-packs from "
                    f"{option.store}, and a line takes at most {MAX_PACKS}; use a bigger pack, "
                    f"or lower fill_to or extra_units_to_closet"), s.slot))
    for product in sources.extras_wanted() - extras_placed:
        warnings.append(f"extra_units_to_closet for {product} ignored: no line buys {product}")

    # 6. Cost: expected at the primary's expected price, max at the dearer option.
    cost = _cost_plan(slots, desired.off_cycle, config, current.balance_cents)

    # 7. Gates.
    used = _stores_used(slots)
    now_local = now.astimezone(_tz(config.timezone)) if used else now
    for store, slot_names in sorted(used.items()):
        cutoff = config.cutoffs.get(store)
        if cutoff is None or not _past_cutoff(now_local, cutoff, config.week_starts):
            continue
        when = f"{_WEEKDAYS[cutoff.weekday]} {cutoff.at:%H:%M}"
        if store in config.soft_cutoff_stores:
            warnings.append(
                f"past the {store} cutoff ({when}) for slots {', '.join(slot_names)}; "
                f"the order may arrive after the visit")
        else:
            refusals.append(Refusal("past_cutoff", (
                f"past the {store} cutoff ({when}) for slots {', '.join(slot_names)}; "
                f"switch to Amazon or wait for next week")))

    if current.pending:
        ids = ", ".join(p.request_id for p in current.pending)
        refusals.append(Refusal("request_pending", (
            f"request {ids} is still unresolved; wait for its receipts")))

    if cost.max_cents > current.balance_cents:
        refusals.append(Refusal("insufficient_balance", (
            f"max cost {_dollars(cost.max_cents)} (visit fee {_dollars(cost.visit_fee_cents)} "
            f"included) exceeds the spendable balance of {_dollars(current.balance_cents)}; "
            f"buy less or cheaper")))

    floors: list[SlotFloor] = []
    for s in slots:
        if s.action == NO_CHANGE:
            continue
        if s.action == PRICE_ONLY:
            refusals.append(Refusal("price_only_unsupported", (
                f"slot {s.slot} changes only its price, but every line must load at least "
                f"one unit and there is no price-change route yet; load at least one unit "
                f"or keep the old price"), s.slot))
        floor = slot_floor_cents([p.floor_cents for p in s.parts]) if s.parts else 0
        floors.append(SlotFloor(s.slot, floor, tuple(s.parts)))
        refusals.extend(check_price(s.price_cents, floor, config.price_ceiling_cents, s.slot))

    # 8. The plan, with every refusal found.
    lines = tuple(_line(s) for s in slots if s.need)
    if not lines and not refusals:
        # Only when nothing else is wrong: a refused slot already says why
        # it loads nothing, and the API refuses a request with no lines.
        if desired.off_cycle:
            refusals.append(Refusal("midweek_no_op", (
                f"a midweek visit costs {_dollars(config.midweek_fee_cents)} and this plan "
                f"loads nothing; don't spend it")))
        else:
            refusals.append(Refusal("nothing_to_load", (
                "no slot loads anything, so there is no request to send; "
                "skip this week's request")))
    plan = Plan(lines, _directive(desired.off_cycle, slots), cost, tuple(floors),
                tuple(warnings), tuple(refusals), None, desired.off_cycle)
    if refusals:
        return plan
    body = json.dumps(plan.request(), sort_keys=True)
    return Plan(lines, plan.directive, cost, plan.floors, plan.warnings, (),
                hashlib.sha256(body.encode()).hexdigest(), desired.off_cycle)


# --- Step 1 helpers ----------------------------------------------------------

def _check_slot_list(targets: tuple[DesiredSlot, ...], ours: tuple[str, ...],
                     refusals: list[Refusal]) -> dict[str, DesiredSlot]:
    """All of our slots, each exactly once. The first listing of a slot counts."""
    seen: dict[str, DesiredSlot] = {}
    repeated: set[str] = set()
    for t in targets:
        if t.slot not in ours:
            refusals.append(Refusal("slot_list", f"slot {t.slot} is not one of ours", t.slot))
        elif t.slot in seen:
            if t.slot not in repeated:
                repeated.add(t.slot)
                refusals.append(Refusal("slot_list", f"slot {t.slot} is listed more than once", t.slot))
        else:
            seen[t.slot] = t
    for slot in ours:
        if slot not in seen:
            refusals.append(Refusal("slot_list", (
                f"slot {slot} is missing; list all {len(ours)} slots, "
                f"with keep: true for no change"), slot))
    return seen


def _intend(slot: str, state: SlotState | None, target: DesiredSlot | None,
            active: set[str], config: Config, refusals: list[Refusal]) -> _Slot:
    """Read one slot's target, refusing what can't be planned."""
    state = state or SlotState(slot, None, None, 0, None)
    on_hand = max(0, state.units)   # loaded - sold can drift below zero
    held = _Slot(slot, state, on_hand, state.product, state.price_cents, on_hand, on_hand)
    if target is None or target.keep:
        return held

    usable = True
    if target.product is None:
        usable = False
        refusals.append(Refusal("empty_slot", (
            f"slot {slot} would be left empty, which the API can't do yet; "
            f"keep or switch it"), slot))
    elif target.product not in active:
        usable = False
        refusals.append(Refusal("product_unknown", (
            f"{target.product} is not active in the catalog; request it and wait "
            f"for approval"), slot))

    price = None
    if target.sale_price is None:
        usable = False
        refusals.append(Refusal("price_off_step", f"slot {slot} needs a sale_price", slot))
    else:
        try:
            price = to_cents(target.sale_price)
        except ValueError as e:
            usable = False
            refusals.append(Refusal("price_off_step", f"slot {slot}: {e}", slot))
        else:
            if price <= 0:
                usable = False
                refusals.append(Refusal("below_floor", f"slot {slot}: a sale price must be above $0.00", slot))

    fill_to = config.slot_capacity if target.fill_to is None else target.fill_to
    if not 0 <= fill_to <= config.slot_capacity:
        usable = False
        refusals.append(Refusal("over_capacity", (
            f"slot {slot} fill_to {fill_to} is outside 0-{config.slot_capacity}"), slot))

    if not usable:
        return held
    same = target.product == state.product
    staying = on_hand if same else 0
    return _Slot(slot, state, on_hand, target.product, price, fill_to, staying,
                 need=max(0, fill_to - staying), changes_product=not same)


class _Sources:
    """Sourcing per product: the agent's entry if given, else the table's.

    The agent's entries are checked up front. A table entry is checked only
    when a plan needs it, so a stale row the plan doesn't use refuses nothing.
    """

    def __init__(self, given: tuple[Sourcing, ...], table: tuple[Sourcing, ...],
                 config: Config, refusals: list[Refusal]):
        self._config = config
        self._refusals = refusals
        self._table = {s.product: s for s in table}
        self._parsed: dict[str, _Source | None] = {}
        self._bad: set[str] = set()
        for s in given:
            if s.product in self._parsed:
                self._bad.add(s.product)
                refusals.append(Refusal("bad_sourcing", f"sourcing for {s.product} is given twice"))
                continue
            self._parsed[s.product] = self._parse(s, from_table=False)

    def get(self, product: str) -> _Source | None:
        if product not in self._parsed:
            raw = self._table.get(product)
            self._parsed[product] = raw and self._parse(raw, from_table=True)
        return self._parsed[product]

    def is_bad(self, product: str) -> bool:
        return product in self._bad

    def extras_wanted(self) -> set[str]:
        return {p for p, s in self._parsed.items() if s is not None and s.extras}

    def _parse(self, s: Sourcing, from_table: bool) -> _Source | None:
        problems: list[str] = []
        primary = self._option(s.primary, "primary", problems)
        alternate = s.alternate and self._option(s.alternate, "alternate", problems)
        if isinstance(s.extra_units_to_closet, bool) or not isinstance(s.extra_units_to_closet, int) \
                or s.extra_units_to_closet < 0:
            problems.append(f"extra_units_to_closet must be a whole number, not {s.extra_units_to_closet!r}")
        if problems:
            self._bad.add(s.product)
            self._refusals.extend(Refusal("bad_sourcing", f"{s.product}: {p}") for p in problems)
            return None
        if not from_table:
            # The agent rarely knows the expected price; the table usually does.
            self._borrow_expected(s.product, primary)
            if alternate is not None:
                self._borrow_expected(s.product, alternate)
        return _Source(primary, alternate, s.extra_units_to_closet)

    def _option(self, o: PurchaseOption, label: str, problems: list[str]) -> _Option | None:
        before = len(problems)
        if o.store not in self._config.stores:
            problems.append(f"{label} store {o.store!r} is not one of {', '.join(sorted(self._config.stores))}")
        if o.store == AMAZON:
            if not o.url:
                problems.append(f"{label} is from Amazon but has no url")
            elif "amazon." not in (urlparse(o.url).hostname or ""):
                problems.append(f"{label} url {o.url!r} is not an Amazon link")
        if isinstance(o.units_per_pack, bool) or not isinstance(o.units_per_pack, int) \
                or not 1 <= o.units_per_pack <= MAX_UNITS_PER_PACK:
            problems.append(
                f"{label} units_per_pack must be a whole number from 1 to {MAX_UNITS_PER_PACK}, "
                f"not {o.units_per_pack!r}")
        max_cents = self._cents(o.max_price_per_pack, f"{label} max_price_per_pack", problems)
        if max_cents == 0 or max_cents > MAX_PRICE_PER_PACK_CENTS:
            problems.append(
                f"{label} max_price_per_pack must be above $0.00 and at most "
                f"{_dollars(MAX_PRICE_PER_PACK_CENTS)}")
        expected = None
        if o.expected_price_per_pack is not None:
            expected = self._cents(o.expected_price_per_pack, f"{label} expected_price_per_pack", problems)
        if o.pack_description is not None and not 1 <= len(o.pack_description) <= MAX_PACK_DESCRIPTION:
            problems.append(f"{label} pack_description must be 1-{MAX_PACK_DESCRIPTION} characters")
        if len(problems) > before:
            return None
        return _Option(o.store, o.url, o.units_per_pack, max_cents, expected, o.pack_description)

    @staticmethod
    def _cents(value: Price, what: str, problems: list[str]) -> int:
        try:
            cents = to_cents(value)
        except ValueError as e:
            problems.append(f"{what}: {e}")
            return -1
        if cents < 0:
            problems.append(f"{what} cannot be negative")
        return cents

    def _borrow_expected(self, product: str, option: _Option) -> None:
        if option.expected_cents is not None:
            return
        row = self._table.get(product)
        for o in (row.primary, row.alternate) if row else ():
            if o is not None and o.store == option.store and o.units_per_pack == option.units_per_pack \
                    and o.expected_price_per_pack is not None:
                try:
                    option.expected_cents = to_cents(o.expected_price_per_pack)
                except ValueError:
                    pass
                return


# --- Steps 4-6 helpers -------------------------------------------------------

def _add_part(s: _Slot, source: str, unit_cost: Fraction | int | None,
              config: Config, warnings: list[str], units: int) -> None:
    """Record a cost the slot's price must clear, if we know it."""
    if unit_cost is None:
        warnings.append(
            f"slot {s.slot}: the cost of {units} units from {source} is unknown, "
            f"so the floor leaves them out")
        return
    cost = Fraction(unit_cost)
    s.parts.append(FloorPart(source, cost, floor_cents(cost, config.tax_percent)))


def _size(option: _Option, short: int, extra: int) -> PlannedPurchase:
    """Whole packs covering the shortfall plus extras; the rest goes to the closet."""
    packs = round_up_to_step(Fraction(short + extra), option.units_per_pack) // option.units_per_pack
    expected = option.max_cents if option.expected_cents is None else option.expected_cents
    return PlannedPurchase(option.store, option.url, option.units_per_pack, packs,
                           option.max_cents, expected, option.pack_description,
                           packs * option.units_per_pack - short)


def _cost_plan(slots: list[_Slot], off_cycle: bool, config: Config, balance: int) -> Cost:
    """Expected assumes the primary at its expected price. Max assumes the worst
    option at its max, because the worker may buy either and won't call back."""
    lines = []
    for s in slots:
        p = s.purchase
        if p is None:
            if s.need:
                lines.append(LineCost(s.slot, 0, 0))
            continue
        options = [o for o in (p.primary, p.alternate) if o is not None]
        lines.append(LineCost(
            s.slot,
            p.primary.packs * p.primary.expected_price_per_pack_cents,
            max(o.packs * o.max_price_per_pack_cents for o in options)))
    fee = config.midweek_fee_cents if off_cycle else 0
    expected = sum(c.expected_cents for c in lines) + fee
    worst = sum(c.max_cents for c in lines) + fee
    return Cost(tuple(lines), fee, expected, worst, balance, balance - worst)


# --- Step 7 helpers ----------------------------------------------------------

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _tz(zone: str | tzinfo) -> tzinfo:
    if isinstance(zone, str):
        from zoneinfo import ZoneInfo
        return ZoneInfo(zone)
    return zone


def _past_cutoff(now_local: datetime, cutoff: Cutoff, week_starts: int) -> bool:
    """True from just after the cutoff minute until the week turns over.

    Weekdays are counted from `week_starts`, so Thursday 23:59 is late in the
    week and Saturday morning is early in the next one.
    """
    def position(weekday: int, hour: int, minute: int) -> tuple[int, int, int]:
        return ((weekday - week_starts) % 7, hour, minute)
    return (position(now_local.weekday(), now_local.hour, now_local.minute)
            > position(cutoff.weekday, cutoff.at.hour, cutoff.at.minute))


def _stores_used(slots: list[_Slot]) -> dict[str, list[str]]:
    used: dict[str, list[str]] = defaultdict(list)
    for s in slots:
        if s.purchase is None:
            continue
        for o in (s.purchase.primary, s.purchase.alternate):
            if o is not None and s.slot not in used[o.store]:
                used[o.store].append(s.slot)
    return used


# --- Step 8 helpers ----------------------------------------------------------

def _money(cents: int) -> str:
    """Cents as the API's dollar string. A string, so no float ever carries it."""
    return f"{cents // 100}.{cents % 100:02d}"


def _line(s: _Slot) -> Line:
    pulled = s.state.product if s.changes_product and s.on_hand else None
    return Line(
        slot=s.slot,
        source=BACK_STOCK if s.purchase is None else PURCHASE,
        action=s.action,
        product=s.product,
        sale_price_cents=s.price_cents,
        change_product=s.changes_product,
        fill_to=s.fill_to,
        units_staying=s.staying,
        units_to_load=s.need,
        purchase=s.purchase,
        pulled_product=pulled,
        units_pulled=s.on_hand if pulled else 0)


def _directive(off_cycle: bool, slots: list[_Slot]) -> str:
    """The plan as the worker reads it: one row per slot, then what to buy where."""
    rows = [("Slot", "Action", "Product", "Price", "Load", "From", "Pull")]
    for s in slots:
        source = "-"
        if s.purchase:
            source = "buy"
        elif s.from_back_stock:
            source = "back stock"
        pull = f"{s.on_hand} {s.state.product} to closet" if s.changes_product and s.on_hand else "-"
        rows.append((
            s.slot, s.action.replace("_", " "), s.product or "-",
            _dollars(s.price_cents) if s.price_cents is not None else "-",
            str(s.need) if s.need else "-", source, pull))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    out = [f"Visit: {'midweek (off cycle)' if off_cycle else 'scheduled'}", ""]
    out += ["  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows]

    shopping: dict[str, list[str]] = defaultdict(list)
    for s in slots:
        if s.purchase is None:
            continue
        p = s.purchase
        shopping[p.primary.store].append(_shopping_row(s, p.primary, None))
        if p.alternate is not None:
            shopping[p.alternate.store].append(_shopping_row(s, p.alternate, p.primary.store))
    if shopping:
        out += ["", "Shopping list"]
        for store in sorted(shopping):
            out.append(store)
            out += shopping[store]
    return "\n".join(out)


def _shopping_row(s: _Slot, o: PlannedPurchase, alternate_for: str | None) -> str:
    row = (f"  {s.product}: {o.packs} x {o.units_per_pack}-pack, max "
           f"{_dollars(o.max_price_per_pack_cents)} a pack, for slot {s.slot}")
    if o.units_to_closet:
        row += f" ({o.units_to_closet} to closet)"
    if alternate_for:
        row += f"; only if {alternate_for} is unavailable or over its max"
    if o.url:
        row += f"\n    {o.url}"
    return row
