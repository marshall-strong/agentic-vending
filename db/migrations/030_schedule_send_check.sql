-- A live scheduled run's checks, again at the send (#238 review), and what it sent.
--
-- `agent_run_lease.schedule_due`: the `agent_schedule` time whose run holds the lease, or null for a run by
-- hand. The scheduler sets it in the transaction that takes the lease (`run_lease.take` clears it first), and
-- `give_back` clears it. The submit tool reads it just before the POST: a scheduled run's purchase request goes
-- only if both switches still say live, the competition has started, no other submission is open, the slot check
-- is fresh, and no other scheduled submission went for the same week's visit (`vending.agent_schedule`).
-- `run_lease.attach` also writes the run's id onto that `agent_schedule` row, so the row names its run even if
-- the worker never records the ending.
--
-- `purchase_submissions.schedule_due`: the scheduled time a submission was sent for, saved with the `sending`
-- row before the POST; null for a run by hand or the fill. The dashboard says what a scheduled run sent (or may
-- have sent) from it, and it allows one scheduled submission per week's visit, whatever became of it.

-- As in 025: a submit in flight reads purchase_submissions in its gate transaction, then inserts on a second
-- connection. Give up fast rather than queue behind it; run it again once no submit is running.
set local lock_timeout = '2s';

alter table agent_run_lease add column schedule_due timestamptz;
alter table purchase_submissions add column schedule_due timestamptz;
