-- One row per run of the agent: what it planned, what it told the team, and
-- what it noted for next week. The dashboard reads this table; the agent loop
-- (#39) writes it through `vending.agent_runs`.
--
-- Numbered 009 because open PRs use 006 to 008; the migrator applies any file
-- not yet applied, so the order they merge in doesn't matter.
--
-- A run is inserted as `running` when it starts, so the dashboard shows it
-- while it works, and updated once when it ends. A row left `running` long
-- after it started means the process died; the dashboard says so.
--
-- `plan` is the plan in the replayer's decider shape (`docs/week-replayer.md`
-- on #28): {"visit", "reason", "slots": [{"slot", "fill_to", "sale_price",
-- "product", "keep", "reason"}]}. Stored as the agent gave it, so a field we
-- don't show yet isn't lost.
create table agent_runs (
    id           bigserial   primary key,
    started_at   timestamptz not null default now(),
    finished_at  timestamptz,
    -- `live` sends the purchase request, `dry-run` stops before it,
    -- `replay` is a past week from the replayer.
    mode         text        not null check (mode in ('live', 'dry-run', 'replay')),
    model        text,
    status       text        not null default 'running'
                 check (status in ('running', 'submitted', 'skipped', 'halted', 'failed')),
    plan         jsonb,
    -- The weekly update for the team, and the agent's notes to itself.
    team_update  text,
    notes        text,
    -- Why it skipped, halted or failed, in a sentence.
    detail       text,
    -- What the run's model calls cost, in dollars. Exact, never a float.
    cost_usd     numeric,
    -- The purchase request it submitted, if any.
    request_id   text,
    check ((status = 'running') = (finished_at is null))
);

create index agent_runs_started on agent_runs (started_at);
