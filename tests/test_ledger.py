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
    assert money(Fraction(-1, 10)) == "0.00"        # a tenth of a cent short is not "-0.00"
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


def test_one_misread_between_good_polls_is_not_booked_as_takings():
    rows = [(at(0), "15", 100000), (at(1), "15", 500), (at(2), "15", 100250)]
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
from tests.test_sync_requests import BODY, fulfilled, later, receipt  # noqa: E402
from vending import ledger  # noqa: E402
from vending.purchase_requests import record as record_request, store  # noqa: E402
from vending.tools.account_balance import get_account_balance  # noqa: E402

OPENED = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
# One 12-pack at $12.37, one unit broken: the 11 survivors cost 1237/11 cents each.
UNIT_COST = Fraction(1237, 11)


def open_ledger(conn, cents=60000):
    return ledger.record(conn, "opening", cents, "opening capital", "marshall", OPENED)


def deliver(conn, request_id, off_cycle=False, resolved_at="2026-11-07T16:00:00Z", visit_fee_cents=None, losses=()):
    """A fulfilled request for slot 15: 12 bought at $12.37, 8 loaded, 3 to the closet, 1 lost."""
    record_request(conn, request_id, OPENED + timedelta(days=5), off_cycle, BODY, visit_fee_cents=visit_fee_cents)
    store(conn, request_id, fulfilled(request_id, receipt(resolved_at=resolved_at, later_losses=list(losses))), NOW)


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
        ledger.record(conn, "capital", -100, "x", "marshall")       # a top-up is a credit
    with pytest.raises(LedgerError):
        ledger.record(conn, "tokens", 5, "x", "harness")            # spend is a debit
    with pytest.raises(LedgerError):
        ledger.record(conn, "tax_remitted", 5, "x", "marshall")     # so is a remittance
    with pytest.raises(LedgerError):
        ledger.record(conn, "adjustment", 0, "x", "marshall")       # records nothing
    with pytest.raises(LedgerError):
        ledger.record(conn, "adjustment", 5, "", "marshall")        # needs a detail
    with pytest.raises(LedgerError):
        ledger.record(conn, "adjustment", 5, "x", " ")              # needs a name


def test_the_balance_is_opening_plus_takings_less_receipts_fees_and_spend(conn):
    open_ledger(conn)
    deliver(conn, "r1")
    deliver(conn, "r2", off_cycle=True)
    sell(conn, units=5, price=250)                       # 1250 cents of takings, tax 93 (1250 x 8/108 = 92.59)
    ledger.record_tokens(conn, "0.0421", "claude-sonnet-5-5", 8120, 640, "msg_01", at=NOW - timedelta(days=1))
    ledger.record(conn, "adjustment", -350, "bottle deposit, per David", "marshall", NOW - timedelta(days=1))
    b = ledger.balance(conn, NOW)
    assert (b.opening_cents, b.capital_cents, b.takings.cents, b.takings.tax_cents) == (60000, 0, 1250, 93)
    assert (b.receipts_cents, b.receipt_lines) == (2474, 2)
    assert (b.visit_fees_cents, b.visits) == (2500, 1)
    assert (b.token_spend_cents, b.tax_remitted_cents, b.adjustments_cents) == (Fraction(421, 100), 0, -350)
    assert b.balance_cents == 60000 + 1250 - 2474 - 2500 - Fraction(421, 100) - 350
    assert b.tax_owed_cents == 93 and b.spendable_cents == b.balance_cents - 93
    assert money(b.spendable_cents) == "558.29"          # 55921.79 - 93 = 55828.79 -> 558.29
    assert any("first poll after" in n for n in b.notes)   # no poll before the opening in this fixture


def test_capital_and_a_tax_remittance_move_money_without_being_profit(conn):
    open_ledger(conn)
    sell(conn, units=5, price=250)                       # takings 1250, tax 93
    ledger.record(conn, "capital", 10000, "top-up from David", "marshall", NOW - timedelta(days=1))
    ledger.record_remittance(conn, "0.93", "marshall", NOW - timedelta(days=1), "email 8 Nov")
    b = ledger.balance(conn, NOW)
    assert (b.capital_cents, b.tax_remitted_cents) == (10000, 93)
    assert b.balance_cents == 60000 + 10000 + 1250 - 93
    assert b.tax_owed_cents == 0                           # the remittance settled the set-aside
    assert b.spendable_cents == b.balance_cents            # so nothing is held back twice
    p = ledger.pnl(conn, OPENED, NOW)
    assert (p.capital_cents, p.tax_remitted_cents) == (10000, 93)
    assert p.net_cents == p.revenue_ex_tax_cents == 1157   # neither is in the P&L


