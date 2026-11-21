-- Our books as sent to the bridge's tracking check (`POST /api/v1/tracking/check`), and its answers.
--
-- `python -m vending.tracking check --send` inserts the row before it sends, then fills in the
-- answer, so a send whose answer never came still shows (`status` null, `used_check` null).
--
-- `used_check` says whether the day's one check is gone after this row:
--   true   the bridge answered 200, or 429 `daily_check_used` (today's check was already used);
--   false  the bridge refused it before checking (403, 404, 409, 422), or it never arrived;
--   null   unknown: sent, but no usable answer (a timeout, a 5xx, a dropped connection).
-- From the competition start, a second send on the same Eastern day is refused unless every earlier
-- row that day is `false` (`vending.tracking`).
--
-- The request holds only our slots (the server's assignment) and our own closet.

create table tracking_checks (
    id              bigint      generated always as identity primary key,
    sent_at         timestamptz not null,
    eastern_day     date        not null,       -- `sent_at` in America/New_York: the bridge's day
    sent_by         text        not null,
    as_of           text        not null,       -- the poll's session id
    as_of_time      timestamptz not null,       -- that poll's `started_at`, from our copy
    request         jsonb       not null check (jsonb_typeof(request) = 'object'),
    answered_at     timestamptz,
    status          integer,                    -- HTTP status; null: no answer
    answer          jsonb,                      -- the bridge's JSON answer, success or error
    error_type      text,                       -- the answer's `error_type`, or our own word for no answer
    used_check      boolean,
    results         jsonb,                      -- on a 200: cash, sales_tax_owed, coil, back_stock -> within | outside
    next_check_from timestamptz
);

create index tracking_checks_day on tracking_checks (eastern_day);
