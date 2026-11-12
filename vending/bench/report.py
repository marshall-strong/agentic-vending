"""Read a bench run: one line per config, the change since the last comparable run, and what regressed.

Score is the judge's: 100 x (decision + reasoning) / (5 x plans judged), over
the plans whose case the judge passed its control on (`judge_ok` true). Hard
checks, cost and speed come from the answers themselves.

A run is compared, config by config, with earlier finished runs on the same
cases (`cases_hash`), in the order they ran (`started_at`, not id: an old sweep
imported late belongs where it ran). Two earlier runs can be used:

- **Scores and decisions** are compared with the latest earlier run graded by
  the same judge and rubric that has at least one counted grade for the config.
  A run with no grades (`--judge none`, a purse that ran out before judging, a
  judge that failed every case) would otherwise hide a fall, and a score from
  another judge measures the judge as much as the config. The change is taken
  over the plans judged in both runs, so a config that stops answering cases
  can't raise its score by dropping them.
- **Hard checks, cost and answered cases** are compared with the latest earlier
  run that ran the config at all, judged or not.

When the prompt, the judge or the code changed in between, the report says so,
because that is what the benchmark is for: putting a change in score down to a
change in the inputs.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from vending.bench.store import CONTROL
from vending.harness import EASTERN

SCORE_DROP = Decimal(10)        # points: a fall this big is a regression
COST_RISE = Decimal("1.5")      # cost per decision up by more than half is a regression
RIGHT, WRONG = 3, 0


def _round(value: Decimal | None, places: str) -> Decimal | None:
    return None if value is None else value.quantize(Decimal(places), ROUND_HALF_UP)


def judged(rows: list[dict]) -> list[dict]:
    """The plans whose grade counts: judged, on a case where the judge passed its control."""
    return [r for r in rows if r["decision"] is not None and r["judge_ok"] is True]


def score(rows: list[dict]) -> Decimal | None:
    counted = judged(rows)
    if not counted:
        return None
    points = sum(r["decision"] + r["reasoning"] for r in counted)
    return _round(Decimal(100 * points) / (5 * len(counted)), "0.1")


def not_judged(rows: list[dict]) -> int:
    """Plans with no grade at all: not answered (error, dropped), or the judge couldn't grade them.

    A grade the judge's failed control threw out (`judge_ok` false) isn't counted:
    that is the judge's problem, said once per case, not the config's.
    """
    return sum(r["decision"] is None for r in rows)


def line(config: str, rows: list[dict]) -> dict:
    """One config's results in one run, every repeat together."""
    answered = [r for r in rows if r["status"] == "scored"]
    counted = judged(rows)
    cost = sum((r["cost_usd"] for r in rows), Decimal(0))
    secs = [r["seconds"] for r in answered if r["seconds"] is not None]
    return {
        "config": config, "control": config == CONTROL, "model": rows[0]["model"], "effort": rows[0]["effort"],
        "plans": len(rows), "answered": len(answered), "judged": len(counted), "score": score(rows),
        "right_calls": sum(r["decision"] == RIGHT for r in counted),
        "wrong_calls": sum(r["decision"] == WRONG for r in counted),
        "invented": sum(bool(r["invented"]) for r in counted),
        "valid": sum(bool(r["valid"]) for r in answered),
        "checks_ok": sum(not r["failed"] for r in answered),
        "errors": sum(r["status"] == "error" for r in rows), "dropped": sum(r["status"] == "dropped" for r in rows),
        "cost_usd": cost,
        "cost_per_decision": _round(cost / len(answered), "0.0001") if answered else None,
        "avg_seconds": _round(sum(secs, Decimal(0)) / len(secs), "0.1") if secs else None,
    }


def by_config(rows: list[dict]) -> dict[str, list[dict]]:
    """Rows grouped by config, the control first, then in the order they were stored."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in sorted(rows, key=lambda r: not r["control"]):
        groups[r["config"]].append(r)
    return dict(groups)


def lines(rows: list[dict]) -> list[dict]:
    return [line(name, group) for name, group in by_config(rows).items()]


# --- Against an earlier run ---------------------------------------------------

_LONG_AGO = datetime(1970, 1, 1, tzinfo=timezone.utc)


def when(run: dict) -> tuple:
    """A run's place in time: when it started, then its id for runs that started together."""
    return (run.get("started_at") or _LONG_AGO, run["id"])


