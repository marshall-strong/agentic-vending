-- One row per weekly price check the worker handled (`vending.price_schedule`, #202).
--
-- Off unless the container's PRICE_CHECK_MODE and `config.price_check_mode` both turn it on; nothing writes here
-- while the container's mode is unset or off. `due_at` is the primary key, so one time in the schedule gets one
-- row, and so at most one run, whatever restarts or replicas there are.
--
-- mode:     what the worker ran: `live` (searched and stored), `dry-run` (listed what it would look up, spent
--           nothing), or `off` (a time the database's setting turned off, or one no worker reached).
-- compare:  the run compared every store (every `price_compare_every_days`), else it re-checked primary stores.
-- outcome:  running, done, partial (finished, but a drink got no usable price), failed, skipped (off, or the
--           weekly cap used), halted (the halt switch), missed (no run started before `latest_at`).
-- drinks, changed, failures, cost_usd: what the run looked up, how many prices it changed, how many drinks got
--           no usable price, and what its lookups cost. Null until it ends, and for a time that never ran.
-- Only our own drinks and prices; nothing about the other team.
create table price_check_runs (
    due_at      timestamptz primary key,
    latest_at   timestamptz not null check (latest_at >= due_at),
    mode        text        not null check (mode in ('live', 'dry-run', 'off')),
    compare     boolean     not null default false,
    claimed_at  timestamptz not null default now(),
    finished_at timestamptz,
    outcome     text        not null default 'running'
                check (outcome in ('running', 'done', 'partial', 'failed', 'skipped', 'halted', 'missed')),
    drinks      integer,
    changed     integer,
    failures    integer,
    cost_usd    numeric(12, 6),
    detail      text,
    check ((outcome = 'running') = (finished_at is null))
);
