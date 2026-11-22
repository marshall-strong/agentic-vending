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
from vending import dev_spend, key_dates, ledger, money_panel, money_series, stock  # noqa: E402
from vending.ledger import LedgerError, RESET_CLOSES, reset_refusal  # noqa: E402
from vending.purchase_requests import record as record_request  # noqa: E402
from vending.start import EASTERN, START  # noqa: E402
from vending.tools.account_balance import get_account_balance  # noqa: E402
from vending.tools.pnl import get_pnl  # noqa: E402

TEST_DETAIL = "opening capital (test state, 11 Nov; to be reset before launch)"
TEST_OPENED = datetime(2026, 11, 11, 19, 0, tzinfo=timezone.utc)
RESET_AT = datetime(2026, 11, 29, 20, 0, tzinfo=timezone.utc)       # Sun 29 Nov, 15:00 Eastern
REAL_OPENED = datetime(2026, 11, 29, 21, 0, tzinfo=timezone.utc)
AFTER = datetime(2026, 12, 2, 12, 0, tzinfo=timezone.utc)


# The database refuses an archive insert that commits from the cutoff on (migration 022). The shipped cutoff is
# fixed, 00:00 Eastern on Mon 30 Nov, with nothing a role can write to move it (Sol review of #222, round 4,
# finding 2). These tests run on any date, so each database test replaces the function in its own disposable
# schema with a far cutoff, the cutoff tests with a close one, and the fixture puts the shipped one back after.
CUTOFF_FUNCTION = "ledger_reset_closes()"


def set_cutoff(conn, closes):
    """Replace the database's cutoff in this test's schema only (the connection's search_path)."""
    conn.execute(f"create or replace function ledger_reset_closes() returns timestamptz language sql stable"
                 f" as $$ select timestamptz '{closes.isoformat()}' $$")


def shipped_cutoff(conn):
    """The function as migration 022 made it, to put back."""
    return conn.execute("select pg_get_functiondef(%s::regprocedure)", (CUTOFF_FUNCTION,)).fetchone()[0]


@pytest.fixture(autouse=True)
def _far_cutoff(request):
    if "schema_url" not in request.fixturenames:
        yield None
        return
    url = request.getfixturevalue("schema_url")
    with open_in(*url) as c:
        shipped = shipped_cutoff(c)
        set_cutoff(c, datetime(2100, 1, 1, tzinfo=timezone.utc))
    yield shipped
    with open_in(*url) as c:      # the pool may hand this schema to another test
        c.execute(shipped)


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


# --- Sol review, round 3 -----------------------------------------------------------------------

def test_the_database_cutoff_is_fixed_at_midnight_eastern_on_30_nov(conn, _far_cutoff):
    """Round 3, finding 1, and round 4, finding 2: the database enforces RESET_CLOSES, and no config row moves
    it. The cutoff the shipped migration makes reads nothing but its own text."""
    conn.execute(_far_cutoff)                    # the shipped function, as migration 022 made it
    assert conn.execute("select ledger_reset_closes()").fetchone()[0] == RESET_CLOSES
    conn.execute("insert into config (key, value, updated_by) values ('ledger_reset_closes_for_tests',"
                 " to_jsonb('2100-01-01T00:00:00+00:00'::text), 'test')")
    assert conn.execute("select ledger_reset_closes()").fetchone()[0] == RESET_CLOSES
    assert "config" not in _far_cutoff


def test_a_reset_that_pauses_past_the_cutoff_after_its_last_clock_read_does_not_commit(conn, monkeypatch):
    """Finding 1 (blocker): the reset reads the clock after its last write, then pauses before the commit.
    The cutoff passes in the pause. The database refuses the commit, so nothing moves."""
    seed(conn)
    set_cutoff(conn, conn.execute("select clock_timestamp() + interval '1.5 seconds'").fetchone()[0])
    reads = []

    def clock(c):                                # Python sees a time before the window closes, every read
        reads.append(1)
        if len(reads) == 2:                      # the last read, after the last write: then the pause
            time.sleep(2)
        return RESET_CLOSES - timedelta(minutes=1)

    monkeypatch.setattr(ledger, "db_now", clock)
    with pytest.raises(LedgerError, match="window closed"):
        ledger.reset_test_opening(conn, "kevin")
    assert len(reads) == 2
    assert ledger.opening(conn).detail == TEST_DETAIL and ledger.archived(conn) == []
    assert conn.execute("select count(*) from action_log where tool = 'ledger.reset_test_opening'").fetchone() == (0,)


