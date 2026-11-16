"""Tests for the money series: days and weeks cut from one read of the window, each line adding up
to `ledger.pnl` over the same window and each day's cash to `ledger.balance` at its end, on a
throwaway schema with synthetic polls, receipts and entries. The pure tests at the end build days
by hand."""

import json
from contextlib import nullcontext
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from fractions import Fraction

import pytest

psycopg = pytest.importorskip("psycopg")

from tests.conftest import store_poll  # noqa: E402
from tests.test_ledger import EASTERN, OPENED, UNIT_COST, deliver, open_ledger, sell  # noqa: E402
from tests.test_sync_requests import later  # noqa: E402
from vending import ledger, money_series  # noqa: E402
from vending.money_series import as_json, by_week, cumulative, daily, weekly  # noqa: E402

# Every day in these tests is over by then, so none of them is "so far" whatever today is.
AFTER = datetime(2027, 1, 31, tzinfo=timezone.utc)
# Each series line and the `ledger.Pnl` figure it must add up to.
PNL = {
    "takings_cents": lambda p: p.takings.cents, "sales_tax_cents": lambda p: p.takings.tax_cents,
    "revenue_ex_tax_cents": lambda p: p.revenue_ex_tax_cents, "units_sold": lambda p: p.units_sold,
    "units_costed": lambda p: p.units_costed, "cost_of_sales_cents": lambda p: p.cost_of_sales_cents,
    "gross_profit_known_cents": lambda p: p.gross_margin_cents, "shrinkage_units": lambda p: p.shrinkage_units,
    "shrinkage_cents": lambda p: p.shrinkage_cents, "visits": lambda p: p.visits,
    "visit_fees_cents": lambda p: p.visit_fees_cents, "token_spend_cents": lambda p: p.token_spend_cents,
    "adjustments_cents": lambda p: p.adjustments_cents, "net_known_cents": lambda p: p.net_cents,
    "purchases_cents": lambda p: p.receipts_cents, "capital_cents": lambda p: p.capital_cents,
    "tax_remitted_cents": lambda p: p.tax_remitted_cents,
}


def window(first: date, last: date, opened: datetime = OPENED) -> tuple[datetime, datetime]:
    """The `ledger.pnl` window for Eastern days `first` to `last`, as the series counts them."""
    start = datetime.combine(first, time.min, EASTERN).astimezone(timezone.utc) - timedelta(microseconds=1)
    end = datetime.combine(last + timedelta(days=1), time.min, EASTERN).astimezone(timezone.utc)
    return max(start, opened), end - timedelta(microseconds=1)


def assert_adds_up_to_pnl(conn, rows, first: date, last: date, opened: datetime = OPENED) -> None:
    p = ledger.pnl(conn, *window(first, last, opened))
    for name, figure in PNL.items():
        assert cumulative(rows, name)[-1] == figure(p), name
    assert rows[-1].inventory_cents == p.inventory_cents


def assert_cash_is_the_balance(conn, days) -> None:
    for d in days:
        if d.counted:
            end = window(d.date, d.date)[1]
            b = ledger.balance(conn, end)
            assert (d.cash_balance_cents, d.tax_owed_cents) == (b.balance_cents, b.tax_owed_cents), d.date


# --- Days and weeks against the ledger -------------------------------------------------------

