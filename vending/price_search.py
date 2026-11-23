"""Look up one pack's price with a capped web search on the store's own site (#202 part 2), as the live agent
or as a person.

    searcher = PriceSearch(harness, connect, model="claude-opus-5-5")
    text, failed = searcher.lookup({"sku": "DRPEPPER-20BOT", "store": "Amazon", "units_per_pack": 24})

    python -m vending.price_search --plan picked --by NAME [--dry-run]     # the day-one fill's drinks (#234)
    python -m vending.price_search SKU --store Amazon --pack 24 --by NAME [--no-fallback] [--max-usd 5.00]

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

**Other stores.** When the store tried gives no usable price (`FALLBACK_ON`: blocked or no answer, no listing,
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

**Guardrails, checked in code on what the model reports and the search returned:**

- the store is one the purchase API accepts (`planner.STORES`), and the pack size is named; else refused before
  a search;
- the cited page is one of the pages the searches returned, exactly: only the case of the scheme and host and a
  trailing slash may differ. The stored link is the search result's, never the model's text;
- that page is on the store's own domain or its `www.` host, with no port or login part, and is the store's
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
- otherwise a new row: the primary if the drink has none, else the alternate if it has none or only a stale
  or unpriced one. Its max is the price plus 25%, to the cent (`agent_max`). The primary a person set is never
  replaced.

**Does it count as checked?** Yes, every price stored: it clears `no_checked_price` and `stale_price`, goes stale
after 7 days, and a receipt replaces it after the next fill. The max is the safety margin: the worker never pays
more than it. This is the default for the team to review (#202, #237).

A lookup for a drink whose price at that store and pack is already fresh is refused (`fresh_price`), so nothing
is spent.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from typing import Any
from urllib.parse import urlsplit

import psycopg
from pydantic import BaseModel

from vending import config, ledger, planner, planning, sourcing, start
from vending.harness import (SEARCH_TOOL_TYPE, Abandoned, Budget, CapReached, Harness, HarnessError, NotHeld,
                             PastDeadline)
from vending.pricing import Refusal, pack_floor_cents
from vending.receipt_prices import listing
from vending.write_gate import Refused, log, run_write

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
                         "over_ceiling"})

# `python -m vending.price_search`: a person runs the same lookup, before the start too (the day-one fill, #234).
CLI_LABEL = "price_search_cli"      # not "price_search ": the live agent's $1 a day doesn't count these
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
    "pack_mismatch": "The listing found is for another pack size than the one asked for. Nothing was stored.",
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
    """A lookup that ends without a stored price. `detail` is for the log only; the agent gets `REASONS[code]`.
    `logged`: `run_write` has already logged it."""
    code: str
    detail: str
    logged: bool = False


def offered(*, live: bool, now: datetime) -> bool:
    """Whether a run gets `find_pack_price`: live runs only (it writes and spends), from the start on."""
    return live and start.on_or_after_start(now)


def agent_max(expected_cents: int) -> int:
    """The price plus 25%, to the cent (half up), at most the purchase API's $200: 27.48 -> 34.35."""
    raised = (Decimal(expected_cents) * AGENT_MARKUP).to_integral_value(rounding=ROUND_HALF_UP)
    return min(int(raised), planner.MAX_PRICE_PER_PACK_CENTS)


