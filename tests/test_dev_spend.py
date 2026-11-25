"""Development spend: every model call before the ledger's opening, by Eastern week and day, read from
model_calls and the ledger. The pure tests run without a database; the rest use a throwaway schema."""

import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction

from vending import dev_spend, ledger
from vending.dev_spend import Call, parse_detail, parse_reference, summarise

E = dev_spend.EASTERN
OPEN = datetime(2026, 11, 30, 10, 0, tzinfo=E)                  # a Monday morning, Eastern
NOW = datetime(2026, 12, 3, 17, 0, tzinfo=timezone.utc)
BEFORE_OPEN_NOW = datetime(2026, 11, 15, 22, 0, tzinfo=timezone.utc)


def et(day: int, hour: int = 12, month: int = 11) -> datetime:
    return datetime(2026, month, day, hour, tzinfo=E)


def add(conn, at, purpose="test", model="claude-opus-5-5", cost="0.10", status="ok", label="agent run 1 turn 1",
        run="r1", answered_by=None, tokens=(1000, 200), entry=None):
    tin, tout = tokens if status == "ok" else (None, None)
    conn.execute(
        "insert into model_calls (at, purpose, label, run_id, model, answered_by, key_env, status, input_tokens,"
        " cache_read_tokens, output_tokens, max_tokens, worst_case_usd, cost_usd, ledger_entry, recorded_by)"
        " values (%s, %s, %s, %s, %s, %s, 'VENDING_ANTHROPIC_API_KEY', %s, %s, 50, %s, 16000, 1, %s, %s, 'harness')",
        (at, purpose, label, run, model, answered_by, status, tin, tout, Decimal(cost), entry))


def opening(conn, at=OPEN):
    ledger.record(conn, "opening", 60000, "opening capital", "kevin", at=at)


def cents(dollars: str) -> Fraction:
    return Fraction(Decimal(dollars)) * 100


# --- Reading the ledger's own words (pure) ----------------------------------------------------

def test_a_reference_gives_purpose_label_and_whether_it_was_corrected():
    assert parse_reference("test agent run 4 turn 2 msg_01AbC") == ("test", "agent run 4", True)
    # Corrected, but to the worst case: the harness couldn't price the answer, so its cost isn't known.
    assert parse_reference("live sweep x msg_01AbC (answered by unpriced y; kept at worst case)") == (
        "live", "sweep x", False)
    assert parse_reference("live agent run 2 turn 1 pending") == ("live", "agent run 2", False)
    assert parse_reference("test bench 7 estimate: APITimeoutError") == ("test", "bench 7", False)
    assert parse_reference("live msg_01") == ("live", "", True)
    # A person's own `ledger tokens` record names no purpose: only live calls and people's records reach the ledger.
    assert parse_reference("msg_01") == ("live", "", True)
    assert parse_reference(None) == ("live", "", True)
    assert parse_reference("invoice 12 Nov") == ("live", "invoice 12 Nov", True)


def test_a_detail_gives_the_model_and_its_tokens():
    assert parse_detail("claude-sonnet-5-5: 8120 tokens in, 640 out") == ("claude-sonnet-5-5", 8120, 640)
    assert parse_detail("hand-entered refund") == ("unknown", None, None)


# --- Summing (pure) ---------------------------------------------------------------------

def call(at, cost="0.10", purpose="test", model="claude-opus-5-5", label="run", ok=True, run=None):
    return Call(at, purpose, model, label, run or label, cents(cost), ok, None if not ok else 100, None if not ok else 10)


def test_weeks_start_on_the_ledgers_saturday():
    friday, saturday = et(13, 23), et(14, 1)
    r = summarise([call(friday), call(saturday)], None, "week", BEFORE_OPEN_NOW)
    assert [b.start for b in r.buckets] == [date(2026, 11, 7), date(2026, 11, 14)]
    assert all(b.start == ledger.week_of(datetime.combine(b.start, datetime.min.time())) for b in r.buckets)
    assert all(b.start.weekday() == ledger.WEEK_STARTS for b in r.buckets)


