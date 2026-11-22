"""Resetting the test opening (#215): a throwaway schema seeded like the team ledger on 21 Nov (a test
opening on 11 Nov, run 2's model spend and the 12 Nov test receipt), then the 29 Nov runbook:
dry run, reset, `ledger open`, check."""

import threading
import time
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction

import pytest

psycopg = pytest.importorskip("psycopg")

from tests.conftest import open_in, store_poll  # noqa: E402
from tests.test_ledger import deliver  # noqa: E402
from vending import dev_spend, ledger, money_panel, money_series, stock  # noqa: E402
from vending.ledger import LedgerError, RESET_CLOSES, reset_refusal  # noqa: E402
from vending.start import START  # noqa: E402
from vending.tools.account_balance import get_account_balance  # noqa: E402
from vending.tools.pnl import get_pnl  # noqa: E402

TEST_DETAIL = "opening capital (test state, 11 Nov; to be reset before launch)"
TEST_OPENED = datetime(2026, 11, 11, 19, 0, tzinfo=timezone.utc)
RESET_AT = datetime(2026, 11, 29, 20, 0, tzinfo=timezone.utc)       # Sun 29 Nov, 15:00 Eastern
REAL_OPENED = datetime(2026, 11, 29, 21, 0, tzinfo=timezone.utc)
AFTER = datetime(2026, 12, 2, 12, 0, tzinfo=timezone.utc)


def seed(conn):
    """The team ledger as it stands: a test opening, run 2's spend, a test receipt, sales on the counters."""
    ledger.record(conn, "opening", 60000, TEST_DETAIL, "marshall", TEST_OPENED)
    ledger.record_tokens(conn, "0.40", "claude-opus-5-5", 9000, 900, "run 2 turn 1", at=TEST_OPENED + timedelta(days=1))
    ledger.record_tokens(conn, "0.08", "claude-opus-5-5", 2000, 200, "dry run", at=TEST_OPENED + timedelta(days=1, hours=2))
    deliver(conn, "pr_test", resolved_at="2026-11-12T16:30:00Z")         # $12.37 on 12 Nov
    store_poll(conn, "p1", "2026-11-12T18:00:00+00:00", {"15": (250, 100)})
    store_poll(conn, "p2", "2026-11-13T18:00:00+00:00", {"15": (250, 104)})      # 4 sold in the test state


def sell_after_the_opening(conn):
    store_poll(conn, "p3", "2026-11-29T20:30:00+00:00", {"15": (250, 104)})      # the baseline, before the opening
    store_poll(conn, "p4", "2026-12-01T18:00:00+00:00", {"15": (250, 107)})      # 3 sold: 750 cents


def test_the_guard_needs_the_known_test_opening_inside_the_window():
    def entry(detail=TEST_DETAIL, at=TEST_OPENED, id=1):
        return ledger.Entry(id, at, "opening", Fraction(60000), detail, None, "marshall")

    not_it = "isn't the 11 Nov test opening"
    assert reset_refusal(entry(), RESET_AT) is None
    assert not_it in reset_refusal(entry("Opening capital (TEST)"), RESET_AT)
    assert not_it in reset_refusal(entry("opening capital"), RESET_AT)
    assert not_it in reset_refusal(entry(id=2), RESET_AT)
    assert not_it in reset_refusal(entry(at=TEST_OPENED + timedelta(days=1)), RESET_AT)
    # The date is Eastern: 23:30 Eastern on 11 Nov is 04:30 UTC on 12 Nov, and still the test day.
    assert reset_refusal(entry(at=datetime(2026, 11, 12, 4, 30, tzinfo=timezone.utc)), RESET_AT) is None
    assert not_it in reset_refusal(entry(at=datetime(2026, 11, 11, 4, 30, tzinfo=timezone.utc)), RESET_AT)
    assert "nothing to reset" in reset_refusal(None, RESET_AT)
    assert "already reset" in reset_refusal(entry(), RESET_AT, already=(RESET_AT, "kevin"))
    assert "already reset" in reset_refusal(None, RESET_AT, already=(RESET_AT, "kevin"))
    assert "window closed" in reset_refusal(entry(), RESET_CLOSES)
    assert reset_refusal(entry(), RESET_CLOSES - timedelta(seconds=1)) is None
    assert "competition started" in reset_refusal(entry(), START)
    assert "competition started" in reset_refusal(None, START + timedelta(days=1))


