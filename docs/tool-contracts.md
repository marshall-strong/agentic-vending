# Tool contracts

A tool is one thing the agent can ask our code to do: read the machine's state, plan a restock,
place an order. Each tool is deterministic Python, served to the agent over stdio by an MCP server
(`python -m vending.server`, see [`mcp-server.md`](mcp-server.md)). The tools never call a
language model: the model is called only by the agent loop (PR #58, open, not yet on `main`), so
every figure below is worked out by code, not by the model. Every tool that changes anything
outside our database (a write tool) passes the write gate: the halt switch is checked first, and
every attempt is logged.

This is the contract for each of the 11 tools registered in `vending/server.py` on `main`, as
Summative 2 asks: "inputs, outputs, preconditions, side effects, cost, failure behavior, and who
owns it" (Project Brief (Revised), section 5). Checked against the code at `ba14a6d`, 12 Nov 2026.

## Summary

| Tool | Read or write | What it is for | Owner |
| --- | --- | --- | --- |
| `get_machine_state` | Read | Each of our 20 slots now: price, sales rate, stock on hand; and the closet at cost | Kevin Sykes, with Meaghan Gartland |
| `get_sales_history` | Read | Units and revenue per slot between two times | Kevin Sykes |
| `get_catalog` | Read | The products we are allowed to order | Sean Kelley |
| `get_account_balance` | Read | How much money we have, and how much of it we may spend | Marshall Strong |
| `get_pnl` | Read | Profit and loss over a range of days, and inventory at cost | Marshall Strong |
| `preview_desired_state` | Read | Check a restock plan without sending it: cost, prices, every problem | Marshall Strong (planner: Meaghan Gartland) |
| `submit_desired_state` | Write | Send a checked restock plan as a purchase request (spends money) | Marshall Strong |
| `get_purchase_requests` | Read | Our purchase requests, their status and receipts | Meaghan Gartland |
| `get_project_rules` | Read | Our notes on the project brief, by topic | Kevin Sykes |
| `request_catalog_product` | Write | Ask the instructors to add a drink to the catalog | Kevin Sykes |
| `set_price` | Write | Dropped: always refuses and says to use `submit_desired_state` | Kevin Sykes (dropped it) |

How the owners were found: the `mcp-tool` issues #15 to #24 and the pull request that built each
tool. Where an issue had no assignee, the owner named is the author of the pull request that built
the tool, and the section says so. `get_project_rules` has no issue; it came with PR #63.

All money in replies is a dollar string such as `"2.50"`, never a floating-point number. Every
reply has `success`, `message` and `count`; a failure's `message` says what a person or the agent
should do next.

## `get_machine_state`

- **Inputs**: none.
- **Outputs**: one row per slot (all 20) in `data`: `price`, `lifetime_units`,
  `units_sold_in_window` and `units_per_day` over the last 7 days of stored polls; and, from the
  purchase receipts, `product`, `stock_on_hand`, `units_sold_since_load`, `days_since_loaded`
  and `days_of_stock_left`. Also the closet (back stock) per product in `closet`, with `units`,
  `unit_cost` and `value_at_cost`, and its total in `closet_value_at_cost`. Plus `as_of` (the
  poll's time), `window_start`, `stale` and any stock `warnings`.
- **Preconditions**: at least one good poll stored by the history sync
  (`python -m vending.sync_history`). The stock fields also need receipts, stored by
  `python -m vending.sync_requests`.
- **Side effects**: one `action_log` row. Nothing else is written.
- **Cost**: database queries only. It never polls the machine or calls the bridge.
- **Failure behavior**:
  - No good poll stored: `success: false`, naming the sync command.
  - A slot with no receipts has `null` (not zero) in every receipt field, so "unknown" is never
    read as "empty"; the message names those slots.
  - Latest poll over 3 hours old: `stale: true`, and the message starts `STALE:` and tells the
    agent not to price on these rates until the sync catches up.
  - Only one poll stored: the 7-day rates are `null` and the message says the window is
    unavailable.
  - Database unreachable: the call returns an error; the server reopens its connection on the
    next call.
- **Owner**: Kevin Sykes built it (PR #14); Meaghan Gartland added the receipt fields and the
  closet (PR #45). Issue #15 has no assignee.

## `get_sales_history`

- **Inputs**: `start` and `end` (date and time, read as UTC when no zone is given); optional
  `slots`, a list of our slot numbers (omit for all 20).
- **Outputs**: per slot, `units`, `revenue` (shelf takings, 8% tax included) and `revenue_ex_tax`;
  the same two totals; and `start` and `end` set to the two polls the window actually spans.
  Tax inside takings T is T x 8/108, rounded half up to the cent.
- **Preconditions**: two good polls in or around the window.
- **Side effects**: one `action_log` row.
- **Cost**: database queries only.
- **Failure behavior**:
  - A slot that isn't ours: `success: false`, listing ours.
  - `start` not before `end`: `success: false`.
  - Fewer than two polls in the window: `success: false`, suggesting a wider window or the sync.
  - No poll before `start`: it starts at the first poll and the message says so.
  - A slot whose counter went down between the two polls (a replaced board) gets `null` and is
    left out of the totals, rather than showing negative sales.
  - It does not flag an old latest poll; the message names the poll times instead.
- **Owner**: Kevin Sykes built it (PR #14). Issue #16 has no assignee.

## `get_catalog`

- **Inputs**: optional `search`. Every word must appear somewhere in the product's name, line,
  flavor or size (case does not matter).
- **Outputs**: `data`, the listed single-item products: `sku`, `name`, `line`, `flavor`, `size`;
  and `synced_at`, when the catalog was last copied from the bridge. Products the bridge stopped
  listing are left out.
- **Preconditions**: the catalog has been synced at least once (`python -m vending.sync_catalog`,
  which the worker in our Container App runs every hour).
- **Side effects**: one `action_log` row.
- **Cost**: database queries only. It reads our copy and never calls the bridge.
- **Failure behavior**:
  - Never synced: `success: false`, naming the sync command.
  - Last sync over 36 hours old: still `success: true`, but the message says the catalog is stale
    and names the command.
  - No match: `success: true` with no rows, suggesting fewer words or `request_catalog_product`.
  - Version 1 limit: our last unit cost, sourcing and pending catalog requests are not in it yet.
- **Owner**: Sean Kelley (issue #17, PR #43).

## `get_account_balance`

- **Inputs**: none.
- **Outputs**: `balance`, worked out each time from the ledger, never stored:
  opening capital + later capital + takings - receipts - midweek visit fees - token spend - tax
  remitted, plus or minus adjustments. Each part is its own field (`opening`, `capital`,
  `revenue`, `receipts`, `visit_fees` and `visits`, `token_spend`, `tax_remitted`,
  `adjustments`). Also `tax_set_aside`, `tax_owed` (set aside less remitted) and `spendable`
  (balance less tax owed), which is the figure the planner's spend check uses. Plus `as_of`,
  `opening_at` and `notes`.
- **Preconditions**: a person has opened the ledger with
  `python -m vending.ledger open --amount ... --by NAME`.
- **Side effects**: one `action_log` row.
- **Cost**: database queries only.
- **Failure behavior**:
  - Ledger not opened: `success: false`, naming the command, so the agent can't plan against a
    balance nobody has set.
  - Ledger entries dated before the opening are outside the balance and named in `notes`.
  - Takings are only as current as the last stored poll; this tool does not flag an old poll.
- **Owner**: Marshall Strong (issue #18, PR #35).

## `get_pnl`

- **Inputs**: optional `start` and `end`, calendar days in Eastern time, both inclusive. Default:
  from the ledger's opening to today.
- **Outputs**: `revenue` (tax included), `tax_set_aside`, `revenue_ex_tax`, `units_sold`,
  `units_costed`, `cost_of_sales` (weighted-average cost from receipts) and
  `cost_of_sales_complete`, `gross_margin`, `shrinkage` and `shrinkage_units` (units lost without
  a sale), `visit_fees`, `visits`, `token_spend`, `adjustments`, `net`; also `purchases`,
  `capital` and `tax_remitted`, which are shown but are not in `net`; inventory at cost
  (`inventory_machine`, `inventory_closet`, `inventory`, `inventory_complete`); per-week revenue
  in `weeks`; and `notes`.
- **Preconditions**: the ledger is open, unless both dates are given.
- **Side effects**: one `action_log` row.
- **Cost**: database queries only.
- **Failure behavior**:
  - No `start` and the ledger not opened: `success: false`, naming the command.
  - `end` before `start`: `success: false`.
  - Units sold that no receipt can cost: `cost_of_sales_complete: false` and a note saying how
    many. Cost of sales is never "unknown" as a whole.
  - Stock whose cost is unknown: `inventory_complete: false` and a note.
- **Owner**: Marshall Strong (issue #23, PR #35).

## `preview_desired_state`

- **Inputs**: `desired`, the state all 20 slots should be in after the next visit:
  - `slots`: each with `slot`, and either `keep: true` or a `product` (catalog SKU), a
    `sale_price` (shelf price including tax, such as `"2.50"`) and an optional `fill_to` (units on
    the rail after the visit; omitted means fill the column).
  - `off_cycle`: `true` asks for a midweek visit, which costs the midweek fee ($25 by default).
  - `sourcing`: per product to buy, a `primary` and optional `alternate` purchase option (`store`,
    one of `Walmart`, `Costco`, `Amazon`, `BJs`; `units_per_pack` 1 to 60; `max_price_per_pack`;
    `url`, required for Amazon; optional `expected_price_per_pack` and `pack_description`), and
    `extra_units_to_closet`.
- **Outputs**: `success` (whether the plan could be submitted as it stands); `plan_hash` (only for
  a plan with no refusals); `lines`, one per slot that changes, with the exact request body in
  `api`; `directive`, the instructions the worker will read; `cost` (`expected`, `max`,
  `visit_fee`, `spendable`, `spendable_left_at_max`, and per line); `floors`, each changed slot's
  lowest legal price and how it was worked out; `warnings`; `refusals` (each a `code`, `message`
  and `slot`); `notes`; `as_of` and `prices_as_of`.
- **Preconditions**: the ledger is open; the bridge's catalog can be read; `capacity_overrides` in
  `config` is valid. Missing polls or receipts don't stop it: they add a note and count as empty.
- **Side effects**: one `action_log` row. Nothing is written and nothing is sent.
- **Cost**: one bridge call (`GET /api/v1/products`, one try, 20-second timeout) and database
  queries. No money.
- **Failure behavior**:
  - Halt switch on: the only refusal is `halted` ("stop and do nothing"). If the bridge is also
    down, the message still says to stop, not to check the network.
  - Something a person must fix first: `success: false`, "Can't plan yet:" and what to do (open
    the ledger; the bridge unreachable, or its catalog empty or unreadable; an invalid
    `capacity_overrides`).
  - A plan that breaks rules: `success: false` with **every** refusal at once, plus everything that
    could still be worked out, so the agent can fix the whole plan in one turn. The codes are in
    the table below.
  - Latest poll over 3 hours old: a `STALE` note, since stock on the rails is overstated.
- **Owner**: Marshall Strong (issue #19, PR #36). The planner it runs, `vending/planner.py`, is
  Meaghan Gartland's (PR #13).

### Refusal codes from the planner

Both `preview_desired_state` and `submit_desired_state` return these.

| Code | Means |
| --- | --- |
| `halted` | The halt switch is on. Stop all writes. |
| `slot_list` | A slot isn't ours, is listed twice, or is missing (all 20 must be listed). |
| `empty_slot` | The plan would leave a slot empty, which the API can't do yet. |
| `product_unknown` | The SKU is not active in the catalog. |
| `price_off_step` | No sale price, a malformed one, or not a multiple of 5 cents. |
| `below_floor` | Price under the slot's floor, or $0.00. |
| `above_ceiling` | Price over the ceiling (`price_ceiling_cents`, $6.00 by default). |
| `unknown_capacity` | Nobody knows how many of this package fit in a column. |
| `over_capacity` | `fill_to` is more than fit in a column. |
| `bad_sourcing` | A purchase option is invalid (store, Amazon link, pack size, max price below expected). |
| `no_sourcing` | A slot needs buying and no sourcing was given. |
| `insufficient_stock` | The closet holds some of the product but not enough, and no sourcing was given. |
| `too_many_packs` | One line would need more than 20 packs. |
| `past_cutoff` | Past an in-store order cutoff (Thursday 23:59 Eastern) for this Friday's visit. |
| `request_pending` | An earlier purchase request is still pending. |
| `insufficient_balance` | The worst-case cost, visit fee included, is more than the spendable balance. |
| `price_only_unsupported` | A slot changes only its price; every line must load at least one unit. |
| `midweek_no_op` | A midweek visit that loads nothing; don't spend the fee. |
| `nothing_to_load` | No slot loads anything, so there is no request to send. |
| `submission_unresolved` | An earlier submission may or may not exist on the bridge; a person must settle it. |

The floor is the pricing gate in `vending/pricing.py`: the highest per-unit cost the slot might
carry (the max pack price of either option, or what units already in the slot cost) x 1.08,
rounded up to the next 5 cents. Off-step prices are refused, never rounded. Past the Amazon cutoff
(Wednesday 23:59) is a warning, not a refusal. Cutoffs don't apply to an `off_cycle` plan.

## `submit_desired_state`

- **Inputs**: the same `desired` as `preview_desired_state`, and an optional
  `expected_plan_hash`, the `plan_hash` the preview returned.
- **Outputs**: `success`; on acceptance `request_id`, `status`, `plan_hash`, `submission` (our
  record's number), `lines`, `max_cost` and `directive`; the plan's `warnings` and `notes`; on a
  refusal, `refusals`.
- **Preconditions**: the halt switch is off; everything `preview_desired_state` needs; the plan
  passes every check above; no earlier submission is unsettled; and, if
  `expected_plan_hash` is given, the plan is unchanged since the preview.
- **Side effects**: on success, a real purchase request on the bridge (`POST
  /api/v1/purchase-requests`): the student worker buys what it lists and loads the machine at the
  next visit. Our records: a `purchase_submissions` row saved **before** sending, settled after
  as `accepted`, `refused`, `not_filed` or `unknown`; on acceptance a `purchase_requests` row,
  which the request sync then follows. One `action_log` row for every attempt.
- **Cost**: **real money**. Up to `max_cost`: for every line, the dearer of its two purchase
  options at its max pack price, plus the midweek fee (`midweek_fee_cents`, $25 by default) if
  `off_cycle`. That worst case must fit within the spendable balance or the plan is refused. The
  fee quoted is saved with the request, so a later change to the config fee never rewrites it.
  Bridge: one catalog read and one POST, each one try with a 20-second timeout. An accepted
  request counts toward the bridge's limit on open requests.
- **Failure behavior**:
  - Every refusal comes back at once, and nothing is written except the log row. As well as the
    planner's codes: `plan_changed` (the lines, their worst-case cost or the visit fee changed
    since the preview: preview again) and `submission_unresolved`.
  - Something a person must fix first: `success: false`, "Can't submit yet:" and what to do.
  - The bridge says too many requests are open (`429`): refused `too_many_open_requests`; wait for
    earlier ones to be decided.
  - The bridge refuses the request (another `4xx`): `success: false` with the bridge's reason;
    nothing was ordered.
  - Bridge unreachable: `success: false`, "nothing was sent"; the record is `not_filed`.
  - Sent but no usable answer (a timeout, a `5xx`, or an answer with no request ID): the record is
    `unknown`, the agent is told not to submit again, and every later preview and submit refuses
    `submission_unresolved` until a person checks the bridge and runs
    `python -m vending.submissions resolve`.
  - Accepted, but storing the answer fails (the database dropped mid-call): the request ID is
    written to the server's error log as a `LOST ANSWER` line, and the error the agent sees names
    the ID and the `resolve` command. The record stays `sending`, which blocks further submits.
  - The POST is never retried: a retry after a lost answer could order twice.
- **Owner**: Marshall Strong (issue #20, PR #37).

## `get_purchase_requests`

- **Inputs**: `pending_only` (default `false`).
- **Outputs**: `data`, our requests newest first: `request_id`, `status` (`pending`, `rejected` or
  `fulfilled`), `submitted_at`, `off_cycle`, `plan_hash`, `decision_note`, `total_paid`,
  `fulfilled_at`, `last_checked`, `plan` (the body we sent) and `lines` (each line with its receipt
  and any later losses). Also `pending` (how many) and `not_checked_recently`.
- **Preconditions**: none. With no requests it answers `success: true`, "No purchase requests
  yet."
- **Side effects**: one `action_log` row.
- **Cost**: database queries only; it never calls the bridge.
- **Failure behavior**:
  - It is only as fresh as the last `python -m vending.sync_requests` (the worker runs it every
    hour). A pending request not read from the bridge in 6 hours (counted from submission if
    never read) is listed in `not_checked_recently`, and the message names it and the command.
- **Owner**: Meaghan Gartland built it (PR #29). Issue #21 has no assignee.

## `get_project_rules`

- **Inputs**: optional `topic`, a plain name from the topic list (such as `pricing`).
- **Outputs**: with no topic, `data`, the topics and a one-line summary of each; with a topic,
  its `text`.
- **Preconditions**: the notes in `docs/agent/` ship with the code.
- **Side effects**: one `action_log` row.
- **Cost**: reads one local file. No bridge call.
- **Failure behavior**:
  - An unknown topic, or anything shaped like a path (`..`, slashes): `success: false` with the
    list of valid topics. No file outside `docs/agent/` can be read.
- **Owner**: Kevin Sykes built it (PR #63). It has no issue among #15 to #24.

## `request_catalog_product`

- **Inputs**: `name` and `size` (such as `"Coca-Cola Cherry"`, `"20 oz bottle"`); optional
  `line`, `flavor` and `note`. Each field 1 to 100 characters; `note` at most 500.
- **Outputs**: `success`; on acceptance `request_id` and `status` (`pending`, `approved` or
  `rejected`); on a refusal, `refusals`.
- **Preconditions**: the halt switch is off; the bridge's catalog can be read (for the duplicate
  check); the drink is not already in the catalog or already requested; we are under our weekly
  limit.
- **Side effects**: a product request on the bridge (`POST /api/v1/product-requests`), which lands
  with an instructor to decide. Our record: a `product_requests` row saved as `sending` before the
  POST, then updated with the bridge's answer, or deleted if the request certainly wasn't filed.
  One `action_log` row for every attempt.
- **Cost**: no money and nothing is ordered, but each request takes an instructor's time and
  counts toward the bridge's open-request limit. Bridge: one catalog read and one POST, each one
  try with a 20-second timeout.
- **Failure behavior**: every refusal comes back at once. Codes:
  - `bad_field`: a field too long or empty.
  - `not_a_drink`: the size isn't a liquid volume (`20 oz bottle`, `12 fl oz can`, `500 mL`).
  - `alcohol_or_thc`: the description names alcohol or THC (root beer and ginger ale are fine).
  - `already_in_catalog`: the same drink is in the catalog; order it by SKU instead.
  - `already_requested`: we already asked for it, including a request whose answer was lost.
  - `request_limit`: over our own limit on requests in any 7 days: 3, or 15 while the catalog has
    fewer than 25 products. **These numbers are placeholders the team has not set yet**
    (`WEEKLY_LIMIT`, `STARTUP_WEEKLY_LIMIT`, `SMALL_CATALOG` in
    `vending/tools/catalog_request.py`).
  - `too_many_open_requests`: the bridge answered `429`.
  - `halted`: the halt switch is on.
  - Bridge unreachable when reading the catalog: `success: false`, "nothing was sent".
  - The bridge refuses the POST (`4xx`) or can't be reached: `success: false`; our row is deleted.
  - Sent but no usable answer (a timeout, a `5xx`, no request ID): the row becomes `unknown`, the
    agent is told not to send it again, and the same drink is refused `already_requested` until a
    person checks the bridge and runs `python -m vending.product_requests resolve`.
  - The POST is never retried: a retry after a lost answer could file it twice.
- **Owner**: Kevin Sykes built it (PR #14); Marshall Strong fixed the bugs found in review
  (PR #33). Issue #24 has no assignee.

## `set_price`

- **Inputs**: `slot` and `sale_price`.
- **Outputs**: never succeeds.
- **Preconditions**: registered only while `VENDING_TOOLS` is `all` (the default). For the live
  run it is set to `built`, and then this tool is not offered to the agent at all.
- **Side effects**: one `action_log` row. Nothing else.
- **Cost**: none.
- **Failure behavior**: it runs through the write gate like any write, so while halted it is
  refused `halted`. Otherwise it returns an error saying it was dropped: prices can't change
  remotely, so set `sale_price` on that slot in `submit_desired_state`, and the worker changes the
  tag at the Friday visit (free) or a midweek visit (`off_cycle: true`, $25).
- **Owner**: dropped by Kevin Sykes (PR #46), closing issue #22, which had no assignee.

## Shared guarantees

These hold for every tool, because they are in the server and the gate, not in each tool or in
the prompt.

- **Every call is logged** to `action_log`: the tool, its arguments, the outcome (`done`,
  `failed` or `refused`) and the full reply. Reads are logged with `kind = 'read'`, so a reviewer
  can see what the agent saw before each decision. A call whose arguments fail validation never
  reaches the tool but is still logged as `refused`. A reply with `success: false` is logged as
  `failed`. Logging is our code writing to our database; it costs no model tokens.
- **Writes go through one gate**, `vending.write_gate.run_write`. In one transaction it checks the
  halt flag while holding a row lock on it, runs the write, and inserts the log row; the write and
  its log row commit together. A refused or crashed write is rolled back and then logged on its
  own, so every attempt leaves a row. Because the lock is held for the whole write, a halt turned
  on during a submit waits for that submit to finish, and every write that starts after it
  refuses `halted`. `tests/test_server.py` checks that every write tool refuses and is logged
  while halted.
- **The halt switch starts on** and is flipped only by a person:
  `python -m vending.halt on --by NAME` or `off --by NAME`. A missing flag counts as halted. The
  harder stop needs no database: stop the server process or remove it from the agent's config.
- **The pricing gate and the spend cap are enforced in code.** Every sale price passes
  `vending.pricing.check_price` (floor, ceiling, 5-cent steps), and every plan's worst-case cost,
  visit fee included, is checked against the spendable balance from the ledger. The planner runs
  again inside the gate at submit time, so the plan that is sent is the plan that was checked.
  Nothing in the prompt can loosen either check; the planner's settable limits (`price_ceiling_cents`,
  `tax_percent`, `midweek_fee_cents`, `capacity_overrides`) change only through
  `python -m vending.config set ... --by NAME`, which checks each value's range and logs who
  changed it.
- **Nothing sent to the bridge is retried**, and anything sent is recorded before it is sent, so a
  lost answer leaves a record a person settles rather than a duplicate order.
- **No tool calls the model.** Model spend (token cost) is recorded in the ledger by the agent
  harness and comes out of the same balance as stock.
