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
- **Rules card:** the system prompt starts with [`docs/agent/rules-card.md`](agent/rules-card.md), read once when the run starts, and tells the model to call `get_project_rules` for details. If the card is missing or empty, the run is recorded as `failed` before the server starts or any model request is sent. The system prompt holds nothing that changes between runs (the time, briefing and notes are in the first message), so it can be read from cache. Note: the container image must include `docs/agent/`.
- **Startup read:** after the halt, rules-card and key checks, code calls `get_briefing` once before the first model request. Its compact Markdown follows the time and mode, before the prior notes, labelled as a trusted startup read with its `computed_at` and `snapshot_id`. It uses the stored ledger, stock, polls and request status without a model call. Source ages remain visible even when the briefing was generated just now. If the read fails, the opening says so and directs the model to the existing pull tools. Its competition figures count only from the ledger opening, like `get_account_balance` and `get_pnl`. It does not replace `get_account_balance` every run, the request check before another order, or preview before submit. See [the startup-read note](agent/startup-read.md).
- **Tools:** the server runs with `VENDING_TOOLS=built`, so the model sees only tools that work. A dry run also hides `submit_desired_state`, `set_price`, `request_catalog_product` and `set_wildcard_slot`. If the model calls submit anyway, it gets "Not sent", and nothing reaches the server.
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

**Every step is recorded too**, in `agent_steps` (migration 014), as it happens: the model's reasoning, any text it wrote, each tool call and each tool result (cut to 4,000 characters). The reasoning is the summary the API returns when asked with `thinking: {display: "summarized"}`; the raw chain of thought is never available. The dashboard shows them under each run as **How it decided**. A step that can't be stored is reported on stderr and the run goes on.

The startup read has its own first `tool_result` step for `get_briefing`, labelled `source: "startup"`, with success or failure and the snapshot reference. It has no model-selected `tool_call` step. The full typed snapshot and rendered text are in the MCP read's `action_log` result.

`plan` is the last plan that previewed OK (with its `plan_hash`), or the plan that was submitted. **It also carries what the dashboard shows beside the plan:** `visit` (`scheduled` or `midweek`), the run's `reason`, and a `reason` on each slot the model said it changed (its final answer lists them as `slot_reasons`). A run that stopped before answering stores the plan without reasons. `team_update` and `notes` come from the model's final answer. `cost_usd` is what the harness spent, estimates included.

## When it runs unattended

The worker (`python -m vending.worker`, the Azure container) starts the agent on a schedule, but only when `AGENT_SCHEDULE_MODE` is set. **Unset, it never runs the agent**, and the worker behaves as before. The code is `vending/agent_schedule.py`. It calls `vending.agent.run`, the same entry point as `python -m vending.agent`, so the halt switch, the caps and the records are the same.

| Setting | Default | |
|---|---|---|
| `AGENT_SCHEDULE_MODE` | unset (off) | `off`, `dry-run` or `live`. `dry-run` is test spend; `live` may submit a purchase request |
| `AGENT_SCHEDULE` | `wed 14:00-22:00` | Eastern times, comma-separated, as `day HH:MM-HH:MM`: when the run is due, then the latest it may start. The default is one restock run each Wednesday at 2 PM, so its order is in before Amazon's Wed 23:59 cutoff (#188) |
| `AGENT_SCHEDULE_FROM` | `2026-12-02` | The first Eastern day with runs |

The times and the first day are placeholders until #129 decides them. The times follow Eastern wall-clock time through daylight saving.

- **Turn it on:** set `AGENT_SCHEDULE_MODE=dry-run` (or `live`) on the container app, as [`docs/deploy.md`](deploy.md) shows. The worker reads it when it starts, so a new revision is needed. **Turn it off:** set it to `off`. The halt switch also stops a run that has started, and is the faster way.
- **One run per time:** each time in the schedule gets one row in `agent_schedule` (migration 022), keyed by the time, before the agent starts. A restart, or a second replica, never runs the same time again. A database lock keeps two scheduled runs from going at once, and a scheduled run is not started while another agent run (one started by hand too) is still `running`. That time is recorded as `skipped`.
- **A missed time:** if the worker was down at the time, a restarted worker records it as `missed`. If it comes back before the latest start, it runs late, and the row's detail says how late. If a worker stopped mid-run, the next one records that time as `failed`. It is never run again.
- **What is recorded:** the row's `outcome` is the run's own ending (`submitted`, `skipped`, `halted`, `failed`), or `missed`, or `running` while it goes on, with the `agent_runs` id. A `skipped` or `halted` run is a result, not a success.

**See the last run:** `python -m vending.agent_schedule` prints the settings as that machine reads them, the next time and the latest rows with their details. The worker's `/health` and its home page show the mode, the next time and the last scheduled run's outcome (never its detail). The dashboard's Needs attention says when the last one failed, was missed or was halted, or when a setting is wrong. The run itself is in the dashboard's runs, like any other.

## Before the first run

A run reads the database `DATABASE_URL` points at, so each of these is per database:

 - [ ] `python -m vending.db migrate`: the run needs `agent_runs` and `model_calls`.
 - [ ] `python -m vending.halt off --by <name>`: a halted database records the run as `halted` and does nothing else, dry runs included.
 - [ ] `python -m vending.ledger open --amount 600.00 --by <name>`: until then `get_account_balance` and `preview_desired_state` can only tell the model the ledger isn't open.
 - [ ] `VENDING_ANTHROPIC_API_KEY` set in your environment, as the README describes.
 - [ ] The class tailnet, since the read tools reach the bridge.

**The first runs against the real model were made on 11 and 12 Nov**, with `claude-opus-5-5` at `medium` and the run cap raised to $5 for testing: a dry run that cost $0.80 over 8 model calls, a live run that cost $0.40, and a dry run that cost $0.08. None was cut off. The caps below are still placeholders, so set them from what these runs cost (`python -m vending.usage` shows each run). At the placeholder $1 run cap, a run like the first would likely be squeezed in its later turns: each turn reserves its worst case (the whole conversation at the cache-write price plus 16,000 output tokens) before it is sent.

## Still to decide

- **Model, effort and budget:** the defaults are `claude-opus-5-5` at `medium` effort (`--effort` also takes `low`, `high`, `xhigh` and `max`), and the caps are $1 per run and $4 per day. All of them are placeholders.
- **Schedule:** the times the worker starts the run (the placeholder is Wednesday 2 PM, above), whether it runs `dry-run` or `live`, and which flags start an off-cycle run (#129, #219).
