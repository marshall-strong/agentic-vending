# Day-one plan model

These scripts project and pick the day-one fill: which drink goes in each of our 20 slots, at what price, with what it costs and what it may earn. They are planning aids. The `vending` package does not import them, and the agent does not use them. They used to live in a personal folder. They moved here with #231 so the team can see and change the model behind each plan round (#230).

Round 7 is the current round, and **plan 7b is the picked plan** in the team database. Plan 7b is round 7 re-run at the $400 cap. Read [`round7/compare7.md`](round7/compare7.md) first: it compares plan 7 with plan 6, then plan 7 with plan 7b ("7 vs 7b").

## Files

| File | What it is |
|---|---|
| `project.py` | The model. It projects a plan for 3 Dec to 29 Jan under three scenarios (low, base, high) and checks every floor, price and capacity with `vending.pricing` and `vending.capacity`. |
| `optimize.py` | The first optimiser. Round 7 imports `best_price` from it. Its `main` reads our slots from a plan file (`--slots-from`). |
| `round6/plan6.json`, `round6/projection6.json` | Plan 6 (picked 21 Nov) and its projection under the old duplicate rule. Round 7 reads our slots and the allowed wildcard spots from plan 6, and `compare7.py` compares against it. |
| `round7/build_catalog7.py` | Builds `catalog7.json` from a team DB snapshot (see "Data"). |
| `round7/catalog7.json` | Round 7's priced catalog: 83 live products, 57 of them plannable. |
| `round7/optimize7.py` | Round 7's product mix: the optimiser plus the test slot and the brand lean. It writes `opt7.json` (plan 7, `--cap-cents 36600`) or `opt7b.json` (plan 7b, the default $400 cap). |
| `round7/place7.py` | Puts the mix on our slots by fall position data. It writes `plan7.json` or `plan7b.json`, using the one-line reasons in `why7.json` or `why7b.json`, and projects the plan at the cap its `opt` file records. |
| `round7/compare7.py` | Prints the tables in `compare7.md`'s plan 6 sections. It also writes `projection6_rescored.json`, which is not committed. |
| `round7/compare7b.py` | Prints the tables in `compare7.md`'s "7 vs 7b" section. The prose around them was written by hand. |
| `round7/plan7.json`, `round7/projection7.json` | Plan 7, at the $366 cap. |
| `round7/plan7b.json`, `round7/projection7b.json` | Plan 7b, at the $400 cap: the picked plan. |
| `round7/compare7.md` | Round 7's write-up. |

## What the model assumes

Every rate is an assumption, and `project.py` writes each one into the projection's `assumptions` field.

