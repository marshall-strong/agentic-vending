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

**The Interloper's prices are out too.** The snack machine placed next to ours sells
drinks. It isn't the other team, but David answered on 17 Nov that we may not use its
prices as an input to ours, and that it is outside the project's scope. This one is the
course staff's rule, not a team choice. See `docs/agent/interloper.md`.

Which selections are ours is the inventory server's assignment (brief, section 1). The
machine is split evenly between the two teams. The instructors change the assignment exactly once,
through the server, before the competition starts, and that change is what the competition uses
(David, 16 Nov, as Kevin reported; nobody knows yet whether it has happened). The brief (section 1)
says the assignment is fixed for both quarters, so the change is the instructors' practice, not the
brief's text.

## Fall data (before the fall cut)

**Before the fall cut, no selection is either team's.** Course staff stocked and priced the whole
machine in the fall, and this quarter the instructors let us see all of it ("this quarter: yes,
everything", 21 Sep meeting). So the analysis and the agent may use the fall's per-selection sales
and prices from all 40 selections, for polls taken before the cut (Kevin, 15 Nov).

**The cut comes before the day-one fill.** Once staff start loading the teams' products they
"basically turn it off" (David, 12 Nov), and from then on the machine's prices and counters are the
teams' own choices. The cut is the setting `FALL_CUTOFF`: unset, 18 Nov 2026 00:00 Eastern, the
first day of the break after Fall Quarter classes, safely before any loading (about 30 Nov to
2 Dec). It is never later than the start, 3 Dec 2026 00:00 Eastern.

**From the cut on**, nothing reads per-selection sales, prices or stock outside our assignment.
What counts is when the poll was taken, not when the data is read: fall data stays usable after the
start.

This rests on Kevin's reading of the 21 Sep meeting; the instructors haven't confirmed it in
writing. #102 asks David whether we may use the fall's per-selection sales for all 40 selections,
from before the day-one fill, to set starting rates.

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

- **Agent tools answer for our slots only.** The read tools filter to the assignment the
  inventory server gave us, stored by `python -m vending.slots record`, on `DEVICE_ID`
  (`vending/slots.py`). `get_sales_history` refuses any other slot ("Not our slots: ...").
- **The agent has no web-fetch tool.** It can't browse to another team's dashboard.
  `tests/test_server.py::test_tool_list_is_pinned` pins the MCP server's tool names, so
  adding a tool (a web fetcher, say) is a deliberate change that shows up in review.
- **Our stock count reads only our machine's counters.** `vending/stock.py` reads poll
  counters only from `DEVICE_ID`, and only for selections in the current server assignment
  that our own receipts name. A historical receipt does not authorize reads after reassignment.
- **Count estimates use the same assignment.** Both the count rows and the counter query filter
  selections in SQL. Dashboard response filtering is an additional display check.
- **The bridge script never prints a whole poll.** `scripts/bridge.py` (`raw`, `latest`,
  `poll --live`) prints every poll rebuilt from our selections and the door counter only, whatever
  its time: no other selection and no machine-wide total. The bridge's own times (a listing's
  `started_at`, a reply's `Date`) can be wrong, so the script doesn't judge from them that a poll
  is pre-cut. `--out` only archives the raw poll to a regular file. Pre-cut fall data for all 40
  comes from `analysis/export_sales.py --fall-all`. The script prints no unchecked field of any
  reply and no route's error reply (`tests/test_post_start.py`).
- **The fall analysis stops at the fall cut.** `analysis/export_sales.py --fall-all` writes
  per-selection rows for all 40 selections, but only from polls before the cut
  (`analysis/slots_config.py`, `fall_end`: `FALL_CUTOFF`, never later than 3 Dec 2026 00:00
  Eastern; the one definition is `vending.start.fall_cut`). `check_before_start` refuses any row of the fall run from the cut on, ours included,
  in the export and again when the analysis loads it. Whole-machine totals in a normal export
  stop at the cut too; from it, they cover our assignment only. Normal runs may keep rows from the fall cut on only for our assignment as stored from the
  inventory server (recorded in each export as `assigned_slots`; the code has no fixed list),
  and a time without a UTC offset is refused. `tests/test_analysis_slots.py` checks all of it.

The fall rule applies to the poll timestamps, not the date an analysis runs. Historical fall
data remains usable after the start. Our own historical purchase receipts and action logs also
remain accounting and audit records after reassignment. They do not authorize counter reads.

From the fall cut on, everything that reads the polls for analysis, display or decisions must
filter to our assignment, and nothing may show a machine-wide total: what counts is when the poll
was taken. From 3 Dec the polls carry the full, raw DEX response (David, 17 Nov), so the other
team's selections arrive with ours. On 17 Nov some readers didn't yet: see "Where the code stands" in
`planning/2026-11-17-david-answers.md`. We still store every selection from each poll. The polls are the machine's own record, and
which selections are ours may change when the inventory server assigns them.
