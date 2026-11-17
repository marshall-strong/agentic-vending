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
  counter and the price at each end split them. Units it can't split keep
  the cost taken from the slot (`unattributed_cost_cents`), which is the new
  product's average cost and may be the wrong product's; the ledger counts it
  in cost of sales and says so in a note, so profit isn't overstated.

A slot's count starts again whenever its product changes, so `loaded` and
`sold` are since the last change, and on hand is loaded - sold - lost.

The cost method is weighted average. The team hasn't chosen between that and
FIFO, so the method lives in `weighted_average` and nowhere else: replace that
one function to change it. Money is exact cents (`Fraction`), never a float.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import TYPE_CHECKING

from vending import counters
from vending.slots import DEVICE_ID, our_slots

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
    price_prior: int | None = None       # sale: the price at the poll before `since`, in cents
    booked_at: datetime | None = None    # sale_before_change: the poll that measured its window
    changes: int = 0                     # sale: product changes inside a window left unsplit


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
    The ledger's cost of sales is the sum of these, plus `unattributed_cost_cents`:
    for units sold across a product change the counters can't split, the cost
    they took out of the new product's count. Their product is uncertain, so
    `cost_cents` stays None, but the cost still lands in cost of sales and the
    books add up (#112). `at` is the poll that
    measured the units, the same moment the takings book them to. The old
    product's share of a window split at a product change left its count
    earlier, at the change: that is `taken_at`.
    """
    at: datetime
    slot: str
    product: str
    units: int
    costed_units: int
    cost_cents: Fraction | None
    taken_at: datetime | None = None   # when the units left the count, if before `at`
    unattributed_cost_cents: Fraction | None = None   # an unsplit window's cost; see above


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


class StockWarning(str):
    """A warning's text, with the selection it is about (`slot`; None for the closet).

    Still a `str` everywhere it is shown. The public dashboard keeps a warning only when its
    `slot` is one of ours (`vending.dashboard.only_ours`), instead of reading the slot out of the text."""

    slot: str | None

    def __new__(cls, text: str, slot: str | None) -> StockWarning:
        made = super().__new__(cls, text)
        made.slot = slot
        return made


@dataclass(frozen=True)
class Stock:
    closet: tuple[ClosetCount, ...]
    slots: tuple[SlotCount, ...]
    warnings: tuple[StockWarning, ...] = ()
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
    that spans a change then can't be split, and its sales keep the new
    product's average cost as `unattributed_cost_cents`.
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
        polled = sorted(at for at, _ in by_selection[selection])
        before = dict(zip(polled[1:], polled))   # each poll's previous poll
        cents_rose = {at: (start, rise) for start, at, rise in counters.windows(cents_by_selection.get(selection, ()))}
        for start, at, units in counters.windows(by_selection[selection]):
            # Only a cents rise over the same window can split these units.
            # No cents rise at `at` is unknown, not 0: the cents counter may have misread there.
            known = at in cents_rose and cents_rose[at][0] == start
            moves.append(Movement(at, "sale", selection, None, units=units, since=start,
                                  cents=cents_rose[at][1] if known else None,
                                  price_before=prices.get((selection, start)),
                                  price_after=prices.get((selection, at)),
                                  price_prior=prices.get((selection, before.get(start)))))
    return moves


def _old_units(m: Movement, change: datetime) -> int | None:
    """How many of a window's units sold at the old price, or None when the counters can't say.

    With `n` units and `c` cents in the window, old price `a` and new price
    `b`: `a*old + b*(n - old) = c`, so `old = (b*n - c) / (b - a)`. Only a whole
    number from 0 to `n` fits; anything else is a misread or a third price.
    The same price on both sides can't be split at all (#97), except when
    the window's first poll landed during the visit: the price had already
    changed from the poll before it, and the change ends the visit within
    `_VISIT_MAX`. Then the old product was already out, so none sold at the
    old price (#112).
    """
    a, b = m.price_before, m.price_after
    if (a is not None and a == b and m.price_prior is not None and m.price_prior != a
            and m.since is not None and change - m.since <= _VISIT_MAX):
        return 0
    if a is None or b is None or m.cents is None or a == b:
        return None
    old, rest = divmod(b * m.units - m.cents, b - a)
    return old if not rest and 0 <= old <= m.units else None


# The longest a restock visit runs, from the poll inside it to its `resolved_at`.
_VISIT_MAX = timedelta(hours=1)


def _split_at_changes(movements: list[Movement]) -> list[Movement]:
    """Split each sale whose window spans a product change into the old product's and the new one's.

    The old product's units are replayed at the change, just before the pull,
    so they come out of the old count instead of being written off as
    shrinkage, but they keep the window's poll as `booked_at`. A window the
    counters can't split, or one with two changes in it, is left whole, and
    `weighted_average` costs it at the new product's average, kept as
    `unattributed_cost_cents`. Splitting twice changes nothing.

    The change is the receipt's `resolved_at`, the end of the visit. A poll
    that lands during a visit, after the worker set the new price, starts a
    window with the new price at both ends; `_old_units` reads that as no old
    units sold when the poll before it shows the old price. Door-closed times
    (not built yet) would pin the visit exactly. Units are assumed not to sell
    while the door is open.
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
        old = _old_units(m, inside[0]) if len(inside) == 1 else None
        if old is None:
            out.append(replace(m, changes=len(inside)) if inside else m)
            continue
        if old:
            out.append(replace(m, at=inside[0], kind="sale_before_change", units=old, booked_at=m.at))
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
    written_off: int = 0          # units of `replaced` written off as shrinkage at the change


def weighted_average(movements: list[Movement]) -> Stock:
    """Replay movements in time order and value what is left at weighted-average cost.

    This is the one place the cost method lives. Each pool's cost is the sum
    of what went in, and units leave it at the pool's average at that moment.
    """
    closet: dict[str, _Pool] = {}
    slots: dict[str, _Slot] = {}
    warnings: list[StockWarning] = []

    def warn(slot: str | None, text: str) -> None:
        warnings.append(StockWarning(text, slot))
    sales: list[SaleCost] = []
    shrinkage: list[Shrinkage] = []

    def shelf(sku: str) -> _Pool:
        return closet.setdefault(sku, _Pool())

    for m in sorted(_split_at_changes(movements), key=_replay_order):
        slot = slots.get(m.slot) if m.slot else None
        if m.kind == "pull":
            if slot is None or slot.product != m.sku:
                warn(m.slot, f"{m.ref}: {m.units} units of {m.sku} pulled from slot {m.slot}, "
                                "which we never loaded with it; their cost is unknown")
                shelf(m.sku).add(m.units, None)
                continue
            # Sales can have emptied the count; the units still cost what the last ones did.
            each = slot.pool.unit_cost() if slot.pool.units else slot.last_unit_cost
            taken, _ = slot.pool.take(m.units)
            if taken < m.units:
                warn(m.slot, f"{m.ref}: {m.units} units pulled from slot {m.slot} but we counted only {taken}")
            shelf(m.sku).add(m.units, None if each is None else each * m.units)
            slot.last_moved = m.at
        elif m.kind in ("purchase", "load"):
            changed = slot is not None and slot.product != m.sku
            if changed != m.reassigned and slot is not None:
                warn(m.slot, f"{m.ref}: slot {m.slot} went from {slot.product} to {m.sku} "
                                f"but slot_reassigned is {str(m.reassigned).lower()}")
            if changed and slot.pool.units:
                warn(m.slot, f"{m.ref}: {slot.pool.units} units of {slot.product} still counted in slot "
                                f"{m.slot} when it changed to {m.sku}; written off as shrinkage")
                shrinkage.append(Shrinkage(m.at, f"slot {m.slot}", slot.product, slot.pool.units, slot.pool.cost))
            if slot is None or changed:
                slot = slots[m.slot] = _Slot(product=m.sku, since=m.at, replaced=slot.product if changed else None,
                                             written_off=slot.pool.units if changed else 0)
            if m.kind == "purchase":
                survivors = m.to_slot + m.to_closet
                each = None if m.paid_cents is None or not survivors else m.paid_cents / survivors
                if m.paid_cents is None:
                    warn(m.slot, f"{m.ref}: bought {m.sku} for slot {m.slot} with no total_paid; its cost is unknown")
                elif not survivors and m.lost:
                    warn(m.slot, f"{m.ref}: every unit of {m.sku} for slot {m.slot} was lost before loading; "
                                    "the whole line is written off as shrinkage")
                    shrinkage.append(Shrinkage(m.at, f"slot {m.slot}", m.sku, m.lost, m.paid_cents))
                shelf(m.sku).add(m.to_closet, None if each is None else each * m.to_closet)
                slot.pool.add(m.to_slot, None if each is None else each * m.to_slot)
            else:
                wanted = m.to_slot + m.lost
                taken, cost = shelf(m.sku).take(wanted)
                if taken < wanted:
                    warn(m.slot, f"{m.ref}: loaded {wanted} units of {m.sku} from the closet "
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
                warn(m.slot, f"{m.ref}: {m.units} units of {m.sku} lost in slot {m.slot}, "
                                "which no longer holds it; ignored")
                continue
            if gone:
                shrinkage.append(Shrinkage(m.at, where, m.sku, gone, cost))
            if gone < m.units:
                warn(None if where == "the closet" else m.slot,
                     f"{m.ref}: {m.units} units of {m.sku} lost in {where} but we counted only {gone}")
        elif m.kind in ("sale", "sale_before_change") and slot is not None:
            taken, cost = slot.pool.take(m.units)
            slot.sold += m.units
            slot.oversold += m.units - taken
            if m.kind == "sale_before_change" and taken < m.units:
                # The old slot is replaced next, so the oversold check at the end never sees it.
                warn(m.slot, f"slot {m.slot}: the counters split {m.units} units of {slot.product} to before "
                                f"the change at {m.at.isoformat()}, but we counted only {taken}")
            if m.kind == "sale" and slot.replaced and m.since is not None and m.since < slot.since:
                # Still taken from the new count, so on hand errs low, but their
                # cost could be either product's, so it is left out (#97). Old
                # units that sold here were also written off at the change.
                across = (f"across {m.changes} product changes, the last from {slot.replaced} to {slot.product}"
                          if m.changes > 1 else f"across the change from {slot.replaced} to {slot.product}")
                written_off = (f"; the {slot.written_off} units of {slot.replaced} written off as shrinkage at the "
                               "change may be some of these sales, not losses" if slot.written_off else "")
                warn(m.slot, f"slot {m.slot}: {m.units} units sold between the polls at {m.since.isoformat()} "
                                f"and {m.at.isoformat()}, {across}; the counters can't split them, so they are "
                                f"costed at {slot.product}'s average, which may be the wrong product's{written_off}")
                sales.append(SaleCost(m.at, m.slot, slot.product, m.units, taken, None,
                                      unattributed_cost_cents=cost))
            else:
                sales.append(SaleCost(m.booked_at or m.at, m.slot, slot.product, m.units, taken, cost,
                                      taken_at=m.at if m.booked_at else None))
        if slot is not None and slot.pool.units:
            slot.last_unit_cost = slot.pool.unit_cost()

    for name, slot in sorted(slots.items()):
        if slot.oversold:
            warn(name, f"slot {name}: the polls show {slot.oversold} more sales than units we loaded "
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


def _receipts(conn: psycopg.Connection) -> tuple[list[dict], list[dict]]:
    """Every fulfilled line and every loss recorded against one: our own purchases, whatever the assignment."""
    lines = [dict(zip(_LINE_COLUMNS, row)) for row in conn.execute(
        f"select {', '.join('l.' + c for c in _LINE_COLUMNS)} from purchase_lines l"
        " join purchase_requests r using (request_id) where r.status = 'fulfilled'")]
    losses = [dict(zip(_LOSS_COLUMNS, row)) for row in conn.execute(
        "select o.request_id, o.line_no, l.slot, o.location, o.sku, o.units, o.recorded_at"
        " from purchase_line_losses o join purchase_lines l using (request_id, line_no)"
        " join purchase_requests r using (request_id) where r.status = 'fulfilled'")]
    return lines, losses


def closet(conn: psycopg.Connection) -> tuple[ClosetCount, ...]:
    """The closet now, from the receipts alone: no slot assignment and no counters needed.

    The closet isn't a selection, so it can be counted when no assignment is stored. The units are
    exactly what `stock` counts: only purchases, loads, pulls and closet losses move them, never a sale.
    A product pulled from a slot came out at that slot's cost, and sales between its loads change
    that cost, so its unit cost here is unknown (None) rather than a figure that skipped the sales.
    """
    return closet_from_receipts(*_receipts(conn))


def closet_from_receipts(lines: list[dict], losses: list[dict]) -> tuple[ClosetCount, ...]:
    """`closet` without the database: the same receipt fields as `movements_from_receipts`."""
    pulled = {line["pulled_sku"] for line in lines if line["units_pulled"]}
    return tuple(ClosetCount(c.product, c.units, None if c.product in pulled else c.unit_cost)
                 for c in weighted_average(movements_from_receipts(lines, losses)).closet)


def stock(conn: psycopg.Connection, until: datetime | None = None) -> Stock:
    """Closet and slot stock now, or as of `until`, from every fulfilled request and every good poll."""
    assigned = set(our_slots(conn))
    lines, losses = _receipts(conn)
    # Receipts preserve our purchases, but cannot authorize counter reads after reassignment.
    slots = sorted({line["slot"] for line in lines} & assigned)
    # poll_selections only has rows for polls whose checksum passed. The sync
    # stores every device's polls, so read only our machine's counters.
    # Cents and price too, to split a window that spans a product change (#97).
    counters = conn.execute(
        "select s.selection, p.started_at, s.lifetime_units, s.lifetime_sales_cents, s.price_cents"
        " from poll_selections s"
        " join polls p using (session_id) where p.device_id = %s and s.selection = any(%s)",
        (DEVICE_ID, slots),
    ).fetchall()
    return replay(movements_from_receipts(lines, losses) + sales_from_counters(counters), until)


def replay(movements: list[Movement], until: datetime | None = None) -> Stock:
    """`weighted_average` of the movements up to `until`. Pure.

    Windows that span a product change are split before the cut (#97). A
    period can end between a swap and the poll that measures the window; the
    old product's share then still leaves its count at the swap, instead of
    being written off as shrinkage. Its sale is booked to that later poll,
    like the takings, so it is in the stock as of `until` but not in its sales.
    So for one period opening + purchases - cost of sales - shrinkage = closing
    can be off by that share; over the two periods together it adds up.
    """
    movements = _split_at_changes(movements)
    if until is None:
        return weighted_average(movements)
    counted = weighted_average([m for m in movements if m.at <= until])
    return replace(counted, sales=tuple(s for s in counted.sales if s.at <= until))


_LINE_COLUMNS = ("request_id", "line_no", "slot", "outcome", "sku", "total_paid", "units_loaded",
                 "units_lost", "units_to_back_stock", "units_from_back_stock", "units_pulled",
                 "pulled_sku", "slot_reassigned", "resolved_at")
_LOSS_COLUMNS = ("request_id", "line_no", "slot", "location", "sku", "units", "recorded_at")
