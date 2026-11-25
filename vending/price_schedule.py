"""The weekly price check, started by the worker (#202, Kevin 23 Nov: weekly price checks are essential spend,
so make them as cheap as possible). Off unless two settings both turn it on.

    python -m vending.price_schedule        # the settings as this machine reads them, and the latest runs

**What a run does.** `vending.price_search.check_prices`: the drinks in our slots now and in the picked plan
(`--plan ours`), each looked up only if its price is older than `MAX_AGE_HOURS` (a fresh receipt counts: receipts
first). Every `price_compare_every_days` (default 14) days it compares every store (`--compare`); in the weeks
between it re-checks each drink's primary store only (`--recheck`), usually one search on the listing we already
have. Either way it uses the price memory (`vending.price_search`: known dead ends skipped, known listings
re-checked, a cheaper model's find checked by `price_check_model` before it is stored). The model is the
`price_search_model` setting; the team can set it to `claude-haiku-5-5` and keep the Sonnet check.

**When.** `PRICE_CHECK_SCHEDULE` (environment, or `.env`), Eastern, as `day HH:MM-HH:MM`, comma-separated, the
same shape as `AGENT_SCHEDULE`. Unset: `tue 12:00-20:00`, so prices are fresh before Wednesday 14:00's restock run
(`vending.agent_schedule`). `PRICE_CHECK_FROM` is the first Eastern day with runs (default 2026-12-01). Each time
gets one row in `price_check_runs` (migration 034), keyed by the time, so it runs at most once whatever restarts
or replicas there are. A time no worker reached before its latest start is `missed`; a row left `running` by a
worker that stopped is `failed` after `STUCK_AFTER`.

**Off by default, twice,** like the agent's schedule. `PRICE_CHECK_MODE` on the container and `price_check_mode`
in the database: `off` (unset), `dry-run` (lists what it would look up; searches and spends nothing) or `live`
(searches and stores). The lower of the two runs. A container value that isn't a mode is a dry run, shown on
`/health` as a problem.

**Money.** Its own weekly cap, `price_check_weekly_cap_cents` (default 300, at most 1000): the spend of its
lookups (`model_calls` labelled `price_check`) in the 7 days before a run is taken off it, and the run's own cap is
what is left (at most `price_search.HARD_CLI_CAP_CENTS`). With less than `MIN_RUN_CENTS` left the time is
`skipped`. Before the start (3 Dec) the spend is test spend; from the start it is live spend, in the ledger. It
never counts against the agent's own $1.00 a day of lookups.

**The halt switch** stops it: a time due while halted is recorded `halted`, and each lookup checks it too.

**Endings:** `done` (every drink looked up got a usable price or needed none), `partial` (the run finished, but a
drink got no usable price: one store no longer carries it, a check disagreed), `failed` (the run couldn't run or
stopped: no model key, the database), `skipped` (off in the database, or the weekly cap is used), `halted`,
`missed`. Each row keeps the cost, the drinks looked up, the prices changed and the failures. `/health` shows
the last run; the dashboard's Needs attention shows the last run when it ended `partial`, `failed`, `halted` or
`missed`.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from vending import agent_schedule, settings
from vending.agent_schedule import EASTERN, Schedule, database_now, lower, next_time, times_between

MODE_SETTING, TIMES_SETTING, FROM_SETTING = "PRICE_CHECK_MODE", "PRICE_CHECK_SCHEDULE", "PRICE_CHECK_FROM"
DB_MODE_KEY = "price_check_mode"
DEFAULT_TIMES = "tue 12:00-20:00"       # before Wednesday 14:00's restock run
DEFAULT_FROM = "2026-12-01"
BY = "weekly price check"               # who the lookups and the log name
LABEL = "price_check"                   # its `model_calls` label: its own weekly cap reads it
DEFAULT_WEEKLY_CAP_CENTS = 300
HARD_WEEKLY_CAP_CENTS = 1000
DEFAULT_COMPARE_EVERY_DAYS = 14
MAX_AGE_HOURS = 120                     # a price checked within 5 days (Friday's receipt, on Tuesday) is kept
MIN_RUN_CENTS = 10                      # less left this week than this: skip, rather than a run that can't search
LOOKBACK = timedelta(days=7)
STUCK_AFTER = timedelta(hours=2)
RETRY = timedelta(minutes=1)
LOCK = 4_200_202
OUTCOMES = ("running", "done", "partial", "failed", "skipped", "halted", "missed")
PROBLEMS = ("partial", "failed", "halted", "missed")
GARBLED_MODE = ("The weekly price check runs as a dry run only: the container's PRICE_CHECK_MODE is {value!r}, not "
                "off, dry-run or live.")
OFF_IN_DB = f"Not run: {DB_MODE_KEY} in the database is off or unset."
HALTED = "Not run: the halt switch is on."
CAP_USED = "Not run: the weekly cap is used: ${spent} of ${cap} spent in the last 7 days."
MISSED = "No run started between its time and its latest start ({latest}): the worker was down or the check was off."
SUPERSEDED = "Not run: a later time was already due when the worker reached this one."
ABANDONED = "The run hadn't ended {after} after it started, so the worker let it go; its results may be partial."


@dataclass
class Result:
    """What one check did: its exit code (`price_search.main`), drinks looked up, prices changed, drinks that got no
    usable price, and the spend."""
    exit_code: int
    drinks: int = 0
    changed: int = 0
    failures: int = 0
    cost_usd: Decimal = Decimal(0)
    no_credit: bool = False     # stopped: the model API key has no credit, so nothing more could be looked up


def schedule() -> Schedule | None:
    """The schedule from the settings, or None when it is off. `settings.SettingsError` for times or a first day
    that aren't; a mode that isn't one is a dry run, with `problem` saying so."""
    raw = settings.get(MODE_SETTING) or "off"
    mode, problem = raw.strip().lower(), None
    if mode == "off":
        return None
    if mode not in agent_schedule.MODES:
        mode, problem = "dry-run", GARBLED_MODE.format(value=raw.strip())
    entries = []
    for part in settings.get(TIMES_SETTING, DEFAULT_TIMES).split(","):
        if part.strip():
            try:
                entries.append(agent_schedule._entry(part.strip()))
            except ValueError:
                raise settings.SettingsError(
                    f"{TIMES_SETTING} has {part.strip()!r}, not a day and two times like 'tue 12:00-20:00' "
                    "(Eastern, 24-hour; the latest start after the first, the same day)") from None
    if not entries:
        raise settings.SettingsError(f"{TIMES_SETTING} lists no times")
    first = settings.get(FROM_SETTING, DEFAULT_FROM).strip()
    try:
        first_day = date.fromisoformat(first)
    except ValueError:
        raise settings.SettingsError(f"{FROM_SETTING} is {first!r}, not a date like 2026-12-01") from None
    return Schedule(mode, tuple(entries), first_day, problem)


def database_mode(conn) -> str:
    """`price_check_mode` from `config`: off when missing, dry-run when it isn't one of the three words."""
    row = conn.execute("select value from config where key = %s", (DB_MODE_KEY,)).fetchone()
    if row is None:
        return "off"
    return row[0] if agent_schedule.valid_database_mode(row[0]) else "dry-run"


