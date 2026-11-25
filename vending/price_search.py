"""Look up one pack's price with a capped web search on the store's own site (#202 part 2), as the live agent
or as a person.

    searcher = PriceSearch(harness, connect, model="claude-opus-5-5")
    text, failed = searcher.lookup({"sku": "DRPEPPER-20BOT", "store": "Amazon", "units_per_pack": 24})

    python -m vending.price_search --plan picked --by NAME [--max-age 24] [--dry-run]   # the day-one fill (#234)
    python -m vending.price_search --plan ours --max-age 24 --by NAME                   # the weekly check
    python -m vending.price_search SKU --pack 24 --compare --by NAME                    # one drink, every store
    python -m vending.price_search --plan picked --compare --no-store --model M --json out.json --by NAME
    python -m vending.price_search SKU --store Amazon --pack 24 --by NAME [--no-fallback] [--max-usd 5.00]
    python -m vending.price_search SKU --history                                        # its latest lookups

Receipts keep the prices of drinks we already buy fresh (`vending.receipt_prices`). A drink we have never
bought, or one whose price is over a week old (`sourcing.STALE_AFTER`), has no checked price, and nobody is
watching the table once the competition runs. So from the start (3 Dec) the live agent has one more tool,
`find_pack_price`. The repo has no competition end date yet (run week 9 isn't confirmed,
`docs/agent/goal-and-scoring.md`), so the tool has no end gate.

**The command** runs the same lookup for people, before the start too, so nobody checks prices by hand. With
`--plan picked` it looks up every drink in the picked plan that the fill would refuse (`plan_needs`: no row, or
a primary or alternate with no checked price or a stale one); with a SKU, that one pack. Each lookup goes through
the same `Harness`, guardrails, halt switch, `write_gate.run_write` and `action_log`. The run is one harness, and
its run cap is the invocation's cap (`--max-usd`, default `DEFAULT_CLI_CAP_CENTS`, at most `HARD_CLI_CAP_CENTS`);
the purpose's daily model cap applies on top. Before the start its spend is `test` (dev spend on the key, in
`model_calls`, not the ledger); from the start, `live`. Its calls are labelled `CLI_LABEL`, so the live agent's
$1 a day doesn't count them. It prints each lookup's cost and the run's total.

**Order at the lowest price** (Kevin, 23 Nov; `PriceSearch.compare`, the default with `--plan`). For each drink
the command prices every approved store and keeps the cheapest usable pack per unit as the primary and the next
best as the alternate. Usable: a pack the drink's slots can use at one fill (`planner.pack_usable`; extras go to the
closet at cost), not out of stock, from a store whose cutoff for the next stocking hasn't passed (`next_visit`,
`past_cutoff`, the fill's rule), and passing every check below. A store with a fresh row is compared from the table
without a search; a store past its cutoff isn't searched. `--max-age HOURS` re-prices rows checked longer ago than
that, though fresh by the 7-day rule. `--plan ours` adds the drinks in our slots now (`current_drinks`), and
`check_prices` is that for a weekly job. `--no-store` is a benchmark: it searches every store, saves what each gave
(`--json`), writes nothing to the table, and books its spend as test spend (`BENCH_LABEL`).
`scripts/compare_price_runs.py` lines up two or three such files. The agent's tool still prices one store a call.

**The model.** The `price_search_model` setting (a model `model_prices.json` prices), else the agent's model. The
command's `--model` overrides it.

**Other stores** (`--first-usable`, the old way). When the store tried gives no usable price (`FALLBACK_ON`: blocked or no answer, no listing,
no product page, another pack, out of stock, a price we can't use there), `with_fallback` tries the other stores
in `STORE_ORDER`, for the same pack, within the same caps, and the command says which store gave the price.
The live agent tries one store a call and picks the next itself.

**How a lookup works.** One Messages API request through the run's own `Harness`, with Anthropic's basic web
search tool (`web_search_20250305`) and a `report_price` tool the model answers with. The search is limited by
`allowed_domains` to the one store's own site, so it can't reach a dashboard, a repo, another seller or the
other team. The harness prices the searches ($10 per 1,000, per `usage.server_tool_use.web_search_requests`;
a search that errored isn't billed) with the tokens, so each lookup is a `model_calls` row (label
`price_search SKU STORE`) and, in a live run, a ledger `tokens` entry.

**Caps.** At most `HARD_SEARCHES_PER_RUN` (4) web searches in one agent run, counting every search the model
tried, billed or not, and at most `SEARCHES_PER_LOOKUP` (2) in one lookup; at most `HARD_DAILY_CAP_CENTS`
($1.00) of lookups in one Eastern day. The settings `price_search_searches_per_run` and
`price_search_daily_cap_cents` can lower them, never raise them (`config` refuses more, and a hand-edited value
is held to the limit here). The run and daily model caps apply on top.

**What the $1 a day means.** The harness reserves each lookup's worst case before it sends it, under the same
lock and in the same transaction that reads the day's lookup spend (`harness.Budget`), on the Eastern day of the
reservation. A lookup whose worst case doesn't fit what is left is refused before anything is sent. The worst
case is the fee for `max_uses` searches, `MAX_TOKENS` out, and the prompt read once more per search with
`harness.SEARCH_RESULT_TOKENS` (25,000) of results per search so far (`harness.tokens_in`): about $0.41 for a
two-search lookup on Opus. So the day's reservations never pass $1. What is billed stays within them as long as
each search's results stay within that allowance. The API doesn't bound how many tokens a result adds (they bill
as input tokens), so that part is our assumption (A27), not a guarantee: a lookup can be billed more than its
worst case. The harness records what was billed and marks the row ("billed more than its worst case"), and from
then on `Budget` leaves nothing for the rest of that Eastern day. **So a day's lookups bill at most $1 plus one
lookup's overshoot**, and the overshoot's only ceiling is the model's context window, read up to three times.

**The price memory** (Kevin, 23 Nov: weekly checks are essential spend, so make them cheap; `docs/sourcing.md`).
Every request that went out is a row in `price_lookups` (`remember`, migration 032), `--no-store` runs too, marked
as such; `--history` prints them. A store whose open search found no listing for the drink, in the packs asked for,
is skipped for `price_dead_end_days` (`dead_ends`, `not_carried`). A row with the store's link is re-checked on that
listing with one search (`_find_at`), then searched openly if that fails. With `--plan`, a drink a receipt priced
within the max age needs no lookup (`receipt_priced`). A find by a model cheaper than `price_check_model` that is the
first price for that drink, store and pack, or more than `price_check_percent` from the stored one, is re-read by
the check model on that listing (`_verified`) and stored at the check's price only if the two agree within
`CHECK_AGREE_PERCENT`; else `check_disagrees`. `--full` skips the memory (not the check). Each connection is tried
`CONNECT_TRIES` times (`retrying`); a drink that still can't reach the database is skipped (`db_error`) and the
`--json` file is saved however the run ends.

**Guardrails, checked in code on what the model reports and the search returned:**

- the store is one the purchase API accepts (`planner.STORES`), and the pack size is named; else refused before
  a search;
- the cited page is one of the pages the searches returned, exactly: only the case of the scheme and host and a
  trailing slash may differ. The stored link is the search result's, never the model's text, and an Amazon one is
  stored plain, `https://www.amazon.com/dp/<ASIN>` (`sourcing.plain_link`: no ref path, query or affiliate tag);
- that page is on the store's own domain or its `www.` host (for Amazon also `OTHER_HOSTS`, stored on `www.`),
  with no port or login part, and is the store's
  product page (`product_page`: Amazon `/dp/ASIN` or `/gp/product/ASIN`, Walmart `/ip/...`, Costco
  `....product.....html`, BJs `/product/...`). So a home page, a search page, a made-up link or a page about the
  Interloper (`docs/agent/interloper.md`), the other team or any other seller is refused;
- the reported pack is the pack asked for, and the price is in the purchase API's limits, at most
  `MOST_PER_UNIT_CENTS` a unit;
- the page or search result doesn't say the pack is out of stock (`out_of_stock`). In stock or unknown is
  stored with the row (`sourcing_options.stock`, migration 031);
- the price per unit is within half and twice (`PLAUSIBLE`) of the price we already have for the drink (the row
  it would refresh, else the primary, else the alternate, stale or not). With no price to compare, the checks
  above are the whole check: a drink's first price counts as checked too;
- a new row whose max would put the slot's floor above the price ceiling is not stored (`over_ceiling`): it would
  make the slot unpriceable;
- a row someone changed while the search ran is not overwritten (`changed_meanwhile`);
- the halt switch refuses a lookup before it spends, and again when it writes (`write_gate.run_write`).

A refusal gives the agent only a fixed reason (`REASONS`), never the model's words or the price it reported: the
search reads pages anyone can write. The whole report is kept in `action_log` for people.

**What is stored.** Through the sourcing table's own writers, so the change is logged with what it replaced, and
dated when it was written:

- a row for the same drink, store and pack (for Amazon, the same listing) gets the new price, `checked_by`
  `agent-search`, its note naming the page (`sourcing.set_price`). Its max stays: a price above it is not stored,
  and a person decides (`over_max`);
- otherwise a new row: the primary if the drink has none **or only a stale or unpriced one** (the fill refuses a
  stale primary however fresh the alternate is), else the alternate if it has none or only a stale or unpriced
  one. Its max is the price plus 25%, to the cent (`agent_max`). A fresh primary is never replaced. If the find
  is the alternate's own listing and the primary is stale, the find becomes the primary and the alternate copy
  is removed.

**Does it count as checked?** Yes, every price stored: it clears `no_checked_price` and `stale_price`, goes stale
after 7 days, and a receipt replaces it after the next fill. The max is the safety margin: the worker never pays
more than it. This is the default for the team to review (#202, #237).

A lookup for a drink whose price at that store and pack is already fresh is refused (`fresh_price`), so nothing
is spent.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import psycopg
from pydantic import BaseModel

from vending import config, key_dates, ledger, planner, planning, sourcing, start
from vending.harness import (SEARCH_TOOL_TYPE, Abandoned, Budget, CapReached, Harness, HarnessError, NotHeld,
                             PastDeadline)
from vending.pricing import Refusal, pack_floor_cents
from vending.receipt_prices import listing
from vending.write_gate import Refused, log, run_write

logger = logging.getLogger(__name__)

TOOL_NAME = "find_pack_price"
LABEL = "price_search"              # the model_calls label every lookup starts with (its daily budget reads it)
AGENT_BY = sourcing.AGENT_BY
HARD_SEARCHES_PER_RUN = 4           # the settings may lower these, never raise them
HARD_DAILY_CAP_CENTS = 100
DEFAULT_SEARCHES_PER_RUN = HARD_SEARCHES_PER_RUN
DEFAULT_DAILY_CAP_CENTS = HARD_DAILY_CAP_CENTS
SEARCHES_PER_LOOKUP = 2             # `max_uses` for one lookup, when the run has that many left
MAX_TOKENS = 2000
AGENT_MARKUP = Decimal("1.25")
MOST_PER_UNIT_CENTS = 1000          # $10 a can or bottle at a store is a misread, not a price
PLAUSIBLE = (Fraction(1, 2), Fraction(2))      # per unit, against the price we already have
# The stores the purchase API accepts, and the one site a search may read for each.
STORE_DOMAINS = {"Amazon": "amazon.com", "Walmart": "walmart.com", "Costco": "costco.com", "BJs": "bjs.com"}
assert set(STORE_DOMAINS) == planner.STORES
# Other hosts that are the store's own site. `allowed_domains` takes in subdomains, and the search returned Amazon
# product pages on us.amazon.com (23 Nov). Such a link is stored on the `www.` host, same path (same ASIN).
OTHER_HOSTS = {"Amazon": frozenset({"us.amazon.com", "smile.amazon.com"})}
# Each store's product page, as a whole path.
PRODUCT_PATHS = {
    "Amazon": re.compile(r"(?:/[^/]+)?/(?:dp|gp/product)/[A-Z0-9]{10}(?:/[^/]*)*", re.IGNORECASE),
    "Walmart": re.compile(r"/ip/[^/]+(?:/[^/]+)*/?"),
    "Costco": re.compile(r"/[^/]+\.product\.[^/]+\.html"),
    "BJs": re.compile(r"/product/[^/]+(?:/[^/]+)*/?"),
}
assert set(PRODUCT_PATHS) == planner.STORES
# The order a lookup tries the other stores in when the one asked for gives no usable price. Amazon is the
# worker's default channel (#234); the rest is a fixed order for the team to change.
STORE_ORDER = ("Amazon", "Walmart", "Costco", "BJs")
assert set(STORE_ORDER) == planner.STORES
# The refusals that say "this store didn't give a usable price", so the next store may: blocked or no answer,
# no listing, no product page, another pack, out of stock, a price we can't use there.
FALLBACK_ON = frozenset({"not_searched", "no_report", "not_found", "unsourced_url", "off_store_source",
                         "not_a_product_page", "bad_report", "pack_mismatch", "out_of_stock", "implausible_price",
                         "over_ceiling", "not_carried", "check_disagrees"})

# The price memory (Kevin, 23 Nov: weekly checks are essential spend, so make them cheap). Every lookup is a row in
# `price_lookups` (migration 032, `remember`).
DEFAULT_DEAD_END_DAYS = 21          # a store with no listing for a drink is skipped this long (`price_dead_end_days`)
LISTING_SEARCHES = 1                # `max_uses` to re-check a listing we already have a link for
DEFAULT_CHECK_MODEL = "claude-sonnet-5-5"       # checks a cheaper model's find (`price_check_model`)
DEFAULT_CHECK_PERCENT = 5           # a find within this of the stored price needs no check (`price_check_percent`)
CHECK_AGREE_PERCENT = 2             # the check stores its price only within this of the find
CHECK_SEARCHES = 1                  # `max_uses` for the check: it names the listing
CONFIRMED_FOR = timedelta(days=28)  # a price a check (or the check model itself) found counts as stored this long
CONNECT_TRIES = 3                   # a database connection that fails is tried this many times in all
CONNECT_BACKOFF = 1.0               # seconds before the second try; twice that before the third
_sleep = time.sleep

# `python -m vending.price_search`: a person runs the same lookup, before the start too (the day-one fill, #234).
CLI_LABEL = "price_search_cli"      # not "price_search ": the live agent's $1 a day doesn't count these
BENCH_LABEL = "price_search_bench"  # a --no-store run: test spend, nothing stored
CLI_SEARCHES_PER_RUN = 160          # 2 a lookup, 4 stores, 20 drinks; the dollar cap is the real limit
HARD_CLI_CAP_CENTS = 1000           # most one invocation may spend (`--max-usd`)
DEFAULT_CLI_CAP_CENTS = 500

# What the agent is told when a lookup stores nothing. Fixed text: nothing the search or the model wrote.
REASONS = {
    "disallowed_store": f"Not a store the worker buys from; use one of {', '.join(sorted(planner.STORES))}. "
                        "Nothing was searched.",
    "bad_request": f"Give the catalog SKU, the store and units_per_pack (1 to {planner.MAX_UNITS_PER_PACK}). "
                   "Nothing was searched.",
    "not_live": "The competition hasn't started, so people price drinks (python -m vending.sourcing set). "
                "Nothing was searched.",
    "halted": "The halt switch is on. Stop all writes and report it. Nothing was searched.",
    "not_ready": "A setting this lookup needs is unreadable; a person fixes it. Nothing was searched.",
    "not_in_catalog": "That SKU is not in our catalog copy. Nothing was searched.",
    "fresh_price": "That drink already has a fresh price at that store for that pack; use it. Nothing was searched.",
    "search_cap": "The price lookups' cap for this run or today is used up. Plan with the prices you have and name "
                  "the drink left unpriced in the update. Nothing was searched.",
    "not_searched": "The search didn't run or didn't answer. Nothing was stored.",
    "no_report": "The search ended without a report. Nothing was stored.",
    "not_found": "No matching listing at that store. Try another store, or plan without the drink. "
                 "Nothing was stored.",
    "unsourced_url": "The page cited is not exactly one the searches returned. Nothing was stored.",
    "off_store_source": "The page is not on the store's own site. Nothing was stored.",
    "not_a_product_page": "The page is not the store's product page for one listing. Nothing was stored.",
    "bad_report": "The search's report was not usable. Nothing was stored.",
    "pack_mismatch": "The listing found is for a pack size that wasn't asked for. Nothing was stored.",
    "out_of_stock": "The store's page says that pack is out of stock. Nothing was stored; try another store, or "
                    "plan without the drink.",
    "implausible_price": "The price found is too far from the price we already have for this drink to trust. "
                         "Nothing was stored; plan with the price you have, or a person checks it.",
    "over_max": "The price found is above the row's max. Nothing was stored; a person decides whether to raise "
                "the max. Plan with the stored price or another drink.",
    "over_ceiling": "At the max this price would get, the slot's floor would be above the price ceiling, so the "
                    "slot couldn't be priced. Nothing was stored.",
    "no_free_row": "The drink already has a primary and a fresh alternate at other stores or packs. Nothing was "
                   "stored; use those, or a person changes the table.",
    "changed_meanwhile": "Someone changed this drink's sourcing while the search ran. Nothing was stored; read the "
                         "table again.",
    # A comparison of every store (`PriceSearch.compare`, people's runs only).
    "past_cutoff": "That store's cutoff for the next visit has passed, so it can't supply it. Not searched.",
    "no_usable_price": "No store gave a usable price: in a pack the slots can use, not out of stock, before its "
                       "cutoff. Nothing was stored.",
    # The price memory.
    "not_carried": "Not carried: a search of that store found no listing for this drink and pack within the last few "
                   "weeks. Try another store. Nothing was searched.",
    "check_disagrees": "A second model's check of this listing didn't confirm the price. Nothing was stored from this "
                       "store.",
    "db_error": "The database couldn't be reached for this drink, after retries. Nothing more was done for it.",
}

TOOL = {
    "name": TOOL_NAME,
    "description": (
        "Look up today's price of one pack of a catalog drink at one store the worker buys from, with a capped "
        "web search on that store's own site, and record it in our sourcing table as found by the agent. Use it "
        "only before a restock, for a drink in your plan with no checked price or one older than 7 days. Costs "
        "money: a few searches a run. Never for any other purpose."),
    "input_schema": {
        "type": "object",
        "properties": {
            "sku": {"type": "string", "description": "The catalog SKU."},
            "store": {"type": "string", "enum": sorted(planner.STORES)},
            "units_per_pack": {"type": "integer", "minimum": 1, "maximum": planner.MAX_UNITS_PER_PACK,
                               "description": "The pack size to price: a listing for another size is refused."},
        },
        "required": ["sku", "store", "units_per_pack"],
        "additionalProperties": False,
    },
}

REPORT_TOOL = {
    "name": "report_price",
    "description": "Report what the searches found. Call it once, at the end.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "found": {"type": "boolean"},
            "price": {"type": "string", "description": "The pack's price in dollars, like 27.48. Empty if not found."},
            "units_per_pack": {"type": "integer", "description": "Cans or bottles in the pack. 0 if not found."},
            "pack_description": {"type": "string", "description": "Like '24 x 20 oz bottles'."},
            "url": {"type": "string", "description": "The product page, exactly as a search result gave it."},
            "stock": {"type": "string", "enum": list(sourcing.STOCK),
                      "description": "in_stock or out_of_stock only if the page or search result says so for "
                                     "this pack; else unknown."},
            "note": {"type": "string", "description": "One short sentence: where on the page the price was."},
        },
        "required": ["found", "price", "units_per_pack", "pack_description", "url", "stock", "note"],
        "additionalProperties": False,
    },
}

SYSTEM = """\
You look up one price for a small vending business. Search only the named store's own site. Report with \
report_price: the price the store's own product page shows for the pack, its units per pack, a short pack \
description, the product page's link exactly as a search result gave it, and whether the page or result says \
the pack is in stock or out of stock (unknown if it says neither). Use only what the search results \
show; never guess or work out a price. If no product page matches the product and the pack size asked for, report \
found false. Never use prices from vending machines, other sellers, marketplaces' third-party offers, home pages, \
search pages or anything that is not the store's own product page."""


