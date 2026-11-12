# Naming proposal: the business, Rocco and the crew

**Status: draft for the team, 11 Nov 2026.** Every name here is a suggestion. Change anything; comment on any line of this PR.
Visual version: [Meet Rocco and the Crew](https://claude.ai/artifact/2zci11rMobPqXMddJGxXyc).

## Why name things

We run 20 of the machine's 40 selections as a small business for nine weeks, and we present it in Summative 2. Names make the slides, the dashboard and the agent's logs easier to follow. "Rocco pulled slot 36" reads better than "the planner returned action=pull". They also make the architecture easy to explain: one head agent that decides, and a crew of narrow jobs around him.

## The business

Each option comes from something real about our setup: Rochester (the "ROC"), the campus at 150 Sawgrass Drive, and a machine of glass-front rails.

| Name | The angle |
|---|---|
| **Twenty Rails Beverage Co.** | We run exactly 20 rails. Sounds like a real company and fits a slide title. |
| **Sawgrass Sips** | From the campus address. Friendly and local. |
| **Cold Call Beverage Co.** | A business-school pun: every sale is a cold call. |
| **Thirst Quarter** | The competition runs one quarter. Cheeky, but still fine in front of David. |

## The head agent: Rocco

**Rocco**, as in ROC-co. He makes the weekly call: what goes in each of our 20 slots, at what price, whether to pull a slow seller, and whether a $25 midweek visit is worth it. He signs every weekly plan ("Rocco's call for Friday") and appears by name on the dashboard and in the logs.

## Rocco's crew

Each crew member is one narrow job. Some are cheap model calls; most are plain code with no model at all.

| Crew | Job | Runs on |
|---|---|---|
| **Rocco** | Head agent. Reads what the crew reports, decides the Friday plan within the guardrails, and explains every change in one line. | Sonnet or Opus, weekly |
| **The Doorman** | Watches the machine's door-open counter (EVA-DTS `EA2*DO`, in every poll), logs each visit, and flags a visit nobody planned. The counter dated every refill this term, including an unexplained visit on Nov 11. | Code |
| **Tally** | Matches sales against stock, catches counter misreads, keeps the stock estimate honest. | Code |
| **Scout** | Weekly digest for Rocco: what sold, what ran empty, what's coming on the calendar (breaks, Thanksgiving). | Haiku |
| **Tagger** | Checks every price before Rocco sees it: between the floor and the ceiling, in 5-cent steps. | Code |
| **The Buyer** | Drafts catalog requests and turns Rocco's plan into a shopping list. | Haiku |
| **The Loader** | Writes the student worker's restock instructions from Rocco's plan. | Haiku |
| **The Oracle** | Forecasts next week's demand with the Holt-Winters model from the sales analysis (#68), the most accurate of 13 models on our history, narrowly ahead of seasonal naive. | Code |

## What the split does and doesn't save

Splitting into a head and cheaper workers sounds like a big cost saving. For us it isn't, and it's better to say so up front.

| Model | One restock decision | Nine weeks, one decision a week |
|---|---|---|
| Opus | about $0.05 | about $0.50 |
| Sonnet | about $0.02–0.03 | about $0.25 |
| Haiku | about $0.002 | about $0.02 |

These come from the first benchmark sweep on 11 Nov (90 calls, $2.43 in total, every call recorded in `model_calls`). Model spend does come out of profit, but at these sizes it's pocket change next to a single sale.

- **Most crew jobs should be code, not models.** The build plan already says so: arithmetic in plain code, the model only where judgment is needed. Code is free, exact and testable.
- **The real win from a crew is reliability.** A worker with one narrow job, a short prompt and its own benchmark case makes fewer mistakes than one big prompt doing everything. It's also easier to explain and defend in Summative 2.
- **Rocco on Sonnet or Opus.** In the benchmark, Haiku at medium effort scored nearly as well as Opus on the restock decision. Rocco runs only once a week, though, and judgment is where bigger models earn their cost. The standing benchmark (#66) will tell us if that's wrong, and switching is one setting.

## The other team's slots

Our analysis and dashboard show only our 20 slots, as `docs/integrity.md` requires. The other 20 appear hatched out with no numbers. We know which slots they are; showing nothing about them is how we stay fair. Label options with a bit of personality:

| Label | Hover text |
|---|---|
| **Not our turf** | "Rival rails. Eyes front." |
| **Behind the velvet rope** | "Their 20. Rocco doesn't look." |
| **No peeking** | "Not our slot. Integrity rule." |

## What we need from the team

1. **A business name.** Pick one or suggest your own. It goes on the slides and in Summative 2.
2. **Rocco, or another name** for the head agent.
3. **A label for the other team's slots.**
4. **The crew.** Keep the split as drawn, or merge jobs. The names are the easy part. Which jobs are code and which are models is the part worth arguing about.

Once the team picks, the names go into the labs and the dashboard, and a short naming note goes into `docs/agent/` (the rules the agent reads). Each change goes up as a normal PR.
