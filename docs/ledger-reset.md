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
   ([opening capital plan](opening-capital-plan.md)). Leave out `--at`: the opening is now. If the
   fill request has already gone in, stop: go to [If the fill went in first](#if-the-fill-went-in-first).

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
- **Step 4 fails after step 3:** the ledger has no opening. Fix the cause (it names it). If the fill
  request has not gone in, run step 4 again. If it has gone in, for any reason, don't run step 4:
  go to [If the fill went in first](#if-the-fill-went-in-first). The test opening is
  safe in the archive.
- **The wrong amount went in at step 4:** the reset refuses the real opening, by design. Record
  the difference as an adjustment with a detail saying why, or ask the team before anything else.

## If the window closed

From 00:00 Eastern on Mon 30 Nov the command refuses. Don't edit the database by hand. What to do
depends on how far the steps got:

- **Step 3 ran, but step 4 didn't, and the fill request went in:** go to
  [If the fill went in first](#if-the-fill-went-in-first).
- **Step 3 ran, step 4 didn't, and the fill hasn't gone in:** run step 4 now, without `--at`, then
  send the fill.
- **Step 3 never ran:** the 11 Nov test opening is still the opening, so the books count from
  11 Nov. Run 2's model spend is in the balance, and the 12 Nov test receipt is in purchases and
  stock. Ask the team before anything else. A fix then is a person's decision, recorded in the
  ledger with a detail saying why, not a reset.

## If the fill went in first

Use this section whenever the fill request was sent before the real opening was recorded, for any
reason: the window closed, step 4 failed and was retried later, or the steps ran out of order. It
doesn't matter whether it is before or after midnight. An opening recorded now, without `--at`, would
fall after the fill, and the fill would drop out of Key dates and its receipt out of the books.

The opening has to be dated in the past, in one interval. A command finds it and records it for you.
It reads each bound with the same clock the books use, so you don't need any SQL. (The
[DB explorer](db-explorer.md) has no SQL box, and hand-written SQL kept getting those clocks wrong.)
Don't type a time into `open --at` yourself: a model call or receipt recorded between reading the
time and recording it would land in the real books.

1. **Preview the interval.** Give the fill's request ID, as `purchase_requests` stores it (the DB
   explorer lists them, with `submitted_at` in Eastern). This command changes nothing:

   ```
   python -m vending.ledger safe-opening --fill REQUEST_ID
   ```

   It prints the latest test-period record, the earliest moment the fill counts from, the interval,
   a suggested opening (the next whole minute after that record), and the `open --recover` command
   for step 2. The opening must be:

   - after our slot assignment was first read (`first_read_at`, the 20 Nov redraw);
   - after every row in `ledger_entries`. With no opening yet, every row there is test-period, a
     model call from the live run included;
   - after every test request's receipt lines and midweek visit fee, at the times the balance and
     stock use (door close, or the line's `resolved_at` when two visits' door times clash; a visit
     with no lines at its `fulfilled_at`);
   - after every test request was sent. A test request is any other request sent at or before the
     fill;
   - at or before the fill's `submitted_at`, and at or before the fill's own receipt lines and visit
     on those same clocks. Door times are to the minute, so a fill stocked at once can count from
     14:00:00 when it was sent at 14:00:35: the opening can't be after 14:00:00. A fill not stocked
     yet is held to the minute it was sent for the same reason.

   **It refuses**, and changes nothing, when:

   - a test request is still pending, or a submission is unresolved: "Resolve or wait for request X
     first". Its receipt could still land later. Wait for the sync to read it, or resolve the
     submission (`python -m vending.submissions resolve`), then run the command again;
   - the ledger already has an opening, or the 11 Nov test opening is still there (step 3 never ran:
     ask the team);
   - the request isn't the fill: unknown, rejected, or sent at or after the start (a restock).

   **If it says no time works** (exit 1), a test-period record stands at or after the moment the fill
   counts from. The message names both. Don't record the one-time opening. Ask the team.

2. **Record the opening** with the command it printed. Run it without `--yes` first: that previews
   the opening and records nothing. Then with `--yes`:

   ```
   python -m vending.ledger open --recover --fill REQUEST_ID --by NAME
   python -m vending.ledger open --recover --fill REQUEST_ID --by NAME --yes
   ```

   With `--yes` it works the interval out again and records the opening (600.00 unless you give
   `--amount`) at the suggested time, in one transaction. While it does, model calls, receipts and
   requests wait for it, so nothing recorded since step 1 is missed. A record made since step 1 can
   move the time later, or leave no time: then it refuses, records nothing and says why. It refuses
   for the same reasons as step 1.

3. **Check.**
   - Before 00:00 Eastern on Thu 3 Dec, the dashboard's Key dates panel shows **Fill request sent**
     as done, with the fill's time. If it says "no purchase request since the opening", ask the team.
     From the start that check is gone from the panel; `open --recover` already checked the same rule.
   - `python -m vending.ledger show`: the opening capital line shows the time it recorded, token
     spend 0.00, and a note that the earlier model calls are development spend. Receipts is the
     fill's total once it is fulfilled, plus any later receipts, so step 5's "receipts 0.00" doesn't
     apply.
   - `python -m vending.ledger safe-opening --fill REQUEST_ID` now refuses: the ledger has an
     opening.

For example, the reset ran at 15:00 Eastern on Sun 29 Nov, a live run made a model call at 19:30,
and the fill went in at 20:00. `safe-opening` names the 19:30 call and suggests 19:31. An opening at
19:00 would put that call in the real books, and one at 23:00 would leave the 20:00 fill out of Key
dates. If the run makes another call before step 2, that call is dated after the fill:
`open --recover` sees it, finds no time, and refuses. Ask the team then.

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
