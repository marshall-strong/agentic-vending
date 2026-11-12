# The standing benchmark

The same test, run again after every change, with every result kept. We keep changing the agent's
prompt, the team rules, the test cases and the model. The benchmark tells us whether each change
made the agent's decisions better or worse, and the database keeps every run, so a slow slide shows
up as a trend and a sudden drop as a regression.

```
.\scripts\run_bench.ps1                              # one click: plan, type yes, run, report
python -m vending.bench run --dry-run                # what would run, its worst case, today's cap
python -m vending.bench run --note "prompt: name the threshold"
python -m vending.bench report                       # the latest run against the last comparable one
python -m vending.bench report --history 10          # each config's score over the last 10 runs
python -m vending.bench report --json                # the same, as JSON for scripts
python -m vending.bench list                         # every run, newest first
```

**Before the first run**, apply the migrations, so the database has the `bench_runs` and
`bench_results` tables (`db/migrations/012_bench.sql`):

```
python -m vending.db migrate
```

Without them, `run`, `report` and `list` stop and say so, and nothing is spent. The run also needs
the team's Anthropic key. `scripts\run_bench.ps1` loads it from `~\.vending\anthropic-key`, a file
only your Windows account can read, or else from your `VENDING_ANTHROPIC_API_KEY` user variable.
The README's "The Anthropic API key is for the agent only" says how to set up either.

## What it tests

Each run does three things.

