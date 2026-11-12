"""What we have on hand, and what it actually cost, from receipts and polls.

The planner needs units and unit cost for every slot and for the closet, and
the only record of what went in is the purchase-request receipts. So this
turns receipts into stock movements and replays them in time order:

- a purchase puts `units_loaded` in the slot and `units_to_back_stock` in the
  closet. `total_paid` is authoritative, and units lost on the way are already
  paid for, so the cost is spread over the units that survived: 1 of 12 broken
  means $X over 11.
- a back-stock load takes `units_from_back_stock` out of the closet and puts
  `units_loaded` in the slot. Again the survivors carry the cost of the lost.
- `units_pulled` of `pulled_sku` go from the slot to the closet at the cost
  they had in the slot.
- `later_losses` take units out of where the receipt says they were found
  (`coil` is the slot, `back_stock` the closet), at their average cost at that
  moment. That cost is **shrinkage**: expensed, so the P&L sees it and the
  units left keep their unit cost. Units still counted in a slot when its
  product changes, and a purchase line that lost every unit, are shrinkage too.
- sales come from the polls' lifetime counters, one movement per rise between
  two polls, timed at the later poll (`vending.counters` decides what a
  reading that goes down means).
- a poll can't tell when a worker swapped a slot's product (#97), so a window
  between two polls that spans the swap holds units of both. The cents
  counter and the price at each end split them; units it can't split are
  booked with no cost and a warning, never at the new product's cost.

A slot's count starts again whenever its product changes, so `loaded` and
`sold` are since the last change, and on hand is loaded - sold - lost.

The cost method is weighted average. The team hasn't chosen between that and
FIFO, so the method lives in `weighted_average` and nowhere else: replace that
one function to change it. Money is exact cents (`Fraction`), never a float.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from fractions import Fraction
from typing import TYPE_CHECKING

from vending import counters
from vending.slots import DEVICE_ID

if TYPE_CHECKING:  # the replay itself needs no database, so its tests run without psycopg
    import psycopg

# Within one moment: pulls happen before loads (the old product comes out
# first), and losses and sales are only ever recorded after both. The old
# product's share of a window split at a product change sold before the door
# opened, so it goes first.
_ORDER = {"sale_before_change": -1, "pull": 0, "purchase": 1, "load": 1, "loss": 2, "sale": 3}


@dataclass(frozen=True)
class Movement:
    """One change to our stock. Which fields matter depends on `kind`."""
    at: datetime
    kind: str                    # purchase | load | pull | loss | sale
    slot: str | None
    sku: str | None
    units: int = 0               # pull, loss, sale
    to_slot: int = 0             # purchase, load
    to_closet: int = 0           # purchase
    lost: int = 0                # purchase, load: lost before reaching the coil
    paid_cents: Fraction | None = None   # purchase: total_paid
    location: str | None = None  # loss: coil | back_stock
    reassigned: bool = False     # purchase, load: the receipt's slot_reassigned
    ref: str = ""                # request and line, for warnings
    since: datetime | None = None        # sale: the poll the rise is measured from
    cents: int | None = None             # sale: the cents counter's rise over the same window
    price_before: int | None = None      # sale: the slot's price at `since`, in cents
    price_after: int | None = None       # sale: the slot's price at `at`, in cents


@dataclass(frozen=True)
class ClosetCount:
    """Units of one product in the closet. Fields match the planner's `ClosetStock`.

    `unit_cost` is None when some of the units have no known cost (old
    product pulled from a slot we never loaded).
    """
    product: str
    units: int
    unit_cost: Fraction | None   # cents per unit


@dataclass(frozen=True)
class SlotCount:
    """One slot since its last product change. `units` and `unit_cost` feed `SlotState`."""
    slot: str
    product: str
    units: int                   # loaded - sold - lost, never below 0
    unit_cost: Fraction | None   # cents per unit of those units
    loaded: int
    sold: int
    lost: int
    since: datetime
    last_moved: datetime | None = None   # the slot's latest purchase, load, pull or loss


@dataclass(frozen=True)
class SaleCost:
    """Units sold from one slot between two polls, and what they had cost us.

    `cost_cents` is None when their cost was unknown; `costed_units` is how
    many of them we had counted in the slot (sales past the count are oversold).
    The ledger's cost of sales is the sum of these. The old product's share
    of a window split at a product change is timed at the change.
    """
    at: datetime
    slot: str
    product: str
    units: int
    costed_units: int
    cost_cents: Fraction | None


@dataclass(frozen=True)
class Shrinkage:
    """Units gone without a sale, and what they had cost us: a later loss, units
    still counted in a slot when its product changed, or a purchase line that
    lost every unit. `cost_cents` is None when their cost was unknown.
    The ledger's shrinkage line is the sum of these."""
    at: datetime
    where: str                   # "slot 15" or "the closet"
    product: str
    units: int
    cost_cents: Fraction | None


