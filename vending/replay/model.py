"""A model decider: Claude reads the week and returns a plan, under hard spending caps.

One Messages API call per decision. The answer comes back as JSON in a fixed
shape (structured output) and goes through `check_plan` like any other
decider's, so a bad answer fails "valid plan" instead of crashing the replay.

**Every request goes through `vending.harness` as a `test` call** (`Budget`):

- It is counted first (free), its worst case is reserved under the run cap
  ($1 a decision) and today's test cap (the team's `model_test_daily_cap_cents`,
  $20 by default), and `max_tokens` is lowered to fit. If even a short answer
  won't fit, nothing is sent.
- Retries are off, so a timeout can't bill twice.
- **It is a row in `model_calls`**, with its model, effort, tokens, cost,
  latency and which key paid, so every use of the key shows on the dashboard
  and in `python -m vending.usage`. Test calls never reach the ledger: they
  aren't the competition's $600. A request the API rejected (a 4xx) stays at
  $0; one that timed out, failed on a 5xx or crashed stays at its worst case.
- So the replay needs the database even for `--cases`: spend we can't record
  isn't spent.

The key comes from `VENDING_ANTHROPIC_API_KEY` (the team's key) and never from
an `ANTHROPIC_*` variable, which Claude Code reads and would bill. Requests go to
api.anthropic.com unless `VENDING_ANTHROPIC_BASE_URL` says otherwise.
"""

from __future__ import annotations

import json
import uuid
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction

from vending import config, db, harness, settings
from vending.replay.state import EASTERN, WeekState
from vending.slots import SLOTS

DEFAULT_MODEL = "claude-opus-5-5"   # pending a group decision
DEFAULT_EFFORT = "medium"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_KEY_ENV = "VENDING_ANTHROPIC_API_KEY"
API_URL = "https://api.anthropic.com"

RUN_CAP = Decimal("1.00")   # per decision; the daily cap is the team's model_test_daily_cap_cents

MAX_TOKENS = 16_000     # non-streaming ceiling
MIN_TOKENS = 4_000      # below this a plan for 20 slots, with thinking, won't fit

