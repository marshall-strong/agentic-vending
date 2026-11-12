"""Every request sent with our Anthropic keys goes through here, so all of it is recorded.

    harness = Harness(client, db.connect, purpose="live")
    response = harness.call({"model": "claude-opus-5-5", "max_tokens": 16000, "messages": [...]})

**Every call is a row in `model_calls`**, whatever it was for: a live agent run,
a replayed week, a model sweep. That table is where the key's whole usage is
read from (`python -m vending.usage`, the dashboard). **Only `live` calls also
go in the ledger** as `tokens` entries, because only their cost comes out of the
$600; test spend is outside it (Kevin, 4 Nov) but never invisible.

Each call is **recorded at its worst case before it is sent, and corrected after
it answers**:

1. The request's tokens are counted (`messages.count_tokens`, free). The count
   is an estimate, so the worst case adds a margin to it, prices it at the
   dearest rate it could bill (a cache write if the request asks for caching;
   the higher tier if the prompt could cross one; US-only routing), and adds
   `max_tokens` of output.
2. Under a short database lock, `max_tokens` is lowered so the worst case fits
   under both caps (what's left of this run's, and of today's for this purpose
   in Eastern time), and the worst case is written as a `pending` row (and, for
   a live call, a ledger estimate). If the minimum output won't fit, nothing is
   sent and `CapReached` says why. If the row can't be written, nothing is sent
   either: spend we can't record isn't spent.
3. The request is sent once; the client's own retries are turned off, because
   each could bill again.
4. The answer's `usage` is priced from `model_prices.json` (at the model that
   actually answered) and the row is corrected to that cost, with its tokens,
   latency, stop reason and Anthropic's request id. If the correction fails, the
   response is still returned and the estimate stays, so the records overstate
   the spend rather than lose it. A crash anywhere after step 2 leaves the
   estimate in place the same way.

A call that fails after it may have run (a timeout, a dropped connection, a
5xx) keeps its worst case, as `error`. A 4xx is a request the API refused and
didn't bill: its row stays, at $0, as `refused`, and a live call's ledger
estimate is removed. A "run" is one `Harness`: its run cap covers every call
made through it, and its `run_id` ties their rows together.

Caps come from `config`: `model_run_cap_cents` per run, `model_daily_cap_cents`
for live calls and `model_test_daily_cap_cents` for test calls, each per Eastern
day (`python -m vending.config set model_test_daily_cap_cents 2000 --by <name>`).
The defaults are placeholders until the team sets a budget. `key_env` names the
variable the key came from (never the key), so each row says which key paid.

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
import socket
import time
import uuid
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
DEFAULT_TEST_DAILY_CAP_CENTS = 2000   # test runs need room to compare models (Kevin, 11 Nov)
DEFAULT_MIN_TOKENS = 1024
DEFAULT_KEY_ENV = "VENDING_ANTHROPIC_API_KEY"
# Each purpose has its own daily cap: live spend is the competition's, test spend isn't.
DAILY_CAPS = {"live": ("model_daily_cap_cents", DEFAULT_DAILY_CAP_CENTS),
              "test": ("model_test_daily_cap_cents", DEFAULT_TEST_DAILY_CAP_CENTS)}
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


def check_client(client: Any, key_env: str) -> None:
    """Refuse a real Anthropic client that would pay with anything but the key in `key_env`.

    `key_env` is only a label in `model_calls`, so the client itself is checked:
    its key must be that variable's value, with no bearer token beside it, and no
    header set from the environment (`ANTHROPIC_CUSTOM_HEADERS` is merged into
    every request and could carry another key or a billing header the harness
    never sees). Test stand-ins that aren't `anthropic.Anthropic` are left alone.
    """
    import os

    import anthropic

    if not isinstance(client, anthropic.Anthropic):
        return
    if os.environ.get("ANTHROPIC_CUSTOM_HEADERS") is not None:
        raise HarnessError("ANTHROPIC_CUSTOM_HEADERS is set, and the SDK adds it to every request, so the key "
                           "or the billing could differ from what is recorded; unset it. Nothing was sent")
    from vending import settings

    expected = (settings.get(key_env) or "").strip()
    if not expected or client.api_key != expected or getattr(client, "auth_token", None) is not None:
        raise HarnessError(f"the client doesn't use the key in {key_env} (alone), so its calls couldn't be "
                           "recorded against the right key. Nothing was sent")
    custom = {k.lower() for k in (getattr(client, "_custom_headers", None) or {})}
    if custom & {"authorization", "x-api-key", "anthropic-beta"}:
        raise HarnessError("the client carries its own authorization or beta headers, which the harness "
                           "can't price or attribute. Nothing was sent")


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


def spent_since(conn: psycopg.Connection, since: datetime, purpose: str = "live") -> Decimal:
    """Dollars of model spend since `since`, estimates included.

    Live spend is read from the ledger, the competition's books; test spend from
    `model_calls`, since test calls never reach the ledger.
    """
    if purpose == "live":
        row = conn.execute("select coalesce(sum(-amount_cents), 0) from ledger_entries"
                           " where kind = 'tokens' and at >= %s", (since,)).fetchone()
        return Decimal(row[0]) / 100
    row = conn.execute("select coalesce(sum(cost_usd), 0) from model_calls where purpose = %s and at >= %s",
                       (purpose, since)).fetchone()
    return Decimal(row[0])


@dataclass
class Call:
    response: Any
    cost_usd: Decimal
    recorded: bool          # False when the estimate couldn't be corrected (it stays, overstating)
    max_tokens: int         # what was actually sent, after fitting the caps
    entry: int | None       # the ledger entry's id (live calls only)
    call_id: int | None = None      # the model_calls row
    seconds: float | None = None


@dataclass(frozen=True)
class _Reserved:
    max_tokens: int
    worst: Decimal
    call_id: int
    entry: int | None


class Harness:
    """Sends one request at a time under the run and daily caps, and records each one's cost."""

    def __init__(self, client: Any, connect: Callable[[], psycopg.Connection], *, purpose: str,
                 run_cap_usd: Decimal | None = None, daily_cap_usd: Decimal | None = None,
                 prices: PriceTable | None = None, min_tokens: int = DEFAULT_MIN_TOKENS,
                 now: Callable[[], datetime] | None = None, by: str = "harness",
                 key_env: str = DEFAULT_KEY_ENV, run_id: str | None = None):
        if purpose not in PURPOSES:
            raise HarnessError(f"purpose must be one of {', '.join(PURPOSES)}, not {purpose!r}")
        if key_env.upper().startswith("ANTHROPIC_"):
            # Claude Code reads ANTHROPIC_* variables, so a team key there would pay for its sessions.
            raise HarnessError(f"use VENDING_ANTHROPIC_API_KEY or another non-ANTHROPIC_ name, not {key_env}")
        check_client(client, key_env)
        self.client = client.with_options(max_retries=0)
        self.connect, self.purpose, self.by, self.key_env = connect, purpose, by, key_env
        self.run_id = run_id or uuid.uuid4().hex
        self.host = socket.gethostname()
        self.prices = prices or load_prices()
        self.min_tokens = min_tokens
        self.now = now      # None: the database clock, read under the lock (a test may pass a fixed one)
        daily_key, daily_default = DAILY_CAPS[purpose]
        with connect() as conn:
            self.run_cap = run_cap_usd if run_cap_usd is not None else Decimal(
                config.get_value(conn, "model_run_cap_cents", DEFAULT_RUN_CAP_CENTS)) / 100
            self.daily_cap = daily_cap_usd if daily_cap_usd is not None else Decimal(
                config.get_value(conn, daily_key, daily_default)) / 100
        self.spent = Decimal(0)     # this run, estimates included
        self.reserved = 0           # requests that got past the caps (each has a model_calls row)
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
        effort = (request.get("output_config") or {}).get("effort")

        try:
            counted = self.client.messages.count_tokens(**{k: v for k, v in request.items() if k in COUNTED}).input_tokens
        except anthropic.APIError as error:
            raise HarnessError(f"counting the request failed ({_describe(error)}); nothing was sent or spent") from None

        reference = f"{self.purpose}{' ' + label if label else ''}"
        held = self._reserve(model, effort, counted, wanted, floor, caches, label, reference)
        self.spent += held.worst
        self.reserved += 1

        body = {**request, "max_tokens": held.max_tokens, "service_tier": "standard_only"}
        started = time.monotonic()
        try:
            response = self.client.messages.create(**body)
        except anthropic.APIStatusError as error:
            seconds = time.monotonic() - started
            if error.status_code < 500:      # refused by the API, not billed
                self._refused(held, f"{reference} refused: {_describe(error)}", error, seconds)
                self.spent -= held.worst
                raise HarnessError(f"the model call was refused ({_describe(error)}); not billed") from None
            self._mark(held, f"{reference} estimate: {_describe(error)}", error, seconds)
            raise HarnessError(f"the model call failed ({_describe(error)}); kept at its worst case, "
                               f"${held.worst:.4f}") from None
        except anthropic.APIError as error:   # timeout or dropped connection: it may have run
            self._mark(held, f"{reference} estimate: {_describe(error)}", error, time.monotonic() - started)
            raise HarnessError(f"the model call failed ({_describe(error)}); kept at its worst case, "
                               f"${held.worst:.4f}") from None
        seconds = time.monotonic() - started

        # From here on the response is paid for: nothing may stop it reaching the caller.
        worst = held.worst
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
            recorded = self._settle(held, paid, answered_by, usage, response, seconds,
                                    f"{reference} {response.id}{note}", note.strip())
        except Exception:
            log.exception("model call %s answered but its cost could not be worked out or recorded; "
                          "the worst-case estimate stays in the books", reference)
            paid, recorded = worst, False
        self.spent += paid - worst
        call = Call(response, paid, recorded, held.max_tokens, held.entry, held.call_id, round(seconds, 2))
        self.calls.append(call)
        return call

    def _reserve(self, model: str, effort: str | None, counted: int, wanted: int, floor: int, caches: str | None,
                 label: str, reference: str) -> _Reserved:
        """Fit max_tokens under the caps and write the worst case, under a short lock."""
        try:
            with self.connect() as conn, conn.transaction():
                conn.execute(f"set local lock_timeout = '{LOCK_TIMEOUT}'")
                conn.execute("select pg_advisory_xact_lock(%s)", (LOCK_KEY,))
                now = self.now() if self.now else ledger.db_now(conn)
                left_today = self.daily_cap - spent_since(conn, day_start(now), self.purpose)
                allowed = min(self.run_cap - self.spent, left_today)
                room = allowed - worst_case_usd(model, counted, 0, self.prices, caches=caches)
                r = self.prices.rates(model, padded(counted))
                per_token = r.output * self.prices.us_only_multiplier / MILLION
                max_tokens = min(wanted, int(room / per_token)) if room > 0 else 0
                if max_tokens < floor:
                    raise CapReached(
                        f"not sent: ${max(allowed, Decimal(0)):.4f} is left (run cap ${self.run_cap:.2f}; "
                        f"${max(left_today, Decimal(0)):.4f} of today's {self.purpose} cap, ${self.daily_cap:.2f}), "
                        f"and this {counted}-token request needs room for at least {floor} output tokens. "
                        "Nothing was spent.")
                worst = worst_case_usd(model, counted, max_tokens, self.prices, caches=caches)
                entry = None
                if self.purpose == "live":      # only live calls are the competition's money
                    entry = ledger.record_tokens(conn, worst, model, padded(counted), max_tokens,
                                                 f"{reference} pending", by=self.by, at=now).id
                call_id = conn.execute(
                    "insert into model_calls (at, purpose, label, run_id, model, effort, key_env, max_tokens,"
                    " worst_case_usd, cost_usd, ledger_entry, detail, recorded_by, host)"
                    " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s) returning id",
                    (now, self.purpose, label, self.run_id, model, effort, self.key_env, max_tokens,
                     worst, worst, entry, self.by, self.host)).fetchone()[0]
        except CapReached:
            raise
        except Exception as error:
            raise HarnessError(f"couldn't write the worst-case estimate ({type(error).__name__}: "
                               f"{error}); nothing was sent") from None
        return _Reserved(max_tokens, worst, call_id, entry)

    def _settle(self, held: _Reserved, dollars: Decimal, model: str, usage: Any, response: Any, seconds: float,
                reference: str, note: str) -> bool:
        """Correct the estimate to what the call billed. False if that failed."""
        fresh, read = _n(usage.input_tokens), _n(usage.cache_read_input_tokens)
        written, out = _n(usage.cache_creation_input_tokens), _n(usage.output_tokens)
        try:
            with self.connect() as conn, conn.transaction():
                conn.execute(
                    "update model_calls set status = 'ok', finished_at = now(), answered_by = %s, stop_reason = %s,"
                    " request_id = %s, message_id = %s, input_tokens = %s, cache_write_tokens = %s,"
                    " cache_read_tokens = %s, output_tokens = %s, cost_usd = %s, seconds = %s, detail = %s"
                    " where id = %s",
                    (model, getattr(response, "stop_reason", None), getattr(response, "_request_id", None),
                     getattr(response, "id", None), fresh, written, read, out, dollars, round(seconds, 2),
                     note, held.call_id))
                if held.entry is not None:
                    conn.execute("update ledger_entries set amount_cents = %s, detail = %s, reference = %s"
                                 " where id = %s and kind = 'tokens'",
                                 (ledger._numeric(-ledger.dollars_to_cents(dollars)),
                                  f"{model}: {fresh + read + written} tokens in, {out} out", reference, held.entry))
                    conn.execute("insert into action_log (kind, tool, arguments, outcome, detail)"
                                 " values ('write', 'ledger.tokens', %s, 'done', %s)",
                                 (Jsonb({"entry": held.entry, "cost": str(dollars), "reference": reference}),
                                  f"model call estimate {held.entry} settled at ${dollars:.6f}"))
            return True
        except Exception:
            log.exception("couldn't settle model call %s at $%s; its worst-case estimate stays", held.call_id, dollars)
            return False

    def _refused(self, held: _Reserved, reference: str, error: Any, seconds: float) -> None:
        """A 4xx: the API refused it and didn't bill. Keep the row at $0, so the attempt is still seen."""
        try:
            with self.connect() as conn, conn.transaction():
                conn.execute("update model_calls set status = 'refused', finished_at = now(), cost_usd = 0,"
                             " request_id = %s, seconds = %s, detail = %s, ledger_entry = null where id = %s",
                             (_request_id(error), round(seconds, 2), reference, held.call_id))
                if held.entry is not None:
                    conn.execute("delete from ledger_entries where id = %s and kind = 'tokens'", (held.entry,))
        except Exception:
            log.exception("couldn't clear refused model call %s; its worst-case estimate stays", held.call_id)

    def _mark(self, held: _Reserved, reference: str, error: Any, seconds: float) -> None:
        """Keep the worst case (it may have billed), and say why."""
        try:
            with self.connect() as conn, conn.transaction():
                conn.execute("update model_calls set status = 'error', finished_at = now(), request_id = %s,"
                             " seconds = %s, detail = %s where id = %s",
                             (_request_id(error), round(seconds, 2), reference, held.call_id))
                if held.entry is not None:
                    conn.execute("update ledger_entries set reference = %s where id = %s", (reference, held.entry))
        except Exception:
            log.exception("couldn't mark model call %s as an estimate; it stays at its worst case", held.call_id)


def _describe(error: Any) -> str:
    status = getattr(error, "status_code", None)
    return f"{type(error).__name__}{f' {status}' if status else ''}"


def _request_id(error: Any) -> str | None:
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    try:
        return headers.get("request-id") if headers is not None else None
    except Exception:
        return None
