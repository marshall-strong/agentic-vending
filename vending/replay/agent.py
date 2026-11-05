"""The agent replay: Claude runs several past weeks in a row, with tools, notes and a weekly update.

Closer to the live agent than the one-question exam in `model.py`. Each week:

1. It's the end of the week, before the Friday restock. Claude gets its own notes
   from last week and the agent's read tools, which only see polls up to that
   moment, so it can't peek at the weeks it's being tested on.
2. It looks up what it wants, then answers with three things: the plan for every
   slot, a short **weekly update** for the team (what happened, what it's
   changing and why), and **notes** to itself for next week.
3. The plan is checked and scored against what actually sold the week after,
   like any decider's. The updates are saved to a file for people to grade.

The tools are #14's `get_machine_state` and `get_sales_history`, with the same
names and replies, cut off at the replay date. The rest are still stubs on #14,
so Claude is told they aren't built, as the live agent would be.

Every request goes through the exam's `Budget`: $1 for a week's whole
conversation, $4 a day across everything, the worst case worked out and
reserved before each request, every request logged. A week that runs out of
budget part-way ends with no plan, which fails "valid plan", and the replay stops.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import psycopg

from vending.replay import model
from vending.replay.model import Budget, BudgetError
from vending.replay.scoring import Score, score
from vending.replay.state import EASTERN, NoData, WeekState, replay_week
from vending.slots import SLOTS
from vending.tools import machine_state as ms
from vending.tools.sales_history import get_sales_history

MAX_TURNS = 10   # tool rounds per week; on the last one it must answer

SYSTEM = f"""\
You run the 20 vending-machine slots a student team operates as a business, one week at a time. \
Profit is revenue (ex 8% sales tax) minus what the drinks cost minus $25 for each midweek visit, \
so don't buy what won't sell and don't run out of what does.

It is the end of a week, just before the Friday restock. You have read tools that see the \
machine's records up to now and no further. You also get the notes you wrote yourself last week. \
Look up what you need, then answer with three things:

1. plan: the restock plan, following the rules below.
2. update: a short weekly update for the team, under 150 words: what happened last week, what you \
are changing and why. Plain language; someone running operations should act on it without asking.
3. notes: what you want to remember next week, under 200 words: what you're watching, what you \
tried, what you'd do differently.