class Recorded(BaseModel):
    """What `run_write` logs for the write step."""
    success: bool
    message: str


@dataclass
class _Stop(Exception):
    """A lookup that ends without a stored price. `detail` is for the log only; the agent gets `REASONS[code]`, and
    `reply`: our own facts only (the day a dead end was found), never the model's words. `logged`: `run_write` has
    already logged it."""
    code: str
    detail: str
    logged: bool = False
    reply: dict = field(default_factory=dict)


def offered(*, live: bool, now: datetime) -> bool:
    """Whether a run gets `find_pack_price`: live runs only (it writes and spends), from the start on."""
    return live and start.on_or_after_start(now)


def agent_max(expected_cents: int) -> int:
    """The price plus 25%, to the cent (half up), at most the purchase API's $200: 27.48 -> 34.35."""
    raised = (Decimal(expected_cents) * AGENT_MARKUP).to_integral_value(rounding=ROUND_HALF_UP)
    return min(int(raised), planner.MAX_PRICE_PER_PACK_CENTS)


def on_store(url: str, store: str) -> bool:
    """An http(s) link on the store's own domain, its `www.` host or one of its `OTHER_HOSTS`, with no port and no
    login part."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return False
    domain = STORE_DOMAINS[store]
    return (parts.scheme.lower() in ("http", "https") and port is None and parts.username is None
            and parts.password is None and "@" not in parts.netloc
            and (host in (domain, "www." + domain) or host in OTHER_HOSTS.get(store, ())))


def stored_link(url: str, store: str) -> str:
    """The link to store for a page `on_store`: on one of `OTHER_HOSTS`, the same link on the `www.` host (for
    Amazon the same ASIN); an Amazon product page as `https://www.amazon.com/dp/<ASIN>` (`sourcing.plain_link`);
    else the search result's link as it is."""
    parts = urlsplit(url)
    if (parts.hostname or "").lower() in OTHER_HOSTS.get(store, ()):
        url = urlunsplit((parts.scheme.lower(), "www." + STORE_DOMAINS[store], parts.path, parts.query,
                          parts.fragment))
    # An Amazon product page is stored plain, `https://www.amazon.com/dp/<ASIN>`: no query, ref path or affiliate tag.
    return sourcing.plain_link(store, url) or url


def product_page(url: str, store: str) -> bool:
    """`on_store`, and the path is that store's product page (`PRODUCT_PATHS`)."""
    return on_store(url, store) and PRODUCT_PATHS[store].fullmatch(urlsplit(url).path) is not None


