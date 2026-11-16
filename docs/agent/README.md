# Project rules for the agent

Our own condensed notes on the project brief, for the agent to look up mid-run with `get_project_rules`. Each rule cites "Project Brief (Revised), section N". The brief itself is not in this repo.

Each topic file starts with a `#` title and then a one-line summary, which must match its line below. `get_project_rules` reads only the topics listed here; `tests/test_project_rules.py` checks the list and the files match.

## Topics

- `rules-card`: The always-on card: goal, hard guardrails, integrity, and how to work.
- `goal-and-scoring`: The winner has the most cumulative profit at the end of run week 9, and must qualify by actually running the slots as a business.
- `money`: The account balance is the only spending limit: $600 once, sales flow back in, and fees and agent costs come out of the same account.
- `restocking`: One free restock every Friday; an extra dispatch costs $25 per request (not per unit), may take longer than 24 hours, and should be batched.
- `pricing`: Prices stay within the floor and ceiling, must be explainable, may need deliberate variation to learn demand, and come from our own costs and sales only.
- `price-history`: What each drink sold for in our 20 slots this fall, before the competition: the starting price for a product new to a slot.
- `inventory-belief`: Stock on hand is an estimate with uncertainty, built from restocks as reset points; demand is censored when a slot runs out, and stockouts shift demand to substitutes.
- `guardrails`: Five non-negotiable rules: spend only what you have, price within bounds, obey the halt switch, log every action, and order only through the approved channel.
- `directive`: The restock and pricing directive must let a person who has never seen our code load every slot and set every price without asking a question.
- `competition-and-integrity`: Our slot assignment is private; we may not know which other slots are a rival's; the team has chosen to ignore other slots' prices, though the brief calls them a pricing input.
