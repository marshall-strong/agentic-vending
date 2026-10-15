# Vending agent build plan

Modeled on our Data Systems final project, the Equipment Rental MCP server.
**Revised 21 September after the Design Brief meeting with Graham and David.** The
meeting answers are in [`2026-09-21-design-brief-meeting.md`](2026-09-21-design-brief-meeting.md).

Short version: **build an MCP server with tools, and we have built this pattern once
already.** Last quarter's project gives us about half of the architecture. Monday's
meeting filled in most of the rest: where the data comes from, how purchasing works, and
how we find out what was loaded into the machine.

**Running the agent costs money, and that money comes from the same $600 that buys our
stock.** Every call to the AI model uses tokens. The token bill is charged to our
account exactly like a $25 dispatch fee, and it counts against our profit. A dollar
spent on tokens is a dollar we can't spend on drinks. That's why the plan keeps
arithmetic in plain code, calls the model only where judgment is actually needed, keeps
the tool list short, and logs token usage from day one.

Note: this plan contains strategy. It was kept out of this repo at first because the repo
is public; on 15 October we decided to publish it here anyway so the whole team works
from one place. This is the 21 September version. Section 4's tool table is superseded
by [`tool-list.md`](tool-list.md), which reflects the purchasing API as actually built.

## What changed on 21 September

| Before the meeting | After |
|---|---|
| We had to build our own polling archive, and any day we missed was lost for good | **The bridge stores every poll, from anyone, back to 28 August.** `GET /api/v1/history` lists them and `/history/{session_id}/raw` returns each one, without touching the machine. |
| The agent places orders with a retailer | **The agent submits a purchase order** by Thursday midnight. A student worker buys it on Friday, restocks, and reports back. The worker can change or refuse a PO. |
| Stock level is invisible (`PA1-08` is empty), so we'd have to estimate it | **The restock report says what went into each slot.** Stock = loaded − sold since. |
| Price floor and ceiling come from the instructors | **There aren't any.** The brief still requires price bounds as a guardrail, so **we set our own**. |
| We might build a restocker interface | **They built the purchaser portal.** Not ours to build. |
| Capacity is 7 or 8 per spot | **No fixed capacity.** Spots are slanted rails, so as many as physically fit. |

## 1. What carries over directly from Data Systems

**The server pattern.** An n8n `MCP Server Trigger` with one `toolWorkflow` node per
tool, each pointing to a sub-workflow shaped like this:

```
Execute Workflow Trigger  ->  Postgres (one SQL statement)  ->  Code (format the reply)
```

**The harness.** `Data Systems/.../mcp_agent.py` is already an agent loop. It runs
Claude with Anthropic's MCP connector against an n8n server and prints `usage` token
counts. The vending agent is that script plus a schedule, a new system prompt, and
usage saved to the database.

**The reply envelope.** Every tool returns `{success, message, count, data}`, and each
failure message names the tool that fixes it.

**The guardrail pattern.** `create_rental` checks its preconditions and does the write
in one SQL statement, and the `INSERT` only fires when every check passes. That's the
brief's "enforce this in code, at the gate every order must pass through, not in a
prompt." It gets reused for every write.

**Compute from facts, not stored labels.** `get_overdue_rentals` works out overdue from
the dates because the status column goes stale. Here the same rule means: stock comes
from loaded minus sold, and the balance comes from the ledger.

**The team process.** Four capabilities, four owners, and `n8n-tool-build-guide.md`,
reused almost unchanged.

## 2. What is different, and where the old pattern breaks

1. **Last quarter the server was the product. This quarter the agent is.** It runs
   unattended, so it needs a scheduler, a halt switch, and a log we can reconstruct
   events from.
2. **Purchasing is asynchronous and has a human in it.** The agent submits a PO, gets a
   PO ID back, and learns the outcome later by checking the PO's status. Meanwhile the
   purchaser may pay a different price, substitute an item, or refuse the PO. So the
   agent has to be built around "what I asked for" and "what actually happened" being
   different. That's why every PO carries one or two alternates.
3. **n8n Cloud still can't reach the machine.** The history endpoints are on
   `aihub-pi`, which is on Tailscale only. So a small job on a Tailscale machine copies
   the data into Postgres, and the n8n tools only talk to Postgres. That now includes
   the purchasing endpoints if they're also on the Pi. Where those endpoints live is
   still to be confirmed.
