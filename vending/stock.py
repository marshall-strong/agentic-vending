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
- a receipt line is timed by its request's door times when the receipt has
  them: pulls at door open, purchases and loads at door close. Without them,
  everything is timed at the line's `resolved_at`, when the worker filled it in.
  The door times are to the minute, so each is a minute it happened in, not an
  instant (`_split_by_door`). Two visits to a slot that share a minute go in
  the order they were submitted, or by `resolved_at` if that can't settle it
  (`_usable_door_times`).
- a poll can't tell when a worker swapped a slot's product (#97), so a window
  between two polls that spans the swap holds units of both. The cents
  counter and the price at each end split them. Units it can't split keep
  the cost taken from the slot (`unattributed_cost_cents`), which is the new
  product's average cost and may be the wrong product's; the ledger counts it
  in cost of sales and says so in a note, so profit isn't overstated.

A slot's count starts again whenever its product changes, so `loaded` and
`sold` are since the last change, and on hand is loaded - sold - lost.

**Receipts before the ledger's opening don't count once it exists.** Purchases
made before the opening are tests: they never came out of the $600 (#104), so
they are not our stock and cost nothing in the competition. `vending.ledger`
leaves their purchases and visit fees out at the same boundary,
`receipts_count_from`, read on the same clock (`receipt_lines`): door close
when the replay uses the receipt's door times, else the line's `resolved_at`.
Until the opening
is recorded every receipt counts, so the planner and the tools can be tested
on test purchases. Sales counters are read as before; a sale from a slot with
no counted receipt is sold but not costed.

The cost method is weighted average. The team hasn't chosen between that and
FIFO, so the method lives in `weighted_average` and nowhere else: replace that
one function to change it. Money is exact cents (`Fraction`), never a float.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from itertools import combinations
from typing import TYPE_CHECKING

from vending import counters
from vending.slots import DEVICE_ID, our_slots

if TYPE_CHECKING:  # the replay itself needs no database, so its tests run without psycopg
    import psycopg

# Within one moment: receipt movements come first, request by request in the
# order they were submitted (`Movement.seq`), and losses and sales are only
# ever recorded after them. Within one request, pulls happen before loads (the
# old product comes out first), and the old product's share of a window split
# at the request's product change sold before the door opened, so it goes first.
_ORDER = {"sale_before_change": -1, "pull": 0, "purchase": 1, "load": 1, "loss": 2, "sale": 3}
_GROUP = {"sale_before_change": 0, "pull": 0, "purchase": 0, "load": 0, "loss": 1, "sale": 2}

# Door times are to the minute. Each is read as the minute it happened in:
# the door opened in [open, open + 1 min) and closed in [close, close + 1 min).
_MINUTE = timedelta(minutes=1)


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
    opened_at: datetime | None = None    # purchase, load: the door opened then; `at` is door close (None: no door times)
    seq: int = 0                         # receipt movements: the request's place in submission order (0: unknown)
    notes: tuple[StockWarning, ...] = ()  # warnings to pass on when this movement is replayed, each with its slot
    door_conflict: str = ""              # sale: the receipt it contradicts, by selling with the door open
    split: bool = False                  # sale: already split at its changes; another pass leaves it alone


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

    def __getnewargs__(self) -> tuple[str, str | None]:     # copy, deepcopy and pickle keep the slot
        return str(self), self.slot


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
    A line may also carry its request's `door_opened_at` and `door_closed_at`.
    With both, the pull is at door open and the purchase or load at door close.
    Without them, all of it is at `resolved_at`. A line may also carry its
    request's `submitted_at`, which orders door-timed requests whose movements
    share a moment. Lines without door times keep main's order at one moment:
    every pull before any load, whichever request it came from.
    """
    lines = _usable_door_times(lines)
    submitted = sorted({(line["submitted_at"], line["request_id"]) for line in lines
                        if line.get("submitted_at") is not None})
    seqs = {request_id: n for n, (_, request_id) in enumerate(submitted, start=1)}
    moves = []
    for line in lines:
        ref = f"{line['request_id']} line {line['line_no']}"
        slot, first = line["slot"], len(moves)
        opened, closed = line.get("door_opened_at"), line.get("door_closed_at")
        if opened is None or closed is None:
            opened, seq = None, 0
            pulled_at = loaded_at = line["resolved_at"]
        else:
            pulled_at, loaded_at, seq = opened, closed, seqs.get(line["request_id"], 0)
        if line["units_pulled"]:
            moves.append(Movement(pulled_at, "pull", slot, line["pulled_sku"], units=line["units_pulled"], ref=ref,
                                  seq=seq))
        if line["outcome"] in ("primary", "alternate"):
            moves.append(Movement(
                loaded_at, "purchase", slot, line["sku"], to_slot=line["units_loaded"],
                to_closet=line["units_to_back_stock"], lost=line["units_lost"],
                paid_cents=cents(line["total_paid"]), reassigned=line["slot_reassigned"], ref=ref,
                opened_at=opened, seq=seq))
        elif line["outcome"] == "loaded":
            moves.append(Movement(
                loaded_at, "load", slot, line["sku"], to_slot=line["units_loaded"],
                lost=line["units_from_back_stock"] - line["units_loaded"],
                reassigned=line["slot_reassigned"], ref=ref, opened_at=opened, seq=seq))
        if len(moves) > first and line.get("door_notes"):
            # Each note names its own slot, which may not be this line's (the replay passes each on once).
            moves[first] = replace(moves[first], notes=line["door_notes"])
    for loss in losses:
        moves.append(Movement(
            loss["recorded_at"], "loss", loss["slot"], loss["sku"], units=loss["units"],
            location=loss["location"], ref=f"{loss['request_id']} line {loss['line_no']}"))
    return moves


def _usable_door_times(lines: list[dict]) -> list[dict]:
    """The lines, without door times on visits whose order in a slot can't be told.

    Two requests' visits to one slot that share a minute leave the replay
    unsure which went first: say one closed at 15:34 and the next opened at
    15:34. They go in the order they were submitted, when that differs and
    the earlier one's door closed no later than the later one's opened.
    Otherwise (submitted together, or door times that overlap or disagree with
    the submission order) neither visit's door times are used: their lines are
    timed by `resolved_at`, as without door times. Each of those lines carries
    `door_notes`, one warning per slot where its visit clashed, each about that
    slot. Running this again on its own output changes nothing.
    """
    visits: dict[str, dict[str, tuple]] = {}
    for line in lines:
        opened, closed = line.get("door_opened_at"), line.get("door_closed_at")
        if opened is not None and closed is not None:
            visits.setdefault(line["slot"], {})[line["request_id"]] = (opened, closed, line.get("submitted_at"))
    notes: dict[str, dict[str, StockWarning]] = {}
    for slot, by_request in sorted(visits.items()):
        for (p, (p_open, p_close, p_sent)), (q, (q_open, q_close, q_sent)) in combinations(
                sorted(by_request.items()), 2):
            if not (p_open < q_close + _MINUTE and q_open < p_close + _MINUTE):
                continue   # no minute in common, so the door times order them
            if p_sent is not None and q_sent is not None and p_sent != q_sent:
                early_close, late_open = (p_close, q_open) if p_sent < q_sent else (q_close, p_open)
                if early_close <= late_open:
                    continue   # the earlier request's door closed in the minute the later one's opened
            for request_id, other in ((p, q), (q, p)):
                notes.setdefault(request_id, {}).setdefault(slot, StockWarning(
                    f"{request_id}: its door times share a minute with {other}'s in slot {slot}, and which "
                    "visit came first can't be told; its lines are timed by resolved_at instead", slot))
    if not notes:
        return lines
    return [{**line, "door_opened_at": None, "door_closed_at": None,
             "door_notes": tuple(notes[line["request_id"]].values())} if line["request_id"] in notes else line
            for line in lines]


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
    """How many of a window's units sold at the old price, when the timing can't say. Main's rule.

    `change` is the receipt's `resolved_at`, so where the visit sat is a guess,
    or its door close, for a window the door times can't settle (`_split_by_door`).
    The same price on both sides can't be split at all (#97), except when the
    window's first poll landed during the visit: the price had already changed
    from the poll before it, and the change ends the visit within `_VISIT_MAX`.
    Then the old product was already out, so none sold at the old price (#112).
    Otherwise the counters decide (`_solve`).
    """
    a, b = m.price_before, m.price_after
    if (a is not None and a == b and m.price_prior is not None and m.price_prior != a
            and m.since is not None and change - m.since <= _VISIT_MAX):
        return 0
    return _solve(m)


def _solve(m: Movement) -> int | None:
    """How many of a window's units sold at the old price, or None when the counters can't say.

    With `n` units and `c` cents in the window, old price `a` and new price
    `b`: `a*old + b*(n - old) = c`, so `old = (b*n - c) / (b - a)`. Only a whole
    number from 0 to `n` fits; anything else is a misread or a third price.
    The same price on both sides can't be split at all (#97).
    """
    a, b = m.price_before, m.price_after
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
    shrinkage, but they keep the window's poll as `booked_at`. A window that
    can't be split is left whole, and `weighted_average` costs it at the
    average of what the slot holds then, kept as `unattributed_cost_cents`.
    Splitting twice changes nothing.

    Without door times, the change is the receipt's `resolved_at`, when the
    worker filled in the line. A window that holds it is split by `_old_units`;
    one with two changes in it is left whole. With door times, `_split_by_door`
    splits the window. Units are assumed not to sell while the door is open.
    """
    # Per slot, each product change (the purchase or load that made it), in replay order.
    changes: dict[str, list[Movement]] = {}
    holds: dict[str, str] = {}
    for m in sorted(movements, key=_replay_order):
        if m.kind in ("purchase", "load"):
            if holds.get(m.slot, m.sku) != m.sku:
                changes.setdefault(m.slot, []).append(m)
            holds[m.slot] = m.sku
    out = []
    for m in movements:
        if m.kind != "sale" or m.since is None or m.split:
            out.append(m)
            continue
        inside = [c for c in changes.get(m.slot, ()) if _touches(m, c)]
        if not inside:
            out.append(m)
        elif all(c.opened_at is not None for c in inside):
            out.extend(_split_by_door(m, inside))
        elif len(inside) == 1:
            out.extend(_shares(m, inside[0], _old_units(m, inside[0].at)))
        else:
            out.append(replace(m, changes=len(inside), split=True))
    return out


def _touches(m: Movement, change: Movement) -> bool:
    """Whether a sale's window could hold any of a change's visit.

    Without door times the change is the instant `resolved_at`. With them the
    visit runs from the start of the door-open minute to the end of the
    door-close minute. A window that ends at the very start of the open minute
    touches it too, so its sales are replayed before that visit's pull.
    """
    if change.opened_at is None:
        return m.since < change.at <= m.at
    return m.since < change.at + _MINUTE and change.opened_at <= m.at


def _split_by_door(m: Movement, inside: list[Movement]) -> list[Movement]:
    """Split a sale's window by the door times of the changes it touches (`inside`, in order).

    No unit sells while the door is open. With p0 the product before the first
    change and p1, p2, ... the products after each, p_j can only have sold
    between the previous change's door closing and the next one's opening.
    Each door time is a minute, not an instant: the door closed no earlier
    than the start of its close minute, and it had opened by the end of its
    open minute. So p_j can be in the window only if the window ends after the
    previous change's close minute starts, and starts before the next change's
    open minute ends.

    - One product possible: all the units are it. A window that ends before
      the close minute is all old; one that starts after the open minute is
      all new. That includes a window from inside one visit to inside the
      next: only the product loaded at the first can have sold.
    - Two possible, across one change: the window holds the whole visit, or
      a poll fell in the open or close minute, which can't settle the
      boundary. The counters decide, as without door times (`_old_units`);
      the same price on both sides stays unsplit.
    - None possible: the window lies wholly inside a visit, with the door
      open. A sale then contradicts the receipt, so it is flagged
      (`door_conflict`) and left unattributed.
    - Anything else (more than one product possible across two or more
      changes): left unattributed.
    """
    k = len(inside)
    possible = [j for j in range(k + 1)
                if (j == 0 or m.at > inside[j - 1].at) and (j == k or m.since < inside[j].opened_at + _MINUTE)]
    if not possible:
        visit = next(c for c in inside if m.since >= c.opened_at + _MINUTE and m.at <= c.at)
        return [replace(m, changes=k, split=True, door_conflict=visit.ref.split(" line ")[0])]
    if len(possible) == 1:
        [j] = possible
        if j == k:
            return [replace(m, since=max(m.since, inside[-1].at), split=True)]
        change = inside[j]
        return [replace(m, at=change.opened_at, kind="sale_before_change", booked_at=m.at, seq=change.seq,
                        split=True)]
    if k == 1:
        [change] = inside
        # `_old_units` reads a window with the new price at both ends, and the old price at the poll
        # before, as starting during the visit. The door times rule that out for a window that starts
        # before the open minute, so there only the counters decide.
        return _shares(m, change, _old_units(m, change.at) if m.since >= change.opened_at else _solve(m))
    return [replace(m, changes=k, split=True)]


def _shares(m: Movement, change: Movement, old: int | None) -> list[Movement]:
    """A window split at one change: `old` units of the old product, the rest of the new one.

    The old product's share leaves at door open (or `resolved_at`), just before
    that request's pull; the new one's window starts at door close (or
    `resolved_at`). With `old` None the counters can't split it: it stays whole.
    """
    if old is None:
        return [replace(m, changes=1, split=True)]
    out = []
    if old:
        out.append(replace(m, at=change.opened_at or change.at, kind="sale_before_change", units=old,
                           booked_at=m.at, seq=change.seq, split=True))
    if old < m.units:
        out.append(replace(m, since=change.at, units=m.units - old, split=True))
    return out


def _replay_order(m: Movement) -> tuple[datetime, int, int, int]:
    return m.at, _GROUP[m.kind], m.seq, _ORDER[m.kind]


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
        for note in m.notes:
            if note not in warnings:
                warnings.append(note)
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
            across_change = slot.replaced and m.since is not None and m.since < slot.since
            if m.kind == "sale" and m.door_conflict:
                # The receipt says the door was open for the whole window. Taken from
                # the count the slot has then, but which product sold is unknown.
                warn(m.slot, f"slot {m.slot}: {m.units} units sold between the polls at {m.since.isoformat()} "
                             f"and {m.at.isoformat()}, while {m.door_conflict}'s door times say the door was "
                             "open; a sale with the door open contradicts the receipt, so they are left "
                             f"unattributed, costed at {slot.product}'s average, which may be the wrong product's")
                sales.append(SaleCost(m.at, m.slot, slot.product, m.units, taken, None,
                                      unattributed_cost_cents=cost))
            elif m.kind == "sale" and (across_change or m.changes):
                # Still taken from the count (the new one, unless the door times show the
                # window ended before it went in), so on hand errs low, but their cost
                # could be either product's, so it is left out (#97). Old units that
                # sold here were also written off at the change.
                if not across_change:
                    across = (f"across {m.changes} product changes" if m.changes > 1 else "across a product change")
                    across += f" not yet finished at {m.at.isoformat()}"
                elif m.changes > 1:
                    across = f"across {m.changes} product changes, the last from {slot.replaced} to {slot.product}"
                else:
                    across = f"across the change from {slot.replaced} to {slot.product}"
                written_off = (f"; the {slot.written_off} units of {slot.replaced} written off as shrinkage at the "
                               "change may be some of these sales, not losses" if across_change and slot.written_off
                               else "")
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


def opening_at(conn: psycopg.Connection) -> datetime | None:
    """When the ledger opened, or None. Read here, not from `vending.ledger`, which imports this module."""
    if conn.execute("select to_regclass('ledger_entries')").fetchone()[0] is None:
        return None
    row = conn.execute("select at from ledger_entries where kind = 'opening'").fetchone()
    return None if row is None else row[0]


def receipts_count_from(conn: psycopg.Connection, start: datetime | None = None) -> datetime | None:
    """The earliest moment a receipt (and the visit that brought it) counts: the later of `start` and
    the ledger's opening, or `start` (None: no limit) while there is no opening.

    The one boundary for stock here and for purchases and visit fees in `vending.ledger`, so a test
    purchase can't be left out of one figure and kept in another. See the module docstring.
    """
    opened = opening_at(conn)
    if opened is None:
        return start
    return opened if start is None else max(start, opened)


def receipt_lines(conn: psycopg.Connection) -> list[dict]:
    """Every fulfilled line, each with `at`: the one moment it counts from, the receipt clock.

    That is its request's door close when the replay can use the receipt's
    door times, else the line's own `resolved_at`. The replay falls back to
    `resolved_at` for visits whose order in a slot can't be told
    (`_usable_door_times`), so a line with door times isn't always at door
    close. The opening boundary here, the ledger's purchases and visit fees,
    the money chart and the machine state's load dates all read this clock, so
    a line can't be in one figure at 10:05 and in another at 09:55.
    """
    lines = _usable_door_times([dict(zip(_LINE_COLUMNS + _REQUEST_COLUMNS, row)) for row in conn.execute(
        f"select {', '.join('l.' + c for c in _LINE_COLUMNS)}, {', '.join('r.' + c for c in _REQUEST_COLUMNS)}"
        " from purchase_lines l join purchase_requests r using (request_id) where r.status = 'fulfilled'"
        " order by l.request_id, l.line_no")])
    for line in lines:
        door = line["door_opened_at"] is not None and line["door_closed_at"] is not None
        line["at"] = line["door_closed_at"] if door else line["resolved_at"]
    return lines


def request_times(lines: list[dict]) -> dict[str, datetime]:
    """Each request's visit on the receipt clock: its earliest line's `at` (`receipt_lines`)."""
    out: dict[str, datetime] = {}
    for line in lines:
        if line["at"] is not None and (line["request_id"] not in out or line["at"] < out[line["request_id"]]):
            out[line["request_id"]] = line["at"]
    return out


def _receipts(conn: psycopg.Connection) -> tuple[list[dict], list[dict]]:
    """Every fulfilled line and every loss recorded against one: our own purchases, whatever the assignment.

    Once the ledger has an opening entry, lines before it on the receipt clock (`receipt_lines`) are
    test purchases, and they and their later losses are left out: see the module docstring.
    """
    since_at = receipts_count_from(conn)
    lines = [line for line in receipt_lines(conn)
             if since_at is None or (line["at"] is not None and line["at"] >= since_at)]
    kept = {(line["request_id"], line["line_no"]) for line in lines}
    losses = [loss for loss in (dict(zip(_LOSS_COLUMNS, row)) for row in conn.execute(
        "select o.request_id, o.line_no, l.slot, o.location, o.sku, o.units, o.recorded_at"
        " from purchase_line_losses o join purchase_lines l using (request_id, line_no)"
        " join purchase_requests r using (request_id) where r.status = 'fulfilled'"))
        if (loss["request_id"], loss["line_no"]) in kept]
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
    """Closet and slot stock now, or as of `until`, from every fulfilled request and every good poll.

    Once the ledger has an opening entry, receipt lines resolved before it (test purchases) and their
    later losses are left out (`_receipts`): see the module docstring.
    """
    return replay(load_movements(conn), until)


def load_movements(conn: psycopg.Connection) -> list[Movement]:
    """Every stock movement on record, unsorted: what `stock()` replays.

    Separate so a caller that needs the stock at many moments (the daily money
    series) can read the history once and pass it to `replay(movements, moment)`
    for each, which is exactly `stock(conn, until=moment)` without a query per
    moment. Don't cut a prefix yourself: `replay` must split product changes
    (#97) before it cuts.
    """
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
    return movements_from_receipts(lines, losses) + sales_from_counters(counters)


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
_REQUEST_COLUMNS = ("submitted_at", "door_opened_at", "door_closed_at")   # the request's, on every one of its lines
_LOSS_COLUMNS = ("request_id", "line_no", "slot", "location", "sku", "units", "recorded_at")
