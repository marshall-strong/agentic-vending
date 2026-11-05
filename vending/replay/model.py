"""A model decider: Claude reads the week and returns a plan, under hard spending caps.

One Messages API call per decision. The answer comes back as JSON in a fixed
shape (structured output) and goes through `check_plan` like any other
decider's, so a bad answer fails "valid plan" instead of crashing the replay.

**Spending is capped before each request, not discovered after it** (`Budget`):

- The request is counted first, output schema and tools included. Counting is free.
- The most it can cost is input x input price + max_tokens x output price, and
  `max_tokens` (which includes the model's thinking) is set so that can't exceed
  the smaller of the run cap and what's left of today's cap. If even a short
  answer won't fit, nothing is sent.
- The spend log is locked from reading today's total until the worst case is
  written, so two runs at once can't both spend the same dollar.
- Retries are off, so a timeout can't bill twice.

**Every request is logged** to a JSON Lines file outside the repo and OneDrive
(`~/.vending/replay-spend.jsonl`, or `VENDING_SPEND_LOG`): a line with the worst
case before it's sent, then a line with the real cost. A request the API
rejected (a 4xx: bad key, bad request, rate limit) isn't billed, so it closes at
$0. One that timed out, failed on a 5xx, or crashed stays at its worst case. The
daily cap sums today's lines (Eastern time) on this computer only.

The key comes from `VENDING_ANTHROPIC_API_KEY` (the team's key) and never from
an `ANTHROPIC_*` variable, which Claude Code reads and would bill. Requests go to
api.anthropic.com unless `VENDING_ANTHROPIC_BASE_URL` says otherwise.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from pathlib import Path

from vending import settings
from vending.replay.state import EASTERN, WeekState
from vending.slots import SLOTS

DEFAULT_MODEL = "claude-opus-5-5"   # pending a group decision
DEFAULT_EFFORT = "medium"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_KEY_ENV = "VENDING_ANTHROPIC_API_KEY"
API_URL = "https://api.anthropic.com"

RUN_CAP = Decimal("1.00")
# Placeholder for the team: a low cap on all test spending per day, kept under $5.
DAILY_CAP = Decimal("4.00")

# Dollars per million tokens (input, output), Anthropic API list prices, Nov 2026.
PRICES = {
    "claude-opus-5-5": (Decimal("4.00"), Decimal("20.00")),
    "claude-sonnet-5-5": (Decimal("2.00"), Decimal("10.00")),
    "claude-haiku-5-5": (Decimal("0.10"), Decimal("0.50")),
}
# Haiku 5.5 costs more on prompts over 100K tokens.
LONG_PROMPT = 100_000
HAIKU_LONG = (Decimal("0.50"), Decimal("2.50"))
COUNT_MARGIN = 100      # tokens, on top of an exact count
MAX_TOKENS = 16_000     # non-streaming ceiling
MIN_TOKENS = 4_000      # below this a plan for 20 slots, with thinking, won't fit
LOCK_WAIT = 30          # seconds to wait for another run's lock on the spend log

PLAN_RULES = """\
The plan lists every slot once:
- keep: true means no change and nothing loaded. Then fill_to, sale_price and product are null.
- Otherwise fill_to is the units on the rail after the visit (0 to 50) and sale_price is the shelf \
price including tax, as text like "2.50": a multiple of $0.05, at most $6.00, and when the unit cost \
is known, at least unit cost x 1.08 rounded up to the next $0.05.
- product is null to keep the slot's current product. Name a product only to switch, only one in \
the catalog, and never one whose catalog request was refused.
- What you buy (units added x unit cost, plus $25 for a midweek visit) must fit the balance when it's known.
- visit is "scheduled" (the free Friday visit) or "midweek" ($25). A midweek visit only makes sense \
when the state is taken midweek and the sales it saves are worth more than $25.
- Every slot needs a reason: one short sentence operations could act on without asking.
- A null in what you're given means unknown: never treat it as zero."""

SYSTEM = f"""\
You decide the next restock and prices for the 20 vending-machine slots a student team runs as a \
business. Profit is revenue (ex 8% sales tax) minus what the drinks cost minus $25 for each midweek \
visit, so don't buy what won't sell and don't run out of what does.

The user message is the state of one week as JSON. Return the plan.

{PLAN_RULES}"""

_SLOT = {
    "type": "object",
    "properties": {
        "slot": {"type": "string", "enum": list(SLOTS)},
        "keep": {"type": "boolean"},
        "fill_to": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "sale_price": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "product": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "reason": {"type": "string"},
    },
    "required": ["slot", "keep", "fill_to", "sale_price", "product", "reason"],
    "additionalProperties": False,
}
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "visit": {"type": "string", "enum": ["scheduled", "midweek"]},
        "reason": {"type": "string"},
        "slots": {"type": "array", "items": _SLOT},
    },
    "required": ["visit", "reason", "slots"],
    "additionalProperties": False,
}


class BudgetError(ValueError):
    """The request wasn't made, or failed: the message says why and what it cost."""