def test_a_reset_inside_a_callers_transaction_refuses(conn, schema_url):
    """Round 3, finding 1, and round 4, finding 1 (blocker): the reset owns its transaction. Inside a caller's,
    it refuses before it reads or writes anything, so a caller can't run it (or `SET CONSTRAINTS ALL IMMEDIATE`
    with it) before midnight and commit after."""
    seed(conn)
    for begin in ("select 1", "set constraints all immediate"):
        outer = open_in(*schema_url)
        try:
            outer.autocommit = False
            outer.execute(begin)                 # an open transaction
            with pytest.raises(LedgerError, match="own transaction"):
                ledger.reset_test_opening(outer, "kevin", RESET_AT)
            outer.commit()
        finally:
            outer.close()
    assert ledger.opening(conn).detail == TEST_DETAIL and ledger.archived(conn) == []
    assert conn.execute("select count(*) from action_log where tool = 'ledger.reset_test_opening'").fetchone() == (0,)


def test_a_reset_after_an_outer_begin_refuses_and_archives_nothing(conn, schema_url):
    """Round 4, finding 1, Sol's regression: BEGIN on an autocommit connection, then the reset."""
    seed(conn)
    with open_in(*schema_url) as outer:
        outer.execute("begin")
        outer.execute("set constraints all immediate")
        with pytest.raises(LedgerError, match="own transaction"):
            ledger.reset_test_opening(outer, "kevin", RESET_AT)
        outer.execute("commit")
    assert ledger.opening(conn).detail == TEST_DETAIL and ledger.archived(conn) == []


def test_the_reset_commits_its_own_transaction_on_an_idle_connection(schema_url):
    """Round 4, finding 1: on a connection that isn't autocommit but has no open transaction, the reset opens and
    commits its own, so the move is committed when it returns."""
    with open_in(*schema_url) as c:
        seed(c)
    with open_in(*schema_url) as c:
        c.autocommit = False
        assert ledger.reset_test_opening(c, "kevin", RESET_AT).id == 1
        assert c.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    with open_in(*schema_url) as c:
        assert ledger.opening(c) is None and len(ledger.archived(c)) == 1


def test_a_reset_before_the_database_cutoff_commits(conn):
    seed(conn)
    set_cutoff(conn, conn.execute("select clock_timestamp() + interval '1 minute'").fetchone()[0])
    assert ledger.reset_test_opening(conn, "kevin", RESET_AT).id == 1
    assert ledger.opening(conn) is None and len(ledger.archived(conn)) == 1


# --- Sol review, round 4 -----------------------------------------------------------------------

def test_the_dry_run_is_bounded_by_its_own_clock(conn):
    """Finding 5: an entry or receipt dated after the dry run's clock doesn't fall before an opening recorded
    then, so the plan doesn't list it as falling before it; it counts it apart."""
    seed(conn)
    ledger.record(conn, "adjustment", -350, "bottle deposit", "kevin", RESET_AT + timedelta(hours=2))
    deliver(conn, "pr_late", resolved_at=(RESET_AT + timedelta(hours=3)).isoformat())
    plan = ledger.plan_reset(conn, RESET_AT)
    assert plan.later_entries == {"tokens": (2, Fraction(-48))}
    assert plan.receipts == (1, 1237)
    assert plan.after_now == 2


