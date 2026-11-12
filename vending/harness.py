"""Every model call goes through here, so what it cost lands in the ledger.

    harness = Harness(client, db.connect, purpose="live")
    response = harness.call({"model": "claude-opus-5-5", "max_tokens": 16000, "messages": [...]})

Model spend comes out of the same $600 that buys drinks, so each call is
**recorded at its worst case before it is sent, and corrected after it answers**:

1. The request's tokens are counted (`messages.count_tokens`, free). The count
   is an estimate, so the worst case adds a margin to it, prices it at the
   dearest rate it could bill (a cache write if the request asks for caching;
   the higher tier if the prompt could cross one; US-only routing), and adds
   `max_tokens` of output.
2. Under a short database lock, `max_tokens` is lowered so the worst case fits
   under both caps (what's left of this run's, and of today's in Eastern time,
   where today's spend is every `tokens` entry since midnight), and the worst
   case is written to the ledger as an `estimate` entry. If the minimum output
   won't fit, nothing is sent and `CapReached` says why. If the entry can't be
   written, nothing is sent either: spend the books can't record isn't spent.
3. The request is sent once; the client's own retries are turned off, because
   each could bill again.
4. The answer's `usage` is priced from `model_prices.json` (at the model that
   actually answered) and the estimate entry is corrected to that cost. If the
   correction fails, the response is still returned and the estimate stays,
   so the books overstate the spend rather than lose it. A crash anywhere
   after step 2 leaves the estimate in place the same way.

A call that fails after it may have run (a timeout, a dropped connection, a
5xx) keeps its worst-case estimate. A 4xx is a request the API refused and
didn't bill, so its estimate is removed. A "run" is one `Harness`: its run cap
covers every call made through it.

Caps come from `config` (`python -m vending.config set model_daily_cap_cents
400 --by <name>`); the defaults are placeholders until the team sets the
weekly budget. `purpose` ("live" or "test") starts every entry's reference, so
test spend can be told apart from the agent's.

Not priced here, so refused: server tools (web search bills per use), fast
mode, priority capacity (requests are sent `standard_only`), streaming (use a
non-streaming call), `extra_body`, `betas` and `fallbacks`. Add their prices
before allowing them. (A fallback re-runs a declined request on another model in
the same call and bills both, more than the one worst case reserved here.)

"Now" is the database clock, read under the lock, the same clock the ledger's
cut-offs use (see `ledger.db_now`). The host clock can run behind it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

from vending import config, ledger

log = logging.getLogger(__name__)

EASTERN = ZoneInfo("America/New_York")
PRICES_FILE = Path(__file__).with_name("model_prices.json")
MILLION = Decimal(1_000_000)

# Placeholders for the team to set (in cents).
DEFAULT_RUN_CAP_CENTS = 100
DEFAULT_DAILY_CAP_CENTS = 400
DEFAULT_MIN_TOKENS = 1024
MAX_NON_STREAMING = 21_333      # above this the SDK insists on streaming (default 10-minute timeout)
LOCK_KEY = 0x76656E64           # "vend": one cap check and estimate at a time
LOCK_TIMEOUT = "30s"
COUNT_MARGIN = Decimal("1.10")  # count_tokens is an estimate: allow 10% more, plus a fixed pad
COUNT_PAD = 200
PURPOSES = ("live", "test")
# What messages.count_tokens accepts; anything else in the request is create-only.
COUNTED = ("messages", "model", "cache_control", "output_config", "output_format", "system", "thinking",
           "tool_choice", "tools")
# Anthropic-defined tools that run on our side bill as ordinary tokens; other typed tools are server tools.
CLIENT_TOOL_PREFIXES = ("custom", "bash_", "text_editor_", "memory_", "computer_")


class HarnessError(Exception):
    """The call wasn't made, or failed. The message says why and what was recorded."""


class CapReached(HarnessError):
    """Not sent: the worst case doesn't fit under what's left of a cap. Nothing was spent."""


@dataclass(frozen=True)
class Rates:
    """Dollars per million tokens, for prompts up to `prompt_up_to` tokens (None: no limit)."""
    prompt_up_to: int | None
    input: Decimal
    cache_write_5m: Decimal
    cache_write_1h: Decimal
    cache_read: Decimal
    output: Decimal


@dataclass(frozen=True)
class PriceTable:
    checked: str
    us_only_multiplier: Decimal
    models: dict[str, tuple[Rates, ...]]

    def rates(self, model: str, prompt_tokens: int) -> Rates:
        """The tier this prompt length bills at. Every input token counts, cached ones too."""
        if model not in self.models:
            raise HarnessError(f"no price for {model}; add it to {PRICES_FILE.name} (checked {self.checked}) first")
        for tier in self.models[model]:
            if tier.prompt_up_to is None or prompt_tokens <= tier.prompt_up_to:
                return tier
        raise HarnessError(f"{PRICES_FILE.name} has no tier for a {prompt_tokens}-token prompt on {model}")


