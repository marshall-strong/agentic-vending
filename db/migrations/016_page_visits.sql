-- Who looks at the container's pages: one row per sign-in and per visit (`vending.visits`).
--
-- `browser` is a random id kept in that browser's own cookie. There are no names
-- (the pages have one shared password) and no IP addresses or browser strings.
-- A visit is a browser opening /dashboard or /db, at most once per page per half
-- hour; the dashboard's own 30-second refresh never counts.
--
-- The worker writes these with the syncs' login. The pages' read-only login reads
-- them for the dashboard's Visitors tile, through the default privileges that
-- `python -m vending.pages grant` set up.
--
-- Numbered 016 because 015 is taken on an open branch (#132, the sourcing table).

create table page_visits (
    id       bigint      generated always as identity primary key,
    at       timestamptz not null default now(),
    kind     text        not null check (kind in ('sign_in', 'view')),
    page     text        not null check (page in ('/login', '/dashboard', '/db')),
    browser  text        not null check (browser ~ '^[0-9a-f]{16}$'),
    check ((kind = 'sign_in') = (page = '/login'))
);

create index page_visits_at on page_visits (at);