def test_the_visit_fee_comes_from_config_when_the_request_has_none(conn):
    from psycopg.types.json import Jsonb
    open_ledger(conn)
    deliver(conn, "r2", off_cycle=True)
    conn.execute("insert into config (key, value) values ('midweek_fee_cents', %s)", (Jsonb(3000),))
    assert ledger.balance(conn, NOW).visit_fees_cents == 3000


def test_the_fee_stored_on_the_request_wins_so_a_config_change_is_not_retroactive(conn):
    from psycopg.types.json import Jsonb
    open_ledger(conn)
    deliver(conn, "r2", off_cycle=True, visit_fee_cents=1500)
    conn.execute("insert into config (key, value) values ('midweek_fee_cents', %s)", (Jsonb(3000),))
    b = ledger.balance(conn, NOW)
    assert (b.visits, b.visit_fees_cents) == (1, 1500)


def test_entries_outside_the_window_do_not_count(conn):
    open_ledger(conn)
    ledger.record(conn, "adjustment", 1000, "late", "marshall", NOW + timedelta(days=1))
    assert ledger.balance(conn, NOW).adjustments_cents == 0
    assert ledger.balance(conn, NOW + timedelta(days=2)).adjustments_cents == 1000


def test_an_entry_made_just_now_counts_when_the_host_clock_is_behind_the_database(conn, monkeypatch):
    # An entry with no time is stamped by the database clock. "The balance now" must
    # use that clock too, or an entry made a moment ago drops out whenever the host
    # runs behind the database (seen at 0.7 s on a laptop with Postgres in Docker).
    class HostBehind(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) - timedelta(hours=1)

    monkeypatch.setattr(ledger, "datetime", HostBehind)
    open_ledger(conn)
    ledger.record(conn, "capital", 10000, "top-up", "marshall")
    assert ledger.balance(conn).capital_cents == 10000


def test_the_balance_inside_a_transaction_counts_an_entry_committed_since_it_began(conn, schema_url):
    # `now()` would stay at the start of this transaction and miss the other connection's entry.
    open_ledger(conn)
    with conn.transaction():
        conn.execute("select 1")
        with open_in(*schema_url) as other:
            ledger.record(other, "capital", 10000, "top-up", "marshall")
        assert ledger.balance(conn).capital_cents == 10000


def test_the_balance_time_is_utc_whatever_the_session_time_zone(conn):
    conn.execute("set timezone = 'America/New_York'")
    open_ledger(conn)
    as_of = ledger.balance(conn).as_of
    assert as_of.utcoffset() == timedelta(0)
    assert f"as of {as_of:%Y-%m-%d %H:%M} UTC" in get_account_balance(conn).message


def test_entries_dated_before_the_opening_are_outside_the_balance_and_said_so(conn):
    open_ledger(conn)
    ledger.record_tokens(conn, "0.50", "m", 1, 1, "dev", at=OPENED - timedelta(days=10))
    b = ledger.balance(conn, NOW)
    assert b.token_spend_cents == 0
    assert any("1 entries totalling -0.50 are dated before the opening" in n for n in b.notes)


def test_pnl_costs_sales_at_weighted_average_and_values_what_is_left(conn):
    open_ledger(conn)
    deliver(conn, "r1")                                  # 8 in the slot, 3 in the closet
    sell(conn, units=5, price=250)
    p = ledger.pnl(conn, OPENED, NOW)
    assert (p.takings.cents, p.takings.tax_cents, p.revenue_ex_tax_cents) == (1250, 93, 1157)
    assert (p.units_sold, p.units_costed, p.cost_of_sales_complete) == (5, 5, True)
    assert p.cost_of_sales_cents == 5 * UNIT_COST
    assert p.gross_margin_cents == 1157 - 5 * UNIT_COST
    assert (p.shrinkage_units, p.shrinkage_cents) == (0, 0)
    assert (p.receipts_cents, p.visit_fees_cents, p.token_spend_cents) == (1237, 0, 0)
    assert p.net_cents == p.gross_margin_cents
    assert (p.inventory_machine_cents, p.inventory_closet_cents) == (3 * UNIT_COST, 3 * UNIT_COST)
    assert p.inventory_cents == 6 * UNIT_COST and p.inventory_complete


def test_sales_with_no_receipts_count_as_sold_but_not_costed(conn):
    open_ledger(conn)
    sell(conn, units=5, price=250)                       # sales, but no receipts at all
    p = ledger.pnl(conn, OPENED, NOW)
    assert (p.takings.cents, p.units_sold, p.units_costed) == (1250, 5, 0)
    assert (p.cost_of_sales_cents, p.cost_of_sales_complete) == (0, False)
    assert p.net_cents == 1157                           # a figure, flagged, not "unknown"
    assert any("no receipts are stored" in n for n in p.notes)
    assert any("5 of 5 units sold have no known cost" in n for n in p.notes)