def test_the_dry_run_says_which_entries_fall_before_the_opening_recorded_now(schema_url, monkeypatch, capsys):
    """Finding 5: the dry run names its clock, lists only what falls before an opening recorded then, and says
    plainly that anything dated later is left out, rather than suggesting it be re-recorded."""
    from vending import db as db_module

    monkeypatch.setattr(db_module, "connect", lambda: open_in(*schema_url))
    monkeypatch.setattr(ledger, "db_now", lambda conn: RESET_AT)
    with open_in(*schema_url) as conn:
        seed(conn)
        ledger.record(conn, "adjustment", -350, "bottle deposit", "kevin", RESET_AT + timedelta(hours=2))
    assert ledger.main(["reset-test-opening", "--by", "kevin"]) == 0
    out = capsys.readouterr().out
    assert "2026-11-29 15:00 Eastern" in out                        # the dry run's clock
    assert "adjustment entries" not in out and "Re-record" not in out
    assert "1 entry or receipt line is dated after 2026-11-29 15:00 Eastern" in out
    assert "falls after an opening recorded now" in out


# --- Sol review, round 5 -----------------------------------------------------------------------

def test_a_recovery_opening_must_come_after_the_test_ledger_entries(conn):
    """Round 5, finding 1: the recovery opening must clear the test period's ledger entries too, not just
    the last test purchase and its receipt. A live run's model call at 19:30 Eastern on Sun 29 Nov, after
    the reset and before the 20:00 fill, lands in the real books under the old 19:00 choice. The
    documented choice — strictly after the latest test-period entry and the last test receipt's time,
    and at or before the fill's submission — keeps token spend at 0."""
    seed(conn)
    ledger.record_tokens(conn, "0.25", "claude-sonnet-5-5", 4000, 400, "live run turn",
                         at=datetime(2026, 11, 29, 19, 30, tzinfo=EASTERN))
    record_request(conn, "FILL", datetime(2026, 11, 29, 20, 0, tzinfo=EASTERN), False, {})

    def open_at(when):
        conn.execute("delete from ledger_entries where kind = 'opening'")
        ledger.record(conn, "opening", 60000, "opening capital", "kevin", when)
        return ledger.balance(conn, AFTER)

    # The old runbook's example: 19:00, after the last test purchase (28 Nov) and its receipt (12 Nov).
    old = open_at(datetime(2026, 11, 29, 19, 0, tzinfo=EASTERN))
    assert (old.receipts_cents, old.visits) == (0, 0) and old.takings.cents == 0
    assert old.token_spend_cents == Fraction(25)          # the 19:30 live call is in the real books
    assert old.balance_cents == 60000 - 25                # and it comes off the $600

    # The documented rule: strictly after the latest test-period entry (the 19:30 call) and the last
    # test receipt (12 Nov), at or before the fill's submission (20:00).
    documented_at = datetime(2026, 11, 29, 19, 45, tzinfo=EASTERN)
    documented = open_at(documented_at)
    (fill_sent,) = conn.execute(
        "select submitted_at from purchase_requests where request_id = 'FILL'").fetchone()
    assert documented_at < fill_sent
    assert documented.token_spend_cents == 0              # the call is development spend now
    assert (documented.receipts_cents, documented.receipt_lines, documented.visits) == (0, 0, 0)
    assert documented.balance_cents == 60000
    assert any("3 model-call entries totalling 0.73" in n and "development spend" in n
               for n in documented.notes)


def test_a_rejected_sunday_fill_leaves_no_fill_for_the_post_launch_check(conn):
    """Round 5, finding 2: after the start, the runbook's check must not mistake the first restock for the
    fill. The Sunday fill was rejected; the first request standing is Thursday's restock, sent after the
    start. The documented rule — the first non-rejected request submitted at or after the opening and
    before the start — picks no fill, like Key dates on the same records."""
    opening = datetime(2026, 11, 29, 19, 45, tzinfo=EASTERN)
    ledger.record(conn, "opening", 60000, "opening capital", "kevin", opening)
    record_request(conn, "FILL", datetime(2026, 11, 29, 20, 0, tzinfo=EASTERN), False, {})
    conn.execute("update purchase_requests set status = 'rejected' where request_id = 'FILL'")
    record_request(conn, "WEEK1", datetime(2026, 12, 3, 10, 0, tzinfo=EASTERN), False, {})

    # The check's list, from the runbook's query: the rejected Sunday fill is already left out.
    requests = conn.execute(
        "select request_id, submitted_at, off_cycle from purchase_requests"
        " where status <> 'rejected' order by submitted_at").fetchall()
    assert [r[0] for r in requests] == ["WEEK1"]

    # The documented inference, Key dates' rule: sent at or after the opening, and before the start.
    assert next((r for r in requests if r[1] >= opening and r[1] < START), None) is None
    # The old wording ("the first one that isn't off-cycle and was sent at or after the opening")
    # would have named the restock:
    old_rule = next((r for r in requests if not r[2] and r[1] >= opening), None)
    assert old_rule is not None and old_rule[0] == "WEEK1"
    # Key dates, whose rule the doc now states, picks no fill on the same records.
    assert key_dates._fill({"requests": [{"request_id": r[0], "submitted_at": r[1]}
                                         for r in requests]}) is None