def test_a_restock_a_sale_and_spend_add_up_to_pnl_by_day_and_by_week(conn):
    open_ledger(conn)                                     # Saturday 31 Oct, 20:00 Eastern
    deliver(conn, "r1", losses=[later(2, at="2026-11-08T10:00:00Z")])      # Sat 7 Nov; 2 dented on Sun 8
    deliver(conn, "r2", off_cycle=True)                   # a midweek visit the same day: $25.00
    sell(conn)                                            # 5 sold, seen at 12:00 Eastern on Sun 8 Nov
    ledger.record_tokens(conn, "0.0421", "model", 10, 5, "run-1", at=datetime(2026, 11, 8, 12, tzinfo=EASTERN))
    ledger.record(conn, "adjustment", -25, "deposit", "tester", datetime(2026, 11, 8, 13, tzinfo=EASTERN))
    for n, ref in enumerate(("run-2a", "run-2b")):
        ledger.record_tokens(conn, "0.10", "model", 10, 5, ref, at=datetime(2026, 11, 9, 12, n, tzinfo=EASTERN))
    ledger.record(conn, "capital", 10000, "top-up", "tester", datetime(2026, 11, 9, 14, tzinfo=EASTERN))
    ledger.record_remittance(conn, "0.50", "tester", datetime(2026, 11, 10, 9, tzinfo=EASTERN))

    first, last = date(2026, 11, 6), date(2026, 11, 14)
    days = daily(conn, first, last, now=AFTER)
    assert [d.date for d in days] == [first + timedelta(days=n) for n in range(9)]
    restock, sale, spend = days[1:4]
    assert (restock.purchases_cents, restock.visits, restock.visit_fees_cents) == (2474, 1, 2500)
    assert {e.kind for e in restock.events} == {"restock_visit", "midweek_visit", "receipt"}
    assert sum(e.amount_cents for e in restock.events if e.kind == "receipt") == 2474
    assert (sale.units_sold, sale.units_costed, sale.cost_of_sales_cents) == (5, 5, 5 * UNIT_COST)
    assert (sale.shrinkage_units, sale.shrinkage_cents, sale.costs_complete) == (2, 2 * UNIT_COST, True)
    assert sale.token_spend_cents == Fraction(421, 100) and sale.adjustments_cents == -25
    assert sale.net_cents == sale.net_known_cents == (sale.revenue_ex_tax_cents - 7 * UNIT_COST - 25
                                                      - Fraction(421, 100))
    assert [(e.kind, e.reference, e.amount_cents) for e in sale.events] == [("tokens", "run-1", Fraction(421, 100))]
    # Two calls are one event for the day, with no single reference.
    assert [(e.detail, e.reference, e.amount_cents) for e in spend.events] == [("2 model calls", None, 20)]
    assert (spend.capital_cents, days[4].tax_remitted_cents) == (10000, 50)
    # Net worth leaves out the sales tax still owed: it was never ours.
    assert sale.tax_owed_cents == ledger.balance(conn, window(sale.date, sale.date)[1]).tax_owed_cents > 0
    assert sale.net_worth_cents == sale.cash_balance_cents - sale.tax_owed_cents + sale.inventory_cents

    # Every line adds up to the ledger's P&L over the window, and so does every single day,
    # except tax: a day's tax is what it added to its week's rounded tax, not its own rounding.
    assert_adds_up_to_pnl(conn, days, first, last)
    for d in days:
        p = ledger.pnl(conn, *window(d.date, d.date))
        for name, figure in PNL.items():
            if name not in ("sales_tax_cents", "revenue_ex_tax_cents", "gross_profit_known_cents", "net_known_cents"):
                assert getattr(d, name) == figure(p), (d.date, name)
        assert d.inventory_cents == p.inventory_cents
    assert_cash_is_the_balance(conn, days)

    weeks = by_week(days, date(2026, 10, 31))
    assert [(w.week, w.number, w.days, w.partial) for w in weeks] == [
        (date(2026, 10, 31), 1, 1, True), (date(2026, 11, 7), 2, 7, False), (date(2026, 11, 14), 3, 1, True)]
    assert weeks[0].notes == ("partial week: 1 of 7 days counted, Fri 06 Nov to Fri 06 Nov",)
    assert_adds_up_to_pnl(conn, weeks, first, last)
    whole = weeks[1]
    assert whole.sales_tax_cents == ledger.pnl(conn, *window(date(2026, 11, 7), date(2026, 11, 13))).takings.tax_cents
    assert whole.cash_balance_cents == days[7].cash_balance_cents
    assert weekly(conn, first, last, now=AFTER) == weeks