def test_sales_before_the_first_load_and_from_slots_never_loaded_are_counted_but_not_costed(conn):
    open_ledger(conn)
    deliver(conn, "r1", resolved_at="2026-11-07T16:00:00Z")
    store_poll(conn, "a", "2026-11-06T17:00:00+00:00", {"15": (250, 100), "16": (300, 48)})
    store_poll(conn, "b", "2026-11-07T10:00:00+00:00", {"15": (250, 103), "16": (300, 48)})   # 3 before the load
    store_poll(conn, "c", "2026-11-08T17:00:00+00:00", {"15": (250, 108), "16": (300, 50)})   # 5 after, 2 from 16
    p = ledger.pnl(conn, OPENED, NOW)
    assert (p.units_sold, p.units_costed, p.cost_of_sales_complete) == (10, 5, False)
    assert p.cost_of_sales_cents == 5 * UNIT_COST
    assert any("5 of 10 units sold have no known cost" in n for n in p.notes)


def test_lost_units_are_shrinkage_not_a_cost_the_rest_carry(conn):
    open_ledger(conn)
    deliver(conn, "r1", losses=[later(2, at="2026-11-08T10:00:00Z")])   # 2 of the 8 on the rail found dented
    sell(conn, units=5, price=250)                                         # then 5 sold at 17:00
    p = ledger.pnl(conn, OPENED, NOW)
    assert (p.shrinkage_units, p.shrinkage_cents, p.shrinkage_complete) == (2, 2 * UNIT_COST, True)
    assert p.cost_of_sales_cents == 5 * UNIT_COST
    assert (p.inventory_machine_cents, p.inventory_closet_cents) == (1 * UNIT_COST, 3 * UNIT_COST)
    # Every cent of the $12.37 is somewhere: sold, written off, or on hand.
    assert p.cost_of_sales_cents + p.shrinkage_cents + p.inventory_cents == 1237
    assert p.net_cents == 1157 - 5 * UNIT_COST - 2 * UNIT_COST


def test_a_quiet_window_still_has_a_net(conn):
    open_ledger(conn)
    deliver(conn, "r1")
    deliver(conn, "r2", off_cycle=True)
    ledger.record_tokens(conn, "0.40", "m", 10, 5, "msg_02", at=NOW - timedelta(days=1))
    p = ledger.pnl(conn, OPENED, NOW)                    # no polls, so no sales
    assert (p.units_sold, p.cost_of_sales_cents, p.cost_of_sales_complete) == (0, 0, True)
    assert p.net_cents == -2500 - 40
    assert any("no sales in the window" in n for n in p.notes)


def test_pnl_window_cuts_sales_and_inventory_at_its_end(conn):
    open_ledger(conn)
    deliver(conn, "r1")
    sell(conn, units=5, price=250)                       # sold at 2026-11-08 17:00
    before = ledger.pnl(conn, OPENED, datetime(2026, 11, 8, 0, 0, tzinfo=timezone.utc))
    assert (before.units_sold, before.inventory_machine_cents) == (0, 8 * UNIT_COST)
    after = ledger.pnl(conn, OPENED, NOW)
    assert (after.units_sold, after.inventory_machine_cents) == (5, 3 * UNIT_COST)


def test_purchases_and_the_stock_they_bring_go_by_the_same_clock(conn):
    open_ledger(conn)
    # Resolved on the 7th but the bridge marked the request fulfilled on the 9th.
    record_request(conn, "r1", OPENED + timedelta(days=5), False, BODY)
    store(conn, "r1", fulfilled("r1", receipt(resolved_at="2026-11-07T16:00:00Z"), fulfilled_at="2026-11-09T09:00:00Z"), NOW)
    p = ledger.pnl(conn, OPENED, datetime(2026, 11, 8, 0, 0, tzinfo=timezone.utc))
    assert (p.receipts_cents, p.inventory_machine_cents) == (1237, 8 * UNIT_COST)


# --- Through the MCP client -------------------------------------------------------------

