# Round 7: plan 6, plan 7 and plan 7b

Round 7 runs round 6's pipeline with Kevin's 22 Nov rules (#230):

- a second slot of the same drink adds capacity, never demand;
- no duplicates on day one;
- one test slot, which rotates new drinks, besides the wildcard;
- lean toward a new category or brand.

Plan 7b is round 7 re-run at the $400 most-we-pay cap (Kevin, 22 Nov). **Plan 7b is the picked plan** in the team database.

This page keeps the totals and the method only. The plans themselves map our slots to products. The repo is public and our assignment is private (docs/integrity.md), so the plans and the per-slot tables live outside git: in the team database (`first_fill_plans`) and in each person's local folder (`analysis/dayone/README.md`).

Figures are from the #231 review's version of `project.py`. It adds a product's sales before rounding, so a few totals moved by a cent or two from the first run. For example, plan 7b's base profit went from $1,088.66 to $1,088.65.

## In short

- **Plans 7 and 7b each have 20 different drinks. Plan 6 had 14**, with six drinks doubled.
- **24% of plan 6's projected profit was the old duplicate credit** (`DUP_DECAY = 0.75`): $274.50 of $1,137.43 in the base case. That was demand a second slot could not create. Re-scored under the new rule, plan 6's base profit is $862.93.
- **Plan 7 ($366 cap) projects $1,018.29 in the base case. Plan 7b ($400 cap) projects $1,088.65.**
- **No drink sells out between Friday refills in the base case**, in any of the three plans, so a duplicate would add nothing. The busiest drinks sell about 5.7 units per refill cycle against 8 to 11 units of capacity.
- **The cap shapes the mix.** Plan 7's fill comes to exactly $366.00. Plan 7b's comes to $399.00, $1.00 under its cap.

## Totals

| | Plan 6, old rule | Plan 6, re-scored | Plan 7 | Plan 7b |
|---|---|---|---|---|
| Cap, most we pay | $366 | $366 | $366 | $400 |
| Different drinks | 14 | 14 | 20 | 20 |
| Brands | | | 9 | 10 |
| Fill cost, expected | $308.09 | $308.09 | $315.16 | $343.50 |
| Fill cost, most we pay | $357.50 | $357.50 | $366.00 | $399.00 |
| Spendable after the fill, worst case ($600 less most we pay; floor $53) | $242.50 | $242.50 | $234.00 | $201.00 |
| Projected profit, low | $557.47 | $421.84 | $498.60 | $533.37 |
| Projected profit, base | $1,137.43 | $862.93 | $1,018.29 | $1,088.65 |
| Projected profit, high | $1,592.64 | $1,209.13 | $1,426.18 | $1,524.47 |
| Units lost to empty slots, any scenario | 0.0 | 0.0 | 0.0 | 0.0 |
| Closet stock after the fill (units, at cost) | 56 | 56 | 90 | 94 |

"Plan 6, old rule" is plan 6's own projection from round 6. The other columns are scored by round 7's `project.py`.

## How much of plan 6's profit was the duplicate credit

| Scenario | Plan 6, old rule | Plan 6, re-scored | Credit | Share |
|---|---|---|---|---|
| Low | $557.47 | $421.84 | $135.63 | 24% |
| Base | $1,137.43 | $862.93 | $274.50 | 24% |
| High | $1,592.64 | $1,209.13 | $383.51 | 24% |

## Plan 7b against plan 7

- **The extra $34 of cap is worth it.** Profit is up $34.77 (low), $70.36 (base) and $98.29 (high). The fill costs $28.34 more expected and $33.00 more at most. Leftover stock counts at cost (A5), so no extra money is at risk. Spendable after the fill is still $201 in the worst case, against the $53 floor.
- **What the money bought.**
  - Out: Hint Grape, Snapple Peach Tea and bubly Cherry.
  - In: C4 and two Snapple juices (Kiwi Strawberry and Fruit Punch).
  - No soda or protein drink got in: under the model they still earn less per dollar of pack.
- **Brands and categories.**
  - Brands go from 9 to 10. Energy reaches its limit of 9 drinks (`CAT_FREE`).
  - Snapple goes from 2 to 3 flavours. That runs against the brand lean, but no new-brand swap came within its $5 tolerance.
  - Categories, plan 7 → plan 7b: energy 8 → 9, juice 1 → 3, sparkling 4 → 3, sports 2 → 2, tea 3 → 2, water 2 → 1.
- **The cap is nearly full.** A price rise of more than $1 at the next price check breaks the cap. Re-run 7b if that happens.

| Brand | Plan 7 | Plan 7b |
|---|---|---|
| Celsius | 4 | 4 |
| bubly | 4 | 3 |
| Red Bull | 3 | 3 |
| Snapple | 2 | 3 |
| Arizona | 2 | 2 |
| Hint | 2 | 1 |
| Gatorade | 1 | 1 |
| Powerade | 1 | 1 |
| Rockstar | 1 | 1 |
| C4 | 0 | 1 |

## Checked-price sourcing rows

14 of plan 7b's 20 drinks have only a price estimate, with no checked-price sourcing row. The fill can't buy them until one is added (`python -m vending.sourcing set`):

- AriZona Green Tea with Ginseng and Honey 16 oz can
- bubly Blackberry, Grapefruit and Strawberry 12 oz cans
- C4 Performance Energy Cosmic Rainbow 16 oz can
- Celsius Sparkling Arctic Vibe, Kiwi Guava and Tropical Vibe 12 oz cans
- Gatorade Thirst Quencher 20 oz bottle
- Powerade Sports Drink 20 oz bottle
- Red Bull Yellow Edition Tropical 8.4 oz can
- Rockstar Energy 16 oz can
- Snapple Apple and Kiwi Strawberry 16 oz bottles

A projection's `needs_new_source` lists exactly these, from the catalog's `checked_source` flag. Before the #231 review it listed none, because it confused "has a price" with "has a checked source".

## Method

- **Catalog.** `build_catalog7.py` prices the team catalog of 20 Nov: 83 live products, 57 plannable. It gives each product three separate flags:
  - `priced`: there is a price estimate;
  - `checked_source`: there is a sourcing row;
  - `plannable`: priced, with a capacity figure, and not left out on purpose.
- **The mix.** `optimize7.py` searches for the best mix of different drinks under the cap:
  - It prices each drink at the 5-cent step that maximises margin x demand.
  - It fills the slots greedily, with a look-ahead so an early pick never leaves the cap unable to buy the rest, then swaps single slots until nothing gains.
  - A mix that repeats a drink scores as infeasible. If no mix of different drinks fits, it says so.
  - The search is local, so it also starts from earlier plans (`--seed`). Plan 7b was seeded with plans 7 and 6, and the #231 version picks the same 7b.
  - Plan 7 came from the earlier search, which could repeat a drink while it searched, and stalled. Re-run at $366 with the #231 version, the search finds a different mix worth $12.82 more base gross profit ($1,040.13 against $1,027.31). Plan 7 is superseded by 7b, so it isn't redone here.
- **The test slot.** For each category, the optimiser fills the other slots without that category, then puts the category's best drink in the test slot. If that drink leaves no room for the other slots under the cap, it tries the category's next drink. Juice won for plan 7 and water for plan 7b. A run without the test-slot rule scores no higher for either.
- **The brand lean.** A slot holding a second product of a brand we already stock is swapped for a new brand when the swap costs at most $5 of base gross profit over the run. No swap passed for plan 7 or plan 7b. The closest for plan 7 were C4 for a Red Bull ($7.02) and Pepsi mini cans for a tea ($8.60).
- **Placement.** `place7.py` orders our slots by shelf, then by fall sales.
  - Before counting any fall row, it checks every row against the fall cut and stops at one from the cut on.
  - The best-earning drinks get the best positions.
  - The test slot never lands on a spot the wildcard may use.
- **Category limits.** History shows at most 9 energy drinks and 4 of any other category. Past the limit, the products with the least demand before the penalty lose 40% per place, whatever the plan's order. No category is past its limit in plans 7 and 7b.
