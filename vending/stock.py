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
  (`coil` is the slot, `back_stock` the closet), oldest units first, at what
  those units cost. That cost is **shrinkage**: expensed, so the P&L sees it and the
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
  counter and the price at each end split them. Units it can't split are
  unattributed (#172): booked to neither product, with the reason
  (`SaleCost.unattributed`, `SlotCount.unattributed`). They still leave the
  slot's known count and keep the cost taken from it (`unattributed_cost_cents`):
  that of the new product's oldest units, which may be the wrong product's; the ledger
  counts it in cost of sales and says so in a note, so profit isn't overstated.

A slot's count starts again whenever its product changes, so `loaded` and
`sold` are since the last change, and on hand is loaded - sold - unattributed
- lost: what we know is there. More may be, if unattributed sales were the old
product's, but never more than `at_most`: what is left if none of them were
this product's.

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

The cost method is first in, first out by load, the team's choice since
19 Nov (#128; it came in with #172, replacing weighted average): sales,
pulls, losses and loads take the oldest units first, so a sale is costed at
the stock that was there. What is left is valued at its remaining layers'
costs, and a count's `unit_cost` is the exact mean of them. The method lives in
`first_in_first_out` and `_Pool`, nowhere else. Money is exact cents
(`Fraction`), never a float.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from fractions import Fraction
import re
from typing import TYPE_CHECKING

from vending import counters
from vending.slots import DEVICE_ID, our_slots

if TYPE_CHECKING:  # the replay itself needs no database, so its tests run without psycopg
    import psycopg

# Within one moment: receipt movements come first, and losses and sales are
# only ever recorded after them. Among receipt movements, as on main, every
# pull goes before every load, across visits and whatever the submission order
# (the old product comes out first, and a pull can stock the closet a load
# takes from), and the old product's share of a window split at a product
# change sold before that, so it goes first of all. The one exception is two
# door-timed visits to the same slot that share the minute: the later-submitted
# one (`Movement.seq`) goes wholly after the earlier one's load there, since it
# pulls what that load put in (`_in_replay_order`).
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
    prior_at: datetime | None = None     # sale: when that poll was taken
    window_since: datetime | None = None # sale: the counters' window start; `since` moves when a window is split
    booked_at: datetime | None = None    # sale_before_change: the poll that measured its window
    changes: int = 0                     # sale: product changes inside a window left unsplit
    opened_at: datetime | None = None    # purchase, load: the door opened then; `at` is door close (None: no door times)
    seq: int = 0                         # receipt movements: the request's place in submission order (0: unknown)
    door: tuple[datetime, datetime] | None = None   # receipt movements: the receipt's door times, even if not used
    notes: tuple[StockWarning, ...] = ()  # warnings to pass on when this movement is replayed, each with its slot

    def __post_init__(self) -> None:
        # A sale made with only `since` still knows where its window began once a split moves `since`.
        if self.window_since is None and self.kind == "sale" and self.since is not None:
            object.__setattr__(self, "window_since", self.since)
    door_conflict: str = ""              # sale: the receipt it contradicts, by selling with the door open
    split: bool = False                  # sale: already split at its changes; another pass leaves it alone
    read_as_new: bool = False            # sale: unsplit, but main booked it all to the new product (`_old_units`)


@dataclass(frozen=True)
class ClosetCount:
    """Units of one product in the closet. Fields match the planner's `ClosetStock`.

    `unit_cost` is None when some of the units have no known cost (old
    product pulled from a slot we never loaded). Otherwise it is the exact
    mean of `layers`, the units by arrival, oldest first, the order loads
    take them in.
    """
    product: str
    units: int
    unit_cost: Fraction | None   # cents per unit
    layers: tuple[tuple[int, Fraction | None], ...] = ()   # (units, cents per unit), oldest first


@dataclass(frozen=True)
class SlotCount:
    """One slot since its last product change. `units` and `unit_cost` feed `SlotState`.

    `unattributed` units sold in windows that span a product change the
    counters and door times can't split. They are not in `sold`, since they
    may have been the old product's. They are out of `units` (the units we
    know are there), so on hand is `units` to `at_most`: what would be left if
    none of them were this product's. Later sales, pulls and losses lower
    `at_most` too; `unattributed` stays the count of those sales.
    """
    slot: str
    product: str
    units: int                   # loaded - sold - unattributed - lost, never below 0
    unit_cost: Fraction | None   # cents per unit of those units: the mean of `layers`, None if one is unknown
    loaded: int
    sold: int
    lost: int
    since: datetime
    last_moved: datetime | None = None   # the slot's latest purchase, load, pull or loss
    unattributed: int = 0                # sold, but which product is unknown; see above
    at_most: int | None = None           # the most that can be on hand; see above (None: `units`)
    layers: tuple[tuple[int, Fraction | None], ...] = ()   # `units` by arrival as (units, cents per unit), oldest first


@dataclass(frozen=True)
class SaleCost:
    """Units sold from one slot between two polls, and what they had cost us.

    `cost_cents` is what those of known cost cost, and `costed_units` is how
    many of them that is; the rest had no known cost or were past the count
    (oversold). `cost_cents` is None only when every unit counted had no known cost.
    The ledger's cost of sales is the sum of these, plus `unattributed_cost_cents`:
    for units sold across a product change the counters can't split, the cost
    they took out of the new product's count. Their product is unknown, so
    `product` is None, `unattributed` says why, and `cost_cents` stays None,
    but the cost still lands in cost of sales and the books add up (#112,
    #172). A window main read as all the new product's, and #172 leaves
    unattributed, keeps that cost in `cost_cents` instead, as main had it, so
    the money series still counts it as the ledger does. `at` is the poll that
    measured the units, the same moment the takings book them to. The old
    product's share of a window split at a product change left its count
    earlier, at the change: that is `taken_at`.
    """
    at: datetime
    slot: str
    product: str | None                # None: unattributed
    units: int
    costed_units: int
    cost_cents: Fraction | None
    taken_at: datetime | None = None   # when the units left the count, if before `at`
    unattributed_cost_cents: Fraction | None = None   # an unsplit window's cost; see above
    unattributed: str = ""             # why the product is unknown; empty when it is known
    since: datetime | None = None      # the poll before `at`: the measured window is (since, at]


@dataclass(frozen=True)
class Shrinkage:
    """Units gone without a sale, and what they had cost us: a later loss, units
    still counted in a slot when its product changed, or a purchase line that
    lost every unit. `cost_cents` is None when their cost was unknown; units
    of known and unknown cost lost together are two of these, so the known
    part still counts. The ledger's shrinkage line is the sum of these."""
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


# How a warning names a selection: "slot 15". Plurals, lists and ranges ("slots 15-17",
# "slots 12, 15", "slot 12 and 15") are read too, so a new wording can't slip one past.
_SLOT_NAMED = re.compile(r"\b(?:slot|selection) (\w+)", re.IGNORECASE)
_SLOT_LIST = re.compile(r"\b(?:slots?|selections?) (\d+(?:\s*(?:,|-|–|and|or)\s*(?:(?:slot|selection)\s+)?\d+)*)",
                        re.IGNORECASE)
_LIST_SEP = re.compile(r"\s*(?:,|\band\b|\bor\b)\s*(?:(?:slot|selection)\s+)?", re.IGNORECASE)


def warning_is_ours(warning: str, ours: set[str]) -> bool:
    """Keep a warning only when this module attached a selection to it (`StockWarning.slot`), that
    selection is ours or it is about the closet (None), and its text names no other. A warning with
    nothing attached is dropped: no caller guesses the selection from the text alone. The dashboard
    and the agent's machine state both filter by this, so neither shows a slot we don't hold."""
    if not hasattr(warning, "slot"):
        return False
    if warning.slot is not None and warning.slot not in ours:
        return False
    return _slots_named(warning) <= ours


def _slots_named(warning: str) -> set[str]:
    """Every selection a warning names. A warning that mentions slots but names none we can read
    counts as naming one that isn't ours."""
    named = set(_SLOT_NAMED.findall(warning))
    for listed in _SLOT_LIST.findall(warning):
        for part in _LIST_SEP.split(listed):
            low, _, high = (x.strip() for x in part.replace("–", "-").partition("-"))
            if high.isdigit() and low.isdigit():
                a, b = sorted((int(low), int(high)))
                named.update(str(n) for n in range(a, b + 1))
            elif low:
                named.add(low)
    if not named and re.search(r"\b(?:slots?|selections?)\b", warning, re.IGNORECASE):
        named.add("?")
    return named


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
        door = line.get("door_recorded") or ((opened, closed) if door_timed(line) else None)
        if not door_timed(line):
            opened, seq = None, 0
            pulled_at = loaded_at = line["resolved_at"]
        else:
            pulled_at, loaded_at, seq = opened, closed, seqs.get(line["request_id"], 0)
        if line["units_pulled"]:
            moves.append(Movement(pulled_at, "pull", slot, line["pulled_sku"], units=line["units_pulled"], ref=ref,
                                  seq=seq, door=door))
        if line["outcome"] in ("primary", "alternate"):
            moves.append(Movement(
                loaded_at, "purchase", slot, line["sku"], to_slot=line["units_loaded"],
                to_closet=line["units_to_back_stock"], lost=line["units_lost"],
                paid_cents=cents(line["total_paid"]), reassigned=line["slot_reassigned"], ref=ref,
                opened_at=opened, seq=seq, door=door))
        elif line["outcome"] == "loaded":
            moves.append(Movement(
                loaded_at, "load", slot, line["sku"], to_slot=line["units_loaded"],
                lost=line["units_from_back_stock"] - line["units_loaded"],
                reassigned=line["slot_reassigned"], ref=ref, opened_at=opened, seq=seq, door=door))
        if len(moves) > first and line.get("door_notes"):
            # Each note names its own slot, which may not be this line's (the replay passes each on once).
            moves[first] = replace(moves[first], notes=line["door_notes"])
    for loss in losses:
        moves.append(Movement(
            loss["recorded_at"], "loss", loss["slot"], loss["sku"], units=loss["units"],
            location=loss["location"], ref=f"{loss['request_id']} line {loss['line_no']}"))
    return moves


def door_timed(line: dict) -> bool:
    """Whether the replay times this line by its request's door times: it has both, and `_usable_door_times`
    left them (a line of visits that clash has them cleared). The one place that says so."""
    return line.get("door_opened_at") is not None and line.get("door_closed_at") is not None


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
    slot, and `door_recorded`, the door times it had: the visit still happened
    then, which `_visits` needs. Running this again on its own output changes nothing.
    """
    visits: dict[str, dict[str, tuple]] = {}
    for line in lines:
        opened, closed = line.get("door_opened_at"), line.get("door_closed_at")
        if opened is not None and closed is not None:
            visits.setdefault(line["slot"], {})[line["request_id"]] = (opened, closed, line.get("submitted_at"))
    notes: dict[str, dict[str, StockWarning]] = {}
    for slot, by_request in sorted(visits.items()):
        for (p, (p_open, p_close, p_sent)), (q, (q_open, q_close, q_sent)) in _sharing_a_minute(by_request):
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
             "door_recorded": (line["door_opened_at"], line["door_closed_at"]),
             "door_notes": tuple(notes[line["request_id"]].values())} if line["request_id"] in notes else line
            for line in lines]


def _sharing_a_minute(by_request: dict[str, tuple]) -> list[tuple[tuple, tuple]]:
    """Each pair of visits to one slot that share a minute, `((p, visit), (q, visit))` with p < q.

    Sorted by door open, a visit can only share a minute with the ones after it
    that open before its close minute ends, so the scan stops there: linear in
    the visits plus the pairs found, not every pair.
    """
    by_open = sorted(by_request.items(), key=lambda item: (item[1][0], item[0]))
    pairs = []
    for i, p in enumerate(by_open):
        j = i + 1
        # q opened no earlier than p, so p opened before q's close minute ended too.
        while j < len(by_open) and by_open[j][1][0] < p[1][1] + _MINUTE:
            pairs.append(tuple(sorted((p, by_open[j]))))
            j += 1
    return sorted(pairs)


def sales_from_counters(rows: list[tuple]) -> list[Movement]:
    """Sales from `(selection, polled_at, lifetime_units, lifetime_sales_cents, price_cents)` rows.

    Lifetime counters never reset, so units sold between two polls are the
    difference. A counter that goes down (a board swap, a misread) starts a new
    baseline instead of producing negative sales.

    Each sale carries its window, the cents rise over it and the price at both
    ends, so `first_in_first_out` can split a window that spans a product change.
    Rows of just `(selection, polled_at, lifetime_units)` still work; a window
    that spans a change then can't be split, and its sales keep the cost of
    the new product's oldest units as `unattributed_cost_cents`.
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
                                  price_prior=prices.get((selection, before.get(start))),
                                  prior_at=before.get(start), window_since=start))
    return moves


