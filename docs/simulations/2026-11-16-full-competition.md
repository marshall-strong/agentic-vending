# First full-competition simulation (16 Nov 2026)

**This is simulated data.** The machine, the customers and the student worker are made up. The agent,
its tools, the planner, the stock count and the ledger are our real code, unchanged.

We ran our weekly agent through a whole simulated competition: the day-one fill on Mon 30 Nov, then
every Thursday run to the end of Wed 3 Feb. We did it three times: once with the agent as built
(`claude-opus-5-5` at `medium`), once with `claude-sonnet-5-5` at `medium`, and once with a simple
scripted rule and no model at all. Each season is in the dashboard under **Simulations**, where you can
step through the agent dashboard and the P&L as they looked at any moment.

Then we fixed what the first round found and ran two more seasons (below, "Round 2"): the agent with
the fixes and Wednesday runs, and the scripted rule with Wednesday runs.

![The Simulations page](images/simulations-index.png)

## Headline

| Season | Profit after model spend | Units sold | Units lost to empty slots | Model spend | Spendable at the end | Stock left, at cost |
|---|---|---|---|---|---|---|
| Opus 5.5, medium (the agent as built) | **$317.12** | 170 | 11 | $2.84 | $905.32 | $14.64 |
| Sonnet 5.5, medium | **$330.96** | 244 | 41 | $1.03 | $866.37 | $65.62 |
| Baseline rule, no model | **$464.62** | 324 | 16 | $0.00 | $576.71 | $487.90 |

![Profit by week and season to date](2026-11-16-weekly-profit.svg)

- **Neither model beat the simple rule.** The rule refills every slot to its column at the reference
  price. Under our demand assumptions it earned about $140 more than either model. Read the caveat on
  assumptions below before taking that number literally: the gap depends heavily on one of them.
- **The agent as built left most of the machine idle.** It used 12 of its 20 slots and only two drinks
  (Red Bull and Celsius) all season. Eight slots sat empty and $900 sat unspent at the end. Cash in the
  account is not profit (`docs/agent/goal-and-scoring.md`).
- **It was cheap and it never broke a rule.** $2.84 for 11 runs, about $0.29 a run, a third of the
  $1.00 the capital plan allows. No price was off the floor-to-ceiling band, no cap was hit, no request
  was refused by the bridge, and the balance never went near zero.
- **Sonnet earned slightly more for a third of the cost.** It did the day-one fill and stocked the
  sodas, so it sold more, but ran more slots empty between visits (41 units lost).

## Round 2: after the fixes

Fixed in this PR, from the lessons below:

- **Lesson 1 and 5, the rules.** The rules card now says the season runs Thu 3 Dec to Wed 3 Feb, that
  the machine starts empty and day one fills it (A9), and that end stock is valued at cost (A5).
  `goal-and-scoring` says the same at length (end date not yet confirmed; A5 not yet confirmed, so
  buy for the last weeks only what should sell), and `inventory-belief` names day one as the
  exception to "no receipts means unknown".
- **Lesson 2, the run day.** The prompt said "the main run is Thursday, before the midnight cutoff";
  it now gives each store's cutoff (Amazon Wednesday 23:59, in-store Thursday 23:59). The run day
  itself isn't decided (`docs/agent.md`), so the simulator gained `--run-day wed|thu` and these
  seasons ran on Wednesdays at 19:00.
- **Lesson 7, the key.** `model_client()` now pins `https://api.anthropic.com` unless
  `VENDING_ANTHROPIC_BASE_URL` says otherwise, as the week replayer already did, so
  `ANTHROPIC_BASE_URL` can't send the team key elsewhere. Tested.

Not changed: the floor (lesson 3) is a course rule, the sourcing table (lesson 4) needs real prices,
and one pack per slot (lesson 6) waits on David.

| Season | Profit after model spend | Units sold | Units lost to empty slots | Model spend | Slots that sold | Stock left, at cost |
|---|---|---|---|---|---|---|
| Opus 5.5, medium, as built (Thursday runs) | $317.12 | 170 | 11 | $2.84 | 12 | $14.64 |
| **Opus 5.5, medium, after the fixes (Wednesday runs)** | **$385.97** | **302** | 33 | $3.24 | 17 | $112.35 |
| Baseline rule, Thursday runs | $464.62 | 324 | 16 | $0.00 | 20 | $487.90 |
| Baseline rule, Wednesday runs | $443.09 | 309 | 21 | $0.00 | 20 | $412.60 |

