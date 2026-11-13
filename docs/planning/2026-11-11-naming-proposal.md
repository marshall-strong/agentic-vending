# Naming proposal: the business, Rocco and the crew

**Status: a proposal for the team, 11 Nov 2026, revised 12 Nov after Marshall's review.** Nothing here is decided. Every name is a suggestion, and so is the "crew" way of describing the system. Change anything; comment on any line of this PR.
Visual version: [Meet Rocco and the Crew](https://claude.ai/artifact/2zci11rMobPqXMddJGxXyc).

## Why name things

We run 20 of the machine's 40 selections as a small business for nine weeks, and we present it in Summative 2. Names make the slides, the dashboard and the agent's logs easier to follow: "Rocco switched slot 36 to Celsius" reads better than "the planner returned action=switch". They can also help explain the design we have: one agent that decides, and the code around it that checks, sizes and writes things down.

## The business

Each option comes from something real about our setup: Rochester (the "ROC"), the campus at 150 Sawgrass Drive, and a machine of glass-front rails.

| Name | The angle |
|---|---|
| **Twenty Rails Beverage Co.** | We run exactly 20 rails. Sounds like a real company and fits a slide title. |
| **Sawgrass Sips** | From the campus address. Friendly and local. |
| **Cold Call Beverage Co.** | A business-school pun: every sale is a cold call. |
| **Thirst Quarter** | The competition runs one quarter. Cheeky, but still fine in front of David. |

## The agent: Rocco

**Rocco**, as in ROC-co: a name for the agent the team already built (#39, #58), the one model that makes the weekly call. It decides what goes in each of our 20 slots, at what price, whether to pull a slow seller, and whether a $25 midweek visit is worth it. It signs every weekly plan ("Rocco's call for Friday") and appears by name on the dashboard and in the logs.

## The crew: names for the parts around Rocco (proposed)

The team hasn't agreed on a "head agent with a crew" design. As built (#39, #58), the system is **one agent calling MCP tools, with the arithmetic in plain code**. Most of the "crew" below is code that already exists, so most of these would just be names for existing parts. Four would be new: Scout, the catalog-drafting half of The Buyer, The Doorman and The Oracle. Whether to build them, and whether any should be a model call, is the team's call.

| Name | The part it names | Exists today? |
|---|---|---|
| **Rocco** | The agent: reads the tools, decides the Friday plan within the guardrails, and explains every change in one line. | Yes: `vending/agent.py` (#58), every call through `vending/harness.py`. Model and effort are the `--model`/`--effort` flags, defaulting to Opus 5.5 pending a team decision. |
| **The Doorman** | Would watch the door-open counter (EVA-DTS `EA2*DO`) in every poll and flag a visit nobody planned. The counter dates **door openings**, not refills: refills are inferred from our slots' sales, and 11 of the 23 visits in the sales analysis show no sign of one. | No. Offline only, in `analysis/sales_analysis.py` and `analysis/visit_windows.py` (#105); nothing in `vending/` reads `EA2*DO` yet. |
| **Tally** | Matches sales against stock, catches counter misreads, keeps the stock estimate honest. | Yes: `vending/stock.py`, `vending/counters.py`. |
| **Scout** | Would write a weekly digest for Rocco: what sold, what ran empty, what's coming on the calendar (breaks, Thanksgiving). | Partly: `get_sales_history` and `get_machine_state` give the data. There's no calendar tool (the calendar is `analysis/golisano_calendar_2026_27.json`), and a model-written digest would be new. |
| **Tagger** | The pricing gate. It checks every price **Rocco proposes**, after him: between the floor and the ceiling, in 5-cent steps. It refuses a price; it doesn't write the physical tag. | Yes: `vending/pricing.py`, applied by `plan_restock` on every `preview_desired_state` and `submit_desired_state`. |
| **The Buyer** | The purchase lines on a request. Neither code nor the agent buys anything: the student worker shops on Friday. | Yes for the lines: `plan_restock` (`vending/planner.py`) sizes every purchase. Rocco files catalog requests himself with `request_catalog_product`; drafting them separately would be new. |
| **The Loader** | The worker's directive: what to load, pull and price in each slot. | Yes: `_directive` in `vending/planner.py`, written by `preview_desired_state`. |
| **The Oracle** | Would forecast next week's demand. | No. The sales analysis (#68) backtested 13 models on **daily units for the whole machine before the competition**, not on our slots: Holt-Winters was narrowly ahead of seasonal naive (MAE 5.78 against 5.89). During the competition a whole-machine model would take in the other selections' sales, which `docs/agent/competition-and-integrity.md` rules out for any decision, and #84 is to cut those totals off at the start. A forecast for our slots would be new work. |

**Code, not models.** Every part that exists today is plain code, and that's the right default: code is free, exact and testable. Putting the Loader's directive or the Buyer's purchase lines on a model would replace tested code with a guess. Where a model could help (a digest, a catalog draft), it's an open question.

**Names that could mislead**, from the review:
- "The Buyer" buys nothing. It sizes a request; the worker buys.
- "The Oracle" sounds certain. The brief and `docs/agent/inventory-belief.md` ask the agent to say how sure it is, and the tested model is a near tie with "same weekday last week".
- "Tagger" is close to the physical price "tag" the worker changes, but it only checks prices.
- "Worker" already means the student worker and the Container App process (`vending/worker.py`). So this doc calls these "parts", not workers.
- "The Doorman" sits next to the `door` source in `slot_counts`, which means a count taken with the door open.

## What the models cost

Splitting work across cheaper models sounds like a big saving. For us it isn't, and it's better to say so up front.

| Model and effort | One replay decision (one API call) | Nine weeks, one decision a week |
|---|---|---|
| Opus 5.5, low | $0.049 | about $0.45 |
| Sonnet 5.5, high | $0.028 | about $0.25 |
| Haiku 5.5, medium | $0.0016 | about $0.015 |

These are from the first benchmark sweep on 11 Nov: 9 setups (3 models × 3 efforts) × 10 test cases = 90 calls, $2.43 in total, every call recorded in `model_calls`. The output of `python -m vending.replay.sweep` is pasted in a comment on this PR. They are **a floor for live cost**: a replay decision is one API call, and a live run that calls MCP tools makes several calls per decision. Marshall's live test run 2 cost $0.40 (#81). Our development testing doesn't come out of the $600 (#102).

Graded blind against each case's good answer (out of 50), Opus at low effort scored 43, Sonnet at high 41, and Haiku at medium 40, at about a thirtieth of Opus's cost. That's one run of 10 made-up cases, so it's a hint, not a ranking. The standing benchmark (#66) is meant to firm it up.

## The other team's slots

We know which slots are **ours** (`vending/slots.py`, columns 5-8). `docs/agent/competition-and-integrity.md` says not to work out which other slots are a rival's, and the dashboard labels them neutrally ("not our selection"). The sales analysis uses whole-machine **totals** for the calendar, time of day, trend and door visits, but shows nothing per slot for the other 20. Label options with a bit of personality, none claiming who owns the rails:

| Label | Hover text |
|---|---|
| **Not our turf** | "Not one of our selections." |
| **Behind the velvet rope** | "Not ours. Rocco doesn't look." |
| **No peeking** | "Not our slot. Integrity rule." |

## What we need from the team

1. **A business name.** Pick one or suggest your own. It goes on the slides and in Summative 2.
2. **Rocco, or another name** for the agent.
3. **A label** for the selections that aren't ours.
4. **The parts.** Keep these names, merge some, or drop the crew framing. Which new parts (if any) to build, and whether any should be a model call.
5. **Which model and effort** for the agent. The default is Opus 5.5, pending this decision.
6. **Where a naming note lives**: the system prompt, the rules card, or a doc. `docs/agent/` holds notes on the brief, each citing a section, and `get_project_rules` reads only the topics in its README, so a naming note may not belong there.

Once the team picks, the names go into the labs and the dashboard as normal PRs.