def test_pnl_by_week_books_each_line_to_its_week_and_adds_up_to_pnl(conn):
    open_ledger(conn)                                     # Saturday 31 Oct, 20:00 Eastern
    ledger.record(conn, "adjustment", -350, "bottle deposit", "marshall", OPENED + timedelta(days=1))
    deliver(conn, "r1")
    deliver(conn, "r2", off_cycle=True)
    store_poll(conn, "a", "2026-11-07T17:00:00+00:00", {"15": (250, 100), "16": (300, 48)})
    store_poll(conn, "b", "2026-11-08T17:00:00+00:00", {"15": (250, 105), "16": (300, 50)})   # 2 from 16 uncosted
    ledger.record_tokens(conn, "0.40", "m", 10, 5, "msg_02", at=NOW - timedelta(days=1))
    end = NOW + timedelta(days=7)                         # a quiet third week
    weeks = ledger.pnl_by_week(conn, OPENED, end)
    assert [w.week for w in weeks] == [date(2026, 10, 31), date(2026, 11, 7), date(2026, 11, 14)]
    quiet_start, busy, quiet_end = weeks
    assert (quiet_start.adjustments_cents, quiet_start.units_sold, quiet_start.net_cents) == (-350, 0, -350)
    assert (busy.units_sold, busy.units_costed, busy.cost_of_sales_complete) == (7, 5, False)
    assert (busy.takings_cents, busy.tax_cents, busy.visits, busy.visit_fees_cents) == (1850, 137, 1, 2500)
    assert busy.cost_of_sales_cents == 5 * UNIT_COST and busy.token_spend_cents == 40
    assert quiet_end.net_cents == 0 and quiet_end.cost_of_sales_complete
    p = ledger.pnl(conn, OPENED, end)
    assert sum(w.takings_cents for w in weeks) == p.takings.cents
    assert sum(w.tax_cents for w in weeks) == p.takings.tax_cents
    for name in ("units_sold", "units_costed", "cost_of_sales_cents", "shrinkage_cents", "visit_fees_cents",
                 "visits", "token_spend_cents", "adjustments_cents", "net_cents"):
        assert sum(getattr(w, name) for w in weeks) == getattr(p, name), name


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
    assert (reply["opening"], reply["capital"], reply["revenue"], reply["receipts"]) == ("600.00", "0.00", "12.50", "12.37")
    assert (reply["balance"], reply["tax_set_aside"], reply["tax_owed"], reply["spendable"]) == ("600.13", "0.93", "0.93", "599.20")
    assert db.execute("select outcome from action_log where tool = 'get_account_balance'").fetchall() == [("done",)]


def test_get_pnl_end_to_end(db):  # noqa: F811
    open_ledger(db)
    deliver(db, "r1")
    sell(db, units=5, price=250)
    reply = call("get_pnl", {"start": "2026-11-01", "end": "2026-11-09"}).structuredContent
    assert reply["success"]
    assert (reply["revenue"], reply["revenue_ex_tax"], reply["units_sold"], reply["units_costed"]) == ("12.50", "11.57", 5, 5)
    assert reply["cost_of_sales"] == money(5 * UNIT_COST) == "5.62"
    assert (reply["cost_of_sales_complete"], reply["shrinkage"], reply["net"]) == (True, "0.00", "5.95")
    assert reply["inventory"] == money(6 * UNIT_COST) == "6.75"
    assert reply["weeks"] == [{"week": "2026-11-07", "revenue": "12.50", "tax_set_aside": "0.93"}]
    assert call("get_pnl", {"start": "2026-11-09", "end": "2026-11-01"}).structuredContent["success"] is False


def test_get_pnl_days_are_eastern_days(db):  # noqa: F811
    open_ledger(db)
    deliver(db, "r1")
    # 3 sold between 19:30 and 21:00 Eastern on Friday 7 Nov, which is already Saturday 8 Nov in UTC.
    store_poll(db, "a", "2026-11-08T00:30:00+00:00", {"15": (250, 100)})
    store_poll(db, "b", "2026-11-08T02:00:00+00:00", {"15": (250, 103)})
    friday = call("get_pnl", {"start": "2026-11-07", "end": "2026-11-07"}).structuredContent
    assert (friday["units_sold"], friday["revenue"]) == (3, "7.50")
    saturday = call("get_pnl", {"start": "2026-11-08", "end": "2026-11-08"}).structuredContent
    assert (saturday["units_sold"], saturday["revenue"]) == (0, "0.00")
    assert "Eastern days" in friday["message"]


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
    assert ledger.main(["capital", "--amount", "100.00", "--detail", "top-up", "--by", "marshall"]) == 0
    assert ledger.main(["remit", "--amount", "-1", "--by", "marshall"]) == 1
    assert "above zero" in capsys.readouterr().err
    assert ledger.main(["adjust", "--amount", "1.00", "--detail", "early", "--by", "marshall",
                        "--at", "2026-10-01T00:00:00Z"]) == 0
    assert "dated before the opening" in capsys.readouterr().err
    assert ledger.main(["show"]) == 0
    out = capsys.readouterr().out
    assert "Spendable" in out and "696.46" in out     # 600.00 - 0.0421 - 3.50 + 100.00; the early 1.00 is outside
    assert "Note: 1 entries totalling 1.00 are dated before the opening" in out
    assert ledger.main(["entries"]) == 0
    assert capsys.readouterr().out.count("\n") == 5


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
