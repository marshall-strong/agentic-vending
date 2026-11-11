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

A slot's count starts again whenever its product changes, so `loaded` and
`sold` are since the last change, and on hand is loaded - sold - lost.

The cost method is weighted average. The team hasn't chosen between that and
FIFO, so the method lives in `weighted_average` and nowhere else: replace that
one function to change it. Money is exact cents (`Fraction`), never a float.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from fractions import Fraction
from typing import TYPE_CHECKING

from vending import counters
from vending.slots import DEVICE_ID, SLOTS

if TYPE_CHECKING:  # the replay itself needs no database, so its tests run without psycopg
    import psycopg

# Within one moment: pulls happen before loads (the old product comes out
# first), and losses and sales are only ever recorded after both.
_ORDER = {"pull": 0, "purchase": 1, "load": 1, "loss": 2, "sale": 3}


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


@dataclass(frozen=True)
class SaleCost:
    """Units sold from one slot between two polls, and what they had cost us.

    `cost_cents` is None when their cost was unknown; `costed_units` is how
    many of them we had counted in the slot (sales past the count are oversold).
    The ledger's cost of sales is the sum of these.
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


def sales_from_counters(rows: list[tuple[str, datetime, int]]) -> list[Movement]:
    """Sales from `(selection, polled_at, lifetime_units)` rows.

    Lifetime counters never reset, so units sold between two polls are the
    difference. A counter that goes down (a board swap, a misread) starts a new
    baseline instead of producing negative sales.
    """
    by_selection: dict[str, list[tuple[datetime, int]]] = {}
    for selection, at, lifetime in rows:
        by_selection.setdefault(selection, []).append((at, lifetime))
    moves = []
    for selection in sorted(by_selection):
        moves += [Movement(at, "sale", selection, None, units=units)
                  for at, units in counters.rises(by_selection[selection])]
    return moves


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

    for m in sorted(movements, key=lambda m: (m.at, _ORDER[m.kind])):
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
                slot = slots[m.slot] = _Slot(product=m.sku, since=m.at)
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
        elif m.kind == "loss":
            if m.location == "back_stock":
                gone, cost = shelf(m.sku).lose(m.units)
                where = "the closet"
            elif slot is not None and slot.product == m.sku:
                gone, cost = slot.pool.lose(m.units)
                slot.lost += gone
                where = f"slot {m.slot}"
            else:
                warnings.append(f"{m.ref}: {m.units} units of {m.sku} lost in slot {m.slot}, "
                                "which no longer holds it; ignored")
                continue
            if gone:
                shrinkage.append(Shrinkage(m.at, where, m.sku, gone, cost))
            if gone < m.units:
                warnings.append(f"{m.ref}: {m.units} units of {m.sku} lost in {where} but we counted only {gone}")
        elif m.kind == "sale" and slot is not None:
            taken, cost = slot.pool.take(m.units)
            slot.sold += m.units
            slot.oversold += m.units - taken
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
        slots=tuple(SlotCount(name, s.product, s.pool.units, s.pool.unit_cost(), s.loaded, s.sold, s.lost, s.since)
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
    slots = sorted({line["slot"] for line in lines} & set(SLOTS))
    # poll_selections only has rows for polls whose checksum passed. Rows stored
    # before the integrity rule also cover other devices (docs/integrity.md).
    counters = conn.execute(
        "select s.selection, p.started_at, s.lifetime_units from poll_selections s"
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
