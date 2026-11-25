"""The weekly scheduled price check (#202, Kevin 23 Nov). The run itself is faked in the scheduler's tests; the
price lookups' Anthropic client is faked in `check_prices`'s: no model call, no web."""

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("anthropic")

from vending import config, dashboard, price_schedule, price_search, settings, sourcing, worker  # noqa: E402
from vending.agent_schedule import Entry, Schedule  # noqa: E402
from vending.price_schedule import PriceChecks, Result  # noqa: E402
from tests.test_price_search import (LIVE, SKU, Client, connect, failing, picked_plan, searched,  # noqa: E402,F401
                                     set_row)
from tests.test_price_compare import offer  # noqa: E402

ET = ZoneInfo("America/New_York")
NAMES = (price_schedule.MODE_SETTING, price_schedule.TIMES_SETTING, price_schedule.FROM_SETTING)
TUE = datetime(2026, 12, 8, tzinfo=ET)
ON = Schedule("live", (Entry(1, time(12), time(20)),), date(2026, 12, 1))


def at(day, hour, minute=0):
    return day.replace(hour=hour, minute=minute).astimezone(timezone.utc)


@pytest.fixture(autouse=True)
def _no_settings(monkeypatch):
    for name in NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(settings, "_FILE", {k: v for k, v in settings._FILE.items() if k not in NAMES})