def test_a_counter_misread_or_restart_is_read_as_the_ledger_reads_it(conn):
    open_ledger(conn)
    for at, units in (("2026-11-02T17:00:00Z", 100), ("2026-11-03T17:00:00Z", 103),
                      ("2026-11-04T04:00:00Z", 1),                 # 23:00 Eastern on the 3rd: a misread
                      ("2026-11-04T17:00:00Z", 105),               # back above 103: 2 sold, not 104
                      ("2026-11-05T17:00:00Z", 2),                 # a board swap...
                      ("2026-11-06T17:00:00Z", 4)):                # ...confirmed: 2 sold since it restarted
        store_poll(conn, at, at, {"15": (100, units)})
    days = daily(conn, date(2026, 11, 3), date(2026, 11, 6), now=AFTER)
    assert [d.units_sold for d in days] == [3, 2, 0, 2]
    assert [d.takings_cents for d in days] == [300, 200, 0, 200]
    assert_adds_up_to_pnl(conn, days, date(2026, 11, 3), date(2026, 11, 6))
    assert_cash_is_the_balance(conn, days)
    # Why the days are cut from one window rather than each being its own: a window that starts
    # just after the misread takes it as its baseline and books the gap as sales.
    assert ledger.pnl(conn, *window(date(2026, 11, 4), date(2026, 11, 4))).units_sold == 104
    # A series that starts there matches that P&L, as promised, but its cash is still the balance.
    later_start = daily(conn, date(2026, 11, 4), date(2026, 11, 6), now=AFTER)
    assert later_start[0].units_sold == 104
    assert_adds_up_to_pnl(conn, later_start, date(2026, 11, 4), date(2026, 11, 6))
    assert_cash_is_the_balance(conn, later_start)


def test_days_are_eastern_and_1_nov_2026_has_25_hours(conn):
    open_ledger(conn)                                     # 20:00 Eastern (EDT) on Saturday 31 Oct
    for at, units in (("2026-10-31T23:00:00Z", 100),      # 19:00 EDT, before the opening: the baseline
                      ("2026-11-01T04:30:00Z", 101),      # 00:30 EDT on 1 Nov
                      ("2026-11-02T04:30:00Z", 103),      # 23:30 EST, still 1 Nov after the clocks went back
                      ("2026-11-02T05:00:00Z", 104)):     # midnight EST sharp: 2 Nov
        store_poll(conn, at, at, {"15": (250, units)})
    ledger.record(conn, "adjustment", 1, "last moment of 1 Nov", "tester",
                  datetime(2026, 11, 2, 4, 59, 59, 999999, tzinfo=timezone.utc))
    ledger.record(conn, "adjustment", 2, "first moment of 2 Nov", "tester",
                  datetime(2026, 11, 2, 5, 0, tzinfo=timezone.utc))
    opening_day, long_day, after = daily(conn, date(2026, 10, 31), date(2026, 11, 2), now=AFTER)
    assert [d.units_sold for d in (opening_day, long_day, after)] == [0, 3, 1]
    assert [d.adjustments_cents for d in (opening_day, long_day, after)] == [0, 1, 2]
    assert opening_day.counted_from == OPENED and not opening_day.whole
    assert "counted from the ledger's opening at 20:00 Eastern" in opening_day.notes
    assert long_day.whole and long_day.counted_from == datetime(2026, 11, 1, 4, 0, tzinfo=timezone.utc)
    assert after.counted_from == datetime(2026, 11, 2, 5, 0, tzinfo=timezone.utc)
    assert_adds_up_to_pnl(conn, [opening_day, long_day, after], date(2026, 10, 31), date(2026, 11, 2))


def test_tax_is_what_each_day_adds_to_its_weeks_rounded_tax(conn):
    open_ledger(conn)
    ledger.record(conn, "adjustment", 17, "the last moment of the day before", "tester",
                  datetime(2026, 11, 7, 0, 0, tzinfo=EASTERN) - timedelta(microseconds=1))
    store_poll(conn, "one", "2026-11-07T12:00:00Z", {"15": (5, 0)})
    store_poll(conn, "two", "2026-11-08T05:00:00Z", {"15": (5, 1)})    # midnight Eastern sharp: the 8th
    store_poll(conn, "three", "2026-11-09T05:00:00Z", {"15": (5, 2)})
    days = daily(conn, date(2026, 11, 7), date(2026, 11, 9), now=AFTER)
    assert [d.units_sold for d in days] == [0, 1, 1]
    # 5 cents holds 0.37 of tax, rounded to 0; the week's 10 cents holds 0.74, rounded to 1.
    assert [d.sales_tax_cents for d in days] == [0, 0, 1]
    assert sum(d.adjustments_cents for d in days) == 0      # stamped on the 6th, so outside