def weekly_spent(conn, now: datetime) -> Decimal:
    """What the scheduled checks' lookups cost in the 7 days before `now` (their `model_calls`)."""
    return conn.execute("select coalesce(sum(cost_usd), 0) from model_calls where label like %s and at > %s",
                        (LABEL + " %", now - LOOKBACK)).fetchone()[0]


def weekly_cap_cents(conn) -> int:
    from vending.price_search import _setting

    return _setting(conn, "price_check_weekly_cap_cents", DEFAULT_WEEKLY_CAP_CENTS, HARD_WEEKLY_CAP_CENTS)


def compare_due(conn, now: datetime) -> bool:
    """Whether this run compares every store: no finished comparing run in the last `price_compare_every_days`."""
    from vending.price_search import _setting

    days = _setting(conn, "price_compare_every_days", DEFAULT_COMPARE_EVERY_DAYS, 365) or DEFAULT_COMPARE_EVERY_DAYS
    return conn.execute("select not exists (select 1 from price_check_runs where compare and mode = 'live'"
                        " and outcome in ('done', 'partial') and due_at > %s)",
                        (now - timedelta(days=days) + timedelta(hours=12),)).fetchone()[0]


def default_run(*, compare: bool, dry_run: bool, cap_cents: int, connect: Callable[[], Any]) -> Result:
    from vending import price_search

    return price_search.check_prices(by=BY, max_age_hours=MAX_AGE_HOURS, connect=connect, compare=compare,
                                     dry_run=dry_run, max_usd=f"{cap_cents / 100:.2f}", label=LABEL)