class Run:
    """A fake check: records how it was called and returns `result`."""

    def __init__(self, result=None, error=None):
        self.calls, self.result, self.error = [], result or Result(0, 12, 3, 0, Decimal("0.4100")), error

    def __call__(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        return self.result


def checks(connect, run, when, sched=ON):
    return PriceChecks(sched, connect=connect, run=run, background=False, clock=lambda: when,
                       db_clock=lambda conn: when, log=lambda _: None)


def runs(conn):
    return conn.execute("select due_at, mode, outcome, compare, drinks, changed, failures, cost_usd, detail"
                        " from price_check_runs order by due_at").fetchall()


def on_in_db(conn, mode="live"):
    config.set_value(conn, "price_check_mode", mode, "test")


# --- The settings: off by default, twice --------------------------------------------------------------

def test_unset_is_off_and_the_default_is_tuesday_noon_to_eight(monkeypatch):
    assert price_schedule.schedule() is None and price_schedule.default_checks(log=lambda _: None) is None
    assert "price_check" not in worker.State().public()
    monkeypatch.setenv(price_schedule.MODE_SETTING, "live")
    sched = price_schedule.schedule()
    assert sched.mode == "live" and sched.entries == (Entry(1, time(12), time(20)),) and sched.problem is None
    monkeypatch.setenv(price_schedule.MODE_SETTING, "yes please")
    sched = price_schedule.schedule()
    assert sched.mode == "dry-run" and "PRICE_CHECK_MODE" in sched.problem
    monkeypatch.setenv(price_schedule.TIMES_SETTING, "tue noon")
    assert isinstance(price_schedule.default_checks(log=lambda _: None), price_schedule.Invalid)


def test_the_database_setting_must_turn_it_on_too(connect):
    run = Run()
    checks(connect, run, at(TUE, 12, 5)).tick(at(TUE, 12, 5))
    with connect() as conn:
        assert [r[1:3] for r in runs(conn)] == [("off", "skipped")]
    assert run.calls == []
    with pytest.raises(ValueError):
        with connect() as conn:
            config.set_value(conn, "price_check_mode", "sometimes", "test")


def test_a_due_time_runs_once_compares_first_and_records_what_it_did(connect):
    with connect() as conn:
        on_in_db(conn)
    run = Run()
    c = checks(connect, run, at(TUE, 12, 5))
    assert c.next_due(at(TUE, 12, 5)) == at(TUE, 12, 5)
    c.tick(at(TUE, 12, 5))
    c.tick(at(TUE, 12, 6))                                  # the same time never runs twice
    assert len(run.calls) == 1
    assert run.calls[0]["compare"] is True and run.calls[0]["dry_run"] is False and run.calls[0]["cap_cents"] == 300
    with connect() as conn:
        [(due, mode, outcome, compare, drinks, changed, failures, cost, detail)] = runs(conn)
    assert (due, mode, outcome, compare, drinks, changed, failures, cost) == \
        (at(TUE, 12), "live", "done", True, 12, 3, 0, Decimal("0.41"))
    assert c.public()["last"]["outcome"] == "done" and c.public()["last"]["changed"] == 3
    assert c.next_due(at(TUE, 12, 7)) == at(TUE + timedelta(days=7), 12)


def test_between_comparisons_it_re_checks_primary_stores(connect):
    with connect() as conn:
        on_in_db(conn)
    run = Run()
    checks(connect, run, at(TUE, 12, 5)).tick(at(TUE, 12, 5))
    week = TUE + timedelta(days=7)
    checks(connect, run, at(week, 12, 5)).tick(at(week, 12, 5))
    fortnight = TUE + timedelta(days=14)
    checks(connect, run, at(fortnight, 12, 5)).tick(at(fortnight, 12, 5))
    assert [c["compare"] for c in run.calls] == [True, False, True]
    with connect() as conn:
        config.set_value(conn, "price_compare_every_days", 7, "test")
    third = TUE + timedelta(days=21)
    checks(connect, run, at(third, 12, 5)).tick(at(third, 12, 5))
    assert run.calls[-1]["compare"] is True


def test_the_lower_mode_wins_and_a_dry_run_spends_nothing(connect):
    with connect() as conn:
        on_in_db(conn, "dry-run")
    run = Run()
    checks(connect, run, at(TUE, 12, 5)).tick(at(TUE, 12, 5))
    assert run.calls[0]["dry_run"] is True
    with connect() as conn:
        assert runs(conn)[0][1] == "dry-run"


def test_the_halt_switch_stops_it(connect):
    with connect() as conn:
        on_in_db(conn)
        config.set_halted(conn, True, "test")
    run = Run()
    checks(connect, run, at(TUE, 12, 5)).tick(at(TUE, 12, 5))
    assert run.calls == []
    with connect() as conn:
        assert runs(conn)[0][2] == "halted"


def spend(conn, when, dollars, label="price_check DRPEPPER-20BOT Amazon"):
    conn.execute("insert into model_calls (at, purpose, label, run_id, model, key_env, max_tokens, worst_case_usd,"
                 " cost_usd, detail, recorded_by) values (%s, 'test', %s, 'test', 'claude-haiku-5-5', 'TEST_KEY', 100,"
                 " %s, %s, 'ok', 'test')", (when, label, dollars, dollars))


def test_its_own_weekly_cap(connect):
    with connect() as conn:
        on_in_db(conn)
        spend(conn, at(TUE, 9) - timedelta(days=3), Decimal("1.00"))
        spend(conn, at(TUE, 9) - timedelta(days=8), Decimal("5.00"))          # more than a week ago
        spend(conn, at(TUE, 9), Decimal("9.00"), label="price_search_cli X Amazon")      # a person's run
    run = Run()
    checks(connect, run, at(TUE, 12, 5)).tick(at(TUE, 12, 5))
    assert run.calls[0]["cap_cents"] == 200
    week = TUE + timedelta(days=7)
    with connect() as conn:
        spend(conn, at(week, 9), Decimal("2.95"))
    checks(connect, Run(), at(week, 12, 5)).tick(at(week, 12, 5))
    with connect() as conn:
        row = runs(conn)[-1]
    assert row[2] == "skipped" and "weekly cap" in row[8]
    with connect() as conn, pytest.raises(ValueError):
        config.set_value(conn, "price_check_weekly_cap_cents", 1001, "test")


def test_a_time_no_worker_reached_is_missed_and_a_stuck_run_is_failed(connect):
    with connect() as conn:
        on_in_db(conn)
    run = Run()
    checks(connect, run, at(TUE, 21)).tick(at(TUE, 21))
    assert run.calls == []
    with connect() as conn:
        assert runs(conn)[0][2] == "missed"
        conn.execute("insert into price_check_runs (due_at, latest_at, mode, claimed_at) values (%s, %s, 'live', %s)",
                     (at(TUE, 12) + timedelta(days=7), at(TUE, 20) + timedelta(days=7), at(TUE, 12) + timedelta(days=7)))
    later = at(TUE, 15) + timedelta(days=7)
    checks(connect, run, later).tick(later)
    with connect() as conn:
        assert runs(conn)[-1][2] == "failed"


@pytest.mark.parametrize("result,error,outcome", [
    (Result(1, 12, 2, 1, Decimal("0.5")), None, "partial"),
    (Result(2), None, "failed"),
    (None, RuntimeError("the database went away"), "failed"),
])
def test_a_drink_without_a_price_is_partial_and_a_run_that_stops_is_failed(connect, result, error, outcome):
    with connect() as conn:
        on_in_db(conn)
    checks(connect, Run(result, error), at(TUE, 12, 5)).tick(at(TUE, 12, 5))
    with connect() as conn:
        assert runs(conn)[0][2] == outcome


# --- /health and the dashboard ---------------------------------------------------------------------

def test_health_shows_the_last_run(connect):
    with connect() as conn:
        on_in_db(conn)
    c = checks(connect, Run(), at(TUE, 12, 5))
    c.tick(at(TUE, 12, 5))
    shown = worker.State(price_checks=c).public()["price_check"]
    assert shown["mode"] == "live" and shown["last"]["drinks"] == 12 and "detail" not in shown["last"]


def test_needs_attention_shows_a_failed_or_partial_last_run(connect):
    with connect() as conn:
        on_in_db(conn)
    checks(connect, Run(Result(1, 12, 2, 1, Decimal("0.5"))), at(TUE, 12, 5)).tick(at(TUE, 12, 5))
    with connect() as conn:
        panel = dashboard._panel(lambda: dashboard._price_checks(conn))
    [(level, text)] = dashboard._price_check_lines(panel)
    assert level == "warn" and "12 drinks" in text and "2 prices changed" in text and "1 without" in text \
        and "$0.50" in text
    assert dashboard._price_check_lines({"ok": True, "data": {"outcome": "done"}}) == []
    assert dashboard._price_check_lines({"ok": True, "data": None}) == []


# --- The worker's loop ----------------------------------------------------------------------------

def test_the_worker_ticks_the_price_check_between_rounds(monkeypatch):
    ticked = []

    class Fake:
        def next_due(self, now):
            return now if not ticked else now + timedelta(days=30)

        def tick(self, now):
            ticked.append(now)

        def public(self):
            return {"mode": "live", "next_at": None, "last": None, "problem": None}

    monkeypatch.setattr(worker, "_slot_lock", lambda: None)
    now = [at(TUE, 11)]

    def sleep(seconds):
        now[0] += timedelta(seconds=seconds)
        if now[0] > at(TUE, 14):
            raise KeyboardInterrupt

    assert worker.main(["--port", "0", "--at-minute", "off"], jobs=[("a", lambda: 0)], sleep=sleep,
                       log=lambda _: None, clock=lambda: now[0], price_checks=Fake()) == 0
    assert len(ticked) == 1


# --- check_prices: what a run looks up -------------------------------------------------------------

@pytest.fixture
def ours(connect, monkeypatch):
    """Our slots hold Dr Pepper now (a full column of 7); no plan is picked."""
    monkeypatch.setattr(price_search, "current_drinks", lambda conn, slots, now: {SKU: 7})
    return connect


def test_a_re_check_looks_up_the_primary_store_on_its_listing(ours):
    with ours() as conn:
        set_row(conn, checked=LIVE - timedelta(days=6), price=2599, most=3000)
    client = Client(searched(searches=1, report={"found": True, "price": "26.49", "units_per_pack": 24,
                                                 "pack_description": "24 x 20 oz", "url": price_search_url(),
                                                 "stock": "in_stock", "note": "x"}))
    result = price_search.check_prices(by=price_schedule.BY, max_age_hours=120, connect=ours, client=client,
                                       now=lambda: LIVE, compare=False, label=price_schedule.LABEL)
    assert [b["tools"][0]["max_uses"] for b in client.sent] == [1]
    assert (result.exit_code, result.drinks, result.changed, result.failures) == (0, 1, 1, 0)
    assert result.cost_usd > 0
    with ours() as conn:
        assert sourcing._row(conn, SKU, "primary").expected_cents == 2649
        labels = {label for (label,) in conn.execute("select label from model_calls")}
    assert labels == {f"{price_schedule.LABEL} {SKU} Amazon"}


def price_search_url():
    from tests.test_price_search import URL

    return URL


def test_a_compare_prices_every_store_and_a_dry_run_searches_nothing(ours):
    with ours() as conn:
        set_row(conn, checked=LIVE - timedelta(days=9), price=2599, most=3000)
    idle = Client()
    result = price_search.check_prices(by=price_schedule.BY, max_age_hours=120, connect=ours, client=idle,
                                       now=lambda: LIVE, compare=True, dry_run=True, label=price_schedule.LABEL)
    assert idle.sent == [] and result.exit_code == 0 and result.cost_usd == 0
    client = Client(offer("Amazon", 24, "26.49"), offer("Walmart", 12, "12.00"), failing("Costco"), failing("BJs"))
    result = price_search.check_prices(by=price_schedule.BY, max_age_hours=120, connect=ours, client=client,
                                       now=lambda: LIVE, compare=True, label=price_schedule.LABEL)
    assert len(client.sent) == 4 and (result.drinks, result.changed, result.failures) == (1, 1, 0)


def test_a_drink_priced_by_a_recent_receipt_is_not_looked_up(ours):
    with ours() as conn:
        set_row(conn, checked=LIVE - timedelta(days=4), price=2599, most=3000, by=sourcing.RECEIPT_BY)
        sourcing.set_option(conn, sourcing.Option(
            sku=SKU, rank="alternate", store="Walmart", units_per_pack=24, expected_cents=2400, max_cents=3000,
            url=None, pack_description=None, checked_at=LIVE - timedelta(days=9), checked_by="Kevin"), now=LIVE)
    client = Client()
    result = price_search.check_prices(by=price_schedule.BY, max_age_hours=120, connect=ours, client=client,
                                       now=lambda: LIVE, compare=True, label=price_schedule.LABEL)
    assert client.sent == [] and result.drinks == 0


def test_a_key_with_no_credit_fails_the_weekly_check_and_counts_no_drink(ours):
    """Sol, #261: a compare that stops on no_credit looked nothing up. It is `failed`, not `partial`, so the next
    weekly run still compares every store, and it says no drink was looked up and why."""
    from tests.test_price_search import NoCreditClient

    with ours() as conn:
        set_row(conn, checked=LIVE - timedelta(days=9), price=2599, most=3000)
    result = price_search.check_prices(by=price_schedule.BY, max_age_hours=120, connect=ours, client=NoCreditClient(),
                                       now=lambda: LIVE, compare=True, label=price_schedule.LABEL)
    assert (result.exit_code, result.drinks, result.no_credit) == (2, 0, True)


def test_the_weekly_check_says_the_key_had_no_credit(connect):
    with connect() as conn:
        on_in_db(conn)
    checks(connect, Run(Result(2, no_credit=True)), at(TUE, 12, 5)).tick(at(TUE, 12, 5))
    with connect() as conn:
        (outcome, detail), = conn.execute("select outcome, detail from price_check_runs").fetchall()
    assert outcome == "failed" and "no credit" in detail
