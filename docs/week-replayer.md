# The week replayer

Tests a decision-maker on weeks that have already happened (issue #26). It rebuilds a past week
from the polls we already store, asks a decider what to stock, how many and at what price, and
scores the answer against what actually sold the week after. It turns "which model, which
effort, does jev help" into numbers instead of opinions.

**This first version makes no model calls.** The only deciders are a simple baseline script and
a plan read from a JSON file. Nothing writes: the replay connection is read-only, and nothing
polls the machine.

```
python -m vending.replay --week 2026-10-02 --decider baseline
python -m vending.replay --week 2026-10-02 --unit-cost 0.80 --balance 600 --reasons
python -m vending.replay --cases
python -m vending.replay --case tuesday-sellout --reasons
python -m vending.replay --case low-balance --decider file:my-plan.json
```

You need the local database with real polls in it, as in [`mcp-server.md`](mcp-server.md):
`docker compose up -d db`, `python -m vending.db migrate`, then `python -m vending.sync_history`.
`--cases` and `--case` need no database.

## What it does

1. **Rebuilds the week.** A week is 7 days from midnight Eastern on the date you give. Polls give
   each slot's price, units sold, tax-included revenue and units per day, through the same code as
   `get_sales_history`. It notes when the price changed during the week, and warns when the polls
   bounding it span more than a day away from 7 days: a gap in the polls, or a week not over yet.
   The table shows the following week's actual poll span and warnings too, since that week is the
   demand every plan is scored against. **Product, stock and unit cost are unknown** until restock receipts are
   synced (PR #25), so they stay unknown. Nothing unknown is ever filled with zero.
2. **Asks a decider.** It gets the week and returns a plan: for each of the 20 slots, the units on
   the rail after the visit (`fill_to`), the price, the product if it changes, and a short reason.
   It can also ask for a `midweek` visit instead of the free Friday one.
3. **Runs the hard checks.** Each passes, fails, or is skipped when the replay lacks what it needs.
   A skip is never a pass.
4. **Scores the outcome** against what sold the following week: units short, units unsold and
   margin, per slot and in total.

## The hard checks

| Check | Fails when | Skipped when |
| --- | --- | --- |
| valid plan | The answer isn't a plan: not JSON, a slot missing, twice or not ours, a price that isn't exact text such as `"2.50"` (a bare number is refused: cents or dollars?), a `fill_to` that isn't a whole number from 0 to 50, `"product": null`, a slot with no reason, an unknown visit type. Every reason is listed at once. | Never |
| catalog | A named product isn't in the active catalog (a pending request), or its catalog request was refused | The catalog is unknown and the plan names a product |
| prices | A price isn't in 5-cent steps, is zero, is above the ceiling, or is below the floor, all through `vending/pricing.py` | A slot's unit cost is unknown, so its floor can't be checked. Steps, zero and the ceiling are still checked, and still fail. |
| balance | The plan's purchases, plus $25 for a midweek visit, cost more than the balance | The balance or a unit cost is unknown |
| no writes | A write tool call was logged in `action_log` while the decider ran. It counts every write, so run replays when nothing else is writing. | The decider can't reach the database (the baseline and file plans) |

**The price ceiling is $6.00**, the team's proposal in "Decide as a team". It lives in
`PRICE_CEILING_CENTS` in `vending/replay/scoring.py`. The balance check uses actual unit cost;
the restock planner will check the max pack price once it plugs in.

## The outcome score

| Column | Meaning |
| --- | --- |
| Demand | What the slot actually sold the following week |
| Sells | What the plan would have sold: the smaller of demand and what was on the rail |
| Short | Demand the rail couldn't meet |
| Unsold | Units left on the rail |
| Margin | Revenue ex tax, minus units sold × unit cost. Needs a unit cost; `--unit-cost` assumes one for every slot |

The total line says how many of the 20 slots could be scored. A slot is left unscored when its
demand, stock or price is unknown, or when the plan puts a new product in it.

## Demand assumptions

**This is the known weak spot.** We only see what sold at the prices that were really set, so the
score has to assume what demand would have been. These are entries A1 to A5 in the
[assumptions register](assumptions.md), and they print under every score table.

1. **Demand in a slot is what it actually sold the following week.** That's a floor, not the real
   demand: if the slot ran out, more would have sold. Without stock counts we can't see a sell-out,
   so stockouts are undercounted. The `tuesday-sellout` case shows the effect.
2. **Demand doesn't depend on price.** A plan that changes a price is scored as if the same number
   would have sold, and the slot is noted. So the score can't reward or punish a price change yet.
3. **A new product has no sales to score against**, so that slot is left unscored. Grade it by its
   reason instead.
4. **Where stock is unknown, the rail holds exactly `fill_to` after the visit** and every unit is
   bought. That's the worst case for the balance check.
5. **Unsold units cost nothing in the margin.** The closet is valued at what we paid (David, 19
   Oct), so unsold units are counted, not charged.

## Deciders

A decider has a `name` and a `decide(state)` method, in `vending/replay/deciders.py`. It returns a
`Plan`, or a plain dict in this JSON shape, which is what a model or a hand-written file gives:

```json
{"visit": "scheduled",
 "reason": "one line for the whole plan",
 "slots": [
   {"slot": "15", "fill_to": 7, "sale_price": "2.50", "reason": "sold 7 last week"},
   {"slot": "16", "keep": true, "reason": "no sales figure, leave it"},
   {"slot": "17", "product": "MTDEW-20OZ", "fill_to": 6, "sale_price": "2.25", "reason": "..."}
 ]}
```

The fields follow the desired state in [`planning/tool-list.md`](planning/tool-list.md). `fill_to`
is units on the rail after the visit; `product` left out means whatever is there now; `keep` means
no change. A `sourcing` list is passed through unchecked, for the restock planner. It differs from
the team's format in three places, all until the data or David settles them:

- **`fill_to` can't be left out** to mean "fill to capacity", because there is no slot capacity
  yet. It must be 0 to 50, a sanity limit (`MAX_FILL` in `deciders.py`), not a capacity.
- **`"product": null` is refused**, as the team's format refuses an empty slot until David
  confirms one is allowed.
- **Naming the product already in a slot** is fine on a test case. On a replayed week we don't
  know what's in a slot, so a named product can't be told apart from a switch, and that slot is
  left unscored (assumption A3).

| Decider | What it does |
| --- | --- |
| `baseline` | Restocks each slot to last week's sales at the same price, and never moves a product. Keeps at least 1 on the rail rather than leave a slot empty. Keeps any slot it has no figures for. It's the bar any other decider should clear. |
| `file:PATH` | Scores a fixed plan from a JSON file: a hand-written plan, or a model's saved answer |

**The restock planner (PR #13) isn't a decider.** It checks and prices a desired state; it doesn't
choose one. Once it merges, it plugs in as one more entry in `CHECKS` in `scoring.py`, converting a
`Plan` to its `DesiredState`, so every refusal it knows (cutoff, sourcing, max pack price) becomes
a hard check too.

## The test cases

`--cases` scores a decider on ten built weeks, in `vending/replay/cases.py`. **Their numbers are
made up** to pose each question; none is a real sale, cost or balance. Unlike a replayed week, a
case knows stock, unit cost, the balance and the catalog, so every check gets exercised.
`--case NAME` prints one in full, with the question and what a good answer looks like.

| Case | From | The question |
| --- | --- | --- |
| `slow-seller` | Decide as a team; whiteboard | When do we pull a slot that sold nothing two weeks running? |
| `fills-first` | Decide as a team | What goes in our 20 slots first? A cold start: not scoreable on outcome |
| `midweek-visit` | Whiteboard | Is a $25 midweek visit worth it after two sell-outs on Tuesday? |
| `catalog-request` | Whiteboard | Can we stock a product whose catalog request is still pending? |
| `holiday-drinks` | Decide as a team; whiteboard | Does a seasonal drink get a slot, and which slot gives way? |
| `tuesday-sellout` | Stress | A sell-out on Tuesday hides the real demand |
| `low-balance` | Stress | $8 left; a full restock costs $100 |
| `price-below-floor` | Stress | Keeping the price would break the floor |
| `rejected-request` | Stress | A catalog request was refused. Is it still planned? |
| `nonsense-input` | Stress | Half the inputs are unknown. Does the decider invent numbers? |

The baseline fails `low-balance` and `price-below-floor`, and comes up short on `tuesday-sellout`
and `midweek-visit`. That's expected: it's deliberately naive, and those cases are there to catch it.

The issue's third layer, **a short rubric for people to grade**, is the `good_answer` on each
case plus `--reasons`: is the reasoning sound, and could operations act on the plan without
asking?

## The model decider (costs money)

```
python -m vending.replay --case midweek-visit --decider model --reasons
python -m vending.replay --week 2026-10-02 --decider model --model claude-sonnet-5-5 --effort low
```

Claude reads the week as JSON and answers with a plan in the decider shape above, forced by a JSON
schema. The plan is checked and scored like any other. One API call per decision. **The default
is Opus 5.5 at medium effort, pending a group decision**; `--model` takes `claude-sonnet-5-5` or
`claude-haiku-5-5`, and `--effort` takes `low` to `max`. A refusal or an answer cut off mid-JSON
fails "valid plan".

**Every call goes through the harness (`vending/harness.py`) as a `test` call, so every use of the
key is in the database:**

- **$1.00 a decision.** The input is counted first, which is free. The answer's length limit, which
  includes the model's thinking, is then set so the most the call can cost fits under the cap. If
  even a short answer won't fit, the call isn't made.
- **The team's daily test cap**, `model_test_daily_cap_cents` in `config` ($20 by default; set it
  with `python -m vending.config set model_test_daily_cap_cents 2000 --by NAME`). It counts every
  test call made today (Eastern), by anyone, against the same database. Once it's spent, `--cases`
  marks the remaining cases "not scored". Live calls have their own cap, `model_daily_cap_cents`.
- **No retries**, so a timeout can't bill twice.
- **Two runs at once can't both spend the same dollar.** The worst case is reserved under a database
  lock before each request is sent.
- **Every request is a row in `model_calls`**: when, which model and effort, which key paid (the
  variable's name, never the key), tokens in and out, the cost, how long it took, the stop reason,
  Anthropic's request ID, and what asked for it (`exam 2026-10-02`, `sweep ... opus@high
  slow-seller`, `agent-week ... 3`). It is written at its worst case before the request is sent and
  corrected after. A request the API rejected (bad key, rate limit) stays at $0 as `refused`. One
  that timed out, failed on the server, or crashed stays at its worst case. **Test calls never reach
  the ledger:** they aren't the competition's $600 (Kevin, 4 Nov), but they are never invisible.
  `python -m vending.usage` reports them; the dashboard and DB explorer can read the same table.
- **So the replay needs the database, even for `--cases`.** Spend we can't record isn't spent:
  with the database down, nothing is sent.

**The key** comes from the `VENDING_ANTHROPIC_API_KEY` environment variable, never from an
`ANTHROPIC_*` variable in any upper or lower case (Claude Code reads those, and the team key would
pay for its sessions). `--key-env NAME` uses another variable, such as a personal key, and each
`model_calls` row records which one paid. Requests go to api.anthropic.com, whatever
`ANTHROPIC_BASE_URL` says; `VENDING_ANTHROPIC_BASE_URL` overrides it (the tests use it for a local
stand-in). Prices are in `vending/model_prices.json`, the harness's price table.

## The model sweep (costs money)

```
python -m vending.replay.sweep --dry-run
python -m vending.replay.sweep --budget 15
python -m vending.replay.sweep --models haiku,sonnet --efforts low,medium --repeats 3 --budget 5
```

Runs the model decider on every test case for each model and effort, to turn "which model, which
effort" into numbers. Each (model, effort, repeat) answers all ten cases. The baseline is scored on
the same cases, free, as the bar to clear.

- **One budget for the whole sweep** (`--budget`, at most $50; $15 by default). Before each request
  it reserves that request's worst case; one that won't fit isn't sent, and the summary lists every
  dropped request, so a short sweep never reads as a full one. Every request also keeps the
  decider's own caps above.
- **The cheapest models run first**, so a budget that runs out cuts the dearest configs.
- **Results** go to `sweep-results/<time>/` (gitignored): `results.jsonl`, one line per answer as it
  lands; `plans/`, every plan as the model gave it, for people or a judge to grade against each
  case's `good_answer`; and `summary.csv`, `summary.json` and `summary.txt`, one line per config:
  valid plans, plans that pass every hard check, units short and unsold, margin, cost, cost per
  decision, average output tokens and seconds.
- **Every request is a `model_calls` row** labelled with the sweep's time, the config and the case,
  so a sweep can be found and totalled in the database (`python -m vending.usage`).
- **The cases' numbers are made up.** A sweep can narrow the choice of model and effort and show
  whether more effort pays; it can't predict real profit. Valid and "checks ok" count hard checks
  only, not whether a plan is a good one.

## The agent replay (costs money)

```
python -m vending.replay --week 2026-09-25 --agent 4 --unit-cost 0.80 --balance 600
```

Closer to the live agent than the one-question exam above. Claude runs the weeks in order. Each
week it's the end of the week, before the Friday restock:

1. **It looks things up with the agent's own read tools,** `get_machine_state` and
   `get_sales_history` from the MCP server (#14), with the same replies. They're cut off at that
   week, so it can't see the weeks it's tested on: a window that runs past "now" is cut at now,
   and one that starts after it is refused. The tools #14 hasn't built yet aren't offered, and it
   is told so.
2. **It gets the notes it wrote itself last week.**
3. **It answers with three things:** the plan for every slot; a **weekly update** for the team,
   under 150 words, on what happened and what it's changing and why; and **notes** for next week.

The plan is checked and scored like any decider's. The table puts each week beside the baseline's
margin on the same week. The weekly updates and notes go to a Markdown file under
`replay-results/agent-runs/` (gitignored), for people to grade: is the reasoning sound, and could operations act on
the update without asking?

**Caps:** the same `Budget` as the exam, so every request is a `test` row in `model_calls`, one run
per week. A week's whole conversation, every tool round included, gets the $1 run cap; the team's
daily test cap covers everything. At most 10 tool rounds a week, then it
must answer. A week that runs out of budget part-way gets no plan, fails "valid plan", and the
replay stops there. So does any failed request, a rate limit included (retries are off), and any
other error, such as the database dropping. The weeks already run are still shown and saved. At
most 12 weeks a run, and `--week` must be a Friday, because Claude is told the restock is that day.

## Not built yet

- **The restock planner as a check**, once PR #13 merges.
- **Stock, product and unit cost from receipts**, once PR #25 merges. Until then, replayed weeks
  can't score stockouts from stock, and margin needs `--unit-cost`.

## Tests

```
pytest tests/test_replay.py tests/test_replay_model.py tests/test_replay_agent.py tests/test_replay_sweep.py
```

The replay and CLI tests store fixture polls in a throwaway schema through `store_poll`, the
history sync's own `store`, so they run against a real database. They skip when it's down, like
the rest. The plan-check, scoring and case tests need no database. The model, agent and sweep tests
answer from a local stand-in for the Anthropic API, so no test spends money, and every `model_calls`
row they write goes to the throwaway schema, never the real database.