def _before(run: dict, earlier: list[dict]) -> list[dict]:
    """The runs that started before `run`, newest first."""
    return sorted((r for r in earlier if when(r) < when(run)), key=when, reverse=True)


def comparable(run: dict, earlier: list[dict], config: str, configs_of) -> dict | None:
    """The latest earlier finished run on the same cases that ran `config`: the base for checks and cost."""
    if run["cases_hash"] is None:
        return None
    for prev in _before(run, earlier):
        if prev["cases_hash"] == run["cases_hash"] and config in configs_of(prev["id"]):
            return prev
    return None


def graded_base(run: dict, earlier: list[dict], config: str, rows_of) -> dict | None:
    """The latest earlier run on the same cases, graded by the same judge and rubric, with a counted grade for `config`.

    The base for scores and decisions. A run in between with no grades, or graded
    by another judge, is passed over, never compared with.
    """
    if run["cases_hash"] is None:
        return None
    same = (run["cases_hash"], run["judge"], run["judge_hash"])
    for prev in _before(run, earlier):
        if (prev["cases_hash"], prev["judge"], prev["judge_hash"]) != same:
            continue
        if judged([r for r in rows_of(prev["id"]) if r["config"] == config]):
            return prev
    return None


def _key(r: dict) -> tuple[str, int]:
    return r["case_name"], r["repeat"]


def like_for_like(now: list[dict], before: list[dict]) -> tuple[Decimal | None, Decimal | None, int]:
    """(score now, score before, plans) over the plans judged in both runs, so both scores measure the same cases."""
    now_j, before_j = {_key(r): r for r in judged(now)}, {_key(r): r for r in judged(before)}
    both = sorted(now_j.keys() & before_j.keys())
    return score([now_j[k] for k in both]), score([before_j[k] for k in both]), len(both)


def _per_case(rows: list[dict]) -> dict[str, dict]:
    """Per case: the mean decision over its counted repeats, every hard check any repeat failed, and why it went unanswered."""
    out: dict[str, dict] = defaultdict(lambda: {"decisions": [], "failed": set(), "answered": False, "missing": set()})
    for r in rows:
        case = out[r["case_name"]]
        if r["status"] == "scored":
            case["answered"] = True
            case["failed"] |= set(r["failed"])
        else:
            case["missing"].add(r["status"])
        if r["decision"] is not None and r["judge_ok"] is True:
            case["decisions"].append(r["decision"])
    for case in out.values():
        d = case["decisions"]
        case["decision"] = Decimal(sum(d)) / len(d) if d else None
    return out


def score_regressions(config: str, now: list[dict], before: list[dict], before_id: int) -> list[str]:
    """What got worse in `config`'s grades since `before`, a run graded by the same judge."""
    found = []
    score_now, score_before, _ = like_for_like(now, before)
    if score_now is not None and score_before is not None and score_before - score_now >= SCORE_DROP:
        found.append(f"{config}: score fell {score_before - score_now} points ({score_before}% -> {score_now}%)")
    cases_now, cases_before = _per_case(now), _per_case(before)
    for name, c in cases_now.items():
        b = cases_before.get(name)
        if b is not None and b["decision"] == RIGHT and c["decision"] is not None and c["decision"] <= 1:
            found.append(f"{config}/{name}: decision went from 3 to {_fmt(c['decision'])}")
    missing_now, missing_before = not_judged(now), not_judged(before)
    if missing_now > missing_before:
        found.append(f"{config}: {missing_now} of {len(now)} plans not judged now, {missing_before} in run "
                     f"{before_id}, so its score covers fewer cases")
    return found


