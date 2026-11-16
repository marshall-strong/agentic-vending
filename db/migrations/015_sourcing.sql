-- Our sourcing table: where to buy each catalog product, in what pack, and the
-- most to pay for it.
--
-- The catalog (006) names the products we may order but carries no prices or
-- packs (David, 19 Oct: "cost comes from the store and the receipt"). Every
-- purchase line needs a store, a pack size, a max price per pack and, for
-- Amazon, a link. People look those up and record them here with
-- `python -m vending.sourcing set`; the agent has no tool that browses stores.
--
-- One row per product and rank: a `primary` way to buy it and an optional
-- `alternate`, the same shape as a purchase line. Prices are integer cents.
-- `checked_at` is when a person saw the price, so a stale price can be flagged;
-- setting a row again replaces it, and `action_log` keeps the old values.
--
-- The limits match the live purchase-request API and `vending.planner`, so a
-- row stored here is never refused for its shape.
create table sourcing_options (
    sku               text        not null references catalog_products (sku),
    rank              text        not null check (rank in ('primary', 'alternate')),
    store             text        not null check (store in ('Walmart', 'Costco', 'Amazon', 'BJs')),
    units_per_pack    integer     not null check (units_per_pack between 1 and 60),
    expected_cents    integer     check (expected_cents > 0),
    max_cents         integer     not null check (max_cents > 0 and max_cents <= 20000),
    url               text        check (url <> ''),
    pack_description  text        check (char_length(pack_description) between 1 and 100),
    checked_at        timestamptz not null,
    checked_by        text        not null check (checked_by <> ''),
    note              text,
    updated_at        timestamptz not null default now(),
    primary key (sku, rank),
    check (store <> 'Amazon' or url is not null),
    check (expected_cents is null or expected_cents <= max_cents)
);