def _eastern(moment: datetime) -> str:
    return f"{moment.astimezone(EASTERN):%a %d %b %H:%M} ET"


@dataclass(frozen=True)
class Claim:
    due: datetime
    mode: str
    compare: bool
    cap_cents: int


class PriceChecks:
    """Decides when the weekly check is due and starts it in a thread. The worker's loop asks `next_due` and calls
    `tick`."""

    def __init__(self, sched: Schedule, *, connect: Callable[[], Any] | None = None,
                 run: Callable[..., Result] = default_run, log: Callable[[str], None] = print,
                 background: bool = True, clock: Callable[[], datetime] | None = None,
                 db_clock: Callable[[Any], datetime] | None = None):
        self.sched, self.run, self.log, self.background = sched, run, log, background
        self._connect = connect
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.db_clock = db_clock or database_now
        self.thread: threading.Thread | None = None
        self.not_before: datetime | None = None
        self.last: dict | None = None
        self.problem: str | None = None

    def connect(self):
        if self._connect is None:
            from vending import db
            from vending.price_search import retrying

            self._connect = retrying(db.connect)
        return self._connect()

    def public(self) -> dict:
        """What `/health` shows: the most this worker runs, the next time and the last run (never its detail)."""
        return {"mode": self.sched.mode, "next_at": next_time(self.sched, self.clock()).isoformat(),
                "last": self.last, "problem": self.sched.problem or self.problem}

    def next_due(self, now: datetime) -> datetime:
        if self.not_before is not None and now < self.not_before:
            return self.not_before
        if self.thread is not None and self.thread.is_alive():
            return now + RETRY * 10
        due = [d for d, latest in times_between(self.sched, now - LOOKBACK, now) if d not in self._known()]
        return now if due else next_time(self.sched, now)

    def _known(self) -> set[datetime]:
        return getattr(self, "_seen", set())

    def tick(self, now: datetime) -> None:
        """Record what is due and start a run if one may start. Never raises."""
        if self.thread is not None and self.thread.is_alive():
            return
        try:
            claim = self._claim()
            self.problem = None
        except Exception as error:      # noqa: BLE001 - the worker goes on; the next try is a minute away
            self.not_before = now + RETRY
            self.problem = f"The weekly price check can't start: {type(error).__name__}. The worker tries again."
            self.log(f"price check schedule: {type(error).__name__}: {error}; trying again in a minute")
            return
        if claim is None:
            return
        if self.background:
            self.thread = threading.Thread(target=self._run, args=(claim,), name="price-check", daemon=True)
            self.thread.start()
        else:
            self._run(claim)

    def _claim(self) -> Claim | None:
        from vending import config, db

        with self.connect() as conn, conn.transaction():
            behind = db.pending(conn)
            if behind:
                raise RuntimeError(f"the database is missing migrations: {', '.join(behind)}")
            conn.execute("select pg_advisory_xact_lock(%s)", (LOCK,))
            now = self.db_clock(conn)
            conn.execute("update price_check_runs set outcome = 'failed', finished_at = %s, detail = %s"
                         " where outcome = 'running' and claimed_at < %s",
                         (now, ABANDONED.format(after="2 hours"), now - STUCK_AFTER))
            known = {d for (d,) in conn.execute("select due_at from price_check_runs where due_at > %s",
                                                (now - LOOKBACK - timedelta(days=1),))}
            due = [(d, latest) for d, latest in times_between(self.sched, now - LOOKBACK, now) if d not in known]
            claim = None
            for n, (d, latest) in enumerate(due):
                if n < len(due) - 1:
                    self._record(conn, d, latest, "off", "missed", now, SUPERSEDED)
                elif now > latest:
                    self._record(conn, d, latest, "off", "missed", now,
                                 MISSED.format(latest=_eastern(latest)))
                else:
                    mode = lower(self.sched.mode, database_mode(conn))
                    if mode == "off":
                        self._record(conn, d, latest, "off", "skipped", now, OFF_IN_DB)
                    elif config.is_halted(conn):
                        self._record(conn, d, latest, mode, "halted", now, HALTED)
                    else:
                        cap = weekly_cap_cents(conn)
                        spent = weekly_spent(conn, now)
                        left = cap - int(spent * 100)
                        if mode == "live" and left < MIN_RUN_CENTS:
                            self._record(conn, d, latest, mode, "skipped", now,
                                         CAP_USED.format(spent=f"{spent:.2f}", cap=f"{cap / 100:.2f}"))
                        else:
                            from vending.price_search import HARD_CLI_CAP_CENTS

                            compare = compare_due(conn, now)
                            conn.execute("insert into price_check_runs (due_at, latest_at, mode, compare, claimed_at)"
                                         " values (%s, %s, %s, %s, %s)", (d, latest, mode, compare, now))
                            claim = Claim(d, mode, compare, max(1, min(left, HARD_CLI_CAP_CENTS)))
            self._seen = known | {d for d, _ in due}
            self.last = self._last(conn)
        return claim

    def _record(self, conn, due, latest, mode, outcome, now, detail) -> None:
        conn.execute("insert into price_check_runs (due_at, latest_at, mode, outcome, claimed_at, finished_at, detail)"
                     " values (%s, %s, %s, %s, %s, %s, %s) on conflict (due_at) do nothing",
                     (due, latest, mode, outcome, now, now, detail))

    def _run(self, claim: Claim) -> None:
        try:
            result = self.run(compare=claim.compare, dry_run=claim.mode == "dry-run", cap_cents=claim.cap_cents,
                              connect=self.connect)
            outcome = ("failed" if result.exit_code == 2 else "partial" if result.failures or result.exit_code
                       else "done")
            detail = (f"{'Compared every store' if claim.compare else 'Re-checked primary stores'}"
                      f"{' (dry run: nothing searched)' if claim.mode == 'dry-run' else ''}: {result.drinks} drinks "
                      f"looked up, {result.changed} prices changed, {result.failures} without a usable price, "
                      f"${result.cost_usd:.4f}.")
            if getattr(result, "no_credit", False):
                detail = "Stopped: the model API key has no credit; add credit. " + detail
        except Exception as error:      # noqa: BLE001 - recorded, and the worker goes on
            result, outcome = Result(2), "failed"
            detail = f"The check stopped: {type(error).__name__}: {error}"[:500]
            self.log(f"price check: {detail}")
        for attempt in range(3):
            try:
                with self.connect() as conn:
                    conn.execute("update price_check_runs set outcome = %s, finished_at = %s, drinks = %s,"
                                 " changed = %s, failures = %s, cost_usd = %s, detail = %s"
                                 " where due_at = %s and outcome = 'running'",
                                 (outcome, self.db_clock(conn), result.drinks, result.changed, result.failures,
                                  result.cost_usd, detail, claim.due))
                    self.last = self._last(conn)
                return
            except Exception as error:      # noqa: BLE001
                self.log(f"price check: couldn't record its ending ({type(error).__name__}); try {attempt + 1}")
        self.log("price check: its ending wasn't recorded; the next worker marks it failed")

    def _last(self, conn) -> dict | None:
        row = conn.execute("select due_at, mode, outcome, compare, drinks, changed, failures, cost_usd"
                           " from price_check_runs order by due_at desc limit 1").fetchone()
        if row is None:
            return None
        due, mode, outcome, compare, drinks, changed, failures, cost = row
        return {"due_at": due.isoformat(), "mode": mode, "outcome": outcome, "compare": compare, "drinks": drinks,
                "changed": changed, "failures": failures, "cost_usd": None if cost is None else f"{cost:.4f}"}


