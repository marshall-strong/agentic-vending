"""The standing benchmark, against the same local fake of the Anthropic API as the exam's tests.

No test spends money: the answers and the judge both come from the fake, and
every bench and model_calls row goes to the test's throwaway schema.
"""

import json
import re
from decimal import Decimal

import pytest

from tests.test_replay_model import api, baseline_json, case, spend_earlier_today  # noqa: F401  (api is a fixture)
from vending import db
from vending.bench import __main__ as bench
from vending.bench import judge as judging
from vending.bench import store
from vending.bench.__main__ import main
from vending.replay import sweep
from vending.replay.cases import all_cases
from vending.replay.deciders import Baseline
from vending.replay.model import state_json

WEEKS = {json.dumps(state_json(c.state), sort_keys=True): c for c in all_cases()}
# Each model's plan carries its own reason, so the fake judge can tell whose plan a label hides.
WHO = {"claude-haiku-5-5": "plan-A", "claude-sonnet-5-5": "plan-B", "claude-opus-5-5": "plan-C"}
CONTROL_REASON = Baseline().decide(case("slow-seller").state).reason


class Responder:
    """Answers each request by what it is: a plan request gets a valid plan, a judge request gets grades.

    `grades[(who, case)]` is (decision, reasoning); by default models get (3, 2) and the
    control (0, 1). `judge_reply(payload)` can return a raw reply instead.
    """

    def __init__(self, grades=None, judge_reply=None):
        self.grades, self.judge_reply, self.judged = grades or {}, judge_reply, []

    def __call__(self, body):
        if body.get("system") == judging.SYSTEM:
            payload = json.loads(body["messages"][0]["content"])
            self.judged.append(payload)
            raw = self.judge_reply(payload) if self.judge_reply else None
            if raw is not None:
                return raw
            out = []
            for p in payload["plans"]:
                who = "control" if p["plan"]["reason"] == CONTROL_REASON else p["plan"]["reason"]
                d, r = self.grades.get((who, payload["case"]), (0, 1) if who == "control" else (3, 2))
                out.append({"label": p["label"], "decision": d, "reasoning": r, "invented_numbers": False,
                            "why": f"{who} on {payload['case']}"})
            return 200, "end_turn", json.dumps({"grades": out, "case_note": "the good plans did X"})
        week = json.loads(body["messages"][0]["content"].split("\n", 1)[1])
        plan = baseline_json(WEEKS[json.dumps(week, sort_keys=True)].state)
        plan["reason"] = WHO[body["model"]]
        return 200, "end_turn", json.dumps(plan)


@pytest.fixture
def results_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(bench, "RESULTS_DIR", tmp_path / "bench-results")
    return tmp_path / "bench-results"


RUN = ["run", "--configs", "haiku@low,sonnet@low", "--cases", "catalog-request,slow-seller",
       "--judge", "opus@low", "--budget", "2", "--workers", "2"]


def bench_rows(conn, run_id):
    return {(r["config"], r["case_name"]): r for r in store.results(conn, run_id)}


def only_run(conn):
    (run,) = store.runs(conn)
    return run


# --- A whole run ----------------------------------------------------------------------

def test_a_run_answers_judges_stores_and_reports(api, conn, results_dir, capsys):
    api.respond = Responder(grades={("plan-B", "catalog-request"): (2, 1), ("plan-B", "slow-seller"): (2, 1)})
    assert main(RUN) == 0
    out = capsys.readouterr().out
    run = only_run(conn)
    assert (run["status"], run["source"], run["configs"], run["cases"]) == (
        "done", "bench", ["haiku@low", "sonnet@low"], ["slow-seller", "catalog-request"])
    assert run["prompt_hash"] == store.prompt_hash() and run["judge"] == "claude-opus-5-5@low"
    assert run["cases_hash"] == store.cases_hash([case("slow-seller"), case("catalog-request")])
    assert run["judge_hash"] == judging.judge_hash() and run["git_sha"] and run["finished_at"]
    assert run["results_dir"].endswith(f"-run{run['id']}")
    rows = bench_rows(conn, run["id"])
    assert set(rows) == {(cfg, c) for cfg in ("baseline", "haiku@low", "sonnet@low")
                         for c in ("catalog-request", "slow-seller")}
    assert all(r["control"] == (r["config"] == "baseline") for r in rows.values())
    assert all(r["judge_ok"] is True and r["plan"]["slots"] for r in rows.values())
    assert rows[("haiku@low", "slow-seller")]["decision"] == 3 and rows[("sonnet@low", "slow-seller")]["decision"] == 2
    assert rows[("baseline", "slow-seller")]["plan"]["reason"] == CONTROL_REASON
    assert rows[("haiku@low", "catalog-request")]["cost_usd"] == Decimal("0.0013")   # exact, not a float
    # Every request is a test call labelled "bench <id> ...", the judge's too, and the run's total is theirs.
    calls = conn.execute("select label, purpose, cost_usd from model_calls order by label").fetchall()
    labels = sorted(c[0] for c in calls)
    rid = run["id"]
    assert labels == sorted([f"bench {rid} haiku@low catalog-request 2026-10-02", f"bench {rid} haiku@low slow-seller 2026-10-02",
                             f"bench {rid} sonnet@low catalog-request 2026-10-02", f"bench {rid} sonnet@low slow-seller 2026-10-02",
                             f"bench {rid} judge catalog-request", f"bench {rid} judge slow-seller"])
    assert {c[1] for c in calls} == {"test"} and run["spent_usd"] == sum(c[2] for c in calls)
    # Scores: haiku (3+2)/5 on both cases; sonnet (2+1)/5; the control (0+1)/5.
    assert re.search(r"haiku@low\s+2/2\s+100\.0%\s+2\s+0", out)
    assert re.search(r"sonnet@low\s+2/2\s+60\.0%\s+0\s+0", out)
    assert re.search(r"baseline \(control\)\s+2/2\s+20\.0%", out)
    assert "No earlier finished run on the same cases" in out


