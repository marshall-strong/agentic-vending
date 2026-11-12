"""The model sweep, against the same local fake of the Anthropic API as the exam's tests.

No test spends money, and every model_calls row goes to the test's throwaway schema.
"""

import json
import re
from decimal import Decimal

import pytest

from tests.test_replay_model import api, baseline_json, case  # noqa: F401  (api is a fixture)
from vending.replay import sweep
from vending.replay.cases import all_cases
from vending.replay.sweep import Config, Purse, configs, main, summarize, worst_case


def answer_with_baseline(fake, name):
    fake.reply = (200, "end_turn", json.dumps(baseline_json(case(name).state)))


def results(out):
    return [json.loads(line) for line in (out / "results.jsonl").read_text(encoding="utf-8").splitlines()]


def test_cheapest_configs_run_first():
    order = [(c.model, c.effort) for c in configs(["opus", "haiku", "sonnet"], ["high", "low"], 1)]
    assert order == [("claude-haiku-5-5", "low"), ("claude-haiku-5-5", "high"),
                     ("claude-sonnet-5-5", "low"), ("claude-sonnet-5-5", "high"),
                     ("claude-opus-5-5", "low"), ("claude-opus-5-5", "high")]


def test_worst_case_is_the_harness_worst_case_on_a_generous_prompt():
    # 8,000 counted pads to 9,000 in; 16,000 out; x 1.1 for US-only routing.
    assert worst_case("claude-opus-5-5") == Decimal("0.3916")     # ($0.036 + $0.32) x 1.1
    assert worst_case("claude-haiku-5-5") == Decimal("0.009790")   # ($0.0009 + $0.008) x 1.1


def test_the_purse_never_reserves_past_its_budget():
    purse = Purse(Decimal("1.00"))
    assert purse.reserve(Decimal("0.60")) and not purse.reserve(Decimal("0.50"))
    purse.settle(Decimal("0.60"), Decimal("0.10"))
    assert purse.reserve(Decimal("0.50")) and purse.spent == Decimal("0.10")


def test_dry_run_sends_nothing_and_needs_no_key(monkeypatch, capsys):
    monkeypatch.delenv("VENDING_ANTHROPIC_API_KEY", raising=False)
    assert main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "9 configs x 10 cases = 90 requests" in out and "opus@high" in out


def test_bad_arguments_are_refused_before_anything_is_sent(capsys):
    assert main(["--efforts", "turbo", "--dry-run"]) == 2
    assert main(["--budget", "500", "--dry-run"]) == 2
    assert main(["--cases", "no-such-case", "--dry-run"]) == 2
    assert main(["--models", "gpt-5", "--dry-run"]) == 2


def test_a_sweep_scores_every_case_saves_every_plan_and_the_baseline(api, conn, tmp_path, capsys):
    answer_with_baseline(api, "catalog-request")
    out = tmp_path / "run"
    assert main(["--models", "haiku", "--efforts", "low", "--cases", "catalog-request,slow-seller",
                 "--budget", "1", "--out", str(out), "--workers", "2"]) == 0
    rows = results(out)
    assert {(r["config"], r["case"]) for r in rows} == {
        ("baseline", "catalog-request"), ("baseline", "slow-seller"),
        ("haiku@low", "catalog-request"), ("haiku@low", "slow-seller")}
    model_rows = [r for r in rows if r["config"] == "haiku@low"]
    assert all(r["status"] == "scored" and r["valid"] for r in model_rows)
    assert all(r["input_tokens"] == 3000 and r["output_tokens"] == 2000 for r in model_rows)
    # 3,000 in at $0.10/M + 2,000 out at $0.50/M
    assert all(r["cost_usd"] == pytest.approx(0.0013) for r in model_rows)
    assert len(list((out / "plans").glob("haiku_low__*.json"))) == 2
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert Decimal(summary["spent"]) == Decimal("0.0026")
    assert [c["config"] for c in summary["configs"]] == ["baseline", "haiku@low"]
    assert (out / "summary.csv").exists() and "Spent $0.0026" in capsys.readouterr().out
    assert all(p["model"] == "claude-haiku-5-5" and p["output_config"]["effort"] == "low" for p in api.posts)
    # Every request is a test call in model_calls, labelled with the sweep, config and case.
    rows = conn.execute("select label, run_id, purpose, cost_usd from model_calls order by label").fetchall()
    assert [re.fullmatch(r"sweep (\d{8}-\d{6}) haiku@low (\S+) 2026-10-02", r[0]).group(2) for r in rows] == [
        "catalog-request", "slow-seller"]
    (run_id,) = {r[1] for r in rows}
    assert re.fullmatch(r"sweep-\d{8}-\d{6}-haiku@low", run_id) and {r[2] for r in rows} == {"test"}
    assert sum(r[3] for r in rows) == Decimal("0.0026")