def test_a_late_evening_call_is_booked_to_its_eastern_day():
    # 23:30 Eastern on 11 Nov is 04:30 UTC on 12 Nov.
    r = summarise([call(datetime(2026, 11, 12, 4, 30, tzinfo=timezone.utc))], None, "day",
                  datetime(2026, 11, 12, 5, 0, tzinfo=timezone.utc))
    assert r.buckets[0].start == date(2026, 11, 11) and r.buckets[0].total_cents == 10
    assert r.buckets[1].start == date(2026, 11, 12) and r.buckets[1].total_cents == 0     # today, quiet so far


def test_quiet_weeks_are_kept_and_the_running_total_carries_through_them():
    r = summarise([call(et(2), "1.00"), call(et(20), "0.50")], None, "week", BEFORE_OPEN_NOW)
    assert [b.start for b in r.buckets] == [date(2026, 10, 31), date(2026, 11, 7), date(2026, 11, 14)]
    assert [b.total_cents for b in r.buckets] == [100, 0, 50]
    assert [b.cumulative_cents for b in r.buckets] == [100, 100, 150]


def test_without_an_opening_the_weeks_run_to_now():
    r = summarise([call(et(2))], None, "week", datetime(2026, 11, 25, 12, tzinfo=timezone.utc))
    assert r.buckets[-1].start == date(2026, 11, 21)
    assert r.opening_at is None and r.totals.total_cents == 10


def test_nothing_recorded_is_an_empty_report():
    r = summarise([], None, "week", BEFORE_OPEN_NOW)
    assert r.buckets == [] and r.runs == [] and r.totals.total_cents == 0
    assert "No model calls recorded yet." in dev_spend.render(r)


def test_the_demo_is_the_documented_runs_and_says_where_it_came_from():
    r = dev_spend.demo("week", BEFORE_OPEN_NOW)
    assert r.source == "from run notes, not the ledger"
    assert r.totals.total_cents == cents("3.734956")       # $2.43 sweep + $0.80 + $0.40 + $0.08 + $0.024956
    assert r.totals.by_purpose == {"test": cents("3.334956"), "live": cents("0.40")}
    assert r.totals.calls == 99 and r.totals.calls_unknown == 2   # runs 2 and 3 don't say how many calls
    sweep = [x for x in r.runs if x.label.startswith("sweep 20261111-173503")]
    assert len(sweep) == 9 and sum(x.total_cents for x in sweep) == cents("2.43")
    data = dev_spend.to_json(r)
    assert data["source"] == "from run notes, not the ledger" and data["opening_at"] is None


# --- Reading the database ----------------------------------------------------------------------

def test_before_the_opening_is_development_and_test_calls_after_it_are_kept_apart(conn):
    add(conn, et(12), purpose="live", cost="0.40", run="a")          # a live test before the opening: development
    add(conn, et(20), cost="0.25", run="b")
    opening(conn)
    add(conn, et(1, month=12), cost="0.60", run="c")                 # test after the opening: shouldn't happen, flagged
    add(conn, et(2, month=12), purpose="live", cost="5.00", run="d") # the competition's money: not here
    r = dev_spend.weekly(conn, NOW)
    assert r.opening_at == OPEN
    assert r.totals.total_cents == 65 and r.totals.by_purpose == {"live": 40, "test": 25}
    assert sum(b.after_opening_test_cents for b in r.buckets) == 60
    assert sum(b.after_opening_test_calls for b in r.buckets) == 1
    assert r.buckets[-1].cumulative_cents == 65                      # the after-opening test call isn't in it
    assert [x.after_opening for x in r.runs] == [True, False, False]
    assert all(x.label != "" for x in r.runs) and "5.00" not in json.dumps(dev_spend.to_json(r))
    # The week the opening falls in is the last one: no later test call, so the weeks stop there.
    assert r.buckets[-1].start == date(2026, 11, 28)