# --- The recovery time, by command (Sol review of #222, round 6) --------------------------------------
#
# When the fill went in before the real opening was recorded, `ledger safe-opening --fill ID` picks the
# opening's time with the books' own clocks, in place of the runbook's hand-written SQL. Each test builds
# the records with the real recorders and checks the books after `open --at <suggested>`.

from tests.test_sync_requests import BODY, fulfilled, receipt  # noqa: E402
from vending.purchase_requests import store  # noqa: E402

REDRAW = datetime(2026, 11, 20, 12, 0, tzinfo=EASTERN)       # our slots first read after the redraw
FILL_SENT = datetime(2026, 11, 29, 20, 0, tzinfo=EASTERN)


def et(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=EASTERN)


def iso(moment):
    return moment.astimezone(timezone.utc).isoformat()


def after_the_reset(conn, fill=True):
    """The team ledger after step 3 on Sun 29 Nov, our slots read after the 20 Nov redraw, the fill sent."""
    seed(conn)
    ledger.reset_test_opening(conn, "kevin", RESET_AT)
    conn.execute("update slot_assignment set first_read_at = %s", (REDRAW,))
    if fill:
        record_request(conn, "FILL", FILL_SENT, False, BODY)


def deliver_at(conn, request_id, sent, *resolved, off_cycle=False, fee=None, fulfilled_at=None, **door):
    """A request sent at `sent` and fulfilled: one slot-15 line per time in `resolved` ($12.37 each)."""
    record_request(conn, request_id, sent, off_cycle, BODY, visit_fee_cents=fee)
    store(conn, request_id, fulfilled(request_id, *(receipt(resolved_at=iso(t)) for t in resolved),
                                      fulfilled_at=iso(fulfilled_at or max(resolved)),
                                      **{k: iso(v) for k, v in door.items()}), AFTER)


def open_at(conn, when):
    """`ledger open --at when`, again if need be: each test compares choices on the same records."""
    conn.execute("delete from ledger_entries where kind = 'opening'")
    ledger.record(conn, "opening", 60000, "opening capital", "kevin", when)
    return ledger.balance(conn, AFTER)


def fill_check(conn, opened):
    """Key dates' fill check on these records, as the dashboard reads them."""
    facts = key_dates.read(conn, AFTER, opened, ("15",))
    return key_dates._fill(facts), key_dates._fill_request(key_dates.FILL_REQUEST, AFTER, {"facts": facts})


def test_safe_opening_times_a_clashing_visit_by_resolved_at(conn):
    """Finding 1: two test visits to slot 15 submitted together, door times sharing a minute. The replay
    can't order them, so it times their lines by resolved_at (stock._usable_door_times): T2's line counts
    from 19:40, not its 18:05 door close. Door close alone would put the opening at 18:06 and book it."""
    after_the_reset(conn)
    sent = et(28, 10)
    deliver_at(conn, "T1", sent, et(29, 17, 50), door_opened_at=et(29, 18, 0), door_closed_at=et(29, 18, 4))
    deliver_at(conn, "T2", sent, et(29, 19, 40), door_opened_at=et(29, 18, 3), door_closed_at=et(29, 18, 5))
    s = ledger.safe_opening(conn, "FILL")
    assert (s.lower, s.suggested) == (et(29, 19, 40), et(29, 19, 41))
    assert "receipt line 0 of request T2" in s.lower_what
    door_close_choice = open_at(conn, et(29, 18, 6))
    assert door_close_choice.receipts_cents == 1237                     # T2's test purchase in the books
    chosen = open_at(conn, s.suggested)
    assert (chosen.receipts_cents, chosen.receipt_lines, chosen.token_spend_cents) == (0, 0, 0)
    assert stock.stock(conn, until=AFTER).slots == ()