def test_a_budget_too_small_drops_requests_and_says_so(api, tmp_path, capsys):
    answer_with_baseline(api, "slow-seller")
    out = tmp_path / "run"
    # An Opus worst case is $0.3916, so a $0.30 sweep can't send one; a Haiku one still fits.
    assert main(["--models", "haiku,opus", "--efforts", "low", "--cases", "slow-seller,catalog-request",
                 "--budget", "0.30", "--out", str(out), "--workers", "1"]) == 0
    rows = results(out)
    assert {r["status"] for r in rows if r["config"] == "opus@low"} == {"dropped"}
    assert {r["status"] for r in rows if r["config"] == "haiku@low"} == {"scored"}
    assert {p["model"] for p in api.posts} == {"claude-haiku-5-5"}
    assert "Dropped, not sent (2)" in capsys.readouterr().out


def test_a_worst_case_is_held_while_a_request_is_in_flight():
    # Reserved, not yet settled: a second reservation sees the first one's worst case.
    purse = Purse(Decimal("0.50"))
    assert purse.reserve(worst_case("claude-opus-5-5"))
    assert not purse.reserve(worst_case("claude-opus-5-5"))


def test_a_failed_request_is_an_error_row_and_the_sweep_goes_on(api, tmp_path, capsys):
    api.turns = [(500, "end_turn", "{}")]
    answer_with_baseline(api, "slow-seller")
    out = tmp_path / "run"
    assert main(["--models", "haiku", "--efforts", "low", "--cases", "slow-seller,catalog-request",
                 "--budget", "1", "--out", str(out), "--workers", "1"]) == 0
    rows = [r for r in results(out) if r["config"] == "haiku@low"]
    assert sorted(r["status"] for r in rows) == ["error", "scored"]
    failed = next(r for r in rows if r["status"] == "error")
    assert "worst case" in failed["detail"] and failed["cost_usd"] > 0   # a 5xx may have billed
    assert "Errors (1)" in capsys.readouterr().out


def test_an_answer_that_isnt_json_fails_valid_plan_and_is_kept_for_reading(api, tmp_path):
    api.reply = (200, "max_tokens", '{"visit": "scheduled", "slots": [')
    out = tmp_path / "run"
    assert main(["--models", "haiku", "--efforts", "low", "--cases", "slow-seller",
                 "--budget", "1", "--out", str(out)]) == 0
    row = next(r for r in results(out) if r["config"] == "haiku@low")
    assert row["status"] == "scored" and row["valid"] is False and row["stop_reason"] == "max_tokens"
    saved = json.loads(next((out / "plans").glob("haiku_low__*.json")).read_text(encoding="utf-8"))
    assert "not_json" in saved


def test_summary_counts_per_config():
    rows = [sweep.Row("a", "m", "low", 1, "x", "scored", valid=True, cost_usd=0.1, output_tokens=100, seconds=2.0,
                      scored=20, short=1, unsold=2, margin=3.0),
            sweep.Row("a", "m", "low", 1, "y", "scored", valid=False, failed=["valid plan"], cost_usd=0.3,
                      output_tokens=300, seconds=4.0),
            sweep.Row("a", "m", "low", 1, "z", "dropped")]
    (s,) = summarize(rows)
    assert (s["cases"], s["answered"], s["valid"], s["all_checks_pass"], s["dropped"]) == (3, 2, 1, 1, 1)
    assert s["cost_per_decision"] == 0.2 and s["avg_output_tokens"] == 200 and s["margin"] == 3.0


def test_there_are_ten_cases_to_sweep():
    assert len(all_cases()) == 10


# --- Found by the blind review (11 Nov) ----------------------------------------------

def test_a_long_prompt_cannot_overrun_the_sweep_budget(api, conn, tmp_path):
    # The purse holds an 8,000-token worst case; a 30,000-token prompt must fit inside that hold, not overrun it.
    api.tokens, api.usage = 30_000, (30_000, 2_000)   # the API never answers past max_tokens
    answer_with_baseline(api, "catalog-request")
    out = tmp_path / "run"
    assert main(["--models", "opus", "--efforts", "low", "--cases", "catalog-request",
                 "--budget", "0.40", "--out", str(out), "--workers", "1"]) == 0
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert Decimal(summary["spent"]) <= Decimal("0.40")
    (worst,) = conn.execute("select worst_case_usd from model_calls").fetchone()
    assert worst <= sweep.worst_case("claude-opus-5-5")   # the reservation itself fits the hold
    # The harness lowered the answer's room so its worst case fits the $0.3916 hold.
    assert api.posts[0]["max_tokens"] < 16_000