def test_a_call_at_the_opening_moment_is_the_competitions(conn):
    opening(conn)
    add(conn, OPEN, purpose="live", cost="0.30")
    assert dev_spend.weekly(conn, NOW).totals.total_cents == 0


def test_with_no_opening_yet_everything_is_development(conn):
    add(conn, et(11), cost="0.80")
    add(conn, et(12), purpose="live", cost="0.40", run="r2")
    r = dev_spend.weekly(conn, BEFORE_OPEN_NOW)
    assert r.opening_at is None and r.totals.total_cents == 120
    assert r.buckets[-1].start == date(2026, 11, 14)                 # this week, quiet so far


def test_calls_never_corrected_are_unconfirmed_and_refused_ones_are_left_out(conn):
    add(conn, et(11), cost="0.10")
    add(conn, et(11), cost="0.90", status="pending", run="r2")
    add(conn, et(11), cost="0.70", status="error", run="r3")
    add(conn, et(11), cost="0", status="refused", run="r4")
    add(conn, et(11), cost="0.05", run="r5")
    conn.execute("update model_calls set detail = '(served at priority capacity, not priced; kept at worst case)'"
                 " where run_id = 'r5'")
    # Older code wrote only the ledger; this estimate was never corrected.
    ledger.record_tokens(conn, "0.33", "claude-opus-5-5", 9000, 16000, "test agent run 9 turn 1 pending", at=et(11))
    t = dev_spend.daily(conn, BEFORE_OPEN_NOW).totals
    assert (t.confirmed_cents, t.unconfirmed_cents, t.total_cents) == (10, 198, 208)
    assert t.calls == 5 and t.tokens_in == 1050 and t.tokens_out == 200 and t.tokens_unknown == 4
    assert t.by_purpose == {"test": 10}                              # the splits are of confirmed spend
    out = dev_spend.render(dev_spend.daily(conn, BEFORE_OPEN_NOW))
    assert "$1.9800" in out and "Unconfirmed:" in out


def test_models_and_labels_come_from_each_source_once(conn):
    # The harness's live call: a model_calls row tied to its ledger entry, which must not count twice.
    entry = ledger.record_tokens(conn, "0.20", "claude-opus-5-5", 1050, 200, "live agent run 2 turn 1 msg_01", at=et(12))
    add(conn, et(12), purpose="live", cost="0.20", label="agent run 2 turn 1", run="r2", entry=entry.id,
        answered_by="claude-opus-5-5")
    add(conn, et(12), purpose="live", cost="0.10", label="agent run 2 turn 2", run="r2")
    # A sweep that asked for one model and was answered by another is booked to the one that answered.
    add(conn, et(13), cost="0.05", model="claude-sonnet-5-5", answered_by="claude-haiku-5-5", label="sweep x haiku",
        run="r3")
    # Older code: only a ledger entry, its model in the detail and its label in the reference.
    ledger.record_tokens(conn, "0.07", "claude-sonnet-5-5", 500, 60, "test replay week 3 turn 4 msg_02", at=et(13))
    r = dev_spend.weekly(conn, BEFORE_OPEN_NOW)
    t = r.totals
    assert t.total_cents == 42 and t.calls == 4
    assert t.by_model == {"claude-opus-5-5": 30, "claude-haiku-5-5": 5, "claude-sonnet-5-5": 7}
    assert t.by_label == {"agent run 2": 30, "sweep x haiku": 5, "replay week 3": 7}
    assert t.tokens_in == 1050 * 3 + 500 and t.tokens_out == 200 * 3 + 60
    runs = {x.label: x for x in r.runs}
    assert runs["agent run 2"].calls == 2 and runs["agent run 2"].purpose == "live"
    assert runs["replay week 3"].models == ["claude-sonnet-5-5"]


