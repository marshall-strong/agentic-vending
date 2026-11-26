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
   either: spend we can't record isn't spent. A harness given `hold` (the
   agent's run lease, `vending.run_lease.hold`) checks it first, in the same
   transaction: if it says no, nothing is reserved or sent and `NotHeld` says why.
3. The request is sent once; the client's own retries are turned off, because
   each could bill again. A harness given `stop_at` (the caller's deadline)
   checks it after the count and again just before sending: past it, nothing is
   sent (`PastDeadline`). One given `request_deadline` waits at most that long
   for the answer, by the wall clock, since the SDK's timeout only bounds each
   wait for bytes; past it the harness gives up (`Abandoned`) and keeps the
   worst case. Given `hold_margin` too, it sends only if the lease `hold`
   renewed still lasts past that hard end.
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

`check_credit` is the one call made here outside a `Harness`: a free
`messages.count_tokens` the scheduler sends before it claims a time, to see
whether the key has credit. It sends no paid request and writes no row.

Caps come from `config`: `model_run_cap_cents` per run, `model_daily_cap_cents`
for live calls and `model_test_daily_cap_cents` for test calls, each per Eastern
day (`python -m vending.config set model_test_daily_cap_cents 2000 --by <name>`).
The defaults are placeholders until the team sets a budget. `key_env` names the
variable the key came from (never the key), so each row says which key paid.

**One server tool is priced: the basic web search** (`web_search_20250305`, $10
per 1,000 searches, `server_tools` in the price file), and only with a
`max_uses` of 1 to `MAX_SEARCHES` and an `allowed_domains` list, so every search
is capped and stays on named sites (`vending.price_search` is its one user). Its
worst case reads the prompt once more for each search, plus
`SEARCH_RESULT_TOKENS` of results for each search so far, plus the searches'
fee; its cost adds `usage.server_tool_use.web_search_requests` at the fee.
`SEARCH_RESULT_TOKENS` is our allowance, not a documented figure: if a call
bills more than its worst case, the harness logs a warning, records what was
billed and marks the row, and a `Budget` over such calls leaves nothing for the
rest of that Eastern day. The fee is `web_search_requests` (an errored search
isn't billed); a call kept at its worst case already holds every search's fee.

Not priced here, so refused: every other server tool (the web search with
dynamic filtering, `web_search_20260209` and later, which runs code; web
fetch), fast mode, priority capacity (requests are sent `standard_only`), streaming (use a
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
# The one server tool with a price: the basic web search, which Claude calls directly (no code execution).
SEARCH_TOOL_TYPE = "web_search_20250305"
MAX_SEARCHES = 5                # the most `max_uses` one request may ask for
# Input tokens allowed for each search's results in the worst case: our allowance, not a documented figure.
SEARCH_RESULT_TOKENS = 25_000


class HarnessError(Exception):
    """The call wasn't made, or failed. The message says why and what was recorded."""


SPEND_DATABASE_ENV = "SPEND_DATABASE_URL"
SPEND_DATABASE_HELP = (
    "Set SPEND_DATABASE_URL to the team spend database with write access, "
    "and apply its migrations. No paid request was sent."
)


def anthropic_client(**kwargs):
    """Create the SDK client used by Harness."""
    import anthropic

    return anthropic.Anthropic(max_retries=0, **kwargs)


def spend_category(label: str, by: str = "") -> str:
    if label.startswith(("price_search", "price_check ", "pack price lookup ")):
        return "price search"
    if by in {"replay", "simulation"} or label.startswith(("bench ", "sweep ", "replay ")):
        return "simulations/bench"
    return "agent runs"


def _team_database_fallback(url: str) -> bool:
    """Require an explicit remote target before reusing the run's database."""
    import os

    from psycopg.conninfo import conninfo_to_dict
    from vending import db

    params = conninfo_to_dict(url)
    if params.get("service") or any(os.environ.get(name) for name in db.LIBPQ_HOST_OVERRIDES):
        return False
    hosts = params.get("host", "").split(",")
    if params.get("hostaddr"):
        hosts.extend(params["hostaddr"].split(","))
    return all(host.strip() and not host.startswith(("/", "@"))
               and host.lower().rstrip(".") not in db.LOCAL_HOSTS for host in hosts)


def spend_connection() -> psycopg.Connection:
    """Open a separate short connection to the team's spend database."""
    from vending import db, settings

    url = settings.get(SPEND_DATABASE_ENV)
    try:
        if not url:
            url = settings.get("DATABASE_URL")
            if not url or not _team_database_fallback(url):
                raise HarnessError(SPEND_DATABASE_HELP)
        return psycopg.connect(url, autocommit=True, connect_timeout=db.CONNECT_TIMEOUT,
                               options=db.session_options(url, "-c statement_timeout=5000"))
    except (psycopg.Error, ValueError):
        raise HarnessError(SPEND_DATABASE_HELP) from None


def check_spend_database(connect=None) -> None:
    connect = connect or spend_connection
    try:
        with connect() as conn:
            writable = conn.execute(
                "select not current_setting('transaction_read_only')::boolean"
                " and has_table_privilege('model_spend', 'SELECT')"
                " and has_table_privilege('model_spend', 'INSERT')"
                " and has_table_privilege('model_spend', 'UPDATE')"
            ).fetchone()[0]
            if not writable:
                raise HarnessError(SPEND_DATABASE_HELP)
    except psycopg.Error:
        raise HarnessError(SPEND_DATABASE_HELP) from None


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


class NotHeld(HarnessError):
    """Not sent: `hold` said the caller may not spend now (the agent lost its run lease). Nothing was reserved."""


class PastDeadline(HarnessError):
    """Not sent: the caller's deadline (`stop_at`) passed while the request was being counted or reserved."""


class NoCredit(HarnessError):
    """Not sent, or refused unbilled: the API says the key's account has no credit left (a 400 that says "credit
    balance", or an error of type billing_error). Every later call will fail the same way until a person adds credit, so callers
    that loop (the price search) stop at once instead of trying each item. Nothing was spent."""


def _no_credit(error: Any) -> bool:
    """The API refused for the account's credit or billing. Read from the error's type and message only: neither
    is logged or raised (`_describe` gives the class and status)."""
    if getattr(error, "status_code", None) not in (400, 402):
        return False
    parts = [str(getattr(error, "message", "") or "")]
    body = getattr(error, "body", None)
    inner = body.get("error") if isinstance(body, dict) and isinstance(body.get("error"), dict) else body
    if isinstance(inner, dict):
        if inner.get("type") == "billing_error":
            return True
        parts.append(str(inner.get("message") or ""))
    return "credit balance" in " ".join(parts).lower()


class KeyRefused(HarnessError):
    """The API refused the key itself (401 or 403): not a lack of credit, and no wait fixes it. Nothing was spent."""


class Unreachable(HarnessError):
    """The credit check got no answer it could read: the API couldn't be reached, timed out, was busy (429) or
    failed (5xx). The run's own first request would hit the same, so a scheduled run waits. Nothing was spent."""


CREDIT_CHECK_TIMEOUT = 30.0     # seconds for the one count_tokens call of `check_credit`
CREDIT_CHECK_MESSAGES = [{"role": "user", "content": "credit check"}]


def check_credit(client: Any, model: str, *, key_env: str = DEFAULT_KEY_ENV) -> None:
    """Whether the key can pay for `model` now, from one free `messages.count_tokens` call: never a paid request.

    The scheduler runs it before it claims a time (`vending.agent_schedule`), with the run's own client and model.
    The client is checked as the harness checks it (`check_client`), so the check asks with the key the run would
    pay with. Returns when the API counted the request; raises `NoCredit` when it says the account has no credit
    (the same reading as a paid call's, `_no_credit`), `KeyRefused` for a 401 or 403, `Unreachable` for no usable
    answer (a timeout, a dropped connection, a 429 or a 5xx), and `HarnessError` for any other refusal (the run's
    own request would record it). Count calls bill nothing, so no `model_calls` row is written. Neither the key nor
    the API's message is in what it raises."""
    import anthropic

    check_client(client, key_env)
    try:
        client.with_options(max_retries=0, timeout=CREDIT_CHECK_TIMEOUT).messages.count_tokens(
            model=model, messages=CREDIT_CHECK_MESSAGES)
    except anthropic.APIStatusError as error:
        if _no_credit(error):
            raise NoCredit(f"the API says the key has no credit left ({_describe(error)})") from None
        if error.status_code in (401, 403):
            raise KeyRefused(f"the API refused the key ({_describe(error)})") from None
        if error.status_code == 429 or error.status_code >= 500:
            raise Unreachable(f"the API didn't answer the credit check ({_describe(error)})") from None
        raise HarnessError(f"the API refused the credit check ({_describe(error)})") from None
    except anthropic.APIError as error:     # a timeout or a dropped connection
        raise Unreachable(f"the API couldn't be reached ({_describe(error)})") from None


class Abandoned(HarnessError):
    """Sent, but no answer came within `request_deadline`: the harness stopped waiting. It may have billed, so
    its worst case stays in the books. Its thread may still be going: `in_flight()` says so."""


NEAR_END = "the lease `hold` renewed ends before the request's hard end"


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
    web_search_usd: Decimal | None = None      # per search (`SEARCH_TOOL_TYPE`); None: web search is refused

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
    search = (raw.get("server_tools") or {}).get(SEARCH_TOOL_TYPE)
    return PriceTable(raw["checked"], Decimal(raw["us_only_multiplier"]), models,
                      Decimal(search["per_use"]) if search else None)


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


def tokens_in(counted: int, searches: int = 0) -> int:
    """The most input tokens a request counted at `counted` might bill, with up to `searches` web searches.

    After each search the model reads the prompt again with every result so far, so the prompt is read
    `searches + 1` times, and the k-th search's results up to `searches - k + 1` times."""
    return padded(counted) * (searches + 1) + SEARCH_RESULT_TOKENS * searches * (searches + 1) // 2


def worst_case_usd(model: str, counted: int, max_tokens: int, prices: PriceTable, *, caches: str | None,
                   searches: int = 0) -> Decimal:
    """The most a request can bill: padded input at its dearest possible rate and tier,
    plus `max_tokens` out, with US-only routing assumed (the workspace may default to it),
    plus up to `searches` web searches and the results they could add (`tokens_in`).

    `caches` is None, "5m" or "1h": with caching asked for, any input token could be a write.
    """
    read = tokens_in(counted, searches)
    r = prices.rates(model, read)
    unit_in = {None: r.input, "5m": max(r.input, r.cache_write_5m), "1h": max(r.input, r.cache_write_1h)}[caches]
    fee = searches * prices.web_search_usd if searches else Decimal(0)
    return (read * unit_in + max_tokens * r.output) / MILLION * prices.us_only_multiplier + fee


def _caching(request: dict) -> str | None:
    """'1h' if any cache_control in the request asks for an hour, '5m' for any other, else None."""
    text = json.dumps(request, default=str)
    if '"cache_control"' not in text:
        return None
    return "1h" if '"ttl": "1h"' in text else "5m"


def _refuse_unpriced(request: dict, prices: PriceTable | None = None) -> int:
    """Refuse anything the harness can't price. Returns the most web searches the request may make."""
    searches = 0
    for tool in request.get("tools") or []:
        kind = tool.get("type") if isinstance(tool, dict) else None
        if kind is None or str(kind).startswith(CLIENT_TOOL_PREFIXES):
            continue
        if kind != SEARCH_TOOL_TYPE or prices is None or prices.web_search_usd is None:
            raise HarnessError(f"server tool {kind!r} bills per use and has no price here; not sent")
        uses = tool.get("max_uses")
        if isinstance(uses, bool) or not isinstance(uses, int) or not 1 <= uses <= MAX_SEARCHES:
            raise HarnessError(f"a web search needs max_uses of 1 to {MAX_SEARCHES}, so its cost has a limit; "
                               "not sent")
        domains = tool.get("allowed_domains")
        if not isinstance(domains, list) or not domains or "blocked_domains" in tool:
            raise HarnessError("a web search needs an allowed_domains list, so it stays on sites we named; not sent")
        if tool.get("allowed_callers") not in (None, ["direct"]):
            raise HarnessError("a web search must be called directly, not from code execution; not sent")
        searches += uses
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
    return searches


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
class Budget:
    """A daily cap of its own for the calls whose `model_calls.label` starts with `prefix` (`vending.price_search`).

    Read under the reservation lock with the other caps, on the reservation's Eastern day: what those calls billed,
    and the worst case still held for any that haven't settled. If one of them billed more than its worst case
    that day, nothing is left: the worst case is an allowance, not a limit the API promises
    (`SEARCH_RESULT_TOKENS`), so after a miss the day's budget stops rather than trust it again."""
    prefix: str
    daily_usd: Decimal

    def left(self, conn: psycopg.Connection, now: datetime) -> Decimal:
        spent, missed = conn.execute(
            "select coalesce(sum(cost_usd), 0), coalesce(bool_or(status = 'ok' and cost_usd > worst_case_usd), false)"
            " from model_calls where left(label, %s) = %s and at >= %s",
            (len(self.prefix), self.prefix, day_start(now))).fetchone()
        return Decimal(0) if missed else self.daily_usd - Decimal(spent)


@dataclass(frozen=True)
class _Reserved:
    max_tokens: int
    worst: Decimal
    call_id: int
    entry: int | None
    spend_id: uuid.UUID | None


class Harness:
    """Sends one request at a time under the run and daily caps, and records each one's cost."""

    def __init__(self, client: Any, connect: Callable[[], psycopg.Connection], *, purpose: str,
                 run_cap_usd: Decimal | None = None, daily_cap_usd: Decimal | None = None,
                 prices: PriceTable | None = None, min_tokens: int = DEFAULT_MIN_TOKENS,
                 now: Callable[[], datetime] | None = None, by: str = "harness",
                 key_env: str = DEFAULT_KEY_ENV, run_id: str | None = None,
                 hold: Callable[[psycopg.Connection], str | None] | None = None,
                 hold_margin: float | None = None, request_deadline: float | None = None,
                 stop_at: float | None = None,
                 spend_connect: Callable[[], psycopg.Connection] | None = None):
        if purpose not in PURPOSES:
            raise HarnessError(f"purpose must be one of {', '.join(PURPOSES)}, not {purpose!r}")
        if key_env.upper().startswith("ANTHROPIC_"):
            # Claude Code reads ANTHROPIC_* variables, so a team key there would pay for its sessions.
            raise HarnessError(f"use VENDING_ANTHROPIC_API_KEY or another non-ANTHROPIC_ name, not {key_env}")
        import anthropic

        check_client(client, key_env)
        self.spend_connect = spend_connect
        if self.spend_connect is None and isinstance(client, anthropic.Anthropic):
            self.spend_connect = spend_connection
        if self.spend_connect is not None:
            check_spend_database(self.spend_connect)
        self.client = client.with_options(max_retries=0)
        self.connect, self.purpose, self.by, self.key_env = connect, purpose, by, key_env
        self.run_id = run_id or uuid.uuid4().hex
        # Simulated seasons reuse their scratch run numbers.
        self.spend_run_id = f"{self.run_id} / {uuid.uuid4().hex}" if by == "simulation" else self.run_id
        self.host = socket.gethostname()
        self.prices = prices or load_prices()
        self.min_tokens = min_tokens
        self.now = now      # None: the database clock, read under the lock (a test may pass a fixed one)
        # Checked in the reservation's transaction, before anything is reserved: why not to spend, or None.
        # `hold_margin`: the seconds `hold` promises, from when the reservation starts, when it says yes.
        self.hold, self.hold_margin = hold, hold_margin
        # The most seconds one request may take, from send to answer, by the wall clock. The SDK's own timeout
        # only bounds each wait for bytes, so a reply that keeps trickling in could outlast it. None: no limit.
        self.request_deadline = request_deadline
        # `time.monotonic()` past which nothing more is reserved or sent (the caller's deadline). None: none.
        self.stop_at = stop_at
        self._threads: list = []    # requests given up on that may still be going
        daily_key, daily_default = DAILY_CAPS[purpose]
        with connect() as conn:
            self.run_cap = run_cap_usd if run_cap_usd is not None else Decimal(
                config.get_value(conn, "model_run_cap_cents", DEFAULT_RUN_CAP_CENTS)) / 100
            self.daily_cap = daily_cap_usd if daily_cap_usd is not None else Decimal(
                config.get_value(conn, daily_key, daily_default)) / 100
        self.spent = Decimal(0)     # this run, estimates included
        self.reserved = 0           # requests that got past the caps (each has a model_calls row)
        self.calls: list[Call] = []

    def in_flight(self) -> bool:
        """Whether a request the harness gave up on (`Abandoned`) may still be going."""
        return any(t.is_alive() for t in self._threads)

    def _past(self, when: str) -> None:
        if self.stop_at is not None and time.monotonic() >= self.stop_at:
            raise PastDeadline(f"not sent: the run's deadline passed {when}. Nothing was spent.")

    def _send(self, body: dict) -> Any:
        """`messages.create(**body)`, given at most `request_deadline` seconds by the wall clock. Past that, its
        thread is left to end by itself, and this raises `Abandoned`."""
        if self.request_deadline is None:
            return self.client.messages.create(**body)
        import threading

        done: dict = {}

        def send() -> None:
            try:
                done["response"] = self.client.messages.create(**body)
            except BaseException as error:      # noqa: BLE001 - handed to the caller below
                done["error"] = error
        thread = threading.Thread(target=send, name="model_request", daemon=True)
        thread.start()
        thread.join(self.request_deadline)
        if thread.is_alive():
            self._threads.append(thread)
            raise Abandoned(f"no answer within {self.request_deadline:g} s")
        if "error" in done:
            raise done["error"]
        return done["response"]

    def call(self, request: dict, *, label: str = "", daily_budget: Budget | None = None) -> Any:
        """Send `request` (a `messages.create` body) under the caps and record its cost. Returns the response."""
        return self.call_detailed(request, label=label, daily_budget=daily_budget).response

    def call_detailed(self, request: dict, *, label: str = "", daily_budget: Budget | None = None) -> Call:
        """`daily_budget`: one more daily cap, for the calls whose label starts with its prefix (`Budget`), checked
        and reserved under the same lock as the run and daily caps."""
        import anthropic

        searches = _refuse_unpriced(request, self.prices)
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
            if _no_credit(error):
                raise NoCredit(f"the API says the key has no credit left ({_describe(error)}); nothing was sent "
                               "or spent") from None
            raise HarnessError(f"counting the request failed ({_describe(error)}); nothing was sent or spent") from None
        # The caller's deadline can't stop this thread, so it is checked here, and again just before sending.
        self._past("while the request was counted")

        reference = f"{self.purpose}{' ' + label if label else ''}"
        reserving = time.monotonic()
        if daily_budget is not None and not label.startswith(daily_budget.prefix):
            raise HarnessError(f"a call under the {daily_budget.prefix!r} budget must have a label starting with it")
        held = self._reserve(model, effort, counted, wanted, floor, caches, label, reference, searches, daily_budget)
        if held.spend_id is not None:
            try:
                self._reserve_spend(held, label, model)
            except Exception:
                self._refused(held, "not sent: spend database unavailable", None, 0.0)
                raise HarnessError(SPEND_DATABASE_HELP) from None
        self.spent += held.worst
        self.reserved += 1

        # The last checks before money can be spent: the deadline, and that the lease `hold` renewed still lasts
        # past the request's hard end (the reservation's own commit may have been slow).
        try:
            self._past("while the request was reserved")
            if self.hold is not None and self.hold_margin is not None and self.request_deadline is not None                     and time.monotonic() + self.request_deadline > reserving + self.hold_margin:
                raise NotHeld(f"not sent: {NEAR_END}. Nothing was spent.")
        except HarnessError as error:
            self._refused(held, f"{reference} not sent: {error}", None, 0.0)
            self.spent -= held.worst
            raise

        body = {**request, "max_tokens": held.max_tokens, "service_tier": "standard_only"}
        started = time.monotonic()
        try:
            response = self._send(body)
        except Abandoned as error:      # it may still run and bill: kept at its worst case
            self._mark(held, f"{reference} estimate: abandoned, {error}", None, time.monotonic() - started)
            raise Abandoned(f"the model call was abandoned ({error}); kept at its worst case, "
                            f"${held.worst:.4f}") from None
        except anthropic.APIStatusError as error:
            seconds = time.monotonic() - started
            if error.status_code < 500:      # refused by the API, not billed
                self._refused(held, f"{reference} refused: {_describe(error)}", error, seconds)
                self.spent -= held.worst
                if _no_credit(error):
                    raise NoCredit(f"the model call was refused: the API says the key has no credit left "
                                   f"({_describe(error)}); not billed") from None
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
            # Anthropic bills a search per `web_search_requests`; a search that came back as an error isn't counted.
            searched = _n(getattr(getattr(usage, "server_tool_use", None), "web_search_requests", 0))
            if answered_by not in self.prices.models:       # the worst case already holds every search's fee
                note, paid = f" (answered by unpriced {answered_by}; kept at worst case)", worst
                log.error("model call answered by %s, which has no price; kept at its worst case", answered_by)
            elif searched and searches:
                fee = searched * self.prices.web_search_usd
                paid += fee
                note += f" ({searched} web search{'es' if searched != 1 else ''}, ${fee:.2f})"
            if searched and not searches:
                note += " (server tool use not priced)"
                log.error("model call used server tools, which aren't priced: %s", usage.server_tool_use)
            tier = getattr(usage, "service_tier", None)
            if tier not in (None, "standard"):
                note, paid = f" (served at {tier} capacity, not priced; kept at worst case)", worst
                log.error("model call was served at %s capacity, which has no price; kept at its worst case", tier)
            if paid <= 0:       # usage reported nothing (a proxy stripped it?): never erase a paid call
                note, paid = " (usage reported no tokens; kept at worst case)", worst
                log.error("model call %s reported no usage; kept at its worst case", reference)
            elif paid > worst:  # only a search's results can do this: recorded as billed
                note += " (billed more than its worst case)"
                log.warning("model call %s billed $%s, more than its worst case $%s; raise SEARCH_RESULT_TOKENS",
                            reference, paid, worst)
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
                 label: str, reference: str, searches: int = 0, budget: Budget | None = None) -> _Reserved:
        """Fit max_tokens under the caps and write the worst case, under a short lock.

        Every cap is read and the worst case written in one transaction under `LOCK_KEY`, so two calls can't both
        fit in the same room. Each day is the Eastern day of `now`, the reservation's own time."""
        spend_id = uuid.uuid4() if self.spend_connect else None
        try:
            with self.connect() as conn, conn.transaction():
                conn.execute(f"set local lock_timeout = '{LOCK_TIMEOUT}'")
                conn.execute("select pg_advisory_xact_lock(%s)", (LOCK_KEY,))
                if self.hold is not None and (why := self.hold(conn)) is not None:
                    raise NotHeld(f"not sent: {why}. Nothing was reserved or spent.")
                now = self.now() if self.now else ledger.db_now(conn)
                left_today = self.daily_cap - spent_since(conn, day_start(now), self.purpose)
                left_budget = budget.left(conn, now) if budget is not None else None
                allowed = min(self.run_cap - self.spent, left_today,
                              *([left_budget] if left_budget is not None else []))
                room = allowed - worst_case_usd(model, counted, 0, self.prices, caches=caches, searches=searches)
                r = self.prices.rates(model, tokens_in(counted, searches))
                per_token = r.output * self.prices.us_only_multiplier / MILLION
                max_tokens = min(wanted, int(room / per_token)) if room > 0 else 0
                if max_tokens < floor:
                    raise CapReached(
                        f"not sent: ${max(allowed, Decimal(0)):.4f} is left (run cap ${self.run_cap:.2f}; "
                        f"${max(left_today, Decimal(0)):.4f} of today's {self.purpose} cap, ${self.daily_cap:.2f}"
                        + (f"; ${max(left_budget, Decimal(0)):.4f} of today's {budget.prefix.strip()} cap, "
                           f"${budget.daily_usd:.2f}" if budget is not None and left_budget is not None else "")
                        + "), "
                        f"and this {counted}-token request needs room for at least {floor} output tokens. "
                        "Nothing was spent.")
                worst = worst_case_usd(model, counted, max_tokens, self.prices, caches=caches, searches=searches)
                entry = None
                opened = ledger.real_opening(conn) if self.purpose == "live" else None
                if ledger.competition_call(self.purpose, now, opened.at if opened else None):
                    entry = ledger.record_tokens(conn, worst, model, tokens_in(counted, searches), max_tokens,
                                                 f"{reference} pending", by=self.by, at=now).id
                spend_column = ", spend_id" if spend_id else ""
                spend_value = ", %s" if spend_id else ""
                args = (now, self.purpose, label, self.run_id, model, effort, self.key_env, max_tokens,
                        worst, worst, entry, self.by, self.host)
                call_id = conn.execute(
                    "insert into model_calls (at, purpose, label, run_id, model, effort, key_env, max_tokens,"
                    f" worst_case_usd, cost_usd, ledger_entry, detail, recorded_by, host{spend_column})"
                    f" values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s{spend_value}) returning id",
                    (*args, spend_id) if spend_id else args).fetchone()[0]
        except (CapReached, NotHeld):
            raise
        except Exception as error:
            raise HarnessError(f"couldn't write the worst-case estimate ({type(error).__name__}: "
                               f"{error}); nothing was sent") from None
        return _Reserved(max_tokens, worst, call_id, entry, spend_id)

    def _reserve_spend(self, held: _Reserved, label: str, model: str) -> None:
        category = spend_category(label, self.by)
        # Avoid putting lookup targets or other request contents in the spend record.
        name = "Price search" if category == "price search" else label
        with self.spend_connect() as conn, conn.transaction():
            conn.execute(
                "insert into model_spend (id, purpose, category, label, run_id, model, cost_usd)"
                " values (%s, %s, %s, %s, %s, %s, %s) on conflict (id) do nothing",
                (held.spend_id, self.purpose, category, name, self.spend_run_id, model, held.worst))

    def _finish_spend(self, held: _Reserved, *, request_id: str | None = None, model: str | None = None,
                      dollars: Decimal | None = None, confirmed: bool = False,
                      tokens_in: int | None = None, tokens_out: int | None = None,
                      refused: bool = False) -> bool:
        if held.spend_id is None:
            return True
        try:
            with self.spend_connect() as conn, conn.transaction():
                if request_id:
                    conn.execute("select pg_advisory_xact_lock(hashtext(%s))", (request_id,))
                    duplicate = conn.execute("select 1 from model_spend where request_id = %s and id <> %s",
                                             (request_id, held.spend_id)).fetchone()
                    if duplicate:
                        request_id, dollars, refused, confirmed = None, Decimal(0), True, True
                conn.execute(
                    "update model_spend set request_id = %s, model = coalesce(%s, model),"
                    " cost_usd = coalesce(%s, cost_usd), confirmed = %s, tokens_in = %s,"
                    " tokens_out = %s, refused = %s where id = %s",
                    (request_id, model, dollars, confirmed, tokens_in, tokens_out, refused, held.spend_id))
            return True
        except Exception as error:
            log.error("team spend row %s still has its estimate (%s)", held.spend_id, type(error).__name__)
            return False


    def _settle(self, held: _Reserved, dollars: Decimal, model: str, usage: Any, response: Any, seconds: float,
                reference: str, note: str) -> bool:
        """Correct the estimate to what the call billed. False if that failed."""
        fresh, read = _n(usage.input_tokens), _n(usage.cache_read_input_tokens)
        written, out = _n(usage.cache_creation_input_tokens), _n(usage.output_tokens)
        recorded = self._finish_spend(held, request_id=getattr(response, "_request_id", None),
                                      model=model, dollars=dollars, confirmed="kept at worst case" not in note,
                                      tokens_in=fresh + read + written, tokens_out=out)
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
            return recorded
        except Exception:
            log.exception("couldn't settle model call %s at $%s; its worst-case estimate stays", held.call_id, dollars)
            return False

    def _refused(self, held: _Reserved, reference: str, error: Any, seconds: float) -> None:
        """A 4xx: the API refused it and didn't bill. Keep the row at $0, so the attempt is still seen."""
        self._finish_spend(held, request_id=_request_id(error), dollars=Decimal(0), confirmed=True, refused=True)
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
        self._finish_spend(held, request_id=_request_id(error))
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
