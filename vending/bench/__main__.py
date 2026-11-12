"""The bench's command line: run, import, report and list. See vending/bench/__init__.py and docs/benchmark.md."""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from vending import db, settings
from vending.bench import judge as judging
from vending.bench import report, store
from vending.bench.judge import Entry, Judge, Verdict, plan_json
from vending.replay import model, sweep
from vending.replay.cases import Case, all_cases
from vending.replay.deciders import Baseline
from vending.replay.state import EASTERN
from vending.replay.sweep import Sweep, plan_estimate, worst_case

DEFAULT_CONFIGS = "haiku@medium,sonnet@high,opus@low"
DEFAULT_JUDGE = "opus@medium"
DEFAULT_BUDGET = Decimal(5)
MAX_BUDGET = sweep.MAX_BUDGET       # a typo guard: --budget 500 is refused
RESULTS_DIR = settings.REPO_ROOT / "bench-results"   # gitignored; the database is the record


def _connect(connect=None):
    return (connect or db.connect)()


def _cases(spec: str | None) -> list[Case]:
    cases = all_cases()
    if not spec:
        return cases
    wanted = [n.strip() for n in spec.split(",") if n.strip()]
    unknown = sorted(set(wanted) - {c.name for c in cases})
    if unknown:
        raise ValueError(f"No case {', '.join(unknown)}. Cases: {', '.join(c.name for c in cases)}.")
    return [c for c in cases if c.name in wanted]


def _judge_spec(spec: str) -> tuple[str, str] | None:
    return None if spec.strip().lower() == "none" else sweep.parse_pair(spec)


# --- run -----------------------------------------------------------------------

