"""python -m vending.simulate: run a simulated season against a scratch database, then write the report.

    python -m vending.simulate --database-url postgresql://postgres:sim@127.0.0.1:PORT/sim \\
        --assignment slots.json --decider scripted --out sim-out/baseline
    python -m vending.simulate --database-url ... --assignment slots.json --decider model --budget 9.50 \\
        --stop-at 2026-12-05 --out sim-out/agent          # a pilot: day one and week 1, then stop
    python -m vending.simulate --database-url ... --decider model --resume --out sim-out/agent
    python -m vending.simulate --database-url ... --schedule live --from-export sim-out/export.json \
        --decider scripted --out sim-out/rehearsal     # the live schedule, the picked plan, the team's prices

`--schedule live` runs the season as the competition will run (`vending.simulate.live`): the day-one fill is the
exported picked plan, sent with `python -m vending.fill --submit` after `ledger open --amount 600.00`, and the
agent's runs come from the worker's own scheduler (`AGENT_SCHEDULE`, midweek wake-ups, the run lease), with the
round's tracking and midweek checks. `--from-export` is a file from `python -m vending.simulate.export` (read only
from the team database): our slots, the plan and the four-store prices. The season's report ends with the
dashboard timed against the scratch database (`vending.simulate.dashcheck`), mid-season and at the end.

`--assignment` is the season's slots: a JSON file holding the inventory server's answer
(`{"device_id": ..., "slots": [...]}`) or just its list. Copy it from the stored assignment
(`python -m vending.slots`); the code holds no list of its own. `--out` gets the full detail of the
season (`vending.simulate.report`) and its compact summary (`summary.json`). Keep the detail out of
git: only summaries are committed (docs/simulations.md).

`--database-url` is required and must be on this machine; DATABASE_URL is set from it for the run, so
nothing reads `.env`. The model decider pays with VENDING_ANTHROPIC_API_KEY, as `vending.agent` does,
and every call is test spend under `--budget`.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from datetime import datetime, time
from decimal import Decimal
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vending.simulate", description=__doc__.splitlines()[0])
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--assignment", type=Path,
                        help="the season's slots: the server's answer as JSON (needed unless --resume)")
    parser.add_argument("--name", help="the season's name in its summary (default: the decider and model)")
    parser.add_argument("--strategy", default="", help="one sentence on what this season tries, for its summary")
    parser.add_argument("--decider", choices=["scripted", "scripted-wide", "model"], default="scripted")
    parser.add_argument("--model", default=None)
    parser.add_argument("--effort", default=None)
    parser.add_argument("--seed", type=int, default=20261203)
    parser.add_argument("--budget", type=Decimal, default=Decimal("9.50"), help="dollars of model spend, all runs")
    parser.add_argument("--run-cap", type=Decimal, default=Decimal("2.00"), help="dollars per agent run")
    parser.add_argument("--min-run", type=Decimal, default=Decimal("0.30"),
                        help="skip a model run when less than this is left of the budget")
    parser.add_argument("--check-ins", action="store_true", help="Monday check-ins when 4+ slots are empty")
    parser.add_argument("--run-day", choices=["wed", "thu"], default="thu",
                        help="the weekly run's day, at 19:00 (Amazon's cutoff for Friday is Wednesday 23:59)")
    parser.add_argument("--elasticity", type=float, default=0.46)
    parser.add_argument("--facing-exponent", type=float, default=0.5)
    parser.add_argument("--interloper", type=float, default=0.0,
                        help="share of a drink's demand the Interloper next door takes when it sells the same "
                             "line at our price (0 = no Interloper; energy drinks lose 0.4 of that)")
    parser.add_argument("--schedule", choices=["run-day", "live"], default="run-day",
                        help="what starts the agent: one run a week at 19:00 (--run-day), or the worker's live "
                             "scheduler (needs --from-export)")
    parser.add_argument("--from-export", type=Path,
                        help="a file from python -m vending.simulate.export: our slots, the picked plan and the "
                             "team's prices (instead of --assignment and the sourcing CSVs)")
    parser.add_argument("--stop-at", help="stop (and save) at this Eastern date, e.g. 2026-12-05")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--out", type=Path, required=True, help="directory for the checkpoint and data files")
    args = parser.parse_args(argv)

    from vending.simulate import dbclock, export, live, season
    season.check_scratch(args.database_url)
    if args.schedule == "live" and not args.resume and not args.from_export:
        parser.error("--schedule live needs --from-export: the day-one fill is the team's picked plan")
    if not args.resume and args.assignment is None and args.from_export is None:
        parser.error("--assignment (or --from-export) is needed for a new season")
    data = json.loads(args.from_export.read_text(encoding="utf-8")) if args.from_export else None
    if args.resume:
        assignment = ()
    elif data is not None:
        assignment = export.assignment(data)
    else:
        assignment = season.read_assignment(args.assignment)
    url = dbclock.with_clock(args.database_url)
    os.environ["DATABASE_URL"] = url
    os.environ["VENDING_TOOLS"] = "built"
    os.environ["DEX_BASE_URL"] = "http://127.0.0.1:9"      # a dead port, should anything get past the tripwires
    os.environ["DEX_API_KEY"] = "simulation-has-no-key"

    from vending import agent, db
    from vending.simulate import report, world

    args.out.mkdir(parents=True, exist_ok=True)
    checkpoint = args.out / "checkpoint.pickle"
    stop_at = (datetime.combine(datetime.fromisoformat(args.stop_at).date(), time(0), world.EASTERN)
               if args.stop_at else None)
    opts = {"budget": args.budget, "run_cap": args.run_cap, "min_run": args.min_run,
            "elasticity": args.elasticity, "facing_exponent": args.facing_exponent, "interloper": args.interloper}
    from vending.simulate.clock import patched
    # A resumed season keeps the schedule it started with: the checkpoint says which, whatever --schedule says.
    st = season.load(checkpoint) if args.resume else None
    schedule = getattr(st, "schedule", "run-day") if st is not None else args.schedule
    with db.connect() as conn, (live.environment() if schedule == "live" else contextlib.nullcontext()):
        conn.autocommit = True
        if st is None:
            catalog = export.catalog(data) if data is not None else None
            st = season.new_state(args.seed, args.decider, args.model or agent.DEFAULT_MODEL,
                                  args.effort or agent.DEFAULT_EFFORT, args.check_ins,
                                  {"wed": 2, "thu": 3}[args.run_day], assignment, schedule=schedule,
                                  catalog=catalog)
            if data is not None:
                st.products, left = export.products(data)
                st.live["unit_prices"] = export.unit_prices(data)
                st.live["left_out"] = left
                world.register(st.products)
                world.register_prices(st.live["unit_prices"])
            with patched(st.clock):
                season.setup(conn, st, args.budget, args.run_cap,
                             prices=(lambda c, _now: st.live.__setitem__("seeded", export.load(c, data, season.BY)))
                             if data is not None else None)
                if st.schedule == "live":
                    live.configure(conn)
                    live.day_one(conn, st, data)
        if st.schedule == "live":
            opts["driver"] = live.Driver(conn, st, opts, dashboard_url=url)
        st = season.run_season(conn, st, opts, checkpoint, stop_at)
        if st.schedule == "live" and st.done:
            with patched(st.clock):
                opts["driver"].dashboard("end of season")
        opts.pop("driver", None)
        report.write(conn, st, args.out, opts, name=args.name, strategy=args.strategy)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