def check_settings(model: str, effort: str, key_env: str) -> None:
    if effort not in EFFORTS:
        raise ValueError(f"effort must be one of {', '.join(EFFORTS)}, not {effort!r}.")
    # Windows variable names ignore case, so anthropic_api_key would read ANTHROPIC_API_KEY.
    if key_env.upper().startswith("ANTHROPIC_"):
        raise ValueError(f"Use VENDING_ANTHROPIC_API_KEY, not {key_env}: Claude Code reads ANTHROPIC_* "
                         "variables and the team key would pay for its sessions.")
    prices(model, 0)


def spend_log() -> Path:
    return Path(os.environ.get("VENDING_SPEND_LOG") or Path.home() / ".vending" / "replay-spend.jsonl")


def spent_today(path: Path, now: datetime) -> Decimal:
    """Dollars logged today (Eastern). A request without its closing line counts at its worst case."""
    if not path.exists():
        return Decimal(0)
    today, runs = now.astimezone(EASTERN).date(), {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            at = datetime.fromisoformat(row["at"])
            if at.astimezone(EASTERN).date() == today:
                runs[row["run_id"]] = Decimal(row["cost_usd"])
        except (ValueError, KeyError, TypeError, ArithmeticError):
            raise BudgetError(f"Line {number} of the spend log {path} is unreadable, so today's spend is unknown "
                              "and nothing was sent. Fix or delete that line (a crash mid-write can leave half a "
                              "line), keeping every complete line.") from None
    return sum(runs.values(), Decimal(0))


def prices(model: str, input_tokens: int) -> tuple[Decimal, Decimal]:
    if model not in PRICES:
        raise BudgetError(f"No price for {model}; add it to PRICES in vending/replay/model.py first.")
    if model == "claude-haiku-5-5" and input_tokens > LONG_PROMPT:
        return HAIKU_LONG
    return PRICES[model]


def cost(model: str, usage) -> Decimal:
    """Dollars for one response's usage. Cache writes bill at 1.25x input, reads at 0.1x."""
    written = getattr(usage, "cache_creation_input_tokens", 0) or 0
    read = getattr(usage, "cache_read_input_tokens", 0) or 0
    unit_in, unit_out = prices(model, usage.input_tokens + written + read)
    tokens_in = usage.input_tokens + Decimal("1.25") * written + Decimal("0.1") * read
    return (tokens_in * unit_in + usage.output_tokens * unit_out) / 1_000_000


def _append(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


class _LogLock:
    """An exclusive lock file beside the spend log. Stale after LOCK_WAIT x 2 seconds."""

    def __init__(self, log: Path):
        self.path = log.with_suffix(".lock")

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + LOCK_WAIT
        while True:
            try:
                os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > LOCK_WAIT * 2:
                        self.path.unlink(missing_ok=True)   # left behind by a crash
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() > deadline:
                    raise BudgetError(f"Another run holds {self.path}; nothing was sent. Try again, or delete "
                                      "the file if no run is going.") from None
                time.sleep(0.1)

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)


class Budget:
    """Sends one request at a time under the run and daily caps, logging each one."""

    def __init__(self, model: str, effort: str, key_env: str, base_url: str | None,
                 run_cap: Decimal, daily_cap: Decimal):
        check_settings(model, effort, key_env)
        self.model, self.effort, self.key_env = model, effort, key_env
        self.base_url = base_url or settings.get("VENDING_ANTHROPIC_BASE_URL") or API_URL
        self.run_cap, self.daily_cap = run_cap, daily_cap
        self.runs: list[dict] = []   # every request sent, failed ones included

    def client(self):
        import anthropic

        key = settings.get(self.key_env)
        if not key:
            raise BudgetError(f"{self.key_env} is not set, so no model call was made.")
        return anthropic.Anthropic(api_key=key, base_url=self.base_url, max_retries=0, timeout=300.0)

    def send(self, client, request: dict, *, spent: Decimal = Decimal(0), label: dict | None = None):
        """One request, capped at what's left of the run (after `spent`) and of today.

        Returns (response, dollars). Raises BudgetError if it wasn't sent or failed.
        """
        import anthropic

        log = spend_log()
        request = {**request, "model": self.model}
        try:
            counted = client.messages.count_tokens(**request).input_tokens + COUNT_MARGIN
        except anthropic.APIError as error:
            raise BudgetError(f"Counting the request failed ({_describe(error)}); nothing was sent or spent.") from None
        unit_in, unit_out = prices(self.model, counted)
        run = {"run_id": uuid.uuid4().hex, "model": self.model, "effort": self.effort,
               "key_env": self.key_env, **(label or {})}
        with _LogLock(log):
            allowed = min(self.run_cap - spent, self.daily_cap - spent_today(log, datetime.now(timezone.utc)))
            left = allowed - counted * unit_in / 1_000_000
            max_tokens = min(MAX_TOKENS, int(left * 1_000_000 / unit_out)) if left > 0 else 0
            if max_tokens < MIN_TOKENS:
                raise BudgetError(f"Not sent: only ${max(allowed, Decimal(0)):.4f} of the cap is left "
                                  f"(${self.run_cap} a run, ${self.daily_cap} a day). Nothing was spent.")
            worst = (counted * unit_in + max_tokens * unit_out) / 1_000_000
            run.update(max_tokens=max_tokens, worst_case_usd=f"{worst:.6f}")
            _append(log, {**run, "at": _now(), "status": "sent", "cost_usd": f"{worst:.6f}"})
        try:
            response = client.messages.create(**request, max_tokens=max_tokens)
        except anthropic.APIStatusError as error:
            billed = worst if error.status_code >= 500 else Decimal(0)   # a 4xx is rejected, not billed
            self._close(log, run, f"error {error.status_code}", billed)
            raise BudgetError(f"The model call failed ({_describe(error)}); "
                              + ("counted at its worst case." if billed else "rejected, so not billed.")) from None
        except anthropic.APIError as error:
            self._close(log, run, "error", worst, closed=False)   # timed out: it may have run
            raise BudgetError(f"The model call failed ({_describe(error)}); counted at its worst case, "
                              f"${worst:.4f}.") from None
        paid = cost(self.model, response.usage)
        self._close(log, run, response.stop_reason, paid, request_id=response._request_id,
                    input_tokens=response.usage.input_tokens, output_tokens=response.usage.output_tokens)
        return response, paid

    def _close(self, log: Path, run: dict, status: str, dollars: Decimal, closed: bool = True, **extra) -> None:
        """Record the outcome. `closed=False` keeps the worst-case line as the day's figure."""
        row = {**run, "status": status, "cost_usd": f"{dollars:.6f}", **extra}
        if closed:
            _append(log, {**row, "at": _now()})
        self.runs.append(row)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _describe(error) -> str:
    status = getattr(error, "status_code", None)
    return f"{type(error).__name__}{f' {status}' if status else ''}"


def strip_nulls(plan):
    """The schema needs every key, so null stands for "left out": drop it before checking."""
    if isinstance(plan, dict) and isinstance(plan.get("slots"), list):
        plan["slots"] = [{k: v for k, v in row.items() if v is not None} if isinstance(row, dict) else row
                         for row in plan["slots"]]
    return plan


def state_json(state: WeekState) -> dict:
    """The week as the model sees it. Unknowns stay null."""
    def dollars(cents):
        if cents is None:
            return None
        value = Decimal(Fraction(cents).numerator) / Decimal(Fraction(cents).denominator) / 100
        places = Decimal("0.01") if value == value.quantize(Decimal("0.01")) else Decimal("0.0001")
        return str(value.quantize(places, ROUND_HALF_UP))
    return {
        "week_start": state.start.isoformat(),
        "week_end": state.end.isoformat(),
        "taken": "midweek, before the Friday visit" if state.midweek else "ahead of the Friday visit",
        "balance": dollars(state.balance_cents),
        "catalog": None if state.catalog is None else sorted(state.catalog),
        "refused_catalog_requests": sorted(state.rejected),
        "notes": list(state.notes),
        "slots": [{
            "slot": s.slot,
            "product": s.product,
            "price": dollars(s.price_cents),
            "units_sold": s.units_sold,
            "units_per_day": None if s.units_per_day is None else str(s.units_per_day),
            "price_changed_during_week": s.price_changed,
            "stock_on_rail": s.stock,
            "unit_cost": dollars(s.unit_cost_cents),
        } for s in state.slots],
    }


class ModelDecider:
    uses_database = False   # it sees only the state it's handed

    def __init__(self, model: str = DEFAULT_MODEL, effort: str = DEFAULT_EFFORT,
                 key_env: str = DEFAULT_KEY_ENV, base_url: str | None = None,
                 run_cap: Decimal = RUN_CAP, daily_cap: Decimal = DAILY_CAP):
        self.budget = Budget(model, effort, key_env, base_url, run_cap, daily_cap)
        self.name = f"model:{model}@{effort}"

    # The CLI's cost summary reads these.
    runs = property(lambda self: self.budget.runs)
    key_env = property(lambda self: self.budget.key_env)
    run_cap = property(lambda self: self.budget.run_cap)
    daily_cap = property(lambda self: self.budget.daily_cap)

    def decide(self, state: WeekState) -> dict | str:
        request = {
            "system": SYSTEM,
            "messages": [{"role": "user", "content": "The week:\n" + json.dumps(state_json(state), indent=1)}],
            "output_config": {"effort": self.budget.effort,
                              "format": {"type": "json_schema", "schema": PLAN_SCHEMA}},
        }
        response, _ = self.budget.send(self.budget.client(), request,
                                       label={"kind": "exam", "week_start": state.start.isoformat()})
        if response.stop_reason == "refusal":
            return f"The model declined (refusal, category {getattr(response.stop_details, 'category', None)})."
        text = "".join(b.text for b in response.content if b.type == "text")
        try:
            return strip_nulls(json.loads(text))
        except json.JSONDecodeError:
            return text   # cut off at max_tokens, or not JSON: fails "valid plan"
