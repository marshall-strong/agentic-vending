"""Tests for the ledger: the pure arithmetic without a database, then the balance and
P&L on a throwaway schema with synthetic polls, receipts and entries, and the two
tools through the MCP client."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction
from zoneinfo import ZoneInfo

import pytest

psycopg = pytest.importorskip("psycopg")

# The ledger reads the database, so even the pure tests need psycopg installed.
from vending.ledger import (  # noqa: E402
    LedgerError,
    dollars_to_cents,
    money,
    takings_between,
    week_of,
)

EASTERN = ZoneInfo("America/New_York")
T0 = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)   # a Sunday


def at(days: float) -> datetime:
    return T0 + timedelta(days=days)


# --- Pure ---------------------------------------------------------------------------

def test_money_rounds_half_up_and_keeps_the_sign():
    assert money(60000) == "600.00"
    assert money(Fraction(-1237)) == "-12.37"
    assert money(Fraction(421, 100)) == "0.04"      # 4.21 cents: a model call
    assert money(Fraction(1, 2)) == "0.01"
    assert money(Fraction(-1, 2)) == "-0.01"
    assert money(None) is None


def test_dollars_to_cents_is_exact_and_refuses_floats():
    assert dollars_to_cents("600.00") == 60000
    assert dollars_to_cents("0.0421") == Fraction(421, 100)
    assert dollars_to_cents(Decimal("-3.50")) == -350
    with pytest.raises(LedgerError):
        dollars_to_cents(2.5)
    with pytest.raises(LedgerError):
        dollars_to_cents("six hundred")


def test_week_starts_on_saturday():
    # Sunday 1 Nov 2026 belongs to the week that began Saturday 31 Oct.
    assert week_of(datetime(2026, 11, 1, 9, 0, tzinfo=EASTERN)) == date(2026, 10, 31)
    assert week_of(datetime(2026, 11, 7, 0, 0, tzinfo=EASTERN)) == date(2026, 11, 7)
    assert week_of(datetime(2026, 11, 6, 23, 59, tzinfo=EASTERN)) == date(2026, 10, 31)


def test_takings_are_counted_from_the_last_poll_before_the_opening():
    rows = [
        (at(-1), "15", 1000), (at(-1), "16", 500),      # before the opening: the baseline
        (at(1), "15", 1250), (at(1), "16", 500),
        (at(2), "15", 1250), (at(2), "16", 800),
    ]
    t = takings_between(rows, T0, at(3), EASTERN)
    assert (t.start_poll, t.end_poll, t.cents, t.note) == (at(-1), at(2), 550, None)
    assert [(w.week, w.takings_cents) for w in t.by_week] == [(date(2026, 10, 31), 550)]


def test_without_a_poll_before_the_opening_the_first_one_after_is_the_baseline():
    rows = [(at(1), "15", 1000), (at(2), "15", 1300)]
    t = takings_between(rows, T0, at(3), EASTERN)
    assert (t.start_poll, t.cents) == (at(1), 300)
    assert "first poll after" in t.note


def test_a_counter_that_goes_down_starts_again_instead_of_negative_sales():
    rows = [(at(0), "15", 1000), (at(1), "15", 200), (at(2), "15", 450)]
    assert takings_between(rows, T0, at(3), EASTERN).cents == 250


def test_tax_is_rounded_per_week_then_summed():
    # 1234 cents of takings holds 91.41 cents of tax; two weeks of it is 182, not round(182.81) = 183.
    rows = [(at(0), "15", 0), (at(1), "15", 1234), (at(8), "15", 2468)]
    t = takings_between(rows, T0, at(9), EASTERN)
    assert [(w.week, w.takings_cents, w.tax_cents) for w in t.by_week] == [
        (date(2026, 10, 31), 1234, 91), (date(2026, 11, 7), 1234, 91)]
    assert (t.cents, t.tax_cents) == (2468, 182)


def test_polls_after_the_window_are_ignored():
    rows = [(at(0), "15", 0), (at(1), "15", 100), (at(5), "15", 900)]
    assert takings_between(rows, T0, at(2), EASTERN).cents == 100


def test_no_polls_is_zero_with_a_note():
    t = takings_between([], T0, at(1), EASTERN)
    assert (t.cents, t.tax_cents, t.by_week) == (0, 0, ())
    assert "no polls" in t.note


# --- Database -------------------------------------------------------------------------

from tests.conftest import open_in, store_poll  # noqa: E402
from tests.test_server import call, db  # noqa: E402,F401  (db is a fixture)
from tests.test_sync_requests import BODY, fulfilled, receipt  # noqa: E402
from vending import ledger  # noqa: E402
from vending.purchase_requests import record as record_request, store  # noqa: E402

OPENED = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
# One 12-pack at $12.37, one unit broken: the 11 survivors cost 1237/11 cents each.
UNIT_COST = Fraction(1237, 11)


def open_ledger(conn, cents=60000):
    return ledger.record(conn, "opening", cents, "opening capital", "marshall", OPENED)


def deliver(conn, request_id, off_cycle=False, resolved_at="2026-11-07T16:00:00Z"):
    """A fulfilled request for slot 15: 12 bought at $12.37, 8 loaded, 3 to the closet, 1 lost."""
    record_request(conn, request_id, OPENED + timedelta(days=5), off_cycle, BODY)
    store(conn, request_id, fulfilled(request_id, receipt(resolved_at=resolved_at)), NOW)


def sell(conn, units=5, price=250):
    """Two polls of slot 15 after the delivery: `units` sold at `price` cents each."""
    store_poll(conn, "a", "2026-11-07T17:00:00+00:00", {"15": (price, 100)})
    store_poll(conn, "b", "2026-11-08T17:00:00+00:00", {"15": (price, 100 + units)})


def test_no_opening_entry_means_no_balance(conn):
    assert ledger.balance(conn) is None


def test_the_opening_entry_is_recorded_once_and_logged(conn):
    entry = open_ledger(conn)
    assert (entry.kind, entry.amount_cents, entry.at) == ("opening", 60000, OPENED)
    with pytest.raises(LedgerError, match="already has an opening entry"):
        open_ledger(conn)
    assert conn.execute("select tool, outcome from action_log").fetchall() == [("ledger.opening", "done")]
    with pytest.raises(LedgerError):
        ledger.record(conn, "opening", -1, "x", "marshall")
    with pytest.raises(LedgerError):
        ledger.record(conn, "tokens", 5, "x", "harness")      # spend is a debit
    with pytest.raises(LedgerError):
        ledger.record(conn, "adjustment", 5, "", "marshall")  # needs a detail
    with pytest.raises(LedgerError):
        ledger.record(conn, "adjustment", 5, "x", " ")        # needs a name


def test_the_balance_is_opening_plus_takings_less_receipts_fees_and_spend(conn):
    open_ledger(conn)
    deliver(conn, "r1")
    deliver(conn, "r2", off_cycle=True)
    sell(conn, units=5, price=250)                       # 1250 cents of takings, tax 93 (1250 x 8/108 = 92.59)
    ledger.record_tokens(conn, "0.0421", "claude-sonnet-5-5", 8120, 640, "msg_01", at=NOW - timedelta(days=1))
    ledger.record(conn, "adjustment", -350, "bottle deposit, per David", "marshall", NOW - timedelta(days=1))
    b = ledger.balance(conn, NOW)
    assert (b.opening_cents, b.takings.cents, b.takings.tax_cents) == (60000, 1250, 93)
    assert (b.receipts_cents, b.receipt_lines) == (2474, 2)
    assert (b.visit_fees_cents, b.visits) == (2500, 1)
    assert (b.token_spend_cents, b.adjustments_cents) == (Fraction(421, 100), -350)
    assert b.balance_cents == 60000 + 1250 - 2474 - 2500 - Fraction(421, 100) - 350
    assert b.spendable_cents == b.balance_cents - 93
    assert money(b.spendable_cents) == "558.29"          # 55921.79 - 93 = 55828.79 -> 558.29
    assert any("first poll after" in n for n in b.notes)   # no poll before the opening in this fixture


def test_the_visit_fee_comes_from_config_when_set(conn):
    from psycopg.types.json import Jsonb
    open_ledger(conn)
    deliver(conn, "r2", off_cycle=True)
    conn.execute("insert into config (key, value) values ('midweek_fee_cents', %s)", (Jsonb(3000),))
    assert ledger.balance(conn, NOW).visit_fees_cents == 3000


def test_entries_outside_the_window_do_not_count(conn):
    open_ledger(conn)
    ledger.record(conn, "adjustment", 1000, "late", "marshall", NOW + timedelta(days=1))
    assert ledger.balance(conn, NOW).adjustments_cents == 0
    assert ledger.balance(conn, NOW + timedelta(days=2)).adjustments_cents == 1000


def test_pnl_costs_sales_at_weighted_average_and_values_what_is_left(conn):
    open_ledger(conn)
    deliver(conn, "r1")                                  # 8 in the slot, 3 in the closet
    sell(conn, units=5, price=250)
    p = ledger.pnl(conn, OPENED, NOW)
    assert (p.takings.cents, p.takings.tax_cents, p.revenue_ex_tax_cents) == (1250, 93, 1157)
    assert (p.units_sold, p.units_costed, p.cost_of_sales_complete) == (5, 5, True)
    assert p.cost_of_sales_cents == 5 * UNIT_COST
    assert p.gross_margin_cents == 1157 - 5 * UNIT_COST
    assert (p.receipts_cents, p.visit_fees_cents, p.token_spend_cents) == (1237, 0, 0)
    assert p.net_cents == p.gross_margin_cents
    assert (p.inventory_machine_cents, p.inventory_closet_cents) == (3 * UNIT_COST, 3 * UNIT_COST)
    assert p.inventory_cents == 6 * UNIT_COST and p.inventory_complete


def test_pnl_says_when_sales_have_no_cost(conn):
    open_ledger(conn)
    sell(conn, units=5, price=250)                       # sales, but no receipts at all
    p = ledger.pnl(conn, OPENED, NOW)
    assert (p.takings.cents, p.cost_of_sales_cents, p.units_sold) == (1250, None, 0)
    assert any("no receipts are stored" in n for n in p.notes)


def test_pnl_window_cuts_sales_and_inventory_at_its_end(conn):
    open_ledger(conn)
    deliver(conn, "r1")
    sell(conn, units=5, price=250)                       # sold at 2026-11-08 17:00
    before = ledger.pnl(conn, OPENED, datetime(2026, 11, 8, 0, 0, tzinfo=timezone.utc))
    assert (before.units_sold, before.inventory_machine_cents) == (0, 8 * UNIT_COST)
    after = ledger.pnl(conn, OPENED, NOW)
    assert (after.units_sold, after.inventory_machine_cents) == (5, 3 * UNIT_COST)


# --- Through the MCP client -------------------------------------------------------------

def test_get_account_balance_asks_for_an_opening_entry_first(db):  # noqa: F811
    result = call("get_account_balance")
    assert not result.isError
    reply = result.structuredContent
    assert reply["success"] is False and "python -m vending.ledger open" in reply["message"]
    assert db.execute("select tool, outcome from action_log").fetchall() == [("get_account_balance", "failed")]


def test_get_account_balance_end_to_end(db):  # noqa: F811
    open_ledger(db)
    deliver(db, "r1")
    sell(db, units=5, price=250)
    reply = call("get_account_balance").structuredContent
    assert reply["success"]
    assert (reply["opening"], reply["revenue"], reply["receipts"]) == ("600.00", "12.50", "12.37")
    assert (reply["balance"], reply["tax_set_aside"], reply["spendable"]) == ("600.13", "0.93", "599.20")
    assert db.execute("select outcome from action_log where tool = 'get_account_balance'").fetchall() == [("done",)]


def test_get_pnl_end_to_end(db):  # noqa: F811
    open_ledger(db)
    deliver(db, "r1")
    sell(db, units=5, price=250)
    reply = call("get_pnl", {"start": "2026-11-01", "end": "2026-11-09"}).structuredContent
    assert reply["success"]
    assert (reply["revenue"], reply["revenue_ex_tax"], reply["units_sold"]) == ("12.50", "11.57", 5)
    assert reply["cost_of_sales"] == money(5 * UNIT_COST) == "5.62"
    assert reply["inventory"] == money(6 * UNIT_COST) == "6.75"
    assert reply["weeks"] == [{"week": "2026-11-07", "revenue": "12.50", "tax_set_aside": "0.93"}]
    assert call("get_pnl", {"start": "2026-11-09", "end": "2026-11-01"}).structuredContent["success"] is False


def test_get_pnl_defaults_to_the_opening_day(db):  # noqa: F811
    assert "python -m vending.ledger open" in call("get_pnl").structuredContent["message"]
    open_ledger(db)
    reply = call("get_pnl").structuredContent
    assert reply["success"] and reply["start"].startswith("2026-11-01")


# --- Command line ---------------------------------------------------------------------------

def test_cli_opens_shows_and_lists(schema_url, monkeypatch, capsys):
    from vending import db as db_module
    monkeypatch.setattr(db_module, "connect", lambda: open_in(*schema_url))
    assert ledger.main(["show"]) == 1
    assert "no opening entry" in capsys.readouterr().out
    assert ledger.main(["open", "--amount", "600.00", "--by", "marshall", "--at", "2026-11-01T00:00:00Z"]) == 0
    assert ledger.main(["open", "--amount", "600.00", "--by", "marshall"]) == 1
    assert "already has an opening entry" in capsys.readouterr().err
    assert ledger.main(["tokens", "--cost", "0.0421", "--model", "m", "--in", "10", "--out", "5",
                        "--reference", "msg_01"]) == 0
    assert ledger.main(["adjust", "--amount", "-3.50", "--detail", "deposit", "--by", "marshall"]) == 0
    assert ledger.main(["show"]) == 0
    out = capsys.readouterr().out
    assert "Spendable" in out and "596.46" in out     # 600.00 - 0.0421 - 3.50, no sales
    assert ledger.main(["entries"]) == 0
    assert capsys.readouterr().out.count("\n") == 3


def test_cli_refuses_a_schema_missing_migrations(database_url, monkeypatch, capsys):
    import uuid
    from vending import db as db_module
    schema = f"test_{uuid.uuid4().hex[:8]}"
    with open_in(database_url, "public") as admin:
        admin.execute(f"create schema {schema}")
        try:
            monkeypatch.setattr(db_module, "connect", lambda: open_in(database_url, schema))
            assert ledger.main(["show"]) == 2
            assert "python -m vending.db migrate" in capsys.readouterr().err
            with open_in(database_url, schema) as conn:
                assert conn.execute("select to_regclass('schema_migrations')").fetchone() == (None,)
        finally:
            admin.execute(f"drop schema {schema} cascade")
