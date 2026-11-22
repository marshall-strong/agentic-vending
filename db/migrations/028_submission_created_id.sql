-- The request ID from a validated `201` to a purchase POST (#217).
--
-- `submit_desired_state` sets it, with the answer, before it settles the row,
-- and only when the answer was the bridge's documented acceptance in full: a
-- `201`, `application/json`, read whole within the deadline and size cap, with
-- the `PurchaseRequestPending` body. Any other answer leaves it null, even one
-- that names a request.
--
-- `python -m vending.submissions check` (and a live agent run before its model
-- starts) settles an open row only if this is set, and only from a `GET` of this
-- ID that answers for it. A row without it needs a person:
-- `python -m vending.submissions resolve`.

alter table purchase_submissions add column created_id text;
