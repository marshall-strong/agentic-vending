"""The usage report: what our Anthropic keys were used for, read from model_calls."""

import json
from datetime import datetime, timezone
from decimal import Decimal

from vending.usage import main

NOW = datetime(2026, 11, 11, 20, 0, tzinfo=timezone.utc)          # 3 pm Eastern
TODAY = datetime(2026, 11, 11, 14, 0, tzinfo=timezone.utc)
THIS_WEEK = datetime(2026, 11, 8, 14, 0, tzinfo=timezone.utc)
LONG_AGO = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)


def add(conn, at, purpose="test", model="claude-haiku-5-5", effort="low", cost="0.01", status="ok",
        key="VENDING_ANTHROPIC_API_KEY", label="sweep x haiku@low slow-seller", out=100, seconds=2):
    conn.execute(
        "insert into model_calls (at, purpose, label, run_id, model, effort, key_env, status, stop_reason,"
        " input_tokens, output_tokens, max_tokens, worst_case_usd, cost_usd, seconds, recorded_by, host)"
        " values (%s, %s, %s, 'r', %s, %s, %s, %s, 'end_turn', 1000, %s, 16000, 1, %s, %s, 'replay', 'laptop')",
        (at, purpose, label, model, effort, key, status, out, Decimal(cost), seconds))


def test_windows_split_live_and_test_and_count_unsettled_rows(conn, capsys):
    add(conn, TODAY, cost="0.10")
    add(conn, TODAY, purpose="live", model="claude-opus-5-5", effort="medium", cost="0.30")
    add(conn, THIS_WEEK, cost="0.05", status="error")
    add(conn, LONG_AGO, cost="1.00", key="VENDING_PERSONAL_ANTHROPIC_KEY")
    assert main(["--json"], conn=conn, now=NOW) == 0
    rep = json.loads(capsys.readouterr().out)
    today, week, ever = rep["windows"]["today"], rep["windows"]["last 7 days"], rep["windows"]["all time"]
    assert (today["calls"], today["cost_usd"], today["live_usd"], today["test_usd"]) == (2, 0.4, 0.3, 0.1)
    assert (week["calls"], week["error"]) == (3, 1) and (ever["calls"], ever["cost_usd"]) == (4, 1.45)
    keys = {g["group"]: g["cost_usd"] for g in rep["by"]["key"]}
    assert keys == {"VENDING_ANTHROPIC_API_KEY": 0.45, "VENDING_PERSONAL_ANTHROPIC_KEY": 1.0}
    assert rep["by"]["model"][0]["group"] == "claude-haiku-5-5 low"   # biggest spend first


def test_the_table_names_each_purpose_model_key_and_recent_call(conn, capsys):
    add(conn, TODAY, cost="0.10", label="sweep 20261111 haiku@low slow-seller")
    add(conn, TODAY, purpose="live", model="claude-opus-5-5", effort="medium", cost="0.30", label="agent week 3")
    assert main([], conn=conn, now=NOW) == 0
    out = capsys.readouterr().out
    assert "today" in out and "$0.4000" in out and "By model and effort" in out
    assert "claude-opus-5-5 medium" in out and "agent week 3" in out and "VENDING_ANTHROPIC_API_KEY" in out


def test_since_limits_every_figure(conn, capsys):
    add(conn, LONG_AGO, cost="1.00")
    add(conn, TODAY, cost="0.10")
    assert main(["--since", "2026-11-01", "--json"], conn=conn, now=NOW) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["windows"] == {"from 2026-11-01": rep["windows"]["from 2026-11-01"]}
    assert rep["windows"]["from 2026-11-01"]["cost_usd"] == 0.1


def test_an_empty_table_says_so(conn, capsys):
    assert main([], conn=conn, now=NOW) == 0
    assert "No model calls recorded yet." in capsys.readouterr().out


def test_the_report_only_reads(conn, capsys):
    main([], conn=conn, now=NOW)
    assert conn.execute("show default_transaction_read_only").fetchone()[0] == "on"