def _normal(url: str) -> tuple[str, ...] | None:
    """A link with only the trivial differences taken out: the case of the scheme and host, a trailing slash."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    return (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), parts.query, parts.fragment)


def returned(cited: str, pages: list[str]) -> str | None:
    """The search result `cited` names, exactly as the search gave it, or None."""
    want = _normal(cited)
    return next((page for page in pages if want is not None and _normal(page) == want), None)


def _per_unit(o: sourcing.Option) -> Fraction:
    return Fraction(o.expected_cents, o.units_per_pack)


def configured_model(conn: psycopg.Connection, default: str) -> str:
    """The `price_search_model` setting, or `default` (the agent's model) when it is unset. `config set` takes only
    a priced model; a hand-edited unpriced one is refused by the harness before anything is sent."""
    value = config.get_value(conn, "price_search_model", None)
    return value.strip() if isinstance(value, str) and value.strip() else default


def next_visit(now: datetime) -> date:
    """The next stocking a purchase made now can reach: the day-one stocking (`key_dates.STOCKING`) until that
    day, then the first Friday after today (Eastern). On a Friday that day's visit is under way, so it is the
    next one."""
    today = now.astimezone(start.EASTERN).date()
    if today < key_dates.STOCKING.day:
        return key_dates.STOCKING.day
    return today + timedelta(days=(planner._FRIDAY - today.weekday()) % 7 or 7)


def past_cutoff(store: str, visit: date, now: datetime) -> bool:
    """Whether `store`'s cutoff for a stocking on `visit` has passed (`planner.cutoff_deadline`, the fill's rule)."""
    cutoff = planner.Config(slots=()).cutoffs.get(store)
    return cutoff is not None and now > planner.cutoff_deadline(visit, cutoff, start.EASTERN)


def _setting(conn: psycopg.Connection, key: str, default: int, most: int) -> int:
    """A setting held to its hard limit: a hand-edited value past it counts as the limit, a non-number as 0."""
    value = config.get_value(conn, key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, min(value, most))


# --- The price memory ----------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Memory:
    """The price memory's settings for one lookup."""
    dead_end_days: int
    check_model: str
    check_percent: int

    @classmethod
    def read(cls, conn: psycopg.Connection) -> Memory:
        model = config.get_value(conn, "price_check_model", None)
        return cls(_setting(conn, "price_dead_end_days", DEFAULT_DEAD_END_DAYS, 365),
                   model.strip() if isinstance(model, str) and model.strip() else DEFAULT_CHECK_MODEL,
                   _setting(conn, "price_check_percent", DEFAULT_CHECK_PERCENT, 100))


def remember(conn: psycopg.Connection, *, sku: str, store: str, kind: str, asked: tuple[int, int], outcome: str,
             model: str, cost: Decimal, by: str, no_store: bool, at: datetime, seen: dict | None = None,
             searches: Any = None, detail: str | None = None, model_call_id: int | None = None) -> None:
    """Log one lookup in `price_lookups`: what was asked, what the store's page showed (`seen`, as far as the report
    got), the outcome, the model and the cost."""
    seen = seen or {}
    conn.execute(
        "insert into price_lookups (at, sku, store, kind, asked_min, asked_max, units_per_pack, price_cents, url,"
        " stock, outcome, model, cost_usd, searches, no_store, by, detail, model_call_id)"
        " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (at, sku, store, kind, asked[0], asked[1], seen.get("units"), seen.get("price"), seen.get("url"),
         seen.get("stock"), outcome, model, cost, searches if isinstance(searches, int) else None, no_store, by,
         (detail or "")[:500] or None, model_call_id))


def confirmed_price(conn: psycopg.Connection, sku: str, store: str, units: int, url: str | None, now: datetime,
                    trusted: Callable[[str], bool]) -> int | None:
    """The newest price for this drink, store and pack (for Amazon, the same listing) that a lookup log row vouches
    for, from the last `CONFIRMED_FOR`: a check that agreed, or a find by a model `trusted` says needs no check. So
    what each check learns is kept, even for a store whose price lost the comparison and has no sourcing row. A
    cheaper model's unchecked find never counts."""
    rows = conn.execute(
        "select price_cents, url, model from price_lookups where sku = %s and store = %s and units_per_pack = %s"
        " and outcome = 'found' and price_cents is not null and at > %s and at <= %s order by at desc, id desc"
        " limit 50", (sku, store, units, now - CONFIRMED_FOR, now)).fetchall()
    for price, link, model in rows:
        if trusted(model) and (store != planner.AMAZON or same_listing(store, link, url)):
            return price
    return None


def needs_check(prices: Any, model: str, check: str) -> bool:
    """A find by a model cheaper than the check model gets a check (`PriceSearch._verified`). A dearer or the same
    model doesn't: a cheaper model never second-guesses a better one. `prices`: the harness's price table."""
    if model == check:
        return False
    try:
        return prices.rates(model, 0).input < prices.rates(check, 0).input
    except Exception:       # an unpriced model: check, and let the harness refuse what it can't price
        return True


def dead_ends(conn: psycopg.Connection, sku: str, asked: tuple[int, int], now: datetime,
              days: int) -> dict[str, datetime]:
    """The stores whose open search found no listing of `sku` (`not_found`) in the last `days`, for a range of packs
    that covers `asked`, with no find there since in a pack that range covers. Each with when it was searched."""
    if days <= 0:
        return {}
    rows = conn.execute(
        "select n.store, max(n.at) from price_lookups n"
        " where n.sku = %s and n.kind = 'search' and n.outcome = 'not_found'"
        " and n.asked_min <= %s and n.asked_max >= %s and n.at > %s"
        " and not exists (select 1 from price_lookups f where f.sku = n.sku and f.store = n.store"
        "                 and f.outcome = 'found' and f.id > n.id"
        "                 and f.units_per_pack between n.asked_min and n.asked_max)"
        " group by n.store", (sku, asked[0], asked[1], now - timedelta(days=days))).fetchall()
    return dict(rows)


def history(conn: psycopg.Connection, sku: str, limit: int = 30) -> list[str]:
    """A drink's latest lookups, newest first, one line each."""
    rows = conn.execute(
        "select at, store, kind, model, outcome, price_cents, units_per_pack, stock, url, cost_usd, no_store, by"
        " from price_lookups where sku = %s order by at desc, id desc limit %s", (sku, limit)).fetchall()
    lines = []
    for at, store, kind, model, outcome, price, units, stock, url, cost, no_store, by in rows:
        seen = (f"  ${price / 100:.2f} for {units}" if price is not None and units else "") \
            + (f", {stock.replace('_', ' ')}" if stock else "") + (f"  {url}" if url else "")
        lines.append(f"  {at.astimezone(start.EASTERN):%Y-%m-%d %H:%M}  {store:7}  {kind:7}  {outcome:15}  {model}"
                     f"{seen}  (${cost:.4f}, by {by}{', search only' if no_store else ''})")
    return lines


def retrying(connect: Callable[[], psycopg.Connection]) -> Callable[[], psycopg.Connection]:
    """`connect`, tried `CONNECT_TRIES` times in all with a short wait between, so one database timeout (an Azure
    Postgres connection timed out on 23 Nov and ended a run) doesn't end a run."""
    def opened() -> psycopg.Connection:
        for n in range(1, CONNECT_TRIES + 1):
            try:
                return connect()
            except psycopg.OperationalError as error:
                if n == CONNECT_TRIES:
                    raise
                logger.warning("database connection failed (%s); trying again (%d of %d)", error, n + 1,
                               CONNECT_TRIES)
                _sleep(CONNECT_BACKOFF * n)
        raise AssertionError("unreachable")
    return opened


def receipt_priced(rows: list[sourcing.Option], now: datetime, max_age: timedelta | None) -> sourcing.Option | None:
    """The newest of a drink's rows priced from a fulfilled receipt (`vending.receipt_prices`) within `max_age`
    (else the 7-day rule): that drink needs no web lookup. None if there is none."""
    within = max_age or sourcing.STALE_AFTER
    priced = [o for o in rows if o.from_receipt and o.expected_cents is not None and now - o.checked_at <= within]
    return max(priced, key=lambda o: o.checked_at, default=None)


def same_listing(store: str, a: str | None, b: str | None) -> bool:
    """Two links to one listing: for Amazon the same ASIN; else the same page (scheme, host and path; Walmart's item
    number, the path's last part)."""
    if not a or not b:
        return False
    if store == planner.AMAZON:
        return listing(a) == listing(b)
    x, y = _normal(a), _normal(b)
    if x is None or y is None:
        return False
    if store == "Walmart":
        return x[2].rsplit("/", 1)[-1] == y[2].rsplit("/", 1)[-1] and x[1].removeprefix("www.") == \
            y[1].removeprefix("www.")
    return x[:3] == y[:3]


def _match(rows: Any, store: str, found: dict) -> sourcing.Option | None:
    """The row a find would refresh: same store and pack, and for Amazon the same listing."""
    return next((o for o in rows if o is not None and o.store == store and o.units_per_pack == found["units"]
                 and (store != planner.AMAZON or listing(o.url) == listing(found["url"]))), None)


@dataclass(frozen=True)
class Outcome:
    """One lookup's end: the store tried, the refusal code (None when a price was stored), the reply, and what
    the lookup added to the run's spend (the harness's settled cost, or its worst case if it didn't settle)."""
    store: str
    code: str | None
    reply: dict
    cost_usd: Decimal = Decimal(0)

    @property
    def ok(self) -> bool:
        return self.code is None


STOP_RUN = frozenset({"halted", "search_cap"})     # a comparison that meets one of these stops the whole run


@dataclass(frozen=True)
class Drink:
    """A drink to compare: its SKU, the units one fill loads into its slots (all of them), and the plan's pack size
    if it names one (only a hint to the search: any pack the slots can use is compared)."""
    sku: str
    units: int
    pack: int | None = None

    @property
    def smallest_pack(self) -> int | None:
        """The smallest pack that can buy `units` on one purchase line (`planner.pack_usable`); None if none can."""
        return next((n for n in range(1, planner.MAX_UNITS_PER_PACK + 1) if planner.pack_usable(n, self.units)),
                    None)


@dataclass(frozen=True)
class Candidate:
    """One usable price in a comparison: a find (`match`: the row it would refresh) or a fresh table row (`row`)."""
    store: str
    units: int
    price: int                  # cents a pack
    max_cents: int
    url: str | None
    stock: str | None
    description: str | None
    row: sourcing.Option | None = None
    match: sourcing.Option | None = None
    cost: Decimal = Decimal(0)
    searches: Any = None
    first: bool = False          # no earlier price to compare against

    @classmethod
    def from_row(cls, o: sourcing.Option) -> Candidate:
        return cls(o.store, o.units_per_pack, o.expected_cents, o.max_cents, o.url, o.stock, o.pack_description, row=o)

    @property
    def per_unit(self) -> Fraction:
        return Fraction(self.price, self.units)

    def reply(self) -> dict:
        out = {"store": self.store, "price": f"{self.price / 100:.2f}", "units_per_pack": self.units,
               "per_unit": f"{float(self.per_unit) / 100:.4f}", "max": f"{self.max_cents / 100:.2f}",
               "stock": self.stock, "url": self.url, "pack_description": self.description,
               "source": "table" if self.row is not None else "search"}
        if self.row is not None:
            out["checked_at"] = self.row.checked_at.isoformat()
            out["checked_by"] = self.row.checked_by
        else:
            out["searches"] = self.searches
        return out