@dataclass(frozen=True)
class Stock:
    closet: tuple[ClosetCount, ...]
    slots: tuple[SlotCount, ...]
    warnings: tuple[str, ...] = ()
    sales: tuple[SaleCost, ...] = ()
    shrinkage: tuple[Shrinkage, ...] = ()


def movements_from_receipts(lines: list[dict], losses: list[dict]) -> list[Movement]:
    """Movements for fulfilled lines (receipt fields plus `request_id`, `line_no`, `slot`)
    and their later losses (`request_id`, `line_no`, `slot`, `location`, `sku`, `units`, `recorded_at`).

    An `unavailable` line moved nothing, unless the worker still pulled the old product.
    """
    moves = []
    for line in lines:
        ref = f"{line['request_id']} line {line['line_no']}"
        at, slot = line["resolved_at"], line["slot"]
        if line["units_pulled"]:
            moves.append(Movement(at, "pull", slot, line["pulled_sku"], units=line["units_pulled"], ref=ref))
        if line["outcome"] in ("primary", "alternate"):
            moves.append(Movement(
                at, "purchase", slot, line["sku"], to_slot=line["units_loaded"],
                to_closet=line["units_to_back_stock"], lost=line["units_lost"],
                paid_cents=cents(line["total_paid"]), reassigned=line["slot_reassigned"], ref=ref))
        elif line["outcome"] == "loaded":
            moves.append(Movement(
                at, "load", slot, line["sku"], to_slot=line["units_loaded"],
                lost=line["units_from_back_stock"] - line["units_loaded"],
                reassigned=line["slot_reassigned"], ref=ref))
    for loss in losses:
        moves.append(Movement(
            loss["recorded_at"], "loss", loss["slot"], loss["sku"], units=loss["units"],
            location=loss["location"], ref=f"{loss['request_id']} line {loss['line_no']}"))
    return moves


def sales_from_counters(rows: list[tuple]) -> list[Movement]:
    """Sales from `(selection, polled_at, lifetime_units, lifetime_sales_cents, price_cents)` rows.

    Lifetime counters never reset, so units sold between two polls are the
    difference. A counter that goes down (a board swap, a misread) starts a new
    baseline instead of producing negative sales.

    Each sale carries its window, the cents rise over it and the price at both
    ends, so `weighted_average` can split a window that spans a product change.
    Rows of just `(selection, polled_at, lifetime_units)` still work; a window
    that spans a change then can't be split, and its sales get no cost.
    """
    by_selection: dict[str, list[tuple[datetime, int]]] = {}
    cents_by_selection: dict[str, list[tuple[datetime, int]]] = {}
    prices: dict[tuple[str, datetime], int | None] = {}
    for selection, at, lifetime, *rest in rows:
        by_selection.setdefault(selection, []).append((at, lifetime))
        if rest:
            sales_cents, prices[selection, at] = rest
            cents_by_selection.setdefault(selection, []).append((at, sales_cents))
    moves = []
    for selection in sorted(by_selection):
        cents_rose = {at: (start, rise) for start, at, rise in counters.windows(cents_by_selection.get(selection, ()))}
        for start, at, units in counters.windows(by_selection[selection]):
            # Only a cents rise over the same window can split these units.
            cents_start, rise = cents_rose.get(at, (start, 0))
            known = selection in cents_by_selection and cents_start == start
            moves.append(Movement(at, "sale", selection, None, units=units, since=start,
                                  cents=rise if known else None, price_before=prices.get((selection, start)),
                                  price_after=prices.get((selection, at))))
    return moves


