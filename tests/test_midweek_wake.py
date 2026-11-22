"""The midweek wake-up (#219): decide, the one-per-week cap, the worker's job and the surfaces.

The decision math is tested pure (`decide`), against the worked cases in
docs/restock-policy.md section 2. The database tests build a fast-seller week from
synthetic receipts and polls (the same builders the other suites use) and walk the
done-when cases: a fast-seller week wakes exactly once, a normal week wakes nothing,
a second fast seller the same week wakes nothing more, and the job is off by default.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction

import pytest

from tests.conftest import store_poll
from tests.test_dashboard import fulfilled
from vending import db, midweek_wake, settings
from vending.midweek_wake import Outlook, decide

# Monday 7 Dec 2026, noon Eastern (EST, UTC-5): inside the fall term, week of Sat 5 Dec.
MONDAY = datetime(2026, 12, 7, 17, tzinfo=timezone.utc)
TUESDAY = MONDAY + timedelta(days=1, hours=1)          # Tue 8 Dec, 1pm Eastern
WEDNESDAY = MONDAY + timedelta(days=2, hours=2)        # Wed 9 Dec, 2pm Eastern
SATURDAY_BEFORE = MONDAY - timedelta(days=2)           # Sat 5 Dec, 10am Eastern
BREAK_MONDAY = datetime(2026, 12, 21, 17, tzinfo=timezone.utc)  # winter break week: no class days
# The briefing fixture's week (tests.test_briefing's NOW, Thu 12 Nov 2026, is in this week).
BRIEFING_WEEK = "2026-11-07"


def out(slot, rate, days_left, price=450, cost=Fraction(169)):
    """An Outlook for one slot, in cents."""
    return Outlook(slot, None if rate is None else Decimal(str(rate)),
                   None if days_left is None else Decimal(str(days_left)), price, cost)


def premium(slots, rate="1.354", days_left=0, price=450, cost=Fraction(169)):
    """Premium-priced slots ($4.50: the mid-premium fall prior, 1.354 a class day) empty now."""
    return [Outlook(s, Decimal(str(rate)), Decimal(str(days_left)), price, cost) for s in slots]


# --- the decision, pure ---------------------------------------------------------


def test_monday_fast_sellers_wake_with_the_docs_numbers():
    # 4 slots empty now at $4.50 and $1.69 cost, mid-premium prior: per slot
    # 1.354 x 2 saved class days x ($4.50 / 1.08 tax - $1.69) = $6.7068 -> 4 slots $26.83 > $25.
    wake = decide(MONDAY, premium(["15", "16", "17", "18"]))
    assert wake is not None
    assert wake.week_start.isoformat() == "2026-12-05"          # the Saturday that starts the week
    assert wake.slots == ("15", "16", "17", "18")
    assert wake.saved_margin_cents == 2683
    assert "empty now" in wake.reason
    assert "Wednesday afternoon" in wake.reason                 # Monday's request plans on Wednesday
    assert "$26.83" in wake.reason and "$25.00" in wake.reason


def test_one_and_three_empty_slots_never_pay():
    assert decide(MONDAY, premium(["15"])) is None
    assert decide(MONDAY, premium(["15", "16", "17"])) is None   # 3 x $6.7068 = $20.12 < $25
    assert decide(MONDAY, premium(["15", "16", "17", "18"])) is not None


def test_saved_days_count_the_class_days_after_the_landing():
    # Monday's visit lands Wednesday (walk: Tue, Wed, Thu, Fri). A slot is empty from the
    # start of the first class day its days-left run past, counting class days from today.
    # 1.5 days left: empty before Thursday, so Thursday and Friday are saved (2).
    # 2.5: empty during Thursday, so only Friday is saved (1). 3.0: empty as Friday
    # begins: Friday saved (1). 4.5: never empty before Friday's restock: not chosen.
    outlooks = (premium(["15", "16", "17", "18"])
                + premium(["25", "26"], days_left=1.5)
                + premium(["27"], days_left=2.5, rate="1.0")
                + premium(["28"], days_left=3.0, rate="1.0")
                + premium(["35"], days_left=4.5, rate="1.0"))
    wake = decide(MONDAY, outlooks)
    assert wake.slots == ("15", "16", "17", "18", "25", "26", "27", "28")   # 35 sells out after Friday
    margin = Fraction(450) / Fraction(108, 100) - Fraction(169)             # $2.4767 a unit
    saved = 6 * (Fraction("1.354") * 2 * margin) + 2 * (Fraction("1.0") * 1 * margin)
    assert wake.saved_margin_cents == round(saved.numerator / saved.denominator)


def test_unknown_projection_price_or_cost_is_skipped_not_guessed():
    known = premium(["15", "16", "17", "18"])
    assert decide(MONDAY, known + [out("25", None, "0.0")]).slots == ("15", "16", "17", "18")
    assert decide(MONDAY, known + [out("25", "1.354", None)]).slots == ("15", "16", "17", "18")
    assert decide(MONDAY, known + [out("25", "1.354", "0.0", price=None)]).slots == ("15", "16", "17", "18")
    assert decide(MONDAY, known + [out("25", "1.354", "0.0", cost=None)]).slots == ("15", "16", "17", "18")


def test_no_decision_after_tuesday_or_before_the_week():
    assert decide(WEDNESDAY, premium(["15", "16", "17", "18"])) is None   # no visit can still pay
    assert decide(SATURDAY_BEFORE, premium(["15", "16", "17", "18"])) is None  # Monday decides, on fresher data


def test_break_week_finds_no_class_days_to_save():
    # Tue-Fri of break week are all in NO_CLASSES (22 Dec-1 Jan): even empty slots wake
    # nothing; the Friday restock refills the machine before classes resume.
    assert decide(BREAK_MONDAY, premium(["15", "16", "17", "18"])) is None


def test_naive_clock_is_a_guess_and_is_refused():
    with pytest.raises(ValueError):
        decide(MONDAY.replace(tzinfo=None), premium(["15", "16", "17", "18"]))


def test_fee_and_tax_come_from_the_caller():
    # Fee 0: one slot empty now pays ($6.71 > $0). Tax 0: the margin skips the tax divisor.
    assert decide(MONDAY, premium(["15"]), fee_cents=0) is not None
    wake = decide(MONDAY, premium(["15", "16", "17", "18"]), tax_percent=0)
    assert wake.saved_margin_cents == round(4 * Fraction("1.354") * 2 * (450 - 169))


# --- the week cap and the request, on the database -------------------------------


def week_row(week="2026-12-05", slots=("15", "16")):
    return midweek_wake.Wake(datetime.fromisoformat(week).date(), "Slots 15 and 16 would sell out.",
                             slots, 2600)


def test_the_cap_is_the_unique_index(conn):
    assert midweek_wake.record(conn, week_row()) is not None
    assert midweek_wake.record(conn, week_row()) is None        # same week: refused
    assert conn.execute("select count(*) from midweek_wakeups").fetchone()[0] == 1
    assert midweek_wake.record(conn, week_row(week="2026-12-12")) is not None   # a later week is free
    assert conn.execute("select count(*) from midweek_wakeups").fetchone()[0] == 2


def test_request_midweek_run_records_once_and_logs(conn, capsys):
    assert not midweek_wake.request_midweek_run(conn, "Slots 15 and 16 would sell out.", now=MONDAY)
    midweek_wake.record(conn, week_row())
    assert midweek_wake.request_midweek_run(conn, "Slots 15 and 16 would sell out.", by="tester", now=MONDAY)
    row = midweek_wake.current(conn, MONDAY)
    assert row["requested_at"] is not None and row["requested_by"] == "tester"
    assert not midweek_wake.request_midweek_run(conn, "again")   # already asked: nothing to start
    assert "midweek run requested" in capsys.readouterr().out


def test_current_reads_this_week_only(conn):
    assert midweek_wake.current(conn, MONDAY) is None
    midweek_wake.record(conn, week_row())
    row = midweek_wake.current(conn, MONDAY)
    assert row["week_start"].isoformat() == "2026-12-05"
    assert row["slots"] == ["15", "16"] and row["saved_margin_cents"] == 2600
    assert midweek_wake.current(conn, MONDAY + timedelta(days=8)) is None   # next week


# --- the worker's job, on stored facts -------------------------------------------


def fast_week(conn, slots=("15", "16", "17", "18"), *, loaded=6, sold=6, paid="10.14", tag="a"):
    """Slots loaded Friday evening ($1.69 a unit), sold out by Monday's poll, $4.50 each.

    Price $4.50 sits nearest the mid-premium fall prior (1.354 a class day); a Monday
    check saves 2 class days per slot, so 4 slots save $26.83 against the $25 fee."""
    friday_evening = datetime(2026, 12, 4, 22, tzinfo=timezone.utc)
    for slot in slots:
        fulfilled(conn, f"wake-{tag}-{slot}", slot, "WAKE-SKU", units_loaded=loaded, paid=paid,
                  at=friday_evening)
    store_poll(conn, f"wake-before-{tag}", datetime(2026, 12, 4, 20, tzinfo=timezone.utc).isoformat(),
               {s: (450, 100) for s in slots})
    store_poll(conn, f"wake-monday-{tag}", datetime(2026, 12, 7, 16, tzinfo=timezone.utc).isoformat(),
               {s: (450, 100 + sold) for s in slots})


def test_fast_seller_week_wakes_exactly_once(conn):
    fast_week(conn)
    assert midweek_wake.check(conn, now=MONDAY) is True
    row = midweek_wake.current(conn, MONDAY)
    assert row["slots"] == ["15", "16", "17", "18"]
    assert row["requested_at"] is not None and row["requested_by"] == "midweek_wake"
    assert "Wednesday afternoon" in row["reason"] and "$26.83" in row["reason"]
    assert midweek_wake.check(conn, now=MONDAY + timedelta(hours=1)) is False   # same week: held
    assert midweek_wake.check(conn, now=TUESDAY) is False
    assert conn.execute("select count(*) from midweek_wakeups").fetchone()[0] == 1


def test_a_second_fast_seller_the_same_week_wakes_nothing_more(conn):
    fast_week(conn)
    assert midweek_wake.check(conn, now=MONDAY) is True
    fast_week(conn, slots=("25",), tag="b")                     # another slot sells out midweek
    assert midweek_wake.check(conn, now=MONDAY + timedelta(hours=2)) is False
    row = midweek_wake.current(conn, MONDAY)
    assert row["slots"] == ["15", "16", "17", "18"]             # the week's one wake-up stands


def test_normal_week_wakes_nothing(conn, capsys):
    fast_week(conn, loaded=30, sold=3)                          # well stocked: 19.9 class days left
    assert midweek_wake.check(conn, now=MONDAY) is False
    assert midweek_wake.current(conn, MONDAY) is None
    assert "no slot is projected" in capsys.readouterr().out


def test_a_stale_poll_refuses_the_check(conn, capsys):
    fast_week(conn)
    old = MONDAY - timedelta(hours=4)
    conn.execute("delete from polls where started_at > %s", (old,))
    assert midweek_wake.check(conn, now=MONDAY) is False
    assert "stale" in capsys.readouterr().out.lower()


def test_check_without_polls_skips(conn, capsys):
    assert midweek_wake.check(conn, now=MONDAY) is False
    assert "skipped" in capsys.readouterr().out


def test_job_gates_on_the_environment(conn, monkeypatch):
    monkeypatch.delenv("MIDWEEK_WAKE", raising=False)
    monkeypatch.setattr(settings, "_FILE", {})

    def no_connection(*a, **k):
        raise AssertionError("the off job must not open a database connection")

    monkeypatch.setattr(db, "connect", no_connection)
    assert midweek_wake.job() == 0
    monkeypatch.setenv("MIDWEEK_WAKE", "off")
    assert midweek_wake.job() == 0
    assert conn.execute("select count(*) from midweek_wakeups").fetchone()[0] == 0


def test_job_on_runs_the_check(conn, monkeypatch):
    monkeypatch.setenv("MIDWEEK_WAKE", "on")
    monkeypatch.setattr(midweek_wake.ledger, "db_now", lambda _conn: MONDAY)  # the real clock isn't Dec 2026
    fast_week(conn)

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(db, "connect", fake_connect)
    assert midweek_wake.job() == 0
    assert midweek_wake.current(conn, MONDAY) is not None       # the job recorded the week's wake-up


# --- the cost basis ---------------------------------------------------------------


def test_slot_product_costs_survive_a_sell_out(conn):
    from vending import stock

    fast_week(conn)                                             # slots 15-18 empty now, sold out
    costs = stock.slot_product_costs(conn)
    assert [costs[s] for s in ("15", "16", "17", "18")] == [Fraction(169)] * 4   # $10.14 for 6
    assert costs.get("25") is None                              # no receipt: no cost to guess with


# --- the surfaces ----------------------------------------------------------------


def test_briefing_carries_the_wake_up(conn):
    from tests.test_briefing import NOW as BRIEF_NOW, sample
    from vending import briefing

    sample(conn)
    assert briefing.build(conn, BRIEF_NOW).midweek_wakeup is None
    midweek_wake.record(conn, week_row(week=BRIEFING_WEEK))
    b = briefing.build(conn, BRIEF_NOW)
    assert b.midweek_wakeup is not None
    assert b.midweek_wakeup.saved_margin == "26.00" and b.midweek_wakeup.slots == ["15", "16"]
    assert not b.midweek_wakeup.requested
    text = briefing.render(b)
    assert "Midweek wake-up:" in text and "would sell out" in text
    assert "has not been requested yet" in text
    midweek_wake.request_midweek_run(conn, week_row().reason, by="tester", now=BRIEF_NOW)
    b = briefing.build(conn, BRIEF_NOW)
    assert b.midweek_wakeup.requested
    assert "was requested" in briefing.render(b)


def test_briefing_alerts_when_the_table_is_missing(conn):
    from tests.test_briefing import NOW as BRIEF_NOW, sample
    from vending import briefing

    sample(conn)
    conn.execute("drop table midweek_wakeups")
    b = briefing.build(conn, BRIEF_NOW)
    assert b.midweek_wakeup is None
    assert "midweek_wakeup_unavailable" in {a.code for a in b.alerts}


def test_dashboard_attention_carries_the_wake_up(conn):
    from vending import dashboard

    fast_week(conn)
    assert midweek_wake.check(conn, now=MONDAY) is True
    snap = dashboard._snapshot(conn, MONDAY)[0]
    wake = snap["midweek_wakeup"]
    assert wake["ok"] and wake["data"]["slots"] == ["15", "16", "17", "18"]
    items = {i["text"]: i["level"] for i in dashboard.attention(snap)}
    assert next(level for text, level in items.items() if "Midweek wake-up:" in text) == "info"
    dashboard.to_json(snap)                                     # the whole snapshot stays JSON-safe


def test_dashboard_attention_warns_until_requested(conn):
    from vending import dashboard

    fast_week(conn)
    conn.execute("delete from midweek_wakeups")                 # the check recorded a request; take it off
    midweek_wake.record(conn, week_row(week="2026-12-05", slots=("15", "16", "17", "18")))
    snap = dashboard._snapshot(conn, MONDAY)[0]
    items = {i["text"]: i["level"] for i in dashboard.attention(snap)}
    assert next(level for text, level in items.items() if "Midweek wake-up:" in text) == "warn"
    assert "has not been requested yet" in next(text for text in items if "Midweek wake-up:" in text)


def test_dashboard_attention_without_the_table_is_a_missing_panel(conn):
    from vending import dashboard

    conn.execute("drop table midweek_wakeups")
    snap = dashboard._snapshot(conn, MONDAY)[0]
    wake = snap["midweek_wakeup"]
    assert wake["ok"] is False and "migrate" in wake["why"]


def test_worker_carries_the_job():
    from vending import worker

    assert worker.JOB_NAMES["midweek_wake"] == "Midweek wake-up check"
    assert [name for name, _ in worker.default_jobs()][-1] == "midweek_wake"   # after the syncs it reads