def test_a_database_without_the_table_names_the_fix(conn, capsys):
    conn.execute("drop table model_calls")
    assert main([], conn=conn, now=NOW) == 1
    assert "vending.db migrate" in capsys.readouterr().err


# --- Found by the blind review (11 Nov) ----------------------------------------------

def test_last_7_days_starts_at_eastern_midnight_across_a_dst_change(conn, capsys):
    # 5 Nov, after DST ended on 1 Nov: the window must start at midnight EDT on 30 Oct, not 1 am.
    add(conn, datetime(2026, 10, 30, 4, 30, tzinfo=timezone.utc), cost="0.25")   # 00:30 EDT, 30 Oct
    assert main(["--json"], conn=conn, now=datetime(2026, 11, 5, 17, 0, tzinfo=timezone.utc)) == 0
    assert json.loads(capsys.readouterr().out)["windows"]["last 7 days"]["cost_usd"] == 0.25


def test_spend_recorded_outside_model_calls_is_shown_not_hidden(conn, capsys):
    from vending import ledger

    ledger.record_tokens(conn, "0.42", "claude-opus-5-5", 1, 1, "live from an older branch")
    assert main([], conn=conn, now=NOW) == 0
    assert "Also 1 token entries in the ledger, $0.4200, with no model_calls row" in capsys.readouterr().out


def test_before_the_opening_every_call_is_development(conn, capsys):
    add(conn, TODAY, purpose="live", cost="0.40", label="agent run 2 (a live test)")
    add(conn, THIS_WEEK, cost="0.05")
    assert main(["--json"], conn=conn, now=NOW) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["competition_start"] is None and rep["split"]["competition"] is None
    dev = rep["split"]["development"]
    assert (dev["calls"], dev["cost_usd"], dev["live_usd"]) == (2, 0.45, 0.4)
    assert main([], conn=conn, now=NOW) == 0
    assert "No ledger opening yet, so every call so far is development spend" in capsys.readouterr().out


def test_the_opening_splits_development_from_the_competition(conn, capsys):
    from vending import ledger
    opened = datetime(2026, 11, 11, 17, 0, tzinfo=timezone.utc)          # noon Eastern
    ledger.record(conn, "opening", 60000, "the $600", by="test", at=opened)
    add(conn, THIS_WEEK, purpose="live", cost="0.40", label="agent run 2 (a live test)")   # before: development
    add(conn, LONG_AGO, cost="2.43")                                                         # the sweep: development
    add(conn, TODAY, purpose="live", cost="0.30")                                            # 9 am: still before
    add(conn, datetime(2026, 11, 11, 18, 0, tzinfo=timezone.utc), purpose="live", cost="0.50")  # after: competition
    assert main(["--json"], conn=conn, now=NOW) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["competition_start"].startswith("2026-11-11T12:00")
    dev = rep["split"]["development"]
    comp = rep["split"]["competition"]
    assert (dev["calls"], dev["cost_usd"], dev["live_usd"]) == (3, 3.13, 0.7)
    assert (comp["calls"], comp["cost_usd"], comp["live_usd"]) == (1, 0.5, 0.5)
    assert main([], conn=conn, now=NOW) == 0
    text = capsys.readouterr().out
    assert "The competition starts at the ledger's opening" in text and "Development, before it" in text


def test_a_call_at_the_opening_instant_is_competition_and_the_ledger_calls_earlier_tokens_development(conn, capsys):
    from vending import ledger
    opened = datetime(2026, 11, 11, 17, 0, tzinfo=timezone.utc)
    ledger.record(conn, "opening", 60000, "the $600", by="test", at=opened)
    ledger.record_tokens(conn, "0.40", "claude-opus-5-5", 1000, 100, "live run 2", at=THIS_WEEK)
    add(conn, opened, purpose="live", cost="0.25")
    assert main(["--json"], conn=conn, now=NOW) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["split"]["competition"]["calls"] == 1 and rep["split"]["development"]["calls"] == 0
    notes = " ".join(ledger.balance(conn, NOW).notes)
    assert "development spend, not in the balance" in notes and "re-date" not in notes