{model.PLAN_RULES}"""

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "plan": model.PLAN_SCHEMA,
        "update": {"type": "string"},
        "notes": {"type": "string"},
    },
    "required": ["plan", "update", "notes"],
    "additionalProperties": False,
}

TOOLS = [
    {
        "name": "get_machine_state",
        "description": "What each of our 20 slots charges and how fast it sold over the last 7 days, "
                       "from stored polls. Product and stock need receipts, so they're null.",
        "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "strict": True,
    },
    {
        "name": "get_sales_history",
        "description": "Units and revenue per slot between two times (ISO 8601, UTC if no offset). "
                       "slots null means all 20.",
        "input_schema": {
            "type": "object",
            "properties": {
                "start": {"type": "string"},
                "end": {"type": "string"},
                "slots": {"anyOf": [{"type": "array", "items": {"type": "string", "enum": list(SLOTS)}},
                                    {"type": "null"}]},
            },
            "required": ["start", "end", "slots"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]
NOT_BUILT = ("These tools aren't built yet, so you don't have them: get_catalog, get_account_balance, "
             "preview_desired_state, submit_desired_state, get_purchase_requests, set_price, get_pnl.")


# --- The tools, cut off at the replay date ------------------------------------

def machine_state_as_of(conn: psycopg.Connection, as_of: datetime) -> ms.MachineState:
    """`get_machine_state` as it would have answered at `as_of`: the same build, on polls up to then."""
    latest = ms._poll(conn, "started_at <= %s", as_of)
    if latest is None:
        return ms.MachineState(success=False, message="No polls are stored before this date.")
    baseline = ms._poll(conn, "started_at <= %s", latest[1] - ms.WINDOW) or ms._first_poll(conn)
    if baseline and baseline[0] == latest[0]:
        baseline = None
    states = ms.build(latest[1], ms._counters(conn, latest[0]), baseline[1] if baseline else None,
                      ms._counters(conn, baseline[0]) if baseline else {})
    window = f"since {baseline[1]:%Y-%m-%d %H:%M} UTC" if baseline else "unavailable (only one poll stored)"
    return ms.MachineState(
        success=True, count=len(states), as_of=latest[1], window_start=baseline[1] if baseline else None,
        message=f"{len(states)} slots as of the poll at {latest[1]:%Y-%m-%d %H:%M} UTC; sales window {window}. "
                f"{ms.NEEDS_RECEIPTS}.",
        data=states)


def run_tool(conn: psycopg.Connection, as_of: datetime, name: str, arguments: dict) -> tuple[str, bool]:
    """(JSON reply, is_error). A window after `as_of` is cut at `as_of`; one starting after it is refused."""
    try:
        if name == "get_machine_state":
            reply = machine_state_as_of(conn, as_of)
        elif name == "get_sales_history":
            start, end = (datetime.fromisoformat(arguments[k]) for k in ("start", "end"))
            start, end = (t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t for t in (start, end))
            if start >= as_of:
                return json.dumps({"success": False, "message": f"That starts after now ({as_of.isoformat()})."}), True
            reply = get_sales_history(conn, start, min(end, as_of), arguments.get("slots"))
        else:
            return json.dumps({"success": False, "message": f"There is no tool called {name}."}), True
    except (KeyError, ValueError, TypeError) as error:
        return json.dumps({"success": False, "message": f"Bad arguments: {error}"}), True
    return reply.model_dump_json(), not reply.success


# --- The weeks ------------------------------------------------------------------

@dataclass
class Week:
    day: date
    state: WeekState | None
    next_week: WeekState | None
    result: Score | None = None
    update: str = ""
    notes: str = ""
    turns: int = 0
    tool_calls: list[str] = field(default_factory=list)
    cost_usd: Decimal = Decimal(0)
    error: str = ""


class AgentReplay:
    def __init__(self, model_id: str = model.DEFAULT_MODEL, effort: str = model.DEFAULT_EFFORT,
                 key_env: str = model.DEFAULT_KEY_ENV, base_url: str | None = None,
                 week_cap: Decimal = model.RUN_CAP, daily_cap: Decimal = model.DAILY_CAP):
        self.budget = Budget(model_id, effort, key_env, base_url, week_cap, daily_cap)
        self.name = f"agent:{model_id}@{effort}"
        self.cap_unit = "week"   # the run cap covers one week's whole conversation

    # The CLI's cost summary reads these.
    runs = property(lambda self: self.budget.runs)
    key_env = property(lambda self: self.budget.key_env)
    run_cap = property(lambda self: self.budget.run_cap)
    daily_cap = property(lambda self: self.budget.daily_cap)

    def run(self, conn: psycopg.Connection, first: date, weeks: int, *,
            unit_cost_cents: int | None = None, balance_cents: int | None = None) -> list[Week]:
        client = self.budget.client()
        notes, out = "(This is the first week: no notes yet.)", []
        for i in range(weeks):
            day = first + timedelta(days=7 * i)
            try:
                state = replay_week(conn, day)
            except NoData as error:
                out.append(Week(day, None, None, error=str(error)))
                break
            try:
                next_week = replay_week(conn, day + timedelta(days=7))
            except NoData:
                next_week = None
            if unit_cost_cents is not None:
                state = replace(state, slots=tuple(replace(s, unit_cost_cents=unit_cost_cents) for s in state.slots))
            if balance_cents is not None:
                state = replace(state, balance_cents=balance_cents)
            week = Week(day, state, next_week)
            out.append(week)
            try:
                answer = self._week(client, conn, week, notes, unit_cost_cents, balance_cents)
            except Exception as error:   # a database or other failure: keep the weeks already paid for
                week.error = f"Stopped by an error: {type(error).__name__}: {error}"
                answer = week.error
            plan = answer.get("plan") if isinstance(answer, dict) else answer
            week.result = score(state, model.strip_nulls(plan), next_week)
            if isinstance(answer, dict):
                week.update, week.notes = str(answer.get("update", "")), str(answer.get("notes", ""))
                notes = week.notes or "(You left no notes last week.)"
            if week.error:
                # Out of budget, a failed request (retries are off, so a rate limit too), or an error:
                # later weeks would fail the same way, so the replay stops here.
                break
        return out

    def _week(self, client, conn, week: Week, notes: str, unit_cost, balance) -> dict | str:
        as_of = week.state.end
        assumed = []
        if unit_cost is not None:
            assumed.append(f"every unit costs ${unit_cost // 100}.{unit_cost % 100:02d}")
        if balance is not None:
            assumed.append(f"the spendable balance is ${balance // 100}.{balance % 100:02d}")
        facts = (f"Now: {as_of.astimezone(EASTERN):%A %d %B %Y %H:%M} Eastern. The restock is this Friday afternoon.\n"
                 f"For this test, assume {'; '.join(assumed) or 'nothing about unit cost or balance: both are unknown'}.\n"
                 f"{NOT_BUILT}\n\nYour notes from last week:\n{notes}")
        messages = [{"role": "user", "content": facts}]
        # A fresh id per week, so its cost can't mix with another run's week of the same date.
        label = {"kind": "agent-week", "week_start": week.state.start.isoformat(), "week_run": uuid.uuid4().hex}
        for turn in range(MAX_TURNS + 1):
            # The tools stay defined (the history holds tool calls); on the last turn none may be called.
            request = {"system": SYSTEM, "messages": messages, "tools": TOOLS,
                       "tool_choice": {"type": "auto" if turn < MAX_TURNS else "none"},
                       "output_config": {"effort": self.budget.effort,
                                         "format": {"type": "json_schema", "schema": ANSWER_SCHEMA}}}
            try:
                response, _ = self.budget.send(client, request, spent=week.cost_usd,
                                               label={**label, "turn": turn + 1})
            except BudgetError as error:
                week.error = str(error)
                return week.error
            finally:
                # Everything sent for this week, failed requests at what they were booked at.
                sent = [r for r in self.budget.runs if r.get("week_run") == label["week_run"]]
                week.turns = len(sent)
                week.cost_usd = sum((Decimal(r["cost_usd"]) for r in sent), Decimal(0))
            # Append the whole content, thinking blocks included: the history only ever grows.
            messages.append({"role": "assistant", "content": response.content})
            if response.stop_reason == "tool_use":
                results = []
                for block in response.content:
                    if block.type == "tool_use":
                        week.tool_calls.append(block.name)
                        text, failed = run_tool(conn, as_of, block.name, block.input)
                        results.append({"type": "tool_result", "tool_use_id": block.id, "content": text,
                                        "is_error": failed})
                messages.append({"role": "user", "content": results})
                continue
            if response.stop_reason == "refusal":
                return f"The model declined (refusal, category {getattr(response.stop_details, 'category', None)})."
            text = "".join(b.text for b in response.content if b.type == "text")
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text   # cut off, or not JSON: fails "valid plan"
        return "Ran out of turns without answering."


def save_updates(weeks: list[Week], name: str, folder: Path | None = None) -> Path:
    """The weekly updates and notes, as one Markdown file for people to grade."""
    folder = folder or model.spend_log().parent / "agent-runs"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(EASTERN).strftime("%Y-%m-%d-%H%M%S")
    path = folder / f"{stamp}-{name.replace(':', '-').replace('@', '-')}-{uuid.uuid4().hex[:6]}.md"
    parts = [f"# Agent replay: {name}\n",
             "For each week, grade the update: is the reasoning sound, and could operations act on it "
             "without asking?\n"]
    for w in weeks:
        parts.append(f"## Week of {w.day:%d %b %Y}\n")
        if w.error:
            parts.append(f"**Stopped:** {w.error}\n")
        parts.append(f"**Weekly update**\n\n{w.update or '(none)'}\n")
        parts.append(f"**Notes for next week**\n\n{w.notes or '(none)'}\n")
        parts.append(f"Tools used: {', '.join(w.tool_calls) or 'none'}. Requests: {w.turns}. "
                     f"Cost: ${w.cost_usd:.4f}.\n")
    path.write_text("\n".join(parts), encoding="utf-8")
    return path
