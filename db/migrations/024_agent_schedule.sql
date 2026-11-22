-- One row per scheduled agent run the worker handled (`vending.agent_schedule`, #213).
--
-- Scheduled runs are dry runs only: live scheduling is not enabled in this version; it needs a later
-- change approved by the team. So `mode` is never `live` here, and no row is ever `submitted`.
--
-- `due_at` is the primary key, so one time in the schedule gets one row, and so at most one run,
-- whatever restarts or replicas there are. The worker inserts the row before it starts the agent.
-- A time the worker never reached before its latest start gets a `missed` row instead.
--
-- `outcome` is the agent run's own ending (`agent_runs.status`), or `missed` (no run started in the
-- window, the clocks' spring change included), or `running` while the run goes on. A row left
-- `running` after its worker stopped is marked `failed` by the next worker that takes the schedule's
-- lock, once no agent run holds the run lease (`agent_run_lease`, below).
--
-- Nothing writes here while the container's AGENT_SCHEDULE_MODE is unset or off. The agent runs only
-- when `config.agent_schedule_mode` turns it on too (`vending/agent_schedule.py`).
create table agent_schedule (
    due_at       timestamptz primary key,
    -- The latest the run may start. Past it, the time is recorded as `missed`.
    latest_at    timestamptz not null check (latest_at >= due_at),
    -- What the worker ran: `dry-run`, or `off` when the database's setting turned the time off
    -- (then `skipped`). Never `live`: a `live` setting is run as a dry run.
    mode         text        not null check (mode in ('dry-run', 'off')),
    claimed_at   timestamptz not null default now(),
    -- When the run was authorized to start: written in the transaction that read the database's mode
    -- (its row locked) and took the run lease, by the database's clock. None if it never started.
    started_at   timestamptz,
    finished_at  timestamptz,
    outcome      text        not null default 'running'
                 check (outcome in ('running', 'skipped', 'halted', 'failed', 'missed')),
    -- The run in `agent_runs`, once the agent has returned. None for a missed time, or a run
    -- whose worker stopped before it returned.
    run_id       bigint      references agent_runs (id),
    -- Why, in a sentence. A failed row's detail can hold an error, so public pages hide it.
    detail       text,
    check ((outcome = 'running') = (finished_at is null))
);

-- Which agent run may go now (`vending.run_lease`): one row, for every run, by hand or scheduled.
-- A run takes it with a conditional update and renews it every minute while it goes. Unlike a
-- session lock it outlives a dropped or killed connection: only the run's token gives it back, or
-- time. A run that stops renewing (its process died or stalled) loses it at `expires_at`; the next
-- run takes it over and records the old run as failed. No renewal goes past `hard_until`, the run's
-- deadline plus a margin, so a stuck run can't hold it for ever. The run checks it still holds it
-- before each model request (in the same transaction that reserves its budget, renewing it then) and
-- before a purchase request is sent, and stops if it doesn't.
create table agent_run_lease (
    one          boolean     primary key default true check (one),
    token        uuid,
    -- The holder's `agent_runs` id, once it has one. No foreign key: taking or giving back the
    -- lease must never wait on a lock on `agent_runs`.
    run_id       bigint,
    taken_at     timestamptz,
    renewed_at   timestamptz,
    expires_at   timestamptz,
    hard_until   timestamptz,
    check ((token is null) = (expires_at is null)),
    check (expires_at is null or expires_at <= hard_until)
);
insert into agent_run_lease (one) values (true);
