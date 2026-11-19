# Simulated seasons

**Simulated data.** A simulated season runs our real agent, tools, planner, stock count and ledger
from the day-one fill (Mon 30 Nov) to Wed 3 Feb. The machine, the customers, the bridge and the
student worker are made up (`vending/simulate/`). Use seasons to compare strategies with each other.
They don't forecast real sales.

## Where to see them

`/dashboard/demo/simulations` on the container, behind the shared password. It lists every
committed season, and each season has a page with its weeks, report cards and the agent's
decisions. Every page opens with the banner "SIMULATED season: not real sales or money". The pages
read only the summary files in `vending/simulate/seasons/`. They never open a database connection.
The live dashboard (`/dashboard`) never shows or links a simulation.

## What is kept in git

Only a compact summary of each season (`vending/simulate/seasons/<id>.json`): each week's profit,
cash and stock-outs, the report card, and each run's decision with its reason. The full detail
(every run's tool calls, every request and receipt, every slot on every day) stays on the machine
that ran the season.

Three seasons are kept:

| Season | Why it is kept | Profit after model spend |
|---|---|---:|
| `2026-11-16-opus-medium-round3` | The best: Opus 5.5 after round 3's changes, 15 drinks, a top-up rule | $963.69 |
| `2026-11-16-opus-medium` | The worst: the agent as first built | $317.12 |
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

To get the full detail of the seven seasons above, check out
`add_full_competition_simulation` (PR #151, closed). Its `docs/simulations/data/<id>/` keeps them.
`python -m vending.simulate.summary <dir> --id <id>` turns one into a summary.

## Integrity

- A season's slots are the assignment it was given (`--assignment`), stored and confirmed through
  `vending.slots` as live. The summary records them as `assignment`. Nothing names them as columns.
- Demand comes from fall data, before the fall cut, when no selection was either team's
  (`docs/integrity.md`).
- The Interloper knob changes only what simulated customers buy. Our pricing never uses its prices
  (`docs/agent/interloper.md`).