@dataclass
class Comparison:
    """One drink compared at every store: each store's outcome, in `STORE_ORDER`, and the two cheapest usable.
    `code` is None when a price was stored (or, with --no-store, when a winner was found)."""
    drink: Drink
    outcomes: list[Outcome] = field(default_factory=list)
    primary: Candidate | None = None
    alternate: Candidate | None = None
    code: str | None = None
    recorded: str | None = None
    stopped: bool = False       # the run stops here (`STOP_RUN`)
    model: str | None = None
    visit: date | None = None

    @property
    def cost_usd(self) -> Decimal:
        return sum((o.cost_usd for o in self.outcomes), Decimal(0))

    def to_json(self) -> dict:
        stores = []
        for o in self.outcomes:
            r = o.reply
            stores.append({"store": o.store, "code": o.code, "price": r.get("price"),
                           "units_per_pack": r.get("units_per_pack"), "per_unit": r.get("per_unit"),
                           "stock": r.get("stock"), "url": r.get("url"), "pack_description": r.get("pack_description"),
                           "source": r.get("source"), "checked": r.get("checked"),
                           "cost_usd": f"{o.cost_usd:.4f}"})
        return {"sku": self.drink.sku, "units": self.drink.units, "smallest_pack": self.drink.smallest_pack,
                "plan_pack": self.drink.pack, "code": self.code, "cost_usd": f"{self.cost_usd:.4f}", "stores": stores,
                "primary": self.primary and self.primary.reply(),
                "alternate": self.alternate and self.alternate.reply()}