def on_store(url: str, store: str) -> bool:
    """An http(s) link on the store's own domain or its `www.` host, with no port and no login part."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return False
    domain = STORE_DOMAINS[store]
    return (parts.scheme.lower() in ("http", "https") and port is None and parts.username is None
            and parts.password is None and host in (domain, "www." + domain) and "@" not in parts.netloc)


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


def _setting(conn: psycopg.Connection, key: str, default: int, most: int) -> int:
    """A setting held to its hard limit: a hand-edited value past it counts as the limit, a non-number as 0."""
    value = config.get_value(conn, key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, min(value, most))


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


class PriceSearch:
    """One run's price lookups: its harness, and the searches it has used so far.

    `person`: the name of whoever ran `python -m vending.price_search`. Their lookups may run before the start,
    are labelled `CLI_LABEL`, have `CLI_SEARCHES_PER_RUN` searches and no lookup day budget of their own (the
    harness's run cap is the invocation's cap), and name them in the log and the note. None: the live agent."""

    def __init__(self, harness: Harness, connect: Callable[[], psycopg.Connection], *, model: str,
                 now: Callable[[], datetime] | None = None, person: str | None = None):
        self.harness, self.connect, self.model, self.person = harness, connect, model, person
        self.now = now          # None: the database clock
        self.searches = 0       # web searches this run has tried (or may have, when a call failed after sending)

    def _now(self, conn: psycopg.Connection) -> datetime:
        return self.now() if self.now else ledger.db_now(conn)

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
                           {"refused": stop.code, "message": REASONS[stop.code]}, self.harness.spent - before)
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
            now = self._now(conn)
            if self.person is None and not start.on_or_after_start(now):
                raise _Stop("not_live", f"{now.isoformat()} is before the start")
            if config.is_halted(conn):
                raise _Stop("halted", "halted before the search")
            product = conn.execute("select name, size from catalog_products where sku = %s", (sku,)).fetchone()
            if product is None or sku not in sourcing.in_catalog(conn, [sku]):
                raise _Stop("not_in_catalog", f"sku {sku!r}")
            before = {o.rank: o for o in sourcing.options_for(conn, [sku])}
            for o in before.values():
                if o.store == store and o.units_per_pack == wanted and o.expected_cents is not None \
                        and not o.is_stale(now):
                    raise _Stop("fresh_price", f"{o.rank} checked {sourcing.day(o.checked_at)}: {sourcing.describe(o)}")
            if self.person is None:
                limit = _setting(conn, "price_search_searches_per_run", DEFAULT_SEARCHES_PER_RUN,
                                 HARD_SEARCHES_PER_RUN)
                budget = Budget(LABEL + " ", Decimal(_setting(conn, "price_search_daily_cap_cents",
                                                              DEFAULT_DAILY_CAP_CENTS, HARD_DAILY_CAP_CENTS)) / 100)
            else:
                limit, budget = CLI_SEARCHES_PER_RUN, None
            left = limit - self.searches
            if left <= 0:
                raise _Stop("search_cap", f"this run has tried {self.searches} of {limit} searches")

        uses = min(SEARCHES_PER_LOOKUP, left)
        request = self._request(sku, store, wanted, product, uses)
        label = LABEL if self.person is None else CLI_LABEL
        try:
            call = self.harness.call_detailed(request, label=f"{label} {sku} {store}", daily_budget=budget)
        except CapReached as error:
            raise _Stop("search_cap", str(error)) from None
        except (NotHeld, PastDeadline) as error:
            raise _Stop("not_searched", str(error)) from None
        except (Abandoned, HarnessError) as error:      # it may have run: count its searches
            self.searches += uses
            raise _Stop("not_searched", str(error)) from None
        response = call.response
        content = list(getattr(response, "content", None) or [])
        billed = getattr(getattr(response.usage, "server_tool_use", None), "web_search_requests", None)
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
        found = self._checked(report, store, wanted, pages, spent)
        return self._record(arguments, sku, store, found, before, call.cost_usd, billed)

    def _request(self, sku: str, store: str, wanted: int, product: tuple, uses: int) -> dict:
        name, size = product
        return {
            "model": self.model, "max_tokens": MAX_TOKENS, "system": SYSTEM,
            "output_config": {"effort": "low"},
            "tools": [{"type": SEARCH_TOOL_TYPE, "name": "web_search", "max_uses": uses,
                       "allowed_domains": [STORE_DOMAINS[store]]},
                      REPORT_TOOL],
            "tool_choice": {"type": "auto"},
            "messages": [{"role": "user", "content": (
                f"Product: {name}{f' ({size})' if size else ''}, catalog SKU {sku}.\n"
                f"Store: {store} ({STORE_DOMAINS[store]}).\n"
                f"Find today's price of a pack of {wanted} on the store's own product page, then call "
                "report_price.")}],
        }

    def _checked(self, report: dict, store: str, wanted: int, pages: list[str], spent: str) -> dict:
        """The report, checked against the guardrails, as the fields to store. The link is the search result's."""
        detail = f"report: {json.dumps(report)[:400]}." + spent
        url = returned(str(report.get("url") or ""), pages)
        if url is None:
            raise _Stop("unsourced_url", detail)
        if not on_store(url, store):
            raise _Stop("off_store_source", detail)
        if not product_page(url, store):
            raise _Stop("not_a_product_page", detail)
        try:
            price = sourcing.cents(str(report.get("price") or ""), "price")
        except sourcing.SourcingError:
            raise _Stop("bad_report", detail) from None
        units = report.get("units_per_pack")
        if isinstance(units, bool) or not isinstance(units, int) or not 1 <= units <= planner.MAX_UNITS_PER_PACK:
            raise _Stop("bad_report", detail)
        if units != wanted:
            raise _Stop("pack_mismatch", detail)
        if price > planner.MAX_PRICE_PER_PACK_CENTS or price > units * MOST_PER_UNIT_CENTS:
            raise _Stop("implausible_price", detail)
        stock = report.get("stock", "unknown")
        if stock not in sourcing.STOCK:
            raise _Stop("bad_report", detail)
        if stock == "out_of_stock":
            raise _Stop("out_of_stock", detail)
        description = re.sub(r"\s+", " ", str(report.get("pack_description") or "")).strip()
        return {"url": url, "price": price, "units": units, "stock": stock,
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
            match = next((o for o in rows.values() if o is not None and o.store == store
                          and o.units_per_pack == found["units"]
                          and (store != planner.AMAZON or listing(o.url) == listing(found["url"]))), None)
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
            note = f"found with a web search {sourcing.day(now)}{who}{first}: {found['url']}"[:500]
            expected = found["price"]
            try:
                if match is not None:
                    if found["price"] > match.max_cents:
                        refuse("over_max", f"{shown}, above the {match.rank}'s max ${match.max_cents / 100:.2f}")
                    sourcing.set_price(conn, match, expected, now, AGENT_BY, note, by=self.person or AGENT_BY,
                                       now=now, stock=found["stock"])
                    outcome["recorded"], outcome["rank"] = f"refreshed {match.rank}", match.rank
                else:
                    primary, alternate = rows["primary"], rows["alternate"]
                    if primary is None:
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


def plan_needs(conn: psycopg.Connection, checked: Any, now: datetime) -> tuple[list[Need], list[str]]:
    """The lookups the day-one fill needs for a checked round's drinks (`plan_rounds.Round`): a drink with no
    primary row, and every primary or alternate row with no checked price or a stale one, as `vending.fill`
    refuses them (`no_checked_price`, `stale_price`). A drink with no row is tried at `STORE_ORDER[0]` in the
    plan's pack size. Also returns the drinks that can't be looked up, in words."""
    packs = {p.sku: p.units_per_pack for p in checked.purchases}
    skus = sorted({s.sku for s in checked.slots.values() if s.sku})
    rows = {(o.sku, o.rank): o for o in sourcing.options_for(conn, skus)}
    needs: list[Need] = []
    skipped: list[str] = []
    for sku in skus:
        primary = rows.get((sku, "primary"))
        if primary is None:
            if sku in packs:
                needs.append(Need(sku, STORE_ORDER[0], packs[sku], "no sourcing row"))
            else:
                skipped.append(f"{sku}: no sourcing row, and the plan names no pack size; run "
                               f"python -m vending.price_search {sku} --store STORE --pack N --by NAME")
            continue
        for o in (primary, rows.get((sku, "alternate"))):
            if o is None:
                continue
            if o.expected_cents is None:
                needs.append(Need(sku, o.store, o.units_per_pack, f"{o.rank} has no checked price"))
            elif o.is_stale(now):
                needs.append(Need(sku, o.store, o.units_per_pack,
                                  f"{o.rank} checked {sourcing.day(o.checked_at)}, stale"))
    return needs, skipped


def _cap_cents(text: str) -> int:
    """`--max-usd` in cents: above $0 and at most `HARD_CLI_CAP_CENTS`."""
    try:
        value = int((Decimal(text.strip().lstrip("$")) * 100).to_integral_value())
    except (ArithmeticError, ValueError):
        raise ValueError(f"--max-usd must be a dollar amount like 5.00, not {text!r}") from None
    if not 0 < value <= HARD_CLI_CAP_CENTS:
        raise ValueError(f"--max-usd must be above $0.00 and at most ${HARD_CLI_CAP_CENTS / 100:.2f}, not {text!r}")
    return value


def _line(sku: str, units: int, out: Outcome) -> str:
    if out.ok:
        r = out.reply
        return (f"  {sku}: price from {out.store}: {r['recorded']}, ${r['price']} for {r['units_per_pack']} "
                f"(max ${r['max']}), {r['stock'].replace('_', ' ')}, {r['url']} (${out.cost_usd:.4f})")
    return f"  {sku} {units} a pack at {out.store}: {out.code}: {REASONS[out.code]} (${out.cost_usd:.4f})"


def main(argv: list[str] | None = None, *, client: Any = None,
         connect: Callable[[], psycopg.Connection] | None = None, now: Callable[[], datetime] | None = None) -> int:
    """`python -m vending.price_search`. Exit 0 when every lookup stored a price (or none was needed), 1 when
    one didn't, 2 when the command was refused before any lookup."""
    import argparse
    import sys

    from vending import db, fill, slots

    parser = argparse.ArgumentParser(
        prog="python -m vending.price_search",
        description="Look up pack prices with the capped web search on the stores' own sites, and store them in "
                    "the sourcing table, as the live agent's find_pack_price does.")
    parser.add_argument("sku", nargs="?", help="one catalog SKU (or --plan picked)")
    parser.add_argument("--plan", choices=["picked"], help="every drink in the picked plan the fill would refuse")
    parser.add_argument("--store", choices=STORE_ORDER, default=STORE_ORDER[0],
                        help=f"the store to try first (default {STORE_ORDER[0]})")
    parser.add_argument("--pack", type=int, help="units per pack (required with a SKU)")
    parser.add_argument("--by", required=True, help="your name, for the log")
    parser.add_argument("--no-fallback", action="store_true", help="try only the first store")
    parser.add_argument("--max-usd", default=f"{DEFAULT_CLI_CAP_CENTS / 100:.2f}",
                        help=f"most this run may spend (default ${DEFAULT_CLI_CAP_CENTS / 100:.2f}, "
                             f"at most ${HARD_CLI_CAP_CENTS / 100:.2f})")
    parser.add_argument("--model", default=None, help="the model (default: the agent's)")
    parser.add_argument("--dry-run", action="store_true", help="list the lookups; search and spend nothing")
    args = parser.parse_args(argv)

    by = args.by.strip()
    try:
        cap_cents = _cap_cents(args.max_usd)
        if not by or by.lower() in (sourcing.RECEIPT_BY, sourcing.AGENT_BY):
            raise ValueError(f"--by needs your name, not {args.by!r}")
        if (args.sku is None) == (args.plan is None):
            raise ValueError("give one SKU (with --pack) or --plan picked")
        if args.sku is not None and (args.pack is None or not 1 <= args.pack <= planner.MAX_UNITS_PER_PACK):
            raise ValueError(f"--pack must be 1 to {planner.MAX_UNITS_PER_PACK} with a SKU")
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2
    cap = Decimal(cap_cents) / 100
    connect = connect or db.connect

    with connect() as conn:
        behind = db.pending(conn)
        if behind:
            print(f"The database is missing migrations: {', '.join(behind)}. Check DATABASE_URL points where you "
                  "mean, then run: python -m vending.db migrate", file=sys.stderr)
            return 2
        at = now() if now else ledger.db_now(conn)
        skipped: list[str] = []
        if args.plan:
            try:
                pick = fill.picked(conn, slots.our_slots(conn))
            except (fill.NoPick, slots.SlotsNotRecorded, slots.BadAssignment) as error:
                print(error, file=sys.stderr)
                return 1
            if pick.round is None:
                print(f"Plan #{pick.id} was loaded for another slot assignment than ours now; load and pick a plan "
                      "for the current assignment first.", file=sys.stderr)
                return 1
            needs, skipped = plan_needs(conn, pick.round, at)
        else:
            needs = [Need(args.sku.strip(), args.store, args.pack, "asked for")]

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
        return 2
    live = start.on_or_after_start(at)
    harness = Harness(client, connect, purpose="live" if live else "test", run_cap_usd=cap, now=now, by=by)
    searcher = PriceSearch(harness, connect, model=args.model or DEFAULT_MODEL, now=now, person=by)
    missed = bool(skipped)
    for n in needs:
        tried = searcher.with_fallback(n.sku, n.store, n.units_per_pack, fallback=not args.no_fallback)
        for out in tried:
            print(_line(n.sku, n.units_per_pack, out))
        last = tried[-1]
        if not last.ok and not (last.code == "fresh_price" and len(tried) == 1):
            missed = True
        if last.code in ("halted", "search_cap"):
            print("Stopped: no more lookups this run.")
            break
    if args.plan:
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
             "dev spend before the start: in model_calls, not in the ledger") + ").")
    return 1 if missed else 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