![Profit by week: round 1 and round 2](2026-11-16-weekly-profit-after-fixes.svg)

- **The fixes were worth $69 and 132 more units.** The agent filled the machine on day one ("the
  machine opens empty on Thu 3 Dec"), stocked all five drinks (the three sodas at prices above their
  reference, which it accepted this time), sold from 17 slots instead of 12, and kept restocking to
  the last visit, loading the whole closet on Fri 29 Jan "so stock sells instead of being left". On
  Wed 3 Feb it skipped correctly: no visit was left.
- **It ran its first price test**: Red Bull from $4.00 to $4.25 at the 1 Jan visit, one product at a
  time, as `pricing` asks.
- **It still trails the rule**: by $57 on the same Wednesday schedule, $79 against the Thursday one. Its weak spot moved from idle slots to empty ones: 7 slots
  were empty by Wed 13 Jan, and 25 units were lost in weeks 7 and 8 (the rule lost 10). It skipped
  Wed 16 Dec on "more than a week of stock" and refilled only after slots had emptied. A rule in
  `restocking` for when to top up (days of stock left versus days to the next visit) is the next fix
  to try.
- **Wednesday didn't help the rule.** It cost the baseline $22, because a Wednesday schedule's first
  weekly run is Wed 9 Dec, so it loses the Thu 3 Dec run that the Thursday schedule had (which the
  budget-capped day-one fill needed). After week 3 the two schedules are within a few dollars a
  week. If the team moves to Wednesday, keep a run on go-live day.
- Still one seed each. The agent's $69 gain is bigger than any seed effect seen so far, but the
  model-vs-rule gap still rests on the facing assumption (the sensitivity table below).

## What went wrong, and what to change

These are round 1's lessons for the prompt, rules and tools, most important first. Each is something
the real competition would hit too. Lessons 1, 2, 5 and 7 are addressed in round 2, above.

1. **The agent skipped the day-one fill, twice.** On Mon 30 Nov (in this season and in the pilot) it
   saw no receipts and no counts, read that as "contents unknown", and decided to wait for Thursday's
   sales. The machine was in fact empty (staff empty it before 3 Dec). So week 1 sold nothing and
   its first stock arrived Mon 7 Dec. *Fix:* say in the rules card that the machine starts empty and
   the day-one order fills it, and run the day-one plan as its own brief.
2. **The Thursday run misses the Amazon cutoff every week.** Every product in the sourcing table is
   bought on Amazon, and the planner warns after Wednesday 23:59. A Thursday order lands on Saturday,
   so in the simulation each restock was loaded on Monday, not Friday. The agent noticed this itself
   ("Amazon is past its cutoff"). *Fix:* run on Wednesday, or source in-store items for Friday.
3. **The floor priced the sodas out.** The floor comes from the *most* the worker may pay (list price
   x 1.15), so a 20 oz Coke's floor is $2.90 against a $2.00 reference price. The pricing rule then
   says it will sell slowly, and the agent dropped all three sodas. *Fix:* decide whether the floor
   should use the expected price, and give the agent a rule for when a drink above its reference
   price is still worth a slot.
4. **Only five drinks can be bought.** The sourcing table has five rows. The four other catalog
   drinks have no sourcing, and the agent never invented an Amazon URL for them (good), so it could
   not use them. *Fix:* fill the sourcing table before 3 Dec.
5. **It thought the season ended on 31 Jan, and that left-over stock is a loss.** It counted nine weeks
   from the ledger opening (30 Nov), ran stock down from mid-January, and on Mon 1 Feb skipped a
   restock "because the run has ended". The team values end stock at cost (A5), so stock left at the
   end is not lost. *Fix:* put the end date (Wed 3 Feb, or whatever David confirms) and A5 in the rules
   card.
6. **The planner buys at least one pack per slot.** Two slots of one drink buy two packs. A 20-slot
   day-one fill of the five sourced drinks would cost about $980 at most, so any fill has to be split
   across visits, using the closet. The agent and the baseline both learned to load from the closet
   later. Worth knowing when reading a day-one plan.
7. **A model key can be sent to the wrong place.** This machine's environment sets
   `ANTHROPIC_BASE_URL` to a local proxy, and `vending.agent.model_client()` doesn't pin the base URL,
   so the first pilot's requests (and the team key in their header) went to that proxy. It answered
   401 and nothing was spent. *Fix:* pin `base_url` in `model_client()`, and have the harness refuse a
   client that doesn't point at `api.anthropic.com`. *Done in round 2:* `model_client()` now pins it.

