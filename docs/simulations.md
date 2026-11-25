# Simulated seasons

**Simulated data.** A simulated season runs our real agent, tools, planner, stock count and ledger
from the day-one fill (Mon 30 Nov) to Wed 3 Feb. The machine, the customers, the bridge and the
student worker are made up (`vending/simulate/`). Use seasons to compare strategies with each other.
They don't forecast real sales.

## Where to see them

`/dashboard/demo/simulations` on the container, behind the shared password. It opens with the
chart from slide 13 of the final deck: cumulative profit by week for each season, after its AI cost
(model spend), with the AI cost of each beside it. The simple rule is dashed. Below the chart is a
table of every committed season. Each season has a page with its weeks, report cards and the agent's
decisions. Every page opens with the banner "SIMULATED season: not real sales or money". The pages
read only the summary files in `vending/simulate/seasons/`. They never open a database connection.
The live dashboard (`/dashboard`) never shows or links a simulation.

## What is kept in git

Only a compact summary of each season (`vending/simulate/seasons/<id>.json`): each week's profit,
cash and stock-outs, the report card, and each run's decision with its reason. The full detail
(every run's tool calls, every request and receipt, every slot on every day) stays on the machine
that ran the season.

Seven seasons are kept, about 1,400 lines in all:

| Season | Why it is kept | Profit after AI cost |
|---|---|---:|
| `2026-11-25-live-rehearsal-opus-medium` | The dress rehearsal: plan #8, the live schedule, Opus 5.5 medium on current main | $1,445.27 |
| `2026-11-25-live-rehearsal-scripted` | The same rehearsal with the simple rule deciding | $1,292.45 |
| `2026-11-16-opus-medium-round3` | The best: Opus 5.5 after round 3's changes, 15 drinks, a top-up rule | $963.69 |
| `2026-11-16-baseline-wednesday` | Slide 13: the simple rule, Wednesday runs, the bar for the fixed agent | $443.09 |
| `2026-11-16-opus-medium-fixed` | Slide 13: the agent after the first fixes | $385.97 |
| `2026-11-16-opus-medium` | The worst, and slide 13's first run: the agent as first built | $317.12 |
| `2026-11-16-interloper-0-25` | The Interloper next door takes 0.25 of a shared drink's demand (totals only) | $412.11 |

All seven seasons from PR #151, for reference (seed 20261203):

| Season | Runs on | Profit after model spend | Units sold | Units lost | Model spend |
|---|---|---:|---:|---:|---:|
| Opus 5.5 medium, round 3 | Wed | $963.69 | 586 | 53 | $4.62 |
| Simple rule, 15 drinks | Wed | $837.99 | 523 | 65 | $0.00 |
| Simple rule | Thu | $464.62 | 324 | 16 | $0.00 |
| Simple rule | Wed | $443.09 | 309 | 21 | $0.00 |
| Opus 5.5 medium, after the fixes | Wed | $385.97 | 302 | 33 | $3.24 |
| Sonnet 5.5 medium | Thu | $330.96 | 244 | 41 | $1.03 |
| Opus 5.5 medium, as first built | Thu | $317.12 | 170 | 11 | $2.84 |

The model seasons ran on PR #151's branch, whose agent prompt and rules card had changes main doesn't
have. They show that agent, not main's.

## Run a season and keep its summary

1. Start a scratch Postgres on this machine (`docs/docker-recovery.md` has the label and the random
   port), then run `python -m vending.db migrate` against it. The simulator refuses any database that
   isn't on 127.0.0.1 or localhost.
2. Write the season's slots to a file: the stored assignment's answer
   (`{"device_id": ..., "slots": [...]}`). `python -m vending.slots` shows it. The simulator holds no
   list of slots.
3. Run the season. Unset `ANTHROPIC_BASE_URL` before a model season. A scripted season makes no
   model calls and costs nothing.

   ```
   python -m vending.simulate --database-url postgresql://postgres:...@127.0.0.1:PORT/postgres \
       --assignment slots.json --decider scripted --name "Simple rule" --strategy "..." --out sim-out/my-season
   ```

   `--decider model` runs the agent (test spend, capped by `--budget`). `--interloper 0.25` adds the
   Interloper. `--run-day wed` moves the weekly run.
4. `sim-out/<id>/` now holds the full detail and `summary.json`. `sim-out/` is ignored by git.
5. To show the season, copy the summary into the repository and check it:

   ```
   python -m vending.simulate.summary sim-out/my-season --id 2026-11-20-my-season \
       --out vending/simulate/seasons/2026-11-20-my-season.json
   python -m vending.simulate.summary --check
   ```

## A live-schedule rehearsal

`--schedule live` runs the season the way the competition will run (`vending/simulate/live.py`):

| Live | In the rehearsal |
|---|---|
| Sat 28 Nov (before Amazon's 23:59 cutoff): `ledger reset-test-opening`, `ledger open --amount 600.00`, `python -m vending.fill --submit` of the picked plan | The same commands at 18:00 ET Sat 28 Nov, against the simulated bridge. Stocked Mon 30 Nov; an Amazon line past its cutoff (Sat 23:59 for the stocking, Wed 23:59 for a Friday) goes to back stock and a later visit loads it |
| The worker's scheduler: `AGENT_SCHEDULE` `wed 14:00-22:00,thu 12:00-20:00` from 2 Dec, both switches `live` | `vending.agent_schedule.Scheduler` itself, with those settings, on the season's clock |
| Midweek wake-ups (`MIDWEEK_WAKE=on`), the run lease, `model_run_cap_cents` | The same code |
| The daily tracking check (`TRACKING_DAILY=on`) | `tracking_daily.run` against the simulated bridge's books |
| The weekly price check (`PRICE_CHECK_MODE`) | `price_schedule.PriceChecks` as a dry run; the world re-prices on Tuesday at 12:30 |
| Sourcing and prices: the team database's four-store rows | `--from-export`: the same rows, exported read-only |

The real scheduler, the run lease and the submit's checks read the database's clock, so the scratch database gets
the season's clock (`vending/simulate/dbclock.py`: `now()` and `clock_timestamp()` in the schema, before
`pg_catalog` on the search path). The simulator adds that to the URL itself.

```
python -m vending.simulate.export --plan 8 --out sim-out/export.json      # read only; DATABASE_URL is the team's
python -m vending.simulate --database-url postgresql://postgres:...@127.0.0.1:PORT/postgres \
    --schedule live --from-export sim-out/export.json --decider scripted --out sim-out/rehearsal
```

The report's `rehearsal.json` (and the summary's `rehearsal`) lists every scheduled row, the day-one steps, the
round's failures, the tracking and price checks, and the dashboard timed against the scratch database mid-season and
at the end (`vending/simulate/dashcheck.py`: each panel's seconds, panels unavailable, `/dashboard/run/<id>` for
the newest runs). `live.NOT_SIMULATED` says what it still can't do.

To get the full detail of the seven seasons above, check out
`add_full_competition_simulation` (PR #151, closed). Its `docs/simulations/data/<id>/` keeps them.
`python -m vending.simulate.summary <dir> --id <id>` turns one into a summary.

## Integrity

- The repository is public. A season on the current assignment is committed with `--redact-slots`: no `assignment`,
  and every selection named in its text replaced by `[slot]` (`summary.redact`), so the file never says which
  selections are ours (David's honor rule, 20 Nov).
- A season's slots are the assignment it was given (`--assignment`), stored and confirmed through
  `vending.slots` as live. The summary records them as `assignment`. Nothing names them as columns.
- Demand comes from fall data, before the fall cut, when no selection was either team's
  (`docs/integrity.md`).
- The Interloper knob changes only what simulated customers buy. Our pricing never uses its prices
  (`docs/agent/interloper.md`).
