-- The midweek wake-up starts an agent run (#219).
--
-- `agent_schedule.kind`: `weekly` for a time in AGENT_SCHEDULE, `midweek` for the run a midweek wake-up asked for
-- (`midweek_wakeups.requested_at`). A midweek row's `due_at` is the request's time, and `wakeup_id` names its
-- wake-up. The unique index holds one run per wake-up, and the wake-ups' own index holds one wake-up a week.
-- A weekly run never buys an off-cycle visit; a midweek run buys only one. Each kind sends at most one submission
-- a week (`vending.agent_schedule`).

-- As in 030: a scheduled run in flight locks agent_schedule rows. Give up fast; run it again once no run goes.
set local lock_timeout = '2s';

alter table agent_schedule
    add column kind text not null default 'weekly' check (kind in ('weekly', 'midweek')),
    -- No foreign key, as `agent_run_lease.run_id`: a claim never waits on a lock on midweek_wakeups.
    add column wakeup_id bigint,
    add constraint agent_schedule_midweek_has_wakeup check ((kind = 'midweek') = (wakeup_id is not null));

create unique index agent_schedule_one_run_per_wakeup on agent_schedule (wakeup_id);
