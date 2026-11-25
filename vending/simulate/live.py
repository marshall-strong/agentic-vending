"""The live schedule in a simulated season: what the deployed worker and the team will actually run.

`python -m vending.simulate --schedule live --from-export FILE` runs a season the way the competition will run:

- **Sat 28 Nov 18:00 ET (`FILL_AT`)**: the team's steps, through the real commands: `ledger reset-test-opening`
  (a dry run: a scratch database has no test opening), `ledger open --amount 600.00`, the exported plan stored and
  picked (`plan_store.pick`), then `vending.fill` previewed and sent with `--submit --hash`, against the simulated
  bridge. The fill's own guards (the $400 fill cap, checked prices, the deadline) run as they will on Saturday.
- **The agent's runs** come from `vending.agent_schedule.Scheduler`, the worker's own class, with the settings the
  team sets on Saturday (`ENV`: `AGENT_SCHEDULE_MODE=live`, the default times and first day) and `agent_schedule_mode`
  live in the database. Its claims, windows, checks before a send, run lease, midweek wake-ups and records are the
  real ones, on the season's clock (`dbclock`). Each run is `vending.agent.run`, under `model_run_cap_cents`.
- **The worker's round** after every hourly poll: the slots check and the syncs (as before), then the midweek wake
  check (`MIDWEEK_WAKE=on`, `midweek_wake.check`), then the daily tracking check (`TRACKING_DAILY=on`,
  `tracking_daily.run`) against the simulated bridge's books, then `Scheduler.poke`, as `vending.worker` does.
- **The weekly price check** (`vending.price_schedule.PriceChecks`) runs on its times as a dry run: it lists what it
  would look up and spends nothing. The world re-prices the sourcing table on Tuesdays at 12:30 instead, before
  Wednesday's run (`season.handle`, "sourcing_check").

What it can't run is listed in `NOT_SIMULATED`, and each season's report repeats it.
"""

from __future__ import annotations

import contextlib
import io
import os
from collections.abc import Iterator
from datetime import datetime, time, timedelta

import psycopg

from vending import agent_schedule, config, db, midweek_wake, price_schedule, tracking_daily
from vending.simulate import world
from vending.simulate.world import EASTERN

BY = "simulation"
# The team sends the day-one fill on Saturday evening (Kevin, 25 Nov: before Amazon's Sat 23:59 cutoff for the Mon 30 Nov
# stocking, found by this rehearsal).
FILL_AT = datetime(2026, 11, 28, 18, 0, tzinfo=EASTERN)
# The worker's settings on the container from Saturday 28 Nov (docs/agent.md, "Live scheduled runs"; docs/deploy.md).
ENV = {"AGENT_SCHEDULE_MODE": "live", "AGENT_SCHEDULE": agent_schedule.DEFAULT_TIMES,
       "AGENT_SCHEDULE_FROM": agent_schedule.DEFAULT_FROM, "MIDWEEK_WAKE": "on", "TRACKING_DAILY": "on",
       "PRICE_CHECK_MODE": "dry-run"}
# And in the database. The per-run cap is the season's `--run-cap`; the daily cap is live's $4 (Kevin, 24 Nov).
DB_SETTINGS = {"agent_schedule_mode": "live", "price_check_mode": "dry-run"}
DAILY_CAP_CENTS = 400
# The mid-season dashboard check, after the round at this time.
MID_SEASON = datetime(2027, 1, 6, 12, 0, tzinfo=EASTERN)
# The world re-prices after the weekly price check's time (Tue 12:00), so the dry-run check sees the week-old
# prices a live check would look up, and lists them.
TUESDAY, REPRICE_AT = 1, time(12, 30)
NOT_SIMULATED = (
    "Customers, the machine, the bridge and the student worker are made up (vending/simulate/world.py, bridge.py).",
    "The agent's find_pack_price and the weekly price check's lookups make no web search: the tool isn't offered, "
    "the check runs as a dry run, and the world re-prices the sourcing table each Tuesday at 12:30.",
    "Each agent run happens at one instant of the season's clock: a run that takes minutes live takes none here, so "
    "the run lease never expires mid-run and no deadline (60 min) is ever reached.",
    "Model spend is test spend (purpose test) under a $4 daily cap, not the ledger's live spend.",
    "The tracking check's money figures are compared with our own ledger's (the simulated bridge has no tax "
    "calculation of its own); only its counts are checked against the simulated machine.",
    "Deliveries: one visit per request (Mon 30 Nov 15:00 for the fill, Friday for a weekly one); an Amazon line "
    "past its cutoff is paid, goes to back stock and is loaded by a later request's visit. Amazon always takes "
    "exactly two days; no store-side delays.",
    "The worker runs in-process: no container, no /health, no restarts, no replicas, no network failures.",
    "Visit-day live polls (vending.visit_polls) aren't simulated: the season polls hourly.",
    "Catalog requests aren't decided: the round doesn't run sync_product_requests, so a request the agent files "
    "stays pending.",
)