class PriceSearch:
    """One run's price lookups: its harness, and the searches it has used so far.

    `person`: the name of whoever ran `python -m vending.price_search`. Their lookups may run before the start,
    are labelled `CLI_LABEL` (`BENCH_LABEL` with `bench`), have `CLI_SEARCHES_PER_RUN` searches and no lookup day
    budget of their own (the harness's run cap is the invocation's cap), and name them in the log and the note.
    None: the live agent.

    `model` is the fallback: each lookup runs on the `price_search_model` setting if it is set
    (`configured_model`), unless `model_fixed` (the command's `--model`). `max_age`: a price checked longer ago
    than this is looked up again even if it is still fresh by the 7-day rule (`sourcing.STALE_AFTER`).

    `full`: ignore the price memory: search a store that didn't carry the drink lately, and search openly instead of
    re-checking a listing we have a link for."""

    def __init__(self, harness: Harness, connect: Callable[[], psycopg.Connection], *, model: str,
                 now: Callable[[], datetime] | None = None, person: str | None = None, model_fixed: bool = False,
                 max_age: timedelta | None = None, bench: bool = False, full: bool = False,
                 label: str | None = None):
        self.harness, self.connect, self.model, self.person = harness, connect, model, person
        self.model_fixed, self.max_age, self.bench, self.full = model_fixed, max_age, bench, full
        # The weekly price check passes its own (`vending.price_schedule.LABEL`), so its weekly cap can read it.
        self.label = label or (LABEL if person is None else BENCH_LABEL if bench else CLI_LABEL)
        self.now = now          # None: the database clock
        self.searches = 0       # web searches this run has tried (or may have, when a call failed after sending)

    def _now(self, conn: psycopg.Connection) -> datetime:
        return self.now() if self.now else ledger.db_now(conn)

    def fresh(self, o: sourcing.Option, now: datetime) -> bool:
        """A checked price that needs no lookup: priced, not stale, and no older than `max_age` if one is set."""
        return (o.expected_cents is not None and not o.is_stale(now)
                and (self.max_age is None or now - o.checked_at <= self.max_age))

    def lookup(self, arguments: dict) -> tuple[str, bool]:
        """Run one lookup the model asked for: (JSON text for the model, is_error)."""
        out = self.find(arguments)
        return json.dumps(out.reply), not out.ok

    def find(self, arguments: dict) -> Outcome:
        """Run one lookup. A refusal's reply is only its code and fixed message (`REASONS`)."""
        before = self.harness.spent
        try:
            reply = self._lookup(arguments)
        except _Stop as stop:
            if not stop.logged:
                self._log(arguments, "refused", f"{stop.code}: {stop.detail}")
            return Outcome(str(arguments.get("store")), stop.code,
                           {"refused": stop.code, "message": REASONS[stop.code], **stop.reply},
                           self.harness.spent - before)
        return Outcome(reply["store"], None, reply, self.harness.spent - before)

    def with_fallback(self, sku: str, store: str, units_per_pack: int, *, fallback: bool = True) -> list[Outcome]:
        """Look up the pack at `store`; if that store can't give a usable price (`FALLBACK_ON`), try the other
        stores in `STORE_ORDER`, for the same pack, until one does. Returns every store's outcome, in order."""
        order = [store] + [s for s in STORE_ORDER if s != store] if fallback else [store]
        tried: list[Outcome] = []
        for n, at in enumerate(order):
            out = self.find({"sku": sku, "store": at, "units_per_pack": units_per_pack})
            tried.append(out)
            if out.ok:
                break
            # A fresh price at the store asked for: nothing to look up. At a later store: skip it.
            if out.code == "fresh_price" and n > 0:
                continue
            if out.code not in FALLBACK_ON:
                break
        return tried

    def _lookup(self, arguments: dict) -> dict:
        sku = str(arguments.get("sku") or "").strip()
        store = arguments.get("store")
        wanted = arguments.get("units_per_pack")
        if store not in planner.STORES:
            raise _Stop("disallowed_store", f"store {store!r}")
        if isinstance(wanted, bool) or not isinstance(wanted, int) or not 1 <= wanted <= planner.MAX_UNITS_PER_PACK:
            raise _Stop("bad_request", f"units_per_pack {wanted!r}")
        with self.connect() as conn:
            now, product, model = self._open(conn, sku)
            before = {o.rank: o for o in sourcing.options_for(conn, [sku])}
            head = before.get("primary")
            stale_head = head is not None and (head.expected_cents is None or head.is_stale(now))
            for o in before.values():
                # A fresh alternate behind a stale primary still leaves the fill refusing the drink, so search
                # anyway: the find becomes the primary (`_record`).
                if o.store == store and o.units_per_pack == wanted and self.fresh(o, now) \
                        and not (o.rank == "alternate" and stale_head):
                    raise _Stop("fresh_price", f"{o.rank} checked {sourcing.day(o.checked_at)}: {sourcing.describe(o)}")
            memory = Memory.read(conn)
            dead = None if self.full else dead_ends(conn, sku, (wanted, wanted), now, memory.dead_end_days).get(store)
            if dead is not None:
                raise _Stop("not_carried", f"no listing found {sourcing.day(dead)}",
                            reply={"checked": sourcing.day(dead)})
            limit, budget = self._limits(conn)
        known = None if self.full else next(
            (o for o in before.values() if o.store == store and o.units_per_pack == wanted and o.url), None)
        found = self._find_at(sku, store, product, model, {wanted}, f"a pack of {wanted}", limit, budget, known)
        found = self._verified(sku, store, product, model, found, before, memory, limit, budget)
        return self._record(arguments, sku, store, found, before, found["cost"], found["billed"])

    def _find_at(self, sku: str, store: str, product: tuple, model: str, packs: Any, asked: str, limit: int,
                 budget: Budget | None, known: sourcing.Option | None) -> dict:
        """With `known` (a row at this store with its product link), re-check that listing with one search first
        (Kevin, 23 Nov); if that gives no usable price, search the store openly."""
        if known is not None:
            try:
                return self._search(sku, store, product, model, {known.units_per_pack},
                                    f"the pack of {known.units_per_pack} on that listing", limit, budget,
                                    kind="listing", uses=LISTING_SEARCHES, link=known.url)
            except _Stop as stop:
                if stop.code not in FALLBACK_ON:
                    raise
                self._log({"sku": sku, "store": store, "listing": known.url}, "refused",
                          f"re-checking the listing: {stop.code}: {stop.detail}; searching the store instead")
        return self._search(sku, store, product, model, packs, asked, limit, budget)

    def _needs_check(self, model: str, check: str) -> bool:
        """`needs_check` on the harness's price table."""
        return needs_check(self.harness.prices, model, check)

    def _verified(self, sku: str, store: str, product: tuple, model: str, found: dict,
                  rows: dict[str, sourcing.Option], memory: Memory, limit: int, budget: Budget | None) -> dict:
        """A cheaper model's find, checked by `memory.check_model` before it is stored (Kevin, 23 Nov: Haiku
        misread a few prices in the benchmark). No check when the find is within `memory.check_percent` of the
        stored price for that drink, store and pack (`_match`; with no row, the newest `confirmed_price` in the
        lookup log), or when it is far enough off that storing will refuse it anyway (`PLAUSIBLE`). Otherwise one check lookup of that listing and pack: the find is stored, at the
        check's price, only if the two agree within `CHECK_AGREE_PERCENT`; else `check_disagrees`."""
        if self.bench or not self._needs_check(model, memory.check_model):
            return found
        match = _match(rows.values(), store, found)
        stored = match.expected_cents if match is not None else None
        if stored is None:
            try:
                with self.connect() as conn:
                    stored = confirmed_price(conn, sku, store, found["units"], found["url"], self._now(conn),
                                             lambda m: not self._needs_check(m, memory.check_model))
            except psycopg.Error as error:      # no memory to read: the find gets its check
                logger.error("couldn't read the confirmed prices of %s at %s: %s", sku, store, error)
        if stored is not None and abs(found["price"] - stored) * 100 <= memory.check_percent * stored:
            return found
        known = next((o for o in (match, rows.get("primary"), rows.get("alternate"))
                      if o is not None and o.expected_cents is not None), None)
        if known is not None and not PLAUSIBLE[0] <= Fraction(found["price"], found["units"]) / _per_unit(known) \
                <= PLAUSIBLE[1]:
            return found
        checked = self._search(sku, store, product, memory.check_model, {found["units"]},
                               f"the pack of {found['units']} on that listing", limit, budget, kind="check",
                               uses=CHECK_SEARCHES, link=found["url"], against=found)
        return {**found, "price": checked["price"], "stock": checked["stock"], "checked_with": memory.check_model,
                "cost": found["cost"] + checked["cost"]}

    def _open(self, conn: psycopg.Connection, sku: str) -> tuple[datetime, tuple, str]:
        """(now, the catalog's name and size, the model) for a lookup that may start; else `_Stop`."""
        now = self._now(conn)
        if self.person is None and not start.on_or_after_start(now):
            raise _Stop("not_live", f"{now.isoformat()} is before the start")
        if config.is_halted(conn):
            raise _Stop("halted", "halted before the search")
        product = conn.execute("select name, size from catalog_products where sku = %s", (sku,)).fetchone()
        if product is None or sku not in sourcing.in_catalog(conn, [sku]):
            raise _Stop("not_in_catalog", f"sku {sku!r}")
        return now, product, self.model if self.model_fixed else configured_model(conn, self.model)

    def _limits(self, conn: psycopg.Connection) -> tuple[int, Budget | None]:
        """The run's search limit and the lookup day budget: the agent's settings, held to their hard limits.
        A person's run has `CLI_SEARCHES_PER_RUN` and its own run cap only."""
        if self.person is not None:
            return CLI_SEARCHES_PER_RUN, None
        limit = _setting(conn, "price_search_searches_per_run", DEFAULT_SEARCHES_PER_RUN, HARD_SEARCHES_PER_RUN)
        budget = Budget(LABEL + " ", Decimal(_setting(conn, "price_search_daily_cap_cents",
                                                      DEFAULT_DAILY_CAP_CENTS, HARD_DAILY_CAP_CENTS)) / 100)
        return limit, budget

    def _search(self, sku: str, store: str, product: tuple, model: str, packs: Any, asked: str, limit: int,
                budget: Budget | None, *, kind: str = "search", uses: int = SEARCHES_PER_LOOKUP,
                link: str | None = None, against: dict | None = None) -> dict:
        """One search request at `store` for a pack in `packs`, checked (`_checked`): the fields to store, plus
        `cost` (the harness's settled cost) and `billed` (searches billed). Every request that went out is logged in
        `price_lookups` (`remember`), with its `kind`: `search`, `listing` (a re-check of `link`) or `check` (a
        check of `against`, a cheaper model's find: any answer that doesn't agree is `check_disagrees`)."""
        left = limit - self.searches
        if left <= 0:
            raise _Stop("search_cap", f"this run has tried {self.searches} of {limit} searches")
        uses = min(uses, left)
        before = self.harness.spent
        sent: dict = {}
        seen: dict = {}
        try:
            found = self._ask(sku, store, product, model, packs, asked, uses, budget, link, sent, seen)
            if against is not None:
                self._agrees(store, found, against)
        except _Stop as stop:
            if not sent:
                raise
            if kind == "check" and stop.code not in STOP_RUN:
                stop = _Stop("check_disagrees", f"the check found {stop.code}: {stop.detail}")
            self._remember(sku, store, kind, packs, stop.code, model, self.harness.spent - before, seen,
                           sent.get("billed"), stop.detail, sent.get("call_id"))
            raise stop from None
        self._remember(sku, store, kind, packs, "found", model, self.harness.spent - before, seen, found["billed"],
                       None, sent.get("call_id"))
        return found

    def _agrees(self, store: str, checked: dict, found: dict) -> None:
        """The check's answer is the same listing and pack, at a price within `CHECK_AGREE_PERCENT` of the find."""
        if not same_listing(store, checked["url"], found["url"]) or checked["units"] != found["units"] \
                or abs(checked["price"] - found["price"]) * 100 > CHECK_AGREE_PERCENT * found["price"]:
            raise _Stop("check_disagrees", f"the find said ${found['price'] / 100:.2f} for {found['units']} at "
                                           f"{found['url']}; the check said ${checked['price'] / 100:.2f} for "
                                           f"{checked['units']} at {checked['url']}")

    def _remember(self, sku: str, store: str, kind: str, packs: Any, outcome: str, model: str, cost: Decimal,
                  seen: dict, billed: Any, detail: str | None, model_call_id: int | None = None) -> None:
        """`remember`, on its own connection. A log that can't be written never stops the lookup: it is logged."""
        try:
            with self.connect() as conn:
                remember(conn, sku=sku, store=store, kind=kind, asked=(min(packs), max(packs)), outcome=outcome,
                         model=model, cost=cost, by=self.person or AGENT_BY, no_store=self.bench,
                         at=self._now(conn), seen=seen, searches=billed, detail=detail, model_call_id=model_call_id)
        except psycopg.Error as error:
            logger.error("couldn't log the %s lookup of %s at %s (%s): %s", kind, sku, store, outcome, error)

    def _ask(self, sku: str, store: str, product: tuple, model: str, packs: Any, asked: str, uses: int,
             budget: Budget | None, link: str | None, sent: dict, seen: dict) -> dict:
        """Send one search request and check its report. Sets `sent["at"]` once the request may have gone out, and
        fills `seen` with what the report showed as far as the checks got."""
        request = self._request(sku, store, product, uses, model, asked, link)
        try:
            call = self.harness.call_detailed(request, label=f"{self.label} {sku} {store}", daily_budget=budget)
        except CapReached as error:
            raise _Stop("search_cap", str(error)) from None
        except (NotHeld, PastDeadline) as error:
            raise _Stop("not_searched", str(error)) from None
        except (Abandoned, HarnessError) as error:      # it may have run: count its searches
            self.searches += uses
            sent["at"] = True
            raise _Stop("not_searched", str(error)) from None
        sent["at"] = True
        sent["call_id"] = call.call_id
        response = call.response
        content = list(getattr(response, "content", None) or [])
        billed = getattr(getattr(response.usage, "server_tool_use", None), "web_search_requests", None)
        sent["billed"] = billed
        tried = sum(1 for b in content if getattr(b, "type", None) == "server_tool_use"
                    and getattr(b, "name", None) == "web_search")
        self.searches += max(tried, int(billed or 0)) if (tried or billed is not None) else uses
        spent = f" The lookup cost ${call.cost_usd:.4f}."

        pages = [r.url for b in content if getattr(b, "type", None) == "web_search_tool_result"
                 and isinstance(b.content, list) for r in b.content
                 if getattr(r, "type", None) == "web_search_result" and isinstance(getattr(r, "url", None), str)]
        report = next((b.input for b in content if getattr(b, "type", None) == "tool_use"
                       and b.name == REPORT_TOOL["name"]), None)
        if not isinstance(report, dict):
            raise _Stop("no_report", "the search ended without a report." + spent)
        if not report.get("found"):
            raise _Stop("not_found", f"report: {json.dumps(report)[:400]}" + spent)
        found = self._checked(report, store, packs, pages, spent, seen)
        return {**found, "cost": call.cost_usd, "billed": billed}

    def _request(self, sku: str, store: str, product: tuple, uses: int, model: str, asked: str,
                 link: str | None = None) -> dict:
        name, size = product
        if link:
            named = listing(link)
            asin = f" (ASIN {named[1]})" if named and named[0] == "asin" else ""
            task = (f"Re-check this exact listing on the store's own site: {link}{asin}. Find today's price of "
                    f"{asked} on that product page, then call report_price. If the search doesn't return that page, "
                    "report found false.")
        else:
            task = f"Find today's price of {asked} on the store's own product page, then call report_price."
        return {
            "model": model, "max_tokens": MAX_TOKENS, "system": SYSTEM,
            "output_config": {"effort": "low"},
            "tools": [{"type": SEARCH_TOOL_TYPE, "name": "web_search", "max_uses": uses,
                       "allowed_domains": [STORE_DOMAINS[store]]},
                      REPORT_TOOL],
            "tool_choice": {"type": "auto"},
            "messages": [{"role": "user", "content": (
                f"Product: {name}{f' ({size})' if size else ''}, catalog SKU {sku}.\n"
                f"Store: {store} ({STORE_DOMAINS[store]}).\n" + task)}],
        }

    def _checked(self, report: dict, store: str, packs: Any, pages: list[str], spent: str,
                 seen: dict | None = None) -> dict:
        """The report, checked against the guardrails, as the fields to store. The link is the search result's,
        on the store's `www.` host when it came from one of its `OTHER_HOSTS`, and plain for Amazon
        (`stored_link`). `seen` gets each field once it passes its own check, for the lookup log."""
        seen = {} if seen is None else seen
        detail = f"report: {json.dumps(report)[:400]}." + spent
        url = returned(str(report.get("url") or ""), pages)
        if url is None:
            raise _Stop("unsourced_url", detail)
        if not on_store(url, store):
            raise _Stop("off_store_source", detail)
        if not product_page(url, store):
            raise _Stop("not_a_product_page", detail)
        seen["url"] = stored_link(url, store)
        try:
            price = sourcing.cents(str(report.get("price") or ""), "price")
        except sourcing.SourcingError:
            raise _Stop("bad_report", detail) from None
        units = report.get("units_per_pack")
        if isinstance(units, bool) or not isinstance(units, int) or not 1 <= units <= planner.MAX_UNITS_PER_PACK:
            raise _Stop("bad_report", detail)
        seen["units"] = units
        if price <= planner.MAX_PRICE_PER_PACK_CENTS:
            seen["price"] = price
        if report.get("stock") in sourcing.STOCK:
            seen["stock"] = report["stock"]
        if units not in packs:
            raise _Stop("pack_mismatch", detail)
        if price > planner.MAX_PRICE_PER_PACK_CENTS or price > units * MOST_PER_UNIT_CENTS:
            raise _Stop("implausible_price", detail)
        stock = report.get("stock", "unknown")
        if stock not in sourcing.STOCK:
            raise _Stop("bad_report", detail)
        if stock == "out_of_stock":
            raise _Stop("out_of_stock", detail)
        description = re.sub(r"\s+", " ", str(report.get("pack_description") or "")).strip()
        return {"url": stored_link(url, store), "price": price, "units": units, "stock": stock,
                "description": description[:planner.MAX_PACK_DESCRIPTION] or None}

    def _record(self, arguments: dict, sku: str, store: str, found: dict, before: dict[str, sourcing.Option],
                cost: Decimal, billed: Any) -> dict:
        outcome: dict = {}

        def refuse(code: str, detail: str) -> None:
            raise Refused([Refusal(code, detail)])

        def write(conn: psycopg.Connection) -> Recorded:
            sourcing.lock_skus(conn, {sku})
            rows = {rank: sourcing.locked_row(conn, sku, rank) for rank in sourcing.RANKS}
            if {r: o for r, o in rows.items() if o is not None} != before:
                refuse("changed_meanwhile", f"{sku}'s rows changed while the search ran")
            now = self._now(conn)
            outcome["at"] = now
            match = _match(rows.values(), store, found)
            # The price we already have for this drink: the row this refreshes, else the primary, else the alternate.
            known = next((o for o in (match, rows["primary"], rows["alternate"])
                          if o is not None and o.expected_cents is not None), None)
            price = Fraction(found["price"], found["units"])
            if known is not None and not PLAUSIBLE[0] <= price / _per_unit(known) <= PLAUSIBLE[1]:
                refuse("implausible_price", f"${found['price'] / 100:.2f} for {found['units']} against "
                                            f"{sourcing.describe(known)}")
            shown = f"${found['price'] / 100:.2f} for {found['units']}"
            who = f", run by {self.person}" if self.person else ""
            first = "" if known is not None else ", the drink's first price"
            check = f", confirmed by {found['checked_with']}" if found.get("checked_with") else ""
            note = f"found with a web search {sourcing.day(now)}{who}{first}{check}: {found['url']}"[:500]
            expected = found["price"]
            primary, alternate = rows["primary"], rows["alternate"]
            # A stale or unpriced primary blocks the fill however fresh the alternate is, so a fresh find goes to
            # the primary instead (Kevin, 23 Nov). Only a refresh of the primary's own listing keeps its max.
            stale_primary = primary is not None and (primary.expected_cents is None or primary.is_stale(now))
            refresh = match is not None and not (stale_primary and match.rank == "alternate")
            try:
                if refresh:
                    if found["price"] > match.max_cents:
                        refuse("over_max", f"{shown}, above the {match.rank}'s max ${match.max_cents / 100:.2f}")
                    sourcing.set_price(conn, match, expected, now, AGENT_BY, note, by=self.person or AGENT_BY,
                                       now=now, stock=found["stock"])
                    outcome["recorded"], outcome["rank"] = f"refreshed {match.rank}", match.rank
                else:
                    if primary is None or stale_primary:
                        rank = "primary"
                    elif alternate is None or alternate.expected_cents is None or alternate.is_stale(now):
                        rank = "alternate"
                    else:
                        refuse("no_free_row", f"{sku} has a primary and a fresh alternate")
                    most = agent_max(found["price"])
                    try:
                        ceiling = planning._limit(conn, "price_ceiling_cents", planner.AGREED_CEILING_CENTS)
                    except planning.NotReady as error:
                        refuse("not_ready", str(error))
                    tax = planning._int_setting(conn, "tax_percent", planner.DEFAULT_TAX_PERCENT)
                    if pack_floor_cents(most, found["units"], tax) > ceiling:
                        refuse("over_ceiling", f"max ${most / 100:.2f} for {found['units']} puts the floor above "
                                               f"${ceiling / 100:.2f}")
                    done = sourcing.set_option(conn, sourcing.Option(
                        sku=sku, rank=rank, store=store, units_per_pack=found["units"],
                        expected_cents=expected, max_cents=most, url=found["url"],
                        pack_description=found["description"], checked_at=now, checked_by=AGENT_BY, note=note,
                        stock=found["stock"]), now=now)
                    outcome["recorded"], outcome["rank"] = f"{done} {rank}", rank
                    if rank == "primary" and match is not None and match.rank == "alternate":
                        # The alternate was this same listing; now the primary holds it, so drop the copy.
                        sourcing.remove(conn, sku, "alternate", self.person or AGENT_BY)
                        outcome["recorded"] += " (its alternate was the same listing, so it was removed)"
            except sourcing.SourcingError as error:
                if "changed" in str(error):
                    refuse("changed_meanwhile", str(error))
                refuse("bad_report", str(error))
            stored = sourcing.locked_row(conn, sku, outcome["rank"])
            outcome["max"] = stored.max_cents
            return Recorded(success=True, message=f"{sku} {outcome['recorded']}: {sourcing.describe(stored)}, "
                                                  f"{found['stock'].replace('_', ' ')}, from {found['url']}")

        with self.connect() as conn:
            try:
                run_write(conn, TOOL_NAME, {**arguments, **self._who(), "url": found["url"],
                                            "price": found["price"], "units_per_pack": found["units"],
                                            "stock": found["stock"], "cost_usd": str(cost)}, write)
            except Refused as refused:
                first = refused.refusals[0]
                code = first.code if first.code in REASONS else "bad_report"
                raise _Stop(code, f"{first.message} The lookup cost ${cost:.4f}.", logged=True) from None
        return {"recorded": outcome["recorded"], "checked": True, "sku": sku, "store": store,
                "units_per_pack": found["units"], "price": f"{found['price'] / 100:.2f}",
                "max": f"{outcome['max'] / 100:.2f}", "stock": found["stock"], "url": found["url"],
                "seen_at": outcome["at"].isoformat(), "searches": billed, "cost_usd": f"{cost:.4f}",
                "message": "Stored as a checked price."}

    # --- Comparing every store (people's runs only) ---------------------------------------------------------

    def compare(self, drink: Drink, *, store: bool = True, visit: date | None = None) -> Comparison:
        """Price `drink` at every approved store and keep the cheapest usable pack per unit as the primary and the
        next best as the alternate (Kevin, 23 Nov).

        Usable: a pack the slots can use (`planner.pack_usable` for the units one fill loads), not out of stock,
        at a store whose cutoff for the next visit (`next_visit`, `past_cutoff`) hasn't passed, and passing every
        check a one-store lookup makes (the source, the price band, the row's max, the ceiling). A store with a
        `fresh` row is compared from the table, not searched again; a store past its cutoff is not searched.
        Ties go to in-stock, then `STORE_ORDER`.

        `store=False` (the benchmark, `--no-store`): every store is searched, whatever the table or the cutoffs
        hold, the winner is worked out from the searches alone, and nothing is written."""
        if self.person is None:
            raise ValueError("comparing every store is for people's runs: the agent's tool prices one store a call")
        arguments = {"sku": drink.sku, "compare": True, "units": drink.units, "store": store, **self._who()}
        lowest = drink.smallest_pack
        packs = range(lowest or 1, planner.MAX_UNITS_PER_PACK + 1)
        asked = (f"the pack with the lowest price per can or bottle among the store's own listings of this product "
                 f"in packs of {lowest} to {planner.MAX_UNITS_PER_PACK}"
                 + (f" (we usually buy packs of {drink.pack})" if drink.pack else "")
                 + ", and that pack's units and price")
        try:
            if lowest is None:
                raise _Stop("bad_request", f"{drink.units} units can't be bought on one purchase line")
            with self.connect() as conn:
                now, product, model = self._open(conn, drink.sku)
                before = {o.rank: o for o in sourcing.options_for(conn, [drink.sku])}
                limit, budget = self._limits(conn)
                memory = Memory.read(conn)
                dead = {} if self.full or not store else dead_ends(
                    conn, drink.sku, (lowest, planner.MAX_UNITS_PER_PACK), now, memory.dead_end_days)
                try:
                    ceiling = planning._limit(conn, "price_ceiling_cents", planner.AGREED_CEILING_CENTS)
                except planning.NotReady as error:
                    raise _Stop("not_ready", str(error)) from None
                tax = planning._int_setting(conn, "tax_percent", planner.DEFAULT_TAX_PERCENT)
        except _Stop as stop:
            self._log(arguments, "refused", f"{stop.code}: {stop.detail}")
            return Comparison(drink, [Outcome("-", stop.code, {"refused": stop.code,
                                                                  "message": REASONS[stop.code]})], code=stop.code)
        visit = visit or next_visit(now)
        late = {s for s in STORE_ORDER if past_cutoff(s, visit, now)}
        kept = [Candidate.from_row(o) for o in before.values()
                if store and self.fresh(o, now) and planner.pack_usable(o.units_per_pack, drink.units)]
        comparison = Comparison(drink, [], model=model, visit=visit)
        candidates = [c for c in kept if c.store not in late]
        for at in STORE_ORDER:
            if store and at in late:
                comparison.outcomes.append(Outcome(at, "past_cutoff", {"refused": "past_cutoff",
                                                                       "message": REASONS["past_cutoff"]}))
                continue
            table = [c for c in kept if c.store == at]
            if table:
                comparison.outcomes.append(Outcome(at, "fresh_price", {**table[0].reply(), "from_table": True}))
                continue
            if at in dead:
                checked = sourcing.day(dead[at])
                comparison.outcomes.append(Outcome(at, "not_carried", {"refused": "not_carried", "checked": checked,
                                                                       "message": REASONS["not_carried"]}))
                continue
            # A listing we have the link for (not fresh, or it would be in `kept`) is re-checked with one search.
            known = None if self.full or not store else next(
                (o for o in sorted(before.values(), key=lambda o: sourcing.RANKS.index(o.rank))
                 if o.store == at and o.url and planner.pack_usable(o.units_per_pack, drink.units)), None)
            before_cost = self.harness.spent
            try:
                found = self._find_at(drink.sku, at, product, model, packs, asked, limit, budget, known)
                candidate = self._candidate(drink.sku, at, found, before, tax, ceiling)
                if store:
                    checked_find = self._verified(drink.sku, at, product, model, found, before, memory, limit, budget)
                    if checked_find is not found:     # stored at the check's price, so its own checks again
                        candidate = self._candidate(drink.sku, at, checked_find, before, tax, ceiling)
            except _Stop as stop:
                self._log({**arguments, "store": at}, "refused", f"{stop.code}: {stop.detail}")
                comparison.outcomes.append(Outcome(at, stop.code, {"refused": stop.code,
                                                                   "message": REASONS[stop.code], **stop.reply},
                                                   self.harness.spent - before_cost))
                if stop.code in STOP_RUN:
                    comparison.code, comparison.stopped = stop.code, True
                    return comparison
                continue
            comparison.outcomes.append(Outcome(at, None, candidate.reply(), self.harness.spent - before_cost))
            if at not in late:
                candidates.append(candidate)
        ranked = sorted(candidates, key=lambda c: (c.per_unit, c.stock != "in_stock", STORE_ORDER.index(c.store)))
        comparison.primary, comparison.alternate = (ranked + [None, None])[:2]
        if comparison.primary is None:
            comparison.code = "no_usable_price"
            self._log(arguments, "refused", "no_usable_price: " + "; ".join(
                f"{o.store} {o.code}" for o in comparison.outcomes))
        elif store:
            comparison.code, comparison.recorded = self._keep(arguments, drink.sku, before, comparison)
        return comparison

    def _candidate(self, sku: str, store: str, found: dict, before: dict[str, sourcing.Option], tax: int,
                   ceiling: int) -> Candidate:
        """A find, checked against the table as `_record` checks one: the price band against the price we have, and
        the max it would get (the matching row's, else `agent_max`) against the row's max and the ceiling."""
        match = _match(before.values(), store, found)
        known = next((o for o in (match, before.get("primary"), before.get("alternate"))
                      if o is not None and o.expected_cents is not None), None)
        shown = f"${found['price'] / 100:.2f} for {found['units']}"
        price = Fraction(found["price"], found["units"])
        if known is not None and not PLAUSIBLE[0] <= price / _per_unit(known) <= PLAUSIBLE[1]:
            raise _Stop("implausible_price", f"{shown} against {sourcing.describe(known)}")
        if match is not None:
            most = match.max_cents
            if found["price"] > most:
                raise _Stop("over_max", f"{shown}, above the {match.rank}'s max ${most / 100:.2f}")
        else:
            most = agent_max(found["price"])
            if pack_floor_cents(most, found["units"], tax) > ceiling:
                raise _Stop("over_ceiling", f"max ${most / 100:.2f} for {found['units']} puts the floor above "
                                            f"${ceiling / 100:.2f}")
        return Candidate(store, found["units"], found["price"], most, found["url"], found["stock"],
                         found["description"], match=match, cost=found["cost"], searches=found["billed"],
                         first=known is None)

    def _keep(self, arguments: dict, sku: str, before: dict[str, sourcing.Option],
              comparison: Comparison) -> tuple[str | None, str | None]:
        """Write the winner as the primary and the runner-up as the alternate (none: the alternate is removed),
        through the table's own writers in one `run_write`. Returns (refusal code or None, what changed)."""
        done: dict = {}

        def refuse(code: str, detail: str) -> None:
            raise Refused([Refusal(code, detail)])

        def write(conn: psycopg.Connection) -> Recorded:
            sourcing.lock_skus(conn, {sku})
            rows = {rank: sourcing.locked_row(conn, sku, rank) for rank in sourcing.RANKS}
            if {r: o for r, o in rows.items() if o is not None} != before:
                refuse("changed_meanwhile", f"{sku}'s rows changed while the searches ran")
            now = self._now(conn)
            want = {"primary": self._as_row(sku, comparison.primary, "primary", now),
                    "alternate": comparison.alternate and self._as_row(sku, comparison.alternate, "alternate", now)}
            said = []
            try:
                for rank in sourcing.RANKS:            # the primary first: an alternate needs one
                    current, new = rows[rank], want[rank]
                    if new is None:
                        if current is not None:
                            sourcing.remove(conn, sku, rank, self.person or AGENT_BY)
                            said.append(f"{rank} removed (was {sourcing.describe(current)})")
                        continue
                    if new != current:
                        sourcing.set_option(conn, new, now=now)
                    said.append(f"{rank} {'kept' if new == current else 'set'}: {sourcing.describe(new)}")
            except sourcing.SourcingError as error:
                refuse("changed_meanwhile" if "changed" in str(error) else "bad_report", str(error))
            done["said"] = "; ".join(said)
            return Recorded(success=True, message=f"{sku} compared at every store: {done['said']}")

        with self.connect() as conn:
            try:
                run_write(conn, TOOL_NAME, {**arguments, "primary": comparison.primary.reply(),
                                            "alternate": comparison.alternate and comparison.alternate.reply(),
                                            "cost_usd": str(comparison.cost_usd)}, write)
            except Refused as refused:
                first = refused.refusals[0]
                return (first.code if first.code in REASONS else "bad_report"), None
        return None, done["said"]

    def _as_row(self, sku: str, c: Candidate, rank: str, now: datetime) -> sourcing.Option:
        """The row a candidate becomes at `rank`: a table row as it is; a find that refreshes a row keeps that row's
        store, pack, link, description and max; a new find gets `agent_max`."""
        if c.row is not None:
            return replace(c.row, rank=rank)
        who = f", run by {self.person}" if self.person else ""
        first = ", the drink's first price" if c.first else ""
        note = f"found comparing every store {sourcing.day(now)}{who}{first}: {c.url}"[:500]
        if c.match is not None:
            return replace(c.match, rank=rank, expected_cents=c.price, checked_at=now, checked_by=AGENT_BY,
                           note=note, stock=c.stock)
        return sourcing.Option(sku=sku, rank=rank, store=c.store, units_per_pack=c.units, expected_cents=c.price,
                               max_cents=c.max_cents, url=c.url, pack_description=c.description, checked_at=now,
                               checked_by=AGENT_BY, note=note, stock=c.stock)

    def _who(self) -> dict:
        return {"by": self.person} if self.person else {}

    def _log(self, arguments: dict, outcome: str, detail: str) -> None:
        try:
            with self.connect() as conn:
                log(conn, "write", TOOL_NAME, {**arguments, **self._who()}, outcome, detail)
        except psycopg.Error:
            pass        # the agent's own step record still has the reply