## The report cards

Each Sat-Fri ledger week is graded on fixed rules (`vending/simulate/report.py` has them, with the
thresholds), so seasons can be compared. Full cards, with each run's reasons, are on each season's
report page and in `data/*/tables.md`.

| Rule | Opus 5.5 | Sonnet 5.5 | Baseline |
|---|---|---|---|
| Stock-outs (share of demand lost to empty slots) | 7 good, 1 watch, 1 bad | 2 good, 4 watch, 3 bad | 6 good, 3 watch |
| Slow sellers (stocked, no sale for 5 class days, not swapped) | 3 good, 6 bad | 6 good, 3 bad | 1 good, 8 bad |
| Gross margin | 8 good, 1 watch | 8 good, 1 watch | 7 good, 1 watch, 1 bad |
| Spend vs plan | all good | all good | all good |
| Refusals in the preview | 8 good, 2 watch | 8 good, 2 watch | 1 good, 9 watch (by design: it previews twice) |
| Guardrails | all good | all good | all good |

The slow-seller flags are mostly slots that share a drink with several others (six Red Bull slots
splitting one drink's demand). That is partly our demand assumption (below), and partly a real
point: the slow-seller rule doesn't know about duplicates.

## The Opus season, week by week

Weeks are the ledger's Sat-Fri weeks, so W1 is the setup days plus Thu 3 and Fri 4 Dec, and W10 ends
on Wed 3 Feb. Profit is revenue ex tax, less cost of sales, shrinkage, midweek fees and model spend.

| Week | Class days | Units | Lost | Revenue ex tax | Gross profit | Model | Profit | Season to date | Spendable |
|---|---|---|---|---|---|---|---|---|---|
| W1 (from 28 Nov) | 2 | 0 | 0 | $0.00 | $0.00 | $0.65 | -$0.65 | -$0.65 | $600.00 |
| W2 (5 Dec) | 5 | 21 | 0 | $72.69 | $39.18 | $0.36 | $38.83 | $38.17 | $451.33 |
| W3 (12 Dec) | 5 | 24 | 0 | $81.48 | $42.13 | $0.28 | $41.85 | $80.03 | $532.81 |
| W4 (19 Dec) | 1 | 4 | 0 | $12.96 | $5.94 | $0.25 | $5.69 | $85.72 | $501.95 |
| W5 (26 Dec) | 0 | 0 | 0 | $0.00 | $0.00 | $0.09 | -$0.09 | $85.63 | $501.95 |
| W6 (2 Jan) | 5 | 35 | 0 | $124.07 | $69.60 | $0.27 | $69.33 | $154.96 | $626.02 |
| W7 (9 Jan) | 5 | 24 | 0 | $83.80 | $45.45 | $0.37 | $45.08 | $200.04 | $709.82 |
| W8 (16 Jan) | 5 | 29 | 1 | $100.93 | $54.42 | $0.28 | $54.14 | $254.18 | $790.04 |
| W9 (23 Jan) | 5 | 21 | 3 | $73.15 | $39.91 | $0.20 | $39.71 | $293.89 | $863.19 |
| W10 (30 Jan) | 3 | 12 | 6 | $42.13 | $23.32 | $0.09 | $23.23 | $317.12 | $905.32 |

What it decided, in its own words (from `runs.json`):

| Run | Decision | Its reason |
|---|---|---|
| Mon 30 Nov (day one) | skip | "Friday's restock can still be ordered at Thursday's main run, so I'm waiting" |
| Thu 3 Dec | submit | "Our slots have sold nothing since we opened ... so I ordered energy drinks for 8 slots" |
| Thu 10 Dec | submit, $0 | "Loads all the Celsius and some Red Bull from the closet ... the sodas would earn only $0.07 to $0.47 a bottle and Amazon is past its cutoff" |
| Thu 17 Dec | submit | "Celsius sold about 2.4 cans a day across its 5 slots ... I ordered two 12-packs ($49 max)" |
| Thu 24 Dec | submit, $0 | "A free Friday top-up from the closet fills seven slots at no cost and counts toward qualifying" |
| Thu 31 Dec | skip | "Nobody bought anything ... buying or changing anything now would only tie up cash" |
| Thu 7 Jan | submit, $0 | "Moves our last 2 closet units into the 2 fastest-selling slots and buys nothing" |
| Thu 14 Jan | submit | "Slot 17 sold nothing for 5 class days while it held 10 Red Bull, so the slow-seller rule says swap it" |
| Thu 21 Jan | submit, $0 | "Buying more stock now would likely leave unsold leftovers when run week 9 ends" |
| Thu 28 Jan | skip | "The quarter ends around 31 Jan, so anything bought now ... would cost more than it brings in" |
| Mon 1 Feb (check-in) | skip | "The run ended with week 9 (it opened 30 Nov 2026)" |

![The dashboard as it stood after the Thu 14 Jan run](images/dashboard-thu-14-jan.png)

## How it was simulated

**What is real.** `vending.agent.run(live=True)` with the agent's default model and effort, its rules
card, its notes carried from run to run, and up to 15 tool rounds. The MCP server's real tools,
including `preview_desired_state` and `submit_desired_state`, so every plan went through the real
planner and every submit through the real write gate. Polls are real EVA-DTS documents stored by
`sync_history.store`. Receipts are read back by `sync_requests.sync`. Stock is `vending.stock`, money
is `vending.ledger`. Every model call went through `vending.harness`.

**What differs from the live run, and why.**

| | Live | Simulation | Why |
|---|---|---|---|
| Bridge and worker | The DEX bridge | `vending/simulate/bridge.py` | Never touch the real bridge. It refuses what the real one refuses (422 line checks, 429 when one is open) |
| Clock | Real time | Simulated time for the tools, planner, ledger and harness (`clock.py`) | A season in an hour |
| Model spend | `live`, in the ledger | `test`, outside the ledger | So the scratch ledger never mixes with real spend. The tables above add it back |
| MCP server | A subprocess over stdio | The same server in-process, over MCP's in-memory transport | So the simulated clock reaches the tools |
| Schedule | Undecided (#107) | Day one Mon 30 Nov 10:00; Thursdays 19:00; a Monday 07:30 check-in when 4+ slots are empty by our records | The restock policy's bar for a midweek visit (rule 2) |

Tripwires make `Bridge()`, its HTTP calls and `urllib` raise inside the simulation, so nothing could
reach the real bridge. Every season ran on a scratch Postgres in Docker on 127.0.0.1, and
`season.check_scratch` refuses any other host.

**The world, from the repo's own facts.**

- *Calendar:* `analysis/golisano_calendar_2026_27.json`: 36 class days, the Winter Break (22 Dec to
  1 Jan), and the closed days 25 and 28 Dec and 1 Jan.
- *Slots and capacities:* our 20 slots (`vending.slots`), columns from `vending/capacity.py`.
- *Products:* the five drinks in `db/sourcing/2026-11-11-amazon.csv`, at its prices, plus four
  drinks from the capital plan's 11 Nov list that are in the catalog with no sourcing row (fixture
  SKUs in `world.py`).
- *Demand:* each drink sells at the fall rate of the slots it sold from (`analysis/REPORT.md`, "Our
  slots" and "By product line"), per class day, at its reference price
  (`docs/agent/price-history.md`). Days follow the fall weekday index; 66% of a day's sales come 8 to
  11 AM. Weekends and no-class days sell 5% of a class day; closed days nothing. Draws are Poisson,
  seeded.
- *Fulfilment:* the worker buys the primary if the shop has it at or under its max, else the
  alternate, else nothing. Shops are out 3% of the time, pack prices drift about 3% a week, and
  0.5% of units arrive broken. It loads what the directive says, pulls an old drink to the closet
  only when a new one goes in, and sets the tag from the line. Amazon takes two days, so a request
  is loaded at the first visit after its goods land. Closed Fridays move to the next open weekday.
- *The team:* re-checks the sourcing table's prices each Wednesday.

**Two assumptions with no good basis in the repo.** We tested how much they matter by rerunning the
baseline rule (free) with other values.

| Price elasticity | Extra demand from a second slot of the same drink | Baseline profit |
|---|---|---|
| **0.46** (used) | **square root** (used) | **$464.62** |
| 0 | square root | $465.20 |
| 1.5 | square root | $461.21 |
| 0.46 | none (two slots, same demand) | $185.87 |
| 0.46 | full (each slot sells on its own) | $1,423.57 |
| 1.5 | full | $1,416.49 |

- *Price elasticity 0.46* comes from comparing the fall's mid and fast segments (ln(1.354/1.701) /
  ln(4.326/2.626)): different products, so weak. It barely moves the baseline, which prices at the
  reference, but it is why a high price loses few sales here. Don't read the agent's prices as tested.
- *Facings:* a drink in k slots sells k^0.5 times one slot's rate, shared between them. The repo has
  no estimate of substitution (`docs/inventory-demand.md`, assumptions). The restock policy's "two
  columns for one product" treats a second column as capacity, not demand (none); the fall's
  "restocked scenario" adds slots up as if independent (full). **This one decides the comparison:**
  with no extra demand, a lean machine like the agent's loses little; with full, the empty slots
  cost about $1,000. Measure it early in the real run.

## Model spend

| | Runs | Spend |
|---|---|---|
| Pilot (day one and week 1, before the Amazon rule; superseded) | 2 | $0.45 |
| Opus 5.5 season | 11 | $2.84 |
| Sonnet 5.5 season | 10 | $1.03 |
| Opus 5.5 season after the fixes (round 2) | 10 | $3.24 |
| Baseline and sensitivity seasons | 70 | $0.00 |
| **Total** | | **$7.56** of the $10 allowed |

All calls were `test` spend through the harness, under `model_test_daily_cap_cents` and a run cap
set so the total could not pass $9.50. The pilot found a run costs about $0.30, so the run cap was
$2.00 for Opus and $1.00 for Sonnet; no run came close. Round 2's Opus season had a $4.50 budget
(so the total stayed under $9.50); its dearest run cost $0.53.

## See it in the dashboard

`/simulations` on the dashboard (the Container App, or `python -m vending.dashboard` locally) lists
every archived season: profit, units, spend, the chart and the report cards. Open a season to see the
agent dashboard exactly as it would have looked at any of about 180 moments (each run, each visit,
and morning, midday and evening of each day), with a time bar to step by moment, day or week. The P&L
page is beside it, at the same moment. `/simulations/ID/weeks.json` has the weekly data for a script
or an agent.

![The P&L at the end of the season](images/pnl-end-of-season.png)

These pages are built from files in `docs/simulations/data/`, never from the team's database. To
browse a season at *any* minute, not just the archived moments, point the time-travel viewer at its
scratch database (below).

## Run another season

```
docker run -d --name av-sim --label agentic-vending.scratch=true -e POSTGRES_PASSWORD=sim \
    -e POSTGRES_DB=sim -p 127.0.0.1:55999:5432 postgres:17
set DATABASE_URL to postgresql://postgres:sim@127.0.0.1:55999/sim, then: python -m vending.db migrate

# free: the scripted rule
python -m vending.simulate --database-url postgresql://postgres:sim@127.0.0.1:55999/sim \
    --decider scripted --check-ins --out sim-out          # --run-day wed for Wednesday runs
# the model: unset ANTHROPIC_BASE_URL first (lesson 7). Load the key as scripts/run_bench.ps1 does.
python -m vending.simulate --database-url ... --decider model [--model claude-sonnet-5-5] \
    --budget 3.00 --check-ins --out sim-out          # --stop-at 2026-12-05 for a pilot, then --resume

python -m vending.simulate.report  --database-url ... --out sim-out
python -m vending.simulate.archive --database-url ... --data docs/simulations/data/NEW-ID \
    --name "What it was" --strategy "what was different"     # after copying sim-out's files there
python -m vending.simulate.viewer  --sim name=postgresql://...      # any minute, from the database
```

`--elasticity` and `--facing-exponent` change the demand assumptions; `--seed` changes every draw.
A season with the scripted rule takes about 4 minutes; with a model, about 30.

## Data

Each season's directory in `data/` has:

| File | What |
|---|---|
| `season.json` | Season totals and settings |
| `weeks.json` | Every week: P&L, balance, spendable, model spend, sales and losses per slot, runs, visits, report card |
| `runs.json` | Every run: status, cost, tokens, tool calls, previews and their refusals, the plan, the reasons, the update, the notes, the model's reasoning summaries |
| `requests.json` | Every purchase request, its body and every receipt line |
| `model_calls.csv` | Every model call: tokens, cost, stop reason |
| `daily_slots.csv` | Each slot each day: drink, price, sold, unmet demand, hours empty |
| `events.json` | Every event: runs, filed requests, visits, sourcing checks, check-ins |
| `tables.md` | The tables and report cards in this page's format |
| `archive.jsonl.xz` | The dashboard and P&L pages at every archived moment |

No key, connection string or host name is in any of them.

## What is missing

- One seed per season. The differences between the two models are within what another seed could
  change; run more seeds before choosing a model on this.
- No price tests: none of the models changed a price after setting it.
- Product requests to the instructors are filed but never approved in the simulation.
- `action_log` and `agent_steps` rows are stamped at simulated times only after the event that wrote
  them; within one run they share a time.