def test_weekly_sums_equal_daily_sums(conn):
    for i, (day, hour) in enumerate([(2, 9), (6, 23), (7, 1), (13, 22), (14, 0), (20, 12), (29, 8)]):
        add(conn, et(day, hour), cost=f"0.{i + 1}3", run=f"r{i}", purpose="live" if i % 3 == 0 else "test",
            model="claude-opus-5-5" if i % 2 else "claude-haiku-5-5")
    add(conn, et(25), cost="0.41", status="error", run="e")
    opening(conn)
    add(conn, et(1, month=12), cost="0.60", run="later")
    weeks, days = dev_spend.weekly(conn, NOW), dev_spend.daily(conn, NOW)
    by_week: dict[date, list] = defaultdict(list)
    for d in days.buckets:
        by_week[ledger.week_of(datetime.combine(d.start, datetime.min.time()))].append(d)
    assert [w.start for w in weeks.buckets] == sorted(by_week)
    for w in weeks.buckets:
        ds = by_week[w.start]
        assert w.total_cents == sum(d.total_cents for d in ds)
        assert w.unconfirmed_cents == sum(d.unconfirmed_cents for d in ds)
        assert w.calls == sum(d.calls for d in ds) and w.tokens_in == sum(d.tokens_in for d in ds)
        assert w.after_opening_test_cents == sum(d.after_opening_test_cents for d in ds)
        for key in ("live", "test"):
            assert w.by_purpose.get(key, 0) == sum(d.by_purpose.get(key, 0) for d in ds)
        assert w.cumulative_cents == ds[-1].cumulative_cents
    assert weeks.totals.total_cents == days.totals.total_cents == sum(w.total_cents for w in weeks.buckets)


def test_a_database_without_model_calls_reads_the_ledger_alone(conn):
    ledger.record_tokens(conn, "0.12", "claude-opus-5-5", 100, 10, "live agent run 1 msg_01", at=et(11))
    conn.execute("drop table model_calls cascade")
    assert dev_spend.weekly(conn, BEFORE_OPEN_NOW).totals.total_cents == 12


# --- The CLI and the page ----------------------------------------------------------------------

def test_the_cli_prints_json_and_a_table_and_only_reads(conn, capsys):
    add(conn, et(11), cost="0.80", label="agent run 1 turn 3")
    assert dev_spend.main(["--json"], conn=conn, now=BEFORE_OPEN_NOW) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["unit"] == "week" and data["totals"]["total_usd"] == "0.8" and data["runs"][0]["label"] == "agent run 1"
    assert data["buckets"][0]["start"] == "2026-11-07" and data["source"] == "database"
    assert dev_spend.main(["--daily"], conn=conn, now=BEFORE_OPEN_NOW) == 0
    out = capsys.readouterr().out
    assert "Wed 11 Nov" in out and "$0.8000" in out and "No ledger opening yet" in out
    assert conn.execute("show default_transaction_read_only").fetchone()[0] == "on"


def test_the_page_carries_its_data_inline(conn, tmp_path, capsys):
    add(conn, et(11), cost="0.80", label="</script><b>x</b>")
    path = tmp_path / "spend.html"
    assert dev_spend.main(["--html", str(path)], conn=conn, now=BEFORE_OPEN_NOW) == 0
    html = path.read_text(encoding="utf-8")
    assert "/*DATA*/null" not in html and '"weekly":' in html and '"daily":' in html
    assert "</script><b>" not in html                                 # a label can't end the script early
    assert "default-src 'none'" in html                               # it fetches nothing


