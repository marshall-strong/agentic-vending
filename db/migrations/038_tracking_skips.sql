-- Rounds of the worker's daily tracking check that sent nothing on purpose (#254), one row per Eastern day and
-- reason, so the dashboard can show a Check line instead of a failed job.
--
-- reason:    not_loaded: no counted receipt has loaded `slots` (a day-one line that came back unavailable),
--            and that is the only reason the figures can't be worked out. Nothing is guessed and nothing is sent
--            until the slots are loaded (`vending.tracking.NotLoaded`).
-- slots:     the slots of the latest round, our own (the server's assignment).
-- first_at, last_at, rounds: the day's first and latest skipped round, and how many.
create table tracking_skips (
    id          bigint      generated always as identity primary key,
    eastern_day date        not null,
    reason      text        not null check (reason in ('not_loaded')),
    slots       text[]      not null,
    first_at    timestamptz not null,
    last_at     timestamptz not null,
    rounds      integer     not null default 1 check (rounds >= 1),
    unique (eastern_day, reason)
);