def _old_units(m: Movement) -> int | None:
    """How many of a window's units sold at the old price, or None when the counters can't say.

    With `n` units and `c` cents in the window, old price `a` and new price
    `b`: `a*old + b*(n - old) = c`, so `old = (b*n - c) / (b - a)`. Only a whole
    number from 0 to `n` fits; anything else is a misread or a third price.
    The same price on both sides can't be split at all (docs/cutover.md, #105).
    """
    a, b = m.price_before, m.price_after
    if a is None or b is None or m.cents is None or a == b:
        return None
    old, rest = divmod(b * m.units - m.cents, b - a)
    return old if not rest and 0 <= old <= m.units else None


def _split_at_changes(movements: list[Movement]) -> list[Movement]:
    """Split each sale whose window spans a product change into the old product's and the new one's.

    The old product's units are replayed at the change, just before the pull,
    so they come out of the old count instead of being written off as
    shrinkage. A window the counters can't split, or one with two changes in
    it, is left whole, and `weighted_average` books it with no cost.
    """
    changes: dict[str, list[datetime]] = {}
    holds: dict[str, str] = {}
    for m in sorted(movements, key=_replay_order):
        if m.kind in ("purchase", "load"):
            if holds.get(m.slot, m.sku) != m.sku:
                changes.setdefault(m.slot, []).append(m.at)
            holds[m.slot] = m.sku
    out = []
    for m in movements:
        inside = [t for t in changes.get(m.slot, ())
                  if m.kind == "sale" and m.since is not None and m.since < t <= m.at]
        old = _old_units(m) if len(inside) == 1 else None
        if old is None:
            out.append(m)
            continue
        if old:
            out.append(replace(m, at=inside[0], kind="sale_before_change", units=old))
        if old < m.units:
            out.append(replace(m, since=inside[0], units=m.units - old))
    return out


def _replay_order(m: Movement) -> tuple[datetime, int]:
    return m.at, _ORDER[m.kind]


def cents(amount: Decimal | str | None) -> Fraction | None:
    """Exact cents from a decimal amount in dollars."""
    return None if amount is None else Fraction(Decimal(str(amount))) * 100


@dataclass
class _Pool:
    """Units of one product in one place and their total cost in cents.

    `cost` is None once any unit in the pool has no known cost, and stays so
    until the pool is empty, since an average over part of it would be a guess.
    """
    units: int = 0
    cost: Fraction | None = Fraction(0)

    def unit_cost(self) -> Fraction | None:
        return None if not self.units or self.cost is None else self.cost / self.units

    def add(self, units: int, cost: Fraction | None) -> None:
        if not units:
            return  # nothing arrived, so an unknown cost on nothing can't taint what is here
        self.units += units
        self.cost = None if cost is None or self.cost is None else self.cost + cost
        self._settle()

    def take(self, units: int) -> tuple[int, Fraction | None]:
        """Move units out at their average cost. Returns how many there were, and their cost."""
        taken = min(units, self.units)
        each = self.unit_cost()
        cost = None if each is None and taken else (each or 0) * taken
        self.units -= taken
        if self.cost is not None:
            self.cost -= cost
        self._settle()
        return taken, cost

    def lose(self, units: int) -> tuple[int, Fraction | None]:
        """Units gone without a sale, at their average cost: shrinkage, not a cost the rest carry."""
        return self.take(units)

    def _settle(self) -> None:
        if self.units == 0:
            self.cost = Fraction(0)  # nothing left to carry a cost, known or not