def _old_units(m: Movement, change: Movement, visits: list[tuple[str, datetime, datetime]]) -> int | None:
    """How many of a window's units sold at the old price, when the timing can't say. Main's rule.

    `change.at` is the receipt's `resolved_at`, so where the visit sat is a guess,
    or its door close, for a window the door times can't settle (`_split_by_door`).
    The same price on both sides can't be split at all (#97), except when the
    window's first poll landed during the visit: the price had already changed
    from the poll before it, and the change ends the visit within `_VISIT_MAX`.
    Then the old product was already out, so none sold at the old price (#112).
    That holds only if this visit set the price. When another visit of ours
    (`visits`, from `_visits`) falls between those two polls, it may have, and
    the first poll may have come before this visit's door opened (#172).
    Otherwise the counters decide (`_solve`).
    """
    if _read_as_new(m, change) and not _price_set_by_another_visit(m, change, visits):
        return 0
    return _solve(m)


def _read_as_new(m: Movement, change: Movement) -> bool:
    """Whether main read the window as all the new product's: new price at both ends, another at the poll before."""
    a, b = m.price_before, m.price_after
    return (a is not None and a == b and m.price_prior is not None and m.price_prior != a
            and m.since is not None and change.at - m.since <= _VISIT_MAX)


def _split_one(m: Movement, change: Movement, visits: list[tuple[str, datetime, datetime]]) -> list[Movement]:
    """`_shares` at one change by `_old_units`. A window left unsplit that main read as all new is marked."""
    old = _old_units(m, change, visits)
    shares = _shares(m, change, old)
    return [replace(shares[0], read_as_new=True)] if old is None and _read_as_new(m, change) else shares


