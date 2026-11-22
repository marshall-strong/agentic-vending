"""Wake the agent midweek when a slot is about to sell out before Friday (#219).

    python -m vending.midweek_wake            # one pass: decide, record, ask for the run
    python -m vending.midweek_wake --check    # decide only; prints the decision, writes nothing

**Off by default.** The worker runs this job each round only when `MIDWEEK_WAKE=on`
(environment or `.env`; the value `on`, any case). Unset, the job does nothing and
opens no database connection. The CLI checks the same setting, so one switch gates
the whole module.

**The decision** is the team's midweek rule, as the planner already reads it
(docs/restock-policy.md, section 2; #110 item 3). After each sync round, for each of
our slots (the server's assignment, `vending.slots.our_slots`):

- the projection is the one the agent already plans with (`vending.demand`, shown by
  `get_machine_state` and the briefing): class days of stock left at the slot's own
  in-stock rate, a day the slot sat empty left out. Days left is units on hand over
  the rate, unrounded: the display string rounds, and a rounded 3.0 must not decide
  what an exact 3.03 leaves working (#219 review). Never guessed: a slot with no
  usable projection, price, column capacity or load price is skipped;
- a slot counts when it is projected to be empty before this week's Friday restock,
  and the sales a paid midweek visit would save beat the visit fee
  (`config.midweek_fee_cents`, $25): saved margin = sum over those slots of
  (units one visit can load × margin per unit), where the loadable units are the
  saved class days' demand capped at one column of the slot's product (the planner's
  capacity inputs, `vending.capacity`), and margin per unit is shelf price ÷ (1 + tax)
  − what loading that unit costs: the closet's oldest layers first
  (`stock.visit_load_prices`), then new stock at the primary sourcing option's price;
- the check runs only from the competition's start Eastern (`start.on_or_after_start`):
  fall data never wakes anybody; and it records nothing when the planner would refuse
  the visit it asks for — an open purchase request (`request_pending`) or a spendable
  balance under the fee plus the units to load (`insufficient_balance`) leaves the
  week open for a later round;
- the insert and the run request are one transaction, so a crash between them cannot
  leave a week recorded with its request missing;
- class days saved counts the class days after the visit's planned landing, which
  is two class days after the request (a Monday request plans on Wednesday: 2 days
  saved; Tuesday: 1; later: none), so the check decides only on a Monday or Tuesday,
  Eastern — later in the week no visit can still pay, and on the weekend the Monday
  check decides with fresher data. A class day counts only when the slot would sit
  empty from its start, the same convention the projection uses (`vending.demand`).

**One wake-up a week.** The row goes in `midweek_wakeups` (migration 027), keyed by
the Saturday that starts the Saturday-to-Friday week (Eastern), with a unique index:
restarts and racing replicas cannot double the cap. The dashboard's Needs attention
and the agent's briefing (`vending.briefing`) show the row, so the next run sees why.

**Starting the run.** `request_midweek_run` is the hook. Today it records the request
on this week's row and logs it; it starts nothing. The scheduled agent (#224) should
call it — or read `current` for a row with no `requested_at` — once both PRs merge,
and start `vending.agent.run` with the row's reason in its briefing.

The decision itself is pure: `decide` takes gathered facts and a clock, and touches
no database. `evaluate` gathers the same stored facts the agent reads; `record` and
`request_midweek_run` write. Nothing here calls a model, stocks the machine, or
reads anything but our own side.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from fractions import Fraction
from typing import Iterable

from vending import capacity, class_days, db, ledger, planning, settings, sourcing, start, stock
from vending.ledger import DEFAULT_MIDWEEK_FEE_CENTS
from vending.pricing import DEFAULT_TAX_PERCENT
from vending.start import EASTERN
from vending.tools import machine_state

SETTING = "MIDWEEK_WAKE"
ON = "on"
# The planned visit lands two class days after the request (docs/restock-policy.md, section 2):
# Monday's request plans on Wednesday (2 class days saved before Friday), Tuesday's on Thursday
# (1), and later in the week none, so nothing pays past Tuesday.
LANDING_AFTER_CLASS_DAYS = 2
# The Eastern weekdays that may still start a paying visit: Monday and Tuesday.
DECIDE_WEEKDAYS = (0, 1)
BY = "midweek_wake"


def enabled() -> bool:
    """True only when `MIDWEEK_WAKE` is `on` (any case). Unset, or anything else, is off."""
    return (settings.get(SETTING) or "").strip().lower() == ON


@dataclass(frozen=True)
class Outlook:
    """One slot's projection and money, gathered (`evaluate`). Pure data; nothing guessed.

    `units_per_class_day` is the rate the agent plans with (`vending.demand`, as
    `get_machine_state` shows it) and `class_days_left` is the same rate against the
    unrounded units on hand -- never the display string, whose rounding can flip the
    decision (#219 review). `capacity` is the column one visit can load into the slot
    (the planner's capacity inputs, `vending.capacity`) and `new_unit_cents` the
    sourcing price per unit of new stock for the slot's product. The closet's shared
    layers reach `decide` once for the whole machine, so they are priced into one
    slot's load, never into all of them. Any None means unknown, and `decide` skips
    the slot rather than guess.
    """
    slot: str
    units_per_class_day: Decimal | None
    class_days_left: Fraction | Decimal | None   # exact: units on hand / rate, unrounded
    price_cents: int | None
    capacity: int | None = None
    new_unit_cents: Fraction | None = None


@dataclass(frozen=True)
class Wake:
    """A wake-up worth recording: the week it belongs to, the slots, and why."""
    week_start: date                   # the Saturday that starts the Saturday-to-Friday week
    reason: str
    slots: tuple[str, ...]
    saved_margin_cents: int            # exact, rounded half up to the whole cent for the record
    visit_cost_cents: Fraction = Fraction(0)   # the fee with tax, plus the units it loads


def decide(now: datetime, outlooks: Iterable[Outlook], *,
           closet_layers: tuple[tuple[Fraction, Fraction], ...] = (),
           fee_cents: int = DEFAULT_MIDWEEK_FEE_CENTS,
           tax_percent: int = DEFAULT_TAX_PERCENT) -> Wake | None:
    """Whether a paid midweek visit now would pay for slots about to sell out. Pure.

    None when nothing is projected to empty before Friday's restock, when no visit
    could still pay (past Tuesday, Eastern; the team's rule, #110 item 3), or when
    the saved margin doesn't beat the fee. `now` must carry a time zone: a naive
    clock is a guess about which day it is. `closet_layers` is the machine closet's
    shared stock, oldest layer first: the visit draws those units once, whatever
    slots they end up in, so the same $3 can is never priced into two loads.
    """
    if now.tzinfo is None:
        raise ValueError("midweek_wake now must have a time zone")
    now_local = now.astimezone(EASTERN)
    if now_local.weekday() not in DECIDE_WEEKDAYS:
        return None
    week_start = ledger.week_of(now_local)
    friday = week_start + timedelta(days=6)      # this week's Friday restock
    walk: list[date] = []
    day = now_local.date() + timedelta(days=1)
    while day <= friday:
        if class_days.is_class_day(day):
            walk.append(day)
        day += timedelta(days=1)
    if len(walk) <= LANDING_AFTER_CLASS_DAYS:
        return None    # the planned landing is on or past the restock: no class day left to save
    landing_day = walk[LANDING_AFTER_CLASS_DAYS - 1]
    chosen: list[tuple[Outlook, int, Fraction]] = []
    total = Fraction(0)
    visit_cost = Fraction(fee_cents) * (1 + Fraction(tax_percent, 100))   # the fee, taxed like a sale
    closet = list(closet_layers)                 # (units, cents per unit), oldest first, shared
    for o in sorted(outlooks, key=lambda o: o.slot):
        if (o.units_per_class_day is None or o.class_days_left is None or o.price_cents is None
                or o.capacity is None):
            continue
        # The exact projection, unrounded: the decision must not turn on a display's
        # rounding (#219 review). Days left is units on hand divided by the rate.
        rate = Fraction(o.units_per_class_day)
        days_left = Fraction(o.class_days_left)
        # Class days after the planned landing that the slot would sit empty from the start
        # of, counting class days after today (a class day counts when the slot held stock
        # as it began, `vending.demand`).
        saved = sum(1 for index in range(LANDING_AFTER_CLASS_DAYS + 1, len(walk) + 1)
                    if days_left <= index - 1)
        if saved == 0:
            continue
        # One visit loads at most one column into the slot (`vending.capacity`, the planner's
        # capacity inputs): the credit counts what it can load, never more.
        loadable = min(saved * rate, Fraction(o.capacity))
        closet_units = sum((units for units, _ in closet), Fraction(0))
        if loadable > closet_units and o.new_unit_cents is None:
            continue                             # needs new stock and new stock has no price
        # What the load costs: the closet's oldest layers first (`first_in_first_out`'s
        # order, `stock.closet`), then new stock at the sourcing price per unit.
        left, load_cost = loadable, Fraction(0)
        while left > 0 and closet:
            units, unit_cents = closet[0]
            grab = min(left, units)
            load_cost += grab * unit_cents
            left -= grab
            if grab == units:
                closet.pop(0)
            else:
                closet[0] = (units - grab, unit_cents)
        if left > 0:
            load_cost += left * o.new_unit_cents
        # margin per unit is shelf price / (1 + tax); the load costs what it prices above
        a_unit = Fraction(o.price_cents) / (1 + Fraction(tax_percent, 100))
        total += loadable * a_unit - load_cost
        visit_cost += load_cost
        chosen.append((o, saved, loadable))
    if not chosen or total <= fee_cents:
        return None
    saved_cents = _whole_cents(total)
    parts = "; ".join(
        f"{o.slot}: empty now" if Fraction(o.class_days_left) == 0 else
        f"{o.slot}: {_number(Fraction(o.class_days_left))} class days of stock at "
        f"{_number(o.units_per_class_day)} a class day" for o, _, _ in chosen)
    names = ", ".join(o.slot for o, _, _ in chosen[:-1])
    names = f"{names} and {chosen[-1][0].slot}" if names else chosen[0][0].slot
    reason = (f"Slots {names} are projected to sell out before Friday's restock ({parts}). "
              f"A midweek visit requested now plans on {landing_day:%A} afternoon and would save $"
              f"{ledger.money(saved_cents)} before Friday, more than the ${ledger.money(fee_cents)} "
              "fee (docs/restock-policy.md, section 2).")
    return Wake(week_start, reason, tuple(o.slot for o, _, _ in chosen),
                saved_cents, visit_cost)


def _whole_cents(value: Fraction) -> int:
    """A Fraction of cents, rounded half up to the whole cent."""
    return int((Decimal(value.numerator) / Decimal(value.denominator))
               .quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _number(value: Decimal | Fraction) -> str:
    """A Decimal or Fraction as plain text without trailing zeros: 2.50 -> '2.5', 10.0 -> '10'."""
    d = value if isinstance(value, Decimal) else (
        Decimal(value.numerator) / Decimal(value.denominator)).quantize(Decimal("0.01"))
    return f"{d:f}".rstrip("0").rstrip(".") or "0"


def evaluate(conn, now: datetime | None = None, log=print) -> Wake | None:
    """Decide from the same stored facts the agent reads. Read-only.

    Uses `get_machine_state` for the projection (polls, receipts, a person's count
    and the rate), the receipts for what a visit's load costs (`vending.stock`, the
    closet's oldest layers first, then the sourcing price for new stock) and the
    planner's capacity inputs for the column it can load (`vending.capacity`). Days
    left is units on hand over the rate, unrounded: the display string rounds, and
    its rounding must not decide (#219 review). Skips when the latest poll is
    stale: an old poll overstates what is on the rails, and the sync's own jobs own
    that failure. Refuses before the competition starts: fall data must not wake
    anybody. Raises `planning.NotReady` when a person has to act first (our slots
    not recorded, a bad config setting). Every outcome is logged: a None here means
    "nothing to record", and the log says which skip or no it was.
    """
    now = now or ledger.db_now(conn)
    if not start.on_or_after_start(now):
        log(f"midweek_wake: the competition starts {start.START.day} {start.START:%b} Eastern; "
            "no midweek check before it")
        return None
    cfg = planning.planner_config(conn)     # our slots, the fee and the tax; NotReady when unready
    state = machine_state.get_machine_state(conn, now=now)
    if not state.success:
        log(f"midweek_wake: skipped, {state.message}")
        return None
    if state.stale:
        hours = machine_state.STALE_AFTER.total_seconds() / 3600
        log(f"midweek_wake: skipped, the latest poll is stale (over {hours:.0f} hours old), so what "
            "is on the rails is not known well enough to project a sell-out")
        return None
    columns, new_prices, closet_layers = _visit_inputs(conn, cfg.capacity_overrides)
    if closet_layers is None:
        log("midweek_wake: skipped, the closet holds units whose paid cost is not known, so a "
            "visit's load cannot be priced")
        return None
    outlooks = [Outlook(slot=s.slot,
                        units_per_class_day=None if s.units_per_class_day is None else Decimal(s.units_per_class_day),
                        class_days_left=_days_left(s),
                        price_cents=None if s.price is None else int(ledger.dollars_to_cents(s.price)),
                        capacity=columns.get(s.slot),
                        new_unit_cents=new_prices.get(s.slot))
                for s in state.data]
    wake = decide(now, outlooks, closet_layers=closet_layers,
                  fee_cents=cfg.midweek_fee_cents, tax_percent=cfg.tax_percent)
    if wake is None:
        log("midweek_wake: no slot is projected to sell out before Friday's restock "
            "with a visit that would pay")
    return wake


def _days_left(s) -> Fraction | None:
    """The slot's exact days left: units on hand over the rate, unrounded, None without either."""
    if s.units_per_class_day is None or s.units_per_class_day == 0:
        return None                             # no usable rate, or it never empties
    on_hand = s.counted_on_hand if s.counted_on_hand is not None else s.stock_on_hand
    if on_hand is None:
        return None
    return Fraction(on_hand) / Fraction(Decimal(s.units_per_class_day))


def _visit_inputs(conn, overrides):
    """The visit's inputs, gathered once: each slot's column and sourcing price per unit,
    and the closet's priced layers oldest first.

    The column comes from the planner's capacity inputs (`capacity.for_product`: a
    person's count override, else the loading chart for the catalog size). The price
    comes from the primary sourcing option (its pack price over its units per pack,
    max when no price was seen). The closet's layers are shared across the slots the
    visit may load, so they reach `decide` once for the machine. When any closet unit's
    paid cost is unknown the load cannot be priced, and the closet comes back None.
    """
    counts = stock.stock(conn)
    skus = sorted({c.product for c in counts.slots})
    sizes = dict(conn.execute("select sku, size from catalog_products where sku = any(%s)",
                              (skus,)).fetchall()) if skus else {}
    prices: dict[str, Fraction] = {}
    for o in sourcing.options_for(conn, skus):
        if o.sku not in prices and o.rank == sourcing.RANKS[0]:   # options_for: primary first
            pack = o.expected_cents if o.expected_cents is not None else o.max_cents
            prices[o.sku] = Fraction(pack, o.units_per_pack)      # the price of one unit
    columns, new_prices = {}, {}
    for s in counts.slots:
        columns[s.slot] = capacity.for_product(s.product, sizes.get(s.product), overrides)
        new_prices[s.slot] = prices.get(s.product)
    layers: list[tuple[Fraction, Fraction]] = []
    for c in stock.closet(conn):
        if c.units and c.unit_cost is None:      # a pulled product's cost is unknown: a guess
            return columns, new_prices, None
        if c.units:
            layers.append((Fraction(c.units), c.unit_cost))
    return columns, new_prices, tuple(layers)


def current(conn, now: datetime | None = None) -> dict | None:
    """This week's wake-up row (Eastern week, Saturday start), as a dict, or None.

    The dashboard and the briefing read this to show why the week was woken; the
    scheduled agent (#224) reads it for a row whose `requested_at` is still null.
    """
    now = now or ledger.db_now(conn)
    week = ledger.week_of(now.astimezone(EASTERN))
    row = conn.execute(
        "select id, week_start, decided_at, reason, slots, saved_margin_cents, requested_at, requested_by"
        " from midweek_wakeups where week_start = %s", (week,)).fetchone()
    if row is None:
        return None
    (id, week_start, decided_at, reason, slots, saved, requested_at, requested_by) = row
    return {"id": id, "week_start": week_start, "decided_at": decided_at, "reason": reason,
            "slots": list(slots), "saved_margin_cents": saved,
            "requested_at": requested_at, "requested_by": requested_by}


def record(conn, wake: Wake) -> int | None:
    """Insert the week's wake-up row. Returns its id, or None when the week already has one.

    The unique index (`midweek_wakeups_one_per_week`) holds the cap in the database,
    so a restart or two workers in the same minute cannot record a second wake-up.
    """
    row = conn.execute(
        "insert into midweek_wakeups (week_start, reason, slots, saved_margin_cents)"
        " values (%s, %s, %s, %s) on conflict (week_start) do nothing returning id",
        (wake.week_start, wake.reason, list(wake.slots), wake.saved_margin_cents)).fetchone()
    return row[0] if row else None


def request_midweek_run(conn, reason: str, *, by: str | None = None,
                        now: datetime | None = None, log=print) -> bool:
    """Ask for a midweek agent run, with `reason` as what its briefing should carry.

    The hook the scheduled agent (#224) will wire up. Today it only records the
    request on this week's wake-up row and logs it: nothing starts a run. True when
    the request was recorded now; False when this week has no wake-up row, or its
    request is already recorded, so there is nothing to start a run for.
    """
    now = now or ledger.db_now(conn)
    week = ledger.week_of(now.astimezone(EASTERN))
    row = conn.execute(
        "update midweek_wakeups set requested_at = %s, requested_by = coalesce(%s, requested_by)"
        " where week_start = %s and requested_at is null returning id", (now, by, week)).fetchone()
    if row is None:
        log(f"midweek_wake: no open wake-up for week {week}; nothing requested")
        return False
    log(f"midweek_wake: midweek run requested for week {week} (row {row[0]}): {reason}")
    return True


def blocked(conn, wake: Wake, *, now: datetime | None = None, log=print) -> bool:
    """Whether the visit behind a wake couldn't happen now, by the planner's own two gates.

    The planner submits no new purchase request while one is still open (`request_pending`,
    `vending.planning.pending_requests`) and refuses a buy the spendable balance can't cover
    (`insufficient_balance`, the same `ledger.balance` figure `planning.gather` gives it). A
    visit that would be refused can't follow this wake, so the caller records nothing and a
    later round in the week decides again on fresh facts.
    """
    now = now or ledger.db_now(conn)
    pending = planning.pending_requests(conn)
    if pending:
        log(f"midweek_wake: purchase request {pending[0].request_id} is still open, so the planner "
            "would not submit a midweek visit; this wake records nothing")
        return True
    balance = ledger.balance(conn, now)
    if balance is None:
        log("midweek_wake: the ledger has no opening entry, so there is no spendable balance "
            "to cover the visit; this wake records nothing")
        return True
    if balance.spendable_cents < wake.visit_cost_cents:
        log(f"midweek_wake: the visit would cost ${ledger.money(wake.visit_cost_cents)} "
            f"with the fee and its units, more than the spendable balance "
            f"(${ledger.money(balance.spendable_cents)}); the planner would refuse it, "
            "so this wake records nothing")
        return True
    return False


def check(conn, now: datetime | None = None, log=print) -> bool:
    """One pass: decide, record this week's wake-up if there is one, ask for the run.

    True when this pass recorded a wake-up. Before the competition starts it does
    nothing at all: fall data must not wake anybody. A week that already has its
    wake-up is left alone, a week that decides nothing is left untouched, and a wake
    whose visit the planner would refuse (an open request, a balance under the cost)
    records nothing, so a later round can decide again. The insert and the run
    request are one transaction: a crash between them must not leave a week recorded
    with its request missing (#219 review).
    """
    now = now or ledger.db_now(conn)
    if not start.on_or_after_start(now):
        log(f"midweek_wake: the competition starts {start.START.day} {start.START:%b} Eastern; "
            "no midweek check before it")
        return False
    if current(conn, now) is not None:
        log(f"midweek_wake: week {ledger.week_of(now.astimezone(EASTERN))} already has its "
            "wake-up; nothing to do")
        return False
    try:
        wake = evaluate(conn, now, log=log)
    except planning.NotReady as skip:
        log(f"midweek_wake: skipped, {skip}")
        return False
    if wake is None:
        return False                         # evaluate logged why: stale, no polls, or nothing projected
    if blocked(conn, wake, now=now, log=log):
        return False
    with conn.transaction():
        if record(conn, wake) is None:       # a replica recorded it first; the index held the cap
            log("midweek_wake: another run recorded this week's wake-up; nothing to do")
            return False
        request_midweek_run(conn, wake.reason, by=BY, now=now, log=log)
    return True


def _refuse_behind(conn) -> bool:
    """True (after the standard message) when migrations are pending: this job never migrates.

    In production `DATABASE_URL` is the shared Azure database, and a schema change there
    should be a person's decision (`python -m vending.db migrate`), like the syncs' own refusals.
    """
    behind = db.pending(conn)
    if not behind:
        return False
    print(f"The database is missing migrations: {', '.join(behind)}.\n"
          "Check DATABASE_URL points where you mean, then run: python -m vending.db migrate",
          file=sys.stderr)
    return True


def job() -> int:
    """The worker's job (`vending.worker.default_jobs`). Off, it does nothing and opens no
    connection; on, it refuses a database that is behind migrations, like the syncs."""
    if not enabled():
        return 0
    with db.connect() as conn:
        if _refuse_behind(conn):
            return 2
        check(conn)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vending.midweek_wake",
                                     description="Decide whether a slot is about to sell out "
                                                 "before Friday's restock and a midweek visit "
                                                 "would pay (#219). Off unless MIDWEEK_WAKE=on.")
    parser.add_argument("--check", action="store_true",
                        help="decide only: print the decision, write nothing")
    args = parser.parse_args(argv)
    if not enabled():
        print(f"midweek_wake: {SETTING} is off; set it to on to run this")
        return 0
    if args.check:
        with db.connect() as conn:
            if _refuse_behind(conn):
                return 2
            wake = evaluate(conn)
        print("would record: " + wake.reason if wake else
              "no slot is projected to sell out before Friday's restock with a visit that would pay")
        return 0
    return job()


if __name__ == "__main__":
    sys.exit(main())
