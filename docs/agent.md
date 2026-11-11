# The weekly agent run

`python -m vending.agent` runs the agent once: it starts our MCP server, lets the model call the tools, and records the run.

```
python -m vending.agent            # dry run (the default): write tools are hidden, no write is sent
python -m vending.agent --live     # the real run: the model may call submit_desired_state once
```

- **Halt switch:** if it is on, the run is recorded as `halted`. No model call is made and no tool is called. The switch is checked again before every model request, so turning it on mid-run stops the run at that point. A new database starts halted, so turn it off with `python -m vending.halt off --by <name>` first.
- **Spend:** every model request goes through `vending.harness`. Its worst case is in the ledger before it is sent, and the run and daily caps (`model_run_cap_cents`, `model_daily_cap_cents`) hold. A dry run spends real tokens too, and its read tools still read from the bridge. Each request asks for prompt caching, because every turn re-sends the whole conversation.
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
| `failed` | a cap was reached, it ran out of turns, a submit was refused, it said "submit" on a live run without submitting, or something broke. A detail that starts with **SUBMISSION UNRESOLVED** means the bridge may have accepted a request: check `python -m vending.submissions list` before anyone submits again |

`plan` is the last plan that previewed OK (with its `plan_hash`), or the plan that was submitted. `team_update` and `notes` come from the model's final answer. `cost_usd` is what the harness spent, estimates included.

## Still to decide

- **Model, effort and budget:** the defaults are `claude-opus-5-5` at `medium` effort (`--effort` also takes `low`, `high`, `xhigh` and `max`), and the caps are $1 per run and $4 per day. All of them are placeholders.
- **Schedule:** when the worker starts the run (Thursday, before the midnight cutoff), and which flags start an off-cycle run. Until that is decided, run it by hand.