def test_week_one_opens_on_a_thursday_and_is_two_days_long(conn):
    opened = datetime(2026, 12, 3, 14, 0, tzinfo=timezone.utc)          # Thursday 3 Dec, 09:00 Eastern
    ledger.record_tokens(conn, "0.80", "model", 10, 5, "dry run", at=opened - timedelta(minutes=1))
    ledger.record(conn, "opening", 60000, "opening capital", "marshall", opened)
    store_poll(conn, "early", "2026-12-03T13:00:00Z", {"15": (250, 100)})
    store_poll(conn, "at", "2026-12-03T14:00:00Z", {"15": (250, 102)})  # at the opening: the baseline
    store_poll(conn, "thu", "2026-12-03T20:00:00Z", {"15": (250, 105)})
    store_poll(conn, "fri", "2026-12-04T20:00:00Z", {"15": (250, 106)})
    store_poll(conn, "sat", "2026-12-05T20:00:00Z", {"15": (250, 108)})
    ledger.record_tokens(conn, "0.40", "model", 10, 5, "live run", at=opened + timedelta(hours=1))

    days = daily(conn, date(2026, 12, 2), date(2026, 12, 5), now=AFTER)
    before, thursday, friday, saturday = days
    assert not before.counted and before.notes == ("before the ledger's opening on 2026-12-03, so not competition money",)
    assert thursday.counted_from == opened and not thursday.whole
    assert [d.units_sold for d in days] == [0, 3, 1, 2]                 # the 2 before the opening aren't ours
    assert [d.token_spend_cents for d in days] == [0, 40, 0, 0]         # nor is the dry run
    assert_adds_up_to_pnl(conn, days, date(2026, 12, 2), date(2026, 12, 5), opened)
    assert days[-1].cash_balance_cents == ledger.balance(conn, window(saturday.date, saturday.date)[1]).balance_cents

    weeks = weekly(conn, date(2026, 12, 2), date(2026, 12, 5), now=AFTER)
    assert [(w.week, w.number, w.first_day, w.last_day, w.days, w.partial) for w in weeks] == [
        (date(2026, 11, 28), 1, date(2026, 12, 3), date(2026, 12, 4), 2, True),
        (date(2026, 12, 5), 2, date(2026, 12, 5), date(2026, 12, 5), 1, True)]
    assert weeks[0].notes == ("partial week: 2 of 7 days counted, Thu 03 Dec to Fri 04 Dec, "
                              "the first from the opening at 09:00",
                              "4 of 4 units sold have no known cost, so gross profit and net are unknown")
    assert (weeks[0].units_sold, weeks[0].token_spend_cents) == (4, 40)
    shown = json.dumps([as_json(r) for r in days + weeks])
    assert "0.80" not in shown and "dry run" not in shown and "development" not in shown


def test_spend_before_the_opening_is_left_out(conn):
    """Development model calls before the opening aren't competition money, so no day shows them."""
    ledger.record_tokens(conn, "0.80", "model", 10, 5, "test dry run",
                         at=datetime(2026, 10, 30, 12, tzinfo=EASTERN))
    open_ledger(conn)                       # 1 Nov 00:00 UTC, the evening of 31 Oct in Eastern time
    ledger.record_tokens(conn, "0.40", "model", 10, 5, "live run",
                         at=datetime(2026, 11, 1, 12, tzinfo=EASTERN))
    days = daily(conn, date(2026, 10, 30), date(2026, 11, 1), now=AFTER)
    before, opening_day, after = days
    assert before.token_spend_cents == 0 and before.events == () and before.cash_balance_cents is None
    assert any("before the ledger's opening" in note for note in before.notes)
    assert opening_day.token_spend_cents == 0
    assert after.token_spend_cents == 40 and [e.reference for e in after.events] == ["live run"]
    assert cumulative(days, "token_spend_cents")[-1] == 40
    # The balance notes the development spend for people; nothing here repeats it.
    assert not any("0.80" in note or "development" in note for d in days for note in d.notes)