def _request(m: Movement) -> str:
    """The request a receipt movement came from."""
    return m.ref.split(" line ")[0]


def _visits(movements: list[Movement]) -> list[tuple[str, datetime, datetime]]:
    """`(request, start, end)` for each receipt movement: when that visit could have set a price.

    With door times on the receipt, from the start of the door-open minute to
    the end of the close minute, even when the replay times the movement by
    `resolved_at` instead (`Movement.door`). Without them, the instant `resolved_at`.
    """
    return [(_request(m), m.door[0], m.door[1] + _MINUTE) if m.door else (_request(m), m.at, m.at)
            for m in movements if m.kind in ("pull", "purchase", "load")]


def _price_set_by_another_visit(m: Movement, change: Movement, visits: list[tuple[str, datetime, datetime]]) -> bool:
    """Whether a visit of ours other than `change`'s falls between the poll before `m`'s window and its first."""
    own = _request(change)
    return m.prior_at is None or any(request != own and start <= m.since and end > m.prior_at
                                     for request, start, end in visits)


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
    can't be split is left whole, and `first_in_first_out` costs it at the
    oldest units the slot holds then, kept as `unattributed_cost_cents`.
    Splitting twice changes nothing.

    Without door times, the change is the receipt's `resolved_at`, when the
    worker filled in the line. A window that holds it is split by `_old_units`;
    one with two changes in it is left whole. With door times, `_split_by_door`
    splits the window. Units are assumed not to sell while the door is open.
    """
    # Per slot, each product change (the purchase or load that made it), in replay order.
    changes: dict[str, list[Movement]] = {}
    holds: dict[str, str] = {}
    for m in _in_replay_order(movements):
        if m.kind in ("purchase", "load"):
            if holds.get(m.slot, m.sku) != m.sku:
                changes.setdefault(m.slot, []).append(m)
            holds[m.slot] = m.sku
    visits = _visits(movements)
    out = []
    for m in movements:
        if m.kind != "sale" or m.since is None or m.split:
            out.append(m)
            continue
        inside = [c for c in changes.get(m.slot, ()) if _touches(m, c)]
        if not inside:
            out.append(m)
        elif all(c.opened_at is not None for c in inside):
            out.extend(_split_by_door(m, inside, visits))
        elif len(inside) == 1:
            out.extend(_split_one(m, inside[0], visits))
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


def _split_by_door(m: Movement, inside: list[Movement],
                   visits: list[tuple[str, datetime, datetime]]) -> list[Movement]:
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
        return [replace(m, changes=k, split=True, door_conflict=_request(visit))]
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
        return _split_one(m, change, visits) if m.since >= change.opened_at else _shares(m, change, _solve(m))
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


def _in_replay_order(movements: list[Movement]) -> list[Movement]:
    """The movements sorted for the replay: see the note at `_ORDER`.

    Within one moment's receipt movements, rank 0 is every pull (and old
    product's share), rank 1 every load and purchase. A door-timed visit to a
    slot where an earlier-submitted visit loaded at the same moment goes after
    that load, all of it (rank 2), in submission order.
    """
    first_load: dict[tuple[datetime, str | None], int] = {}
    for m in movements:
        if m.kind in ("purchase", "load") and m.seq:
            key = (m.at, m.slot)
            first_load[key] = min(first_load.get(key, m.seq), m.seq)

    def order(m: Movement) -> tuple[datetime, int, int, int, int]:
        if _GROUP[m.kind]:
            rank = 0
        elif m.seq and first_load.get((m.at, m.slot), m.seq) < m.seq:
            rank = 2
        else:
            rank = 1 if m.kind in ("purchase", "load") else 0
        return m.at, _GROUP[m.kind], rank, m.seq, _ORDER[m.kind]

    return sorted(movements, key=order)


def cents(amount: Decimal | str | None) -> Fraction | None:
    """Exact cents from a decimal amount in dollars."""
    return None if amount is None else Fraction(Decimal(str(amount))) * 100


@dataclass
class _Layer:
    """Units of one product that arrived together, at one cost per unit (None: unknown)."""
    units: int
    unit_cost: Fraction | None


@dataclass
class _Pool:
    """Units of one product in one place, in layers by arrival, oldest first.

    Units leave first in, first out: the oldest layer goes first. A layer whose
    cost is unknown taints only the units in it: what leaves with it still
    books the known layers' cost (`_known`). The pool's `cost` and `unit_cost`
    are None while any layer here has no known cost, since an average over
    part of it would be a guess.
    """
    layers: list[_Layer] = field(default_factory=list)

    @property
    def units(self) -> int:
        return sum(layer.units for layer in self.layers)

    @property
    def cost(self) -> Fraction | None:
        return _cost(self.layers)

    def unit_cost(self) -> Fraction | None:
        cost = self.cost
        return None if not self.units or cost is None else cost / self.units

    def add(self, units: int, cost: Fraction | None) -> None:
        """Put `units` in as one layer that cost `cost` in all."""
        if units:  # nothing arrived, so an unknown cost on nothing can't taint what is here
            self.add_layers([_Layer(units, None if cost is None else cost / units)])

    def add_layers(self, layers: list[_Layer]) -> None:
        for layer in layers:
            if not layer.units:
                continue
            if self.layers and self.layers[-1].unit_cost == layer.unit_cost:
                self.layers[-1].units += layer.units
            else:
                self.layers.append(_Layer(layer.units, layer.unit_cost))

    def take_layers(self, units: int) -> list[_Layer]:
        """Move up to `units` out, oldest first, as the layers they came from."""
        out = []
        while units > 0 and self.layers:
            oldest = self.layers[0]
            n = min(units, oldest.units)
            out.append(_Layer(n, oldest.unit_cost))
            oldest.units -= n
            units -= n
            if not oldest.units:
                self.layers.pop(0)
        return out

    def take(self, units: int) -> tuple[int, int, Fraction | None]:
        """Move units out, oldest first. Returns how many there were, how many of them had a known
        cost, and what those cost (`_known`)."""
        layers = self.take_layers(units)
        return (sum(layer.units for layer in layers), *_known(layers))

    def counted(self) -> tuple[tuple[int, Fraction | None], ...]:
        """`(units, unit_cost)` per layer, oldest first: `SlotCount.layers` and `ClosetCount.layers`."""
        return tuple((layer.units, layer.unit_cost) for layer in self.layers)


def _cost(layers: list[_Layer]) -> Fraction | None:
    """What the layers cost in all, or None if any of them has no known cost."""
    known, cost = _known(layers)
    return cost if known == sum(layer.units for layer in layers) else None


def _known(layers: list[_Layer]) -> tuple[int, Fraction | None]:
    """How many of the units have a known cost, and what those cost in all.

    The cost is None only when there are units and none of them has a known
    cost. Otherwise a few unknown units don't hide what the rest cost: a sale
    of an unknown unit and a $2 one books $2 whether one poll or two see it,
    and the count of known units says the rest are missing.
    """
    known = [layer for layer in layers if layer.unit_cost is not None and layer.units]
    if not known and any(layer.units for layer in layers):
        return 0, None
    return sum(layer.units for layer in known), sum((layer.unit_cost * layer.units for layer in known), Fraction(0))


def _less_lost(layers: list[_Layer], lost: int) -> tuple[list[_Layer], list[_Layer]]:
    """Layers drawn from the closet for a load, less `lost` units dropped on the way, and the lost
    layers that go to be written off as shrinkage.

    The lost units come out of the oldest layers, and their known cost is
    spread evenly over the known units that made it, so the load still costs
    what was drawn and the layers keep their order and differences: a dearer
    layer stays dearer for later sales and floors. If none of the units that
    made it has a known cost, every lost layer is written off, a $0 one too.
    Lost units of unknown cost are always written off, so shrinkage shows
    they are missing a cost instead of leaving them out.
    """
    pool = _Pool()
    pool.add_layers(layers)
    dropped = [layer for layer in pool.take_layers(lost) if layer.units]
    known = sum(layer.units for layer in pool.layers if layer.unit_cost is not None)
    if not known:
        return pool.layers, dropped
    _, cost = _known(dropped)
    if cost:
        for layer in pool.layers:
            if layer.unit_cost is not None:
                layer.unit_cost += cost / known
    return pool.layers, [layer for layer in dropped if layer.unit_cost is None]


@dataclass
class _Slot:
    product: str
    since: datetime
    pool: _Pool = field(default_factory=_Pool)
    loaded: int = 0
    sold: int = 0
    lost: int = 0
    oversold: int = 0
    last_moved: datetime | None = None
    replaced: str | None = None   # the product this one replaced, if the slot changed
    written_off: int = 0          # units of `replaced` written off as shrinkage at the change
    unattributed: int = 0         # sold in windows that can't be split; not in `sold`
    at_most: int = 0              # on hand if none of those were this product: loaded less everything else out


def first_in_first_out(movements: list[Movement]) -> Stock:
    """Replay movements in time order and value what is left, first in, first out.

    This is the one place the cost method lives. Each pool keeps its units in
    layers by arrival (`_Pool`), and sales, pulls, losses and loads take the
    oldest first. So a sale in a window that also holds a top-up of the same
    product is costed at the stock that was there, not at an average with the
    top-up (#172).
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

    def write_off(at: datetime, where: str, product: str, layers: list[_Layer]) -> int:
        """Book units gone without a sale as shrinkage: those of known cost with it, the rest
        apart with None, so the known part still counts. Returns how many units there were."""
        units = sum(layer.units for layer in layers)
        known, cost = _known(layers)
        if known:
            shrinkage.append(Shrinkage(at, where, product, known, cost))
        if units > known:
            shrinkage.append(Shrinkage(at, where, product, units - known, None))
        return units

    for m in _in_replay_order(_split_at_changes(movements)):
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
            layers = slot.pool.take_layers(m.units)
            taken = sum(layer.units for layer in layers)
            shelf(m.sku).add_layers(layers)
            slot.at_most = max(0, slot.at_most - m.units)
            if taken < m.units:
                warn(m.slot, f"{m.ref}: {m.units} units pulled from slot {m.slot} but we counted only {taken}")
                # Every recorded layer has moved; any cost on the rest would be counted twice or made up.
                shelf(m.sku).add(m.units - taken, None)
            slot.last_moved = m.at
        elif m.kind in ("purchase", "load"):
            changed = slot is not None and slot.product != m.sku
            if changed != m.reassigned and slot is not None:
                warn(m.slot, f"{m.ref}: slot {m.slot} went from {slot.product} to {m.sku} "
                                f"but slot_reassigned is {str(m.reassigned).lower()}")
            if changed and slot.pool.units:
                warn(m.slot, f"{m.ref}: {slot.pool.units} units of {slot.product} still counted in slot "
                                f"{m.slot} when it changed to {m.sku}; written off as shrinkage")
                write_off(m.at, f"slot {m.slot}", slot.product, slot.pool.layers)
            if slot is None or changed:
                slot = slots[m.slot] = _Slot(product=m.sku, since=m.at, replaced=slot.product if changed else None,
                                             written_off=slot.pool.units if changed else 0)
            if m.kind == "purchase":
                survivors = m.to_slot + m.to_closet
                each = None if m.paid_cents is None or not survivors else m.paid_cents / survivors
                if m.paid_cents is None:
                    warn(m.slot, f"{m.ref}: bought {m.sku} for slot {m.slot} with no total_paid; its cost is unknown")
                if not survivors and m.lost:
                    warn(m.slot, f"{m.ref}: every unit of {m.sku} for slot {m.slot} was lost before loading; "
                                    "the whole line is written off as shrinkage")
                    # With no total_paid the cost is None, so the ledger marks shrinkage incomplete.
                    shrinkage.append(Shrinkage(m.at, f"slot {m.slot}", m.sku, m.lost, m.paid_cents))
                shelf(m.sku).add(m.to_closet, None if each is None else each * m.to_closet)
                slot.pool.add(m.to_slot, None if each is None else each * m.to_slot)
            else:
                wanted = m.to_slot + m.lost
                layers = shelf(m.sku).take_layers(wanted)
                taken = sum(layer.units for layer in layers)
                if taken < wanted:
                    warn(m.slot, f"{m.ref}: loaded {wanted} units of {m.sku} from the closet "
                                    f"but we counted only {taken} there")
                    layers.append(_Layer(wanted - taken, None))   # the ones we didn't count: cost unknown
                kept, stranded = _less_lost(layers, m.lost)
                slot.pool.add_layers(kept)
                if any(layer.unit_cost is not None for layer in stranded):
                    warn(m.slot, f"{m.ref}: no unit of {m.sku} loaded into slot {m.slot} has a known cost to carry "
                                    "the units lost on the way; they are written off as shrinkage")
                if stranded:
                    write_off(m.at, f"slot {m.slot}", m.sku, stranded)
            slot.loaded += m.to_slot
            slot.at_most += m.to_slot
            slot.last_moved = m.at
        elif m.kind == "loss":
            if m.location == "back_stock":
                layers = shelf(m.sku).take_layers(m.units)
                where = "the closet"
            elif slot is not None and slot.product == m.sku:
                layers = slot.pool.take_layers(m.units)
                slot.lost += sum(layer.units for layer in layers)
                slot.at_most = max(0, slot.at_most - m.units)
                slot.last_moved = m.at
                where = f"slot {m.slot}"
            else:
                warn(m.slot, f"{m.ref}: {m.units} units of {m.sku} lost in slot {m.slot}, "
                                "which no longer holds it; ignored")
                continue
            gone = write_off(m.at, where, m.sku, layers)
            if gone < m.units:
                warn(None if where == "the closet" else m.slot,
                     f"{m.ref}: {m.units} units of {m.sku} lost in {where} but we counted only {gone}")
        elif m.kind in ("sale", "sale_before_change") and slot is not None:
            taken, costed, cost = slot.pool.take(m.units)
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
                             "unattributed, costed at the oldest units taken from "
                             f"{slot.product}'s count, which may be the wrong product's")
                slot.unattributed += m.units
                sales.append(SaleCost(m.at, m.slot, None, m.units, costed, None, unattributed_cost_cents=cost,
                                      unattributed=f"sold while {m.door_conflict}'s door times say the door was open",
                                      since=m.window_since))
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
                                f"costed at the oldest units taken from {slot.product}'s count, which may be the wrong "
                                f"product's{written_off}")
                slot.unattributed += m.units
                # Main costed a window it read as all new in cost_cents; it stays there (see `SaleCost`).
                sales.append(SaleCost(m.at, m.slot, None, m.units, costed, cost if m.read_as_new else None,
                                      unattributed_cost_cents=None if m.read_as_new else cost,
                                      unattributed=f"sold {across}; the counters can't split them",
                                      since=m.window_since))
            else:
                slot.sold += m.units
                slot.at_most = max(0, slot.at_most - m.units)
                sales.append(SaleCost(m.booked_at or m.at, m.slot, slot.product, m.units, costed, cost,
                                      taken_at=m.at if m.booked_at else None, since=m.window_since))

    for name, slot in sorted(slots.items()):
        if slot.oversold:
            warn(name, f"slot {name}: the polls show {slot.oversold} more sales than units we loaded "
                            f"since {slot.since.isoformat()}; counted as 0 on hand")
    return Stock(
        closet=tuple(ClosetCount(sku, pool.units, pool.unit_cost(), pool.counted())
                     for sku, pool in sorted(closet.items()) if pool.units),
        slots=tuple(SlotCount(name, s.product, s.pool.units, s.pool.unit_cost(), s.loaded, s.sold, s.lost, s.since,
                              s.last_moved, s.unattributed, s.at_most, s.pool.counted())
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
        line["at"] = line["door_closed_at"] if door_timed(line) else line["resolved_at"]
    return lines


def request_times(lines: list[dict]) -> dict[str, datetime]:
    """Each request's visit on the receipt clock: its earliest line's `at` (`receipt_lines`)."""
    out: dict[str, datetime] = {}
    for line in lines:
        if line["at"] is not None and (line["request_id"] not in out or line["at"] < out[line["request_id"]]):
            out[line["request_id"]] = line["at"]
    return out


def counted_lines(conn: psycopg.Connection, lines: list[dict] | None = None) -> list[dict]:
    """The fulfilled lines the stock counts (`receipt_lines`): once the ledger has an opening entry,
    lines before it on the receipt clock are test purchases and are left out. `lines` is
    `receipt_lines(conn)` when the caller has read it already."""
    since_at = receipts_count_from(conn)
    return [line for line in (receipt_lines(conn) if lines is None else lines)
            if since_at is None or (line["at"] is not None and line["at"] >= since_at)]


def _receipts(conn: psycopg.Connection, lines: list[dict] | None = None) -> tuple[list[dict], list[dict]]:
    """Every fulfilled line and every loss recorded against one: our own purchases, whatever the assignment.

    Once the ledger has an opening entry, lines before it on the receipt clock (`receipt_lines`) are
    test purchases, and they and their later losses are left out: see the module docstring. `lines`
    is `receipt_lines(conn)` when the caller has read it already.
    """
    lines = counted_lines(conn, lines)
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
    return tuple(ClosetCount(c.product, c.units, None, ((c.units, None),)) if c.product in pulled else c
                 for c in first_in_first_out(movements_from_receipts(lines, losses)).closet)


def stock(conn: psycopg.Connection, until: datetime | None = None, lines: list[dict] | None = None) -> Stock:
    """Closet and slot stock now, or as of `until`, from every fulfilled request and every good poll.

    Once the ledger has an opening entry, receipt lines resolved before it (test purchases) and their
    later losses are left out (`_receipts`): see the module docstring. `lines` is
    `receipt_lines(conn)` when the caller has read it already.
    """
    return replay(load_movements(conn, lines), until)


def load_movements(conn: psycopg.Connection, lines: list[dict] | None = None) -> list[Movement]:
    """Every stock movement on record, unsorted: what `stock()` replays.

    Separate so a caller that needs the stock at many moments (the daily money
    series) can read the history once and pass it to `replay(movements, moment)`
    for each, which is exactly `stock(conn, until=moment)` without a query per
    moment. Don't cut a prefix yourself: `replay` must split product changes
    (#97) before it cuts.
    """
    assigned = set(our_slots(conn))
    lines, losses = _receipts(conn, lines)
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


LOADING = ("primary", "alternate", "loaded")   # line outcomes that put units in the slot


def loads(conn: psycopg.Connection, until: datetime | None = None,
          lines: list[dict] | None = None) -> dict[str, list[tuple[datetime, int]]]:
    """Every time each slot had units put in it, from fulfilled receipts, as `(at, units)`, up to `until`.

    Dated on the receipt clock (`receipt_lines`), when the replay puts the units in. Lines before
    the ledger's opening (test purchases) are left out, as in `_receipts`. `lines` is
    `receipt_lines(conn)` when the caller has read it already."""
    out: dict[str, list[tuple[datetime, int]]] = {}
    for line in counted_lines(conn, lines):
        if (line["outcome"] in LOADING and line["units_loaded"] and line["at"] is not None
                and (until is None or line["at"] <= until)):
            out.setdefault(line["slot"], []).append((line["at"], line["units_loaded"]))
    return out


def request_loads(conn: psycopg.Connection, request_ids: list[str],
                  lines: list[dict] | None = None) -> dict[str, list[tuple[str, int]]]:
    """What the receipts of these requests loaded into each slot, as `(sku, units)`: one visit's own loads.

    Lines before the ledger's opening on the receipt clock (test purchases) are left out, as in `_receipts`."""
    out: dict[str, list[tuple[str, int]]] = {}
    for line in counted_lines(conn, lines):
        if line["request_id"] in request_ids and line["outcome"] in LOADING and line["units_loaded"]:
            out.setdefault(line["slot"], []).append((line["sku"], line["units_loaded"]))
    return out



def replay(movements: list[Movement], until: datetime | None = None) -> Stock:
    """`first_in_first_out` of the movements up to `until`. Pure.

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
        return first_in_first_out(movements)
    counted = first_in_first_out([m for m in movements if m.at <= until])
    return replace(counted, sales=tuple(s for s in counted.sales if s.at <= until))


_LINE_COLUMNS = ("request_id", "line_no", "slot", "outcome", "sku", "total_paid", "units_loaded",
                 "units_lost", "units_to_back_stock", "units_from_back_stock", "units_pulled",
                 "pulled_sku", "slot_reassigned", "resolved_at")
_REQUEST_COLUMNS = ("submitted_at", "door_opened_at", "door_closed_at")   # the request's, on every one of its lines
_LOSS_COLUMNS = ("request_id", "line_no", "slot", "location", "sku", "units", "recorded_at")