def test_the_dry_run_changes_nothing(conn):
    seed(conn)
    before = (ledger.entries(conn), conn.execute("select count(*) from action_log").fetchone())
    plan = ledger.plan_reset(conn, RESET_AT)
    assert (plan.opening.detail, plan.opening.at) == (TEST_DETAIL, TEST_OPENED)
    assert plan.later_entries == {"tokens": (2, Fraction(-48))}
    assert plan.receipts == (1, 1237) and plan.visits == (0, 0)
    assert (ledger.entries(conn), conn.execute("select count(*) from action_log").fetchone()) == before
    assert ledger.archived(conn) == []


def test_reset_then_open_counts_only_from_the_new_opening(conn):
    seed(conn)
    assert stock.opening_at(conn) == TEST_OPENED
    assert sum(s.units for s in stock.stock(conn).slots) == 4          # the test receipt is stock while it counts
    moved = ledger.reset_test_opening(conn, "kevin", RESET_AT)
    assert (moved.id, moved.detail) == (1, TEST_DETAIL)
    assert ledger.opening(conn) is None and ledger.balance(conn) is None
    [(kept, _, who)] = ledger.archived(conn)
    assert (kept.id, kept.kind, kept.amount_cents, kept.at, kept.detail, who) == (
        1, "opening", 60000, TEST_OPENED, TEST_DETAIL, "kevin")
    assert conn.execute("select tool, outcome from action_log where tool like 'ledger.reset%'").fetchall() == [
        ("ledger.reset_test_opening", "done")]
    assert [e.kind for e in ledger.entries(conn)] == ["tokens", "tokens"]   # model calls stay

    ledger.record(conn, "opening", 60000, "opening capital", "kevin", REAL_OPENED)
    sell_after_the_opening(conn)
    b = ledger.balance(conn, AFTER)
    assert (b.opening_at, b.opening_cents, b.takings.cents) == (REAL_OPENED, 60000, 750)
    assert (b.receipts_cents, b.receipt_lines, b.visits, b.token_spend_cents) == (0, 0, 0, 0)
    assert b.balance_cents == 60000 + 750
    assert any("2 model-call entries totalling 0.48" in n and "development spend" in n for n in b.notes)
    assert not any("re-date" in n for n in b.notes)
    # Stock and cost: the 12 Nov receipt is a test purchase now.
    counted = stock.stock(conn, until=AFTER)
    assert (counted.slots, counted.closet) == ((), ())
    p = ledger.pnl(conn, REAL_OPENED, AFTER)
    assert (p.receipts_cents, p.token_spend_cents, p.units_sold, p.inventory_cents) == (0, 0, 3, None)
    # The other readers of the opening agree.
    reply = get_account_balance(conn, AFTER)
    assert (reply.success, reply.opening_at, reply.balance, reply.receipts, reply.token_spend) == (
        True, REAL_OPENED, "607.50", "0.00", "0.00")
    assert get_pnl(conn, date(2026, 11, 29), date(2026, 12, 2)).purchases == "0.00"
    days = money_series.daily(conn, date(2026, 11, 28), date(2026, 12, 2), now=AFTER)
    assert [d.cash_balance_cents for d in days][:1] == [None]           # 28 Nov: before the opening
    assert days[-1].cash_balance_cents == 60750 and sum(d.purchases_cents for d in days) == 0
    assert sum(d.token_spend_cents for d in days) == 0
    panel = money_panel.read(conn, AFTER)
    assert panel["opened"] and panel["balance"] == "607.50"
    # Run 2's spend is development spend on the dev-spend page.
    report = dev_spend.weekly(conn, AFTER)
    assert report.opening_at == REAL_OPENED and report.totals.total_cents == 48


def test_a_second_reset_and_a_reset_after_the_real_opening_refuse(conn):
    seed(conn)
    ledger.reset_test_opening(conn, "kevin", RESET_AT)
    with pytest.raises(LedgerError, match="already reset"):
        ledger.reset_test_opening(conn, "kevin", RESET_AT)
    with pytest.raises(LedgerError, match="already reset"):
        ledger.plan_reset(conn, RESET_AT)
    ledger.record(conn, "opening", 60000, "opening capital", "kevin", REAL_OPENED)
    with pytest.raises(LedgerError, match="already reset"):
        ledger.reset_test_opening(conn, "kevin", RESET_AT)
    assert ledger.opening(conn).at == REAL_OPENED and len(ledger.archived(conn)) == 1