def test_the_judge_is_blind_and_sees_the_case(api, conn, results_dir):
    responder = Responder()
    api.respond = responder
    assert main(RUN) == 0
    payload = next(p for p in responder.judged if p["case"] == "slow-seller")
    assert [p["label"] for p in payload["plans"]] == ["P1", "P2", "P3"]
    assert payload["good_answer"] == case("slow-seller").good_answer
    assert payload["units_sold_next_week"]["15"] == 0 and payload["week"]["slots"][0]["slot"] == "15"
    assert all(set(p) == {"label", "checks", "plan"} for p in payload["plans"])
    text = json.dumps(payload)
    assert "haiku" not in text and "sonnet" not in text and "baseline" not in text
    sent = next(p for p in api.posts if p.get("system") == judging.SYSTEM)
    schema = sent["output_config"]["format"]["schema"]
    assert schema["properties"]["grades"]["items"]["properties"]["label"]["enum"] == ["P1", "P2", "P3"]


def test_blinding_is_fixed_by_a_hash_not_by_order_or_chance():
    entries = [judging.Entry(cfg, rep, {}, {}) for cfg, rep in
               (("baseline", 1), ("haiku@low", 1), ("haiku@low", 2), ("opus@low", 1))]
    first = {k: (e.config, e.repeat) for k, e in judging.blind("x", entries).items()}
    again = {k: (e.config, e.repeat) for k, e in judging.blind("x", list(reversed(entries))).items()}
    assert first == again and list(first) == ["P1", "P2", "P3", "P4"]
    other = {k: (e.config, e.repeat) for k, e in judging.blind("y", entries).items()}
    assert other != first   # each case shuffles differently


def test_a_judge_that_passes_the_control_on_a_trap_case_is_thrown_out_there(api, conn, results_dir, capsys):
    api.respond = Responder(grades={("control", "slow-seller"): (2, 1)})   # slow-seller is a trap case
    assert main(RUN) == 0
    out = capsys.readouterr().out
    run = only_run(conn)
    rows = bench_rows(conn, run["id"])
    assert {r["judge_ok"] for (cfg, c), r in rows.items() if c == "slow-seller"} == {False}
    assert {r["judge_ok"] for (cfg, c), r in rows.items() if c == "catalog-request"} == {True}
    assert "control" in run["judge_notes"]["slow-seller"]["problem"]
    assert "Judge failed its control on slow-seller" in out
    # Only catalog-request counts: one judged plan of two per config.
    assert re.search(r"haiku@low\s+1/2\s+100\.0%", out)


def test_the_control_passing_a_case_that_isnt_a_trap_is_fine():
    grades = {("baseline", 1): judging.Grade(3, 2, False, "")}
    assert not judging.control_failed("catalog-request", grades)
    assert judging.control_failed("low-balance", grades)
    assert judging.TRAP_CASES == {"slow-seller", "midweek-visit", "tuesday-sellout", "low-balance", "price-below-floor"}


@pytest.mark.parametrize("reply", [
    (200, "end_turn", "not json at all"),
    (200, "end_turn", json.dumps({"grades": [{"label": "P1", "decision": 7, "reasoning": 1,
                                              "invented_numbers": False, "why": ""}], "case_note": ""})),
    (200, "end_turn", json.dumps({"grades": [], "case_note": "forgot them"})),
    (200, "refusal", ""),
    (500, None, None),
])
def test_an_unusable_judge_answer_leaves_the_case_unjudged_and_the_run_goes_on(api, conn, results_dir, capsys,
                                                                               reply):
    api.respond = Responder(judge_reply=lambda p: reply if p["case"] == "slow-seller" else None)
    assert main(RUN) == 0
    out = capsys.readouterr().out
    run = only_run(conn)
    assert run["status"] == "done"
    rows = bench_rows(conn, run["id"])
    assert all(r["decision"] is None and r["judge_ok"] is None for (cfg, c), r in rows.items() if c == "slow-seller")
    assert all(r["decision"] is not None for (cfg, c), r in rows.items() if c == "catalog-request")
    assert run["judge_notes"]["slow-seller"]["problem"] and "slow-seller: " in out


