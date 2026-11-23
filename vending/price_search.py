"""The live agent looks up one pack's price itself, with a capped web search on the store's own site (#202 part 2).

    searcher = PriceSearch(harness, connect, model="claude-opus-5-5")
    text, failed = searcher.lookup({"sku": "DRPEPPER-20BOT", "store": "Amazon", "units_per_pack": 24})

Receipts keep the prices of drinks we already buy fresh (`vending.receipt_prices`). A drink we have never
bought, or one whose price is over a week old (`sourcing.STALE_AFTER`), has no checked price, and nobody is
watching the table once the competition runs. So from the start (3 Dec) the live agent has one more tool,
`find_pack_price`. Before the start, people price drinks (`python -m vending.sourcing set`), as with catalog
requests.

**How a lookup works.** One Messages API request through the run's own `Harness`, with Anthropic's basic web
search tool (`web_search_20250305`) and a `report_price` tool the model answers with. The search is limited by
`allowed_domains` to the one store's own site, so it can't reach a dashboard, a repo, another seller or the
other team. The harness prices the searches ($10 per 1,000) with the tokens, so each lookup is a `model_calls`
row (label `price_search ...`) and, in a live run, a ledger `tokens` entry.

**Caps.** `price_search_searches_per_run` (default 4) web searches in one agent run, at most
`SEARCHES_PER_LOOKUP` in one lookup; `price_search_daily_cap_cents` (default $1.00) a day for all lookups,
searches and tokens, passed to the harness as the call's own cap. The run and daily model caps apply on top.
A lookup the caps can't fit is refused before anything is sent.

**Guardrails, checked in code on what the model reports:**

- the store is one the purchase API accepts (`planner.STORES`); anything else is refused before a search;
- the product page is on that store's own domain (`STORE_DOMAINS`) and is one of the pages the searches
  actually returned, so a made-up link or a page about the Interloper (`docs/agent/interloper.md`), the other
  team or any other seller is refused;
- an Amazon page names its listing (`/dp/ASIN`), as the purchase API needs;
- the price and pack are in the purchase API's limits, and a unit costs at most `MOST_PER_UNIT_CENTS`;
- the halt switch refuses a lookup before it spends, and again when it writes (`write_gate.run_write`).

**What is stored.** Through the sourcing table's own writers, so the change is logged with what it replaced:

- a row for the same drink, store and pack (for Amazon, the same listing) gets the new price, dated now,
  `checked_by` `agent-search`, its note naming the page (`sourcing.set_price`). Its max stays: a price
  above it is not stored, and a person decides (`over_max`);
- otherwise a new row: the primary if the drink has none, else the alternate if it has none or only a stale
  or unpriced one. Its max is the price plus 25%, rounded up to 50 cents (`agent_max`), more room than a
  person's 15% because a search can be a little off. The primary a person set is never replaced.

**Does it count as checked?** Yes: an agent-found price is a checked price dated when it was found, so it
clears `no_checked_price` and `stale_price`, and a receipt replaces it after the next fill. The wider max is
the safety margin: the worker never pays more than it, so a wrong search costs at most one line. This is the
default for the team to review (#202).

A lookup for a drink whose price at that store is already fresh is refused (`fresh_price`), so nothing is spent.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, Decimal
from typing import Any
from urllib.parse import urlsplit

import psycopg
from pydantic import BaseModel

from vending import config, ledger, planner, sourcing, start
from vending.harness import (SEARCH_TOOL_TYPE, Abandoned, CapReached, Harness, HarnessError, NotHeld, PastDeadline,
                             day_start)
from vending.pricing import Refusal
from vending.receipt_prices import listing
from vending.write_gate import Refused, log, run_write

TOOL_NAME = "find_pack_price"
LABEL = "price_search"              # the model_calls label every lookup starts with (the daily cap reads it)
AGENT_BY = sourcing.AGENT_BY
DEFAULT_SEARCHES_PER_RUN = 4
DEFAULT_DAILY_CAP_CENTS = 100
SEARCHES_PER_LOOKUP = 2             # `max_uses` for one lookup, when the run has that many left
MAX_TOKENS = 2000
AGENT_MARKUP = Decimal("1.25")
MOST_PER_UNIT_CENTS = 1000          # $10 a can or bottle at a store is a misread, not a price
# The stores the purchase API accepts, and the only sites a search may read for each.
STORE_DOMAINS = {"Amazon": ("amazon.com",), "Walmart": ("walmart.com",), "Costco": ("costco.com",),
                 "BJs": ("bjs.com",)}
assert set(STORE_DOMAINS) == planner.STORES

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
                               "description": "The pack size you want, if you know it."},
        },
        "required": ["sku", "store"],
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
            "note": {"type": "string", "description": "One short sentence: where on the page the price was."},
        },
        "required": ["found", "price", "units_per_pack", "pack_description", "url", "note"],
        "additionalProperties": False,
    },
}

SYSTEM = """\
You look up one price for a small vending business. Search only the named store's own site. Report with \
report_price: the price the product page shows for the pack, its units per pack, a short pack description, and \
the product page's link exactly as a search result gave it. Use only what the search results show; never guess \
or work out a price. If no listing matches the product and size, report found false. Never use prices from \
vending machines, other sellers, marketplaces' third-party offers or anything that is not the store's own page."""


class Recorded(BaseModel):
    """What `run_write` logs for the write step."""
    success: bool
    message: str


@dataclass
class _Stop(Exception):
    """A lookup that ends without a stored price. `logged`: `run_write` has already logged it."""
    code: str
    message: str
    logged: bool = False


def offered(*, live: bool, now: datetime) -> bool:
    """Whether a run gets `find_pack_price`: live runs only (it writes and spends), from the start on."""
    return live and start.on_or_after_start(now)


def agent_max(expected_cents: int) -> int:
    """The price plus 25%, rounded up to the next 50 cents, at most the purchase API's $200: 27.48 -> 34.50."""
    raised = Decimal(expected_cents) * AGENT_MARKUP / sourcing.MAX_ROUNDING_CENTS
    rounded = int(raised.to_integral_value(rounding=ROUND_CEILING)) * sourcing.MAX_ROUNDING_CENTS
    return min(rounded, planner.MAX_PRICE_PER_PACK_CENTS)