class Invalid:
    """Stands in when a setting is wrong, so `/health` shows the check isn't running."""

    def public(self) -> dict:
        return {"mode": "invalid", "next_at": None, "last": None}


def default_checks(log: Callable[[str], None] = print) -> PriceChecks | Invalid | None:
    """What the worker runs: None when off, `Invalid` when a setting is wrong (logged; never stops the worker)."""
    try:
        sched = schedule()
        if sched is None:
            return None
        first = next_time(sched, datetime.now(timezone.utc))
    except Exception as error:      # noqa: BLE001
        log(f"the weekly price check is off: {error}")
        return Invalid()
    log(f"weekly price check: {MODE_SETTING} is {sched.mode} ({DB_MODE_KEY} in the database must say so too), "
        f"next due {_eastern(first)}")
    return PriceChecks(sched, log=log)


def main(argv: list[str] | None = None) -> int:
    """`python -m vending.price_schedule`: the settings as this machine reads them, and the latest runs."""
    import argparse

    from vending import db

    parser = argparse.ArgumentParser(prog="python -m vending.price_schedule",
                                     description="Show the weekly price check: the settings and the latest runs.")
    parser.add_argument("--rows", type=int, default=10)
    args = parser.parse_args(argv)
    try:
        sched = schedule()
    except settings.SettingsError as error:
        print(f"The weekly price check is off here: {error}")
        sched = None
    else:
        print(f"The weekly price check is off here ({MODE_SETTING} is unset or off)." if sched is None else
              f"{MODE_SETTING} here: {sched.mode}; next due {_eastern(next_time(sched, datetime.now(timezone.utc)))}.")
    with db.connect() as conn:
        behind = db.pending(conn)
        if behind:
            print(f"The database is missing migrations ({', '.join(behind)}); run python -m vending.db migrate.")
            return 2
        in_db = database_mode(conn)
        print(f"{DB_MODE_KEY} in the database: {in_db}. A worker runs {lower(sched.mode, in_db) if sched else 'off'}."
              f" Spent in the last 7 days: ${weekly_spent(conn, database_now(conn)):.4f} of "
              f"${weekly_cap_cents(conn) / 100:.2f}.")
        rows = conn.execute("select due_at, mode, outcome, compare, drinks, changed, failures, cost_usd, detail"
                            " from price_check_runs order by due_at desc limit %s", (args.rows,)).fetchall()
    if not rows:
        print("No weekly price check has been recorded yet.")
    for due, mode, outcome, compare, drinks, changed, failures, cost, detail in rows:
        print(f"{_eastern(due)}  {mode}  {outcome}" + (f"  {'compare' if compare else 're-check'}: {drinks} drinks, "
                                                       f"{changed} changed, {failures} failed, ${cost:.4f}"
                                                       if drinks is not None else "")
              + (f"\n    {detail}" if detail else ""))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
