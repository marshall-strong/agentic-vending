"""The model sweep: every model x effort on the team's test cases, under one dollar budget.

    python -m vending.replay.sweep --dry-run
    python -m vending.replay.sweep --budget 15
    python -m vending.replay.sweep --models haiku,sonnet --efforts low,medium --repeats 3 --budget 5
    python -m vending.replay.sweep --configs haiku@medium,sonnet@high,opus@low --budget 5

Each (model, effort, repeat) is a "config": every model with every effort, or
just the pairs given with `--configs`. Each config answers every test case
once through the exam's `ModelDecider`, so every request goes through the
harness as a `test` call: counted first, worst case reserved, $1 a request,
the team's daily test cap (`model_test_daily_cap_cents`), and **a row in
`model_calls`** labelled with the sweep, config and case, so the whole sweep
shows on the dashboard and in `python -m vending.usage`. On top of that:

- **The sweep has its own budget** (`--budget`). Before each request it reserves
  that request's worst case (`worst_case`) against what the sweep has spent and
  reserved so far. A request that won't fit isn't sent, and the summary lists
  every one that was dropped, so a short sweep never reads as a full one.
- **Cheapest configs run first**, so a budget that runs out cuts the dearest ones.
- **Nothing is lost on a crash.** Every result is appended to `results.jsonl` as
  it lands; the plan each model gave is saved beside it for people (or a judge)
  to grade against the case's `good_answer`.

The baseline is scored on the same cases for free, as the bar to clear.

Spends money only with the key set (`VENDING_ANTHROPIC_API_KEY`, never an
`ANTHROPIC_*` variable). `--dry-run` sends nothing and needs no key.
See docs/week-replayer.md, "The model sweep".
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

from vending import config, db, harness, settings
from vending.replay import model
from vending.replay.cases import Case, all_cases
from vending.replay.deciders import Baseline
from vending.replay.model import ModelDecider
from vending.replay.scoring import FAIL, Score, score
from vending.replay.state import EASTERN

ALIASES = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}
DEFAULT_MODELS = ("haiku", "sonnet", "opus")
DEFAULT_EFFORTS = ("low", "medium", "high")
DEFAULT_BUDGET = Decimal("15")
MAX_BUDGET = Decimal("50")          # a typo guard, not a policy: --budget 150 is refused
# A test case's prompt is about 3,000-4,000 tokens; reserve well above that.
PROMPT_ALLOWANCE = 8_000
RESULTS_DIR = settings.REPO_ROOT / "sweep-results"   # gitignored, wherever the command runs


def model_id(name: str) -> str:
    return ALIASES.get(name, name)


def worst_case(model_name: str) -> Decimal:
    """The most one exam request on this model can bill: a generous prompt plus a full answer."""
    return model.worst_case(model_name, PROMPT_ALLOWANCE)


@dataclass(frozen=True)
class Config:
    model: str
    effort: str
    repeat: int

    @property
    def name(self) -> str:
        """The model and effort, without the repeat: what a benchmark compares across runs."""
        short = next((k for k, v in ALIASES.items() if v == self.model), self.model)
        return f"{short}@{self.effort}"

    @property
    def label(self) -> str:
        return self.name + (f"#{self.repeat}" if self.repeat > 1 else "")


def _cheapest_first(out: list[Config]) -> list[Config]:
    """Cheapest model first, so a budget that runs out cuts the dearest."""
    order = {e: i for i, e in enumerate(model.EFFORTS)}
    return sorted(out, key=lambda c: (worst_case(c.model), order.get(c.effort, 99), c.repeat))


def configs(models: list[str], efforts: list[str], repeats: int) -> list[Config]:
    """Every combination, cheapest model first."""
    models = list(dict.fromkeys(model_id(m) for m in models))   # a name twice would overwrite its own plans
    efforts = list(dict.fromkeys(efforts))
    return _cheapest_first([Config(m, e, r) for m in models for e in efforts for r in range(1, repeats + 1)])


def parse_pair(text: str) -> tuple[str, str]:
    """Read "opus@low" as ("claude-opus-5-5", "low"). Raises ValueError on an unknown model or effort."""
    name, at, effort = text.strip().partition("@")
    if not at or not name or not effort:
        raise ValueError(f"A config is MODEL@EFFORT, such as haiku@medium, not {text.strip()!r}.")
    if effort not in model.EFFORTS:
        raise ValueError(f"effort must be one of {', '.join(model.EFFORTS)}, not {effort!r}.")
    model.check_settings(model_id(name), effort, model.DEFAULT_KEY_ENV)   # an unpriced model fails here
    return model_id(name), effort


def pair_configs(spec: str, repeats: int) -> list[Config]:
    """Just these (model, effort) pairs, such as "haiku@medium,sonnet@high,opus@low", cheapest first.

    A benchmark compares a few chosen configs run after run; the full cross product
    would cost three times as much to say the same thing.
    """
    pairs = list(dict.fromkeys(parse_pair(p) for p in spec.split(",") if p.strip()))
    if not pairs:
        raise ValueError("Give at least one config, such as haiku@medium.")
    return _cheapest_first([Config(m, e, r) for m, e in pairs for r in range(1, repeats + 1)])


class Purse:
    """The sweep's own budget: reserve a worst case before a request, settle it after."""

    def __init__(self, budget: Decimal):
        self.budget, self.spent, self.reserved = budget, Decimal(0), Decimal(0)
        self._lock = threading.Lock()

    def reserve(self, amount: Decimal) -> bool:
        with self._lock:
            if self.spent + self.reserved + amount > self.budget:
                return False
            self.reserved += amount
            return True

    def settle(self, reserved: Decimal, paid: Decimal) -> None:
        with self._lock:
            self.reserved -= reserved
            self.spent += paid


