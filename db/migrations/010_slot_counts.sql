-- What a person saw in the machine: one row per selection per count.
--
-- The polls give each selection's price and sales, but never which product is
-- in it, and receipts only name what we stocked ourselves. A count fills that
-- gap: someone looks at (or photographs) the machine and records, for every
-- selection, the product and how many units were on the rail. Selections that
-- aren't ours are recorded too, so a change in which slots are ours loses
-- nothing.
--
-- `units` is null when it couldn't be counted. `exact` is false for an
-- estimate, such as bottles counted from a photo where the rail goes back
-- further than the picture shows. `product` is free text as seen, because
-- what staff loaded may not be in our catalog.
--
-- Numbered 010 because open PRs use 006 to 009; the migrator applies any file
-- not yet applied, so the order they merge in doesn't matter.
create table slot_counts (
    count_id      text        not null,
    counted_at    timestamptz not null,
    counted_by    text        not null,
    source        text        not null check (source in ('photo', 'door')),
    device_id     text        not null,
    selection     text        not null,
    product       text,
    units         integer     check (units >= 0),
    exact         boolean     not null,
    price_on_tag  numeric,
    note          text,
    recorded_at   timestamptz not null default now(),
    primary key (count_id, selection)
);

create index slot_counts_selection on slot_counts (device_id, selection, counted_at);
