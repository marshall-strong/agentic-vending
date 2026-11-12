# The weekly agent run

`python -m vending.agent` runs the agent once: it starts our MCP server, lets the model call the tools, and records the run.

```
python -m vending.agent            # dry run (the default): write tools are hidden, no write is sent
python -m vending.agent --live     # the real run: the model may call submit_desired_state once
```

- **Halt switch:** if it is on, the run is recorded as `halted`. No model call is made and no tool is called. The switch is checked again before every model request, so turning it on mid-run stops the run at that point. A new database starts halted, so turn it off with `python -m vending.halt off --by <name>` first.
- **Model key:** the client is built from `VENDING_ANTHROPIC_API_KEY` and nothing else (see the README for where the team's key lives). Without it the run is recorded as `failed` before the server starts or any request is sent. `ANTHROPIC_API_KEY` is never read, so a run can't bill whoever is signed in on that machine, and no model key is passed on to the MCP server.
- **Spend:** every model request goes through `vending.harness`. Its worst case is in `model_calls` before it is sent, and the run cap (`model_run_cap_cents`) holds. **A live run is the competition's money:** it also goes in the ledger, under `model_daily_cap_cents`. **A dry run is test spend:** it spends real tokens, but under `model_test_daily_cap_cents` and outside the ledger, so practice runs don't use up the live cap or show in the P&L. `python -m vending.usage` shows both. A dry run's read tools still read from the bridge. Each request asks for prompt caching, because every turn re-sends the whole conversation.
- **A cut-off answer:** the harness lowers a request's `max_tokens` to fit what is left of the caps, so late in an expensive run the final answer can be cut off mid-JSON. That run is recorded as `failed` with a detail that says so. Raise the cap, or use a cheaper model or a lower effort.
- **Rules card:** the system prompt starts with [`docs/agent/rules-card.md`](agent/rules-card.md), read once when the run starts, and tells the model to call `get_project_rules` for details. If the card is missing or empty, the run is recorded as `failed` before the server starts or any model request is sent. The system prompt holds nothing that changes between runs (the time and notes are in the first message), so it can be read from cache. Note: the container image must include `docs/agent/`.
- **Tools:** the server runs with `VENDING_TOOLS=built`, so the model sees only tools that work. A dry run also hides `submit_desired_state`, `set_price` and `request_catalog_product`. If the model calls submit anyway, it gets "Not sent", and nothing reaches the server.
- **Live runs:** a live run refuses to start while the server has no `submit_desired_state` (#37). It sends only the plan its last `preview_desired_state` accepted, and adds that preview's `expected_plan_hash` itself. It sends at most one submit per run, and any later call gets "already submitted".
- **Notes:** the model's notes from the last finished run are in its opening message, labelled with where they came from. A live run reads only live runs' notes, so a dry run's "would submit" is never taken as done.

## What a run records

Each run is one row in `agent_runs`, which the dashboard reads:

| status | when |
|---|---|
| `submitted` | a `submit_desired_state` call succeeded. The `request_id` is stored right away, and the run stays `submitted` whatever stops it afterwards |
| `skipped` | the model chose to skip, or a dry run ended (the detail says whether it would have submitted) |
| `halted` | the halt switch was on |
| `failed` | the model key is missing, a cap was reached, the answer was cut off, it ran out of turns, a submit was refused, it said "submit" on a live run without submitting, or something broke. A detail that starts with **SUBMISSION UNRESOLVED** means the bridge may have accepted a request: check `python -m vending.submissions list` before anyone submits again |

`plan` is the last plan that previewed OK (with its `plan_hash`), or the plan that was submitted. **It also carries what the dashboard shows beside the plan:** `visit` (`scheduled` or `midweek`), the run's `reason`, and a `reason` on each slot the model said it changed (its final answer lists them as `slot_reasons`). A run that stopped before answering stores the plan without reasons. `team_update` and `notes` come from the model's final answer. `cost_usd` is what the harness spent, estimates included.

## Before the first run

A run reads the database `DATABASE_URL` points at, so each of these is per database:

 - [ ] `python -m vending.db migrate`: the run needs `agent_runs` and `model_calls`.
 - [ ] `python -m vending.halt off --by <name>`: a halted database records the run as `halted` and does nothing else, dry runs included.
 - [ ] `python -m vending.ledger open --amount 600.00 --by <name>`: until then `get_account_balance` and `preview_desired_state` can only tell the model the ledger isn't open.
 - [ ] `VENDING_ANTHROPIC_API_KEY` set in your environment, as the README describes.
 - [ ] The class tailnet, since the read tools reach the bridge.

**No run has been made against a real model yet.** The first dry run will show what a cycle costs. At the placeholder $1 run cap, a `claude-opus-5-5` run is likely to be squeezed in its later turns: each turn reserves its worst case (the whole conversation at the cache-write price plus 16,000 output tokens) before it is sent.

## Still to decide

- **Model, effort and budget:** the defaults are `claude-opus-5-5` at `medium` effort (`--effort` also takes `low`, `high`, `xhigh` and `max`), and the caps are $1 per run and $4 per day. All of them are placeholders.
- **Schedule:** when the worker starts the run (Thursday, before the midnight cutoff), and which flags start an off-cycle run. Until that is decided, run it by hand.