def test_safe_opening_counts_a_visit_fee_with_no_lines(conn):
    """Finding 2: a fulfilled off-cycle test visit with no lines. The ledger times its fee at fulfilled_at
    (`visit_times`); the old SQL joined lines and never saw it."""
    after_the_reset(conn)
    record_request(conn, "V", et(28, 9), True, BODY, visit_fee_cents=2500)
    store(conn, "V", fulfilled("V", fulfilled_at=iso(et(29, 19, 20))), AFTER)
    assert stock.request_times(stock.receipt_lines(conn)).get("V") is None
    s = ledger.safe_opening(conn, "FILL")
    assert (s.lower, s.suggested) == (et(29, 19, 20), et(29, 19, 21))
    assert s.lower_what == "midweek visit fee of request V"
    assert open_at(conn, et(29, 19, 0)).visit_fees_cents == 2500        # before the fee: it's in the books
    chosen = open_at(conn, s.suggested)
    assert (chosen.visits, chosen.visit_fees_cents) == (0, 0)


def test_safe_opening_clears_a_late_model_call(conn):
    """Finding 3: a live run's call at 19:30:27 Eastern, after the reset and before the fill. The opening
    goes at the next whole minute, so the call is development spend, not real token spend."""
    after_the_reset(conn)
    ledger.record_tokens(conn, "0.25", "claude-sonnet-5-5", 4000, 400, "live run turn",
                         at=et(29, 19, 30) + timedelta(seconds=27))
    s = ledger.safe_opening(conn, "FILL")
    assert s.suggested == et(29, 19, 31)
    assert s.lower_what.startswith("ledger entry") and "(tokens, -0.25)" in s.lower_what
    chosen = open_at(conn, s.suggested)
    assert (chosen.token_spend_cents, chosen.balance_cents) == (0, 60000)


def test_safe_opening_refuses_while_a_test_request_is_pending(conn):
    """A test request still pending could land a receipt later, so no bound is safe yet. Same for a
    submission whose answer was lost. Nothing is written."""
    after_the_reset(conn)
    record_request(conn, "T", et(29, 19, 50), False, BODY)
    before = ledger.entries(conn)
    with pytest.raises(LedgerError, match="Resolve or wait for request T first"):
        ledger.safe_opening(conn, "FILL")
    assert ledger.entries(conn) == before and ledger.opening(conn) is None
    conn.execute("update purchase_requests set status = 'rejected' where request_id = 'T'")
    conn.execute("insert into purchase_submissions (at, plan_hash, body, status) values (%s, 'h', '{}', 'unknown')",
                 (et(29, 19, 55),))
    with pytest.raises(LedgerError, match="Resolve or wait for submission"):
        ledger.safe_opening(conn, "FILL")


def test_safe_opening_counts_a_resolved_test_request_by_when_it_was_sent(conn):
    """The pending request above, now resolved: rejected, so no receipt. It still sets the bound by its
    submitted_at."""
    after_the_reset(conn)
    record_request(conn, "T", et(29, 19, 50), False, BODY)
    store(conn, "T", {"id": "T", "status": "rejected", "decision_note": "test"}, AFTER)
    s = ledger.safe_opening(conn, "FILL")
    assert (s.lower, s.lower_what, s.suggested) == (et(29, 19, 50), "request T sent", et(29, 19, 51))


def test_safe_opening_says_when_no_time_works(schema_url, monkeypatch, capsys):
    """A test-period ledger entry after the fill went in: no opening clears it and still counts the fill.
    The command says so, exits 1 and records nothing."""
    from vending import db as db_module

    monkeypatch.setattr(db_module, "connect", lambda: open_in(*schema_url))
    with open_in(*schema_url) as conn:
        after_the_reset(conn)
        ledger.record_tokens(conn, "0.25", "claude-sonnet-5-5", 4000, 400, "live run turn", at=et(29, 20, 30))
        s = ledger.safe_opening(conn, "FILL")
        assert s.suggested is None and s.lower == et(29, 20, 30)
    assert ledger.main(["safe-opening", "--fill", "FILL"]) == 1
    out, err = capsys.readouterr()
    assert "Sun 29 Nov 2026 20:30:00 Eastern" in out and "No time works" in err and "Ask the team" in err
    with open_in(*schema_url) as conn:
        assert ledger.opening(conn) is None


