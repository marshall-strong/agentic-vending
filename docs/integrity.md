# Integrity rule

## The rule

David said in class that each team knows which machine slots are its own, so teams act
with integrity. A team does not change its strategy based on the other team's slots,
pricing or strategy, and it follows the project instructions.

The machine is split 50/50, with no unassigned selections (Kevin, 11 Nov). Ours are
columns 5-8; the other team's are columns 1-4. So we know where they are. The machine
still shows us all 40 selections in every poll. Seeing them is not permission to use them.

## Why we ignore their prices, though the brief says otherwise

The project brief (Revised, section 3.2) says "the other row's price is an input to your
pricing logic". That conflicts with David's rule above. **The team chose David's rule
(Kevin, 11 Nov): our pricing ignores the other team's prices completely.** It is the
safer reading, and it still lets us pass the guardrails. We say this openly in Summative 2
("how the problem is framed for the agent"), and docs/assumptions.md A10 records it. If
David says the brief's reading applies, revisit this page first.

## What it means

**For the agent.** It decides from our 20 slots only (`vending/slots.py`: `SLOTS` on
`DEVICE_ID`). Its prices, products and restock plans never depend on the other team's
slots, prices, sales or strategy.

**For people.** Don't look for or open the other team's dashboard, repo, container or
data. Don't read their selections out of a raw poll. If you see something by accident,
don't use it and don't pass it on.

**For AI coding tools** (Claude Code, Codex, T3 and the like). The same applies. Never
search for, fetch or open the other team's dashboard, repo, container or data. Never
write code that reads, stores or reports selections outside `SLOTS`. Pricing logic never
takes rival data as an input, not even as an optional signal. `AGENTS.md` at the repo
root points here.

## Guardrails in code

- **Agent tools answer for our slots only.** The read tools filter to `SLOTS` on
  `DEVICE_ID`. `get_sales_history` refuses any other slot ("Not our slots: ...").
- **The agent has no web-fetch tool.** It can't browse to another team's dashboard.
  `tests/test_server.py::test_tool_list_is_pinned` pins the MCP server's tool names, so
  adding a tool (a web fetcher, say) is a deliberate change that shows up in review.
- **The history sync stores only our slots.** `vending/sync_history.py` writes
  `poll_selections` rows only for `SLOTS`, and only for polls from `DEVICE_ID`. The poll
  row and its raw document are still kept whole, because the checksum covers the whole
  document.

## Rows stored before this rule

Polls synced before this change still have rows for all 40 selections in
`poll_selections`. No reader uses the other selections: every query filters to `SLOTS`
on `DEVICE_ID`. Nothing deletes them automatically.

**Needs the team's decision before anyone runs it.** This deletes those rows from the
shared database for good:

```sql
delete from poll_selections where selection <> all(array['15','16','17','18','25','26','27','28','35','36','37','38','45','46','47','48','55','56','57','58']) or session_id in (select session_id from polls where device_id <> 'vmc1');
```

The raw documents in `polls.raw` still hold every selection. Removing those would lose
the checksummed original, so that is a separate decision.
