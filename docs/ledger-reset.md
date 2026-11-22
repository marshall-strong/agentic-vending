# Resetting the ledger for the real opening (29 Nov)

The team ledger already has an opening entry from testing: entry 1, "opening capital (test state,
11 Nov; to be reset before launch)". The ledger allows one opening, so `ledger open` refuses the
real one while the test one is there (#215). This page says how to move the test opening out of
the way and record the real one.

**When:** Sun 29 Nov, by 23:59 Eastern, before the day-one fill request goes in. The command refuses
from 00:00 Eastern on Mon 30 Nov (`RESET_CLOSES`). Staff buy and
stock on Mon 30 Nov. An opening recorded after the fill's receipt leaves the fill out of our books
([20 Nov notes](planning/2026-11-20-david-update.md)).

**Who:** one team member, from a laptop with `DATABASE_URL` pointing at the team database (as in
[deploy.md](deploy.md)). Kevin, unless the team names someone else. One person runs every step, in
order, so nothing runs twice.

## What the reset changes

`python -m vending.ledger reset-test-opening` moves the test opening from `ledger_entries` to
`ledger_entries_archive` (migration 022), in one transaction, and logs it in `action_log`. Nothing
is deleted. Nothing else moves. Once the real opening is recorded:

- **Model calls** before it, such as run 2's on 12 Nov, are development spend. They show on the
  [dev-spend page](dev-spend.md), not in the balance (#104).
- **Receipts and midweek visits** before it, such as the 12 Nov test purchase
  (`pr_v8hB_h3gQatc`), are test purchases. They are not in the balance, the P&L or stock
  (`stock.receipts_count_from`).
- **Takings** count from the last poll at or before it.

Every reader of the opening (the balance, the P&L, the money chart, the dashboard's money panel,
`get_account_balance`, `get_pnl` and the dev-spend page) reads `ledger_entries`, so they all move to
the new opening together.

**It moves one entry only:** entry 1, dated 11 Nov (Eastern), with the detail "opening capital
(test state, 11 Nov; to be reset before launch)" exactly (`TEST_OPENING_ID`, `TEST_OPENING_DATE`,
`TEST_OPENING_DETAIL` in `vending/ledger.py`). It doesn't look for the word "test", so the real
opening is never moved, whatever its detail says.

**It refuses**, and changes nothing, when any of these is true:

1. The competition has started (`vending/start.py`, `START`).
2. It is 00:00 Eastern on Mon 30 Nov or later (`RESET_CLOSES`). The fill can be stocked that day, so
   an opening recorded then could fall after the fill's receipt.
3. The archive already holds a reset. The reset runs once, even if a row like the test one comes
   back.
4. There is no opening.
5. The opening isn't that exact entry.

It reads these guards inside its transaction, after taking a lock and with the opening row locked,
so two people running it at once can't both move something. After its last write it reads the
database clock again, still inside the transaction. A write can wait on a lock another connection
holds. If 00:00 Eastern on Mon 30 Nov passes while it waits, the reset refuses with the same
"window closed" message and nothing changes.

The database checks the window too, at commit (migration 022). A pause after the last clock read
can't commit the reset after 00:00 Eastern on Mon 30 Nov: the commit fails with "the reset window
closed", and nothing changes. The cutoff in the database is fixed in the migration; no setting moves it.

The reset runs in its own transaction. On a connection that already has one open, it refuses and
changes nothing, so nobody can start it before midnight and commit it after. The CLI always runs it
on its own.

**The agent's notes** count from the real opening too. Until it is recorded, no run's notes carry
over to the next run. After it, only notes from runs that started at or after both the opening and the
moment our slots (the server's assignment) were first read carry over (`docs/agent.md`). So an opening
backdated before the 20 Nov redraw still can't bring back notes from before it.

## The steps

1. **Migrate**, so the archive table exists. Use the owner login, as for any migration:

   ```
   python -m vending.db migrate
   ```

2. **Dry run.** It changes nothing and prints what it would do. Check it names entry 1, the test
   opening, and nothing else. It lists what is dated up to the time it prints, which is what falls
   before an opening recorded then. Anything dated later is counted apart and still counts:

   ```
   python -m vending.ledger reset-test-opening --by NAME
   ```

3. **Reset**, then go straight to step 4. Between the two, the ledger has no opening, and the agent
   and the dashboard say so:

   ```
   python -m vending.ledger reset-test-opening --by NAME --yes
   ```

4. **Record the real opening.** The amount is the $600.00 the brief gives
   ([opening capital plan](opening-capital-plan.md)). Leave out `--at`: the opening is now.

   ```
   python -m vending.ledger open --amount 600.00 --by NAME
   ```

5. **Check.**
   - `python -m vending.ledger show`: opening capital 600.00 dated today, receipts 0.00, token
     spend 0.00, and a note that the earlier model calls are development spend.
   - `python -m vending.ledger entries`: the new opening is in the list, and the test opening is
     under "Archived, not in the books".
   - `python -m vending.dev_spend`: run 2 is in development spend, and development ends at the
     new opening.
   - The dashboard's money panel shows the new opening date.

Then the fill request can go in.

## If something goes wrong

- **Step 2 or 3 refuses:** read the message. It names the guard. Don't edit the database by hand.
  Ask the team.
- **Step 4 fails after step 3:** the ledger has no opening. Fix the cause (it names it) and run
  step 4 again. The test opening is safe in the archive.
- **The wrong amount went in at step 4:** the reset refuses the real opening, by design. Record
  the difference as an adjustment with a detail saying why, or ask the team before anything else.

## If the window closed

From 00:00 Eastern on Mon 30 Nov the command refuses. Don't edit the database by hand. What to do
depends on how far the steps got:

- **Step 3 ran, but step 4 didn't, and the fill request went in:** record the real opening with a
  time in the past. The opening's time decides what counts. Key dates counts the fill request by when
  it was sent, stock and spend count its receipt by the receipt's time, and the balance counts every
  ledger entry dated at or after the opening. So the opening has to fall in one interval:
  - after the 20 Nov redraw: when our slots (the server's assignment) were first read, the
    `first_read_at` of `slot_assignment`,
  - strictly after the latest test-period ledger entry of any kind and the last test receipt's
    effective time, and
  - at or before the fill request's `submitted_at`, which also puts it before the fill's receipt.

  Any kind: any row still in `ledger_entries`, a model call or an adjustment included — not just the
  last test purchase. Until the real opening is recorded, every row there is test-period, including
  a call the live run made after the reset. A receipt's effective time is its request's door close,
  else the line's `resolved_at`; a test midweek visit's fee counts from the same clock, so one bound
  covers both.

  Read the ends of the interval on the [DB explorer](db-explorer.md) (read only, times in Eastern):
  `first_read_at` in `slot_assignment` and the requests' `submitted_at` in `purchase_requests`,
  plus, in psql or the explorer's SQL box:

  ```sql
  -- The latest test-period ledger entry, any kind.
  select kind, at at time zone 'America/New_York' as at_eastern, detail
    from ledger_entries order by at desc limit 5;

  -- When each fulfilled receipt counts from (door close, else the line's resolution; a visit's fee
  -- counts from the same clock). Leave out the fill's own row.
  select r.request_id, r.off_cycle,
         greatest(max(coalesce(r.door_closed_at, l.resolved_at)), r.fulfilled_at)
           at time zone 'America/New_York' as counts_from_eastern
    from purchase_requests r join purchase_lines l using (request_id)
   where r.status = 'fulfilled'
   group by r.request_id, r.fulfilled_at, r.off_cycle
   order by counts_from_eastern desc limit 5;
  ```

  Pick a time strictly after the latest time those queries return (the fill's own row excepted) and
  after `first_read_at`, and at or before the fill's `submitted_at`. For example, if the latest
  test-period entry is a model call at 19:30 Eastern on Sun 29 Nov and the fill went in at 20:00,
  use 19:45. An opening at 19:00 would put that call in the real books as $0.25 of token spend, and
  an opening at 23:00 would leave the 20:00 fill out of Key dates. Don't use a time before the
  redraw, such as 18 Nov: that counts pre-redraw test activity in the books. If no time meets all
  of this — the latest test entry stands after the fill went in, say — the interval is empty. Stop:
  don't record the one-time opening; ask the team. Give the time with its offset (a time without one
  is read as UTC):

  ```
  python -m vending.ledger open --amount 600.00 --by NAME --at 2026-11-29T19:45:00-05:00
  ```

  Then check that the opening is at or before the fill request, and that the books hold the fill's
  receipt. Before 00:00 Eastern on Thu 3 Dec, the dashboard's Key dates panel shows **Fill request
  sent** as done, with the fill's time. If it says "no purchase request since the opening", the
  opening is after the request: ask the team. From the start the panel no longer shows that check,
  so check the records instead. This works on any date:

  1. The opening and the requests since it. On the DB explorer: `ledger_entries` (the row with kind
     `opening`) and `purchase_requests`. Or in psql:

     ```sql
     select at at time zone 'America/New_York' as opening_eastern
       from ledger_entries where kind = 'opening';

     select request_id, submitted_at at time zone 'America/New_York' as sent_eastern,
            off_cycle, status, total_paid,
            fulfilled_at at time zone 'America/New_York' as fulfilled_eastern
       from purchase_requests where status <> 'rejected'
      order by submitted_at;
     ```

     Verify the fill by its own `request_id`, the one the fill went in under: its `sent_eastern` must
     be at or after `opening_eastern`. If it is before, ask the team. If you have to infer which
     request is the fill, use the same rule as Key dates: the first request in the list that was sent
     at or after the opening and before the start (00:00 Eastern on Thu 3 Dec); the query already
     leaves rejected ones out. A request sent at or after the start is a restock, never the fill. If
     the rule finds no fill — the Sunday fill rejected, say, so the first request standing is a
     Thursday restock — don't count the restock as the fill: ask the team.
  2. The fill's receipt total, once its status is `fulfilled` (put its `request_id` in):

     ```sql
     select count(*) as lines, sum(total_paid) as paid
       from purchase_lines where request_id = 'REQUEST_ID';
     ```

  3. `python -m vending.ledger show`: Receipts is that `paid` total, plus any later receipts. It is
     not 0.00 once the fill is in, so step 5's "receipts 0.00" doesn't apply here. The opening
     capital line shows the time you gave. Token spend and the development-spend note are as in
     step 5.
- **Step 3 never ran:** the 11 Nov test opening is still the opening, so the books count from
  11 Nov. Run 2's model spend is in the balance, and the 12 Nov test receipt is in purchases and
  stock. Ask the team before anything else. A fix then is a person's decision, recorded in the
  ledger with a detail saying why, not a reset.

## Example

The CLI on a scratch database seeded like the team ledger (a test opening on 11 Nov, two model
calls, one test receipt). The CLI prints times in Eastern.

```
$ python -m vending.ledger reset-test-opening --by kevin
Dry run: nothing was changed. With --yes this moves one entry out of the books, into ledger_entries_archive:
     1  2026-11-11 14:00  opening         600.00  opening capital (test state, 11 Nov; to be reset before launch)  by marshall
Nothing else moves. Record the real opening now (step 4, without --at), and these, dated from 2026-11-11 to 2026-11-29 15:00 Eastern, fall before it:
  model calls since 2026-11-11: 2 entries, 0.48 spent. Development spend: on the dev-spend page, not in the balance
  receipts since 2026-11-11: 1 lines, 12.37 paid. Test purchases: not in the balance, the P&L or stock
  midweek visits since 2026-11-11: 0, 0.00 in fees. Not in the balance or the P&L
To make the change: python -m vending.ledger reset-test-opening --by kevin --yes
```