1. **It answers the ten test cases** in `vending/replay/cases.py` with three configs (model @
   effort): `haiku@medium`, `sonnet@high` and `opus@low` by default. This goes through the model
   sweep, so it uses the same prompt, the same caps, the same `model_calls` rows and the same hard
   checks as `python -m vending.replay.sweep` ([week-replayer.md](week-replayer.md)). The baseline
   (restock to last week's sales) answers too, for free.
2. **A judge grades every plan** against the case's `good_answer`. This is Opus at medium effort
   by default, one request per case. The judge sees all the plans for a case at once, blind, as
   P1, P2 and so on, and grades each one:
   - **decision, 0-3**: 3 makes exactly the call the good answer describes, 2 makes the right call
     with a flaw, 1 is partly right, 0 makes the wrong call.
   - **reasoning, 0-2**: 2 is true to the numbers and operations could act on it without asking,
     1 is mostly sound, 0 is vague, wrong or contradicts the plan.
   - **invented numbers**: whether the plan treats an unknown as a number or states a figure that
     isn't in the week.

   The rubric is `RUBRIC` in `vending/bench/judge.py`.
3. **It stores everything.** One `bench_runs` row holds the git commit, a hash of the prompt, a hash
   of the cases and a hash of the rubric. One `bench_results` row per answer holds the plan, the
   hard checks, the outcome numbers, the cost, the time and the grade.

**Score** = 100 x (decision + reasoning) / (5 x plans judged). A config that makes every right call
with sound reasons scores 100%.

A run ends **done**, or **stopped** when it crashed, couldn't store its answers, or no model
answered a single case. A run still marked **running** was killed before it could say which (a
closed window, a power cut). Only done runs are compared with.

## What it can't tell you

- **The cases are made up.** They pose the team's open questions, such as when to pull a slow
  seller or whether a $25 midweek visit pays. They don't use real sales, so a score says how well a
  config reasons about those questions. It doesn't predict profit.
- **Outcome margin barely separates the models.** On the first run, the nine configs' margins
  differed by less than 3%, and the baseline had the highest margin of all while it broke two hard
  rules. That is why a judge grades the decision, and why margin is not in the score.
- **The judge is a model too.** It can be wrong, and the same plan can get a different grade on
  another day. Two things keep it honest:
  - **A control it must fail.** The baseline is built to make the wrong call on five trap cases:
    `slow-seller`, `midweek-visit`, `tuesday-sellout`, `low-balance` and `price-below-floor`. On
    those cases the baseline keeps the dead product, skips the visit, plans at the sold-out count,
    overspends the $8 or prices below the floor. If the judge gives the baseline 2 or more on one of
    these cases, it didn't read the plans. That case's grades are left out of every score, and the
    report says "Judge failed its control on <case>".
    **Two of the five are weaker tests.** On `low-balance` and `price-below-floor` the baseline's own
    `balance` or `prices` check shows `fail`, and the judge sees each plan's checks. So a judge that
    reads only the check list still fails the control there. Only `slow-seller`, `midweek-visit`
    and `tuesday-sellout` show that the judge read the plan itself.
  - **The control's own score between runs.** On the same cases the baseline's plans are always the
    same. So a change in the baseline's score is the judge's own noise, and the report says so when
    it moves 10 points or more. Read smaller changes in the other configs with that in mind.
- **One answer per case is a small sample.** Ten cases at 5 points each make 50 points. One case
  that goes from 3 to 1 moves a config about 4 points. `--repeats 3` gives steadier numbers at three
  times the cost.

## Reading the report

An example (made-up numbers):

```
Config              Judged  Score  Right  Wrong  Invented  Valid  Checks ok  Errors  Dropped  $/decision  Secs  vs run  Change
baseline (control)  10/10   28.0%  1      5      0         10/10  8/10       0       0        $0.0000     0.0   12      +0.0
haiku@medium        10/10   80.0%  7      0      0         10/10  10/10      0       0        $0.0016     10.4  12      -2.0
sonnet@high         9/10    82.2%  7      0      0         9/10   9/10       1       0        $0.0279     13.4  12      +1.1 on 8
...
```

- **Judged** is the plans whose grade counts, out of the plans the config was asked for. A plan
  doesn't count when it wasn't answered, when the judge couldn't grade it, or when the judge failed
  its control on that case. **Right** is decision 3, **Wrong** is decision 0, and **Invented** is the
  plans that made up a number.
- **Valid** and **Checks ok** are the automatic hard checks: a plan in the right shape, and a plan
  that breaks no rule (catalog, price floor, balance).
- **Errors** are requests that failed or were refused. **Dropped** are requests never sent, because
  the run's budget or the daily cap couldn't hold their worst case.
- **vs run** is the run the score is compared with: the latest earlier finished run on the same
  cases (the same `cases_hash`), graded by the same judge and rubric, with at least one counted
  grade for this config. A run in between with no grades (`--judge none`, or a budget that ran out
  before judging) or with another judge is passed over. The hard checks, the cost and the cases
  answered are compared with the latest earlier run on the same cases that ran this config, judged
  or not. When that is a different run, it is shown as "checks N".
- **Change** is the score difference in points, over the plans judged in both runs, so a config
  can't raise its score by answering fewer cases. "on 8" means it rests on 8 plans, fewer than
  either run judged.

**Regressions** are listed by name:

- the score fell 10 points or more;
- a case went from decision 3 to 1 or lower;
- more of the config's plans went unjudged than in the run compared with, so its score covers fewer
  cases;
- a case it answered before now ends in an error or is dropped;
- a case now fails a hard check that it passed before;
- the cost per decision rose by more than half.

The first three compare with the score's base run, the last three with the checks' base run.

**What changed in between** is listed too: "prompt changed since run N", "judge changed since run
N" or "code changed since run N (abc1234 -> def5678)". This is the point of the benchmark: when a
score moves, check this list first.

## When to rerun

Rerun after any change to:

- the prompt (`SYSTEM` or `PLAN_RULES` in `vending/replay/model.py`);
- the team rules the agent follows;
- the cases;
- the model or effort we plan to use.

Put what changed in `--note`, so the history reads like a changelog. A run with uncommitted changes
is marked, because then the git sha doesn't say exactly what ran.

**Nightly**, optionally: `.\scripts\register_bench_task.ps1` registers a Windows task that runs
`run_bench.ps1 -Yes` every day at 02:00 as you. A person must run it and type yes, because it sets
up a recurring spend. Remove it with `-Unregister`. A nightly run with nothing changed is still
useful, because it measures run-to-run noise. Expect a few points of difference between two runs of
the same inputs. The 10-point regression line is set above that.

## Adding or changing a case

Cases live in `all_cases()` in `vending/replay/cases.py`. Each case needs a question, a built week,
the week after, and a `good_answer` that is specific enough for a grader to apply, such as "raises
slot 15 to at least $2.60". If the baseline is built to fail the new case, add the case to
`TRAP_CASES` in `vending/bench/judge.py`.

**Any change to a case changes `cases_hash`, so the trend restarts.** Runs are compared only with
runs on the same cases. When no earlier run has this run's cases, the report says "cases changed
since run N", and nothing before it is compared. A `--cases` subset run in between doesn't break
the trend: the next full run is still compared with the last full one. This is on purpose: a score
on different questions is a different score.

## Cost

About $1-2 a run at the default three configs. This is an estimate. On the first sweep the three
configs' answers cost about $0.80 for ten cases. The judge's cost isn't measured yet. A guess is
about $0.10 a case on Opus at medium, so about $1 a run.

Every run is capped:

- **`--budget`**: $5 by default, $50 at most. Answers and the judge share this one budget. Before
  each request, its worst case is reserved, and a request that won't fit isn't sent. If the budget
  runs out before judging, the cases are left unjudged and the report says so.
- **The team's daily test cap**: `model_test_daily_cap_cents`, $20 by default. When today's is
  used up, a run is refused before it starts. When less than the budget is left, the run stops
  at about what is left.
- **$1 a request**, the judge's included, through the harness. A request whose worst case is
  more than that gets a shorter answer limit, or isn't sent.

Every request is a `test` row in `model_calls`, labelled `bench <run id> ...`. So
`python -m vending.usage` shows every request, and the bench's own `spent_usd` is the total of those
rows. Test spend never reaches the ledger.

## The first run (11 November)

The first model sweep (`sweep-results/20261111-173503`) ran nine configs on the ten cases and spent
$2.43. Blind Claude Code subagents graded it with the same rubric, but the baseline wasn't graded,
so that judge had no control. Import it as the benchmark's first run with:

```
python -m vending.bench import --dir <path>\sweep-results\20261111-173503 --grades <path>\grades.json
```

An imported run's git sha is taken when you import it, not when the run was made, and its note says
so. Its prompt and cases hashes are left empty, so no later run is compared with it: the prompt or the
cases may have changed between the run and the import (the 11 Nov sweep started at 17:35 ET, and
`4795a15` changed `cases.py` and `model.py` at 17:50). Add `--same-inputs` only when you know they
match; the import then takes today's hashes. The same folder can't be imported twice. These figures come from
`grades.json` and `summary.json` through the importer:

| Config | Score | Points of 50 | Right | Wrong | Invented | Checks ok | $/decision | Secs |
|---|---|---|---|---|---|---|---|---|
| opus@low | 86% | 43 | 8 | 1 | 0 | 10/10 | $0.0492 | 14.8 |
| sonnet@high | 82% | 41 | 7 | 0 | 0 | 10/10 | $0.0279 | 13.4 |
| haiku@medium | 80% | 40 | 7 | 0 | 0 | 10/10 | $0.0016 | 10.4 |
| opus@medium | 80% | 40 | 7 | 1 | 2 | 10/10 | $0.0570 | 18.3 |
| opus@high | 78% | 39 | 6 | 1 | 2 | 10/10 | $0.0609 | 20.5 |
| sonnet@low | 74% | 37 | 6 | 0 | 0 | 10/10 | $0.0214 | 8.9 |
| sonnet@medium | 74% | 37 | 6 | 0 | 0 | 10/10 | $0.0217 | 8.9 |
| haiku@high | 72% | 36 | 6 | 0 | 0 | 10/10 | $0.0019 | 13.1 |
| haiku@low | 70% | 35 | 6 | 1 | 2 | 9/10 | $0.0014 | 11.8 |

What it showed:

- **More effort didn't help.** Opus scored best at low effort, and Haiku scored best at medium.
- **Haiku at medium was within 6 points of the top** at about a thirtieth of Opus's cost per
  decision. That is why the default configs are the best one per model.
- **Every config failed at least one case that the outcome numbers can't see.** On `slow-seller`,
  eight of the nine left the dead product in slot 15. On `midweek-visit`, all three Opus configs
  skipped a visit that pays.
- **Margin ranked them differently.** Margin ran from $1,110.59 (sonnet@low) to $1,140.39
  (opus@high), which ranked opus@high first and opus@low near the bottom. That is the reason for the
  judge.

## Where things are

| | |
|---|---|
| `vending/bench/__main__.py` | The CLI: run, import, report, list |
| `vending/bench/judge.py` | The rubric, the blinding, the judge request, the control |
| `vending/bench/report.py` | Scores, comparisons, regressions, history |
| `vending/bench/store.py` | The hashes and the `bench_runs` / `bench_results` rows |
| `db/migrations/012_bench.sql` | The tables |
| `scripts/run_bench.ps1`, `scripts/register_bench_task.ps1` | One click, and the nightly task |
| `bench-results/` (gitignored) | Each run's raw answers, as the sweep writes them |
| `tests/test_bench.py` | Against the local fake API: no test spends money |
