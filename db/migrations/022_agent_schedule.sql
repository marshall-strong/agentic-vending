-- One row per scheduled agent run the worker handled (`vending.agent_schedule`, #213).
--
-- Numbered 022 because PR #204 uses 021; the migrator applies any file not yet applied.
--
-- `due_at` is the primary key, so one time in the schedule gets one row, and so at most one run,
-- whatever restarts or replicas there are. The worker inserts the row before it starts the agent.
-- A time the worker never reached before its latest start gets a `missed` row instead.
--
-- `outcome` is the agent run's own ending (`agent_runs.status`), or `missed`, or `running` while
-- the run goes on. A row left `running` after its worker stopped is marked `failed` by the next
-- worker that takes the schedule's lock.
create table agent_schedule (
    due_at       timestamptz primary key,
    -- The latest the run may start. Past it, the time is recorded as `missed`.
    latest_at    timestamptz not null check (latest_at >= due_at),
    mode         text        not null check (mode in ('live', 'dry-run')),
    claimed_at   timestamptz not null default now(),
    finished_at  timestamptz,
    outcome      text        not null default 'running'
                 check (outcome in ('running', 'submitted', 'skipped', 'halted', 'failed', 'missed')),
    -- The run in `agent_runs`, once the agent has returned. None for a missed time, or a run
    -- whose worker stopped before it returned.
    run_id       bigint      references agent_runs (id),
    -- Why, in a sentence. A failed row's detail can hold an error, so public pages hide it.
    detail       text,
    check ((outcome = 'running') = (finished_at is null))
);