def answer_regressions(config: str, now: list[dict], before: list[dict], before_id: int) -> list[str]:
    """What got worse in `config`'s answers since `before`: cases it stopped answering, new hard-check failures, cost."""
    found = []
    cases_now, cases_before = _per_case(now), _per_case(before)
    for name, c in cases_now.items():
        b = cases_before.get(name)
        if b is None or not b["answered"]:
            continue
        if not c["answered"]:
            found.append(f"{config}/{name}: answered in run {before_id}, {' and '.join(sorted(c['missing']))} now")
            continue
        new = sorted(c["failed"] - b["failed"])
        if new:
            found.append(f"{config}/{name}: now fails {', '.join(new)}")
    was, now_cost = line(config, before)["cost_per_decision"], line(config, now)["cost_per_decision"]
    if was and now_cost is not None and now_cost > was * COST_RISE:
        found.append(f"{config}: cost per decision up {int((now_cost / was - 1) * 100)}% (${was} -> ${now_cost})")
    return found


def _fmt(value: Decimal) -> str:
    return str(int(value)) if value == int(value) else f"{value:.1f}"


def changes(run: dict, prev: dict) -> list[str]:
    """What changed between two runs that could explain a change in score."""
    out = []
    if prev["prompt_hash"] != run["prompt_hash"]:
        out.append(f"prompt changed since run {prev['id']}")
    if prev["judge"] != run["judge"] or prev["judge_hash"] != run["judge_hash"]:
        out.append(f"judge changed since run {prev['id']} ({prev['judge']} -> {run['judge']})")
    if prev["git_sha"] != run["git_sha"]:
        out.append(f"code changed since run {prev['id']} ({_short(prev['git_sha'])} -> {_short(run['git_sha'])})")
    return out


def _short(value: str | None, n: int = 7) -> str:
    return (value or "?")[:n]


def build(run: dict, rows: list[dict], earlier: list[dict], rows_of, history: list[dict] | None = None) -> dict:
    """Everything the text report and `report --json` show for one run.

    `earlier` is every finished run (any order); `rows_of(run_id)` loads a run's results.
    """
    cache: dict[int, list[dict]] = {}

    def load(run_id: int) -> list[dict]:
        if run_id not in cache:
            cache[run_id] = rows_of(run_id)
        return cache[run_id]

    configs_of = lambda run_id: {r["config"] for r in load(run_id)}
    of = lambda run_id, config: [r for r in load(run_id) if r["config"] == config]
    now_lines = lines(rows)
    compared, found, flags, uncompared = {}, [], [], []
    for ln in now_lines:
        name, now = ln["config"], [r for r in rows if r["config"] == ln["config"]]
        plain = comparable(run, earlier, name, configs_of)
        graded = graded_base(run, earlier, name, load)
        if plain is None and graded is None:
            continue
        compared[name] = _comparison(now, None if graded is None else of(graded["id"], name), graded, plain)
        delta = compared[name]["delta"]
        if ln["control"]:
            if delta is not None and abs(delta) >= SCORE_DROP:
                flags.append(f"The control's score moved {delta:+} points since run {graded['id']} on the same plans: "
                             "that is the judge's own noise, so read smaller changes with care")
            continue
        if graded is not None:
            found += score_regressions(name, now, of(graded["id"], name), graded["id"])
        if plain is not None:
            found += answer_regressions(name, now, of(plain["id"], name), plain["id"])
        reason = _not_compared(ln, graded, compared[name]["on"])
        if reason:
            uncompared.append(f"{name}: {reason}")
        for prev in (p for p in (graded, plain) if p is not None):
            for change in changes(run, prev):
                if change not in flags:
                    flags.append(change)
    # Only when no earlier run shares this run's cases: a `--cases` subset run in between changes
    # nothing, since the comparisons above skip it for the last run on the same cases.
    previous = _before(run, earlier)
    last = next(iter(previous), None)
    same_cases = run["cases_hash"] is not None and any(p["cases_hash"] == run["cases_hash"] for p in previous)
    if last is not None and last["cases_hash"] != run["cases_hash"] and not same_cases:
        flags.append(f"cases changed since run {last['id']}: runs before that aren't compared with this one")
    if run["git_dirty"] and run["source"] == "bench":
        flags.append("ran with uncommitted changes, so the git sha doesn't say exactly what ran")
    return {"run": run, "configs": now_lines, "compared": compared, "regressions": found, "flags": flags,
            "uncompared": uncompared, "judge": judge_state(run, rows), "history": history or []}


