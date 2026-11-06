-- Our requests to add a product to the catalog (`request_catalog_product`).
--
-- The bridge has no route that lists our requests, so this table is the only
-- record of which ones exist, and the tool's duplicate check reads it.
-- Numbered 005 because PR #25 uses 004; the migrator applies any file not yet
-- applied, so the order they merge in doesn't matter.
--
-- `status` is `sending` from just before the POST until the answer is stored,
-- so a crash in between leaves a row behind instead of losing the request.
-- `pending` means the bridge accepted it. `unknown` means we sent it but never
-- got an answer (a timeout or a server error). For `sending` and `unknown` the
-- request may or may not exist on the bridge: a person checks it on the Swagger
-- page and settles it with `python -m vending.product_requests resolve`, and the
-- tool refuses to send the same product again meanwhile. Nothing
-- updates `pending` yet; a later sync can read `GET /api/v1/product-requests/{id}`.
create table product_requests (
    id             bigserial   primary key,
    requested_at   timestamptz not null default now(),
    name           text        not null,
    size           text        not null,
    line           text,
    flavor         text,
    note           text,
    -- The normalised words of name, flavor and size, for the duplicate check.
    words          text[]      not null,
    bridge_id      text        unique,
    status         text        not null check (status in ('sending', 'pending', 'approved', 'rejected', 'unknown')),
    decision_note  text,
    sku            text,
    -- The bridge's answer as it came, so a field we don't store yet isn't lost.
    last_response  jsonb
);
