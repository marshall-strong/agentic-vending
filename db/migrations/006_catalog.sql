-- The catalog: the drinks our team can order, copied from `GET /api/v1/products`
-- by the catalog sync, so `get_catalog` reads our database and never the bridge.
--
-- Products are single items (`sku`, `name`, `line`, `flavor`, `size`), with no
-- prices or pack sizes: those belong to our sourcing table and the purchase
-- line. `flavor` is null for a plain product (Coca-Cola 20 oz). The bridge may
-- add or drop fields, so `last_response` keeps each product as it last came.
--
-- A product the bridge stops listing is not deleted: `delisted_at` is set, so
-- old purchase requests and receipts still name a known product, and a later
-- sync that sees it again clears it. "In the catalog" means `delisted_at is null`.
create table catalog_products (
    sku            text        primary key check (sku <> ''),
    name           text        not null check (name <> ''),
    line           text,
    flavor         text,
    size           text,
    first_seen_at  timestamptz not null default now(),
    last_seen_at   timestamptz not null default now(),
    delisted_at    timestamptz,
    last_response  jsonb       not null
);

-- One row per completed catalog sync, so `get_catalog` can say how old its
-- data is and a stale catalog shows up instead of passing as current.
create table catalog_syncs (
    id             bigserial   primary key,
    synced_at      timestamptz not null default now(),
    product_count  integer     not null check (product_count >= 0),
    added          integer     not null default 0,
    delisted       integer     not null default 0
);

create index catalog_syncs_at on catalog_syncs (synced_at);