def events() -> list:
    """The season's own events when the scheduler runs the agent: the daily catalog sync and the Tuesday re-price.
    No agent run and no ledger opening: the scheduler starts the runs, and the day one steps open the ledger."""
    from vending.simulate.season import Event

    out = []
    day = world.SIM_START.date()
    while datetime.combine(day, time(0), EASTERN) < world.COMPETITION_END:
        at = datetime.combine(day, time(6), EASTERN)
        if at > world.SIM_START:
            out.append(Event(at, "catalog_sync"))
        if day.weekday() == TUESDAY:
            out.append(Event(datetime.combine(day, REPRICE_AT, EASTERN), "sourcing_check"))
        day += timedelta(days=1)
    return out


@contextlib.contextmanager
def environment() -> Iterator[None]:
    """The container's settings (`ENV`) for as long as the season runs, and the old values back after."""
    saved = {k: os.environ.get(k) for k in ENV}
    os.environ.update(ENV)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def configure(conn: psycopg.Connection) -> None:
    """The database's settings the team sets on Saturday, and live's daily cap for the season's (test) spend."""
    for key, value in DB_SETTINGS.items():
        config.set_value(conn, key, value, BY)
    config.set_value(conn, "model_test_daily_cap_cents", DAILY_CAP_CENTS, BY)


def _quiet(call, *args, **kwargs) -> tuple[object, str]:
    """Run a command's `main`, keeping what it prints: (its return, its output)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        result = call(*args, **kwargs)
    return result, out.getvalue()


def day_one(conn: psycopg.Connection, st, data: dict, say=print) -> dict:
    """Saturday's go-live steps on the scratch database, at `FILL_AT`, through the real commands. Returns what happened;
    raises SystemExit when the fill is refused, since a season without its day one isn't a rehearsal."""
    from vending import fill, ledger
    from vending.simulate import export, season

    steps: dict = {}
    rc, text = _quiet(ledger.main, ["reset-test-opening", "--by", BY])
    steps["reset_test_opening"] = {"exit": rc, "said": text.strip()[:300]}
    rc, text = _quiet(ledger.main, ["open", "--amount", "600.00", "--by", BY])
    steps["ledger_open"] = {"exit": rc, "said": text.strip()[:300]}
    if rc != 0:
        raise SystemExit(f"ledger open failed: {text.strip()}")
    steps["plan_id"] = export.load_plan(conn, data, BY)
    preview = fill.preview(conn, st.bridge, None)
    steps["preview"] = {"ok": preview.ok, "refusals": [f"{r.code}: {r.message}" for r in preview.refusals],
                        "warnings": list(preview.warnings), "deviations": list(preview.deviations),
                        "blocked": preview.blocked}
    if preview.plan is not None:
        steps["preview"]["cost"] = {"expected_cents": int(preview.plan.cost.expected_cents),
                                    "max_cents": int(preview.plan.cost.max_cents)} \
            if hasattr(preview.plan, "cost") else None
    if not preview.ok:
        st.live["day_one"] = steps
        raise SystemExit("the fill's preview refused it: " + "; ".join(steps["preview"]["refusals"])
                         + (f" ({preview.blocked})" if preview.blocked else ""))
    argv = ["--submit", "--hash", preview.approval, "--by", BY]
    before = set(st.bridge.requests)
    rc, text = _quiet(fill.main, argv, bridge=lambda: st.bridge, connect=db.connect)
    steps["submit"] = {"argv": argv[:1] + ["--hash", "(the preview's)"] + argv[3:], "exit": rc, "said": text.strip()[:600]}
    refused = [line[2:].split(":", 1)[0] for line in text.splitlines() if line.startswith("- ")]
    if rc != 0 and refused and set(refused) == {fill.DEVIATION} and preview.deviations:
        # What a person does on Saturday when the only refusal is a pack or cost that differs from the pick: the
        # preview listed it, and the documented flag sends it anyway (the log records the flag and the list).
        argv.append("--accept-deviations")
        steps["refused_first"] = {"codes": refused, "said": text.strip()[:600]}
        rc, text = _quiet(fill.main, argv, bridge=lambda: st.bridge, connect=db.connect)
        steps["submit"] = {"argv": argv[:1] + ["--hash", "(the preview's)"] + argv[3:], "exit": rc,
                           "said": text.strip()[:600]}
    st.live["day_one"] = steps
    if rc != 0:
        raise SystemExit(f"the fill's submit was refused: {text.strip()}")
    season.schedule_visits(st, before, st.clock.now())
    st.runs.append({"kind": "day-one fill", "at": st.clock.now().isoformat(), "status": "submitted",
                    "detail": f"python -m vending.fill --submit: plan #{steps['plan_id']}",
                    "request_id": next(iter(set(st.bridge.requests) - before), None), "cost_usd": "0",
                    "mode": "live", "decision": "submit", "reason": "the team's picked plan"})
    say(f"{st.clock.now().astimezone(EASTERN):%a %d %b %H:%M} day-one fill: plan #{steps['plan_id']} sent")
    return steps


