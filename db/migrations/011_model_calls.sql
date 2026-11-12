-- Every request sent with one of our Anthropic keys, whatever it was for.
--
-- The ledger's `tokens` entries are the competition's books: only `live` calls
-- go there, because their cost comes out of the $600. Test runs, replays and
-- model sweeps don't (Kevin, 4 Nov: development spend is outside the $600), but
-- the team still has to see them: what the key was used for, by whom, on which
-- model and effort, and what it cost. This table holds every call, live ones
-- included, so it is the one place the key's whole usage is read from: the
-- dashboard, `python -m vending.usage`, and the daily caps.
--
-- `vending/harness.py` is the only writer. A row is written as `pending` at its
-- worst case before the request is sent, then corrected to what the API billed.
-- A row left `pending` or `error` keeps its worst case, so the table can
-- overstate spend but never understate it.
--
-- Numbered 011 because 008-010 are taken on open branches; the migrator applies
-- any file not yet applied, so the order they merge in doesn't matter.

create table model_calls (
    id               bigserial   primary key,
    at               timestamptz not null default now(),   -- when it was reserved
    finished_at      timestamptz,
    purpose          text        not null check (purpose in ('live', 'test')),
    -- What asked for it: "sweep opus@high slow-seller", "agent week 2026-10-02", ...
    label            text        not null default '',
    -- One Harness: a sweep config, an agent run, one replayed week.
    run_id           text        not null,
    model            text        not null,      -- what was asked for
    answered_by      text,                      -- what the API says answered
    effort           text,
    -- The name of the variable the key came from, never the key: which key paid.
    key_env          text        not null,
    status           text        not null default 'pending'
                     check (status in ('pending', 'ok', 'refused', 'error')),
    stop_reason      text,
    request_id       text,                      -- Anthropic's request-id, for support
    message_id       text,
    input_tokens     integer,
    cache_write_tokens integer,
    cache_read_tokens  integer,
    output_tokens    integer,
    max_tokens       integer     not null,      -- what was sent, after fitting the caps
    worst_case_usd   numeric     not null,
    cost_usd         numeric     not null,      -- worst case until settled
    seconds          numeric,
    ledger_entry     bigint      references ledger_entries (id) on delete set null,
    detail           text        not null default '',
    recorded_by      text        not null,
    -- Where it ran: a hostname, for telling laptops and the container apart.
    host             text        not null default ''
);

create index model_calls_at on model_calls (at);
create index model_calls_run on model_calls (run_id);