# --- python -m vending.price_search: the same lookup, run by a person ---------------------------------------

@dataclass(frozen=True)
class Need:
    """One lookup to run: a drink, the store to try first, the pack, and why."""
    sku: str
    store: str
    units_per_pack: int
    why: str


def row_needs(primary: sourcing.Option | None, alternate: sourcing.Option | None, now: datetime,
              max_age: timedelta | None = None) -> list[tuple[sourcing.Option | None, str]]:
    """Why a drink's rows need a lookup, as `vending.fill` refuses them (`no_checked_price`, `stale_price`): no
    primary, or a primary or alternate with no checked price or a stale one; with `max_age`, also one checked longer
    ago than that. Each with the row it is about (None: no primary)."""
    if primary is None:
        return [(None, "no sourcing row")]
    found: list[tuple[sourcing.Option | None, str]] = []
    for o in (primary, alternate):
        if o is None:
            continue
        if o.expected_cents is None:
            found.append((o, f"{o.rank} has no checked price"))
        elif o.is_stale(now):
            found.append((o, f"{o.rank} checked {sourcing.day(o.checked_at)}, stale"))
        elif max_age is not None and now - o.checked_at > max_age:
            found.append((o, f"{o.rank} checked {sourcing.day(o.checked_at)}, older than {_hours(max_age)} hours"))
    return found