class _Skipped(Exception):
    """A round's job whose prerequisite didn't finish (`vending.worker.NEEDS`)."""


class Driver:
    """The worker's scheduled work in a season: the agent's scheduler, the weekly price check and the round's jobs.
    `run_season` asks `next_due` between the hour's events and calls `tick` then, and `round` after each poll."""

    def __init__(self, conn: psycopg.Connection, st, opts: dict, say=print, dashboard_url: str | None = None):
        from vending import agent

        self.conn, self.st, self.opts, self.say = conn, st, opts, say
        self.dashboard_url = dashboard_url
        st.live.setdefault("log", [])
        self.agents = agent_schedule.Scheduler(agent_schedule.schedule(), run_agent=self._run_agent,
                                               connect=db.connect, log=self._logger("agent schedule"),
                                               background=False, clock=st.clock.now, bridge=lambda: st.bridge)
        self.prices = price_schedule.PriceChecks(price_schedule.schedule(), connect=db.connect,
                                                 log=self._logger("price check"), background=False,
                                                 clock=st.clock.now)
        self._agent = agent

    def _logger(self, source: str):
        def log(line: str) -> None:
            self.st.live["log"].append({"at": self.st.clock.now().isoformat(), "source": source, "line": str(line)})
        return log

    # --- the scheduler's entry point ----------------------------------------------------------------------
    def _run_agent(self, live: bool, authorize=None, occasion: str | None = None):
        """`vending.agent_schedule.default_run_agent`, for the season: the same call, with the season's bridge,
        clock and model or scripted client (`season.run_agent`)."""
        from vending.simulate import season

        kind = "midweek" if occasion is not None else "scheduled"
        o = self.opts
        rec = season.run_agent(self.conn, self.st, kind, o["budget"], o["run_cap"], o["min_run"], live=live,
                               authorize=authorize, occasion=occasion)
        if rec.get("status") == "not_run":
            return None, self._agent.Outcome("skipped", rec.get("detail") or "not run")
        if rec.get("run_id") is not None:
            row = self.conn.execute("select due_at, kind from agent_schedule where run_id = %s",
                                    (rec["run_id"],)).fetchone()
            if row is not None:
                rec["due"], rec["schedule_kind"] = row[0].isoformat(), row[1]
        return rec.get("run_id"), self._agent.Outcome(rec.get("status") or "failed", rec.get("detail") or "")

    # --- the worker's loop ----------------------------------------------------------------------------------
    def next_due(self, now: datetime) -> datetime:
        return min(self.agents.next_due(now), self.prices.next_due(now))

    def tick(self, now: datetime) -> None:
        if self.agents.next_due(now) <= now:
            self.agents.tick(now)
        if self.prices.next_due(now) <= now:
            self.prices.tick(now)

    def round(self, now: datetime, finished: dict[str, bool] | None = None) -> None:
        """The round's jobs after the syncs, in the worker's order: the midweek wake check, the tracking check,
        then the scheduler's poke. `finished`: which of the round's earlier jobs finished with 0 (`slots_check`,
        `sync_history`, `sync_requests`); a job whose prerequisite didn't is skipped, as `vending.worker.NEEDS` does."""
        from vending.worker import NEEDS

        st, conn = self.st, self.conn
        finished = finished or {}
        rounds = st.live.setdefault("rounds", {"count": 0, "failed": []})
        rounds["count"] += 1
        for job, ok in finished.items():
            if not ok:
                rounds["failed"].append({"at": now.isoformat(), "job": job, "exit": 1})
        ready = {job: all(finished.get(need, True) for need in NEEDS[job]) for job in NEEDS}
        self._books(now)
        if midweek_wake.enabled() and ready["midweek_wake"]:
            try:
                if midweek_wake.check(conn, log=self._logger("midweek wake")):
                    st.live.setdefault("wakeups", []).append(now.isoformat())
            except Exception as error:      # noqa: BLE001 - the worker logs a crashed job and goes on
                rounds["failed"].append({"at": now.isoformat(), "job": "midweek_wake", "error": repr(error)[:300]})
        try:
            if not ready["tracking_daily"]:
                raise _Skipped
            rc = tracking_daily.run(conn, bridge=lambda: st.bridge, log=self._logger("tracking"))
            if rc != 0:
                rounds["failed"].append({"at": now.isoformat(), "job": "tracking_daily", "exit": rc})
        except _Skipped:
            rounds.setdefault("skipped", []).append({"at": now.isoformat(), "job": "tracking_daily"})
        except Exception as error:      # noqa: BLE001
            rounds["failed"].append({"at": now.isoformat(), "job": "tracking_daily", "error": repr(error)[:300]})
        if midweek_wake.enabled():
            self.agents.poke()
        if self.dashboard_url and now >= MID_SEASON and not st.live.get("dashboard_mid_done"):
            st.live["dashboard_mid_done"] = True
            self.dashboard("mid-season")

    def _books(self, now: datetime) -> None:
        """The simulated bridge's books for the poll just taken: the machine's own counts, and our ledger's money
        (the simulated bridge has no tax calculation of its own: `NOT_SIMULATED`)."""
        from vending import ledger

        session = world.poll_document(self.st.machine, now)[0]["session_id"]
        b = ledger.balance(self.conn, now)
        if b is None:
            return
        self.st.bridge.record_books(session, now, b.spendable_cents, b.tax_owed_cents)

    def dashboard(self, label: str) -> dict:
        from vending.simulate import dashcheck

        runs = [r for (r,) in self.conn.execute(
            "select id from agent_runs where mode = 'live' order by (status = 'submitted') desc, id desc limit 3"
        ).fetchall()]      # the run page shows live runs only
        found = dashcheck.check(self.dashboard_url, runs, label=label)
        self.st.live.setdefault("dashboard", []).append(found)
        self.say(f"dashboard check ({label}): snapshot {found.get('snapshot_seconds')} s, "
                 f"unavailable {found.get('unavailable')}, errors {found.get('errors')}")
        return found


