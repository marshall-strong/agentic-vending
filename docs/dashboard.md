# The agent dashboard

A read-only web page for watching the agent while it runs: is it halted, what is it doing, what
did it plan, and what changed since last week.

```
python -m vending.dashboard              # then open http://127.0.0.1:8765
python -m vending.dashboard --port 9000
```

It reads whatever database `DATABASE_URL` points at: your local one, or the team's Azure
database. The page refreshes every 30 seconds. `/state.json` returns the same data as JSON, for a
script or another agent.

![The dashboard with sample data](dashboard/current.png)

*Sample data from a throwaway database, not a real run.*

## What it shows

The page answers four questions, top to bottom: is it OK, what is it doing, what did it plan, and
what changed and why.

| Part | What's in it | Where it comes from |
|---|---|---|
| Verdict | **OK**, **Check** or **Problem** next to the title (and in the browser tab), from the worst item in Needs attention | everything below |
| Tiles | The halt switch and who last changed it; the latest run and how long it has run; a countdown to the Thursday 23:59 Eastern cutoff; how old the last machine poll is and units sold since; what has been spent on drinks (fulfilled requests) and on the model (run costs) | `config`, `agent_runs`, `polls`, `purchase_requests` |
| Needs attention | One sentence per thing a person should look at, worst first. **Problem:** the latest run failed, or has said `running` for over 2 hours. **Check:** refused or failed tool calls in the last 24 hours; polls over 3 hours old; a catalog request stuck in `sending` or `unknown`; the latest purchase request rejected; and within 24 hours of the cutoff, still halted or no Friday request sent. **Info** (doesn't change the verdict): halted, no polls yet, stock warnings from the receipts, a panel whose table is missing | the snapshot |
| The machine | Every slot, laid out like the machine (rows 1 to 5, columns 5 to 8): the latest plan's product, price and fill, with the price on the tag now struck through when the plan changes it; units on hand as a bar against the fill target; units sold this week; the agent's reason. Slots that changed since the plan before are outlined, tagged (swap, $2.50 to $2.75, fill 6 to 8, pull) and say what they were. Under it, the weekly update for the team and the notes to itself | `agent_runs`, `vending.stock` (receipts and polls), `get_machine_state` (polls) |
| Runs | The last 10 runs as a timeline: mode, model, how long, cost, request, how it ended, and chips for every slot its plan changed from the plan before it | `agent_runs` |
| Tool calls | Counts for the last 24 hours, then the last 50 calls. Writes are marked W and shaded; refused and failed calls are red. Filter to writes or to refused and failed; each call's arguments open on a click | `action_log` |
| Requests | Our last 10 purchase requests (status, what was paid, lines loaded, unavailable and alternates) and catalog requests | `purchase_requests`, `purchase_lines`, `product_requests` |
| Back stock | What the receipts say is in the closet, and what each unit cost us | `vending.stock` |

A slot counts as changed when what the plan asks for changes (product, fill, price, keep). A
reworded reason alone is not a change. Status and the machine follow `live` and `dry-run` runs
only, and a plan is compared only with the last plan of the same mode; replays of past weeks show
under Runs, dimmed. Model spend counts `live` and `dry-run` runs.

The page refreshes itself in place every 30 seconds, keeping your scroll position, any open
arguments and the tool-call filter. If a refresh fails (the database is down) it keeps the last
page and says how old it is. Without JavaScript it reloads the whole page instead.

**Not shown yet:** the balance and profit and loss. They arrive with the ledger (#35); a tile is a
small addition once it merges. "Spent so far" is only what the receipts and run costs record.

## Safety

- **It can't write.** Its connection sets `default_transaction_read_only`, so Postgres refuses
  any write, and it never migrates. A table that doesn't exist yet blanks only its own panel.
- **It listens on 127.0.0.1 only, and has no login.** It answers only requests addressed to
  `127.0.0.1` or `localhost`, so another website can't point its own name at your machine and
  read the page. To watch the Azure container from a laptop, use an SSH tunnel or run it locally
  against the Azure database; don't expose it.
- **It costs nothing to run.** No model calls, no new services, no new packages.

## The run record

The plan, update and notes come from `agent_runs` (`db/migrations/009_agent_runs.sql`). Nothing
writes it yet: the agent loop (#39) should call `vending.agent_runs` at the start and end of each
run, and the agent replay can do the same with `mode="replay"`:

```python
from vending import agent_runs

run_id = agent_runs.start(conn, mode="dry-run", model="claude-sonnet-5-5")
# ... the run ...
agent_runs.finish(conn, run_id, "submitted", plan=plan, update=update, notes=notes,
                  cost_usd=Decimal("0.42"), request_id=request_id)
```

`status` is one of `submitted`, `skipped`, `halted` or `failed`. `plan` is the replayer's decider
shape (`docs/week-replayer.md` on #28). `cost_usd` must be a `Decimal`, and a `Decimal` price in the plan is stored as exact text
(`"2.50"`). A run finishes once; a second `finish` raises.

Times on the page show in your browser's time zone. On Windows, the cutoff date needs the
`tzdata` package (`pip install tzdata`); without it the page says "Thursday 23:59 Eastern".

## Where it could go next

Mockups with made-up numbers, for the team to react to. None of this is built.

**A. Headline numbers and stock.** Tiles for the halt switch, the cutoff countdown, the balance
and the week's profit, and a stock bar in each slot. The tiles need the ledger (#35) and token
spend (#40); the stock bars can use `vending.stock` today.

![Idea A: headline tiles and stock bars](dashboard/idea-a-machine.png)

**B. Plan history.** One card per week: the update, the slots that changed, the notes, what it
cost. Everything it needs is in `agent_runs` once #39 writes it.

![Idea B: plan history, week by week](dashboard/idea-b-history.png)

**C. A live run.** Each tool call as it happens, the draft plans as they change, and the run's
budget. The tool calls are already in `action_log`, and every `preview_desired_state` call logs
the draft plan it checked, so "plan v1 to v2" can be read from it. It needs a run id on
`action_log` rows to group them, and the budget needs #40. The model's reasoning between calls
isn't recorded anywhere yet.

![Idea C: a live run](dashboard/idea-c-live-run.png)
