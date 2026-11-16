"""python -m vending.simulate: run a simulated season against a scratch database, then write the report.

    python -m vending.simulate --database-url postgresql://postgres:sim@127.0.0.1:PORT/sim \\
        --decider scripted --out docs/simulations/data/baseline
    python -m vending.simulate --database-url ... --decider model --budget 9.50 --stop-at 2026-12-05 \\
        --out docs/simulations/data/agent          # a pilot: day one and week 1, then stop
    python -m vending.simulate --database-url ... --decider model --resume --out docs/simulations/data/agent

`--database-url` is required and must be on this machine; DATABASE_URL is set from it for the run, so
nothing reads `.env`. The model decider pays with VENDING_ANTHROPIC_API_KEY, as `vending.agent` does,
and every call is test spend under `--budget`.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, time
from decimal import Decimal
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vending.simulate", description=__doc__.splitlines()[0])
    parser.add_argument("--database-url", required=True)
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
    parser.add_argument("--stop-at", help="stop (and save) at this Eastern date, e.g. 2026-12-05")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--out", type=Path, required=True, help="directory for the checkpoint and data files")
    args = parser.parse_args(argv)

    from vending.simulate import season
    season.check_scratch(args.database_url)
    os.environ["DATABASE_URL"] = args.database_url
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
            "elasticity": args.elasticity, "facing_exponent": args.facing_exponent}
    with db.connect() as conn:
        conn.autocommit = True
        if args.resume:
            st = season.load(checkpoint)
        else:
            st = season.new_state(args.seed, args.decider, args.model or agent.DEFAULT_MODEL,
                                  args.effort or agent.DEFAULT_EFFORT, args.check_ins,
                                  {"wed": 2, "thu": 3}[args.run_day])
            from vending.simulate.clock import patched
            with patched(st.clock):
                season.setup(conn, st, args.budget, args.run_cap)
        st = season.run_season(conn, st, opts, checkpoint, stop_at)
        report.write(conn, st, args.out, opts)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
