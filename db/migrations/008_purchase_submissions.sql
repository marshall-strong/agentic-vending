-- Every attempt `submit_desired_state` makes to send a purchase request.
--
-- The bridge has no route that lists our requests, so a POST whose answer we
-- lose is a request we may never find again. `submit_desired_state` therefore
-- commits a `sending` row here, on its own connection, before the POST, and
-- settles it after: `accepted` (with the bridge's ID, which also goes into
-- `purchase_requests`), `refused` (a 4xx: certainly not filed), `not_filed`
-- (the request never left: no connection), or `unknown` (sent, no usable
-- answer: it may or may not exist). While any row is `sending` or `unknown`,
-- the tool refuses to submit again, until a person checks the bridge and runs
-- `python -m vending.submissions resolve`.
--
-- Numbered 008: 006 is Sean's catalog branch, 007 the ledger.

create table purchase_submissions (
    id           bigserial   primary key,
    at           timestamptz not null default now(),
    plan_hash    text        not null,
    -- The exact body we POSTed, and the directive the worker would read.
    body         jsonb       not null,
    directive    text,
    -- The midweek visit fee the plan quoted, for an off-cycle request; null
    -- otherwise. It travels into `purchase_requests.visit_fee_cents` when the
    -- row is accepted or resolved, so the ledger charges what was agreed then.
    visit_fee_cents integer,
    status       text        not null default 'sending'
                 check (status in ('sending', 'accepted', 'refused', 'not_filed', 'unknown')),
    request_id   text,
    -- The bridge's answer, verbatim, or the error body.
    response     jsonb,
    detail       text,
    resolved_by  text,
    resolved_at  timestamptz
);

create index purchase_submissions_open on purchase_submissions (status) where status in ('sending', 'unknown');
