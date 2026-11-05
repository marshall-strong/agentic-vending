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
   `get_sales_history`. It notes when the price changed during the week, and when the polls cover
   less than 6 days of it. **Product, stock and unit cost are unknown** until restock receipts are
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
| valid plan | The answer isn't a plan: a slot missing, twice or not ours, a price that isn't exact text such as `"2.50"`, a negative or non-whole `fill_to`, a slot with no reason, an unknown visit type. Every reason is listed at once. | Never |
| catalog | A named product isn't in the active catalog (a pending request), or its catalog request was refused | The catalog is unknown and the plan names a product |
| prices | A price isn't in 5-cent steps, is above the ceiling, or is below the floor, all through `vending/pricing.py` | Never; the floor is skipped per slot when its unit cost is unknown, and the detail says how many |
| balance | The plan's purchases, plus $25 for a midweek visit, cost more than the balance | The balance or a unit cost is unknown |
| no writes | A write tool call was logged in `action_log` while the decider ran | The decider has no database access (the cases) |

**The price ceiling is $6.00, a placeholder:** it's the team's proposal, not a decision. It lives
in `PRICE_CEILING_CENTS` in `vending/replay/scoring.py`. The balance check uses actual unit cost;
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
score has to assume what demand would have been. The issue asks for these to go in the
assumptions register; until there is one, they live here and print under every score table.

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
no change.

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

## Not built yet

- **Model deciders.** A later PR adds them with a dollar cap of $1 or less per run, a low daily cap
  on all test spending (under $5 a day; the exact number is a placeholder for the team), and the
  cost logged for every run. The default model is pending a group decision.
- **The restock planner as a check**, once PR #13 merges.
- **Stock, product and unit cost from receipts**, once PR #25 merges. Until then, replayed weeks
  can't score stockouts from stock, and margin needs `--unit-cost`.

## Tests

```
pytest tests/test_replay.py
```

The replay and CLI tests store fixture polls in a throwaway schema through `store_poll`, the
history sync's own `store`, so they run against a real database. They skip when it's down, like
the rest. The plan-check, scoring and case tests need no database.
