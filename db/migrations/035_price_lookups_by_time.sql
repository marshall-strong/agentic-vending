-- Indexes for the dashboard's price-check panel (`vending.price_check_panel`), so its reads stay bounded as the
-- lookup log grows over the quarter.
--
-- price_lookups_by_who: each weekly check's models, read by who ran the lookups and when
--                       (`by = 'weekly price check' and at between claimed_at and finished_at`).
-- price_lookups_at:     the lookup spend this week and since the opening (`at between ...`).
create index price_lookups_by_who on price_lookups (by, at);
create index price_lookups_at on price_lookups (at);
