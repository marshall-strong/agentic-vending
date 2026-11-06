-- Our purchase requests and what the worker did with each line.
--
-- The bridge has no route that lists our requests, so this table is the only
-- record of which IDs exist: `submit_desired_state` inserts a row the moment
-- the bridge accepts a request, and `python -m vending.sync_requests` reads
-- each one back until it is decided.
--
-- Money is `numeric` with no scale, so the decimal strings the bridge sends
-- are stored exactly. Code turns them into cents and never into floats.

create table purchase_requests (
    request_id     text        primary key,
    submitted_at   timestamptz not null,
    off_cycle      boolean     not null,
    -- The planner's hash of the plan this request came from, so a receipt can
    -- be traced back to the plan the agent approved.
    plan_hash      text,
    -- The exact body we POSTed.
    request_body   jsonb       not null,
    status         text        not null default 'pending'
                   check (status in ('pending', 'rejected', 'fulfilled')),
    decision_note  text,
    -- From the bridge once fulfilled: what the whole request cost.
    total_paid     numeric,
    fulfilled_at   timestamptz,
    -- The bridge's last change to the request. Later losses move it.
    updated_at     timestamptz,
    -- When we last read it, whatever it said.
    last_checked   timestamptz,
    -- The bridge's last answer, verbatim, so a field we don't store yet isn't lost.
    last_response  jsonb
);

create index purchase_requests_open on purchase_requests (status) where status = 'pending';

-- One row per line of a fulfilled request: what we asked for and the receipt.
-- Column names are the receipt's own. Rows are replaced as a set each time the
-- request is read, so a corrected receipt simply overwrites the old one.
create table purchase_lines (
    request_id             text        not null references purchase_requests (request_id) on delete cascade,
    line_no                integer     not null,
    slot                   text        not null,
    source                 text        not null check (source in ('purchase', 'back_stock')),
    -- What we asked for on this line, as the bridge echoes it back
    -- (`primary`, `alternate` and `load`).
    asked                  jsonb       not null,
    outcome                text        not null check (outcome in ('primary', 'alternate', 'loaded', 'unavailable')),
    sku                    text,
    store                  text,
    packs_bought           integer     not null,
    units_per_pack         integer,
    units_bought           integer     not null,
    total_paid             numeric,
    unit_cost_paid         numeric,
    units_loaded           integer     not null,
    units_lost             integer     not null,
    loss_note              text,
    units_to_back_stock    integer     not null,
    units_from_back_stock  integer     not null,
    units_pulled           integer     not null,
    pulled_sku             text,
    slot_reassigned        boolean     not null,
    resolved_at            timestamptz not null,
    primary key (request_id, line_no)
);

create index purchase_lines_slot on purchase_lines (slot, resolved_at);

-- `later_losses` from a receipt: units of what the line stocked that were found
-- broken or missing afterwards, in the coil or in our back stock. The bridge
-- only appends to the list, but we store the whole list again on every read
-- (delete, then insert, in one transaction), so re-reading never counts a loss
-- twice. `position` is the index in the bridge's list, oldest first.
create table purchase_line_losses (
    request_id   text        not null,
    line_no      integer     not null,
    position     integer     not null,
    location     text        not null check (location in ('coil', 'back_stock')),
    sku          text        not null,
    units        integer     not null,
    note         text,
    recorded_at  timestamptz not null,
    primary key (request_id, line_no, position),
    foreign key (request_id, line_no) references purchase_lines (request_id, line_no) on delete cascade
);