def _comparison(now: list[dict], before: list[dict] | None, graded: dict | None, plain: dict | None) -> dict:
    """One config's entry in `compared`: the score base (`run`) and its change, and the checks base (`checks_run`)."""
    score_now = score_before = delta = None
    both = 0
    if before is not None:
        score_now, score_before, both = like_for_like(now, before)
        if score_now is not None and score_before is not None:
            delta = score_now - score_before
    return {"run": None if graded is None else graded["id"], "score_before": score_before, "delta": delta,
            "on": both, "judged_before": None if before is None else len(judged(before)),
            "checks_run": None if plain is None else plain["id"]}


def _not_compared(ln: dict, graded: dict | None, both: int) -> str:
    """Why `ln`'s score wasn't compared, or "" if it was."""
    if ln["judged"] == 0:
        return "no plan of it was judged in this run"
    if graded is None:
        return "no earlier run on the same cases was graded by this judge (same judge and rubric)"
    if both == 0:
        return f"no case was judged in both this run and run {graded['id']}"
    return ""


def judge_state(run: dict, rows: list[dict]) -> dict:
    control_failed = sorted({r["case_name"] for r in rows if r["control"] and r["judge_ok"] is False})
    notes = run.get("judge_notes") or {}
    problems = {case: n["problem"] for case, n in notes.items() if n.get("problem")}
    control_graded = any(r["control"] and r["decision"] is not None for r in rows)
    return {"control_failed": control_failed, "problems": problems, "control_graded": control_graded}


def history(runs: list[dict], rows_of, n: int) -> list[dict]:
    """Each config's score in the last `n` finished runs, in the order they ran.

    `breaks` names what changed since the row above (the judge, the cases): a score
    on either side of a break measures something else, so the trend restarts there.
    """
    out, above = [], None
    for run in sorted(runs, key=when)[-n:]:
        breaks = []
        if above is not None and (above["judge"], above["judge_hash"]) != (run["judge"], run["judge_hash"]):
            breaks.append("new judge")
        if above is not None and above["cases_hash"] != run["cases_hash"]:
            breaks.append("new cases")
        out.append({"run": run["id"], "started_at": run["started_at"], "git_sha": run["git_sha"],
                    "prompt_hash": run["prompt_hash"], "cases_hash": run["cases_hash"], "judge": run["judge"],
                    "judge_hash": run["judge_hash"], "breaks": breaks,
                    "scores": {ln["config"]: ln["score"] for ln in lines(rows_of(run["id"]))}})
        above = run
    return out


# --- Text ---------------------------------------------------------------------

def _table(header: tuple, rows: list[tuple]) -> list[str]:
    widths = [max(len(str(r[i])) for r in (header, *rows)) for i in range(len(header))]
    return ["  ".join(str(v).ljust(w) for v, w in zip(r, widths)).rstrip() for r in (header, *rows)]


def _money(value) -> str:
    return "-" if value is None else f"${value:.4f}"


def _pct(value) -> str:
    return "-" if value is None else f"{value}%"


def _when(value) -> str:
    return "?" if value is None else value.astimezone(EASTERN).strftime("%Y-%m-%d %H:%M")