def test_a_real_opening_recorded_early_is_never_reset(conn):
    ledger.record(conn, "opening", 60000, "opening capital", "kevin", datetime(2026, 11, 20, tzinfo=timezone.utc))
    with pytest.raises(LedgerError, match="isn't the 11 Nov test opening"):
        ledger.reset_test_opening(conn, "kevin", RESET_AT)
    assert ledger.opening(conn) is not None and ledger.archived(conn) == []


def test_no_reset_once_the_competition_has_started(conn):
    seed(conn)
    with pytest.raises(LedgerError, match="competition started"):
        ledger.reset_test_opening(conn, "kevin", START)
    assert ledger.opening(conn).detail == TEST_DETAIL


def test_a_reset_needs_a_name(conn):
    seed(conn)
    with pytest.raises(LedgerError, match="--by"):
        ledger.reset_test_opening(conn, " ", RESET_AT)


def test_cli_runbook_end_to_end(schema_url, monkeypatch, capsys):
    """The 29 Nov runbook (docs/ledger-reset.md) through the CLI: dry run, reset, open, show."""
    from vending import db as db_module

    monkeypatch.setattr(db_module, "connect", lambda: open_in(*schema_url))
    monkeypatch.setattr(ledger, "db_now", lambda conn: RESET_AT)
    with open_in(*schema_url) as conn:
        seed(conn)
    assert ledger.main(["reset-test-opening", "--by", "kevin"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Dry run: nothing was changed.") and TEST_DETAIL in out
    assert "2 entries, 0.48 spent" in out and "1 lines, 12.37 paid" in out
    assert "reset-test-opening --by kevin --yes" in out
    with open_in(*schema_url) as conn:
        assert ledger.opening(conn).detail == TEST_DETAIL
    assert ledger.main(["reset-test-opening", "--by", "kevin", "--yes"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Moved one entry out of the books") and "python -m vending.ledger open" in out
    assert ledger.main(["reset-test-opening", "--by", "kevin", "--yes"]) == 1
    assert "already reset" in capsys.readouterr().err
    assert ledger.main(["open", "--amount", "600.00", "--by", "kevin", "--at", REAL_OPENED.isoformat()]) == 0
    capsys.readouterr()
    assert ledger.main(["reset-test-opening", "--by", "kevin"]) == 1
    assert "already reset" in capsys.readouterr().err
    assert ledger.main(["entries"]) == 0
    out = capsys.readouterr().out
    assert "Archived, not in the books:" in out and TEST_DETAIL in out
    monkeypatch.setattr(ledger, "db_now", lambda conn: AFTER)
    assert ledger.main(["show"]) == 0
    out = capsys.readouterr().out
    assert "Opening capital" in out and "2026-11-29" in out and "development spend" in out


# --- Review findings (Sol, 21 Nov), fixed -------------------------------------------------------

def test_a_real_opening_after_a_reset_is_never_archived_whatever_its_detail(conn):
    """Finding 1: the real opening's detail may say "test". Only the known test opening can move."""
    seed(conn)
    ledger.reset_test_opening(conn, "kevin", RESET_AT)
    real = ledger.record(conn, "opening", 60000, "Real opening after test reset", "kevin",
                         datetime(2026, 11, 28, 17, 0, tzinfo=timezone.utc))
    for now in (RESET_AT, datetime(2026, 11, 28, 18, 0, tzinfo=timezone.utc)):
        with pytest.raises(LedgerError):
            ledger.reset_test_opening(conn, "kevin", now)
        with pytest.raises(LedgerError):
            ledger.plan_reset(conn, now)
    assert ledger.opening(conn).id == real.id and len(ledger.archived(conn)) == 1


def test_only_the_known_test_opening_matches(conn):
    """Finding 1: id 1, dated 11 Nov, with the exact detail. Anything else refuses, under the row lock."""
    assert (ledger.TEST_OPENING_ID, ledger.TEST_OPENING_DATE, ledger.TEST_OPENING_DETAIL) == (
        1, date(2026, 11, 11), TEST_DETAIL)
    ledger.record(conn, "opening", 60000, "opening capital (a test, 11 Nov)", "marshall", TEST_OPENED)
    with pytest.raises(LedgerError, match="isn't the 11 Nov test opening"):
        ledger.reset_test_opening(conn, "kevin", RESET_AT)
    assert ledger.opening(conn) is not None and ledger.archived(conn) == []


def test_a_second_reset_refuses_even_if_the_test_opening_is_back(conn):
    """Finding 1: once the archive records a reset, nothing else is moved, even a row just like the test one."""
    seed(conn)
    ledger.reset_test_opening(conn, "kevin", RESET_AT)
    conn.execute("insert into ledger_entries (id, at, kind, amount_cents, detail, recorded_by)"
                 " values (1, %s, 'opening', 60000, %s, 'marshall')", (TEST_OPENED, TEST_DETAIL))
    with pytest.raises(LedgerError, match="already reset"):
        ledger.reset_test_opening(conn, "kevin", RESET_AT)
    assert ledger.opening(conn).id == 1 and len(ledger.archived(conn)) == 1


def test_the_reset_window_closes_at_midnight_eastern_on_30_nov(conn):
    """Finding 2: a reset on Mon 30 Nov would let the opening land after the fill's receipt."""
    closes = ledger.RESET_CLOSES
    assert closes.tzinfo is not None and str(closes.tzinfo) == "America/New_York"
    assert closes == datetime(2026, 11, 30, 5, 0, tzinfo=timezone.utc)          # 00:00 EST
    seed(conn)
    with pytest.raises(LedgerError, match="window closed"):
        ledger.reset_test_opening(conn, "kevin", closes)
    with pytest.raises(LedgerError, match="window closed"):
        ledger.plan_reset(conn, closes + timedelta(hours=12))
    assert ledger.opening(conn).detail == TEST_DETAIL and ledger.archived(conn) == []
    assert ledger.reset_test_opening(conn, "kevin", closes - timedelta(seconds=1)).id == 1


def test_a_reset_that_waits_past_midnight_on_its_last_write_rolls_back(conn, schema_url, monkeypatch):
    """Sol review, round 2, finding 1: the window is checked, then the audit insert waits on a lock held by
    another connection until after 00:00 Eastern on Mon 30 Nov. The reset must not commit then."""
    seed(conn)
    times = [RESET_CLOSES - timedelta(seconds=1)]
    monkeypatch.setattr(ledger, "db_now", lambda c: times[-1])       # the database clock, moved by the test
    blocker = open_in(*schema_url)
    blocker.autocommit = False
    blocker.execute("lock table action_log in access exclusive mode")
    result = {}

    def reset():
        with open_in(*schema_url) as c:
            try:
                result["moved"] = ledger.reset_test_opening(c, "kevin")
            except LedgerError as error:
                result["error"] = str(error)

    worker = threading.Thread(target=reset)
    try:
        worker.start()
        for _ in range(200):                     # until the reset waits on the audit insert
            if conn.execute("select count(*) from pg_stat_activity where wait_event_type = 'Lock'"
                            " and query ilike '%%insert into action_log%%'").fetchone()[0]:
                break
            time.sleep(0.05)
        else:
            pytest.fail("the reset never waited on action_log")
        times.append(RESET_CLOSES)               # midnight passes while it waits
    finally:
        blocker.rollback()
        blocker.close()
        worker.join(30)
    assert "moved" not in result and "window closed" in result["error"]
    assert ledger.opening(conn).detail == TEST_DETAIL and ledger.archived(conn) == []
    assert conn.execute("select count(*) from action_log where tool = 'ledger.reset_test_opening'").fetchone() == (0,)


def test_show_dates_the_opening_in_eastern(schema_url, monkeypatch, capsys):
    """Finding 5: an opening at 20:00 Eastern on 29 Nov is 01:00 UTC on 30 Nov. `show` says 29 Nov."""
    from vending import db as db_module

    monkeypatch.setattr(db_module, "connect", lambda: open_in(*schema_url))
    monkeypatch.setattr(ledger, "db_now", lambda conn: AFTER)
    evening = datetime(2026, 11, 30, 1, 0, tzinfo=timezone.utc)
    with open_in(*schema_url) as conn:
        ledger.record(conn, "opening", 60000, "opening capital", "kevin", evening)
    assert ledger.main(["show"]) == 0
    line = next(x for x in capsys.readouterr().out.splitlines() if x.startswith("Opening capital"))
    assert "2026-11-29" in line and "2026-11-30" not in line and "Eastern" in line
