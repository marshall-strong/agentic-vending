-- When the worker opened and closed the machine's door on a fulfilled request.
--
-- The receipt carries them per request, beside `fulfilled_at`, to the minute.
-- They are null on older requests and when the worker leaves them out.
-- `vending.stock` and `get_machine_state` use them to date the visit: the old
-- product ends at door open and the new one starts at door close. Without
-- them, each line's `resolved_at` stands in, as before. That is when the worker
-- filled in the line, not when it went in the machine.
--
-- They are stored as a pair or not at all, and close is never before open.
-- `vending.purchase_requests.store` refuses an answer with close before open,
-- so this check only backs it up.

alter table purchase_requests
    add column door_opened_at timestamptz,
    add column door_closed_at timestamptz,
    add constraint purchase_requests_door_times
        check ((door_opened_at is null) = (door_closed_at is null) and door_closed_at >= door_opened_at);

-- Backfill from the answers already stored, under the same rules as `store`.
-- The bridge writes ISO times with a zone; one without is UTC, as in `store`.
-- A pair with a value that isn't a time string, or with close before open, is
-- skipped. Rows that already have door times are left alone.
set local time zone 'UTC';

update purchase_requests r
   set door_opened_at = d.opened, door_closed_at = d.closed
  from (
      select request_id,
             (last_response ->> 'door_opened_at')::timestamptz as opened,
             (last_response ->> 'door_closed_at')::timestamptz as closed
        from purchase_requests
       where jsonb_typeof(last_response) = 'object'
         and last_response ->> 'door_opened_at' ~ '^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}'
         and last_response ->> 'door_closed_at' ~ '^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}'
  ) d
 where r.request_id = d.request_id
   and d.closed >= d.opened
   and r.door_opened_at is null;