@dataclass
class Row:
    config: str
    model: str
    effort: str
    repeat: int
    case: str
    status: str                 # "scored", "dropped" (budget), "error"
    valid: bool | None = None
    failed: list[str] = field(default_factory=list)
    checks: dict[str, str] = field(default_factory=dict)
    scored: int | None = None
    short: int | None = None
    unsold: int | None = None
    margin: float | None = None  # dollars
    cost_usd: float = 0.0
    input_tokens: int | None = None
    output_tokens: int | None = None
    seconds: float | None = None
    max_tokens: int | None = None   # the answer's room after the caps; under model.MAX_TOKENS means it was squeezed
    stop_reason: str | None = None
    detail: str = ""


def _fill(row: Row, result: Score) -> Row:
    row.checks = {c.name: c.status for c in result.checks}
    row.failed = [c.name for c in result.checks if c.status == FAIL]
    row.valid = row.checks.get("valid plan") == "pass"
    t = result.totals
    if t is not None:
        row.scored, row.short, row.unsold = t.scored, t.stockout_units, t.unsold_units
        row.margin = None if t.margin_cents is None else round(float(Fraction(t.margin_cents)) / 100, 2)
    return row


def baseline_rows(cases: list[Case]) -> list[Row]:
    rows = []
    for c in cases:
        result = score(c.state, Baseline().decide(c.state), c.next_week)
        rows.append(_fill(Row("baseline", "baseline", "-", 1, c.name, "scored", seconds=0.0), result))
    return rows


def plan_file(config_label: str, case: str) -> str:
    """Where a config's plan for a case is saved under plans/: "haiku@low#2" -> "haiku_low_r2__case.json"."""
    return f"{config_label.replace('@', '_').replace('#', '_r')}__{case}.json"