def summary(conn: psycopg.Connection, st) -> dict:
    """What the season's report says about the live schedule: the scheduled rows, the runs, refusals, the round's
    jobs, tracking checks, price checks, the dashboard checks, and what isn't simulated."""
    rows = [dict(zip(("due", "kind", "mode", "outcome", "run_id", "detail"), r)) for r in conn.execute(
        "select due_at, kind, mode, outcome, run_id, detail from agent_schedule order by due_at").fetchall()]
    for r in rows:
        r["due"] = r["due"].astimezone(EASTERN).isoformat()
        r["detail"] = (r["detail"] or "")[:400]
    tracking = conn.execute("select status, error_type, used_check, results from tracking_checks order by id").fetchall()
    prices = conn.execute("select due_at, mode, outcome from price_check_runs order by due_at").fetchall()
    refusals = dict(zip(("preview_refused", "submit_refused", "tool_errors"), conn.execute(
        "select count(*) filter (where tool = 'preview_desired_state' and content like '%%\"ok\": false%%'),"
        " count(*) filter (where tool = 'submit_desired_state' and content like '%%\"success\": false%%'),"
        " count(*) filter (where is_error) from agent_steps where kind = 'tool_result'").fetchone()))
    by = lambda key: sorted({str(r[key]) for r in rows})  # noqa: E731
    return {
        "schedule": "live", "env": dict(ENV), "db_settings": dict(DB_SETTINGS),
        "scheduled_rows": rows,
        "counts": {"weekly": sum(r["kind"] == "weekly" for r in rows), "midweek": sum(r["kind"] == "midweek" for r in rows),
                   "by_outcome": {o: sum(r["outcome"] == o for r in rows) for o in by("outcome")},
                   "live": sum(r["mode"] == "live" for r in rows), "dry_run": sum(r["mode"] == "dry-run" for r in rows)},
        "tracking_checks": {"sent": len(tracking),
                            "answered_200": sum(t[0] == 200 for t in tracking),
                            "outside": sum(1 for t in tracking if t[3] and "outside" in str(t[3])),
                            "refused": sorted({str(t[1]) for t in tracking if t[1]})},
        "price_checks": [{"due": p[0].astimezone(EASTERN).isoformat(), "mode": p[1], "outcome": p[2]} for p in prices],
        "refusals": refusals,
        "wakeups": st.live.get("wakeups", []),
        "round_failures": (st.live.get("rounds") or {}).get("failed", [])[:50],
        "rounds": (st.live.get("rounds") or {}).get("count", 0),
        "stuck": st.live.get("stuck", []),
        "day_one": st.live.get("day_one"),
        "dashboard": st.live.get("dashboard", []),
        "not_simulated": list(NOT_SIMULATED),
    }

