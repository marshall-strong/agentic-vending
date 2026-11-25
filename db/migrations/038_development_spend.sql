-- Model spend recorded outside the team database. These rows never enter the competition ledger
-- or the harness's live and test request budgets.
create table development_spend (
    source_id   text primary key,
    at          timestamptz not null,
    category    text not null check (category in ('simulations/bench', 'manual')),
    run_id      text not null,
    label       text not null,
    model       text not null,
    cost_usd    numeric not null check (cost_usd >= 0),
    confirmed   boolean not null,
    tokens_in   integer check (tokens_in >= 0),
    tokens_out  integer check (tokens_out >= 0),
    recorded_by text not null
);

-- A paid request reserves its worst case here before the harness sends it.
-- Scratch and team databases use the same table shape, but only the configured
-- spend database receives the durable row.
create table model_spend (
    id           uuid primary key,
    at           timestamptz not null default clock_timestamp(),
    purpose      text not null check (purpose in ('live', 'test')),
    category     text not null check (category in ('agent runs', 'price search', 'simulations/bench')),
    label        text not null,
    run_id       text not null,
    model        text not null,
    cost_usd     numeric not null check (cost_usd >= 0),
    confirmed    boolean not null default false,
    tokens_in    integer,
    tokens_out   integer,
    request_id   text unique,
    refused      boolean not null default false
);

alter table model_calls add column spend_id uuid;
create unique index model_calls_spend_id on model_calls (spend_id) where spend_id is not null;

-- New lookups link directly to the harness row whose cost they copy.
alter table price_lookups add column model_call_id bigint references model_calls(id) on delete set null;
