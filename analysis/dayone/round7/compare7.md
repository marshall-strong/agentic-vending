# Round 7 against round 6

Round 7 runs round 6's pipeline with Kevin's 22 Nov rules (#230):
- a second slot of the same drink adds capacity, never demand;
- no hard rule against duplicates;
- one test slot, which rotates new drinks, besides the wildcard;
- lean toward a new category or brand.

Inputs are the same as round 6: the same team catalog file (`team_catalog_2026-11-20.json`, 83 live products, 57 with a price and a capacity figure), the same slots (read from `round6/plan6.json`), and the same cash cap, pricing and placement method.

## In short

- **Plan 7 has 20 distinct drinks. Plan 6 had 14**, with Red Bull Original, Sugarfree and Yellow, Snapple Apple, Gatorade and Powerade each doubled.
- **24% of plan 6's projected profit was the DUP_DECAY credit**: $274.50 of $1,137.43 in the base case. That was demand the second slots could not create. Re-scored under the new rule, plan 6's base profit is $862.93.
- **Plan 7 projects $1,018.31 in the base case**, $155.38 more than plan 6 re-scored.
- **No drink sells out between Friday refills in the base case**, in either plan, so a duplicate adds nothing. The busiest drinks sell 5.7 units per refill cycle against 8 to 11 slots of capacity. In the high case, the 8-can energy drinks sell exactly 8 per cycle and run out of nothing.
- **The $366 cap is what shapes the mix.** The fill's most-we-pay cost is exactly $366.00. Cheap bubly 8-packs ($4.27 for 8) and Arizona tea fill the slots that plan 6 gave to second Red Bull, Snapple, Gatorade and Powerade slots.
- **Test slot: 45, Snapple Apple**, our only juice.

## Each slot's drink, and peak sales against capacity (base scenario)

Peak = the most units the slot sells between two Friday refills. Plan 6 is shown under both rules: the old rule gave each duplicate slot its own extra demand; the new rule splits the product's one demand across its slots. No slot in any of the three reaches its capacity in the base case.

| Slot | Plan 6 | Plan 6 peak (old rule) | Plan 6 peak (new rule) | Plan 7 | Plan 7 peak | Capacity (6 / 7) |
|---|---|---|---|---|---|---|
| 12 | Celsius Sparkling Tropical Vibe 12 oz can | 5.7 | 5.7 | Celsius Sparkling Arctic Vibe 12 oz can | 5.7 | 8 / 8 |
| 14 | Red Bull Sugarfree 8.4 oz can | 5.7 | 2.9 | Red Bull Sugarfree 8.4 oz can | 5.7 | 11 / 11 |
| 16 | Celsius Sparkling Arctic Vibe 12 oz can | 5.7 | 5.7 | Celsius Sparkling Tropical Vibe 12 oz can | 5.7 | 8 / 8 |
| 18 | Red Bull Original 8.4 oz can | 5.7 | 2.9 | Red Bull Original 8.4 oz can | 5.7 | 11 / 11 |
| 21 | Red Bull Original 8.4 oz can | 4.3 | 2.9 | Rockstar Energy 16 oz can | 5.7 | 11 / 8 |
| 23 | Celsius Sparkling Orange 12 oz can | 5.7 | 5.7 | Celsius Sparkling Orange 12 oz can | 5.7 | 8 / 8 |
| 25 | Red Bull Yellow Edition Tropical 8.4 oz can | 5.7 | 2.9 | Red Bull Yellow Edition Tropical 8.4 oz can | 5.7 | 11 / 11 |
| 27 | Celsius Sparkling Kiwi Guava 12 oz can | 5.7 | 5.7 | Celsius Sparkling Kiwi Guava 12 oz can | 5.7 | 8 / 8 |
| 32 | Gatorade Thirst Quencher 20 oz bottle | 3.3 | 1.6 | Hint Grape 16 oz bottle | 4.2 | 7 / 7 |
| 34 | Hint Georgia Peach 16 oz bottle | 4.2 | 4.2 | Snapple Peach Tea 16 oz bottle | 3.2 | 7 / 7 |
| 36 | Powerade Sports Drink 20 oz bottle | 3.3 | 1.6 | Gatorade Thirst Quencher 20 oz bottle | 3.3 | 7 / 7 |
| 38 | Red Bull Sugarfree 8.4 oz can | 4.3 | 2.9 | Powerade Sports Drink 20 oz bottle | 3.3 | 11 / 7 |
| 41 | Snapple Apple 16 oz bottle | 4.3 | 2.1 | AriZona Green Tea with Ginseng and Honey 16 oz can | 3.2 | 7 / 8 |
| 43 | Snapple Fruit Punch 16 oz bottle | 3.7 | 3.6 | Arizona Iced Tea with Lemon Flavor 16 oz bottle | 3.2 | 7 / 7 |
| 45 | Snapple Apple 16 oz bottle | 3.2 | 2.1 | Snapple Apple 16 oz bottle **(test slot)** | 4.2 | 7 / 7 |
| 47 | Snapple Kiwi Strawberry 16 oz bottle | 4.3 | 4.2 | Hint Georgia Peach 16 oz bottle | 4.2 | 7 / 7 |
| 52 | Powerade Sports Drink 20 oz bottle | 2.5 | 1.6 | Bubly Sparkling Water Cherry 12 oz can | 3.0 | 7 / 8 |
| 54 | Gatorade Thirst Quencher 20 oz bottle | 2.5 | 1.6 | Bubly Sparkling Water Strawberry 12 oz can | 3.0 | 7 / 8 |
| 56 | Red Bull Yellow Edition Tropical 8.4 oz can | 2.6 | 2.9 | Bubly Sparkling Water Grapefruit 12 oz can | 3.0 | 11 / 8 |
| 58 | Hint Grape 16 oz bottle | 4.2 | 4.2 | Bubly Sparkling Water Blackberry 12 oz can | 3.0 | 7 / 8 |

The busiest refill cycle holds 5.0 class days of demand. In the high scenario the busiest 8-can energy drinks (Celsius, Rockstar) sell exactly their 8 between refills, but none runs out.

## Totals

| | Plan 6, old rule | Plan 6, re-scored | Plan 7 |
|---|---|---|---|
| Distinct drinks | 14 | 14 | 20 |
| Fill cost, expected | $308.09 | $308.09 | $315.16 |
| Fill cost, most we pay (cap $366) | $357.50 | $357.50 | $366.00 |
| Spendable after the fill, worst case ($600 less most we pay; floor $53) | $242.50 | $242.50 | $234.00 |
| Projected profit, low | $557.47 | $421.79 | $498.61 |
| Projected profit, base | $1,137.43 | $862.93 | $1,018.31 |
| Projected profit, high | $1,592.64 | $1,209.10 | $1,426.17 |
| Units lost to empty slots, low | 0.0 | 0.0 | 0.0 |
| Units lost to empty slots, base | 0.0 | 0.0 | 0.0 |
| Units lost to empty slots, high | 0.0 | 0.0 | 0.0 |
| Closet stock after the fill (units) | 56 | 56 | 90 |

## How much of plan 6's profit was the DUP_DECAY credit

| Scenario | Plan 6, old rule | Plan 6, re-scored | Credit | Share |
|---|---|---|---|---|
| low | $557.47 | $421.79 | $135.68 | 24% |
| base | $1,137.43 | $862.93 | $274.50 | 24% |
| high | $1,592.64 | $1,209.10 | $383.54 | 24% |

## Drinks new to plan 7, and whether they have a checked-price sourcing row

| Drink | Slot | Price source in catalog7.json | Needs a sourcing row |
|---|---|---|---|
| Rockstar Energy 16 oz can (`ROCKSTAR-ORIGINAL-16CAN`) | 21 | approved request 31: its 15 Nov note; no sourcing row yet | **yes** |
| Snapple Peach Tea 16 oz bottle (`SNAPPLE-PEACH-16BOT`) | 34 | approved request 18: its 15 Nov note; no sourcing row yet | **yes** |
| AriZona Green Tea with Ginseng and Honey 16 oz can (`ARIZONA-GREEN_TEA-16CAN`) | 41 | approved request 37: its 15 Nov note; no sourcing row yet | **yes** |
| Arizona Iced Tea with Lemon Flavor 16 oz bottle (`ARIZONA-ICED_LEMON-16BOT`) | 43 | sourcing row (Amazon, checked 2026-11-15) | no |
| Bubly Sparkling Water Cherry 12 oz can (`BUBLY-CHERRY-12CAN`) | 52 | 15 Nov Amazon check (round 3); no sourcing row yet | **yes** |
| Bubly Sparkling Water Strawberry 12 oz can (`BUBLY-STRAWBERRY-12CAN`) | 54 | 15 Nov Amazon check (round 3); no sourcing row yet | **yes** |
| Bubly Sparkling Water Grapefruit 12 oz can (`BUBLY-GRAPEFRUIT-12CAN`) | 56 | 15 Nov Amazon check (round 3); no sourcing row yet | **yes** |
| Bubly Sparkling Water Blackberry 12 oz can (`BUBLY-BLACKBERRY-12CAN`) | 58 | 15 Nov Amazon check (round 3); no sourcing row yet | **yes** |

Kept from plan 6 and still without a sourcing row: CELSIUS-ARCTIC_VIBE-12CAN, CELSIUS-SPARKLING_KIWI-12CAN, CELSIUS-TROPICAL_VIBE-12CAN, GATORADE-COOL_BLUE-20BOT, POWERADE-MOUNTAIN_BLUE-20BOT, REDBULL-YE_TROPICAL-8_4CAN, SNAPPLE-APPLE-16BOT.

## Test slot

- **Slot 45 holds Snapple Apple** (role `test` in plan7.json). It is the only juice in the plan.
- **How the category was chosen.** For each category, the optimiser filled 19 regular slots without that category, then put that category's best drink in the test slot. The categories scored as follows (base gross profit):

  | Test category | Base gross profit |
  |---|---|
  | Juice | $1,027.31 |
  | Water | $999.91 |
  | Sparkling | $979.49 |
  | Sports | $974.58 |
  | Tea | $956.82 |
  | Soda cans | $941.35 |
  | Energy | $614.24 |
  | Protein, 20 oz soda | Do not fit under the cap |

- **Why it lands on 45.** Its turn in round 6's placement order would have put it on a wildcard spot, so it moved down to 45, the next spot that is not one. Slot 45 sold well in the fall.
- **The constraint costs nothing here.** The same swap search with the test-slot rule removed starts from plan 7 and makes no change.

## Wildcard

- **No change to how it is chosen:** the agent picks the wildcard. The allowed spots, read from plan 6's check, are 21, 23, 27, 32, 36, 38, 41, 43 and 47.
- **Pulling a slot for it now always loses a drink**, because every slot holds a different one. In plan 6, slots 21 and 38 held second Red Bull slots, so pulling either lost nothing.
- **The cheapest to lose are 43 (Arizona Lemon Tea, $31.59 base) and 41 (Arizona Green Tea, $32.11).** Either way, the other Arizona stays.

## Why the first free run still doubled Red Bull, and why plan 7 does not

- **What happened.** The first optimiser pass had no test slot and started from plan 6. It kept two Red Bull doubles (Original and Yellow) at $979.49 base gross profit.
- **The cause was the search, not the model.** A Red Bull 24-pack fills one 11-can slot and leaves 13 cans spare. A second Red Bull slot is filled from those spare cans, so it costs no cash. Removing the double means buying a new pack, and with the fill at the $366 cap, no single swap fits. The one-swap search was stuck.
- **What fixed it.** The test-slot runs start each category from a different set of drinks. The best of them, plan 7, scores $1,027.31 with no doubles. Re-run without the test-slot rule, the search starts from plan 7 and finds nothing better.
- **What that means.** With the new rule, the maths prefers 20 distinct drinks. A double only wins when the cap leaves no cash for another pack.

## Brand lean

- **No new-brand swap passed the $5 tolerance** that optimize7.py allows (`BRAND_TOL`).
- **The four bubly flavours stay.** Nothing from a new brand fits under the cap in a bubly slot, because every other pack costs more than $5.
- **The closest new-brand swaps, and what they cost over the run (base):**

  | Swap | Cost |
  |---|---|
  | Red Bull Yellow → C4 | $7.02 |
  | Snapple Peach Tea → Pepsi Real Sugar mini cans | $8.60 |
  | Arizona Green Tea → Pepsi Real Sugar mini cans | $16.26 |

- **The category term did the rest:**

  | Category | Drinks in plan 7 |
  |---|---|
  | Energy | 8 (under its limit of 9) |
  | Sparkling | 4 |
  | Tea | 3 |
  | Water | 2 |
  | Sports | 2 |
  | Juice | 1 |

## What changed in the code

**`project.py`** (round 6's version is kept as `project_v4.py`):
- **Duplicate rule.** `DUP_DECAY = 0.75` and its factor in the rate are removed. The simulation now runs per product instead of per slot:
  - A product's one demand is computed from its first slot's price and category.
  - Its slots pool their stock. On each Friday visit the pool is refilled to the sum of their fills, with half of Friday's demand before the visit, as before.
  - The product sells min(demand, stock), and its sales, lost units and weekly units are split across its slots by capacity.
- **Category saturation.** `CAT_FREE` (energy 9, others 4) and `CAT_DECAY` (0.6) are unchanged. They now count distinct products, not slots, so a duplicate slot adds no flavour and does not push other drinks in its category past the limit.
- **New check.** A product whose slots have different prices is listed as a problem, because one product has one demand.
- **New fields per slot** (the existing fields are kept): `peak_week_units`, `peak_cycle_units`, `capacity`, `product_slots`, `product_capacity`, `product_units_per_class_day` and `product_peak_cycle_units`.
- **`assumptions`.** `DUP_DECAY` is replaced by `DUPLICATES` (the rule in words) and `CATEGORY_COUNT`.
- **Docstring and wording.** The module docstring records the change. A problem message now says "slot holds" instead of "column holds".
- **Check.** For a plan with no duplicates, the new project.py gives the same profit as project_v4.py to the cent ($421.83 low, $862.93 base, $1,209.12 high, on plan 6's 14 distinct drinks).

**`optimize.py`**: not changed. Round 7 imports `best_price` from it, as round 6 did.

**New in round7/:**
- `optimize7.py`: round 6's optimiser, plus:
  - the test slot (one optimisation per category, with the test drink's pack counted against the cap from the start);
  - the brand lean (`BRAND_TOL`, $5);
  - a free run with no test slot, for reference;
  - our slots read from plan6.json instead of typed in.
- `place7.py`: round 6's placement, plus the test slot's role and its move off wildcard spots. The wildcard spots are read from plan 6's check.
- `build_catalog7.py`: round 6's script, pointed at round7/. It gives a catalog identical to catalog6.json.
- `compare7.py`: writes the tables above and `projection6_rescored.json`.

## Checks run

- **Round 6 reproduced.** project_v4.py, run on plan 6 against the current repo's `vending.pricing` and `vending.capacity`, gives round 6's figures exactly ($557.47 / $1,137.43 / $1,592.64; fill $308.09 / $357.50).
- **Round reader.** The repo's own round reader (`vending.plan_rounds.discover` and `read_round`, used offline, with no database) reads round 7 with no warnings and 20 slots.
- **Not done.** `plan_store` was not run.

## 7 vs 7b: the fill cap raised from $366 to $400

Plan 7b is round 7 re-run with a $400 most-we-pay cap (Kevin, 22 Nov). Everything else is the same: the duplicate rule, the test slot, the brand lean, the catalog file and the $53 spendable floor.

### Verdict: the extra $34 is worth it. Plan 7b is better.

- **Profit is up in every scenario:**

  | Scenario | Change |
  |---|---|
  | Low | +$34.76 |
  | Base | +$70.35 |
  | High | +$98.29 |

- **The extra spend is small.** The fill costs $28.34 more expected and $33.00 more at most. Leftover stock counts at cost (A5), so no extra money is at risk. Spendable after the fill is still $201 in the worst case, against the $53 floor.
- **What the money bought.** Out: Hint Grape, Snapple Peach Tea and bubly Cherry. In: C4 and two Snapple juices (Kiwi Strawberry and Fruit Punch). One bubly was replaced, by a mix of a new brand (C4) and more Snapple, not one-for-one. No soda or protein drink got in: under the model they still earn less per dollar of pack than these drinks.
- **Brands and categories.** Brands go from 9 to 10. Energy reaches its limit of 9 drinks (CAT_FREE). Snapple goes from 2 to 3 flavours. That runs against the brand lean, but no new-brand swap came within the $5 tolerance.
- **The cap is still nearly full.** The fill's most-we-pay cost is $399.00 against $400, so a price rise of more than $1 when the prices are checked again breaks the cap. Re-run 7b if that happens.
- **Test slot.** It moves to slot 34, Hint Georgia Peach, the only water. Its place in the placement order fell on a wildcard spot, and 34 is the next spot that isn't one. Slot 34 sold least of our shelf 3–4 slots in the fall, so a test drink there starts at a disadvantage. Read its weekly sales with that in mind, or let the agent move the test slot later.
- **Code changes for 7b:**
  - `optimize7.py --cap-cents 40000`, and `project.py --cap 400`. In the repo (#231) the default is now $400 (`project.FILL_CAP`); plan 7 needs `--cap-cents 36600` and `--cap 366`.
  - `project.py` now writes the cap it was checked against as `fill_cap`.
  - Plan 7 rebuilt unchanged with the new code: plan7.json is identical and projection7.json's scenarios are identical.
  - The repo's round reader, used offline, reads 7b with no warnings and 20 slots.

### Each slot's drink

| Slot | Plan 7 ($366) | Base GP | Plan 7b ($400) | Base GP |
|---|---|---|---|---|
| 12 | Celsius Sparkling Arctic Vibe 12 oz can | $80.55 | Celsius Sparkling Tropical Vibe 12 oz can | $80.55 |
| 14 | Red Bull Sugarfree 8.4 oz can | $88.38 | Red Bull Original 8.4 oz can | $88.38 |
| 16 | Celsius Sparkling Tropical Vibe 12 oz can | $80.55 | Celsius Sparkling Arctic Vibe 12 oz can | $80.55 |
| 18 | Red Bull Original 8.4 oz can | $88.38 | Red Bull Sugarfree 8.4 oz can | $88.38 |
| 21 | Rockstar Energy 16 oz can | $72.05 | Rockstar Energy 16 oz can | $72.05 |
| 23 | Celsius Sparkling Orange 12 oz can | $75.54 | Celsius Sparkling Orange 12 oz can | $75.54 |
| 25 | Red Bull Yellow Edition Tropical 8.4 oz can | $76.58 | Red Bull Yellow Edition Tropical 8.4 oz can | $76.58 |
| 27 | Celsius Sparkling Kiwi Guava 12 oz can | $80.55 | Celsius Sparkling Kiwi Guava 12 oz can | $80.55 |
| 32 | Hint Grape 16 oz bottle | $37.81 | Gatorade Thirst Quencher 20 oz bottle | $43.73 |
| 34 | Snapple Peach Tea 16 oz bottle | $24.45 | Hint Georgia Peach 16 oz bottle **(test)** | $37.81 |
| 36 | Gatorade Thirst Quencher 20 oz bottle | $43.73 | Powerade Sports Drink 20 oz bottle | $46.39 |
| 38 | Powerade Sports Drink 20 oz bottle | $46.39 | C4 Performance Energy Cosmic Rainbow 16 oz can | $69.56 |
| 41 | AriZona Green Tea with Ginseng and Honey 16 oz can | $32.11 | Snapple Fruit Punch 16 oz bottle | $38.00 |
| 43 | Arizona Iced Tea with Lemon Flavor 16 oz bottle | $31.59 | AriZona Green Tea with Ginseng and Honey 16 oz can | $32.11 |
| 45 | Snapple Apple 16 oz bottle **(test)** | $53.20 | Snapple Apple 16 oz bottle | $53.20 |
| 47 | Hint Georgia Peach 16 oz bottle | $37.81 | Snapple Kiwi Strawberry 16 oz bottle | $44.46 |
| 52 | Bubly Sparkling Water Cherry 12 oz can | $19.41 | Bubly Sparkling Water Strawberry 12 oz can | $19.41 |
| 54 | Bubly Sparkling Water Strawberry 12 oz can | $19.41 | Bubly Sparkling Water Blackberry 12 oz can | $19.41 |
| 56 | Bubly Sparkling Water Grapefruit 12 oz can | $19.41 | Bubly Sparkling Water Grapefruit 12 oz can | $19.41 |
| 58 | Bubly Sparkling Water Blackberry 12 oz can | $19.41 | Arizona Iced Tea with Lemon Flavor 16 oz bottle | $31.59 |

Out in 7b: Bubly Sparkling Water Cherry 12 oz can, Hint Grape 16 oz bottle, Snapple Peach Tea 16 oz bottle.  
In for 7b: C4 Performance Energy Cosmic Rainbow 16 oz can, Snapple Fruit Punch 16 oz bottle, Snapple Kiwi Strawberry 16 oz bottle.  
The rest are the same drinks; most moved slot because placement ranks drinks by value.

### Brands

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
| **Brands** | **9** | **10** |

Categories: energy 8 → 9; juice 1 → 3; sparkling 4 → 3; sports 2 → 2; tea 3 → 2; water 2 → 1.

### Money

| | Plan 7 ($366) | Plan 7b ($400) | Change |
|---|---|---|---|
| Fill cost, expected | $315.16 | $343.50 | +$28.34 |
| Fill cost, most we pay | $366.00 | $399.00 | +$33.00 |
| Spendable after the fill, worst case ($600 less most we pay; floor $53) | $234.00 | $201.00 | −$33.00 |
| Projected profit, low | $498.61 | $533.37 | +$34.76 |
| Projected profit, base | $1,018.31 | $1,088.66 | +$70.35 |
| Projected profit, high | $1,426.17 | $1,524.46 | +$98.29 |
| Closet stock after the fill (units, valued at cost) | 90 | 94 | +4 |

### Drinks in 7b that need a checked-price sourcing row

14 drinks: AriZona Green Tea with Ginseng and Honey 16 oz can, Bubly Sparkling Water Blackberry 12 oz can, Bubly Sparkling Water Grapefruit 12 oz can, Bubly Sparkling Water Strawberry 12 oz can, C4 Performance Energy Cosmic Rainbow 16 oz can (new in 7b), Celsius Sparkling Arctic Vibe 12 oz can, Celsius Sparkling Kiwi Guava 12 oz can, Celsius Sparkling Tropical Vibe 12 oz can, Gatorade Thirst Quencher 20 oz bottle, Powerade Sports Drink 20 oz bottle, Red Bull Yellow Edition Tropical 8.4 oz can, Rockstar Energy 16 oz can, Snapple Apple 16 oz bottle, Snapple Kiwi Strawberry 16 oz bottle (new in 7b).  
Snapple Fruit Punch, new in 7b, already has one (Amazon, 15 Nov). Plan 7's list was also 14; 7b drops bubly Cherry and Snapple Peach Tea from it and adds C4 and Snapple Kiwi Strawberry.