def test_no_opening_says_nothing_is_competition_money(conn):
    ledger.record_tokens(conn, "0.08", "model", 10, 5, "test dry run",
                         at=datetime(2026, 11, 8, 12, tzinfo=EASTERN))
    store_poll(conn, "a", "2026-11-08T12:00:00Z", {"15": (250, 100)})
    store_poll(conn, "b", "2026-11-08T18:00:00Z", {"15": (250, 104)})
    day = daily(conn, date(2026, 11, 8), date(2026, 11, 8), now=AFTER)[0]
    assert (day.token_spend_cents, day.units_sold, day.events, day.cash_balance_cents) == (0, 0, (), None)
    assert day.notes == (money_series.NO_OPENING,) and not day.counted
    assert weekly(conn, date(2026, 11, 8), date(2026, 11, 8), now=AFTER)[0].number is None


def test_missing_costs_make_gross_profit_and_net_unknown(conn):
    open_ledger(conn)
    store_poll(conn, "one", "2026-11-07T17:00:00Z", {"15": (250, 100)})
    store_poll(conn, "two", "2026-11-08T17:00:00Z", {"15": (255, 101)})    # 1 sold, no receipts
    day = daily(conn, date(2026, 11, 8), date(2026, 11, 8), now=AFTER)[0]
    assert (day.units_sold, day.units_costed, day.cost_of_sales_complete) == (1, 0, False)
    assert day.gross_profit_cents is None and day.net_cents is None
    # The synthetic counters' takings are units x price, so the new price lifts them: 25755 - 25000.
    assert day.gross_profit_known_cents == day.net_known_cents == day.revenue_ex_tax_cents == 755 - 56
    assert day.inventory_cents is None and day.net_worth_cents is None
    assert [e.detail for e in day.events if e.kind == "price_change"] == ["slot 15: 2.50 to 2.55 (seen at a poll)"]
    assert any("1 of 1 units sold have no known cost" in note for note in day.notes)
    assert any("no receipts are stored" in note for note in day.notes)
    assert by_week([day], date(2026, 10, 31))[0].net_cents is None


def test_a_day_without_a_poll_and_a_day_not_over_say_so(conn):
    open_ledger(conn)
    store_poll(conn, "a", "2026-11-07T17:00:00Z", {"15": (250, 100)})
    store_poll(conn, "b", "2026-11-09T17:00:00Z", {"15": (250, 103)})
    quiet, sold = daily(conn, date(2026, 11, 8), date(2026, 11, 9), now=datetime(2026, 11, 9, 18, tzinfo=timezone.utc))
    assert any("no good poll this day" in note for note in quiet.notes) and quiet.ended
    assert sold.units_sold == 3 and not sold.ended and not sold.whole
    assert any("so far" in note for note in sold.notes)