def _hours(age: timedelta) -> str:
    return f"{age.total_seconds() / 3600:g}"


def plan_needs(conn: psycopg.Connection, checked: Any, now: datetime,
               max_age: timedelta | None = None) -> tuple[list[Need], list[str]]:
    """The lookups the day-one fill needs for a checked round's drinks (`plan_rounds.Round`), one store at a time
    (`row_needs`). A drink with no row is tried at `STORE_ORDER[0]` in the plan's pack size. Also returns the drinks
    that can't be looked up, in words."""
    packs = {p.sku: p.units_per_pack for p in checked.purchases}
    skus = sorted({s.sku for s in checked.slots.values() if s.sku})
    rows = {(o.sku, o.rank): o for o in sourcing.options_for(conn, skus)}
    needs: list[Need] = []
    skipped: list[str] = []
    for sku in skus:
        for o, why in row_needs(rows.get((sku, "primary")), rows.get((sku, "alternate")), now, max_age):
            if o is not None:
                needs.append(Need(sku, o.store, o.units_per_pack, why))
            elif sku in packs:
                needs.append(Need(sku, STORE_ORDER[0], packs[sku], why))
            else:
                skipped.append(f"{sku}: no sourcing row, and the plan names no pack size; run "
                               f"python -m vending.price_search {sku} --store STORE --pack N --by NAME")
    return needs, skipped


def plan_drinks(checked: Any) -> dict[str, Drink]:
    """Each drink in a checked round, with the units its slots load at the fill and the round's pack size."""
    units: dict[str, int] = {}
    for s in checked.slots.values():
        if s.sku:
            units[s.sku] = units.get(s.sku, 0) + s.units
    packs = {p.sku: p.units_per_pack for p in checked.purchases}
    return {sku: Drink(sku, n, packs.get(sku)) for sku, n in sorted(units.items())}


def current_drinks(conn: psycopg.Connection, ours: tuple[str, ...], now: datetime) -> dict[str, int]:
    """The drinks in our slots now, by our receipts (`vending.stock`), with the units a full column of each holds
    (`vending.capacity`; a person's count wins), summed over the slots that hold it. Empty before our first fill."""
    from vending import capacity, stock

    overrides = config.get_value(conn, "capacity_overrides", {})
    overrides = overrides if isinstance(overrides, dict) else {}
    held = [s for s in stock.stock(conn, until=now).slots if s.slot in ours and s.product]
    sizes = dict(conn.execute("select sku, size from catalog_products where sku = any(%s)",
                              ([s.product for s in held],)).fetchall()) if held else {}
    units: dict[str, int] = {}
    for s in held:
        column = capacity.for_product(s.product, sizes.get(s.product), overrides)
        # A package the chart doesn't cover and nobody counted: the chart's largest column, so the search still asks
        # for packs that can fill it.
        units[s.product] = units.get(s.product, 0) + (column or max(c[3] for c in capacity.CHART))
    return units


def _cap_cents(text: str) -> int:
    """`--max-usd` in cents: above $0 and at most `HARD_CLI_CAP_CENTS`."""
    try:
        value = int((Decimal(text.strip().lstrip("$")) * 100).to_integral_value())
    except (ArithmeticError, ValueError):
        raise ValueError(f"--max-usd must be a dollar amount like 5.00, not {text!r}") from None
    if not 0 < value <= HARD_CLI_CAP_CENTS:
        raise ValueError(f"--max-usd must be above $0.00 and at most ${HARD_CLI_CAP_CENTS / 100:.2f}, not {text!r}")
    return value


def _max_age(text: str | None) -> timedelta | None:
    """`--max-age` in hours: a number above 0, or None when it isn't given."""
    if text is None:
        return None
    try:
        hours = float(text)
    except ValueError:
        raise ValueError(f"--max-age must be a number of hours like 24, not {text!r}") from None
    if not 0 < hours <= 24 * 7:
        raise ValueError(f"--max-age must be above 0 and at most {24 * 7} hours (a week makes a price stale anyway), "
                         f"not {text!r}")
    return timedelta(hours=hours)


def _line(sku: str, units: int, out: Outcome) -> str:
    if out.code == "not_carried":
        return f"  {sku} {units} a pack at {out.store}: skipped: not carried, checked {out.reply['checked']}, no search"
    if out.ok:
        r = out.reply
        return (f"  {sku}: price from {out.store}: {r['recorded']}, ${r['price']} for {r['units_per_pack']} "
                f"(max ${r['max']}), {r['stock'].replace('_', ' ')}, {r['url']} (${out.cost_usd:.4f})")
    return f"  {sku} {units} a pack at {out.store}: {out.code}: {REASONS[out.code]} (${out.cost_usd:.4f})"


def _compared(c: Comparison, stored: bool) -> list[str]:
    """A comparison's lines: each store, then the winner and the runner-up."""
    lines = [f"  {c.drink.sku} ({c.drink.units} units a fill, packs of {c.drink.smallest_pack} to "
             f"{planner.MAX_UNITS_PER_PACK}):"]
    for o in c.outcomes:
        r = o.reply
        if o.ok or r.get("from_table"):
            stock = (r.get("stock") or "stock not recorded").replace("_", " ")
            where = (f"kept from the table, checked {r['checked_at'][:10]} by {r['checked_by']}, no search"
                     if r.get("from_table") else f"{r['url']} (${o.cost_usd:.4f})")
            lines.append(f"    {o.store}: ${r['price']} for {r['units_per_pack']} (${r['per_unit']} a unit), "
                         f"{stock}, {where}")
        elif o.code == "not_carried":
            lines.append(f"    {o.store}: skipped: not carried, checked {r['checked']}, no search")
        else:
            lines.append(f"    {o.store}: {o.code}: {REASONS[o.code]} (${o.cost_usd:.4f})")
    for rank, pick in (("primary", c.primary), ("alternate", c.alternate)):
        if pick is not None:
            lines.append(f"    {rank}: {pick.store}, ${pick.price / 100:.2f} for {pick.units} "
                         f"(${float(pick.per_unit) / 100:.4f} a unit)")
    if c.code is not None:
        lines.append(f"    Nothing stored: {c.code}: {REASONS[c.code]}")
    elif stored:
        lines.append(f"    Stored: {c.recorded}")
    lines.append(f"    Cost for this drink: ${c.cost_usd:.4f}")
    return lines


@dataclass
class CheckResult:
    """What one price check did: `main`'s exit code, the drinks it looked up, the drinks whose sourcing rows changed,
    the drinks that got no usable price, and its spend."""
    exit_code: int
    drinks: int = 0
    changed: int = 0
    failures: int = 0
    cost_usd: Decimal = Decimal(0)


def check_prices(*, by: str, max_age_hours: float, connect: Callable[[], psycopg.Connection] | None = None,
                 client: Any = None, now: Callable[[], datetime] | None = None, max_usd: str | None = None,
                 model: str | None = None, compare: bool = True, dry_run: bool = False,
                 label: str | None = None) -> CheckResult:
    """The weekly price check (`vending.price_schedule` runs it): our slots' current and planned drinks, re-pricing
    anything checked more than `max_age_hours` ago, with the price memory. `compare`: at every store, as
    `python -m vending.price_search --plan ours --max-age HOURS --by NAME`; else each drink's primary store only
    (`--recheck`). `dry_run` lists what it would look up and spends nothing. `label` names its `model_calls`."""
    from vending import db

    connect = retrying(connect or db.connect)
    argv = ["--plan", "ours", "--max-age", f"{max_age_hours:g}", "--by", by]
    argv += ["--max-usd", max_usd] if max_usd else []
    argv += ["--model", model] if model else []
    argv += [] if compare else ["--recheck"]
    argv += ["--dry-run"] if dry_run else []

    def prices() -> dict[tuple[str, str], tuple]:
        with connect() as conn:
            return {(o.sku, o.rank): (o.store, o.units_per_pack, o.expected_cents) for o in sourcing.options(conn)}

    before = prices()
    report: dict = {}
    code = main(argv, client=client, connect=connect, now=now, report=report, label=label)
    after = prices()
    changed = {key[0] for key in before.keys() | after.keys() if before.get(key) != after.get(key)}
    return CheckResult(code, report.get("drinks", 0), len(changed), report.get("failures", 0),
                       report.get("spent", Decimal(0)))


