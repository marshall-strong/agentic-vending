-- Every poll of the machine, from the bridge's stored history.
--
-- `raw` keeps the document byte-for-byte (CRLF and all) because the G85
-- checksum covers those exact bytes. Counters are only extracted from polls
-- whose checksum passes; a failing poll is kept raw with `crc_ok = false` and
-- no rows in `poll_selections`.

create table polls (
    session_id        text primary key,
    device_id         text        not null,
    started_at        timestamptz not null,
    raw               bytea       not null,
    byte_count        integer     not null,
    crc_ok            boolean     not null,
    machine_serial    text,
    decimal_places    smallint,
    -- VA1: machine-wide lifetime paid vends. Lifetime counters never reset;
    -- sales over a period are the difference between two polls.
    total_paid_cents  bigint,
    total_paid_units  integer,
    synced_at         timestamptz not null default now()
);

create index polls_device_time on polls (device_id, started_at);

-- PA1/PA2 for every selection in a poll: price and lifetime counters.
create table poll_selections (
    session_id            text    not null references polls (session_id) on delete cascade,
    selection             text    not null,
    price_cents           integer,
    lifetime_units        integer not null,
    lifetime_sales_cents  bigint  not null,
    primary key (session_id, selection)
);

create index poll_selections_selection on poll_selections (selection, session_id);