@dataclass
class _Slot:
    product: str
    since: datetime
    pool: _Pool = field(default_factory=_Pool)
    loaded: int = 0
    sold: int = 0
    lost: int = 0
    oversold: int = 0
    last_unit_cost: Fraction | None = None
    last_moved: datetime | None = None
    replaced: str | None = None   # the product this one replaced, if the slot changed


def weighted_average(movements: list[Movement]) -> Stock:
    """Replay movements in time order and value what is left at weighted-average cost.

    This is the one place the cost method lives. Each pool's cost is the sum
    of what went in, and units leave it at the pool's average at that moment.
    """
    closet: dict[str, _Pool] = {}
    slots: dict[str, _Slot] = {}
    warnings: list[str] = []
    sales: list[SaleCost] = []
    shrinkage: list[Shrinkage] = []

    def shelf(sku: str) -> _Pool:
        return closet.setdefault(sku, _Pool())

    for m in sorted(_split_at_changes(movements), key=_replay_order):
        slot = slots.get(m.slot) if m.slot else None
        if m.kind == "pull":
            if slot is None or slot.product != m.sku:
                warnings.append(f"{m.ref}: {m.units} units of {m.sku} pulled from slot {m.slot}, "
                                "which we never loaded with it; their cost is unknown")
                shelf(m.sku).add(m.units, None)
                continue
            # Sales can have emptied the count; the units still cost what the last ones did.
            each = slot.pool.unit_cost() if slot.pool.units else slot.last_unit_cost
            taken, _ = slot.pool.take(m.units)
            if taken < m.units:
                warnings.append(f"{m.ref}: {m.units} units pulled from slot {m.slot} but we counted only {taken}")
            shelf(m.sku).add(m.units, None if each is None else each * m.units)
            slot.last_moved = m.at
        elif m.kind in ("purchase", "load"):
            changed = slot is not None and slot.product != m.sku
            if changed != m.reassigned and slot is not None:
                warnings.append(f"{m.ref}: slot {m.slot} went from {slot.product} to {m.sku} "
                                f"but slot_reassigned is {str(m.reassigned).lower()}")
            if changed and slot.pool.units:
                warnings.append(f"{m.ref}: {slot.pool.units} units of {slot.product} still counted in slot "
                                f"{m.slot} when it changed to {m.sku}; written off as shrinkage")
                shrinkage.append(Shrinkage(m.at, f"slot {m.slot}", slot.product, slot.pool.units, slot.pool.cost))
            if slot is None or changed:
                slot = slots[m.slot] = _Slot(product=m.sku, since=m.at, replaced=slot.product if changed else None)
            if m.kind == "purchase":
                survivors = m.to_slot + m.to_closet
                each = None if m.paid_cents is None or not survivors else m.paid_cents / survivors
                if m.paid_cents is None:
                    warnings.append(f"{m.ref}: bought {m.sku} with no total_paid; its cost is unknown")
                elif not survivors and m.lost:
                    warnings.append(f"{m.ref}: every unit of {m.sku} was lost before loading; "
                                    "the whole line is written off as shrinkage")
                    shrinkage.append(Shrinkage(m.at, f"slot {m.slot}", m.sku, m.lost, m.paid_cents))
                shelf(m.sku).add(m.to_closet, None if each is None else each * m.to_closet)
                slot.pool.add(m.to_slot, None if each is None else each * m.to_slot)
            else:
                wanted = m.to_slot + m.lost
                taken, cost = shelf(m.sku).take(wanted)
                if taken < wanted:
                    warnings.append(f"{m.ref}: loaded {wanted} units of {m.sku} from the closet "
                                    f"but we counted only {taken} there")
                    cost = None
                slot.pool.add(m.to_slot, cost)
            slot.loaded += m.to_slot
            slot.last_moved = m.at
        elif m.kind == "loss":
            if m.location == "back_stock":
                gone, cost = shelf(m.sku).lose(m.units)
                where = "the closet"
            elif slot is not None and slot.product == m.sku:
                gone, cost = slot.pool.lose(m.units)
                slot.lost += gone
                slot.last_moved = m.at
                where = f"slot {m.slot}"
            else:
                warnings.append(f"{m.ref}: {m.units} units of {m.sku} lost in slot {m.slot}, "
                                "which no longer holds it; ignored")
                continue
            if gone:
                shrinkage.append(Shrinkage(m.at, where, m.sku, gone, cost))
            if gone < m.units:
                warnings.append(f"{m.ref}: {m.units} units of {m.sku} lost in {where} but we counted only {gone}")
        elif m.kind in ("sale", "sale_before_change") and slot is not None:
            taken, cost = slot.pool.take(m.units)
            slot.sold += m.units
            slot.oversold += m.units - taken
            if m.kind == "sale" and slot.replaced and m.since is not None and m.since < slot.since:
                # Still taken from the new count, so on hand errs low, but their
                # cost could be either product's, so it is left out (#97).
                warnings.append(f"slot {m.slot}: {m.units} units sold between the polls at {m.since.isoformat()} "
                                f"and {m.at.isoformat()}, across the change from {slot.replaced} to {slot.product}; "
                                "the counters can't split them, so they are booked with no cost")
                cost = None
            sales.append(SaleCost(m.at, m.slot, slot.product, m.units, taken, cost))
        if slot is not None and slot.pool.units:
            slot.last_unit_cost = slot.pool.unit_cost()

    for name, slot in sorted(slots.items()):
        if slot.oversold:
            warnings.append(f"slot {name}: the polls show {slot.oversold} more sales than units we loaded "
                            f"since {slot.since.isoformat()}; counted as 0 on hand")
    return Stock(
        closet=tuple(ClosetCount(sku, pool.units, pool.unit_cost())
                     for sku, pool in sorted(closet.items()) if pool.units),
        slots=tuple(SlotCount(name, s.product, s.pool.units, s.pool.unit_cost(), s.loaded, s.sold, s.lost, s.since,
                              s.last_moved)
                    for name, s in sorted(slots.items())),
        warnings=tuple(warnings),
        sales=tuple(sales),
        shrinkage=tuple(shrinkage),
    )


