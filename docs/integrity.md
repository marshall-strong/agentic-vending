# Integrity rule

## The rule

David said in class that teams act with integrity: a team does not change its strategy
based on the other team's slots, pricing or strategy, and it follows the project
instructions.

## Rival prices: our current choice

The project brief (Revised, section 3.2) says "the other row's price is an input to your
pricing logic". David's rule points the other way. **For now, our pricing ignores the
other team's prices** (Kevin, 11 Nov). It keeps us inside David's rule, and we shouldn't
need that information to win. The team can revisit this. If we do, change this page and
assumption A15 first. Explain the choice in Summative 2.

Which selections are ours is not settled yet: the inventory server assigns them (brief,
section 1). The machine is split evenly between the two teams.

## What it means

**For the agent.** Its prices, products and restock plans never depend on the other
team's slots, prices, sales or strategy.

**For people.** Don't look for or open the other team's dashboard, repo, container or
data. If you see something by accident, don't use it and don't pass it on.

**For AI coding tools** (Claude Code, Codex, T3 and the like). The same applies. Never
search for, fetch or open the other team's dashboard, repo, container or data, and don't
write pricing logic that takes the other team's prices as an input. `AGENTS.md` at the
repo root points here.

## Guardrails in code

- **Agent tools answer for our slots only.** The read tools filter to `SLOTS` on
  `DEVICE_ID` (`vending/slots.py`). `get_sales_history` refuses any other slot ("Not our
  slots: ...").
- **The agent has no web-fetch tool.** It can't browse to another team's dashboard.
  `tests/test_server.py::test_tool_list_is_pinned` pins the MCP server's tool names, so
  adding a tool (a web fetcher, say) is a deliberate change that shows up in review.
- **Our stock count reads only our machine's counters.** `vending/stock.py` filters poll
  counters to `DEVICE_ID` and our `SLOTS`.

We still store every selection from each poll. The polls are the machine's own record, and
which selections are ours may change when the inventory server assigns them.