def test_validate_wants_every_label_once_in_range():
    good = {"label": "P1", "decision": 3, "reasoning": 2, "invented_numbers": False, "why": "ok"}
    assert judging.validate({"grades": [good], "case_note": "n"}, ["P1"])[0]["P1"].decision == 3
    for bad in ({**good, "decision": True}, {**good, "reasoning": 3}, {**good, "label": "P9"},
                {**good, "invented_numbers": "no"}):
        with pytest.raises(judging.JudgeError):
            judging.validate({"grades": [bad]}, ["P1"])
    with pytest.raises(judging.JudgeError, match="twice"):
        judging.validate({"grades": [good, good]}, ["P1"])
    with pytest.raises(judging.JudgeError, match="didn't grade P2"):
        judging.validate({"grades": [good]}, ["P1", "P2"])


def test_a_budget_spent_on_answers_drops_the_judge_and_says_so(api, conn, results_dir, capsys):
    api.respond = Responder()
    # Haiku answers fit in $0.05; an Opus judge's worst case (about $0.40) doesn't.
    assert main(["run", "--configs", "haiku@low", "--cases", "catalog-request,slow-seller", "--judge", "opus@low",
                 "--budget", "0.05", "--workers", "1"]) == 0
    out = capsys.readouterr().out
    run = only_run(conn)
    assert run["status"] == "done" and run["spent_usd"] == Decimal("0.0026")
    assert not any(p.get("system") == judging.SYSTEM for p in api.posts)
    assert all(r["decision"] is None for r in store.results(conn, run["id"]))
    assert all("couldn't hold the judge's worst case" in n["problem"] for n in run["judge_notes"].values())
    assert "couldn't hold the judge's worst case" in out


def test_no_judge_means_answers_only(api, conn, results_dir):
    api.respond = Responder()
    assert main(["run", "--configs", "haiku@low", "--cases", "slow-seller", "--judge", "none"]) == 0
    run = only_run(conn)
    assert run["judge"] == "none" and run["judge_hash"] is None and run["status"] == "done"
    assert not any(p.get("system") == judging.SYSTEM for p in api.posts)


def test_no_database_means_no_run_and_nothing_sent(api, monkeypatch, results_dir, capsys):
    def down():
        raise OSError("database is down")

    monkeypatch.setattr(db, "connect", down)
    assert main(RUN) == 1
    assert "database can't be read" in capsys.readouterr().out
    assert api.counts == [] and api.posts == [] and not results_dir.exists()


def test_no_bench_tables_means_no_run(api, conn, results_dir, capsys):
    conn.execute("drop table bench_results; drop table bench_runs")
    assert main(RUN) == 1
    assert "python -m vending.db migrate" in capsys.readouterr().err and api.posts == []


def test_no_key_means_no_run(api, conn, monkeypatch, results_dir, capsys):
    monkeypatch.delenv("VENDING_ANTHROPIC_API_KEY")
    assert main(RUN) == 1
    assert "is not set" in capsys.readouterr().err and store.runs(conn) == [] and api.counts == []