def main(argv: list[str] | None = None, *, client: Any = None,
         connect: Callable[[], psycopg.Connection] | None = None, now: Callable[[], datetime] | None = None,
         report: dict | None = None, label: str | None = None) -> int:
    """`python -m vending.price_search`. Exit 0 when every lookup stored a price (or none was needed), 1 when
    one didn't, 2 when the command was refused before any lookup. `report`, if given, gets `drinks` (looked up),
    `failures` (drinks with no usable price) and `spent`; `label` names the lookups' `model_calls`
    (`check_prices`)."""
    report = {} if report is None else report
    import argparse
    import sys

    from vending import db, fill, slots

    parser = argparse.ArgumentParser(
        prog="python -m vending.price_search",
        description="Look up pack prices with the capped web search on the stores' own sites, and store them in "
                    "the sourcing table, as the live agent's find_pack_price does.")
    parser.add_argument("sku", nargs="?", help="one catalog SKU (or --plan)")
    parser.add_argument("--plan", choices=["picked", "ours"],
                        help="picked: every drink in the picked plan the fill would refuse; ours: also the drinks in "
                             "our slots now (the weekly check)")
    parser.add_argument("--store", choices=STORE_ORDER, default=STORE_ORDER[0],
                        help=f"the store to try first (default {STORE_ORDER[0]})")
    parser.add_argument("--pack", type=int, help="units per pack (required with a SKU)")
    parser.add_argument("--by", help="your name, for the log (not needed with --history)")
    parser.add_argument("--compare", action="store_true",
                        help="price every store and keep the cheapest usable pack per unit (the default with --plan)")
    parser.add_argument("--first-usable", action="store_true",
                        help="with --plan: try one store at a time and keep the first usable price (the old way)")
    parser.add_argument("--max-age", metavar="HOURS",
                        help="also look up prices checked more than HOURS ago, though fresh by the 7-day rule")
    parser.add_argument("--no-store", action="store_true",
                        help="with --compare: search and report only, store nothing (a model benchmark; test spend)")
    parser.add_argument("--json", metavar="PATH", help="with --compare: save what each store gave to this file")
    parser.add_argument("--no-fallback", action="store_true", help="try only the first store")
    parser.add_argument("--max-usd", default=f"{DEFAULT_CLI_CAP_CENTS / 100:.2f}",
                        help=f"most this run may spend (default ${DEFAULT_CLI_CAP_CENTS / 100:.2f}, "
                             f"at most ${HARD_CLI_CAP_CENTS / 100:.2f})")
    parser.add_argument("--model", default=None,
                        help="the model (default: the price_search_model setting, else the agent's)")
    parser.add_argument("--dry-run", action="store_true", help="list the lookups; search and spend nothing")
    parser.add_argument("--full", action="store_true",
                        help="ignore the price memory: search stores that lately had no listing, search openly instead "
                             "of re-checking known listings, and look up drinks a recent receipt priced")
    parser.add_argument("--recheck", action="store_true",
                        help="with --plan: look up each drink at its primary store and pack only (usually one search on "
                             "the listing we have); a drink with no primary is compared")
    parser.add_argument("--history", action="store_true",
                        help="with a SKU: show its latest lookups, newest first; searches and spends nothing")
    args = parser.parse_args(argv)
    connect = retrying(connect or db.connect)

    if args.history:
        if args.sku is None:
            print("--history needs a SKU", file=sys.stderr)
            return 2
        with connect() as conn:
            lines = history(conn, args.sku.strip())
        print(f"Latest price lookups for {args.sku.strip()}, newest first (times Eastern):" if lines
              else f"No price lookups logged for {args.sku.strip()}.")
        for line in lines:
            print(line)
        return 0

    by = (args.by or "").strip()
    try:
        cap_cents = _cap_cents(args.max_usd)
        max_age = _max_age(args.max_age)
        if not by or by.lower() in (sourcing.RECEIPT_BY, sourcing.AGENT_BY):
            raise ValueError(f"--by needs your name, not {args.by!r}")
        if (args.sku is None) == (args.plan is None):
            raise ValueError("give one SKU (with --pack) or --plan picked")
        if args.sku is not None and (args.pack is None or not 1 <= args.pack <= planner.MAX_UNITS_PER_PACK):
            raise ValueError(f"--pack must be 1 to {planner.MAX_UNITS_PER_PACK} with a SKU")
        if args.compare and args.first_usable:
            raise ValueError("--compare and --first-usable are opposites; give one")
        if args.first_usable and args.plan != "picked":
            raise ValueError("--first-usable goes with --plan picked; a SKU alone already tries one store at a time")
        compare = args.compare or (args.plan is not None and not args.first_usable)
        if (args.no_store or args.json) and not compare:
            raise ValueError("--no-store and --json go with --compare")
        if args.recheck and (args.plan is None or not compare or args.no_store):
            raise ValueError("--recheck goes with --plan, and not with --first-usable or --no-store")
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2
    cap = Decimal(cap_cents) / 100

    with connect() as conn:
        behind = db.pending(conn)
        if behind:
            print(f"The database is missing migrations: {', '.join(behind)}. Check DATABASE_URL points where you "
                  "mean, then run: python -m vending.db migrate", file=sys.stderr)
            return 2
        at = now() if now else ledger.db_now(conn)
        skipped: list[str] = []
        receipts: dict[str, sourcing.Option] = {}
        pick = None
        drinks: dict[str, Drink] = {}
        if args.plan:
            try:
                ours = slots.our_slots(conn)
                pick = fill.picked(conn, ours)
            except fill.NoPick as error:
                if args.plan == "picked":
                    print(error, file=sys.stderr)
                    return 1
            except (slots.SlotsNotRecorded, slots.BadAssignment) as error:
                print(error, file=sys.stderr)
                return 1
            if pick is not None and pick.round is None:
                if args.plan == "picked":
                    print(f"Plan #{pick.id} was loaded for another slot assignment than ours now; load and pick a "
                          "plan for the current assignment first.", file=sys.stderr)
                    return 1
                pick = None
            if pick is not None:
                drinks = plan_drinks(pick.round)
            if args.plan == "ours":
                for sku, units in current_drinks(conn, ours, at).items():
                    held = drinks.get(sku)
                    drinks[sku] = Drink(sku, max(units, held.units if held else 0), held and held.pack)
        if compare:
            if args.sku is not None:
                drinks = {args.sku.strip(): Drink(args.sku.strip(), args.pack, args.pack)}
            rows = {(o.sku, o.rank): o for o in sourcing.options_for(conn, list(drinks))}
            why = {sku: [w for _, w in row_needs(rows.get((sku, "primary")), rows.get((sku, "alternate")), at,
                                                 max_age)] for sku in drinks}
            # Receipts first: a drink a fulfilled receipt priced within the max age needs no web lookup.
            if not (args.no_store or args.full or args.sku is not None):
                for sku in drinks:
                    held = [r for r in (rows.get((sku, "primary")), rows.get((sku, "alternate"))) if r is not None]
                    paid = receipt_priced(held, at, max_age) if why[sku] else None
                    if paid is not None:
                        receipts[sku] = paid
            # A benchmark searches every drink; otherwise a drink whose rows need nothing is left alone. A SKU named
            # on its own is always compared.
            todo = [d for sku, d in drinks.items()
                    if sku not in receipts and (args.no_store or args.sku is not None or why[sku])]
            needs = []
        elif args.plan:
            needs, skipped = plan_needs(conn, pick.round, at, max_age)
        else:
            needs = [Need(args.sku.strip(), args.store, args.pack, "asked for")]

    if compare:
        for sku, o in receipts.items():
            print(f"  {sku}: priced from a receipt on {sourcing.day(o.checked_at)}; no lookup")
        report.update(drinks=len(todo), failures=0, spent=Decimal(0))
        if not todo:
            print("Every drink has a fresh checked price; nothing to look up.")
        else:
            print(f"{len(todo)} drink{'s' if len(todo) != 1 else ''} to "
                  + ("re-check at its primary store" if args.recheck else "compare at every store")
                  + (" (search only: nothing will be stored)" if args.no_store else "") + ":")
            for d in todo:
                print(f"  {d.sku}: {d.units} units a fill, packs of {d.smallest_pack} to {planner.MAX_UNITS_PER_PACK}"
                      + (f" ({'; '.join(why[d.sku])})" if why.get(d.sku) else ""))
        if args.dry_run or not todo:
            if args.dry_run:
                print("Dry run: nothing was searched or spent.")
            return 0
    else:
        if not needs:
            print("Every drink in the picked plan has a fresh checked price; nothing to look up.")
        else:
            print(f"{len(needs)} lookup{'s' if len(needs) != 1 else ''}, each trying "
                  + ("only the store named:" if args.no_fallback else "the next store if one fails:"))
            for n in needs:
                print(f"  {n.sku}: {n.store}, {n.units_per_pack} a pack ({n.why})")
        for line in skipped:
            print(f"  Not looked up: {line}")
        if args.dry_run or not needs:
            if args.dry_run:
                print("Dry run: nothing was searched or spent.")
            return 1 if skipped else 0

    from vending.agent import DEFAULT_MODEL, NoModelKey, model_client

    try:
        client = client or model_client()
    except NoModelKey as error:
        print(error, file=sys.stderr)
        report["failures"] = len(todo) if compare else len(needs)
        return 2
    with connect() as conn:
        model = args.model or configured_model(conn, DEFAULT_MODEL)
        opened = ledger.real_opening(conn) if label else None
    scheduled_live = opened is not None and at >= opened.at
    live = (start.on_or_after_start(at) or scheduled_live) and not args.no_store
    try:
        harness = Harness(client, connect, purpose="live" if live else "test", run_cap_usd=cap, now=now, by=by)
    except HarnessError as error:
        print(error, file=sys.stderr)
        return 1
    searcher = PriceSearch(harness, connect, model=model, now=now, person=by, model_fixed=True, max_age=max_age,
                           bench=args.no_store, full=args.full, label=label)
    missed = bool(skipped)
    failures = 0
    if compare:
        results: list[Comparison] = []
        try:
            for d in todo:
                try:
                    primary = rows.get((d.sku, "primary"))
                    if args.recheck and primary is not None:
                        out = searcher.find({"sku": d.sku, "store": primary.store,
                                             "units_per_pack": primary.units_per_pack})
                        print(_line(d.sku, primary.units_per_pack, out))
                        if not out.ok and out.code != "fresh_price":
                            missed, failures = True, failures + 1
                        if out.code in STOP_RUN:
                            print("Stopped: no more lookups this run.")
                            break
                        continue
                    c = searcher.compare(d, store=not args.no_store)
                except psycopg.Error as error:
                    # One drink that can't reach the database, after `retrying`, doesn't end the run (23 Nov).
                    results.append(Comparison(d, [Outcome("-", "db_error", {"refused": "db_error",
                                                                            "message": REASONS["db_error"]})],
                                              code="db_error"))
                    print(f"  {d.sku}: the database failed ({type(error).__name__}: {error}); nothing more for "
                          "this drink, going on to the next.")
                    missed, failures = True, failures + 1
                    continue
                results.append(c)
                for line in _compared(c, stored=not args.no_store):
                    print(line)
                missed = missed or c.code is not None
                failures += c.code is not None
                if c.stopped:
                    print("Stopped: no more lookups this run.")
                    break
        finally:
            if args.json:       # saved whatever happened, so a run that fails late keeps what it found
                doc = {"model": model, "stored": not args.no_store, "at": at.isoformat(),
                       "visit": next((c.visit.isoformat() for c in results if c.visit), None),
                       "max_usd": f"{cap:.2f}", "spent_usd": f"{harness.spent:.4f}",
                       "drinks": [c.to_json() for c in results]}
                with open(args.json, "w", encoding="utf-8") as f:
                    json.dump(doc, f, indent=2)
                print(f"Saved what each store gave to {args.json}.")
        if args.no_store:
            print("Search only (--no-store): nothing was stored.")
    else:
        for n in needs:
            try:
                tried = searcher.with_fallback(n.sku, n.store, n.units_per_pack, fallback=not args.no_fallback)
            except psycopg.Error as error:
                print(f"  {n.sku}: the database failed ({type(error).__name__}: {error}); nothing more for this "
                      "drink, going on to the next.")
                missed, failures = True, failures + 1
                continue
            for out in tried:
                print(_line(n.sku, n.units_per_pack, out))
            last = tried[-1]
            if not last.ok and not (last.code == "fresh_price" and len(tried) == 1):
                missed, failures = True, failures + 1
            if last.code in ("halted", "search_cap"):
                print("Stopped: no more lookups this run.")
                break
    report.update(drinks=len(todo) if compare else len(needs), failures=failures, spent=harness.spent)
    if pick is not None and not args.no_store and args.plan == "picked":
        # A price can land in the alternate while a stale primary stays: say what the fill would still refuse.
        with connect() as conn:
            left, _ = plan_needs(conn, pick.round, now() if now else ledger.db_now(conn))
        if left:
            missed = True
            print("The fill would still refuse:")
            for n in left:
                print(f"  {n.sku}: {n.store}, {n.units_per_pack} a pack ({n.why})")
    print(f"Model spend this run: ${harness.spent:.4f} of the ${cap:.2f} cap ("
          + ("live spend, in the ledger as tokens" if live else
             "test spend: in model_calls, not in the ledger") + ").")
    return 1 if missed else 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
