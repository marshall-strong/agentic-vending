"""The judge: a model grades every plan for a case against the case's good answer.

Hard checks and outcome numbers can't say whether a plan made the right call:
on the first sweep every model passed nearly every check, and margin separated
them by a few percent. So a model grades each plan against `good_answer`, the
way a person would, on a fixed rubric (`RUBRIC`).

Three things keep the judge honest:

- **Blind.** It sees every plan for a case at once, as P1..Pn in an order fixed
  by a hash (`blind`), never the config's name. The same bar for every plan.
- **A control it must fail.** The baseline answers every case too. On the trap
  cases (`TRAP_CASES`) the baseline is built to make the wrong call, so a judge
  that gives it 2 or more there isn't reading the plans: that case's grades are
  thrown out (`judge_ok` false) and the report says so.
- **One budget.** Each judge request reserves its worst case (at most $1, the
  cap on any one test request) in the bench's purse before it is sent, like the
  answers do, and goes through the harness as
  a `test` call with a `model_calls` row ("bench <id> judge <case>"). A request
  that won't fit waits for the judge's other requests to settle; one that still
  won't fit isn't sent, and the case is left unjudged, said so.

A judge answer that is malformed, refused or cut off leaves that case unjudged;
it never stops the run.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal

from vending.replay import model
from vending.replay.cases import Case
from vending.replay.deciders import Plan
from vending.replay.model import Budget, BudgetError, state_json
from vending.replay.sweep import Purse
from vending.slots import SLOTS

# Cases the baseline (restock to last week's sales, same price, never switch) is built to fail.
# From cases.py's good_answers and the first sweep (11 Nov): it fails the balance check on
# low-balance and the prices check on price-below-floor, runs short on tuesday-sellout and
# midweek-visit (no visit, plans at the sold-out count), and leaves the dead product in slot 15.
TRAP_CASES = frozenset({"slow-seller", "midweek-visit", "tuesday-sellout", "low-balance", "price-below-floor"})
CONTROL_FAILS_AT = 2   # a control decision this high on a trap case means the judge isn't reading

RUBRIC = """\
Grade every plan against the case's good answer, on the same bar for all of them. Read the slots \
the case is about first, then the rest for harm (overspending, wrong prices, products that \
aren't allowed, slots emptied or overfilled for no reason).

decision (0-3), the call the plan makes:
  3 = makes exactly the call the good answer describes.
  2 = the right call, with a flaw (a weak number, an unneeded change elsewhere).
  1 = partly right: half the call, or the right direction for the wrong reason.
  0 = the wrong call.
reasoning (0-2), the reasons it gives:
  2 = true to the numbers given, and an operations person could act on them without asking.
  1 = mostly: a slip or a vague slot, but sound overall.
  0 = vague, wrong, or contradicts the plan itself.
invented_numbers: true if the plan treats an unknown (null) as a number, or states a figure that \
isn't in the week or worked out from it.
why: two or three plain sentences a teammate can check against the plan.

Each plan comes with its automatic check results (pass, fail or skip). A failed check is a real \
rule broken; weigh it. Plans are labelled P1, P2, ... in no meaningful order: grade each on its \
own merits, never by its label or its position."""

SYSTEM = f"""\
You grade restock-and-price plans for a student team's 20-slot vending machine. Profit is revenue \
(ex 8% sales tax) minus what the drinks cost minus $25 for each midweek visit. The user message \
holds one test case: the question, the good answer, the week as the planner saw it (a null means \
unknown), what each slot actually sold the week after, and every plan to grade.

{RUBRIC}