class Sweep:
    """Answers every case with every config. `kind` starts each model_calls label: "sweep" or "bench"."""

    def __init__(self, configs: list[Config], cases: list[Case], budget: Decimal, out: Path,
                 workers: int = 4, key_env: str = model.DEFAULT_KEY_ENV, sweep_id: str = "", connect=None,
                 kind: str = "sweep"):
        self.configs, self.cases, self.out, self.workers = configs, cases, out, workers
        self.purse = Purse(budget)
        self.key_env, self.sweep_id, self.connect, self.kind = key_env, sweep_id or out.name, connect, kind
        self.rows: list[Row] = []
        self._write = threading.Lock()

    def run(self, progress=print) -> list[Row]:
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / "plans").mkdir(exist_ok=True)
        for row in baseline_rows(self.cases):
            self._record(row, None)
        for cfg in self.configs:
            decider_for = threading.local()

            def one(c: Case, cfg=cfg):
                # One decider per thread: its `runs` list then belongs to this request alone.
                if not hasattr(decider_for, "d"):
                    decider_for.d = ModelDecider(cfg.model, cfg.effort, self.key_env, connect=self.connect)
                return self._case(cfg, decider_for.d, c)

            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                rows = list(pool.map(one, self.cases))
            done = [r for r in rows if r.status == "scored"]
            progress(f"{cfg.label:<18} {len(done)}/{len(rows)} answered, "
                     f"{sum(bool(r.valid) for r in done)} valid, ${sum(r.cost_usd for r in rows):.4f}; "
                     f"sweep ${self.purse.spent:.4f} of ${self.purse.budget}")
        return self.rows

    def _case(self, cfg: Config, decider: ModelDecider, c: Case) -> Row:
        row = Row(cfg.label, cfg.model, cfg.effort, cfg.repeat, c.name, "dropped")
        reserve = worst_case(cfg.model)
        if not self.purse.reserve(reserve):
            row.detail = f"not sent: the sweep budget couldn't hold its worst case (${reserve:.4f})"
            self._record(row, None)
            return row
        decider.tag = {"kind": f"{self.kind} {self.sweep_id}", "config": cfg.label, "case": c.name,
                       "run_id": f"{self.kind}-{self.sweep_id}-{cfg.label}"}
        # The harness may spend no more than the purse holds for this request: with a longer prompt it
        # shortens the answer, or sends nothing, instead of overrunning the sweep's budget.
        decider.budget.run_cap = reserve
        before = len(decider.runs)
        start = time.monotonic()
        answer, paid = None, Decimal(0)
        try:
            answer = decider.decide(c.state)
            row.status = "scored"
        except ValueError as error:   # BudgetError: not sent, refused, failed or timed out
            text = str(error)
            not_sent = text.startswith("Not sent")
            row.status, row.detail = ("dropped", f"cap: {text}") if not_sent else ("error", text)
        except Exception as error:    # anything else: one case's failure mustn't stop the sweep
            row.status, row.detail = "error", f"{type(error).__name__}: {error}"
        finally:
            row.seconds = round(time.monotonic() - start, 1)
            runs = decider.runs[before:]
            paid = sum((r["cost"] for r in runs), Decimal(0))   # exact, not the display string
            self.purse.settle(reserve, paid)
        row.cost_usd = float(paid)
        if runs:
            last = runs[-1]
            row.stop_reason = last.get("status")
            row.input_tokens, row.output_tokens = last.get("input_tokens"), last.get("output_tokens")
            row.max_tokens = last.get("max_tokens")
        if row.status == "scored":
            _fill(row, score(c.state, answer, c.next_week))
            if not row.valid and isinstance(answer, str):
                row.detail = answer[:300]
        self._record(row, answer)
        return row

    def _record(self, row: Row, answer) -> None:
        with self._write:
            self.rows.append(row)
            with (self.out / "results.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(row.__dict__) + "\n")
            if answer is not None:
                name = plan_file(row.config, row.case)
                body = answer if isinstance(answer, (dict, list)) else {"not_json": str(answer)}
                (self.out / "plans" / name).write_text(json.dumps(body, indent=1), encoding="utf-8")


# --- The summary -------------------------------------------------------------

SUMMARY_FIELDS = ("config", "model", "effort", "cases", "answered", "valid", "all_checks_pass", "errors",
                  "dropped", "squeezed", "compared", "short_units", "unsold_units", "margin", "cost_usd",
                  "cost_per_decision", "avg_output_tokens", "avg_seconds")


def compared_cases(rows: list[Row]) -> list[str]:
    """The cases every config answered with a plan that passed every hard check and could be scored, the baseline included.

    Outcome columns (short, unsold, margin) are summed over these only. Summed over
    whatever each config happened to score, a config whose bad plans dropped out
    of the totals would look better than one that answered every case.
    """
    configs_seen = list(dict.fromkeys(r.config for r in rows))
    cases = list(dict.fromkeys(r.case for r in rows))
    # A plan that broke a hard check (overspent, priced below the floor) earns no outcome credit.
    good = {(r.config, r.case) for r in rows if r.status == "scored" and r.valid and not r.failed and r.scored}
    return [c for c in cases if all((cfg, c) in good for cfg in configs_seen)]


def summarize(rows: list[Row]) -> list[dict]:
    """One line per config, in the order they ran."""
    common = set(compared_cases(rows))
    out = []
    for name in dict.fromkeys(r.config for r in rows):
        group = [r for r in rows if r.config == name]
        answered = [r for r in group if r.status == "scored"]
        fair = [r for r in answered if r.case in common]
        paid = sum(r.cost_usd for r in group)
        outs = [r.output_tokens for r in answered if r.output_tokens is not None]
        secs = [r.seconds for r in answered if r.seconds is not None]
        margins = [r.margin for r in fair]
        out.append({
            "config": name, "model": group[0].model, "effort": group[0].effort, "cases": len(group),
            "answered": len(answered), "valid": sum(bool(r.valid) for r in answered),
            "all_checks_pass": sum(not r.failed for r in answered),
            "errors": sum(r.status == "error" for r in group), "dropped": sum(r.status == "dropped" for r in group),
            "squeezed": sum(r.max_tokens is not None and r.max_tokens < model.MAX_TOKENS for r in answered),
            "compared": len(fair),
            "short_units": sum(r.short or 0 for r in fair), "unsold_units": sum(r.unsold or 0 for r in fair),
            "margin": round(sum(margins), 2) if fair and None not in margins else None,
            "cost_usd": round(paid, 4), "cost_per_decision": round(paid / len(answered), 4) if answered else None,
            "avg_output_tokens": round(sum(outs) / len(outs)) if outs else None,
            "avg_seconds": round(sum(secs) / len(secs), 1) if secs else None,
        })
    return out


def render(summary: list[dict], rows: list[Row], budget: Decimal, spent: Decimal) -> str:
    header = ("Config", "Valid", "Checks ok", "Short", "Unsold", "Margin", "Cost", "$/decision", "Out tok", "Secs")
    table = [(s["config"], f"{s['valid']}/{s['cases']}", f"{s['all_checks_pass']}/{s['cases']}",
              str(s["short_units"]), str(s["unsold_units"]),
              "?" if s["margin"] is None else f"${s['margin']:.2f}", f"${s['cost_usd']:.4f}",
              "-" if s["cost_per_decision"] is None else f"${s['cost_per_decision']:.4f}",
              "-" if s["avg_output_tokens"] is None else str(s["avg_output_tokens"]),
              "-" if s["avg_seconds"] is None else str(s["avg_seconds"])) for s in summary]
    widths = [max(len(str(r[i])) for r in (header, *table)) for i in range(len(header))]
    line = lambda r: "  ".join(str(v).ljust(w) for v, w in zip(r, widths)).rstrip()
    common = compared_cases(rows)
    out = ["Model sweep on the team's test cases (made-up numbers: see --case NAME)", "",
           line(header), *(line(r) for r in table), "",
           f"Spent ${spent:.4f} of the sweep's ${budget} budget.",
           (f"Short, Unsold and Margin are summed over the {len(common)} case{'' if len(common) == 1 else 's'} that "
            f"every config answered with a plan that passed every hard check ({', '.join(common)}), so a config can't look "
            "better by failing a case." if common else
            "Short, Unsold and Margin compare nothing: no case was answered with a scoreable valid plan by every "
            "config. Read Valid and Checks ok instead."),
           "Valid and Checks ok count hard checks only. Whether a plan is a GOOD answer is for people "
           "(or a judge) to grade against each case's good_answer: the plans are saved for that."]
    squeezed = [s for s in summary if s["squeezed"]]
    if squeezed:
        out.append("Answered with less than the full answer room because a cap was near: "
                   + ", ".join(f"{s['config']} ({s['squeezed']})" for s in squeezed)
                   + ". A cut-off answer there is a cap result, not a model result.")
    dropped = [r for r in rows if r.status == "dropped"]
    if dropped:
        out.append(f"\nDropped, not sent ({len(dropped)}): "
                   + ", ".join(f"{r.config}/{r.case}{' (daily or run cap)' if r.detail.startswith('cap:') else ''}"
                               for r in dropped))
    errors = [r for r in rows if r.status == "error"]
    if errors:
        out.append(f"\nErrors ({len(errors)}):")
        out += [f"  {r.config}/{r.case}: {r.detail}" for r in errors]
    return "\n".join(out)


def todays_test_cap(connect=None) -> tuple[Decimal, Decimal] | None:
    """(spent today, today's cap) for test calls, team-wide; None if the database can't be read."""
    try:
        with (connect or db.connect)() as conn:
            now = datetime.now(EASTERN)
            spent = harness.spent_since(conn, harness.day_start(now), "test")
            cap = Decimal(config.get_value(conn, "model_test_daily_cap_cents",
                                           harness.DEFAULT_TEST_DAILY_CAP_CENTS)) / 100
            return spent, cap
    except Exception:
        return None


def save_summary(out: Path, summary: list[dict], text: str, meta: dict) -> None:
    with (out / "summary.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        writer.writerows(summary)
    (out / "summary.json").write_text(json.dumps({**meta, "configs": summary}, indent=1), encoding="utf-8")
    (out / "summary.txt").write_text(text + "\n", encoding="utf-8")


def plan_estimate(cfgs: list[Config], n_cases: int) -> Decimal:
    return sum((worst_case(c.model) * n_cases for c in cfgs), Decimal(0))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vending.replay.sweep", description=__doc__.split("\n")[0])
    parser.add_argument("--models", default=",".join(DEFAULT_MODELS), help="haiku, sonnet, opus or full ids")
    parser.add_argument("--efforts", default=",".join(DEFAULT_EFFORTS), help=f"from {', '.join(model.EFFORTS)}")
    parser.add_argument("--configs", help="just these MODEL@EFFORT pairs, such as haiku@medium,opus@low "
                                          "(instead of every model with every effort)")
    parser.add_argument("--cases", help="only these cases, comma separated (default: all ten)")
    parser.add_argument("--repeats", type=int, default=1, help="answers per case per model and effort (1-5)")
    parser.add_argument("--budget", type=Decimal, default=DEFAULT_BUDGET, help="dollars for the whole sweep")
    parser.add_argument("--workers", type=int, default=4, help="requests in flight at once (1-8)")
    parser.add_argument("--out", type=Path, help="results folder (default: sweep-results/<time>)")
    parser.add_argument("--dry-run", action="store_true", help="print the plan and its worst case; send nothing")
    args = parser.parse_args(argv)
    try:
        models = [m.strip() for m in args.models.split(",") if m.strip()]
        efforts = [e.strip() for e in args.efforts.split(",") if e.strip()]
        for m in models:
            model.check_settings(model_id(m), "medium", model.DEFAULT_KEY_ENV)   # an unpriced model fails here
        for e in efforts:
            if e not in model.EFFORTS:
                raise ValueError(f"effort must be one of {', '.join(model.EFFORTS)}, not {e!r}.")
        pairs = pair_configs(args.configs, args.repeats) if args.configs else None
        if not 1 <= args.repeats <= 5 or not 1 <= args.workers <= 8:
            raise ValueError("--repeats takes 1 to 5 and --workers 1 to 8.")
        if not Decimal(0) < args.budget <= MAX_BUDGET:
            raise ValueError(f"--budget must be above $0 and at most ${MAX_BUDGET}.")
        cases = all_cases()
        if args.cases:
            wanted = [n.strip() for n in args.cases.split(",") if n.strip()]
            unknown = sorted(set(wanted) - {c.name for c in cases})
            if unknown:
                raise ValueError(f"No case {', '.join(unknown)}. Cases: {', '.join(c.name for c in cases)}.")
            cases = [c for c in cases if c.name in wanted]
    except (ValueError, ArithmeticError) as error:
        print(error, file=sys.stderr)
        return 2
    cfgs = pairs or configs(models, efforts, args.repeats)
    worst = plan_estimate(cfgs, len(cases))
    print(f"{len(cfgs)} configs x {len(cases)} cases = {len(cfgs) * len(cases)} requests. "
          f"Worst case if every answer ran to {model.MAX_TOKENS} tokens: ${worst:.2f}; "
          f"the budget stops it at ${args.budget}. Typical cost is a fraction of the worst case.")
    for c in cfgs:
        print(f"  {c.label:<18} up to ${worst_case(c.model) * len(cases):.2f}")
    today = todays_test_cap()
    if today is None:
        print("Today's team test cap: unknown, the database can't be read."
              + ("" if args.dry_run else " Nothing is sent without it: start it and run `python -m vending.db migrate`."))
        if not args.dry_run:
            return 1
    else:
        spent, cap = today
        left = max(cap - spent, Decimal(0))
        print(f"Today's team test cap: ${spent:.2f} of ${cap:.2f} spent, ${left:.2f} left"
              + (f". That is less than the budget, so the sweep stops at about ${left:.2f}; requests past it are "
                 "listed as dropped." if left < args.budget else "."))
    if args.dry_run:
        return 0
    stamp = datetime.now(EASTERN).strftime("%Y%m%d-%H%M%S")
    out = args.out or RESULTS_DIR / stamp
    if (out / "results.jsonl").exists():
        print(f"{out} already holds a sweep's results; give a new --out so they aren't mixed.", file=sys.stderr)
        return 2
    sweep = Sweep(cfgs, cases, args.budget, out, workers=args.workers, sweep_id=stamp)
    sweep.run()
    summary = summarize(sweep.rows)
    text = render(summary, sweep.rows, args.budget, sweep.purse.spent)
    save_summary(out, summary, text, {"started": stamp, "budget": str(args.budget), "spent": str(sweep.purse.spent),
                                      "cases": [c.name for c in cases], "key_env": model.DEFAULT_KEY_ENV,
                                      "model_calls_run_ids": f"sweep-{stamp}-*"})
    print("\n" + text + f"\n\nSaved in {out.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