def load_prices(path: Path = PRICES_FILE) -> PriceTable:
    raw = json.loads(path.read_text(encoding="utf-8"))
    models = {}
    for model, tiers in raw["models"].items():
        built = tuple(Rates(t["prompt_up_to"], *(Decimal(t[k]) for k in
                                                ("input", "cache_write_5m", "cache_write_1h", "cache_read", "output")))
                      for t in tiers)
        if not built or built[-1].prompt_up_to is not None:
            raise HarnessError(f"{path.name}: {model}'s last tier must have no prompt limit")
        models[model] = built
    return PriceTable(raw["checked"], Decimal(raw["us_only_multiplier"]), models)


def _n(value: Any) -> int:
    return int(value or 0)


def cost_usd(model: str, usage: Any, prices: PriceTable, *, us_only: bool = False) -> Decimal:
    """What one response's `usage` bills, in dollars.

    Cache writes bill at the 5-minute or 1-hour rate, split by
    `usage.cache_creation` when the API gives it (all 5-minute otherwise).
    Cache reads have their own rate. A prompt's length, for the tier, counts
    every input token, cached or not.
    """
    fresh, read, written = _n(usage.input_tokens), _n(usage.cache_read_input_tokens), _n(usage.cache_creation_input_tokens)
    split = getattr(usage, "cache_creation", None)
    one_hour = _n(getattr(split, "ephemeral_1h_input_tokens", 0)) if split is not None else 0
    five_min = written - one_hour
    r = prices.rates(model, fresh + read + written)
    dollars = (fresh * r.input + five_min * r.cache_write_5m + one_hour * r.cache_write_1h
               + read * r.cache_read + _n(usage.output_tokens) * r.output) / MILLION
    return dollars * prices.us_only_multiplier if us_only else dollars


def padded(counted: int) -> int:
    """The most input tokens a request counted at `counted` might bill."""
    return int((counted * COUNT_MARGIN).to_integral_value(ROUND_CEILING)) + COUNT_PAD


def worst_case_usd(model: str, counted: int, max_tokens: int, prices: PriceTable, *, caches: str | None) -> Decimal:
    """The most a request can bill: padded input at its dearest possible rate and tier,
    plus `max_tokens` out, with US-only routing assumed (the workspace may default to it).

    `caches` is None, "5m" or "1h": with caching asked for, any input token could be a write.
    """
    tokens_in = padded(counted)
    r = prices.rates(model, tokens_in)
    unit_in = {None: r.input, "5m": max(r.input, r.cache_write_5m), "1h": max(r.input, r.cache_write_1h)}[caches]
    return (tokens_in * unit_in + max_tokens * r.output) / MILLION * prices.us_only_multiplier


def _caching(request: dict) -> str | None:
    """'1h' if any cache_control in the request asks for an hour, '5m' for any other, else None."""
    text = json.dumps(request, default=str)
    if '"cache_control"' not in text:
        return None
    return "1h" if '"ttl": "1h"' in text else "5m"


def _refuse_unpriced(request: dict) -> None:
    for tool in request.get("tools") or []:
        kind = tool.get("type") if isinstance(tool, dict) else None
        if kind is not None and not str(kind).startswith(CLIENT_TOOL_PREFIXES):
            raise HarnessError(f"server tool {kind!r} bills per use and has no price here; not sent")
    if request.get("speed") not in (None, "standard"):
        raise HarnessError("fast mode bills at its own rates and has no price here; not sent")
    if request.get("service_tier") not in (None, "standard_only"):
        raise HarnessError("only standard capacity is priced here; send service_tier standard_only or leave it out")
    if request.get("stream"):
        raise HarnessError("streaming isn't supported here; send a non-streaming request")
    if "fallbacks" in request:
        raise HarnessError("a fallback bills a second model in the same call, more than the worst case "
                           "reserved here; not sent")
    if "betas" in request:
        raise HarnessError("beta features aren't priced here (and need the beta client); not sent")
    for key in ("extra_body", "extra_query", "extra_headers"):
        if key in request:
            raise HarnessError(f"{key} could change what's billed without the harness seeing it; not sent")


def day_start(now: datetime) -> datetime:
    """Midnight Eastern at the start of `now`'s day, in UTC."""
    local = now.astimezone(EASTERN)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def spent_since(conn: psycopg.Connection, since: datetime) -> Decimal:
    """Dollars of model spend in the ledger since `since`, estimates included."""
    row = conn.execute("select coalesce(sum(-amount_cents), 0) from ledger_entries"
                       " where kind = 'tokens' and at >= %s", (since,)).fetchone()
    return Decimal(row[0]) / 100


