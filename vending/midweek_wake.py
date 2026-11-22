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
  in-stock rate, a day the slot sat empty left out, and the paid cost of the slot's
  product from the receipts. Never guessed: a slot with no usable projection, price
  or unit cost is skipped;
- a slot counts when it is projected to be empty before this week's Friday restock,
  and the sales a paid midweek visit would save beat the visit fee
  (`config.midweek_fee_cents`, $25): saved margin = sum over those slots of
  (rate per class day x class days saved x margin per unit), where margin per unit
  is shelf price ÷ (1 + tax) − unit cost;
- class days saved counts the class days after the visit's planned landing, which
  is two class days after the request (a Monday request plans on Wednesday: 2 days
  saved; Tuesday: 1; later: none), so the check decides only on a Monday or Tuesday,
  Eastern — later in the week no visit can still pay, and on the weekend the Monday
  check decides with fresher data. A class day counts only when the slot would sit
  empty from its start, the same convention the projection uses (`vending.demand`).

**One wake-up a week.** The row goes in `midweek_wakeups` (migration 025), keyed by
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

from vending import class_days, db, ledger, planning, settings, stock
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

    `units_per_class_day` and `class_days_left` are the projection the agent plans
    with (`vending.demand`, as `get_machine_state` shows it); `unit_cost_cents` is what
    refilling the slot's product pays (`stock.slot_product_costs`). Any None means
    unknown, and `decide` skips the slot rather than guess.
    """
    slot: str
    units_per_class_day: Decimal | None
    class_days_left: Decimal | None
    price_cents: int | None
    unit_cost_cents: Fraction | None   # cents per unit, from the receipts (`vending.stock`)


@dataclass(frozen=True)
class Wake:
    """A wake-up worth recording: the week it belongs to, the slots, and why."""
    week_start: date                   # the Saturday that starts the Saturday-to-Friday week
    reason: str
    slots: tuple[str, ...]
    saved_margin_cents: int            # exact, rounded half up to the whole cent for the record


def decide(now: datetime, outlooks: Iterable[Outlook], *,
           fee_cents: int = DEFAULT_MIDWEEK_FEE_CENTS,
           tax_percent: int = DEFAULT_TAX_PERCENT) -> Wake | None:
    """Whether a paid midweek visit now would pay for slots about to sell out. Pure.

    None when nothing is projected to empty before Friday's restock, when no visit
    could still pay (past Tuesday, Eastern; the team's rule, #110 item 3), or when
    the saved margin doesn't beat the fee. `now` must carry a time zone: a naive
    clock is a guess about which day it is.
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
    chosen: list[tuple[Outlook, int]] = []
    total = Fraction(0)
    for o in outlooks:
        if None in (o.units_per_class_day, o.class_days_left, o.price_cents, o.unit_cost_cents):
            continue
        # Class days after the planned landing that the slot would sit empty from the start
        # of, counting class days after today (a class day counts when the slot held stock
        # as it began, `vending.demand`).
        saved = sum(1 for index in range(LANDING_AFTER_CLASS_DAYS + 1, len(walk) + 1)
                    if o.class_days_left <= index - 1)
        if saved == 0:
            continue
        margin = Fraction(o.price_cents) / (1 + Fraction(tax_percent, 100)) - o.unit_cost_cents
        total += Fraction(o.units_per_class_day) * saved * margin
        chosen.append((o, saved))
    if not chosen or total <= fee_cents:
        return None
    saved_cents = int((Decimal(total.numerator) / Decimal(total.denominator))
                      .quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    parts = "; ".join(
        f"{o.slot}: empty now" if o.class_days_left == 0 else
        f"{o.slot}: {_number(o.class_days_left)} class days of stock at "
        f"{_number(o.units_per_class_day)} a class day" for o, _ in chosen)
    names = ", ".join(o.slot for o, _ in chosen[:-1])
    names = f"{names} and {chosen[-1][0].slot}" if names else chosen[0][0].slot
    reason = (f"Slots {names} are projected to sell out before Friday's restock ({parts}). "
              f"A midweek visit requested now plans on {landing_day:%A} afternoon and would save $"
              f"{ledger.money(saved_cents)} before Friday, more than the ${ledger.money(fee_cents)} "
              "fee (docs/restock-policy.md, section 2).")
    return Wake(week_start, reason, tuple(o.slot for o, _ in sorted(chosen, key=lambda p: p[0].slot)),
                saved_cents)


def _number(d: Decimal) -> str:
    """A Decimal as plain text without trailing zeros: 2.50 -> '2.5', 10.0 -> '10'."""
    return f"{d:f}".rstrip("0").rstrip(".") or "0"


def evaluate(conn, now: datetime | None = None, log=print) -> Wake | None:
    """Decide from the same stored facts the agent reads. Read-only.

    Uses `get_machine_state` for the projection (polls, receipts, a person's count
    and the rate) and the receipts for the product's paid cost (`vending.stock`). Skips
    when the latest poll is stale: an old poll overstates what is on the rails, and the
    sync's own jobs own that failure. Raises `planning.NotReady` when a person has to
    act first (our slots not recorded, a bad config setting). Every outcome is logged:
    a None here means "nothing to record", and the log says which skip or no it was.
    """
    now = now or ledger.db_now(conn)
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
    costs = stock.slot_product_costs(conn)
    outlooks = [Outlook(slot=s.slot,
                        units_per_class_day=_decimal(s.units_per_class_day),
                        class_days_left=_decimal(s.class_days_of_stock_left),
                        price_cents=None if s.price is None else int(ledger.dollars_to_cents(s.price)),
                        unit_cost_cents=costs.get(s.slot))
                for s in state.data]
    wake = decide(now, outlooks, fee_cents=cfg.midweek_fee_cents, tax_percent=cfg.tax_percent)
    if wake is None:
        log("midweek_wake: no slot is projected to sell out before Friday's restock "
            "with a visit that would pay")
    return wake


def _decimal(text: str | None) -> Decimal | None:
    return None if text is None else Decimal(text)


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
    """One pass: decide, record this week's wake-up if there is one, ask for the run.

    True when this pass recorded a wake-up. A week that already has its wake-up is
    left alone, and a week that decides nothing is left untouched: only `record`
    writes, and only once a week.
    """
    now = now or ledger.db_now(conn)
    if current(conn, now) is not None:
        log(f"midweek_wake: week {ledger.week_of(now.astimezone(EASTERN))} already has its "
            "wake-up; nothing to do")
        return False
    try:
        wake = evaluate(conn, now, log=log)
    except planning.NotReady as blocked:
        log(f"midweek_wake: skipped, {blocked}")
        return False
    if wake is None:
        return False                         # evaluate logged why: stale, no polls, or nothing projected
    if record(conn, wake) is None:           # a replica recorded it first; the index held the cap
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