Return a grade for every plan, each label exactly once, and a one-sentence case_note on what \
separated the good plans from the weak ones."""


def grades_schema(labels: list[str]) -> dict:
    """The answer's shape. Ranges are checked in `validate`: the schema only fixes the keys and labels."""
    grade = {
        "type": "object",
        "properties": {
            "label": {"type": "string", "enum": labels},
            "decision": {"type": "integer"},
            "reasoning": {"type": "integer"},
            "invented_numbers": {"type": "boolean"},
            "why": {"type": "string"},
        },
        "required": ["label", "decision", "reasoning", "invented_numbers", "why"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {"grades": {"type": "array", "items": grade}, "case_note": {"type": "string"}},
        "required": ["grades", "case_note"],
        "additionalProperties": False,
    }


def judge_hash() -> str:
    """The yardstick's version: a changed rubric, prompt or schema makes scores incomparable."""
    text = SYSTEM + json.dumps(grades_schema(["P1"]), sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def plan_json(plan: Plan) -> dict:
    """A `Plan` (the baseline's) in the JSON shape a model answers in, so the judge reads them alike."""
    def price(cents):
        return None if cents is None else f"{cents // 100}.{cents % 100:02d}"
    return {"visit": plan.visit, "reason": plan.reason,
            "slots": [{"slot": s.slot, "keep": s.keep, "fill_to": s.fill_to, "sale_price": price(s.price_cents),
                       "product": s.product, "reason": s.reason} for s in plan.slots]}


@dataclass
class Entry:
    """One plan to grade: whose it is (hidden from the judge), the plan and its checks."""
    config: str
    repeat: int
    plan: object
    checks: dict


def blind(case: str, entries: list[Entry]) -> dict[str, Entry]:
    """{"P1": entry, ...} in an order fixed by sha256(case|config|repeat): no randomness, no config order."""
    key = lambda e: hashlib.sha256(f"{case}|{e.config}|{e.repeat}".encode("utf-8")).hexdigest()
    return {f"P{i}": e for i, e in enumerate(sorted(entries, key=key), start=1)}


def sold_next_week(c: Case) -> dict[str, int | None] | None:
    if c.next_week is None:
        return None
    return {s: (None if c.next_week.slot(s) is None else c.next_week.slot(s).units_sold) for s in SLOTS}


def prompt(c: Case, labelled: dict[str, Entry]) -> str:
    body = {
        "case": c.name,
        "question": c.question,
        "good_answer": c.good_answer,
        "week": state_json(c.state),
        "units_sold_next_week": sold_next_week(c),
        "plans": [{"label": label, "checks": e.checks, "plan": e.plan} for label, e in labelled.items()],
    }
    return json.dumps(body, indent=1)


class JudgeError(ValueError):
    """The judge's answer can't be used: the message says why."""


@dataclass(frozen=True)
class Grade:
    decision: int
    reasoning: int
    invented: bool
    why: str


def validate(answer: object, labels: list[str]) -> tuple[dict[str, Grade], str]:
    """({label: Grade}, case_note), or JudgeError. Every label exactly once, every number in range."""
    if not isinstance(answer, dict) or not isinstance(answer.get("grades"), list):
        raise JudgeError("the answer has no list of grades")
    out: dict[str, Grade] = {}
    for g in answer["grades"]:
        if not isinstance(g, dict):
            raise JudgeError("a grade isn't an object")
        label = g.get("label")
        if label not in labels:
            raise JudgeError(f"it graded {label!r}, which isn't one of the plans")
        if label in out:
            raise JudgeError(f"it graded {label} twice")
        d, r, inv, why = g.get("decision"), g.get("reasoning"), g.get("invented_numbers"), g.get("why")
        if not _int_in(d, 0, 3) or not _int_in(r, 0, 2):
            raise JudgeError(f"{label}: decision must be 0-3 and reasoning 0-2, not {d!r} and {r!r}")
        if not isinstance(inv, bool) or not isinstance(why, str):
            raise JudgeError(f"{label}: invented_numbers must be true or false and why text")
        out[label] = Grade(d, r, inv, why.strip())
    missing = [lab for lab in labels if lab not in out]
    if missing:
        raise JudgeError(f"it didn't grade {', '.join(missing)}")
    note = answer.get("case_note")
    return out, note.strip() if isinstance(note, str) else ""


def _int_in(value, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


@dataclass
class Verdict:
    """What the judge made of one case. `grades` is empty when `problem` says why it couldn't judge."""
    case: str
    grades: dict[tuple[str, int], Grade] = field(default_factory=dict)   # (config, repeat) -> Grade
    note: str = ""
    problem: str = ""
    control_failed: bool = False
    cost: Decimal = Decimal(0)
    seconds: float = 0.0


def control_failed(case: str, grades: dict[tuple[str, int], Grade], control: str = "baseline") -> bool:
    """True if the judge passed the baseline on a case the baseline is built to fail."""
    g = grades.get((control, 1))
    return case in TRAP_CASES and g is not None and g.decision >= CONTROL_FAILS_AT


# A judge prompt is mostly JSON: count it at two characters a token, well above the real rate,
# so the purse's hold covers it. The harness counts exactly and never sends past the hold.
CHARS_PER_TOKEN = 2
# For the dry run's estimate, before any plan exists: a week plus about this much per plan.
WEEK_TOKENS, PLAN_TOKENS = 4_000, 2_500


def estimate(judge_model: str, n_plans: int) -> Decimal:
    """The most one judge request on `n_plans` plans can bill, before the plans exist: at most $1."""
    return min(model.worst_case(judge_model, WEEK_TOKENS + PLAN_TOKENS * n_plans), model.RUN_CAP)


class Judge:
    """Grades one case per request, each through its own `Budget`, holding its worst case in `purse`."""

    def __init__(self, judge_model: str, effort: str, purse: Purse, run_id: int,
                 key_env: str = model.DEFAULT_KEY_ENV, connect=None):
        model.check_settings(judge_model, effort, key_env)
        self.model, self.effort, self.purse, self.run_id = judge_model, effort, purse, run_id
        self.key_env, self.connect = key_env, connect
        self._settled = threading.Condition()
        self._in_flight = 0

    def _hold(self, amount: Decimal) -> bool:
        """Reserve `amount` in the purse, waiting while other judge requests are in flight.

        Each request holds its full worst case but usually spends a fraction of it.
        Refusing at once would drop cases that fit after the requests ahead of them
        settle, so a request waits for those, and gives up only when nothing of
        this judge's is in flight and it still doesn't fit.
        """
        with self._settled:
            while not self.purse.reserve(amount):
                if self._in_flight == 0:
                    return False
                self._settled.wait()
            self._in_flight += 1
            return True

    def _release(self, amount: Decimal, paid: Decimal) -> None:
        with self._settled:
            self.purse.settle(amount, paid)
            self._in_flight -= 1
            self._settled.notify_all()

    def grade(self, c: Case, entries: list[Entry]) -> Verdict:
        verdict = Verdict(c.name)
        labelled = blind(c.name, entries)
        labels = list(labelled)
        text = prompt(c, labelled)
        request = {
            "system": SYSTEM,
            "messages": [{"role": "user", "content": text}],
            "output_config": {"effort": self.effort,
                              "format": {"type": "json_schema", "schema": grades_schema(labels)}},
        }
        # At most $1 a request, like every test call (`model.RUN_CAP`): past that the harness shortens the
        # answer to fit, or sends nothing. The harness may spend no more than the purse holds for this request.
        reserve = min(model.worst_case(self.model, len(SYSTEM + text) // CHARS_PER_TOKEN), model.RUN_CAP)
        budget = Budget(self.model, self.effort, self.key_env, None, run_cap=reserve, connect=self.connect)
        if not self._hold(reserve):
            verdict.problem = f"not judged: the bench budget couldn't hold the judge's worst case (${reserve:.4f})"
            return verdict
        start = time.monotonic()
        try:
            response, _ = budget.send(budget.client(), request, label={
                "kind": f"bench {self.run_id} judge", "case": c.name, "run_id": f"bench-{self.run_id}-judge"})
            answer = _parse(response)
            graded, verdict.note = validate(answer, labels)
            verdict.grades = {(labelled[lab].config, labelled[lab].repeat): g for lab, g in graded.items()}
            verdict.control_failed = control_failed(c.name, verdict.grades)
        except BudgetError as error:
            verdict.problem = f"not judged: {error}"
        except JudgeError as error:
            verdict.problem = f"the judge's answer was unusable: {error}"
        except Exception as error:   # one case's failure mustn't stop the run
            verdict.problem = f"not judged: {type(error).__name__}: {error}"
        finally:
            verdict.seconds = round(time.monotonic() - start, 1)
            verdict.cost = sum((r["cost"] for r in budget.runs), Decimal(0))
            self._release(reserve, verdict.cost)
        return verdict


def _parse(response) -> object:
    if response.stop_reason == "refusal":
        raise JudgeError("the judge declined (refusal)")
    text = "".join(b.text for b in response.content if b.type == "text")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        cut = " (cut off at max_tokens)" if response.stop_reason == "max_tokens" else ""
        raise JudgeError(f"the answer wasn't JSON{cut}") from None
