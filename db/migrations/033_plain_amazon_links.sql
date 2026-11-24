-- Store Amazon links plain (#202, 23 Nov): `https://www.amazon.com/dp/<ASIN>`, with no slug, query, `ref=`
-- path or affiliate tag. A search cited `https://www.amazon.com/dp/B09RGKQW2L/ref=nosim?tag=happywishlist-20`,
-- someone else's tag, and the worker would have bought through it. `vending.sourcing.set_option` stores links
-- plain from now on; this cleans the rows already stored, and logs each change in `action_log` with the link it
-- replaced, as the table's own writers do. Safe to run again: a plain link is left alone.
with changed as (
    select sku, rank, url as old_url,
           'https://www.amazon.com/dp/'
           || upper(substring(url from '(?i)^https?://(?:[^/?#]*\.)?amazon\.com(?:/[^/?#]+)?/(?:dp|gp/product)/([A-Za-z0-9]{10})(?:[/?#]|$)'))
           as new_url
    from sourcing_options
    where store = 'Amazon'
      and url ~* '^https?://(?:[^/?#]*\.)?amazon\.com(?:/[^/?#]+)?/(?:dp|gp/product)/[A-Za-z0-9]{10}(?:[/?#]|$)'
), updated as (
    update sourcing_options s set url = c.new_url, updated_at = now()
    from changed c
    where s.sku = c.sku and s.rank = c.rank and c.new_url is not null and s.url is distinct from c.new_url
    returning s.sku, s.rank, c.old_url, c.new_url
)
insert into action_log (kind, tool, arguments, outcome, detail)
select 'write', 'sourcing.plain_link',
       jsonb_build_object('sku', sku, 'rank', rank, 'old_url', old_url, 'new_url', new_url, 'by', 'migration 033'),
       'done', 'sourcing ' || sku || ' ' || rank || ': Amazon link stored plain, ' || new_url || ' (was ' || old_url || ')'
from updated;
