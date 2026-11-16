-- Running totals for the dashboard's Visitors tile (`vending.visits`), so reading the counts
-- doesn't grow with `page_visits`. The writer adds each batch to these in the same
-- transaction as its rows; the week's numbers still come from `page_visits`, through its
-- index on `at`, and only its last 7 days. Nothing in `page_visits` is deleted.
--
-- Numbered 018 because 017 is taken on an open branch (add_backlog_panel).

-- Each browser that has opened a page, once, so the all-time count of browsers is one row.
create table page_visit_browsers (
    browser   text        primary key check (browser ~ '^[0-9a-f]{16}$'),
    first_at  timestamptz not null default now()
);

-- One row: browsers that opened a page, sign-ins and visits, all time, and when counting began.
create table page_visit_totals (
    only_row  boolean     primary key default true check (only_row),
    browsers  bigint      not null default 0,
    sign_ins  bigint      not null default 0,
    visits    bigint      not null default 0,
    since     timestamptz
);

-- What was counted before this migration.
insert into page_visit_browsers (browser, first_at)
select browser, min(at) from page_visits where kind = 'view' group by browser;

insert into page_visit_totals (browsers, sign_ins, visits, since)
select (select count(*) from page_visit_browsers),
       count(*) filter (where kind = 'sign_in'),
       count(*) filter (where kind = 'view'),
       min(at)
  from page_visits;
