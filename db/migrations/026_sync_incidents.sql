-- One row per failed round of the worker's hourly syncs (#220), written by the worker when a round
-- ends with a failed job (`vending.worker.record_failed_round`). The dashboard's incident log reads
-- this table (`vending.report_card.incidents`), so a round that failed stays listed after a later
-- round passes, or after the worker restarts — the worker's latest round itself lives only in its
-- memory, and decides the banner alone.
--
-- `jobs` holds what /health shows of the round: per job its name, exit code and outcome. It is
-- written by our own code and never carries a job's error text. The pages' read-only login only
-- ever selects from it; the syncs' login writes it.

create table sync_incidents (
    id         bigint      generated always as identity primary key,
    round_id   text        not null unique,   -- the round's time (`State.last_round_at`), so a retried write never doubles a round
    round_at   timestamptz not null,          -- the same moment, as a time
    jobs       jsonb       not null check (jsonb_typeof(jobs) = 'array'),
    created_at timestamptz not null default now()
);