4. **Writes cost real money.** A bad PO spends the $600, so every write is gated.
5. **Tool descriptions cost money.** Every tool definition is sent on every request, and
   those tokens come out of the same $600 that buys stock. So we aim for about 10 tools
   rather than 17, with precise but tight descriptions and prompt caching.
6. **Rival visibility ends next quarter.** This quarter we can see every selection.
   During the live run we probably won't see the other team's prices. **Pricing must not
   depend on rival prices.** Treat them as a bonus signal when available, never a
   required input.
7. **Reconsider "not n8n".** It's on the syllabus, we already know it, and routing
   through Postgres removes the one real obstacle.

## 3. Proposed architecture

```
 aihub-pi (DEX Bridge)                      Tailscale only
   /api/v1/history, /history/{id}/raw         <- every poll since 28 Aug, hourly now
   purchase-order endpoints (coming)          <- location TBC
        |
        v
 sync job on a Tailscale machine  (replaces our live poller)
        |   pulls new history sessions, parses, pushes POs, pulls PO status
        v
 Postgres:  readings . products . purchase_orders . slot_loads
            ledger . action_log . config (halt, price bounds)
        ^
        |   (database only)
 n8n MCP server: vending tools
        ^
        |   Anthropic MCP connector
 vending_agent.py  (grown from mcp_agent.py, on a schedule)
```

**The tool set is the guardrail boundary.** The model can only act through its tools,
and there's deliberately no general "call any URL" tool.

**Our own poller should become a history sync.** The central store already has
everything, and now includes an hourly baseline. Our 30-minute live poller adds load to
a shared machine that's taking real money, and on 21 September it missed 12:07 and
15:07–17:07 because it only runs while the laptop is logged on. The central history had
those hours. Keep one live read only as a fallback, if we find a real need for it.

## 4. Tools, by workstream

| Workstream | Tool | R/W | What it does | Modeled on |
|---|---|---|---|---|
| A. Perception and inventory | `get_machine_state` | R | Per slot: current price, units sold since the last load, stock on hand (= loaded − sold) | `search_equipment` |
| | `get_sales_history` | R | Units and ex-tax revenue per slot over a window | `get_rental_history` |
| B. Pricing | `set_price` | W | Queues a price change for the **next service visit** (prices are physical tags the worker changes). **Gated on our own floor and ceiling** | `update_customer` |
| C. Purchasing and restocking | `get_account_balance` | R | From our ledger, never a stored figure | `get_overdue_rentals` |
| | `preview_purchase_order` | R | Prices a PO without submitting it | `estimate_rental_cost` |
| | `submit_purchase_order` | W | Item, link, quantity, **slot, sale price, 1–2 alternates**. Gated on balance, halt, Thursday-midnight cutoff, and the product existing in the shared product table. Returns a PO ID | `create_rental` (the gate) |
| | `get_purchase_order_status` | R | Pending, or what was actually bought, at what cost, loaded where, what went to backstock | `get_item_service_history` |
| | `request_service_visit` | W | Asks for a **$25 midweek visit**. One visit covers any mix of the three jobs (restock, swap a product out, change prices), so batch them. Gated on balance and halt, and on no visit already pending | `log_cleaning` |
| D. Reporting and books | `get_pnl` | R | Ex-tax revenue, average-cost COGS, dispatch fees, compute | `get_inventory_report` |
| | `check_books` | R | Sends our figures to the instructors' check endpoint and reports "matches" or "off by $X" | new |

Ten tools, against 17 last time.

- **The PO is now the directive.** It carries the slot, the quantity and the sale price,
  which is exactly what the brief's "restock and pricing directive" requires. It's
  written for the purchaser, so the brief's test still applies: someone who has never
  seen our code must be able to act on it without asking.
- **Logging is not a tool.** Every write adds its `action_log` row inside the same
  statement.
- **Competitor prices** are part of `get_machine_state` while they're visible, and the
  tool is written so it still works when they're not.

## How the machine gets changed: service visits

The worker does three jobs on a visit: **add stock**, **remove a product** (to replace it with something else), and **change prices**. Prices are black plastic tags, changed by hand. **Friday's visit is free. Any midweek visit costs $25**, whatever it's for.