def cmd_run(args) -> int:
    try:
        if not 1 <= args.repeats <= 5 or not 1 <= args.workers <= 8:
            raise ValueError("--repeats takes 1 to 5 and --workers 1 to 8.")
        if not Decimal(0) < args.budget <= MAX_BUDGET:
            raise ValueError(f"--budget must be above $0 and at most ${MAX_BUDGET}.")
        cfgs = sweep.pair_configs(args.configs, args.repeats)
        cases = _cases(args.cases)
        judge = _judge_spec(args.judge)
    except (ValueError, ArithmeticError) as error:
        print(error, file=sys.stderr)
        return 2
    names = list(dict.fromkeys(c.name for c in cfgs))
    answers = plan_estimate(cfgs, len(cases))
    judge_worst = Decimal(0) if judge is None else judging.estimate(judge[0], len(cfgs) + 1) * len(cases)
    print(f"{len(names)} configs x {len(cases)} cases x {args.repeats} repeat{'s' if args.repeats > 1 else ''} = "
          f"{len(cfgs) * len(cases)} answers"
          + ("; no judge." if judge is None else f", then {len(cases)} judge requests on {judge[0]}@{judge[1]}."))
    print(f"Worst case if every answer ran to {model.MAX_TOKENS} tokens: ${answers + judge_worst:.2f} "
          f"(answers ${answers:.2f}, judge ${judge_worst:.2f}); the budget stops it at ${args.budget}. "
          "A run usually costs a fraction of that.")
    for name in names:
        c = next(c for c in cfgs if c.name == name)
        print(f"  {name:<16} up to ${worst_case(c.model) * len(cases) * args.repeats:.2f}")
    today = sweep.todays_test_cap()
    if today is None:
        print("Today's team test cap: unknown, the database can't be read."
              + ("" if args.dry_run else " Nothing is sent without it: start it and run `python -m vending.db migrate`."))
        if not args.dry_run:
            return 1
    else:
        spent, cap = today
        left = max(cap - spent, Decimal(0))
        if left <= 0:
            print(f"Today's team test cap: ${spent:.2f} of ${cap:.2f} spent, nothing left"
                  + (": a run would be refused until midnight Eastern." if args.dry_run else "."))
            if not args.dry_run:
                print("Today's team test cap is used up, so nothing was sent. It resets at midnight Eastern; "
                      "the cap is model_test_daily_cap_cents in `python -m vending.config`.", file=sys.stderr)
                return 1
        else:
            print(f"Today's team test cap: ${spent:.2f} of ${cap:.2f} spent, ${left:.2f} left"
                  + (f", less than the budget: the run stops at about ${left:.2f}." if left < args.budget else "."))
    if args.dry_run:
        return 0
    if not (settings.get(model.DEFAULT_KEY_ENV) or "").strip():
        print(f"{model.DEFAULT_KEY_ENV} is not set, so nothing was run. scripts/run_bench.ps1 loads it for you.",
              file=sys.stderr)
        return 1
    try:
        with _connect() as conn:
            if not store.has_tables(conn):
                print("This database has no bench tables yet. Run `python -m vending.db migrate`.", file=sys.stderr)
                return 1
            run_id = store.start_run(
                conn, **store.git_info(), prompt_hash=store.prompt_hash(), cases_hash=store.cases_hash(cases),
                cases=[c.name for c in cases], configs=names, repeats=args.repeats,
                judge="none" if judge is None else f"{judge[0]}@{judge[1]}",
                judge_hash=None if judge is None else judging.judge_hash(),
                budget_usd=args.budget, note=args.note, source="bench", recorded_by=args.by)
    except Exception as error:   # the database went away between the cap check and here
        print(f"The bench run couldn't be recorded ({type(error).__name__}), so nothing was sent.", file=sys.stderr)
        return 1
    # From here on the run row exists: whatever happens, it ends `done` or `stopped`, never left `running`.
    run: Sweep | None = None
    status, notes, stored = "stopped", None, False
    try:
        out = RESULTS_DIR / f"{datetime.now(EASTERN):%Y%m%d-%H%M%S}-run{run_id}"
        with _connect() as conn:
            conn.execute("update bench_runs set results_dir = %s where id = %s", (str(out.resolve()), run_id))
        print(f"\nBench run {run_id}. Answers in {out.resolve()}; model_calls labels start \"bench {run_id}\".\n")
        run = Sweep(cfgs, cases, args.budget, out, workers=args.workers, sweep_id=str(run_id), kind="bench")
        run.run()
        _store_answers(run_id, run, cases)
        stored = True
        if not any(r.status == "scored" and r.config != store.CONTROL for r in run.rows):
            print("\nNo model answered any case (see each config's line above), so nothing was judged and the "
                  "run is marked stopped.")
        else:
            notes = _judge(run_id, run, cases, judge, args.workers) if judge else {}
            status = "done"
    finally:
        spent = run.purse.spent if run is not None else Decimal(0)
        if run is not None and not stored and run.rows:   # a crash part-way: keep the answers that landed
            try:
                _store_answers(run_id, run, cases)
            except Exception as error:
                print(f"Couldn't store the answers of bench run {run_id} ({type(error).__name__}); "
                      f"they are still in {run.out}.", file=sys.stderr)
        try:
            with _connect() as conn:
                store.finish_run(conn, run_id, status, spent, notes)
        except Exception as error:
            print(f"Couldn't mark bench run {run_id} {status} ({type(error).__name__}); "
                  f"it spent ${spent:.4f}.", file=sys.stderr)
    print(f"\nSpent ${spent:.4f} of the run's ${args.budget} budget, answers and judge together.\n")
    shown = show(run_id)
    return shown if status == "done" else 1


def _plans(run: Sweep, cases: list[Case]) -> dict[tuple[str, str], object]:
    """Every plan the run has, by (config label, case): the models' as saved, the baseline's as built."""
    by_case = {c.name: c for c in cases}
    out = {}
    for r in run.rows:
        if r.config == store.CONTROL:
            out[(r.config, r.case)] = plan_json(Baseline().decide(by_case[r.case].state))
            continue
        path = run.out / "plans" / sweep.plan_file(r.config, r.case)
        if r.status == "scored" and path.exists():
            out[(r.config, r.case)] = json.loads(path.read_text(encoding="utf-8"))
    return out


