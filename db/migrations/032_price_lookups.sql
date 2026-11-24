-- The price memory (#202, Kevin 23 Nov): every web lookup `vending.price_search` makes, one row each.
--
-- Weekly price checks are worth the spend, so they should spend as little as they can. This table lets a
-- run use what earlier lookups learned: a store that had no listing for a drink (`not_found`) is skipped for
-- `price_dead_end_days` (default 21), and `python -m vending.price_search SKU --history` shows them.
--
-- kind:      search (the open search), listing (a re-check of a link we already have, one search) or check
--            (a better model checking a cheaper model's find before it is stored).
-- asked_min, asked_max: the pack sizes asked for: one size for a one-store lookup, a range for a comparison.
-- units_per_pack, price_cents, url, stock: what the store's page showed, when the report got that far (an
--            out-of-stock pack keeps its price here; it is never a usable price in `sourcing_options`).
-- outcome:   found, or the refusal code (not_found, out_of_stock, check_disagrees, ...).
-- no_store:  a benchmark run (`--no-store`): searched, nothing stored.
-- by:        the person who ran the command, or `agent-search` for the live agent.
-- Only our own lookups on the four stores' own sites; nothing about the other team.
create table price_lookups (
    id             bigserial primary key,
    at             timestamptz not null default now(),
    sku            text not null,
    store          text not null check (store in ('Amazon', 'Walmart', 'Costco', 'BJs')),
    kind           text not null check (kind in ('search', 'listing', 'check')),
    asked_min      integer not null check (asked_min between 1 and 60),
    asked_max      integer not null check (asked_max between asked_min and 60),
    units_per_pack integer check (units_per_pack between 1 and 60),
    price_cents    integer check (price_cents > 0),
    url            text,
    stock          text check (stock in ('in_stock', 'out_of_stock', 'unknown')),
    outcome        text not null,
    model          text not null,
    cost_usd       numeric(12, 6) not null default 0,
    searches       integer,
    no_store       boolean not null default false,
    by             text not null,
    detail         text
);

create index price_lookups_by_drink on price_lookups (sku, store, at desc);