PLAN_RULES = """\
The plan lists every slot once:
- keep: true means no change and nothing loaded. Then fill_to, sale_price and product are null.
- Otherwise fill_to is the units on the rail after the visit (0 to 50) and sale_price is the shelf \
price including tax, as text like "2.50": a multiple of $0.05, at most $6.50, and when the unit cost \
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
    try:
        harness.load_prices().rates(model, 0)
    except harness.HarnessError as error:
        raise BudgetError(str(error)) from None


def worst_case(model: str, prompt_tokens: int) -> Decimal:
    """The most one request with this many prompt tokens can bill, with a full answer."""
    return harness.worst_case_usd(model, prompt_tokens, MAX_TOKENS, harness.load_prices(), caches=None)


class Budget:
    """Sends one request at a time through the harness, as a `test` call, under the run and daily caps.

    Every request is a row in `model_calls`, so it shows on the dashboard and in
    `python -m vending.usage`. Test calls never reach the ledger: they aren't
    the competition's $600. `daily_cap` None means the team's
    `model_test_daily_cap_cents` setting.
    """

    def __init__(self, model: str, effort: str, key_env: str, base_url: str | None,
                 run_cap: Decimal, daily_cap: Decimal | None = None, connect=None):
        check_settings(model, effort, key_env)
        self.model, self.effort, self.key_env = model, effort, key_env
        self.base_url = base_url or settings.get("VENDING_ANTHROPIC_BASE_URL") or API_URL
        self.run_cap, self._daily_cap = run_cap, daily_cap
        self._daily_cap_used: Decimal | None = None   # what the harness last applied
        self.connect = connect or db.connect
        self.runs: list[dict] = []   # every request sent, failed ones included

    @property
    def daily_cap(self) -> Decimal | None:
        """The daily cap: the one given, else the one the last request ran under, else None (not read yet).

        Never reads the database, so printing it after paid calls can't fail.
        """
        return self._daily_cap if self._daily_cap is not None else self._daily_cap_used

    def client(self):
        import anthropic

        key = (settings.get(self.key_env) or "").strip()
        if not key:
            raise BudgetError(f"{self.key_env} is not set, so no model call was made.")
        if any(ord(ch) < 33 or ord(ch) > 126 for ch in key):
            raise BudgetError(f"{self.key_env} holds spaces or unusual characters inside the key; set it again. "
                              "No model call was made.")
        return anthropic.Anthropic(api_key=key, base_url=self.base_url, max_retries=0, timeout=300.0)

    def send(self, client, request: dict, *, spent: Decimal = Decimal(0), label: dict | None = None):
        """One request, capped at what's left of the run (after `spent`) and of today's test cap.

        Returns (response, dollars). Raises BudgetError if it wasn't sent or failed.
        """
        label = dict(label or {})
        run_id = label.get("week_run") or label.get("run_id") or uuid.uuid4().hex
        row = {"run_id": run_id, "model": self.model, "effort": self.effort, "key_env": self.key_env, **label}
        try:
            h = harness.Harness(client, self.connect, purpose="test", run_cap_usd=self.run_cap - spent,
                                daily_cap_usd=self._daily_cap, min_tokens=MIN_TOKENS, by="replay",
                                key_env=self.key_env, run_id=run_id)
        except harness.HarnessError as error:
            raise BudgetError(str(error)) from None
        except Exception as error:   # no database: nothing can be recorded, so nothing is sent
            raise BudgetError(f"The usage records couldn't be opened ({type(error).__name__}), so nothing was "
                              "sent. Start the database and run `python -m vending.db migrate`.") from None
        self._daily_cap_used = h.daily_cap
        words = {**label, "week_start": str(label.get("week_start", ""))[:10]}   # the date is enough
        text = " ".join(str(words[k]) for k in ("kind", "config", "case", "week_start", "turn") if words.get(k))
        try:
            call = h.call_detailed({**request, "model": self.model, "max_tokens": MAX_TOKENS}, label=text)
        except harness.HarnessError as error:
            if h.reserved:   # it was sent (or may have been): refused at $0, or failed at its worst case
                self.runs.append({**row, "status": "refused" if h.spent == 0 else "error",
                                  "cost": h.spent, "cost_usd": f"{h.spent:.6f}"})
            raise BudgetError(_sentence(str(error))) from None
        except Exception:
            # The request may have run; the harness kept its worst case in model_calls.
            self.runs.append({**row, "status": "error", "cost": h.spent, "cost_usd": f"{h.spent:.6f}"})
            raise
        response, usage = call.response, call.response.usage
        # `cost` is exact, for caps and sums; `cost_usd` is for display only.
        self.runs.append({**row, "status": response.stop_reason, "cost": call.cost_usd,
                          "cost_usd": f"{call.cost_usd:.6f}",
                          "call_id": call.call_id, "request_id": getattr(response, "_request_id", None),
                          "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
                          "max_tokens": call.max_tokens, "seconds": call.seconds})
        return response, call.cost_usd


def _sentence(text: str) -> str:
    text = text[:1].upper() + text[1:]
    return text if text.endswith(".") else text + "."


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
                 run_cap: Decimal = RUN_CAP, daily_cap: Decimal | None = None, connect=None):
        self.budget = Budget(model, effort, key_env, base_url, run_cap, daily_cap, connect)
        self.name = f"model:{model}@{effort}"
        # Extra words for each request's model_calls row, such as the sweep config and case.
        self.tag: dict = {}

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
                                       label={"kind": "exam", "week_start": state.start.isoformat(), **self.tag})
        if response.stop_reason == "refusal":
            return f"The model declined (refusal, category {getattr(response.stop_details, 'category', None)})."
        text = "".join(b.text for b in response.content if b.type == "text")
        try:
            return strip_nulls(json.loads(text))
        except json.JSONDecodeError:
            return text   # cut off at max_tokens, or not JSON: fails "valid plan"