def test_safe_opening_refuses_what_it_cannot_settle(conn):
    seed(conn)                                              # the test opening is still the opening
    record_request(conn, "FILL", FILL_SENT, False, BODY)
    with pytest.raises(LedgerError, match=r"reset \(step 3\) never ran"):
        ledger.safe_opening(conn, "FILL")
    ledger.reset_test_opening(conn, "kevin", RESET_AT)
    with pytest.raises(LedgerError, match="No purchase request NOPE"):
        ledger.safe_opening(conn, "NOPE")
    record_request(conn, "WEEK1", et(30, 9) + timedelta(days=3), False, BODY)     # Thu 3 Dec: a restock
    with pytest.raises(LedgerError, match="a restock, not the fill"):
        ledger.safe_opening(conn, "WEEK1")
    conn.execute("delete from slot_assignment")
    with pytest.raises(LedgerError, match="No slot assignment is stored"):
        ledger.safe_opening(conn, "FILL")
    conn.execute("update purchase_requests set status = 'rejected' where request_id = 'FILL'")
    with pytest.raises(LedgerError, match="was rejected"):
        ledger.safe_opening(conn, "FILL")
    ledger.record(conn, "opening", 60000, "opening capital", "kevin", et(29, 19, 45))
    with pytest.raises(LedgerError, match="already has an opening"):
        ledger.safe_opening(conn, "FILL")


def test_safe_opening_then_open_counts_the_fill_and_no_test_spend(schema_url, monkeypatch, capsys):
    """The recovery through the CLI: `safe-opening --fill`, then `open --at` with the time it printed.
    The books then hold the fill's receipt and no test spend, and Key dates counts the fill."""
    from vending import db as db_module

    monkeypatch.setattr(db_module, "connect", lambda: open_in(*schema_url))
    with open_in(*schema_url) as conn:
        after_the_reset(conn, fill=False)
        deliver_at(conn, "TEST", et(28, 10), et(29, 17))                         # a test purchase on Sunday
        ledger.record_tokens(conn, "0.25", "claude-sonnet-5-5", 4000, 400, "live run turn", at=et(29, 19, 30))
        deliver_at(conn, "FILL", FILL_SENT, et(30, 15))                         # stocked on Monday
    assert ledger.main(["safe-opening", "--fill", "FILL"]) == 0
    out = capsys.readouterr().out
    assert "Suggested opening: Sun 29 Nov 2026 19:31:00 Eastern" in out
    [command] = [line.strip() for line in out.splitlines() if "ledger open" in line]
    assert command == "python -m vending.ledger open --amount 600.00 --by NAME --at 2026-11-29T19:31:00-05:00"
    assert ledger.main(["open", "--amount", "600.00", "--by", "kevin", "--at", command.split("--at ")[1]]) == 0
    capsys.readouterr()
    with open_in(*schema_url) as conn:
        b = ledger.balance(conn, AFTER)
        assert b.opening_at == et(29, 19, 31)
        assert (b.token_spend_cents, b.visit_fees_cents) == (0, 0)
        assert (b.receipts_cents, b.receipt_lines) == (1237, 1)                 # the fill's receipt only
        assert b.balance_cents == 60000 - 1237
        fill, check = fill_check(conn, b.opening_at)
        assert fill["request_id"] == "FILL" and check["status"] == "done"
    monkeypatch.setattr(ledger, "db_now", lambda conn: AFTER)
    assert ledger.main(["show"]) == 0
    assert "Token spend" in capsys.readouterr().out
    assert ledger.main(["safe-opening", "--fill", "FILL"]) == 1                 # it is recorded now
    assert "already has an opening" in capsys.readouterr().err