def test_a_config_cant_look_better_by_failing_a_case():
    good = dict(valid=True, scored=20, short=0, unsold=2, margin=10.0, cost_usd=0.1)
    rows = [sweep.Row("baseline", "baseline", "-", 1, "x", "scored", **good),
            sweep.Row("baseline", "baseline", "-", 1, "y", "scored", **{**good, "short": 5, "margin": 2.0}),
            sweep.Row("a", "m", "low", 1, "x", "scored", **good),
            # "a" fails case y: before the fix, its totals simply left y out and beat the baseline.
            sweep.Row("a", "m", "low", 1, "y", "scored", valid=False, failed=["valid plan"], cost_usd=0.1)]
    base, a = summarize(rows)
    assert sweep.compared_cases(rows) == ["x"]
    assert (base["compared"], base["short_units"], base["margin"]) == (1, 0, 10.0)
    assert (a["compared"], a["short_units"], a["margin"]) == (1, 0, 10.0)
    assert a["valid"] == 1 and base["valid"] == 2      # the failure still shows, where it belongs


def test_the_same_model_or_effort_twice_runs_once():
    assert [(c.model, c.effort) for c in configs(["haiku", "claude-haiku-5-5"], ["low", "low"], 1)] == [
        ("claude-haiku-5-5", "low")]


def test_a_results_folder_is_never_reused(api, tmp_path, capsys):
    out = tmp_path / "run"
    out.mkdir()
    (out / "results.jsonl").write_text("{}\n", encoding="utf-8")
    assert main(["--models", "haiku", "--efforts", "low", "--cases", "slow-seller", "--out", str(out)]) == 2
    assert "already holds" in capsys.readouterr().err and api.posts == []


def test_the_daily_cap_shows_up_front_and_its_drops_say_so(api, conn, tmp_path, capsys):
    from vending import config

    config.set_value(conn, "model_test_daily_cap_cents", 100, "test")
    conn.execute("insert into model_calls (purpose, run_id, model, key_env, status, max_tokens, worst_case_usd,"
                 " cost_usd, recorded_by) values ('test', 'x', 'claude-haiku-5-5', 'VENDING_ANTHROPIC_API_KEY',"
                 " 'ok', 1, 0.999, 0.999, 'test')")
    answer_with_baseline(api, "slow-seller")
    assert main(["--models", "haiku", "--efforts", "low", "--cases", "slow-seller", "--dry-run"]) == 0
    assert "Today's team test cap: $1.00 of $1.00 spent, $0.00 left" in capsys.readouterr().out
    out = tmp_path / "run"
    assert main(["--models", "haiku", "--efforts", "low", "--cases", "slow-seller", "--out", str(out)]) == 0
    row = next(r for r in results(out) if r["config"] == "haiku@low")
    assert row["status"] == "dropped" and row["detail"].startswith("cap:") and api.posts == []
    assert "(daily or run cap)" in capsys.readouterr().out


def test_no_database_means_no_sweep(api, monkeypatch, tmp_path, capsys):
    from vending import db

    def down():
        raise OSError("database is down")

    monkeypatch.setattr(db, "connect", down)
    assert main(["--models", "haiku", "--efforts", "low", "--out", str(tmp_path / "run")]) == 1
    assert "unknown, the database can't be read" in capsys.readouterr().out and api.counts == []


def test_results_go_beside_the_repo_wherever_the_command_runs():
    from vending import settings
    from vending.replay import agent

    assert sweep.RESULTS_DIR.is_absolute() and sweep.RESULTS_DIR.parent == settings.REPO_ROOT
    assert agent.RESULTS_DIR.is_absolute() and agent.RESULTS_DIR.parent.parent == settings.REPO_ROOT


def test_a_plan_that_breaks_a_rule_earns_no_outcome_credit():
    # The first sweep's baseline overspent an $8 balance and still topped the margin column.
    good = dict(valid=True, scored=20, short=0, unsold=0, margin=10.0, cost_usd=0.0)
    rows = [sweep.Row("baseline", "baseline", "-", 1, "x", "scored", **good),
            sweep.Row("baseline", "baseline", "-", 1, "low-balance", "scored", failed=["balance"], **{**good, "margin": 184.0}),
            sweep.Row("a", "m", "low", 1, "x", "scored", **good),
            sweep.Row("a", "m", "low", 1, "low-balance", "scored", **{**good, "short": 92, "margin": 63.0})]
    assert sweep.compared_cases(rows) == ["x"]
    base, a = summarize(rows)
    assert base["margin"] == a["margin"] == 10.0

