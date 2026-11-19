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

-- purchase_requests is live: the sync writes to it every hour. Give up rather
-- than queue behind a long transaction and block everything queued behind us.
set local lock_timeout = '10s';
-- The bridge writes ISO times with a zone; one without is UTC, as in `store`.
set local time zone 'UTC';

alter table purchase_requests
    add column door_opened_at timestamptz,
    add column door_closed_at timestamptz,
    add constraint purchase_requests_door_times
        check ((door_opened_at is null) = (door_closed_at is null) and door_closed_at >= door_opened_at);

-- Backfill from the answers already stored, under the same rules as `store`.
-- A value is only cast once it is known to be a whole, valid time: a regex
-- alone lets through `2026-11-31T15:34:00Z`, whose cast would abort the
-- migration. The regex still guards the front, since Postgres also accepts
-- words such as 'now' as times. A pair with only one time, a value that isn't
-- a valid time, or close before open is skipped and named in a notice. Rows
-- that already have door times are left alone.
do $$
declare
    filled int;
    skipped text[];
begin
    with answers as (
        select request_id,
               last_response ->> 'door_opened_at' as opened_text,
               last_response ->> 'door_closed_at' as closed_text
          from purchase_requests
         where jsonb_typeof(last_response) = 'object'
           and door_opened_at is null
    ), parsed as (
        select request_id,
               case when opened_text ~ '^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}'
                     and pg_input_is_valid(opened_text, 'timestamptz')
                    then opened_text::timestamptz end as opened,
               case when closed_text ~ '^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}'
                     and pg_input_is_valid(closed_text, 'timestamptz')
                    then closed_text::timestamptz end as closed
          from answers
         where opened_text is not null or closed_text is not null
    ), updated as (
        update purchase_requests r
           set door_opened_at = p.opened, door_closed_at = p.closed
          from parsed p
         where r.request_id = p.request_id
           and p.closed >= p.opened
        returning r.request_id
    )
    select (select count(*) from updated),
           array(select request_id from parsed
                  where opened is null or closed is null or closed < opened
                  order by request_id)
      into filled, skipped;
    raise notice '019: door times filled in on % request(s)', filled;
    if cardinality(skipped) > 0 then
        raise notice '019: % request(s) skipped, with only one door time, one that is not a valid time, '
                     'or close before open; left null: %', cardinality(skipped), array_to_string(skipped, ', ');
    end if;
end
$$;
