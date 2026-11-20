# Project pages (Claude artifacts)

The team's main pages about the vending project, in one place. These are live pages on claude.ai: each link always shows the latest version, so there is nothing to download or keep in sync.

Eight pages open only with a Golisano Claude sign-in, so use your institute account for them: the Vending Agent Roadmap, the Vending Agent Dashboard, the Day-One Plan Rounds, the Day-One Countdown, the Catalog Request Queue, the Slot Assignment, the Machine Sales Map and the Cutover Clock. The others open with any sign-in.

When you publish a new page about the project, add a row here in the same PR or a follow-up.

## Plan and status

| Page | What it shows |
|---|---|
| [Vending Agent Roadmap](https://claude.ai/artifact/AfECivFPXhKK6eagBZH5Ef) | Every work item with its owner and status, the critical path, open PRs and who's up next, decisions, risks, and a log of what changed. Each workstream's percentage shows its done count beside it. Last refreshed 19 Nov, about 8:30 PM ET, against main `33fa322` (live on Azure as revision 10) |
| [Project So Far](https://claude.ai/artifact/1VTEgX1ZKenp2WoBSD4Z6J) | The whole-team summary of the project to date |
| [Slot Assignment](https://claude.ai/artifact/BFJXdG2SfFtdSmydv8KkMc) | Which 20 of the machine's 40 selections we run (the inventory server's current assignment on vmc1, unchanged from 12 to 19 Nov), how to read a selection number, what happens if the split is redrawn before the 3 Dec start, what `record` does with the server's answer (#69, #146, #75), and whose sales data we may use before and after the start |
| [Vending Agent Tool List](https://claude.ai/code/artifact/82ae22ab-53a2-48c0-b4d5-c636efcc877d) | An older comment copy of [`planning/tool-list.md`](planning/tool-list.md), not kept up to date. Edit the repo file instead: it is the current version (agent tools, bridge API, open questions) |
| [Vending 19 Nov Recap](https://claude.ai/artifact/RmBM4LZhNLLKMcwZvKAQj1) | What changed on 19 Nov: why a can sold around a Friday swap can be the old drink or the new one, drawn to scale with the door times and a read every minute; the ten PRs merged that day (#168, #173, #174, #171, #170, #175, #148, #166, #176, #177); the deploys, ending with main `33fa322` live as revision 10; the switch to first-in-first-out costing (#176, closing #128); what's still open on #172; and what we only learn from the machine. Tracking issue [#172](https://github.com/marshall-strong/agentic-vending/issues/172) |
| [PR Atlas](https://claude.ai/artifact/3zzzgvg1e7jQhMyMiMhWgL) | The project's pull requests grouped by workstream on a timeline, with review status and open issues |
| [Vending Agent Dashboard](https://claude.ai/artifact/LjS75bStnTJgVRLF9hGGmW) | A sample snapshot of the agent dashboard in [`dashboard.md`](dashboard.md), rendered once from main `33fa322` (19 Nov evening, the build live on Azure as revision 10) with made-up test data, as if after a first week (9 Dec), with our 20 slots from the server's current assignment lit (it can still change before 3 Dec). It shows Honcho the mascot, the money panel (balance, where the $600 went, pending orders, profit since the opening), the machine close-up (each rail's stock and what goes back Friday), the plan, runs, tool calls, purchase requests, every catalog request with counts and open places against the bridge's limit, back stock (costed oldest cans first, #176), and the money chart ([`money-chart.md`](money-chart.md)) |
| [Day-One Plan Rounds](https://claude.ai/artifact/NfeMbbF34NDxQfW4vZQtkY) | The agent's day-one plan, one tab per round: our 20 slots on the machine, what changed since the round before, fill cost against the proposed $366 cap, projected gross profit and sell-out per slot with the projection's assumptions, the catalog requests it needs (#119) and the drinks the Interloper also sells (#116). Made with `python -m vending.plan_page --write`; see [`plan-page.md`](plan-page.md) |
| [Day-One Countdown](https://claude.ai/artifact/Et6cANEJjB5vv1JNnfvaW1) | Every date from 17 Nov to the first Friday restock on 4 Dec: the slot redraw, catalog approvals by Wed 25 Nov, the fill request by 23:59 Sun 29 Nov, stocking on Mon 30 Nov, going live on Thu 3 Dec and the first restock request that night, each with its status and what it's waiting on ([`planning/2026-11-17-david-answers.md`](planning/2026-11-17-david-answers.md)) |
| [Catalog Request Queue](https://claude.ai/artifact/T2vFjfjJYrEnXvG1cU9pjM) | Why our catalog requests are stuck: the bridge refused new ones at 24 and then 25 waiting (our filings of 15 and 17 Nov), and the API has no way to withdraw a request, so only a decision frees a place. Every request's status as of 17 Nov, the timeline since 11 Nov, which waiting drinks the day-one plan needs, and the 25 Nov approval deadline ([`planning/2026-11-17-david-answers.md`](planning/2026-11-17-david-answers.md)) |

## Data and sales history

| Page | What it shows |
|---|---|
| [Machine Sales Map](https://claude.ai/artifact/TEcYXfQpAomczgF58wZFUU) | All 40 selections on a picture of the machine front, from the fall (27 Aug to 15 Nov, before the 3 Dec start, when no selection was either team's), colored by drinks sold, takings, drinks a week or average price paid, with each selection's weekly sales and price history. The 20 in our current assignment have a dashed outline; the split may be redrawn before 3 Dec. Also whole-machine sales by week and by day and hour |
| [Vending Forecast Lab](https://claude.ai/artifact/WGfwq36gf8Yd11HszSmyyy) | Sales forecasting (PR #68) |
| [Campus Rhythm Lab](https://claude.ai/artifact/6KzxRRJrdJZV6PR7Ez81Yd) | Sales against the campus calendar, plus the slow-seller rule test and the draft guideline for the agent (PR #68) |
| [Slot and Price Lab](https://claude.ai/artifact/2aR4tSYARzmrqeRcvdQDph) | Slots and prices (PR #68) |
| [The Doorman's Log](https://claude.ai/artifact/MMfwiYd7BrG15FDjbngV1q) | Every visit to the machine and every refill, dated from the machine's own door counter |
| [Shelf Through Time](https://claude.ai/artifact/4JTTXx4J89RwaGoDfTMVTg) | Product history, from Sean's photos |
| [Cutover Clock](https://claude.ai/artifact/BSgBkggAC5PYBpdPqRHp1H) | David's product and price cutover problem from the 12 Nov class meeting (#97): drag two polls around a visit to see how the counters mix two setups, the 19 door visits from 21 Sep to 11 Nov with their refill grades and blind spots (#105), our plan for a visit at any time, and a calculator that splits a window's sales by the two prices. The written version is [`cutover.md`](cutover.md) |

## The agent

| Page | What it shows |
|---|---|
| [Meet Rocco and the Crew](https://claude.ai/artifact/2zci11rMobPqXMddJGxXyc) | The naming proposal for the business, the head agent and his crew (PR #76) |
| [Rocco's Friday](https://claude.ai/artifact/AMWoaVRxK5BZAYZftigsme) | One competition week step by step: who acts, with which tool or data, and what can go wrong |
| [Model Scorecard](https://claude.ai/artifact/H6TbeKM4VWi9yoDmt6UUYb) | The first model sweep as cost against score, with the 10 test cases explained (#64, #66) |
