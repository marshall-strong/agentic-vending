-- Our backlog of catalog requests: drinks we want to ask for next, in order.
--
-- The bridge stops taking product requests while too many of ours are open
-- (about 24 on 15 Nov 2026), so the next batch has to wait until the
-- instructors decide earlier ones. This table keeps that next batch in one place,
-- in priority order, with why each drink is on it, so it isn't lost in a
-- scratch file and the dashboard can show it.
--
-- `python -m vending.request_backlog` adds, orders, drops and files them.
-- Filing goes through the agent's own write gate and `request_catalog_product`,
-- so the halt switch, the duplicate checks and the weekly limit all apply; a
-- filed row points at the `product_requests` row it became.
--
-- `queued` rows are the backlog; `position` orders them (1 = file first).
-- `filed` means the bridge took it (or may have: see `product_requests.status`).
-- `dropped` means a person removed it, or filing found it already requested or
-- already in the catalog; `drop_reason` says which.
-- Numbered 017 because PR #138 uses 016; the migrator applies any file not yet
-- applied, so the order they merge in doesn't matter.
create table product_request_backlog (
    id                  bigserial   primary key,
    position            integer     check (position > 0),
    name                text        not null check (name <> ''),
    size                text        not null check (size <> ''),
    line                text,
    flavor              text,
    note                text,
    why                 text,
    -- Projected gross profit this drink adds to the best day-one plan, if someone
    -- worked it out (integer cents; a planning estimate, not a measurement).
    est_gain_cents      integer,
    source              text,
    added_by            text        not null check (added_by <> ''),
    added_at            timestamptz not null default now(),
    status              text        not null default 'queued' check (status in ('queued', 'filed', 'dropped')),
    status_at           timestamptz,
    product_request_id  bigint      references product_requests (id),
    drop_reason         text,
    check ((status = 'queued') = (position is not null)),
    check (status <> 'dropped' or drop_reason is not null)
);
