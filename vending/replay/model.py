"""A model decider: Claude reads the week and returns a plan, under hard spending caps.

One Messages API call per decision. The answer comes back as JSON in a fixed
shape (structured output) and goes through `check_plan` like any other
decider's, so a bad answer fails "valid plan" instead of crashing the replay.

**Spending is capped before the call, not discovered after it.** The input is
counted first (counting is free). The most the call can cost is then
input x input price + max_tokens x output price, and `max_tokens` (which
includes the model's thinking) is set so that can't exceed the smaller of the
per-run cap and what's left of today's cap. If even a short answer won't fit,
the call isn't made. Retries are off, so a timeout can't bill twice.

**Every run is logged** to a JSON Lines file outside the repo and OneDrive
(`~/.vending/replay-spend.jsonl`, or `VENDING_SPEND_LOG`): a line with the
worst-case cost before the call, then a line with the real cost after. A run
that never got its second line counts at its worst case. The daily cap sums
today's lines (Eastern time) on this computer only; it can't see a teammate's.

The key comes from `VENDING_ANTHROPIC_API_KEY` (the team's key), never
`ANTHROPIC_API_KEY`, which Claude Code would pick up and bill every session to.
`--key-env` names another variable, such as a personal key; the log records
which variable paid.
"""

from __future__ import annotations

import json
import os
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
# Counting covers the system prompt and messages, not the output schema, so allow for it.
SCHEMA_ALLOWANCE = 2_000
MAX_TOKENS = 16_000     # non-streaming ceiling
MIN_TOKENS = 4_000      # below this a plan for 20 slots, with thinking, won't fit

SYSTEM = """\
You decide the next restock and prices for the 20 vending-machine slots a student team runs as a \
business. Profit is revenue (ex 8% sales tax) minus what the drinks cost minus $25 for each midweek \
visit, so don't buy what won't sell and don't run out of what does.

The user message is the state of one week as JSON. A null means unknown: never treat it as zero.

Return a plan with every slot listed once:
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
"""

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
    """The call wasn't made, or failed: the message says why and what it cost."""


def spend_log() -> Path:
    return Path(os.environ.get("VENDING_SPEND_LOG") or Path.home() / ".vending" / "replay-spend.jsonl")


def spent_today(path: Path, now: datetime) -> Decimal:
    """Dollars logged today (Eastern). A run without its closing line counts at its worst case."""
    if not path.exists():
        return Decimal(0)
    today, runs = now.astimezone(EASTERN).date(), {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            if datetime.fromisoformat(row["at"]).astimezone(EASTERN).date() == today:
                runs[row["run_id"]] = Decimal(row["cost_usd"])
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


def state_json(state: WeekState) -> dict:
    """The week as the model sees it. Unknowns stay null."""
    def dollars(cents):
        if cents is None:
            return None
        value = Decimal(Fraction(cents).numerator) / Decimal(Fraction(cents).denominator) / 100
        return str(value.quantize(Decimal("0.01") if value == value.quantize(Decimal("0.01")) else Decimal("0.0001"),
                                  ROUND_HALF_UP))
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
        if effort not in EFFORTS:
            raise ValueError(f"effort must be one of {', '.join(EFFORTS)}, not {effort!r}.")
        if key_env == "ANTHROPIC_API_KEY":
            raise ValueError("Use VENDING_ANTHROPIC_API_KEY, not ANTHROPIC_API_KEY: Claude Code bills to that one.")
        prices(model, 0)   # refuse an unpriced model before anything else
        self.model, self.effort, self.key_env, self.base_url = model, effort, key_env, base_url
        self.run_cap, self.daily_cap = run_cap, daily_cap
        self.name = f"model:{model}@{effort}"
        self.runs: list[dict] = []

    def decide(self, state: WeekState) -> dict | str:
        import anthropic   # only the model decider needs the SDK

        key = settings.get(self.key_env)
        if not key:
            raise BudgetError(f"{self.key_env} is not set, so no model call was made.")
        client = anthropic.Anthropic(api_key=key, base_url=self.base_url, max_retries=0, timeout=300.0)
        messages = [{"role": "user", "content": "The week:\n" + json.dumps(state_json(state), indent=1)}]

        log, now = spend_log(), datetime.now(timezone.utc)
        allowed = min(self.run_cap, self.daily_cap - spent_today(log, now))
        counted = client.messages.count_tokens(model=self.model, system=SYSTEM, messages=messages).input_tokens
        tokens_in = counted + SCHEMA_ALLOWANCE
        unit_in, unit_out = prices(self.model, tokens_in)
        left = allowed - tokens_in * unit_in / 1_000_000
        max_tokens = min(MAX_TOKENS, int(left * 1_000_000 / unit_out)) if left > 0 else 0
        if max_tokens < MIN_TOKENS:
            raise BudgetError(f"Not sent: only ${max(allowed, Decimal(0)):.4f} of the cap is left "
                              f"(${self.run_cap} a run, ${self.daily_cap} a day). Nothing was spent.")
        worst = (tokens_in * unit_in + max_tokens * unit_out) / 1_000_000

        run = {"run_id": uuid.uuid4().hex, "model": self.model, "effort": self.effort, "key_env": self.key_env,
               "week_start": state.start.isoformat(), "max_tokens": max_tokens,
               "worst_case_usd": f"{worst:.6f}"}
        _append(log, {**run, "at": now.isoformat(), "status": "sent", "cost_usd": f"{worst:.6f}"})
        try:
            response = client.messages.create(
                model=self.model, max_tokens=max_tokens, system=SYSTEM, messages=messages,
                output_config={"effort": self.effort,
                               "format": {"type": "json_schema", "schema": PLAN_SCHEMA}},
            )
        except anthropic.APIError as error:
            # Unknown whether it was billed: it stays at its worst case in the log.
            raise BudgetError(f"The model call failed ({type(error).__name__}); counted at its worst case, "
                              f"${worst:.4f}, in {log}.") from None
        spent = cost(self.model, response.usage)
        run.update(status=response.stop_reason, cost_usd=f"{spent:.6f}", request_id=response._request_id,
                   input_tokens=response.usage.input_tokens, output_tokens=response.usage.output_tokens)
        _append(log, {**run, "at": datetime.now(timezone.utc).isoformat()})
        self.runs.append(run)

        if response.stop_reason == "refusal":
            category = getattr(response.stop_details, "category", None)
            return f"The model declined (refusal, category {category})."
        text = "".join(b.text for b in response.content if b.type == "text")
        try:
            answer = json.loads(text)
        except json.JSONDecodeError:
            return text   # cut off at max_tokens, or not JSON: fails "valid plan"
        # The schema needs every key, so null stands for "left out": drop it before checking.
        if isinstance(answer, dict) and isinstance(answer.get("slots"), list):
            answer["slots"] = [{k: v for k, v in row.items() if v is not None} if isinstance(row, dict) else row
                               for row in answer["slots"]]
        return answer


def _append(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
