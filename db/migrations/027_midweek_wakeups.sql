-- The midweek wake-up (#219): the flag the wake check leaves for the agent's next run.
--
-- `vending.midweek_wake` (the worker's job, on when MIDWEEK_WAKE=on) inserts one row when a
-- slot of ours (the server's assignment) is projected to sell out before this week's Friday
-- restock and a paid midweek visit would pay more than it saves (docs/restock-policy.md,
-- section 2; #110 item 3). The reason names the slots, their projection and the saved margin,
-- so the dashboard's Needs attention and the agent's briefing can show why without re-deciding.
--
-- At most one row per Saturday-to-Friday week (Eastern): the unique index is the cap, so
-- restarts and racing replicas cannot double it (`vending.midweek_wake.record`).
--
-- Numbered 027 because 022-023 (PR #222), 024 (PR #224), 025 (PR #223) and 026 (PR #228) are taken; the migrator
-- applies any file not yet applied, so the order PRs merge in doesn't matter.
--
-- `requested_at` is when `request_midweek_run` asked for a midweek agent run. Until the
-- scheduled agent (#224) picks the request up, asking only records and logs: nothing starts
-- a run from this table yet.
create table midweek_wakeups (
    id                 bigint      generated always as identity primary key,
    week_start         date        not null,    -- the Saturday that starts the week, Eastern
    decided_at         timestamptz not null default now(),
    reason             text        not null,
    slots              text[]      not null,
    saved_margin_cents integer     not null,    -- the sales the visit would save, exact to the cent
    requested_at       timestamptz,
    requested_by       text
);

create unique index midweek_wakeups_one_per_week on midweek_wakeups (week_start);