def stock(conn: psycopg.Connection, until: datetime | None = None) -> Stock:
    """Closet and slot stock now, or as of `until`, from every fulfilled request and every good poll."""
    lines = [dict(zip(_LINE_COLUMNS, row)) for row in conn.execute(
        f"select {', '.join('l.' + c for c in _LINE_COLUMNS)} from purchase_lines l"
        " join purchase_requests r using (request_id) where r.status = 'fulfilled'")]
    losses = [dict(zip(_LOSS_COLUMNS, row)) for row in conn.execute(
        "select o.request_id, o.line_no, l.slot, o.location, o.sku, o.units, o.recorded_at"
        " from purchase_line_losses o join purchase_lines l using (request_id, line_no)"
        " join purchase_requests r using (request_id) where r.status = 'fulfilled'")]
    # Every slot our fulfilled receipts name: those are the slots we stocked.
    slots = sorted({line["slot"] for line in lines})
    # poll_selections only has rows for polls whose checksum passed. The sync
    # stores every device's polls, so read only our machine's counters.
    # Cents and price too, to split a window that spans a product change (#97).
    counters = conn.execute(
        "select s.selection, p.started_at, s.lifetime_units, s.lifetime_sales_cents, s.price_cents"
        " from poll_selections s"
        " join polls p using (session_id) where p.device_id = %s and s.selection = any(%s)",
        (DEVICE_ID, slots),
    ).fetchall()
    movements = movements_from_receipts(lines, losses) + sales_from_counters(counters)
    if until is not None:
        movements = [m for m in movements if m.at <= until]
    return weighted_average(movements)


_LINE_COLUMNS = ("request_id", "line_no", "slot", "outcome", "sku", "total_paid", "units_loaded",
                 "units_lost", "units_to_back_stock", "units_from_back_stock", "units_pulled",
                 "pulled_sku", "slot_reassigned", "resolved_at")
_LOSS_COLUMNS = ("request_id", "line_no", "slot", "location", "sku", "units", "recorded_at")
