# Day-one plan model

These scripts project and pick the day-one fill: which drink goes in each of our 20 slots, at what price, with what it costs and what it may earn. They are planning aids. The `vending` package does not import them, and the agent does not use them. They moved here from a personal folder with #231 so the team can see and change the model behind each plan round (#230).

Round 7 is the current round, and **plan 7b is the picked plan**. Plan 7b is round 7 re-run at the $400 cap. [`round7/compare7.md`](round7/compare7.md) has its totals and the method.

## Where the real files live

**A plan maps our slots to products, and our assignment is private** (David's honor rule of 20 Nov, `docs/integrity.md`). This repo is public, so no plan, projection or placement input is committed. They live in two places:

- **The team database.** `first_fill_plans` holds every loaded plan and its projection (`python -m vending.plan_store list`). The dashboard shows the picked one.
- **Each person's local folder**, `analysis/dayone/local/`, which is git-ignored. Set `DAYONE_LOCAL` to use another folder. The scripts read and write the real files there:

  ```
  local/
    team_catalog_2026-11-20.json   the team DB snapshot that build_catalog7.py reads
    round6/plan6.json              plan 6 and its projection; optimize7 and place7 read our slots and the
    round6/projection6.json        wildcard's allowed spots from plan 6 (--slots-from takes another plan)
    round7/opt7.json, opt7b.json   optimize7's output, place7's input
    round7/why7.json, why7b.json   each drink's one-line reason, place7's input
    round7/plan7.json, plan7b.json and projection7.json, projection7b.json
  ```

To get a round's files, copy them from someone who has them. A plan and its projection can also be read back from `first_fill_plans`. Never add them to git. `tests/test_dayone_model.py` uses made-up products and the tests' made-up assignment instead.

## Files in the repo

| File | What it is |
|---|---|
| `project.py` | The model. It projects a plan for 3 Dec to 29 Jan under three scenarios (low, base, high) and checks every floor, price and capacity with `vending.pricing` and `vending.capacity`. |
| `optimize.py` | The first optimiser. Round 7 imports `best_price` and `candidates` from it. Its `main` reads our slots from a plan file (`--slots-from`). |
| `round7/build_catalog7.py` | Builds `catalog7.json` from the team DB snapshot in the local folder. |
| `round7/catalog7.json` | Round 7's priced catalog: 83 live products, 57 of them plannable. It names no slot. |
| `round7/optimize7.py` | Round 7's product mix: the best mix of different drinks under the cap, with the test slot and the brand lean. |
| `round7/place7.py` | Puts the mix on our slots by fall position data and writes the plan. |
| `round7/compare7.py`, `round7/compare7b.py` | Print the comparison tables, per-slot rows included, from the local folder. Keep that output local. |
| `round7/compare7.md` | Round 7's totals and method, with no slot numbers. |

## What the model assumes

Every rate is an assumption, and `project.py` writes each one into the projection's `assumptions` field.

- **Demand.**
  - Each product sells `TIER_RATE[category]` units per class day. The rates are our slots' in-stock rates in the fall (`analysis/REPORT.md`, "Our slots").
  - The rate is scaled by price against the category's reference price: elasticity -2 above the reference and -0.5 below, clamped to 0.1 to 1.6 times.
- **Duplicates add capacity only, never demand** (round 7; Kevin, 22 Nov, #230).
  - A product's demand is modelled once, however many slots hold it.
  - Its slots pool their stock. The product sells min(demand, stock), and its sales are split across its slots by capacity.
  - So a second slot adds sales only when the product's demand between Friday refills is more than one slot holds.
  - Rounds 2 to 6 credited a second slot with `DUP_DECAY = 0.75` of the first slot's sales. That credit was 24% of plan 6's projected profit.
  - The totals add each product's sales before rounding, so they don't depend on how many slots split a product.
- **No duplicates on day one.** The optimiser only builds mixes of different drinks. If none fits under the cap, it stops and says so.
- **Category limits.**
  - History shows at most 9 energy products (`CAT_FREE`) and 4 of any other category (`CAT_FREE_DEFAULT`).
  - Past the limit, the products with the least demand before the penalty lose `CAT_DECAY` (0.6) per place past it, because flavours cannibalise each other.
  - The ranking doesn't depend on the plan's order (`category_ranks`), so the optimiser, the placement and the projection score one mix the same way.
- **The Interloper.** Each scenario cuts every product's demand by an Interloper haircut (30%, 15% or 5%), and cuts the products it also sells by the same again. That is all the model uses it for. Its prices are never an input (`docs/agent/interloper.md`).
- **Calendar.** Non-class days sell nothing. The class days come from `analysis/golisano_calendar_2026_27.json`.
- **Refills.**
  - The machine is loaded to capacity before Thu 3 Dec.
  - Every Friday from 4 Dec it is refilled to capacity at about 3 PM, and half of Friday's demand comes before the visit.
  - There are no midweek visits.
  - A slot's sellout date is the first day demand finds it empty, including the day its last unit sells.
- **Profit.**
  - Profit is units sold x (price / 1.08 - expected unit cost), minus $1 a week of model spend.
  - Unsold stock counts at cost (A5, confirmed), so it is neither profit nor loss.
- **Scenarios.** Demand is x0.6 (low), x1.0 (base) or x1.25 (high), with the haircuts above.
- **Prices and sources.**
  - The catalog keeps three separate flags per product: `priced` (a price estimate exists), `checked_source` (a checked-price sourcing row exists) and `plannable`.
  - The optimisers only consider plannable, priced products.
  - A projection's `needs_new_source` lists every product without a checked sourcing row. The fill can't buy those until one is added (`python -m vending.sourcing set`).
- **The $400 cap.**
  - The optimisers keep the fill's most-we-pay cost at or under $400.00 for all 20 slots (`project.FILL_CAP`).
  - Kevin raised it on 22 Nov from the $366.00 that #113 proposed (`docs/opening-capital-plan.md`).
  - Plan 7 used $366, and its fill comes to exactly $366.00. Plan 7b uses $400, and its fill comes to $399.00 most and $343.50 expected.
  - `project.py --cap` and `optimize7.py --cap-cents` take another figure.
  - The dashboard's reference line (`vending.plan_rounds.FILL_CAP_CENTS`) is changed in a separate PR.
- **Fall data stops at the cut.**
  - `place7.py` ranks positions by fall sales of all 40 selections, which `docs/integrity.md` allows from polls before the fall cut.
  - It checks every row against the cut (`analysis/slots_config.check_before_start`) before counting any, and stops at one from the cut on.

## Run a round

Run from the repo root with the repo's Python environment. `L` below is the local folder. Plan 7b again:

```
L=analysis/dayone/local
python analysis/dayone/round7/build_catalog7.py      # needs $L/team_catalog_2026-11-20.json
python analysis/dayone/round7/optimize7.py analysis/dayone/round7/catalog7.json $L/round7/opt7b.json \
    --seed $L/round7/plan7.json $L/round6/plan6.json
python analysis/dayone/round7/place7.py analysis/dayone/round7/catalog7.json $L/round7/opt7b.json \
    $L/round7/why7b.json $L/round7/plan7b.json
python analysis/dayone/project.py $L/round7/plan7b.json analysis/dayone/round7/catalog7.json $L/round7/projection7b.json
python analysis/dayone/round7/compare7.py > $L/compare7_tables.md     # per-slot rows: keep them local
python analysis/dayone/round7/compare7b.py > $L/compare7b_tables.md
```

- **Plan 7:** use `opt7.json`, `why7.json` and `plan7.json`, with `--cap-cents 36600` for `optimize7.py` and `--cap 366` for `project.py`. Plan 7 came from the optimiser before the #231 review, seeded with plan 6. The fixed optimiser skips that seed, because plan 6 repeats drinks, and at $366 it finds a different mix worth $12.82 more base gross profit ($1,040.13 against $1,027.31). Plan 7b, the picked plan, comes out the same.
- **The search is local.** `--seed` also starts it from earlier plans; plan 7b was seeded with plans 7 and 6.
- **Fall export.**
  - `place7.py` reads it from `analysis/data/fall40/`, or from the folder in `FALL40_DIR`. It is per-selection data for all 40 selections, from polls before the cut.
  - Make it with `python analysis/export_sales.py --fall-all analysis/data/fall40`. `data/` is git-ignored, and `analysis/` commits no per-selection fall figures.
- **Team DB snapshot.** `team_catalog_2026-11-20.json` is a read-only snapshot of the team DB on 20 Nov, about 22:10 ET. It is one JSON object with these keys:
  - `about`;
  - `last_catalog_sync`;
  - `catalog`: live `catalog_products` rows, with `sku`, `name`, `line`, `flavor`, `size` and `first_seen`;
  - `requests`: `product_requests` rows, with `id`, `name`, `size`, `status`, `sku` and `note`;
  - `sourcing`: `sourcing_options` rows, with `sku`, `rank`, `store`, `units_per_pack`, `expected_cents`, `max_cents`, `checked_at` and `note`;
  - `capacity_overrides`: the config value, SKU to units.

  Make a new one with a read-only query of those tables for the next round.
- **A new round.** Make `round8/` scripts beside `round7/`, and keep the round's files in `$L/round8/`. Keep a plan and its projection on the same suffix (`plan8.json` with `projection8.json`), because the round reader pairs them by that suffix.

## Load a round for the dashboard

```
python -m vending.plan_store load analysis/dayone/local --round 7b --by NAME
python -m vending.plan_store list
python -m vending.plan_store pick ID --by NAME
```

- `load` finds `roundN/planN.json` and `projectionN.json` under the folder (`vending.plan_rounds.discover`). A letter after the number is part of the round's ID: `plan7b.json` is round `7b`.
- It checks the round against our current assignment from the inventory server (`vending.slots.our_slots`), and refuses a plan that names any other selection.
- It recomputes every floor, price, capacity and the fill cost itself. It reads the projection's demand figures as they are.
- It stores both files in `first_fill_plans`. `pick` marks the plan the dashboard shows.
- It needs `DATABASE_URL` for the team database.

## Never here

- Any file that maps our slots to products.
- Anything from 3 Dec on.
- Any of the other team's data.
