# Project pages (Claude artifacts)

The team's main pages about the vending project, in one place. These are live pages on claude.ai: each link always shows the latest version, so there is nothing to download or keep in sync.

Five pages open only with a Golisano Claude sign-in, so use your institute account for them: the Vending Agent Roadmap, the Vending Agent Dashboard, the Slot Assignment, the Machine Sales Map and the Cutover Clock. The others open with any sign-in.

When you publish a new page about the project, add a row here in the same PR or a follow-up.

## Plan and status

| Page | What it shows |
|---|---|
| [Vending Agent Roadmap](https://claude.ai/artifact/AfECivFPXhKK6eagBZH5Ef) | Every work item with its owner and status, the critical path, open PRs and who's up next, decisions, risks, and a log of what changed |
| [Project So Far](https://claude.ai/artifact/1VTEgX1ZKenp2WoBSD4Z6J) | The whole-team summary of the project to date |
| [Slot Assignment](https://claude.ai/artifact/BFJXdG2SfFtdSmydv8KkMc) | Which 20 of the machine's 40 selections we run (columns 5 to 8 on vmc1, as the inventory server answered on 12 and 15 Nov), how to read a selection number, what happens if the split is redrawn before the 3 Dec start, what `record` does with the server's answer (#69, #75), and whose sales data we may use before and after the start |
| [Vending Agent Tool List](https://claude.ai/code/artifact/82ae22ab-53a2-48c0-b4d5-c636efcc877d) | The live version of [`planning/tool-list.md`](planning/tool-list.md): the agent's tools, the bridge API and the open questions. Edits and comments go here |
| [PR Atlas](https://claude.ai/artifact/3zzzgvg1e7jQhMyMiMhWgL) | The project's pull requests grouped by workstream on a timeline, with review status and open issues |
| [Vending Agent Dashboard](https://claude.ai/artifact/LjS75bStnTJgVRLF9hGGmW) | A sample snapshot of the agent dashboard in [`dashboard.md`](dashboard.md): the real page rendered once from made-up test data, as if after a first week (9 Dec), with our real slots lit. It shows the money panel (#48: balance, where the $600 went, pending orders, profit since the opening), the machine, the plan, runs, tool calls, requests and back stock |

## Data and sales history

| Page | What it shows |
|---|---|
| [Machine Sales Map](https://claude.ai/artifact/TEcYXfQpAomczgF58wZFUU) | Our 20 slots on a picture of the machine front, colored by drinks sold, takings, drinks a week or average price paid, with each slot's weekly sales and price history; the other 20 are hatched with no values. Also whole-machine sales by week and by day and hour |
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