def _store_answers(run_id: int, run: Sweep, cases: list[Case]) -> None:
    plans = _plans(run, cases)
    records = [store.record(r.__dict__, plans.get((r.config, r.case))) for r in run.rows]
    with _connect() as conn:
        store.store_results(conn, run_id, records)


def _judge(run_id: int, run: Sweep, cases: list[Case], spec: tuple[str, str], workers: int) -> dict:
    """Grade every case, store the grades, and return what the judge said (or couldn't) per case."""
    plans = _plans(run, cases)
    judge = Judge(spec[0], spec[1], run.purse, run_id)
    work = []
    for c in cases:
        entries = [Entry(r.config.partition("#")[0], r.repeat, plans[(r.config, r.case)], r.checks)
                   for r in run.rows if r.case == c.name and r.status == "scored" and (r.config, r.case) in plans]
        work.append((c, entries))

    def one(item) -> Verdict:
        c, entries = item
        if not any(e.config != store.CONTROL for e in entries):
            return Verdict(c.name, problem="not judged: no model answered it")
        return judge.grade(c, entries)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        verdicts = list(pool.map(one, work))
    notes = {}
    with _connect() as conn:
        for v in verdicts:
            notes[v.case] = _apply(conn, run_id, v)
            print(f"judge {v.case:<18} " + (notes[v.case].get("problem") or f"{len(v.grades)} plans graded")
                  + f", ${v.cost:.4f}")
    return notes


def _apply(conn, run_id: int, v: Verdict) -> dict:
    if v.problem:
        return {"problem": v.problem}
    for (config, repeat), g in v.grades.items():
        store.set_grade(conn, run_id, config, repeat, v.case, decision=g.decision, reasoning=g.reasoning,
                        invented=g.invented, why=g.why, ok=not v.control_failed)
    if v.control_failed:
        return {"note": v.note, "problem": "judge failed its control: it gave the baseline "
                f"{v.grades[(store.CONTROL, 1)].decision} on a case the baseline is built to fail"}
    return {"note": v.note}


# --- import --------------------------------------------------------------------