def test_the_series_reads_the_database_the_same_number_of_times_however_long(conn):
    class Counting:
        """The connection, counting what is asked of it."""
        def __init__(self, inner):
            self.inner, self.queries = inner, 0

        def execute(self, *args, **kwargs):
            self.queries += 1
            return self.inner.execute(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    open_ledger(conn)
    deliver(conn, "r1")
    sell(conn)
    short, long = Counting(conn), Counting(conn)
    daily(short, date(2026, 11, 7), date(2026, 11, 9), now=AFTER)
    daily(long, date(2026, 11, 7), date(2027, 1, 15), now=AFTER)
    assert short.queries == long.queries < 20


def test_an_empty_range_is_refused(conn):
    with pytest.raises(ValueError):
        daily(conn, date(2026, 11, 8), date(2026, 11, 7))


# --- The command line ------------------------------------------------------------------------

def test_cli_table_and_json(conn, monkeypatch, capsys):
    open_ledger(conn)
    sell(conn)                                            # 5 sold on the 8th with no receipts: not costed
    ledger.record_tokens(conn, "0.0421", "model", 10, 5, "run-1", at=datetime(2026, 11, 8, 12, tzinfo=EASTERN))
    monkeypatch.setattr(money_series.db, "connect", lambda: nullcontext(conn))

    assert money_series.main(["--start", "2026-10-30", "--end", "2026-11-08"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["Date", "Revenue", "Cost", "Gross", "Tokens", "Net", "Cash", "Inventory", "Notes"]
    assert lines[1] == "2026-10-30  not counted: before the ledger's opening on 2026-10-31, so not competition money"
    assert lines[-1].split()[:8] == ["2026-11-08", "11.57", "0.00", "?", "0.04", "?", "612.46", "?"]
    assert "5 of 5 units not costed" in lines[-1] and "net at known cost 11.53" in lines[-1]

    assert money_series.main(["--start", "2026-11-08", "--end", "2026-11-08", "--json"]) == 0
    day = json.loads(capsys.readouterr().out)[0]
    assert (day["date"], day["counted_from"], day["ended"], day["whole"]) == (
        "2026-11-08", "2026-11-08T05:00:00+00:00", True, True)
    assert (day["revenue_ex_tax"], day["token_spend"], day["net"], day["net_known"]) == ("11.57", "0.04", None, "11.53")
    assert (day["units_sold"], day["units_costed"], day["costs_complete"]) == (5, 0, False)
    assert Fraction(day["exact_cents"]["net_known"]) == 1157 - Fraction(421, 100)
    assert day["exact_cents"]["net"] is None and day["exact_cents"]["token_spend"] == "421/100"
    assert day["events"] == [{"at": "2026-11-08T17:00:00+00:00", "kind": "tokens", "detail": "1 model call",
                              "reference": "run-1", "amount": "0.04", "exact_cents": "421/100"}]

    assert money_series.main(["--weekly", "--start", "2026-11-01", "--end", "2026-11-08", "--json"]) == 0
    weeks = json.loads(capsys.readouterr().out)
    assert [(w["week"], w["number"], w["days"], w["partial"]) for w in weeks] == [
        ("2026-10-31", 1, 6, True), ("2026-11-07", 2, 2, True)]
    assert money_series.main(["--weekly", "--start", "2026-11-01", "--end", "2026-11-08"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].split()[:4] == ["Week", "of", "No", "Days"] and "partial: 6 of 7 days counted, Sun 01 Nov to Fri 06 Nov" in out[1]

    with pytest.raises(SystemExit):
        money_series.main(["--start", "2026-11-08", "--end", "2026-11-07"])


def test_cli_without_an_opening_asks_for_one_or_a_start(conn, monkeypatch, capsys):
    monkeypatch.setattr(money_series.db, "connect", lambda: nullcontext(conn))
    assert money_series.main([]) == 1
    assert "python -m vending.ledger open" in capsys.readouterr().err


# --- Pure: rows built by hand ---------------------------------------------------------------

def made(on: date, **lines) -> money_series.Day:
    """A whole, counted day with the given lines and zeros elsewhere."""
    blank = money_series._not_counted(on, "", AFTER)
    return replace(blank, counted_from=money_series._midnight(on), notes=(), cash_balance_cents=Fraction(0),
                   tax_owed_cents=Fraction(0),
                   inventory_cents=Fraction(0), inventory_complete=True, **lines)


def test_an_unknown_cost_makes_net_unknown_and_every_running_total_after_it():
    days = [made(date(2026, 11, 7), takings_cents=108, sales_tax_cents=8),
            made(date(2026, 11, 8), shrinkage_units=1, shrinkage_complete=False),
            made(date(2026, 11, 9), takings_cents=216, sales_tax_cents=16, units_sold=2, units_costed=2,
                 cost_of_sales_cents=Fraction(150))]
    assert [d.net_cents for d in days] == [100, None, 50]
    assert [d.gross_profit_cents for d in days] == [100, 0, 50]      # gross doesn't include shrinkage
    assert cumulative(days, "net_cents") == [100, None, None]
    assert cumulative(days, "net_known_cents") == [100, 100, 150]
    assert cumulative(days, "units_sold") == [0, 0, 2]
    week = by_week(days, date(2026, 11, 7))[0]
    assert (week.number, week.days, week.partial, week.net_cents, week.net_known_cents) == (1, 3, True, None, 150)
    with pytest.raises(ValueError, match="not a flow"):
        cumulative(days, "cash_balance_cents")


def test_a_week_of_seven_whole_days_is_not_partial_and_weeks_number_from_the_opening():
    days = [made(date(2026, 12, 5) + timedelta(days=n)) for n in range(8)]          # Sat 5 Dec to Sat 12 Dec
    weeks = by_week(days, date(2026, 12, 3))
    assert [(w.week, w.number, w.days, w.partial) for w in weeks] == [
        (date(2026, 12, 5), 2, 7, False), (date(2026, 12, 12), 3, 1, True)]
    assert by_week(days, None)[0].number is None
