# Opening capital plan

How we plan to use the $600 we start with: what the day-one fill costs, what we hold back, and why.

## What this is

The brief asks for "your opening capital plan: how you intend to allocate the initial $600.00, and the reasoning behind holding back whatever you hold back" (section 5; issue #90). This is that plan, for our 20 slots.

The $600 is given once. Sales flow back into the same account; midweek fees and model spend come out of it. An order the balance can't cover is refused (`docs/agent/money.md`).

The instructor's 12 Nov answers (PR #106, issue #107):

- Our **day-one order fills all 20 slots to capacity**, paid from the $600.
- It is due about **Fri 27 Nov**, inside the 26 to 29 Nov closure; time TBC (#102).
- The machine goes live **Thu 3 Dec**. Restocks are **Friday afternoons** (3 PM target, likely later).
- The ledger opens **before the fill is fulfilled**, so its receipts count (#104, #106).

This plan owns the Summative 2 money numbers. The restock policy (`docs/restock-policy.md`, #91) and the demand doc (`docs/inventory-demand.md`, #93) cite them.

## The plan in one table

"Proposed" means not yet team-agreed (see "Open decisions").

| Line | Amount | Basis |
|---|---|---|
| Day-one fill, 19 regular slots (most) | **$360.00** | Proposed cap. Run 1's dry run: $360 most for 20 slots (#90). Example below: $356.50 most for 19 |
| Wildcard slot | $82.00 | Proposed. 11 × $7.407 = $81.48, rounded up (section 2) |
| Midweek visit fees | $50.00 | 2 × $25 (`docs/agent/restocking.md`) |
| Model spend | $9.00 | 9 run weeks × 1 run × $1.00 run cap (#51) |
| Product-switch reserve | $68.00 | Proposed. One pack of the dearest listed drink at the worker's maximum (section 2) |
| Safety margin | $31.00 | Proposed. $600 − $360 − $82 − $50 − $9 − $68 |
| **Total** | **$600.00** | |

**Spendable floor: $53.00 (Proposed).** A rule, not a line. After any request, at most-we-pay cost, spendable must still hold one midweek fee ($25.00, config `midweek_fee_cents`) plus one week of model spend at the daily cap (7 × $4.00 = $28.00, config `model_daily_cap_cents`, default 400). Both can leave before a Friday's takings arrive, and the code enforces both (`vending/planner.py`, `vending/harness.py`). The derivation is the restock policy's (rule 4). The held-back lines contain it: $25 of the fee line, $1.00 of the model line, $27.00 of the margin.

**Headroom.** At expected prices the example's 19 slots cost $308.26 ($315.94 − $7.68), $51.74 under the cap. After the fill at its most ($360 + $82 = $442) and one live run before 3 Dec ($1), $600 − $443 = $157 stays spendable.

## 1. The day-one fill

**Capacity** per column (`vending/capacity.py`, `vending/dashboard.py`): 20 oz and 16 oz bottles 7; 14 oz bottle 8; 12 oz can 8; 8.4 oz can 11. A 16 oz can must be counted by hand first.

**Full slot** = pack price ÷ pack units × column. Prices: the team's 11 Nov Amazon list (branch `add_price_list`, `docs/agent/price-list.md`).

| Drink | Pack | Price | Per unit | Column | Full slot |
|---|---|---|---|---|---|
| Pepsi 12 oz can | 12 | $6.97 | $0.58 | 8 | $4.65 |
| Coca-Cola 12 oz can | 12 | $7.68 | $0.64 | 8 | $5.12 |
| Spindrift Grapefruit 12 oz can | 24 | $20.97 | $0.87 | 8 | $6.99 |
| Celsius Kiwi Guava 12 oz can | 12 | $19.98 | $1.67 | 8 | $13.32 |
| Mountain Dew 20 oz | 24 | $49.24 | $2.05 | 7 | $14.36 |
| Celsius Sparkling Orange 12 oz can | 12 | $21.57 | $1.80 | 8 | $14.38 |
| Diet Coke 20 oz | 24 | $50.99 | $2.12 | 7 | $14.87 |
| Coca-Cola 20 oz | 24 | $54.95 | $2.29 | 7 | $16.03 |
| Red Bull Original 8.4 oz | 24 | $34.99 | $1.46 | 11 | $16.04 |
| Pepsi 20 oz | 12 | $29.99 | $2.50 | 7 | $17.49 |
| Red Bull Red Edition 8.4 oz | 24 | $47.99 | $2.00 | 11 | $22.00 |

The other 9 listed drinks cost $6.97 ($20.91 ÷ 24 × 8) to $19.47 ($42.48 ÷ 24 × 11) a slot, so the range holds. We buy whole packs; leftovers go to the closet but are paid for.

| Case | Units | Packs | Cost |
|---|---|---|---|
| Cheapest: 20 slots Pepsi cans | 160 | 14 × $6.97 | $97.58 |
| Run 1's dry run (#90) | 159 | not recorded | $293 expected, $360 most |
| Example mix | 163 | 10 | $315.94 expected, $365.50 most |
| Dearest: 20 slots Red Bull Red | 220 | 10 × $47.99 | $479.90 expected, $555.00 most |

The dearest case leaves $45. Hence the cap.

**Example mix** (a pricing example, not the team's pick). It leans on energy drinks, 45% of our fall units (`analysis/REPORT.md`, finding 14). Slots are grouped so packs divide: three 20 oz slots use 21 of 24; three Celsius slots use two 12-packs exactly. 17 units go to the closet.

| Drink | Slots | Units | Packs | Expected | Most |
|---|---|---|---|---|---|
| Red Bull Original 8.4 oz | 4 | 44 | 2 × 24 | $69.98 | $81.00 |
| Celsius Sparkling Orange | 3 | 24 | 2 × 12 | $43.14 | $50.00 |
| Celsius Kiwi Guava | 3 | 24 | 2 × 12 | $39.96 | $46.00 |
| Coca-Cola 20 oz | 3 | 21 | 1 × 24 | $54.95 | $63.50 |
| Diet Coke 20 oz | 3 | 21 | 1 × 24 | $50.99 | $59.00 |
| Mountain Dew 20 oz | 3 | 21 | 1 × 24 | $49.24 | $57.00 |
| Coca-Cola 12 oz can | 1 | 8 | 1 × 12 | $7.68 | $9.00 |
| **Total** | **20** | **163** | **10** | **$315.94** | **$365.50** |

"Most" is the list's maximum per pack. Where none is listed we use its rule, price × 1.15 rounded up to 50 cents (its $54.95 × 1.15 = $63.19 is listed as $63.50): Kiwi Guava $19.98 → $22.98 → $23.00; Coca-Cola can $7.68 → $8.83 → $9.00. If the wildcard took the Coca-Cola can slot, the other 19 cost $308.26 expected, $356.50 most ($365.50 − $9.00).

**How long it lasts.** Kept stocked, our slots sell 23.3 units a class day (recent) or 22 (whole era) (`analysis/REPORT.md`, finding 4, "Next 4 weeks"); #90's older estimate is 70 a week, 14 a day. So 163 units last 7 (163 ÷ 23.3) to 11.6 (163 ÷ 14) class days. Per-slot demand: `docs/inventory-demand.md`.

## 2. What we hold back, and why

### Restocking what sold: no reserve

The price floor is the worker's maximum unit cost × 1.08, rounded up to 5 cents (`vending/pricing.py`). Each sale returns its cost after tax: break-even, at most 4 cents over. So refills pay for themselves. Thu 3 to Wed 9 Dec (5 class days):

| | High (23.3 a day) | Low (14 a day) |
|---|---|---|
| Units sold | 116.5 | 70 |
| Takings at $3.35 | $390.28 | $234.50 |
| After tax (÷ 1.08) | $361.37 | $217.13 |
| Refill at $2.26 | $263.29 | $158.20 |
| Left | $98.08 | $58.93 |

$3.35 = $1,881.50 ÷ 562, our fall takings per unit (`analysis/REPORT.md`, finding 1). $2.26 = $360 ÷ 159, run 1's most per unit (#90). Whole packs leave closet units; the reserves cover that. A 4 Dec restock would follow one class day and be small (#107).

### Product-switch reserve: $68.00 (Proposed)

A slot is flagged after 5 in-stock class days with no sales (PR #100). The first 5 end Wed 9 Dec, so the first switch is Fri 11 Dec, with $58.93 to $98.08 of week-one surplus in hand. So we hold one pack, not two: Mountain Dew Baja Blast 20 oz, the dearest listed, $58.80 × 1.15 = $67.62 → $68.00. Reserve plus surplus buys 1.87 to 2.44 packs ((68.00 + 58.93) ÷ 68.00; (68.00 + 98.08) ÷ 68.00). If week one sells less, the second switch waits. Swap-first (Open decision 4) would draw on it sooner.

### Wildcard slot: $82.00 (Proposed)

**Proposal** (Kevin, #100; not agreed). The agent picks one of our 20 slots, names it in its plan with a reason, and moves it only at a Friday visit. Ceiling about $8.00 (others $6.50, PR #100). Same floor as every slot. Aim: every sale profits, no loss leaders, which needs a floor above today's break-even (Open decision 3). Ideas: premium drink, high-price test, rotating feature, seasonal bet, anchor.

**Bound.** No product is picked; the dearest listed unit is Pepsi 20 oz at $2.50 ($29.99 ÷ 12). An $8.00 price allows a unit cost up to $8.00 ÷ 1.08 = $7.407 (`vending/pricing.py` allows fractional cents). The largest column holds 11: 11 × $7.407 = $81.48 → $82.00. (12 oz can column: $59.26; 20 oz: $51.85.)

**Proposed rule.** One wildcard purchase costs at most $82.00 at the worker's maximum. A bigger pack fits only if its unit cost is lower. A higher floor only lowers unit cost, so $82.00 stays the top. Unused on day one, the line joins the switch reserve; a later wildcard is paid from takings or that reserve, same cap.

### Midweek visit fees: $50.00

A non-Friday visit costs $25 (`docs/agent/restocking.md`); we hold 2. When one pays is restock policy rule 2. At run 2's most-we-pay cost, Red Bull at $4.50 nets $2.48 a unit after tax ($4.50 ÷ 1.08 − $40.50 ÷ 24; `docs/sample-directive.md`), so $25 is about 11 sales (25 ÷ 2.48 = 10.1, rounded up).

### Model spend: $9.00

Live calls are charged at real cost from the ledger's opening (`vending/harness.py`, #51, #104). Observed: $0.02 to $0.80 a run (#90); run 2, $0.40 (`docs/sample-directive.md`). Caps are placeholders: $1.00 a run, $4.00 a day (#51). Nothing schedules the agent (#39, #107); we assume one run a week. Every day at the cap would be 63 days (9 × 7) × $4.00 = $252.00, 42% of the $600, so set the daily cap before go-live.

### Safety margin: $31.00 (Proposed)

$27.00 for one week at the daily cap beyond the planned $1.00 (7 × $4.00 − $1.00), $1.00 for one live run before 3 Dec, $3.00 unassigned. Price rises up to the maximum (about 15%) are already in the fill cap and pack lines.

### Sales tax: $0 from the capital

Shelf prices include 8% tax. The ledger sets aside takings × 0.08 ÷ 1.08 (about 7.4%); spendable excludes it (`vending/ledger.py`; `docs/planning/2026-10-19-david-api-qa.md`, row 6). Purchases are tax-exempt (same file).

## 3. When held-back money is released

- **First Friday:** refill sold units from takings.
- **From Fri 11 Dec:** switch reserve plus takings fund switches for flagged slots.
- **Wildcard line:** at the day-one fill, or it joins the switch reserve.
- **Midweek fees:** only when restock policy rule 2 says so, with working shown.
- **Winter break:** classes stop 22 Dec to 1 Jan; closed 25 Dec, 28 Dec, 1 Jan (`analysis/golisano_calendar_2026_27.json`). Fill lightly Fri 18 Dec.
- **Always:** spendable ≥ $53.00 after any request (restock policy rule 4). No code enforces it yet.

## Assumptions

To be numbered in `docs/assumptions.md` (A16 is taken in #69, A17 and A18 in #99).

1. **The fill comes out of the $600.** The instructor said so but is checking (#106, row 3).
2. **It is bought on Amazon at the list's packs and prices.** Not confirmed (#102). Another seller, or no delivery in the closure, changes both (#106).
3. **Prices stay near 11 Nov's.** The ~15% maximum covers drift.
4. **The loading chart is right.** It says it is approximate (`vending/capacity.py`).
5. **New products sell near our fall rate**, 14 to 23.3 a class day. Price effects are unknown (#99, `docs/agent/price-history.md`). Our 20 oz Coke and Diet Coke floors ($2.90, $2.70) exceed their $2.00 fall price, so the example's 9 soda slots may sell slower.
6. **The agent runs live once a week** (#39).
7. **The run is 9 weeks** from 3 Dec (`docs/agent/goal-and-scoring.md`; #106).
8. **Closet stock is valued at cost** at the end; the instructor is checking (`docs/planning/2026-10-19-david-api-qa.md`, row 16).

## Open decisions

1. **Day-one products and prices** (#107). Fall top sellers Monster, Gorgie and Ghost (`analysis/REPORT.md`) are not on the price list.
2. **Wildcard slot** (Kevin, #100). Proposed: an $82.00 line and per-purchase cap (section 2).
3. **The floor.** Today's breaks even after tax. Raising it so every sale profits, for the wildcard or all slots, is open.
4. **Swap first at a slow-seller flag** (Kevin, 12 Nov): at most one price step down, then swap; pulled units to the closet at cost. Depends on assumption 8 (#102, P2). Reasoning: restock policy rule 3.
5. **Fill cap.** Proposed: $360.00 most for 19 regular slots.
6. **Switch reserve.** Proposed: one pack, $68.00 (earlier draft: two, $136.00).
7. **Safety margin.** Proposed: $31.00.
8. **Spendable floor.** Proposed: $53.00 ($25 + 7 × $4.00), replacing the earlier $59 ($50 fees + $9 model).
9. **Midweek visits held back for.** This plan: 2.
10. **Model caps and run frequency** (#51, #39). A $4.00 daily cap doesn't fit a $9.00 line; lowering it shrinks the floor and margin equally.
11. **Day-one deadline and ordering method** (#102).

**Follow-ups (not done here).** Point `docs/agent/money.md` here once agreed (#90). Add a planner check for the $53 floor.

## Sources

- #90: the brief, run 1's dry run (159 units, $293 expected, $360 most), 70 a week, spend per run.
- PR #106 (`docs/planning/2026-11-12-david-answers.md`), #107: 12 Nov answers.
- PR #100: $6.50 ceiling, slow-seller rule, wildcard.
- `docs/restock-policy.md` (#91), `docs/inventory-demand.md` (#93).
- PR #99 (`docs/agent/price-history.md`); PR #104; PR #51.
- Branch `add_price_list` (`docs/agent/price-list.md`): 11 Nov prices.
- `vending/capacity.py`, `dashboard.py`, `pricing.py`, `config.py`, `planner.py`, `harness.py`, `ledger.py`.
- `docs/agent/money.md`, `restocking.md`, `goal-and-scoring.md`.
- `docs/sample-directive.md`: run 2 ($233.31 expected, $270.00 most, $0.40).
- `docs/planning/2026-10-19-david-api-qa.md`: sales and purchase tax.
- `analysis/REPORT.md`: our 20 slots' units, takings, rates.
- `analysis/golisano_calendar_2026_27.json`: break and closures.
