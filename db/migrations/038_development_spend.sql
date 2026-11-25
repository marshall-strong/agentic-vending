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