def test_a_dry_run_spends_and_stores_nothing(api, conn, results_dir, capsys):
    assert main(["run", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "3 configs x 10 cases x 1 repeat = 30 answers, then 10 judge requests on claude-opus-5-5@medium" in out
    assert "haiku@medium" in out and "sonnet@high" in out and "opus@low" in out
    assert "Today's team test cap: $0.00 of $20.00 spent" in out
    assert api.counts == [] and api.posts == [] and store.runs(conn) == []


def test_bad_arguments_are_refused(capsys):
    assert main(["run", "--configs", "haiku", "--dry-run"]) == 2
    assert main(["run", "--configs", "haiku@turbo", "--dry-run"]) == 2
    assert main(["run", "--configs", "gpt-5@low", "--dry-run"]) == 2
    assert main(["run", "--budget", "51", "--dry-run"]) == 2
    assert main(["run", "--judge", "opus", "--dry-run"]) == 2
    assert main(["run", "--cases", "nope", "--dry-run"]) == 2


# --- Config pairs in the sweep ------------------------------------------------------

def test_explicit_pairs_run_only_those_cheapest_first():
    cfgs = sweep.pair_configs("opus@low,haiku@medium,sonnet@high,haiku@medium", 2)
    assert [(c.model, c.effort, c.repeat) for c in cfgs] == [
        ("claude-haiku-5-5", "medium", 1), ("claude-haiku-5-5", "medium", 2),
        ("claude-sonnet-5-5", "high", 1), ("claude-sonnet-5-5", "high", 2),
        ("claude-opus-5-5", "low", 1), ("claude-opus-5-5", "low", 2)]
    assert [c.label for c in cfgs][:2] == ["haiku@medium", "haiku@medium#2"] and cfgs[1].name == "haiku@medium"


def test_the_sweep_takes_pairs_too(api, conn, monkeypatch, capsys):   # api: the dry run reads the test database
    monkeypatch.delenv("VENDING_ANTHROPIC_API_KEY", raising=False)
    assert sweep.main(["--configs", "haiku@medium,opus@low", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "2 configs x 10 cases = 20 requests" in out and "opus@low" in out and "haiku@low" not in out
    assert sweep.main(["--configs", "haiku@nope", "--dry-run"]) == 2


# --- Import -------------------------------------------------------------------------

def sweep_folder(tmp_path, name="20261111-173503"):
    """A small sweep results folder: the baseline and haiku@low on two cases."""
    folder = tmp_path / name
    (folder / "plans").mkdir(parents=True)
    rows = []
    for cfg, model, effort in (("baseline", "baseline", "-"), ("haiku@low", "claude-haiku-5-5", "low")):
        for c in ("slow-seller", "low-balance"):
            failed = ["balance"] if cfg == "baseline" and c == "low-balance" else []
            rows.append({"config": cfg, "model": model, "effort": effort, "repeat": 1, "case": c, "status": "scored",
                         "valid": True, "failed": failed, "checks": {"valid plan": "pass"}, "scored": 20, "short": 0,
                         "unsold": 9, "margin": 174.8, "cost_usd": 0.0 if cfg == "baseline" else 0.0014,
                         "input_tokens": None, "output_tokens": None, "seconds": 11.8, "max_tokens": None,
                         "stop_reason": None, "detail": ""})
            if cfg != "baseline":
                (folder / "plans" / sweep.plan_file(cfg, c)).write_text(
                    json.dumps(baseline_json(case(c).state)), encoding="utf-8")
    (folder / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    (folder / "summary.json").write_text(json.dumps({"started": name, "budget": "15", "spent": "0.0028"}),
                                         encoding="utf-8")
    grades = {"judge": "claude-code-blind", "grades": [
        {"case": "slow-seller", "config": "haiku@low", "decision": 1, "reasoning": 1, "invented_numbers": False,
         "why": "keeps the dead product"},
        {"case": "low-balance", "config": "haiku@low", "decision": 3, "reasoning": 2, "invented_numbers": True,
         "why": "stays within $8"}]}
    (folder / "grades.json").write_text(json.dumps(grades), encoding="utf-8")
    return folder


def test_an_import_stores_the_sweep_and_its_grades(api, conn, tmp_path, capsys):
    folder = sweep_folder(tmp_path)
    assert main(["import", "--dir", str(folder), "--grades", str(folder / "grades.json"), "--note", "first"]) == 0
    assert "4 answers, 2 graded" in capsys.readouterr().out
    run = only_run(conn)
    assert (run["source"], run["status"], run["judge"], run["configs"]) == ("import", "done", "claude-code-blind",
                                                                           ["haiku@low"])
    assert run["spent_usd"] == Decimal("0.0028") and run["budget_usd"] == Decimal(15)
    assert run["note"].startswith("first. Imported from 20261111-173503.") and "as of the import" in run["note"]
    assert run["started_at"].strftime("%Y-%m-%d") == "2026-11-11" and run["results_dir"] == str(folder.resolve())
    rows = bench_rows(conn, run["id"])
    control = rows[("baseline", "low-balance")]
    assert control["control"] and control["judge_ok"] is None and control["failed"] == ["balance"]
    graded = rows[("haiku@low", "low-balance")]
    assert (graded["decision"], graded["reasoning"], graded["invented"], graded["judge_ok"]) == (3, 2, True, True)
    assert graded["plan"]["slots"] and graded["cost_usd"] == Decimal("0.0014")
    capsys.readouterr()
    assert main(["report"]) == 0
    out = capsys.readouterr().out
    # haiku: (1+1 + 3+2) / 10 = 70%; the control wasn't graded.
    assert re.search(r"haiku@low\s+2/2\s+70\.0%\s+1\s+0\s+1", out)
    assert "control (the baseline) wasn't graded" in out


def test_the_same_folder_is_never_imported_twice(api, conn, tmp_path, capsys):
    folder = sweep_folder(tmp_path)
    assert main(["import", "--dir", str(folder)]) == 0
    assert main(["import", "--dir", str(folder), "--grades", str(folder / "grades.json")]) == 2
    assert "already imported, as bench run" in capsys.readouterr().err and len(store.runs(conn)) == 1


def test_bad_grades_are_refused_whole(api, conn, tmp_path, capsys):
    folder = sweep_folder(tmp_path)
    bad = json.loads((folder / "grades.json").read_text(encoding="utf-8"))
    bad["grades"][0]["decision"] = 4
    (folder / "bad.json").write_text(json.dumps(bad), encoding="utf-8")
    assert main(["import", "--dir", str(folder), "--grades", str(folder / "bad.json")]) == 2
    stray = json.loads((folder / "grades.json").read_text(encoding="utf-8"))
    stray["grades"][0]["config"] = "opus@max"
    (folder / "stray.json").write_text(json.dumps(stray), encoding="utf-8")
    assert main(["import", "--dir", str(folder), "--grades", str(folder / "stray.json")]) == 2
    assert store.runs(conn) == []


# --- Report across runs -------------------------------------------------------------

def rec(config, case_name, decision, reasoning=2, failed=(), cost="0.0010", control=False, judge_ok=True,
        status="scored"):
    if status != "scored" or decision is None:   # not answered, or answered but never judged
        decision = reasoning = judge_ok = None
    return {"config": config, "model": "claude-haiku-5-5", "effort": "low", "repeat": 1, "case_name": case_name,
            "control": control, "status": status, "valid": True if status == "scored" else None,
            "failed": list(failed), "checks": {}, "cost_usd": Decimal(cost),
            "seconds": Decimal("2.0") if status == "scored" else None, "decision": decision,
            "reasoning": reasoning, "invented": False, "judge_why": "", "judge_ok": judge_ok}


def unjudged(config, case_name, **kw):
    return rec(config, case_name, None, **kw)


def stored_run(conn, records, prompt="p1", cases="c1", sha="aaaaaaa1", judge_hash="j1",
               judge="claude-opus-5-5@medium", started=None, source="bench"):
    fields = dict(status="done", prompt_hash=prompt, cases_hash=cases, cases=["x", "y"],
                  configs=sorted({r["config"] for r in records if not r["control"]}),
                  judge=judge, judge_hash=judge_hash, git_sha=sha, git_branch="b",
                  git_dirty=False, spent_usd=Decimal("0.01"), source=source)
    if started is not None:
        fields.update(started_at=started, finished_at=started)
    if source == "import":
        fields["results_dir"] = f"/imports/{started}"
    run_id = store.start_run(conn, **fields)
    store.store_results(conn, run_id, records)
    return run_id


def test_the_report_compares_with_the_last_run_and_lists_every_regression(api, conn, capsys):
    first = stored_run(conn, [rec("haiku@low", "x", 3), rec("haiku@low", "y", 3),
                              rec("sonnet@low", "x", 3), rec("sonnet@low", "y", 2),
                              rec("baseline", "x", 0, 1, control=True), rec("baseline", "y", 0, 1, control=True)])
    second = stored_run(conn, [
        rec("haiku@low", "x", 3, failed=["balance"], cost="0.0020"),   # a new hard-check failure; cost doubled
        rec("haiku@low", "y", 1, 1, cost="0.0020"),                      # 3 -> 1; score falls 30 points
        rec("sonnet@low", "x", 3), rec("sonnet@low", "y", 2),            # unchanged
        rec("baseline", "x", 0, 1, control=True), rec("baseline", "y", 0, 1, control=True)],
        prompt="p2", sha="bbbbbbb2")
    assert main(["report"]) == 0
    out = capsys.readouterr().out
    assert f"Bench run {second} (bench)" in out
    assert re.search(rf"haiku@low\s+2/2\s+70\.0%.*{first}\s+-30\.0", out)
    assert re.search(rf"sonnet@low\s+2/2\s+90\.0%.*{first}\s+\+0\.0", out)
    assert "REGRESSIONS (4):" in out
    assert "haiku@low: score fell 30.0 points (100.0% -> 70.0%)" in out
    assert "haiku@low/y: decision went from 3 to 1" in out
    assert "haiku@low/x: now fails balance" in out
    assert "haiku@low: cost per decision up 100% ($0.0010 -> $0.0020)" in out
    assert f"prompt changed since run {first}" in out and f"code changed since run {first} (aaaaaaa -> bbbbbbb)" in out
    assert "sonnet@low:" not in out.split("REGRESSIONS")[1]
    assert main(["report", "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["compared"]["haiku@low"] == {"run": first, "score_before": 100.0, "delta": -30.0, "on": 2,
                                            "judged_before": 2, "checks_run": first}
    assert len(rep["regressions"]) == 4 and rep["run"]["id"] == second
    assert main(["report", "--run", str(first)]) == 0
    assert "No earlier finished run on the same cases" in capsys.readouterr().out


def test_runs_on_other_cases_or_unfinished_are_not_compared(api, conn, capsys):
    stored_run(conn, [rec("haiku@low", "x", 3), rec("haiku@low", "y", 3)], cases="c-old")
    running = stored_run(conn, [rec("haiku@low", "x", 0), rec("haiku@low", "y", 0)])
    conn.execute("update bench_runs set status = 'running' where id = %s", (running,))
    latest = stored_run(conn, [rec("haiku@low", "x", 1), rec("haiku@low", "y", 1)])
    assert main(["report"]) == 0
    out = capsys.readouterr().out
    assert f"Bench run {latest}" in out and "No earlier finished run on the same cases" in out
    assert f"cases changed since run {latest - 2}" in out


def test_a_failed_control_keeps_that_case_out_of_the_comparison(api, conn, capsys):
    stored_run(conn, [rec("haiku@low", "x", 3), rec("haiku@low", "y", 3)])
    stored_run(conn, [rec("haiku@low", "x", 3), rec("haiku@low", "y", 0, 0, judge_ok=False),
                      rec("baseline", "y", 3, 2, control=True, judge_ok=False)])
    assert main(["report"]) == 0
    out = capsys.readouterr().out
    assert "Judge failed its control on y" in out and "No regressions" in out


def test_history_shows_each_configs_score_run_by_run(api, conn, capsys):
    a = stored_run(conn, [rec("haiku@low", "x", 3), rec("haiku@low", "y", 3)], prompt="aaaa1111")
    b = stored_run(conn, [rec("haiku@low", "x", 2), rec("haiku@low", "y", 3), rec("opus@low", "x", 3)],
                   prompt="bbbb2222")
    assert main(["report", "--history", "5"]) == 0
    out = capsys.readouterr().out.split("History")[1]
    assert re.search(rf"{a}\s+\S+\s+aaaaaaa\s+aaaa1111\s+c1\s+claude-opus-5-5@medium j1\s+100\.0%", out)
    assert re.search(rf"{b}\s+\S+\s+aaaaaaa\s+bbbb2222\s+c1\s+claude-opus-5-5@medium j1\s+90\.0%\s+100\.0%", out)
    assert main(["report", "--history", "5", "--json"]) == 0
    hist = json.loads(capsys.readouterr().out)["history"]
    assert [h["run"] for h in hist] == [a, b] and hist[1]["scores"] == {"haiku@low": 90.0, "opus@low": 100.0}


def test_list_shows_runs_newest_first(api, conn, tmp_path, capsys):
    folder = sweep_folder(tmp_path, "20261020-120000")
    assert main(["import", "--dir", str(folder), "--note", "the first sweep"]) == 0
    later = stored_run(conn, [rec("haiku@low", "x", 3)])
    capsys.readouterr()
    assert main(["list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[1].startswith(str(later)) and "import" in lines[2] and "the first sweep" in lines[2]


def test_report_with_no_runs_says_so(api, conn, capsys):
    assert main(["report"]) == 1
    assert "No finished bench run yet" in capsys.readouterr().err


def test_the_report_only_reads(api, conn):
    stored_run(conn, [rec("haiku@low", "x", 3)])
    before = conn.execute("select count(*) from bench_runs").fetchone()[0]
    assert main(["report"]) == 0 and main(["list"]) == 0
    assert conn.execute("select count(*) from bench_runs").fetchone()[0] == before


# --- Hashes -------------------------------------------------------------------------

def test_hashes_move_when_the_inputs_do(monkeypatch):
    cases = all_cases()
    assert store.cases_hash(cases) == store.cases_hash(all_cases())
    assert store.cases_hash(cases) != store.cases_hash(cases[:-1])
    assert store.cases_hash(cases) != store.cases_hash(list(reversed(cases)))
    before = store.prompt_hash()
    from vending.replay import model

    monkeypatch.setattr(model, "SYSTEM", model.SYSTEM + " Be brief.")
    assert store.prompt_hash() != before
    assert re.fullmatch(r"[0-9a-f]{64}", judging.judge_hash())


# --- Review fixes: what a score is compared with ------------------------------------

@pytest.mark.parametrize("between", ["judge none", "nothing judged"])
def test_a_run_with_no_grades_in_between_never_hides_a_score_regression(api, conn, capsys, between):
    first = stored_run(conn, [rec("haiku@low", "x", 3), rec("haiku@low", "y", 3)])
    if between == "judge none":
        stored_run(conn, [unjudged("haiku@low", "x"), unjudged("haiku@low", "y")], judge="none", judge_hash=None)
    else:   # the same judge, but the purse or the judge dropped every case
        stored_run(conn, [unjudged("haiku@low", "x"), unjudged("haiku@low", "y")])
    third = stored_run(conn, [rec("haiku@low", "x", 0, 0), rec("haiku@low", "y", 3)])
    assert main(["report"]) == 0
    out = capsys.readouterr().out
    assert f"Bench run {third}" in out
    assert "haiku@low: score fell 50.0 points (100.0% -> 50.0%)" in out
    assert "haiku@low/x: decision went from 3 to 0" in out
    assert re.search(rf"haiku@low\s+2/2\s+50\.0%.*\s{first}\D.*-50\.0", out)
    assert "No regressions" not in out


def test_scores_are_only_compared_under_the_same_judge(api, conn, capsys):
    stored_run(conn, [rec("haiku@low", "x", 3), rec("haiku@low", "y", 3)], judge="claude-code-blind",
               judge_hash=None, source="import")
    second = stored_run(conn, [rec("haiku@low", "x", 1, 1), rec("haiku@low", "y", 3)])
    assert main(["report"]) == 0
    out = capsys.readouterr().out
    assert f"Bench run {second}" in out and "REGRESSIONS" not in out
    assert "No regressions against the runs compared." not in out
    assert "haiku@low: no earlier run on the same cases was graded by this judge" in out
    assert f"judge changed since run {second - 1}" in out
    assert main(["report", "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["compared"]["haiku@low"]["delta"] is None and rep["compared"]["haiku@low"]["run"] is None
    assert rep["compared"]["haiku@low"]["checks_run"] == second - 1


def test_hard_checks_and_cost_still_compare_with_the_plain_previous_run(api, conn, capsys):
    stored_run(conn, [rec("haiku@low", "x", 3), rec("haiku@low", "y", 3)])
    middle = stored_run(conn, [unjudged("haiku@low", "x"), unjudged("haiku@low", "y")], judge="none",
                        judge_hash=None)
    stored_run(conn, [rec("haiku@low", "x", 3, failed=["balance"]), rec("haiku@low", "y", 3)])
    assert main(["report"]) == 0
    out = capsys.readouterr().out
    assert "haiku@low/x: now fails balance" in out
    assert main(["report", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["compared"]["haiku@low"]["checks_run"] == middle


# --- Review fixes: unanswered cases ---------------------------------------------------

def test_cases_a_config_failed_to_answer_count_against_it(api, conn, capsys):
    first = stored_run(conn, [rec("haiku@low", "a", 3), rec("haiku@low", "b", 0, 0), rec("haiku@low", "c", 3)])
    stored_run(conn, [rec("haiku@low", "a", 3), unjudged("haiku@low", "b", status="error"),
                      unjudged("haiku@low", "c", status="dropped")])
    assert main(["report"]) == 0
    out = capsys.readouterr().out
    # The score is on the one case judged in both runs, not 66.7% -> 100.0% (+33.3).
    assert "+33.3" not in out
    assert re.search(rf"haiku@low\s+1/3\s+100\.0%.*\s1\s+1\s+\$\S+\s+\S+\s+{first}\s+\+0\.0", out)
    assert f"haiku@low/b: answered in run {first}, error now" in out
    assert f"haiku@low/c: answered in run {first}, dropped now" in out
    assert f"haiku@low: 2 of 3 plans not judged now, 0 in run {first}" in out
    assert "No regressions" not in out


# --- Review fixes: order runs by when they ran -----------------------------------------

def test_an_older_sweep_imported_later_takes_its_place_in_time(api, conn, capsys):
    from datetime import datetime, timedelta

    from vending.harness import EASTERN

    day = datetime(2026, 11, 12, 9, 0, tzinfo=EASTERN)
    runs = [stored_run(conn, [rec("haiku@low", "x", 3)], started=day + timedelta(days=i)) for i in range(3)]
    old = stored_run(conn, [rec("haiku@low", "x", 0, 0)], started=day - timedelta(days=1), source="import")
    assert old > runs[-1]
    assert main(["report"]) == 0
    out = capsys.readouterr().out
    assert f"Bench run {runs[-1]} (bench)" in out and "REGRESSIONS" not in out
    assert main(["report", "--run", str(runs[0]), "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["compared"]["haiku@low"]["run"] == old and rep["compared"]["haiku@low"]["delta"] == 100.0
    assert main(["report", "--history", "5", "--json"]) == 0
    assert [h["run"] for h in json.loads(capsys.readouterr().out)["history"]] == [old, *runs]


def test_history_names_the_judge_and_marks_where_the_trend_breaks(api, conn, capsys):
    a = stored_run(conn, [rec("haiku@low", "x", 3)], judge="claude-code-blind", judge_hash=None)
    b = stored_run(conn, [rec("haiku@low", "x", 3)])
    c = stored_run(conn, [rec("haiku@low", "x", 3)])
    d = stored_run(conn, [rec("haiku@low", "x", 3)], cases="c2")
    assert main(["report", "--history", "5"]) == 0
    out = capsys.readouterr().out.split("History")[1]
    assert "Judge" in out.splitlines()[1]
    assert re.search(rf"^{a}\s.*claude-code-blind\s+100\.0%$", out, re.M)
    assert re.search(rf"^{b}\s.*claude-opus-5-5@medium j1\s+100\.0%\s+new judge$", out, re.M)
    assert re.search(rf"^{c}\s.*100\.0%$", out, re.M)
    assert re.search(rf"^{d}\s.*100\.0%\s+new cases$", out, re.M)


# --- Review fixes: a run's own record --------------------------------------------------

def test_a_store_failure_after_spending_still_marks_the_run_stopped(api, conn, monkeypatch, results_dir, capsys):
    api.respond = Responder()

    def broken(*args, **kwargs):
        raise ValueError("jsonb can't hold a NUL")

    monkeypatch.setattr(store, "store_results", broken)
    with pytest.raises(ValueError):
        main(["run", "--configs", "haiku@low", "--cases", "slow-seller", "--judge", "none", "--budget", "1"])
    run = only_run(conn)
    spent = conn.execute("select sum(cost_usd) from model_calls").fetchone()[0]
    assert run["status"] == "stopped" and run["finished_at"] and spent > 0 and run["spent_usd"] == spent
    assert "Couldn't store the answers" in capsys.readouterr().err


def test_a_failure_right_after_the_run_row_still_marks_it_stopped(api, conn, monkeypatch, results_dir):
    real = db.connect

    class NoResultsDir:
        """A connection that can't record the results folder, as if the disk or database went away."""

        def __init__(self, inner):
            self.inner = inner

        def __enter__(self):
            self.inner.__enter__()
            return self

        def __exit__(self, *exc):
            return self.inner.__exit__(*exc)

        def execute(self, sql, *args, **kwargs):
            if "set results_dir" in sql:
                raise OSError("the disk went away")
            return self.inner.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    monkeypatch.setattr(db, "connect", lambda: NoResultsDir(real()))
    with pytest.raises(OSError):
        main(RUN)
    run = only_run(conn)
    assert run["status"] == "stopped" and run["spent_usd"] == 0 and api.posts == []


def test_a_spent_daily_cap_refuses_the_run_before_it_starts(api, conn, results_dir, capsys):
    from vending import config

    config.set_value(conn, "model_test_daily_cap_cents", 100, "test")
    spend_earlier_today(conn, Decimal("1.00"))
    assert main(RUN + ["--dry-run"]) == 0
    assert "nothing left: a run would be refused" in capsys.readouterr().out
    assert main(RUN) == 1
    assert "used up, so nothing was sent" in capsys.readouterr().err
    assert store.runs(conn) == [] and api.posts == [] and api.counts == [] and not results_dir.exists()


def test_a_run_where_no_model_answered_is_stopped_not_done(api, conn, results_dir, capsys):
    api.respond = Responder()
    # $0.001 can't hold one Haiku answer's worst case, so every answer is dropped unsent.
    assert main(["run", "--configs", "haiku@low", "--cases", "slow-seller", "--judge", "opus@low",
                 "--budget", "0.001"]) == 1
    run = only_run(conn)
    assert run["status"] == "stopped" and run["finished_at"] and api.posts == []
    assert {r["status"] for r in store.results(conn, run["id"]) if not r["control"]} == {"dropped"}
    assert "No model answered" in capsys.readouterr().out


# --- Review fixes: the judge waits for room in the purse ------------------------------

def test_judge_requests_wait_for_the_purse_instead_of_dropping_cases(api, conn):
    from concurrent.futures import ThreadPoolExecutor

    from vending.replay import model

    api.respond = Responder()
    api.delay = 0.5   # both requests try to reserve while the first is still in flight
    work = []
    for name in ("slow-seller", "catalog-request"):
        c = case(name)
        entries = [judging.Entry("baseline", 1, judging.plan_json(Baseline().decide(c.state)), {}),
                   judging.Entry("haiku@low", 1, {**baseline_json(c.state), "reason": "plan-A"}, {})]
        work.append((c, entries))
    holds = [model.worst_case("claude-opus-5-5", len(judging.SYSTEM + judging.prompt(c, judging.blind(c.name, e)))
                              // judging.CHARS_PER_TOKEN) for c, e in work]
    purse = sweep.Purse(max(holds) * Decimal("1.5"))   # room for one hold at a time, and both real costs
    judge = judging.Judge("claude-opus-5-5", "low", purse, run_id=1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        verdicts = list(pool.map(lambda item: judge.grade(*item), work))
    assert [v.problem for v in verdicts] == ["", ""]
    assert all(len(v.grades) == 2 for v in verdicts)
    assert purse.reserved == 0 and purse.spent == sum(v.cost for v in verdicts) > 0


def test_a_judge_request_that_can_never_fit_is_still_dropped(api, conn):
    api.respond = Responder()
    c = case("slow-seller")
    entries = [judging.Entry("haiku@low", 1, {**baseline_json(c.state), "reason": "plan-A"}, {})]
    judge = judging.Judge("claude-opus-5-5", "low", sweep.Purse(Decimal("0.01")), run_id=1)
    verdict = judge.grade(c, entries)
    assert "couldn't hold the judge's worst case" in verdict.problem and api.posts == []


def test_a_judge_request_is_held_to_a_dollar_like_every_test_request(api, conn, monkeypatch):
    from vending.replay import model

    api.respond = Responder()
    c = case("slow-seller")
    entries = [judging.Entry("haiku@low", 1, {**baseline_json(c.state), "reason": "plan-A"}, {})]
    full = model.worst_case("claude-opus-5-5", len(judging.SYSTEM + judging.prompt(c, judging.blind(c.name, entries)))
                            // judging.CHARS_PER_TOKEN)
    cap = Decimal("0.20")
    assert full > cap   # so the cap is what limits this request
    monkeypatch.setattr(model, "RUN_CAP", cap)
    purse = sweep.Purse(Decimal(5))
    reserved = []
    real_reserve = purse.reserve
    monkeypatch.setattr(purse, "reserve", lambda amount: reserved.append(amount) or real_reserve(amount))
    verdict = judging.Judge("claude-opus-5-5", "low", purse, run_id=1).grade(c, entries)
    assert verdict.problem == "" and reserved == [cap]
    (sent,) = [p for p in api.posts if p.get("system") == judging.SYSTEM]
    assert sent["max_tokens"] < model.MAX_TOKENS   # the harness shortened the answer to fit $0.20
    assert judging.estimate("claude-opus-5-5", 10_000) == cap


def test_a_subset_run_in_between_doesnt_say_the_cases_changed(api, conn, capsys):
    first = stored_run(conn, [rec("haiku@low", "x", 3), rec("haiku@low", "y", 3)])
    stored_run(conn, [rec("haiku@low", "x", 3)], cases="c-subset")   # `--cases x`
    stored_run(conn, [rec("haiku@low", "x", 3), rec("haiku@low", "y", 1, 1)])
    assert main(["report"]) == 0
    out = capsys.readouterr().out
    assert "cases changed since run" not in out
    assert "haiku@low/y: decision went from 3 to 1" in out and re.search(rf"haiku@low\s+2/2.*\s{first}\s", out)
