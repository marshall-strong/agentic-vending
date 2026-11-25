-- Whether the store had the pack in stock when a price lookup saw it (#237, #202 part 2).
--
-- `vending.price_search` asks the model to report what the store's own page or
-- search result says: `in_stock`, `out_of_stock` or `unknown`. A price on an
-- out-of-stock page is not stored as a usable price, so the second check refuses
-- a row marked `out_of_stock` that has an expected price. Null means nobody
-- recorded it: a person's row, or a price taken from a receipt.
--
-- Numbered 031 to come after 029 and 030 on `add_live_schedule` (#238).
alter table sourcing_options
    add column stock text check (stock in ('in_stock', 'out_of_stock', 'unknown')),
    add check (stock is distinct from 'out_of_stock' or expected_cents is null);