def render(rep: dict) -> str:
    run = rep["run"]
    out = [f"Bench run {run['id']} ({run['source']}), {_when(run['started_at'])} Eastern, {run['status']}",
           f"  git {_short(run['git_sha'])} on {run['git_branch'] or '?'}{' (uncommitted changes)' if run['git_dirty'] else ''}"
           f"; prompt {_short(run['prompt_hash'], 8)}; cases {_short(run['cases_hash'], 8)} ({len(run['cases'])})"
           f"; judge {run['judge']}" + (f" (rubric {_short(run['judge_hash'], 8)})" if run["judge_hash"] else ""),
           f"  spent ${run['spent_usd']:.4f}" + (f" of ${run['budget_usd']}" if run["budget_usd"] is not None else "")]
    if run["note"]:
        out.append(f"  note: {run['note']}")
    header = ("Config", "Judged", "Score", "Right", "Wrong", "Invented", "Valid", "Checks ok", "Errors", "Dropped",
              "$/decision", "Secs", "vs run", "Change")
    table = []
    for ln in rep["configs"]:
        cmp = rep["compared"].get(ln["config"], {})
        table.append((ln["config"] + (" (control)" if ln["control"] else ""), f"{ln['judged']}/{ln['plans']}",
                      _pct(ln["score"]), ln["right_calls"], ln["wrong_calls"], ln["invented"],
                      f"{ln['valid']}/{ln['plans']}", f"{ln['checks_ok']}/{ln['plans']}", ln["errors"], ln["dropped"],
                      _money(ln["cost_per_decision"]), "-" if ln["avg_seconds"] is None else ln["avg_seconds"],
                      _vs(cmp), _change(ln, cmp)))
    out += ["", *_table(header, table), "",
            "Score = 100 x (decision 0-3 + reasoning 0-2) / (5 x plans judged), from the judge. Right = decision 3, "
            "wrong = decision 0. Valid and Checks ok are the automatic hard checks. Change is the score against "
            "the run in vs run, on the plans judged in both."]
    judge = rep["judge"]
    for case in judge["control_failed"]:
        out.append(f"Judge failed its control on {case}: it passed the baseline there, so that case's grades are "
                   "left out of every score.")
    for case, problem in judge["problems"].items():
        if case not in judge["control_failed"]:
            out.append(f"{case}: {problem}")
    if run["judge"] != "none" and not judge["control_graded"]:
        out.append("The control (the baseline) wasn't graded in this run, so the judge's calibration is unchecked.")
    if rep["flags"]:
        out += ["", "Changes that could explain a change in score:", *(f"  {f}" for f in rep["flags"])]
    if rep["regressions"]:
        out += ["", f"REGRESSIONS ({len(rep['regressions'])}):", *(f"  {r}" for r in rep["regressions"])]
    elif rep["compared"] and not rep["uncompared"]:
        out += ["", "No regressions against the runs compared."]
    elif not rep["compared"]:
        out += ["", "No earlier finished run on the same cases to compare with."]
    if rep["uncompared"]:
        out += ["", "Scores not compared, so a fall in them can't be ruled out:",
                *(f"  {u}" for u in rep["uncompared"])]
    if rep["history"]:
        out += ["", "History", *render_history(rep["history"])]
    return "\n".join(out)


def _vs(cmp: dict) -> str:
    """The run the score is compared with, and the one the checks are, when they differ."""
    if not cmp:
        return "-"
    graded, plain = cmp.get("run"), cmp.get("checks_run")
    if graded == plain or plain is None:
        return str(graded)
    return f"{graded or '-'}, checks {plain}"


def _change(ln: dict, cmp: dict) -> str:
    """The score change, with how many plans it rests on when that is fewer than either run judged."""
    delta = cmp.get("delta")
    if delta is None:
        return "-"
    partial = cmp["on"] < ln["judged"] or cmp["on"] < (cmp["judged_before"] or 0)
    return f"{delta:+}" + (f" on {cmp['on']}" if partial else "")


def _judge(h: dict) -> str:
    return h["judge"] + (f" {_short(h['judge_hash'], 8)}" if h["judge_hash"] else "")


def render_history(hist: list[dict]) -> list[str]:
    configs = list(dict.fromkeys(c for h in hist for c in h["scores"]))
    header = ("Run", "Date", "Git", "Prompt", "Cases", "Judge", *configs, "Trend")
    rows = [(h["run"], _when(h["started_at"])[:10], _short(h["git_sha"]), _short(h["prompt_hash"], 8),
             _short(h["cases_hash"], 8), _judge(h),
             *(_pct(h["scores"].get(c)) if c in h["scores"] else "" for c in configs), ", ".join(h["breaks"]))
            for h in hist]
    return _table(header, rows)
