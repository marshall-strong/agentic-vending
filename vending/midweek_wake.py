"""Wake the agent midweek when a slot is about to sell out before Friday (#219).

    python -m vending.midweek_wake            # one pass: decide, record, ask for the run
    python -m vending.midweek_wake --check    # the same pass, writing nothing; says what it would do

**Off by default.** The worker runs this job each round only when `MIDWEEK_WAKE=on`
(environment or `.env`; the value `on`, any case). Unset, the job does nothing and
opens no database connection. The CLI checks the same setting, so one switch gates
the whole module.

**The decision reuses the planner** (docs/restock-policy.md, section 2). After each
sync round, on a Monday or Tuesday Eastern:

1. **Which slots are at risk** comes from the projection the agent plans with
   (`vending.demand`, shown by `get_machine_state`): each of our slots (the server's
   assignment, `vending.slots.our_slots`) at its in-stock rate against the units on
   hand now, unrounded. Demand before the visit lands is the rest of today
   (`demand.share_left`: a 7:30 AM check still has 99% of Monday to sell) plus the
   class days through the landing, two class days after the request (Monday's request
   plans on Wednesday afternoon). A slot is at risk when what is left at the landing
   falls short of the class days from the landing to Friday's restock. Units lost
   before the landing are lost either way; only the shortfall after it can be saved.
2. **What a visit would load and cost** is the planner's answer, not a sum done here:
   a candidate off-cycle desired state restocks each at-risk slot with the product and
   shelf price it has now, filled to its column, every other slot kept, and runs
   through `planning.plan_gathered` on what `planning.gather_live` reads: the
   preview's own path, with a bridge that refuses every call, and no submit. That
   gives whole packs, the configured fee, the closet drawn only for the same product,
   its oldest layers first, and every gate. Any refusal (halt, an open request, the
   balance, an infeasible slot) records nothing and leaves the week open.
3. **Saved margin** = for each restocked at-risk slot, the units the visit actually
   saves (its shortfall after the landing, capped at the units the plan loads into it)
   times shelf price / (1 + tax) less the planner's unit cost for what it loads, at
   the most we pay (`FloorPart.unit_cost`: each purchase option's max per unit, or the
   closet lots it draws; the dearest of them). A unit the visit loads that doesn't sell
   by Friday keeps its cost (A5), so it is no loss and counts nothing.
4. **Wake** only when the saved margin beats the visit's own cost as the planner
   defines it (`Cost.visit_fee_cents`, config `midweek_fee_cents`). The purchases are
   stock that keeps its cost (A5), not a cost of the visit; whether the balance can pay
   for them is the planner's `insufficient_balance` gate in step 2.

Nothing runs before the competition's start Eastern (`start.on_or_after_start`): fall
data never wakes anybody. A stale poll, no rate, no price or an unknown cost for what
the visit loads skips rather than guesses.

**One wake-up a week.** The row goes in `midweek_wakeups` (migration 027), keyed by
the Saturday that starts the Saturday-to-Friday week (Eastern), with a unique index:
restarts and racing replicas cannot double the cap. The insert and the run request
are one transaction. `eligible` is the one path to a wake: the worker's pass (`check`),
`--check` and the simulator all go through it. The dashboard's Needs attention and the
agent's briefing (`vending.briefing`) show the row, so the next run sees why.

**Starting the run.** `request_midweek_run` is the hook. Today it records the request
on this week's row and logs it; it starts nothing. The scheduled agent (#224) should
call it, or read `current` for a row with no `requested_at`, once both PRs merge,
and start `vending.agent.run` with the row's reason in its briefing.

`window`, `at_risk` and `decide` are pure: `decide` takes the gathered facts and a
clock and touches no database. `evaluate` gathers; `record` and `request_midweek_run`
write. Nothing here calls a model or the bridge, stocks the machine, or reads anything
but our own side.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from fractions import Fraction
from typing import Callable, Iterable

from vending import class_days, db, demand, ledger, planner, planning, settings, start
from vending.bridge import BridgeError
from vending.start import EASTERN
from vending.tools import desired_state as schema
from vending.tools import machine_state

SETTING = "MIDWEEK_WAKE"
ON = "on"
# The planned visit lands two class days after the request (docs/restock-policy.md, section 2):
# Monday's request plans on Wednesday afternoon, Tuesday's on Thursday, and later in the week the
# landing is on or past Friday's restock, so nothing pays past Tuesday.
LANDING_AFTER_CLASS_DAYS = 2
# The Eastern weekdays that may still start a paying visit: Monday and Tuesday. On a weekend the
# Monday check decides with fresher data.
DECIDE_WEEKDAYS = (0, 1)
BY = "midweek_wake"


def enabled() -> bool:
    """True only when `MIDWEEK_WAKE` is `on` (any case). Unset, or anything else, is off."""
    return (settings.get(SETTING) or "").strip().lower() == ON


def _quiet(*_args) -> None:
    return None


@dataclass(frozen=True)
class Outlook:
    """One slot's projection, as the agent sees it (`get_machine_state`). None means unknown.

    `units_per_class_day` is the in-stock rate (`vending.demand`); `on_hand` the units on
    the rails now: a person's count when it is newer, else the receipts' replay.
    """
    slot: str
    units_per_class_day: Decimal | None
    on_hand: int | None


@dataclass(frozen=True)
class Window:
    """When a visit requested now would land, in class days of demand."""
    landing_day: date
    before_landing: Fraction    # the rest of today, plus the class days through the landing day
    after_landing: Fraction     # the class days after the landing, through Friday's restock


@dataclass(frozen=True)
class AtRisk:
    """A slot projected to run short after the landing, with no visit."""
    slot: str
    rate: Fraction
    on_hand: int
    short: Fraction             # units it would miss between the landing and Friday's restock


@dataclass(frozen=True)
class Wake:
    """A wake-up worth recording: the week it belongs to, the slots, and why."""
    week_start: date                   # the Saturday that starts the Saturday-to-Friday week
    reason: str
    slots: tuple[str, ...]
    saved_margin_cents: int            # exact, rounded half up to the whole cent for the record
    visit_cost_cents: int = 0          # the planner's most the visit could cost, fee included


def window(now: datetime) -> Window | None:
    """The demand before and after a visit requested at `now` would land. Pure.

    None off a Monday or Tuesday Eastern, or when the landing falls on or past Friday's
    restock. Today counts only when it is a class day, and only for the share of its
    sales still to come (`demand.share_left`). `now` must carry a time zone: a naive
    clock is a guess about which day it is.
    """
    if now.tzinfo is None:
        raise ValueError("midweek_wake now must have a time zone")
    local = now.astimezone(EASTERN)
    if local.weekday() not in DECIDE_WEEKDAYS:
        return None
    friday = ledger.week_of(local) + timedelta(days=6)      # this week's Friday restock
    walk, day = [], local.date() + timedelta(days=1)
    while day <= friday:
        if class_days.is_class_day(day):
            walk.append(day)
        day += timedelta(days=1)
    if len(walk) <= LANDING_AFTER_CLASS_DAYS:
        return None    # the landing is on or past the restock: no class day left to save
    today = demand.share_left(local.hour, local.minute) if class_days.is_class_day(local.date()) else Fraction(0)
    return Window(walk[LANDING_AFTER_CLASS_DAYS - 1], today + LANDING_AFTER_CLASS_DAYS,
                  Fraction(len(walk) - LANDING_AFTER_CLASS_DAYS))


def at_risk(win: Window, outlooks: Iterable[Outlook]) -> list[AtRisk]:
    """The slots that would run short after the landing, by slot. Pure; unknowns are skipped."""
    risks = []
    for o in sorted(outlooks, key=lambda o: o.slot):
        if o.units_per_class_day is None or o.on_hand is None or o.units_per_class_day <= 0:
            continue
        rate = Fraction(o.units_per_class_day)
        on_hand = max(o.on_hand, 0)
        left = max(Fraction(0), on_hand - rate * win.before_landing)
        short = rate * win.after_landing - left
        if short > 0:
            risks.append(AtRisk(o.slot, rate, on_hand, short))
    return risks


def candidate(risks: Iterable[AtRisk], gathered: planning.Gathered,
              log: Callable[..., None] = _quiet) -> tuple[schema.DesiredState, tuple[str, ...]]:
    """The off-cycle desired state a visit would carry: each at-risk slot restocked with the
    product and shelf price it has now, filled to its column (the planner's default), every
    other slot kept. Returns it with the slots it restocks. A slot whose product or price the
    planner doesn't know is kept and logged: restocking it would be a guess."""
    states = {s.slot: s for s in gathered.current.slots}
    wanted = {r.slot for r in risks}
    rows, restocked = [], []
    for slot in gathered.config.slots:
        s = states.get(slot)
        if slot in wanted and s is not None and s.contents_known and s.product and s.price_cents:
            rows.append(schema.DesiredSlot(slot=slot, product=s.product, sale_price=_price(s.price_cents)))
            restocked.append(slot)
            continue
        if slot in wanted:
            log(f"midweek_wake: slot {slot} is at risk, but the planner doesn't know its product or price, "
                "so a visit can't be planned for it; it is left out")
        rows.append(schema.DesiredSlot(slot=slot, keep=True))
    return schema.DesiredState(slots=rows, off_cycle=True), tuple(restocked)


def decide(now: datetime, outlooks: Iterable[Outlook], gathered: planning.Gathered | None,
           log: Callable[..., None] = _quiet) -> Wake | None:
    """Whether a paid midweek visit now would pay, by the planner's own plan for it. Pure.

    `gathered` is what `planning.gather_live` read (or a test's stand-in); it may be None
    only when nothing is at risk. None, logged, when nothing is at risk, when the planner
    refuses the visit, or when the saved margin doesn't beat the planner's visit fee.
    """
    win = window(now)
    if win is None:
        log("midweek_wake: a visit requested now could not land with a class day left before "
            "Friday's restock (only a Monday or Tuesday request can)")
        return None
    risks = at_risk(win, outlooks)
    if not risks:
        log("midweek_wake: no slot is projected to sell out before Friday's restock")
        return None
    if gathered is None:
        raise ValueError("midweek_wake.decide needs the gathered facts when a slot is at risk")
    desired, restocked = candidate(risks, gathered, log)
    if not restocked:
        return None
    plan = planning.plan_gathered(gathered, desired)
    if not plan.ok:
        why = "; ".join(f"{r.code}: {r.message}" for r in plan.refusals)
        log(f"midweek_wake: the planner refuses the visit ({why}); nothing is recorded, so a later "
            "round this week decides again")
        return None
    lines = {line.slot: line for line in plan.lines}
    floors = {f.slot: f for f in plan.floors}
    unknown = {c.product for c in gathered.current.closet if c.units > 0 and c.unit_cost is None}
    taxed = 1 + Fraction(gathered.config.tax_percent, 100)
    saved, counted = Fraction(0), []
    for r in risks:
        line = lines.get(r.slot)
        if r.slot not in restocked or line is None:
            continue
        basis = _unit_cost(line, floors.get(r.slot), unknown)
        if basis is None:
            log(f"midweek_wake: slot {r.slot}: the cost of what the visit loads is unknown, so the "
                "sales it saves are counted at nothing")
            continue
        avoided = min(Fraction(line.units_to_load), r.short)
        saved += avoided * (Fraction(line.sale_price_cents) / taxed - basis)
        counted.append(r)
    fee = plan.cost.visit_fee_cents
    if not counted or saved <= fee:
        log(f"midweek_wake: a visit would save ${ledger.money(_whole_cents(max(saved, Fraction(0))))} "
            f"in margin before Friday, not more than the ${ledger.money(fee)} fee; no wake-up")
        return None
    saved_cents = _whole_cents(saved)
    names = [r.slot for r in counted]
    listed = names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"
    parts = "; ".join(f"{r.slot}: empty now" if r.on_hand == 0 else
                      f"{r.slot}: {r.on_hand} on hand at {_number(r.rate)} a class day" for r in counted)
    reason = (f"{'Slot' if len(names) == 1 else 'Slots'} {listed} "
              f"{'is' if len(names) == 1 else 'are'} projected to sell out before Friday's restock "
              f"({parts}). A midweek visit requested now plans on {win.landing_day:%A} afternoon; the "
              f"planner prices it at up to ${ledger.money(plan.cost.max_cents)} with the "
              f"${ledger.money(fee)} fee, and the sales it would save before Friday are worth "
              f"${ledger.money(saved_cents)} in margin at the most we pay, more than the fee "
              "(docs/restock-policy.md, section 2).")
    return Wake(ledger.week_of(now.astimezone(EASTERN)), reason, tuple(names), saved_cents,
                plan.cost.max_cents)


def _unit_cost(line: planner.Line, floor: planner.SlotFloor | None, unknown: set[str]) -> Fraction | None:
    """The planner's cost per unit for what `line` loads, at the most we pay, or None when unknown.

    The floor's parts other than units staying put (`planner.ON_HAND`) are exactly what the
    line loads: each purchase option's max per unit, or each closet lot it draws. The dearest
    is the basis. A closet lot of unknown cost leaves no part, so a back-stock line for a
    product with one is unknown rather than priced off the lots that are known."""
    if line.source == planner.BACK_STOCK and line.product in unknown:
        return None
    costs = [p.unit_cost for p in (floor.parts if floor else ()) if p.source != planner.ON_HAND]
    return max(costs) if costs else None


def _price(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def _whole_cents(value: Fraction) -> int:
    """A Fraction of cents, rounded half up to the whole cent."""
    return int((Decimal(value.numerator) / Decimal(value.denominator))
               .quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _number(value: Fraction) -> str:
    """A Fraction as plain text to two places without trailing zeros: 2.50 -> '2.5', 10 -> '10'."""
    d = (Decimal(value.numerator) / Decimal(value.denominator)).quantize(Decimal("0.01"))
    return f"{d:f}".rstrip("0").rstrip(".") or "0"


class _NoBridge:
    """Stands in for the bridge: the wake check reads only our stored catalog, never the bridge."""

    def products(self):
        raise BridgeError("the midweek wake check never calls the bridge; run the catalog sync "
                          "(python -m vending.sync_catalog) so our copy of the catalog is filled")


def evaluate(conn, now: datetime | None = None, log=print) -> Wake | None:
    """Decide from the same stored facts the agent reads. Read-only.

    The projection comes from `get_machine_state` (polls, receipts, a person's count and
    the rate); the plan from `planning.gather_live` and `planning.plan_gathered`, the
    preview's path, with the bridge refused. Skips when the latest poll is stale (an old
    poll overstates what is on the rails) or the halt switch is on (the planner would
    refuse; the week stays open for when it is off). Refuses before the competition
    starts. Raises `planning.NotReady` when a person has to act first. Every outcome is
    logged: a None says "nothing to record", and the log says which skip or no it was.
    """
    now = now or ledger.db_now(conn)
    if not start.on_or_after_start(now):
        log(f"midweek_wake: the competition starts {start.START.day} {start.START:%b} Eastern; "
            "no midweek check before it")
        return None
    cfg = planning.planner_config(conn)     # our slots; NotReady when unready
    if cfg.halted:
        log(f"midweek_wake: skipped, {planning.HALTED_HINT}; nothing is recorded, so the week can "
            "still wake once it is off")
        return None
    state = machine_state.get_machine_state(conn, now=now)
    if not state.success:
        log(f"midweek_wake: skipped, {state.message}")
        return None
    if state.stale:
        hours = machine_state.STALE_AFTER.total_seconds() / 3600
        log(f"midweek_wake: skipped, the latest poll is stale (over {hours:.0f} hours old), so what "
            "is on the rails is not known well enough to project a sell-out")
        return None
    outlooks = [Outlook(s.slot,
                        None if s.units_per_class_day is None else Decimal(s.units_per_class_day),
                        s.counted_on_hand if s.counted_on_hand is not None else s.stock_on_hand)
                for s in state.data]
    win = window(now)
    if win is None or not at_risk(win, outlooks):
        return decide(now, outlooks, None, log)    # logs why, before anything else is gathered
    return decide(now, outlooks, planning.gather_live(conn, _NoBridge(), now), log)


def eligible(conn, now: datetime | None = None, log=print) -> Wake | None:
    """The one path to a wake: the start, this week's row, then `evaluate` (halt, the
    projection, and the planner's gates: an open request, the balance, feasibility).

    The worker's pass (`check`) records what this returns; `--check` prints it. None,
    logged, when nothing would be recorded.
    """
    now = now or ledger.db_now(conn)
    if not start.on_or_after_start(now):
        log(f"midweek_wake: the competition starts {start.START.day} {start.START:%b} Eastern; "
            "no midweek check before it")
        return None
    if current(conn, now) is not None:
        log(f"midweek_wake: week {ledger.week_of(now.astimezone(EASTERN))} already has its "
            "wake-up; nothing to do")
        return None
    try:
        return evaluate(conn, now, log=log)
    except planning.NotReady as skip:
        log(f"midweek_wake: skipped, {skip}")
        return None


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


def check(conn, now: datetime | None = None, log=print) -> bool:
    """One pass: decide (`eligible`), record this week's wake-up if there is one, ask for the run.

    True when this pass recorded a wake-up. Everything that records nothing (before the
    start, a week already woken, the halt switch, a stale poll, nothing at risk, a planner
    refusal, a margin under the fee) leaves the week open for a later round. The insert
    and the run request are one transaction: a crash between them must not leave a week
    recorded with its request missing (#219 review).
    """
    now = now or ledger.db_now(conn)
    wake = eligible(conn, now, log=log)
    if wake is None:
        return False                         # eligible logged why
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
                        help="run the worker's pass without writing: print what it would record")
    args = parser.parse_args(argv)
    if not enabled():
        print(f"midweek_wake: {SETTING} is off; set it to on to run this")
        return 0
    if args.check:
        with db.connect() as conn:
            if _refuse_behind(conn):
                return 2
            wake = eligible(conn)     # the worker's own path: the week, halt, the planner's gates
        print("would record: " + wake.reason if wake else
              "would not record a wake-up this round (the line above says why)")
        return 0
    return job()


if __name__ == "__main__":
    sys.exit(main())