def test_amounts_leave_as_exact_decimals_so_they_are_rounded_once():
    # $0.1234499 is $0.1234 to four places. Through a float rounded to six places it became 0.12345, which the page
    # showed as $0.1235.
    r = summarise([call(et(11), "0.1234499", label="a"), call(et(11), "0.0000001", label="b")], None, "week",
                  BEFORE_OPEN_NOW)
    data = dev_spend.to_json(r)
    assert dev_spend.usd(next(x for x in r.runs if x.label == "a").total_cents) == "$0.1234"
    assert {x["label"]: x["total_usd"] for x in data["runs"]} == {"a": "0.1234499", "b": "0.0000001"}
    assert data["totals"]["total_usd"] == "0.12345" and data["totals"]["by_model"] == {"claude-opus-5-5": "0.12345"}
    assert data["totals"]["unconfirmed_usd"] == "0"

    def amounts(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k.endswith("_usd"):
                    yield v
                elif k.startswith("by_"):
                    yield from v.values()
                else:
                    yield from amounts(v)
        elif isinstance(node, list):
            for v in node:
                yield from amounts(v)
    found = list(amounts(data))
    assert found and all(isinstance(v, str) and re.fullmatch(r"-?\d+(\.\d+)?", v) for v in found)   # no floats, no 1E-7
    try:
        dev_spend._dollars(Fraction(1, 3))
    except ValueError:
        pass
    else:
        raise AssertionError("a third of a cent has no exact decimal form and must not be rounded quietly")


def test_the_page_escapes_every_markup_character_in_its_data():
    # "<!--<script>" in a script element stops the browser parsing the rest of the page.
    label = "<!--<script> & \u2028 \u2029 x > y"
    r = summarise([call(et(11), label=label)], None, "week", BEFORE_OPEN_NOW)
    html = dev_spend.page(r, r)
    start = html.index("const DATA = ") + len("const DATA = ")
    data = html[start:html.index(";\n", start)]
    assert not set(data) & set("<>&\u2028\u2029")
    assert json.loads(data)["weekly"]["runs"][0]["label"] == label
    assert html.count("<script") == 1 and html.count("</script") == 1


def test_the_demo_page_needs_no_database(tmp_path, capsys):
    path = tmp_path / "demo.html"
    assert dev_spend.main(["--demo", "--html", str(path)], now=BEFORE_OPEN_NOW) == 0
    html = path.read_text(encoding="utf-8")
    assert "from run notes, not the ledger" in html and "sweep 20261111-173503 opus@low" in html
    assert dev_spend.main(["--demo"], now=BEFORE_OPEN_NOW) == 0
    assert "Source: from run notes, not the ledger." in capsys.readouterr().out


def test_no_agent_tool_or_brief_reads_development_spend():
    # The agent's MCP server and brief must not reach this module (it is for the team and the teachers).
    from pathlib import Path
    root = Path(dev_spend.__file__).parent
    for path in [root / "server.py", root / "agent.py", *sorted((root / "tools").glob("*.py"))]:
        assert "dev_spend" not in path.read_text(encoding="utf-8"), path


def test_bucket_start_spans_dst():
    # DST ended 1 Nov: a week spanning it still starts on the Saturday, and days stay calendar days.
    r = summarise([call(et(31, 12, month=10)), call(et(2, 12))], None, "day", et(2, 13).astimezone(timezone.utc))
    assert [b.start for b in r.buckets] == [date(2026, 10, 31) + timedelta(days=i) for i in range(3)]


def test_one_run_crossing_opening_is_split(conn):
    add(conn, OPEN - timedelta(minutes=1), run="same")
    add(conn, OPEN, run="same", cost="0.20")
    opening(conn)
    report = dev_spend.weekly(conn, NOW)
    assert report.totals.total_cents == cents("0.10")
    assert len(report.runs) == 2
    assert sorted(r.after_opening for r in report.runs) == [False, True]


def test_ok_call_kept_at_worst_case_is_unconfirmed(conn):
    add(conn, et(12), cost="0.50")
    conn.execute("update model_calls set detail = '(usage reported no tokens; kept at worst case)'")
    report = dev_spend.weekly(conn, BEFORE_OPEN_NOW)
    assert report.totals.unconfirmed_cents == cents("0.50")
    assert report.totals.tokens_unknown == 1


def test_opening_before_any_development_is_marked(conn):
    opening(conn)
    add(conn, OPEN + timedelta(days=10))
    report = dev_spend.weekly(conn, OPEN + timedelta(days=11))
    assert report.buckets[0].start == ledger.week_of(OPEN)
    assert report.totals.total_cents == 0
    assert sum(b.after_opening_test_cents for b in report.buckets) == cents("0.10")


def test_opening_without_any_calls_still_has_a_marker(conn):
    opening(conn)
    report = dev_spend.weekly(conn, NOW)
    assert report.buckets[0].start == ledger.week_of(OPEN)


def test_after_opening_unconfirmed_is_separate(conn):
    opening(conn)
    add(conn, OPEN + timedelta(hours=1), status="error", cost="0.25")
    bucket = dev_spend.to_json(dev_spend.weekly(conn, NOW))["buckets"][0]
    assert bucket["after_opening_test_usd"] == "0.25"
    assert bucket["after_opening_test_unconfirmed_usd"] == "0.25"
    assert bucket["unconfirmed_usd"] == "0"


def test_test_opening_keeps_development_open(conn):
    conn.execute("insert into ledger_entries (id, at, kind, amount_cents, detail, recorded_by)"
                 " values (%s, %s, 'opening', 60000, %s, 'tester')",
                 (ledger.TEST_OPENING_ID, et(11), ledger.TEST_OPENING_DETAIL))
    add(conn, et(12))
    r = dev_spend.weekly(conn, BEFORE_OPEN_NOW)
    assert r.opening_at is None
    assert r.totals.total_cents == 10


def test_price_search_calls_are_counted_once(conn):
    add(conn, et(12), label="pack price lookup tea Costco")
    for store, cost in [("Costco", "0.10"), ("Amazon", "0.25")]:
        conn.execute("insert into price_lookups (at, sku, store, model, cost_usd, kind, asked_min,"
                     " asked_max, outcome, by) values (%s, 'tea', %s, 'claude-opus-5-5', %s,"
                     " 'search', 1, 1, 'found', 'tester')", (et(12), store, Decimal(cost)))
    totals = dev_spend.weekly(conn, BEFORE_OPEN_NOW).totals
    assert totals.total_cents == 35
    assert totals.by_category == {"price search": 35}


def test_record_import_is_idempotent_and_uses_actual_date(conn, tmp_path):
    csv = tmp_path / "model_calls.csv"
    csv.write_text("id,at,label,model,status,cost_usd\n"
                   "1,2026-12-03T12:00:00-05:00,week run,opus,ok,0.25\n"
                   "2,2026-12-03T12:01:00-05:00,week run,opus,error,0.50\n")
    args = ["record", "--from", str(tmp_path), "--label", "rehearsal", "--by", "tester"]
    for _ in range(2):
        assert dev_spend.main(args, conn=conn, now=et(12)) == 0
    args = ["record", "--usd", "1.00", "--label", "console", "--by", "tester", "--unconfirmed"]
    for _ in range(2):
        assert dev_spend.main(args, conn=conn, now=et(12)) == 0
    opening(conn)
    report = dev_spend.weekly(conn, NOW)
    assert report.totals.total_cents == 175
    assert report.totals.unconfirmed_cents == 150
    assert report.totals.by_category == {"simulations/bench": 75, "manual": 100}


def test_dashboard_spendable_uses_recorded_spend_until_real_opening(conn):
    from vending import config, dashboard

    conn.execute("insert into ledger_entries (id, at, kind, amount_cents, detail, recorded_by)"
                 " values (%s, %s, 'opening', 60000, %s, 'tester')",
                 (ledger.TEST_OPENING_ID, et(11), ledger.TEST_OPENING_DETAIL))
    add(conn, et(12), cost="0.25", status="pending")
    config.set_value(conn, "dev_spend_cap_cents", 9000, "tester")
    snap = dashboard.snapshot(conn, BEFORE_OPEN_NOW)
    data = snap["development_spend"]["data"]
    assert data["total"] == "$0.25" and data["cap"] == "$90.00"
    assert data["since"] == "12 Nov 2026" and data["unconfirmed"] == "$0.2500"
    page = dashboard.render(snap)
    assert "Recorded spend since 12 Nov 2026" in page and "$0.25 / $90.00" in page