@dataclass
class Call:
    response: Any
    cost_usd: Decimal
    recorded: bool          # False when the estimate couldn't be corrected (it stays, overstating)
    max_tokens: int         # what was actually sent, after fitting the caps
    entry: int | None       # the ledger entry's id


class Harness:
    """Sends one request at a time under the run and daily caps, and records each one's cost."""

    def __init__(self, client: Any, connect: Callable[[], psycopg.Connection], *, purpose: str,
                 run_cap_usd: Decimal | None = None, daily_cap_usd: Decimal | None = None,
                 prices: PriceTable | None = None, min_tokens: int = DEFAULT_MIN_TOKENS,
                 now: Callable[[], datetime] | None = None, by: str = "harness"):
        if purpose not in PURPOSES:
            raise HarnessError(f"purpose must be one of {', '.join(PURPOSES)}, not {purpose!r}")
        self.client = client.with_options(max_retries=0)
        self.connect, self.purpose, self.by = connect, purpose, by
        self.prices = prices or load_prices()
        self.min_tokens = min_tokens
        self.now = now      # None: the database clock (a test may pass a fixed one)
        with connect() as conn:
            self.run_cap = run_cap_usd if run_cap_usd is not None else Decimal(
                config.get_value(conn, "model_run_cap_cents", DEFAULT_RUN_CAP_CENTS)) / 100
            self.daily_cap = daily_cap_usd if daily_cap_usd is not None else Decimal(
                config.get_value(conn, "model_daily_cap_cents", DEFAULT_DAILY_CAP_CENTS)) / 100
        self.spent = Decimal(0)     # this run, estimates included
        self.calls: list[Call] = []

    def call(self, request: dict, *, label: str = "") -> Any:
        """Send `request` (a `messages.create` body) under the caps and record its cost. Returns the response."""
        return self.call_detailed(request, label=label).response

    def call_detailed(self, request: dict, *, label: str = "") -> Call:
        import anthropic

        _refuse_unpriced(request)
        model = request.get("model")
        if not model:
            raise HarnessError("the request names no model")
        self.prices.rates(model, 0)     # an unpriced model fails before anything is sent
        wanted = int(request.get("max_tokens") or 0)
        if wanted < 1:
            raise HarnessError("the request needs a max_tokens")
        wanted = min(wanted, MAX_NON_STREAMING)     # the SDK refuses more without streaming, after we'd reserved
        floor = min(self.min_tokens, wanted)
        budget = (request.get("thinking") or {}).get("budget_tokens")
        if budget:
            floor = max(floor, int(budget) + 1)     # max_tokens must stay above a thinking budget
        caches = _caching(request)

        try:
            counted = self.client.messages.count_tokens(**{k: v for k, v in request.items() if k in COUNTED}).input_tokens
        except anthropic.APIError as error:
            raise HarnessError(f"counting the request failed ({_describe(error)}); nothing was sent or spent") from None

        reference = f"{self.purpose}{' ' + label if label else ''}"
        max_tokens, worst, entry = self._reserve(model, counted, wanted, floor, caches, reference)
        self.spent += worst

        body = {**request, "max_tokens": max_tokens, "service_tier": "standard_only"}
        try:
            response = self.client.messages.create(**body)
        except anthropic.APIStatusError as error:
            if error.status_code < 500:      # refused by the API, not billed
                self._settle(entry, Decimal(0), model, 0, 0, f"{reference} refused: {_describe(error)}")
                self.spent -= worst
                raise HarnessError(f"the model call was refused ({_describe(error)}); not billed") from None
            self._mark(entry, f"{reference} estimate: {_describe(error)}")
            raise HarnessError(f"the model call failed ({_describe(error)}); kept at its worst case, ${worst:.4f}") from None
        except anthropic.APIError as error:   # timeout or dropped connection: it may have run
            self._mark(entry, f"{reference} estimate: {_describe(error)}")
            raise HarnessError(f"the model call failed ({_describe(error)}); kept at its worst case, ${worst:.4f}") from None

        # From here on the response is paid for: nothing may stop it reaching the caller.
        paid, recorded = worst, False
        try:
            usage = response.usage
            answered_by = getattr(response, "model", None) or model
            us_only = request.get("inference_geo") == "us" or getattr(usage, "inference_geo", None) == "us"
            paid = cost_usd(answered_by if answered_by in self.prices.models else model, usage, self.prices,
                            us_only=us_only)
            note = ""
            if answered_by not in self.prices.models:
                note, paid = f" (answered by unpriced {answered_by}; kept at worst case)", worst
                log.error("model call answered by %s, which has no price; kept at its worst case", answered_by)
            if getattr(getattr(usage, "server_tool_use", None), "web_search_requests", 0):
                note += " (server tool use not priced)"
                log.error("model call used server tools, which aren't priced: %s", usage.server_tool_use)
            tier = getattr(usage, "service_tier", None)
            if tier not in (None, "standard"):
                note, paid = f" (served at {tier} capacity, not priced; kept at worst case)", worst
                log.error("model call was served at %s capacity, which has no price; kept at its worst case", tier)
            if paid <= 0:       # usage reported nothing (a proxy stripped it?): never erase a paid call
                note, paid = " (usage reported no tokens; kept at worst case)", worst
                log.error("model call %s reported no usage; kept at its worst case", reference)
            tokens_in = _n(usage.input_tokens) + _n(usage.cache_read_input_tokens) + _n(usage.cache_creation_input_tokens)
            recorded = self._settle(entry, paid, answered_by, tokens_in, _n(usage.output_tokens),
                                    f"{reference} {response.id}{note}")
        except Exception:
            log.exception("model call %s answered but its cost could not be worked out or recorded; "
                          "the worst-case estimate stays in the ledger", reference)
            paid, recorded = worst, False
        self.spent += paid - worst
        call = Call(response, paid, recorded, max_tokens, entry)
        self.calls.append(call)
        return call

    def _reserve(self, model: str, counted: int, wanted: int, floor: int, caches: str | None,
                 reference: str) -> tuple[int, Decimal, int]:
        """Fit max_tokens under the caps and write the worst case, under a short lock."""
        try:
            with self.connect() as conn, conn.transaction():
                conn.execute(f"set local lock_timeout = '{LOCK_TIMEOUT}'")
                conn.execute("select pg_advisory_xact_lock(%s)", (LOCK_KEY,))
                now = self.now() if self.now else ledger.db_now(conn)
                left_today = self.daily_cap - spent_since(conn, day_start(now))
                allowed = min(self.run_cap - self.spent, left_today)
                room = allowed - worst_case_usd(model, counted, 0, self.prices, caches=caches)
                r = self.prices.rates(model, padded(counted))
                per_token = r.output * self.prices.us_only_multiplier / MILLION
                max_tokens = min(wanted, int(room / per_token)) if room > 0 else 0
                if max_tokens < floor:
                    raise CapReached(
                        f"not sent: ${max(allowed, Decimal(0)):.4f} is left (run cap ${self.run_cap:.2f}; "
                        f"${max(left_today, Decimal(0)):.4f} of today's ${self.daily_cap:.2f}), and this "
                        f"{counted}-token request needs room for at least {floor} output tokens. Nothing was spent.")
                worst = worst_case_usd(model, counted, max_tokens, self.prices, caches=caches)
                entry = ledger.record_tokens(conn, worst, model, padded(counted), max_tokens,
                                             f"{reference} pending", by=self.by, at=now)
        except CapReached:
            raise
        except Exception as error:
            raise HarnessError(f"couldn't write the worst-case estimate to the ledger ({type(error).__name__}: "
                               f"{error}); nothing was sent") from None
        return max_tokens, worst, entry.id

    def _settle(self, entry: int, dollars: Decimal, model: str, tokens_in: int, tokens_out: int,
                reference: str) -> bool:
        """Correct the estimate to what the call billed (or remove it if nothing was). False if that failed."""
        try:
            with self.connect() as conn, conn.transaction():
                if dollars > 0:
                    conn.execute("update ledger_entries set amount_cents = %s, detail = %s, reference = %s"
                                 " where id = %s and kind = 'tokens'",
                                 (ledger._numeric(-ledger.dollars_to_cents(dollars)), f"{model}: {tokens_in} tokens in, {tokens_out} out",
                                  reference, entry))
                else:
                    conn.execute("delete from ledger_entries where id = %s and kind = 'tokens'", (entry,))
                conn.execute("insert into action_log (kind, tool, arguments, outcome, detail)"
                             " values ('write', 'ledger.tokens', %s, 'done', %s)",
                             (Jsonb({"entry": entry, "cost": str(dollars), "reference": reference}),
                              f"model call estimate {entry} settled at ${dollars:.6f}"))
            return True
        except Exception:
            log.exception("couldn't settle ledger entry %s at $%s; its worst-case estimate stays", entry, dollars)
            return False

    def _mark(self, entry: int, reference: str) -> None:
        """Keep the worst case, and say why in the reference."""
        try:
            with self.connect() as conn:
                conn.execute("update ledger_entries set reference = %s where id = %s", (reference, entry))
        except Exception:
            log.exception("couldn't mark ledger entry %s as an estimate; it stays at its worst case", entry)


def _describe(error: Any) -> str:
    status = getattr(error, "status_code", None)
    return f"{type(error).__name__}{f' {status}' if status else ''}"
