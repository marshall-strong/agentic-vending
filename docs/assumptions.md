# Assumptions register

Things our code or our scores rely on that we haven't confirmed. Each one says where it's used,
what would show it's wrong, and what happens to it next. Add a row when code starts relying on
something unconfirmed; close it when the data or a decision settles it. This feeds the risks
section of Summative 2.

| ID | Assumption | Used in | What would show it's wrong | Next step |
| --- | --- | --- | --- | --- |
| A1 | Demand in a slot is what it actually sold the following week. Sales are only a floor: a slot that ran out would have sold more. | Week replayer: outcome score | A slot whose stock count hit zero during the week | Receipts (PR #25) give stock, so sell-outs can be spotted and those weeks flagged |
| A2 | Demand doesn't depend on price. A plan that changes a price is scored as if the same number would have sold. | Week replayer: outcome score | Sales moving after a price change on the real machine | Compare weeks before and after our own price changes once we make them |
| A3 | A new product in a slot can't be scored on outcome; it's graded on its reasons only. On replayed weeks the slot's product is unknown, so any named product counts as possibly new. | Week replayer: outcome score | (A limit, not a claim) | Receipts (PR #25) give the product; revisit once we have sales for products we switched in |
| A4 | Where stock is unknown, the rail holds exactly `fill_to` after the visit, and every unit is bought. | Week replayer: outcome score and balance check | Receipts showing units already on the rail | Goes away when receipts (PR #25) give stock |
| A5 | Unsold units cost nothing in the margin: the closet is valued at what we paid. | Week replayer: margin | David's answer on how end-of-run stock is scored (19 Oct: "he believes at cost; verifying") | Confirm with David |
| A6 | A week runs from midnight Eastern on the date given, and the polls just before each midnight bound it. | Week replayer: `replay_week` | (A convention, not a claim) | Change it if the team wants weeks to run Friday visit to Friday visit |
| A7 | Every unit costs the figure passed to `--unit-cost`. | Week replayer: margin, floor and balance on replayed weeks | Any receipt | Goes away when receipts give actual unit cost |
| A10 | Model prices are Anthropic's list prices, checked 9 Nov 2026 (`vending/model_prices.json`, the harness's table): Opus 5.5 $4/$20, Sonnet 5.5 $2/$10, Haiku 5.5 $0.10/$0.50 per million tokens in/out. Every cost in `model_calls` and the ledger, and every cap, is only as exact as this table. | The harness: caps, `model_calls`, the ledger's token entries | A Console bill that doesn't match `python -m vending.usage` | Check the pricing page when a model or price changes; compare `vending.usage` with the Console's Usage and Cost pages monthly |
| A8 | No plan puts more than 50 units on one rail (`MAX_FILL`). A sanity limit, not the rail's capacity. | Week replayer: valid plan check | A rail that really holds more than 50 | Replace with `config.slot_capacity` once the team measures the rails |
| A9 | Our 20 slots start the competition empty, so a slot we have never restocked holds nothing. **Kevin, 9 Nov: the machine will be empty at the start of the quarter; the agent's orders fill it.** | Preview and submit (the planner's current-state gathering, once `preview_desired_state` lands): slot contents and product come only from our receipts | Units or a product in one of our slots before our first receipt for it. Not true while we test this quarter: course staff stocked the slots (slot 15 shows lifetime sales), so test previews undercount stock and may set `change_product` | Confirm the machine is emptied before the live run; read `GET /api/v1/slots` at the start to check |
