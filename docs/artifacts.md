# Project pages (Claude artifacts)

The team's main pages about the vending project, in one place. These are live pages on claude.ai: each link always shows the latest version, so there is nothing to download or keep in sync.

The pages in use come first. Pages whose job is done, or that describe our slots before the 20 Nov redraw, are under [Older pages](#older-pages) at the bottom. They are kept for the record, not deleted.

Pages marked † open only with a Golisano Claude sign-in, so use your institute account for them. The others open with any sign-in.

When you publish a new page about the project, add a row here in the same PR or a follow-up. When a page stops being useful, move its row to Older pages and say why.

## Plan and status

| Page | What it shows |
|---|---|
| [Vending Agent Roadmap](https://claude.ai/artifact/AfECivFPXhKK6eagBZH5Ef) † | Whether the agent can run a week on its own, and exactly where it can't (rebuilt 21 Nov, tracked in [#221](https://github.com/marshall-strong/agentic-vending/issues/221)): readiness by deadline from 29 Nov to 5 Dec, the agent's week as an animated clock, data flowing through the system and stopping where a link is missing, a replay of a real run step by step, every capability by built, tested, scheduled and proven live, and each gap with the date it bites and its issue. Below that, in tabs: every work item, the critical path, open PRs, decisions, risks and a log of what changed. Refreshed 22 Nov: the ranked gaps after the eight readiness PRs merged and main was deployed to Azure at 2:07 PM, and the open PRs (drafts #231 and #233) |
| [Vending Strategy](https://claude.ai/artifact/HYmwHFEaPpoqV7rZ7kiKGL) † | The strategy on one page: readiness, the weekly loop, the day-one plan, variety, pricing, money and the open questions. Refreshed 22 Nov: round 7b as the day-one plan, the variety rules decided in [#230](https://github.com/marshall-strong/agentic-vending/issues/230), the $400 fill cap, and readiness after the 22 Nov merges and deploy |
| [Day-One Countdown](https://claude.ai/artifact/Et6cANEJjB5vv1JNnfvaW1) † | Every date from 17 Nov to the first weekly sales tax calculation on Sat 5 Dec: the slot redraw (done 20 Nov, final), catalog approvals by Wed 25 Nov, the ledger opening and the fill request by 23:59 Sun 29 Nov, stocking by course staff and the interval counter reset on Mon 30 Nov, the tracking check, the opening day (Thu 3 Dec, 00:00 ET, settled: A24) and the first restock request, due 23:59 ET Thu 3 Dec for the Fri 4 Dec restock, each with its status and what it's waiting on. Refreshed 22 Nov: the readiness merges and the 2:07 PM deploy, round 7b picked, test week 23–27 Nov, and the ledger reset and fill request on Sun 29 Nov (the reset refuses from 00:00 Mon 30 Nov) ([`planning/2026-11-20-david-update.md`](planning/2026-11-20-david-update.md), [`planning/2026-11-17-david-answers.md`](planning/2026-11-17-david-answers.md)) |
| [Day-One Plan Rounds](https://claude.ai/artifact/NfeMbbF34NDxQfW4vZQtkY) † | The agent's day-one plan, one tab per round: our 20 slots on the machine, what changed since the round before, fill cost against the cap, projected gross profit and sell-out per slot with the projection's assumptions, and the catalog requests it needs (#119). Rounds 5, 6, 7 and 7b are on our slots after the 20 Nov redraw. Round 7b is the pick (22 Nov, in the team database): 20 distinct drinks from 10 brands under the $400 cap, fill $343.50 expected and $399.00 at most, base projected gross profit $1,098. It replaces round 6, which doubled 6 drinks. Made with `python -m vending.plan_page --write`; see [`plan-page.md`](plan-page.md) |
| [Catalog Request Queue](https://claude.ai/artifact/T2vFjfjJYrEnXvG1cU9pjM) † | Every catalog request and its status: as of 21 Nov, 64 filed, 40 approved, 24 waiting, none rejected, with about one place left before the bridge refuses new ones at about 25 waiting. The 19 filed early on 21 Nov with the reason for each, the timeline since 11 Nov, and the Wed 25 Nov approval deadline |
| [Slot Assignment](https://claude.ai/artifact/BFJXdG2SfFtdSmydv8KkMc) † | Which 20 of the machine's 40 selections we run: our slots (the server's assignment), recorded in the team database on 20 Nov and final for the quarter (David, 20 Nov). How to read a selection number, what happened in the one redraw (ten slots kept, ten new, still four per row), what `record` does with the server's answer (#69, #146, #75), and whose sales data we may use before the fall cut and after the start (`docs/integrity.md`) |
| [Vending Agent Dashboard](https://claude.ai/artifact/LjS75bStnTJgVRLF9hGGmW) † | A sample snapshot of the agent dashboard in [`dashboard.md`](dashboard.md), rendered from main as of 20 Nov (`d391399`) with made-up test data from `vending/demo/snapshot.json`, as if after a first week (9 Dec). The 20 lit selections are the demo's own made-up set (`SELECTIONS` in `vending/demo/make.py`), not our slots. It shows Honcho the mascot, the money panel, the machine close-up, the plan, runs, tool calls, purchase requests, catalog requests, back stock and the money chart ([`money-chart.md`](money-chart.md)). The real dashboard is on Azure behind the team password |
| [Project So Far](https://claude.ai/artifact/1VTEgX1ZKenp2WoBSD4Z6J) | The whole-team summary of the project to date |
| [PR Atlas](https://claude.ai/artifact/3zzzgvg1e7jQhMyMiMhWgL) | The project's pull requests grouped by workstream on a timeline, with review status and open issues |

## Data and the agent

| Page | What it shows |
|---|---|
| [Machine Sales Map](https://claude.ai/artifact/TEcYXfQpAomczgF58wZFUU) † | All 40 selections on a picture of the machine front, from the fall (27 Aug to 15 Nov, before the fall cut, when no selection was either team's), colored by drinks sold, takings, drinks a week or average price paid, with each selection's weekly sales and price history. Our slots (the server's assignment, final since 20 Nov) have a dashed outline. Also whole-machine sales by week and by day and hour |
| [Cutover Clock](https://claude.ai/artifact/BSgBkggAC5PYBpdPqRHp1H) † | How a restock visit splits a slot's sales between two drinks or two prices (#97, now #172): drag two polls around a visit to see how the counters mix two setups, the 19 fall door visits with their refill grades and blind spots (#105, door times merged in #168), and a calculator that splits a window's sales by the two prices. Its fall figures cover the slots we had before the redraw. The written version is [`cutover.md`](cutover.md) |
| [Rocco's Friday](https://claude.ai/artifact/AMWoaVRxK5BZAYZftigsme) | One competition week step by step: who acts, with which tool or data, and what can go wrong |

## Older pages

Kept for the record. Each one's job is done, it is a dated snapshot, or it describes our slots before the 20 Nov redraw. Check a newer page or the repo before you rely on one.

| Page | What it shows | Why it is here |
|---|---|---|
| [Vending Agent Tool List](https://claude.ai/code/artifact/82ae22ab-53a2-48c0-b4d5-c636efcc877d) | An older comment copy of [`planning/tool-list.md`](planning/tool-list.md) | Not kept up to date; the repo file is the current version |
| [Vending 19 Nov Recap](https://claude.ai/artifact/RmBM4LZhNLLKMcwZvKAQj1) | What changed on 19 Nov: selling across a Friday swap, the ten PRs merged that day, the deploys, first-in-first-out costing (#176) | A dated recap. The open work continues in [#172](https://github.com/marshall-strong/agentic-vending/issues/172) |
| [Overnight Session: 12 Nov](https://claude.ai/artifact/Xef245aURCXBdJo6h5PEcZ) † | The 12 Nov class brief, six decisions and drafts for the 12-16 Nov PRs | A dated recap, from before the redraw |
| [Meet Rocco and the Crew](https://claude.ai/artifact/2zci11rMobPqXMddJGxXyc) | The naming proposal for the business, the head agent and his crew | The proposal merged as #76 on 20 Nov |
| [Model Scorecard](https://claude.ai/artifact/H6TbeKM4VWi9yoDmt6UUYb) | The first model sweep as cost against score, with the 10 test cases explained (#64, #66) | The first sweep only; the standing benchmark (#66) keeps every later run |
| [Restock Math](https://claude.ai/artifact/K1ynoCMrLLmkZAeSJg3gyU) † | Sell-out rates per slot and a simulator for a $25 midweek visit (#110) | Built on round 3 and the slots before the redraw; round 6 replaced it |
| [Vending Forecast Lab](https://claude.ai/artifact/WGfwq36gf8Yd11HszSmyyy) | Sales forecasting (PR #68) | Analysis of our 20 slots before the redraw |
| [Campus Rhythm Lab](https://claude.ai/artifact/6KzxRRJrdJZV6PR7Ez81Yd) | Sales against the campus calendar, the slow-seller rule test and the draft guideline for the agent (PR #68) | Analysis of our 20 slots before the redraw |
| [Slot and Price Lab](https://claude.ai/artifact/2aR4tSYARzmrqeRcvdQDph) | Slots and prices (PR #68) | Analysis of our 20 slots before the redraw |
| [The Doorman's Log](https://claude.ai/artifact/MMfwiYd7BrG15FDjbngV1q) | Every fall visit to the machine and every refill, dated from the machine's own door counter | Fall history; the Cutover Clock and `analysis/door_events.py` carry it on |
| [Shelf Through Time](https://claude.ai/artifact/4JTTXx4J89RwaGoDfTMVTg) | Product history, from Sean's photos | Fall history; [`analysis/product_history.md`](../analysis/product_history.md) is the written version |