This affects the design:

 - **Prices can only change when someone visits.** The agent can't adjust prices between Fridays. A midweek flash sale costs $25, the same as a midweek restock.
 - **Batch everything into one visit.** A $25 visit that restocks, swaps and reprices at once costs the same as one that changes a single price. `request_service_visit` should collect every pending change before it asks.
 - **Verify every price change.** The next poll's `PA1-02` shows the price the machine is actually charging. If it doesn't match what we asked for, the change wasn't made, or the tag and the machine disagree.
 - **Removing a product needs a plan for the removed stock.** Do the cans go to backstock, get written off, or get sold off? Still open.

Source: Marshall's understanding from class discussions, not the recorded meeting.

## 5. Guardrails, and where each one lives

| Guardrail | Enforced in | Why the agent can't get around it |
|---|---|---|
| Spend only what you have | `submit_purchase_order` and `request_service_visit` SQL: insert only if the ledger covers it | Check and write are one statement |
| Price bounds (**ours**) | `set_price` and `submit_purchase_order` SQL, against `config.price_floor` and `config.price_ceiling` | No other path sets a price |
| **Prices in 5-cent steps** (course rule) | Same SQL: reject unless `price_cents % 5 = 0` | $2.00 and $2.05 are allowed; $1.63 is rejected, not rounded |
| Halt switch | `config.halted` checked in every write, **plus** switching off the MCP workflow in n8n | The second one needs nothing from the agent |
| Full logging | `action_log` insert inside every write | An unlogged write can't happen |
| Approved channel | `submit_purchase_order` is the only way to buy | And a human purchaser reviews every PO |

**Prices must end in 0 or 5.** This is a course rule from class discussion: $2.00 or $2.05, never $1.63. Three consequences:
 - **The smallest possible undercut is 5 cents.** Undercutting by a cent isn't possible.
 - **Round the floor up to the next 5 cents, never down.** If cost plus tax comes to $1.63, the floor is $1.65. Rounding down would sell at a loss.
 - **The tools reject off-step prices rather than silently rounding them**, so the agent sees its mistake in the tool's error message and picks a legal price itself.

**Setting our own price bounds.** Proposal:
- **Floor:** never sell below average unit cost plus tax, so we can't lose money on a
  unit sold.
- **Ceiling:** a flat cap the team agrees on (for example $6.00) that stops a runaway
  price.

Both are stored in `config` so they can be changed without editing code. The team needs
to decide the actual numbers.

**Tax: don't hard-code it yet.** The accounting lecture treats the shelf price as
tax-inclusive (a $3.00 sale is $0.22 of tax). In the meeting it was spoken as "8% of the
$3" ($0.24). The official method is still owed to us, so the tax calculation lives in one
function we can swap out, with arithmetic rounding (confirmed, not banker's rounding).

## 6. Order of work

1. **History sync, replacing the live poller.** Backfill everything since 28 August.
   This is quick, and it immediately gives us more data than our own archive has.
2. **Parser and tables.** Validate `G85`, apply the `ID4` decimal position, store only
   the lifetime counters.
3. **`get_machine_state` and `get_sales_history`**, tested through `mcp_agent.py`. This
   is the smallest vertical slice that works end to end.
4. **Ledger, `get_account_balance`, `get_pnl`.** Nothing that spends can be built before
   this.
5. **Product table and the PO tools**, once the instructors deliver the schema and
   endpoints. Build against their template, not a guess.
6. **`check_books`**, once the check endpoint exists.
7. **Harness:** schedule it, log token usage, add the halt check. Run the first full
   cycles against a sandbox budget.

Draw the process map before step 3. Section 3 already gives its states and tool
boundary.

## 7. What we're waiting on

**From Graham and David** (they said these are coming):
- The PO template and endpoints: submit, and status
- The balance check endpoint
- The official sales-tax method
- The shared product table
- A ruling on unsold inventory at the end of the run
- A ruling on repo visibility
- Confirmation of the Thursday / Friday timeline

**Still to ask — not covered in the meeting:**
- Which of the two topics in each discussion is ours. (Group is settled: we are group 2,
  so readings go out Thu 1 Oct and we lead Discussion 1 on Thu 8 Oct.)
- How the agent learns its rows
- The bottle deposit
- Whether a Postgres database is provided
- Whether n8n runs count as compute
- Whether Visio is required
- Whether they expect MCP
- What promotion is allowed
- Whether "fireworks.ai / open-weight models" was a recommendation