- **Demand.** Each product sells `TIER_RATE[category]` units per class day. The rates are our slots' in-stock rates in the fall (`analysis/REPORT.md`, "Our slots"). The rate is scaled by price against the category's reference price: elasticity -2 above the reference and -0.5 below, clamped to 0.1 to 1.6 times.
- **Duplicates add capacity only, never demand** (round 7; Kevin, 22 Nov, #230).
  - A product's demand is modelled once, however many slots hold it.
  - Its slots pool their stock. The product sells min(demand, stock), and its sales are split across its slots by capacity.
  - So a second slot adds sales only when the product's demand between Friday refills is more than one slot holds.
  - Rounds 2 to 6 credited a second slot with `DUP_DECAY = 0.75` of the first slot's sales. That credit was 24% of plan 6's projected profit.
- **Category limits.**
  - History shows at most 9 energy products (`CAT_FREE`) and 4 of any other category (`CAT_FREE_DEFAULT`).
  - Each product past the limit sells `CAT_DECAY` (0.6) times less per extra product, because flavours cannibalise each other.
  - Since round 7 the limit counts distinct products, not slots.
- **The Interloper.** Each scenario cuts every product's demand by an Interloper haircut (30%, 15% or 5%), and cuts the products it also sells by the same again. That is all the model uses it for. Its prices are never an input (`docs/agent/interloper.md`).
- **Calendar.** Non-class days sell nothing. The class days come from `analysis/golisano_calendar_2026_27.json`.
- **Refills.**
  - The machine is loaded to capacity before Thu 3 Dec.
  - Every Friday from 4 Dec it is refilled to capacity at about 3 PM, and half of Friday's demand comes before the visit.
  - There are no midweek visits.
- **Profit.**
  - Profit is units sold x (price / 1.08 - expected unit cost), minus $1 a week of model spend.
  - Unsold stock counts at cost (A5, confirmed), so it is neither profit nor loss.
- **Scenarios.** Demand is x0.6 (low), x1.0 (base) or x1.25 (high), with the haircuts above.
- **The $400 cap.**
  - The optimisers keep the fill's most-we-pay cost at or under $400.00 for all 20 slots (`project.FILL_CAP`).
  - Kevin raised it on 22 Nov from the $366.00 that #113 proposed (`docs/opening-capital-plan.md`).
  - The cap shapes the mix. Plan 7 used $366, and its fill comes to exactly $366.00. Plan 7b uses $400, and its fill comes to $399.00 most and $343.50 expected.
  - `project.py --cap` and `optimize7.py --cap-cents` take another figure, for example to re-run plan 7.
  - The dashboard's reference line (`vending.plan_rounds.FILL_CAP_CENTS`) is changed in a separate PR.

## Run a round

Run everything from the repo root, with the repo's Python environment. The scripts find the repo, the calendar and round 6 by themselves. Plan 7b again:

```
python analysis/dayone/round7/build_catalog7.py      # needs the team snapshot (Data, below)
python analysis/dayone/round7/optimize7.py analysis/dayone/round7/catalog7.json analysis/dayone/round7/opt7b.json
python analysis/dayone/round7/place7.py analysis/dayone/round7/catalog7.json analysis/dayone/round7/opt7b.json \
    analysis/dayone/round7/why7b.json analysis/dayone/round7/plan7b.json
python analysis/dayone/project.py analysis/dayone/round7/plan7b.json analysis/dayone/round7/catalog7.json \
    analysis/dayone/round7/projection7b.json
python analysis/dayone/round7/compare7.py            # prints the tables for compare7.md's plan 6 sections
python analysis/dayone/round7/compare7b.py           # prints the tables for its "7 vs 7b" section
```

For plan 7, use `opt7.json`, `why7.json` and `plan7.json`, with `--cap-cents 36600` for `optimize7.py` and `--cap 366` for `project.py`.

- `place7.py` reads the fall export (`analysis/data/fall40/`, or the folder in `FALL40_DIR`).
- To score a plan you edited by hand, run only `project.py`.
- For a new round, copy `round7/` to `round8/`, rename the files to match, and change what the round changes. Keep a plan and its projection on the same suffix (`plan8.json` with `projection8.json`), because the round reader pairs them by that suffix.

## Load a round for the dashboard

```
python -m vending.plan_store load analysis/dayone --round 7b --by NAME
python -m vending.plan_store list
python -m vending.plan_store pick ID --by NAME
```

- `load` finds `roundN/planN.json` and `projectionN.json` under the folder (`vending.plan_rounds.discover`). A letter after the number is part of the round's ID: `plan7b.json` is round `7b`.
- It checks the round against our current assignment from the inventory server (`vending.slots.our_slots`) and refuses a plan that names any other selection.
- It recomputes every floor, price, capacity and the fill cost itself. It reads the projection's demand figures as they are.
- It stores both files in `first_fill_plans`. `pick` marks the plan the dashboard shows.
- It needs `DATABASE_URL` for the team database.

## Data

Only the files a round needs, and only small ones, are committed.

- **Committed.**
  - `catalog7.json`: the round's input, as built.
  - `opt7.json`, `opt7b.json`, `why7.json` and `why7b.json`: what `place7.py` reads.
  - Plans 6, 7 and 7b with their projections, and `compare7.md`.
- **Not committed: the fall export.**
  - `place7.py` reads `sales_events.csv` from the fall export. That export holds per-selection sales for all 40 selections, from polls before the fall cut only, which `docs/integrity.md` allows.
  - `analysis/` doesn't commit per-selection fall figures, so make the export locally: `python analysis/export_sales.py --fall-all analysis/data/fall40`. `data/` is git-ignored.
- **Not committed: the team DB snapshot** (`round7/team_catalog_2026-11-20.json`, git-ignored).
  - It is a read-only snapshot of the team DB on 20 Nov, about 22:10 ET. It is one JSON object with these keys:
    - `about`;
    - `last_catalog_sync`;
    - `catalog`: live `catalog_products` rows, with `sku`, `name`, `line`, `flavor`, `size` and `first_seen`;
    - `requests`: `product_requests`, with `id`, `name`, `size`, `status`, `sku` and `note`;
    - `sourcing`: `sourcing_options`, with `sku`, `rank`, `store`, `units_per_pack`, `expected_cents`, `max_cents`, `checked_at` and `note`;
    - `capacity_overrides`: the config value, SKU to units.
  - Make a new one with a read-only query of those tables for the next round. Don't commit it.
- **Never here.** Anything from 3 Dec on, and any of the other team's data.