def on_store(url: str, store: str) -> bool:
    """An http(s) link on one of the store's own domains or their subdomains."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    return parts.scheme.lower() in ("http", "https") and any(
        host == d or host.endswith("." + d) for d in STORE_DOMAINS[store])


def same_page(a: str, b: str) -> bool:
    """Two links to one product page: the same Amazon listing, or the same host and path."""
    first, second = listing(a), listing(b)
    if first and second and first[0] == second[0] == "asin":
        return first == second
    try:
        x, y = urlsplit(a.strip()), urlsplit(b.strip())
    except ValueError:
        return False
    return ((x.hostname or "").lower(), x.path.rstrip("/")) == ((y.hostname or "").lower(), y.path.rstrip("/"))


class PriceSearch:
    """One run's price lookups: its harness, and the searches it has used so far."""

    def __init__(self, harness: Harness, connect: Callable[[], psycopg.Connection], *, model: str,
                 now: Callable[[], datetime] | None = None):
        self.harness, self.connect, self.model = harness, connect, model
        self.now = now          # None: the database clock
        self.searches = 0       # web searches this run has used (or may have, when a call failed after sending)

    def lookup(self, arguments: dict) -> tuple[str, bool]:
        """Run one lookup the model asked for: (JSON text for the model, is_error)."""
        try:
            reply = self._lookup(arguments)
        except _Stop as stop:
            if not stop.logged:
                self._log(arguments, "refused", f"{stop.code}: {stop.message}")
            return json.dumps({"refused": stop.code, "message": stop.message}), True
        return json.dumps(reply), False

    def _lookup(self, arguments: dict) -> dict:
        sku = str(arguments.get("sku") or "").strip()
        store = arguments.get("store")
        wanted = arguments.get("units_per_pack")
        if store not in planner.STORES:
            raise _Stop("disallowed_store", f"{store!r} is not a store the worker buys from; use one of "
                                            f"{', '.join(sorted(planner.STORES))}. Nothing was searched.")
        if wanted is not None and (isinstance(wanted, bool) or not isinstance(wanted, int)
                                   or not 1 <= wanted <= planner.MAX_UNITS_PER_PACK):
            raise _Stop("bad_request", f"units_per_pack must be 1 to {planner.MAX_UNITS_PER_PACK}. "
                                       "Nothing was searched.")
        with self.connect() as conn:
            now = self.now() if self.now else ledger.db_now(conn)
            if not start.on_or_after_start(now):
                raise _Stop("not_live", "the competition hasn't started, so people price drinks "
                                        "(python -m vending.sourcing set). Nothing was searched.")
            if config.is_halted(conn):
                raise _Stop("halted", "the halt switch is on. Nothing was searched.")
            product = conn.execute("select name, size from catalog_products where sku = %s", (sku,)).fetchone()
            if product is None or sku not in sourcing.in_catalog(conn, [sku]):
                raise _Stop("not_in_catalog", f"{sku!r} is not in our catalog copy. Nothing was searched.")
            for o in sourcing.options_for(conn, [sku]):
                if o.store == store and wanted in (None, o.units_per_pack) and o.expected_cents is not None \
                        and not o.is_stale(now):
                    raise _Stop("fresh_price", f"{sku} already has a price at {store} checked "
                                               f"{sourcing.day(o.checked_at)}: {sourcing.describe(o)}. "
                                               "Nothing was searched.")
            limit = int(config.get_value(conn, "price_search_searches_per_run", DEFAULT_SEARCHES_PER_RUN))
            left = limit - self.searches
            if left <= 0:
                raise _Stop("search_cap", "this run has used all its price searches "
                                          "(price_search_searches_per_run). Nothing was searched.")
            cap = Decimal(config.get_value(conn, "price_search_daily_cap_cents", DEFAULT_DAILY_CAP_CENTS)) / 100
            spent = conn.execute("select coalesce(sum(cost_usd), 0) from model_calls where label like %s and at >= %s",
                                 (LABEL + "%", day_start(now))).fetchone()[0]
            left_usd = cap - Decimal(spent)
            if left_usd <= 0:
                raise _Stop("search_cap", f"price lookups have spent ${spent:.2f} today, the daily cap "
                                          f"(price_search_daily_cap_cents). Nothing was searched.")

        uses = min(SEARCHES_PER_LOOKUP, left)
        request = self._request(sku, store, wanted, product, uses)
        try:
            call = self.harness.call_detailed(request, label=f"{LABEL} {sku} {store}", cap_usd=left_usd)
        except CapReached as error:
            raise _Stop("search_cap", str(error)) from None
        except (NotHeld, PastDeadline) as error:
            raise _Stop("not_searched", str(error)) from None
        except (Abandoned, HarnessError) as error:      # it may have run: count its searches
            self.searches += uses
            raise _Stop("not_searched", str(error)) from None
        response = call.response
        used = getattr(getattr(response.usage, "server_tool_use", None), "web_search_requests", None)
        self.searches += uses if used is None else int(used)
        spent_note = f" The lookup cost ${call.cost_usd:.4f}."

        pages = [r.url for b in response.content if getattr(b, "type", None) == "web_search_tool_result"
                 and isinstance(b.content, list) for r in b.content if getattr(r, "url", None)]
        report = next((b.input for b in response.content if getattr(b, "type", None) == "tool_use"
                       and b.name == REPORT_TOOL["name"]), None)
        if not isinstance(report, dict):
            raise _Stop("no_report", "the search ended without a report, so nothing was stored." + spent_note)
        if not report.get("found"):
            raise _Stop("not_found", f"no matching listing at {store}: {str(report.get('note') or '')[:200]}"
                                     + spent_note)
        found = self._checked(report, store, pages, spent_note)
        return self._record(arguments, sku, store, found, now, call.cost_usd, used)

    def _request(self, sku: str, store: str, wanted: int | None, product: tuple, uses: int) -> dict:
        name, size = product
        pack = f"a pack of {wanted}" if wanted else "the multipack the store sells (the largest usual case)"
        return {
            "model": self.model, "max_tokens": MAX_TOKENS, "system": SYSTEM,
            "output_config": {"effort": "low"},
            "tools": [{"type": SEARCH_TOOL_TYPE, "name": "web_search", "max_uses": uses,
                       "allowed_domains": list(STORE_DOMAINS[store])},
                      REPORT_TOOL],
            "tool_choice": {"type": "auto"},
            "messages": [{"role": "user", "content": (
                f"Product: {name}{f' ({size})' if size else ''}, catalog SKU {sku}.\n"
                f"Store: {store} ({', '.join(STORE_DOMAINS[store])}).\n"
                f"Find today's price of {pack}, then call report_price.")}],
        }

    def _checked(self, report: dict, store: str, pages: list[str], spent_note: str) -> dict:
        """The report, checked against the guardrails, as the fields to store."""
        url = str(report.get("url") or "").strip()
        if not on_store(url, store):
            raise _Stop("off_store_source", f"the page found ({url[:200]!r}) is not on {store}'s own site, so its "
                                            "price can't be used. Nothing was stored." + spent_note)
        if not any(same_page(url, page) for page in pages):
            raise _Stop("unsourced_url", f"{url[:200]!r} is not a page the searches returned, so nothing was "
                                         "stored." + spent_note)
        if store == planner.AMAZON and (listing(url) or ("",))[0] != "asin":
            raise _Stop("not_a_product_page", "an Amazon link must be a product page (/dp/ASIN). Nothing was "
                                              "stored." + spent_note)
        try:
            price = sourcing.cents(str(report.get("price") or ""), "price")
        except sourcing.SourcingError as error:
            raise _Stop("bad_report", f"{error}. Nothing was stored." + spent_note) from None
        units = report.get("units_per_pack")
        if isinstance(units, bool) or not isinstance(units, int) or not 1 <= units <= planner.MAX_UNITS_PER_PACK:
            raise _Stop("bad_report", f"units per pack must be 1 to {planner.MAX_UNITS_PER_PACK}, not {units!r}. "
                                      "Nothing was stored." + spent_note)
        if price > planner.MAX_PRICE_PER_PACK_CENTS or price > units * MOST_PER_UNIT_CENTS:
            raise _Stop("implausible_price", f"${price / 100:.2f} for {units} is not a price we can use. "
                                             "Nothing was stored." + spent_note)
        description = re.sub(r"\s+", " ", str(report.get("pack_description") or "")).strip()
        return {"url": url, "price": price, "units": units,
                "description": description[:planner.MAX_PACK_DESCRIPTION] or None,
                "note": re.sub(r"\s+", " ", str(report.get("note") or "")).strip()[:150]}

    def _record(self, arguments: dict, sku: str, store: str, found: dict, now: datetime, cost: Decimal,
                used: Any) -> dict:
        note = f"found by the agent with a web search {sourcing.day(now)}: {found['url']}"[:500]
        outcome: dict = {}

        def write(conn: psycopg.Connection) -> Recorded:
            sourcing.lock_skus(conn, {sku})
            rows = {rank: sourcing.locked_row(conn, sku, rank) for rank in sourcing.RANKS}
            match = next((o for o in rows.values() if o is not None and o.store == store
                          and o.units_per_pack == found["units"]
                          and (store != planner.AMAZON or listing(o.url) == listing(found["url"]))), None)
            try:
                if match is not None:
                    if found["price"] > match.max_cents:
                        raise Refused([Refusal("over_max", (
                            f"{store} lists {sku} at ${found['price'] / 100:.2f} for {found['units']}, above the "
                            f"{match.rank}'s max ${match.max_cents / 100:.2f}, so it wasn't stored. A person decides "
                            f"whether to raise the max (python -m vending.sourcing set {sku} ...)."))])
                    sourcing.set_price(conn, match, found["price"], now, AGENT_BY, note, by=AGENT_BY, now=now)
                    outcome["recorded"], outcome["rank"] = f"refreshed {match.rank}", match.rank
                else:
                    primary, alternate = rows["primary"], rows["alternate"]
                    if primary is None:
                        rank = "primary"
                    elif alternate is None or alternate.expected_cents is None or alternate.is_stale(now):
                        rank = "alternate"
                    else:
                        raise Refused([Refusal("no_free_row", (
                            f"{sku} already has a primary and a fresh alternate at other stores or packs, so the "
                            f"{store} price wasn't stored. Use those, or a person changes the table."))])
                    done = sourcing.set_option(conn, sourcing.Option(
                        sku=sku, rank=rank, store=store, units_per_pack=found["units"],
                        expected_cents=found["price"], max_cents=agent_max(found["price"]), url=found["url"],
                        pack_description=found["description"], checked_at=now, checked_by=AGENT_BY, note=note),
                        now=now)
                    outcome["recorded"], outcome["rank"] = f"{done} {rank}", rank
            except sourcing.SourcingError as error:
                raise Refused([Refusal("bad_report", str(error))]) from None
            stored = sourcing.locked_row(conn, sku, outcome["rank"])
            outcome["max"] = stored.max_cents
            return Recorded(success=True, message=f"{sku} {outcome['recorded']}: {sourcing.describe(stored)}, "
                                                  f"from {found['url']}")

        with self.connect() as conn:
            try:
                result = run_write(conn, TOOL_NAME, {**arguments, "url": found["url"], "price": found["price"],
                                                     "units_per_pack": found["units"], "cost_usd": str(cost)}, write)
            except Refused as refused:
                first = refused.refusals[0]
                raise _Stop(first.code, f"{first.message} The lookup cost ${cost:.4f}.", logged=True) from None
        return {"recorded": outcome["recorded"], "sku": sku, "store": store, "units_per_pack": found["units"],
                "price": f"{found['price'] / 100:.2f}", "max": f"{outcome['max'] / 100:.2f}",
                "pack_description": found["description"], "url": found["url"], "seen_at": now.isoformat(),
                "searches": used, "cost_usd": f"{cost:.4f}", "message": result.message}

    def _log(self, arguments: dict, outcome: str, detail: str) -> None:
        try:
            with self.connect() as conn:
                log(conn, "write", TOOL_NAME, arguments, outcome, detail)
        except psycopg.Error:
            pass        # the agent's own step record still has the reply