def _load_grades(path: Path) -> tuple[str | None, dict[tuple[str, str], dict]]:
    """(judge, {(case, config label): grade}) from a grades file, or ValueError saying what's wrong."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("grades"), list):
        raise ValueError(f"{path} has no list of grades.")
    out = {}
    for g in raw["grades"]:
        key = (g.get("case"), g.get("config"))
        ok = (isinstance(g.get("decision"), int) and not isinstance(g["decision"], bool) and 0 <= g["decision"] <= 3
              and isinstance(g.get("reasoning"), int) and not isinstance(g["reasoning"], bool)
              and 0 <= g["reasoning"] <= 2 and isinstance(g.get("invented_numbers"), bool))
        if not ok or not all(isinstance(k, str) for k in key):
            raise ValueError(f"{path}: grade {key} needs case, config, decision 0-3, reasoning 0-2 "
                             "and invented_numbers true or false.")
        if key in out:
            raise ValueError(f"{path}: {key[1]} is graded twice on {key[0]}.")
        out[key] = g
    return raw.get("judge"), out


def _started(folder: Path, meta: dict) -> datetime | None:
    for stamp in (meta.get("started"), folder.name):
        try:
            return datetime.strptime(str(stamp), "%Y%m%d-%H%M%S").replace(tzinfo=EASTERN)
        except ValueError:
            continue
    return None


def cmd_import(args) -> int:
    folder = Path(args.dir).resolve()
    try:
        lines = (folder / "results.jsonl").read_text(encoding="utf-8").splitlines()
        rows = [json.loads(line) for line in lines if line.strip()]
        meta_file = folder / "summary.json"
        meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.exists() else {}
        grade_judge, grades = _load_grades(Path(args.grades)) if args.grades else (None, {})
    except (OSError, ValueError) as error:
        print(f"Can't import {folder}: {error}", file=sys.stderr)
        return 2
    labels = {(r["case"], r["config"]) for r in rows}
    stray = sorted(set(grades) - labels)
    if stray:
        print(f"The grades name answers that aren't in results.jsonl: {stray[:5]}", file=sys.stderr)
        return 2
    case_names = list(dict.fromkeys(r["case"] for r in rows))
    known = {c.name: c for c in all_cases()}
    current = [known[n] for n in case_names if n in known]
    records, problems = [], {}
    for r in rows:
        plan_path = folder / "plans" / sweep.plan_file(r["config"], r["case"])
        plan = json.loads(plan_path.read_text(encoding="utf-8")) if plan_path.exists() else None
        rec = store.record(r, plan)
        g = grades.get((r["case"], r["config"]))
        if g is not None:
            rec.update(decision=g["decision"], reasoning=g["reasoning"], invented=g["invented_numbers"],
                       judge_why=g.get("why", ""), judge_ok=True)
        records.append(rec)
    for rec in records:   # the same control as a live judge, if the grades include the baseline
        if rec["control"] and rec["decision"] is not None and rec["case_name"] in judging.TRAP_CASES \
                and rec["decision"] >= judging.CONTROL_FAILS_AT:
            problems[rec["case_name"]] = {"problem": "judge failed its control: it passed the baseline"}
    for rec in records:
        if rec["case_name"] in problems and rec["decision"] is not None:
            rec["judge_ok"] = False
    note = " ".join(filter(None, [
        args.note.strip().rstrip(".") + "." if args.note.strip() else "", f"Imported from {folder.name}.",
        "git_sha, prompt_hash and cases_hash are as of the import, not as of the original run."
        + ("" if len(current) == len(case_names) else " Some cases no longer exist, so cases_hash is left empty.")]))
    spent = meta.get("spent")
    try:
        spent = Decimal(spent) if spent is not None else sum((r["cost_usd"] for r in records), Decimal(0))
        budget = Decimal(meta["budget"]) if meta.get("budget") is not None else None
    except (InvalidOperation, TypeError):
        print(f"{folder / 'summary.json'} has a spent or budget that isn't a number.", file=sys.stderr)
        return 2
    fields = dict(
        status="done", **store.git_info(), prompt_hash=store.prompt_hash(),
        cases_hash=store.cases_hash(current) if len(current) == len(case_names) else None,
        cases=case_names, configs=list(dict.fromkeys(r["config"] for r in records if not r["control"])),
        repeats=max((r["repeat"] for r in records), default=1),
        judge=args.judge or grade_judge or ("imported" if grades else "none"), judge_hash=None,
        judge_notes=problems, budget_usd=budget, spent_usd=spent, note=note, source="import",
        results_dir=str(folder), recorded_by=args.by)
    started = _started(folder, meta)
    if started is not None:
        fields.update(started_at=started, finished_at=started)
    try:
        with _connect() as conn:
            if not store.has_tables(conn):
                print("This database has no bench tables yet. Run `python -m vending.db migrate`.", file=sys.stderr)
                return 1
            with conn.transaction():
                before = conn.execute("select id from bench_runs where results_dir = %s and source = 'import'",
                                      (str(folder),)).fetchone()
                if before:
                    print(f"{folder} is already imported, as bench run {before[0]}.", file=sys.stderr)
                    return 2
                run_id = store.start_run(conn, **fields)
                store.store_results(conn, run_id, records)
    except Exception as error:
        print(f"Import failed ({type(error).__name__}: {error}); nothing was stored.", file=sys.stderr)
        return 1
    graded = sum(r["decision"] is not None for r in records)
    print(f"Imported {folder} as bench run {run_id}: {len(records)} answers, {graded} graded"
          + (f", judge failed its control on {', '.join(problems)}" if problems else "") + ".")
    return 0


# --- report and list ------------------------------------------------------------

def show(run_id: int | None = None, history_n: int = 0, as_json: bool = False) -> int:
    """Print the report for a run (default: the latest finished one). Reads only."""
    with _connect() as conn:
        conn.execute("set default_transaction_read_only = on")
        if not store.has_tables(conn):
            print("This database has no bench tables yet. Run `python -m vending.db migrate`.", file=sys.stderr)
            return 1
        everything = store.runs(conn)
        done = [r for r in everything if r["status"] == "done"]
        run = next((r for r in everything if r["id"] == run_id), None) if run_id else (done[-1] if done else None)
        if run is None:
            print(f"No bench run {run_id}." if run_id else "No finished bench run yet: `python -m vending.bench run`.",
                  file=sys.stderr)
            return 1
        rows_of = lambda i: store.results(conn, i)
        hist = report.history(done, rows_of, history_n) if history_n else []
        rep = report.build(run, rows_of(run["id"]), done, rows_of, hist)
    print(json.dumps(rep, default=_jsonable, indent=1) if as_json else report.render(rep))
    return 0


def _jsonable(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def cmd_list(args) -> int:
    with _connect() as conn:
        conn.execute("set default_transaction_read_only = on")
        if not store.has_tables(conn):
            print("This database has no bench tables yet. Run `python -m vending.db migrate`.", file=sys.stderr)
            return 1
        runs = store.runs(conn)
    if not runs:
        print("No bench runs yet.")
        return 0
    rows = [(r["id"], report._when(r["started_at"]), r["status"], r["source"], ",".join(r["configs"]), r["judge"],
             f"${r['spent_usd']:.4f}", r["note"][:60]) for r in reversed(runs)]
    print("\n".join(report._table(("Run", "Started (Eastern)", "Status", "Source", "Configs", "Judge", "Spent", "Note"),
                                  rows)))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vending.bench", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="answer the cases, judge them, store the run (costs money)")
    run.add_argument("--configs", default=DEFAULT_CONFIGS, help=f"MODEL@EFFORT pairs (default {DEFAULT_CONFIGS})")
    run.add_argument("--repeats", type=int, default=1, help="answers per case per config (1-5)")
    run.add_argument("--cases", help="only these cases, comma separated (default: all; a subset isn't "
                                     "compared with full runs)")
    run.add_argument("--budget", type=Decimal, default=DEFAULT_BUDGET,
                     help=f"dollars for answers and judge together (default {DEFAULT_BUDGET}, at most {MAX_BUDGET})")
    run.add_argument("--judge", default=DEFAULT_JUDGE, help=f"MODEL@EFFORT, or none (default {DEFAULT_JUDGE})")
    run.add_argument("--note", default="", help="what changed since the last run, in a few words")
    run.add_argument("--workers", type=int, default=4, help="requests in flight at once (1-8)")
    run.add_argument("--by", default="bench", help="who ran it, for the record")
    run.add_argument("--dry-run", action="store_true", help="print the plan and its worst case; send nothing")
    imp = sub.add_parser("import", help="store an existing sweep results folder as a bench run")
    imp.add_argument("--dir", required=True, help="the folder holding results.jsonl and plans/")
    imp.add_argument("--grades", help="a grades file: {judge, grades: [{case, config, decision, reasoning, "
                                      "invented_numbers, why}]}")
    imp.add_argument("--judge", help="who graded it (default: the grades file's judge)")
    imp.add_argument("--note", default="")
    imp.add_argument("--by", default="bench", help="who imported it, for the record")
    rep = sub.add_parser("report", help="one run against the last comparable one (reads only)")
    rep.add_argument("--run", type=int, help="the run (default: the latest finished one)")
    rep.add_argument("--json", action="store_true", help="print JSON, for scripts and other tools")
    rep.add_argument("--history", type=int, default=0, help="also each config's score over the last N runs")
    sub.add_parser("list", help="every run, newest first (reads only)")
    args = parser.parse_args(argv)
    if args.command == "run":
        return cmd_run(args)
    if args.command == "import":
        return cmd_import(args)
    if args.command == "report":
        if not 0 <= args.history <= 200:
            print("--history takes 0 to 200.", file=sys.stderr)
            return 2
        return show(args.run, args.history, args.json)
    return cmd_list(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
