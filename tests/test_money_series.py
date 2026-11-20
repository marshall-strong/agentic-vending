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
from tests.test_ledger import (  # noqa: E402
    EASTERN, NOW, OPENED, SWAP_ESTIMATE, UNIT_COST, deliver, open_ledger, sell, swap_receipts, unsplit_swap,
)
from tests.test_sync_requests import BODY, fulfilled, later, receipt  # noqa: E402
from vending import ledger, money_series  # noqa: E402
from vending.money_series import as_json, by_week, cumulative, daily, weekly  # noqa: E402
from vending.purchase_requests import record as record_request, store  # noqa: E402

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
    "tax_remitted_cents": lambda p: p.tax_remitted_cents, "units_estimated": lambda p: p.units_estimated,
    "estimated_cost_of_sales_cents": lambda p: p.estimated_cost_of_sales_cents,
    "unattributed_stock_cents": lambda p: p.unattributed_stock_cents,
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
    assert weeks[0].notes == ("partial week: 1 of 7 days counted, Fri 06 Nov to Fri 06 Nov",
                              "no poll at or before 2026-11-06T04:59:59.999999+00:00, so takings start at the "
                              "first poll after it",
                              "1 of its days had no good poll, so their sales show on the next day that has one")
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
    # A window that starts just after the misread reads the counters from the same history as the
    # stock replay, so the misread is still a misread there: 2 sold, not the gap of 104 (#172 review).
    assert ledger.pnl(conn, *window(date(2026, 11, 4), date(2026, 11, 4))).units_sold == 2
    # A series that starts there matches that P&L, and its cash is still the balance.
    later_start = daily(conn, date(2026, 11, 4), date(2026, 11, 6), now=AFTER)
    assert [d.units_sold for d in later_start] == [2, 0, 2]
    assert_adds_up_to_pnl(conn, later_start, date(2026, 11, 4), date(2026, 11, 6))
    assert_cash_is_the_balance(conn, later_start)


def test_an_unsplit_swap_is_estimated_apart_the_same_in_the_pnl_and_the_series(conn):
    # #172 item 5: the P&L counted an unsplit swap window's cost as exact while the money
    # chart left it out. Now both leave it out of exact cost of sales and report the estimate apart.
    unsplit_swap(conn)                                     # the 2 units seen at 12:00 Eastern on Sun 8 Nov
    first, last = date(2026, 11, 6), date(2026, 11, 14)
    p = ledger.pnl(conn, *window(first, last))
    by_week_pnl = ledger.pnl_by_week(conn, *window(first, last))
    days = daily(conn, first, last, now=AFTER)
    weeks = by_week(days, date(2026, 10, 31))
    [sale_day] = [d for d in days if d.units_sold]
    [sale_week] = [w for w in weeks if w.units_sold]
    [sale_pnl_week] = [w for w in by_week_pnl if w.units_sold]
    expected = (2, 0, 0, 2, SWAP_ESTIMATE, False)
    for row in (p, sale_pnl_week, sale_day, sale_week):
        assert (row.units_sold, row.units_costed, row.cost_of_sales_cents, row.units_estimated,
                row.estimated_cost_of_sales_cents, row.cost_of_sales_complete) == expected, row
    # The estimate never goes into exact profit: gross profit and net stay unknown.
    assert (sale_day.gross_profit_cents, sale_day.net_cents) == (None, None)
    assert sale_day.gross_profit_known_cents == sale_day.revenue_ex_tax_cents
    assert any("estimate" in n for n in sale_day.notes) and any("estimate" in n for n in sale_week.notes)
    assert_adds_up_to_pnl(conn, days, first, last)
    assert_adds_up_to_pnl(conn, weeks, first, last)
    row = as_json(sale_day)
    assert (row["units_estimated"], row["estimated_cost_of_sales"]) == (2, ledger.money(SWAP_ESTIMATE))
    assert row["exact_cents"]["estimated_cost_of_sales"] == str(SWAP_ESTIMATE)
    assert (row["cost_of_sales"], row["gross_profit"], row["net"]) == ("0.00", None, None)


def books(net, capital, tax_set_aside, tax_remitted, unattributed_stock):
    """What a period moves net worth (cash + stock at cost) by: net at known costs, the cash moves that
    aren't profit (the tax set aside is still cash), less the stock that unattributed sales took."""
    return net + capital + tax_set_aside - tax_remitted - unattributed_stock


def test_the_books_balance_with_an_unsplit_swap_in_every_view(conn):
    # #172 review: the 300c of Red Bull stock the replay took for the 2 unsplit sales is in no
    # exact cost, estimate or profit, so it is its own reconciliation line, and opening + flows
    # = closing cash + stock at cost holds exactly in all four views. Opening $600, no stock.
    unsplit_swap(conn)
    end = window(date(2026, 11, 14), date(2026, 11, 14))[1]
    p = ledger.pnl(conn, OPENED, end)
    closing = ledger.balance(conn, end).balance_cents + p.inventory_cents
    # The reviewer's figures: $27.37 bought, $1.12 written off, nothing costed exactly.
    assert (ledger.money(p.receipts_cents), p.cost_of_sales_cents) == ("27.37", 0)
    assert ledger.money(p.shrinkage_cents) == "1.12" and p.unattributed_stock_cents == 300
    # The 1 Coke written off at the swap may be one of the sales: the line is provisional.
    assert p.unattributed_stock_provisional and any("provisional" in n for n in p.notes)
    assert 60000 + books(p.net_cents, p.capital_cents, p.takings.tax_cents, p.tax_remitted_cents,
                         p.unattributed_stock_cents) == closing
    # By week: this fixture has no capital or remittance, which `WeekPnl` doesn't carry.
    weeks = ledger.pnl_by_week(conn, OPENED, end)
    assert sum(w.unattributed_stock_cents for w in weeks) == 300
    assert 60000 + sum(books(w.net_cents, 0, w.tax_cents, 0, w.unattributed_stock_cents) for w in weeks) == closing
    # Day by day and week by week, each against its own end of day cash and stock.
    days = daily(conn, date(2026, 10, 31), date(2026, 11, 14), now=AFTER)
    for rows in (days, by_week(days, date(2026, 10, 31))):
        worth = Fraction(60000)
        for row in rows:
            worth += books(row.net_known_cents, row.capital_cents, row.sales_tax_cents, row.tax_remitted_cents,
                           row.unattributed_stock_cents)
            assert worth == row.cash_balance_cents + (row.inventory_cents or 0), row
        assert worth == closing
    [sale_day] = [d for d in days if d.units_sold]
    assert sale_day.unattributed_stock_provisional and any("provisional" in n for n in sale_day.notes)
    assert as_json(sale_day)["unattributed_stock"] == "3.00"


def test_an_unsplit_window_across_two_swaps_is_provisional_for_the_earlier_write_off(conn):
    # Review round 2: Coke -> Red Bull at 12:00 (1 Coke written off) -> Dasani at 14:00 (every Red
    # Bull pulled, nothing written off). The 2 units may be a Coke and a Dasani, so the $2.00 the
    # replay took from the Dasani is provisional, though the last swap wrote nothing off.
    swap_receipts(conn)
    record_request(conn, "r3", OPENED + timedelta(days=6), False, BODY)
    store(conn, "r3", fulfilled("r3", receipt(
        sku="DASANI", units_per_pack=10, units_bought=10, total_paid="10.00", unit_cost_paid="1.00",
        units_loaded=10, units_lost=0, loss_note=None, units_to_back_stock=0, units_pulled=10, pulled_sku="REDBULL",
        slot_reassigned=True, resolved_at="2026-11-08T14:00:00Z"), fulfilled_at="2026-11-08T14:30:00Z"), NOW)
    store_poll(conn, "a", "2026-11-07T17:00:00+00:00", {"15": (250, 100, 25000)})
    store_poll(conn, "b", "2026-11-08T17:00:00+00:00", {"15": (200, 102, 25450)})
    day = date(2026, 11, 8)
    p = ledger.pnl(conn, *window(day, day))
    [pnl_week] = ledger.pnl_by_week(conn, *window(day, day))
    [d] = daily(conn, day, day, now=AFTER)
    [w] = by_week([d], date(2026, 10, 31))
    for row in (p, pnl_week, d, w):
        assert (row.unattributed_stock_cents, row.unattributed_stock_provisional) == (200, True), row


def test_the_provisional_flag_does_not_need_the_debits_cost(conn):
    # Review round 2: the same single swap with the Red Bull's cost unknown. The debit has no cost,
    # but the 1 Coke written off at the swap may still be one of the sales.
    swap_receipts(conn, paid=None)
    store_poll(conn, "a", "2026-11-07T17:00:00+00:00", {"15": (250, 100, 25000)})
    store_poll(conn, "b", "2026-11-08T17:00:00+00:00", {"15": (400, 102, 25700)})
    day = date(2026, 11, 8)
    p = ledger.pnl(conn, *window(day, day))
    [d] = daily(conn, day, day, now=AFTER)
    for row in (p, d):
        assert (row.unattributed_stock_cents, row.unattributed_stock_provisional) == (0, True), row
        assert any("provisional" in n for n in row.notes), row.notes


def test_a_misread_just_before_the_opening_is_not_the_baseline_in_the_balance_or_the_chart(conn):
    # Review round 2: cents 25000 -> 24000 | opening | 25250. The whole history shows 24000 was a
    # misread, so the opening baseline is 25000: 250 cents taken, not 1250. One named rule
    # (`ledger.opening_rises`) gives the balance and the chart's cash, and they agree.
    store_poll(conn, "p1", "2026-10-31T22:00:00+00:00", {"15": (250, 100, 25000)})
    store_poll(conn, "p2", "2026-10-31T23:00:00+00:00", {"15": (250, 96, 24000)})    # just before the opening
    open_ledger(conn)                                                                 # 00:00 UTC on 1 Nov
    store_poll(conn, "p3", "2026-11-02T17:00:00+00:00", {"15": (250, 101, 25250)})
    end = window(date(2026, 11, 2), date(2026, 11, 2))[1]
    rows = ledger._counter_rows(conn, "lifetime_sales_cents", end)
    assert [r.amount for r in ledger.opening_rises(rows, OPENED, end)[2]] == [250]
    b = ledger.balance(conn, end)
    assert (b.balance_cents, b.tax_owed_cents) == (60250, 19)
    days = daily(conn, date(2026, 10, 31), date(2026, 11, 2), now=AFTER)
    assert (days[-1].cash_balance_cents, days[-1].tax_owed_cents) == (60250, 19)
    assert_cash_is_the_balance(conn, days)


def test_a_counter_drop_before_the_window_is_read_from_the_same_history_as_the_stock(conn):
    # #172 review: 110 -> 100 -> 98 -> 100. From the whole history 100 and 98 are a restart and 2
    # sold across the swap, as the stock replay reads them. A window starting after the first 100
    # took it as its baseline, read 98 as a misread and counted 0 sold, beside 2 estimated units:
    # "complete" cost of sales and a numeric net over sales it never counted.
    swap_receipts(conn)
    for session, at, price, units, cents in (
            ("t1", "2026-11-07T17:00:00+00:00", 250, 110, 30000),
            ("t2", "2026-11-07T18:00:00+00:00", 250, 100, 25000),
            ("t3", "2026-11-08T06:00:00+00:00", 250, 98, 24000),
            ("t4", "2026-11-08T17:00:00+00:00", 400, 100, 24700)):    # 700 cents fits no split
        store_poll(conn, session, at, {"15": (price, units, cents)})
    day = date(2026, 11, 8)
    p = ledger.pnl(conn, *window(day, day))
    [pnl_week] = ledger.pnl_by_week(conn, *window(day, day))
    [d] = daily(conn, day, day, now=AFTER)
    [w] = by_week([d], date(2026, 10, 31))
    for row in (p, pnl_week, d, w):
        assert (row.units_sold, row.units_costed, row.units_estimated, row.cost_of_sales_complete) == (2, 0, 2, False), row
    assert (d.net_cents, w.net_cents) == (None, None)
    assert not any("of 0 units" in n for n in p.notes + d.notes + w.notes)
    # Backstop: an estimated unit never leaves cost of sales complete, whatever the counts say.
    for row in (p, pnl_week, d, w):
        assert not replace(row, units_sold=0, units_costed=0).cost_of_sales_complete, row


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


def test_a_receipt_filled_in_before_the_opening_but_closed_after_it_is_charged_on_the_chart_too(conn):
    # Opening 10:00 Eastern; the worker filled in the line at 09:55, the door closed at 10:05. The
    # ledger charges it at door close, so the chart's purchases and cash must too.
    opened = datetime(2026, 11, 7, 15, 0, tzinfo=timezone.utc)
    ledger.record(conn, "opening", 60000, "opening capital", "marshall", opened)
    record_request(conn, "r1", opened - timedelta(days=1), False, BODY)
    store(conn, "r1", fulfilled("r1", receipt(resolved_at="2026-11-07T14:55:00Z"), fulfilled_at="2026-11-07T15:10:00Z",
                                door_opened_at="2026-11-07T14:58:00Z", door_closed_at="2026-11-07T15:05:00Z"), NOW)
    days = daily(conn, date(2026, 11, 7), date(2026, 11, 8), now=AFTER)
    assert days[0].purchases_cents == 1237
    assert [e.at for e in days[0].events if e.kind == "receipt"] == [datetime(2026, 11, 7, 15, 5, tzinfo=timezone.utc)]
    assert [e.at for e in days[0].events if e.kind.endswith("visit")] == [
        datetime(2026, 11, 7, 15, 5, tzinfo=timezone.utc)]
    assert_cash_is_the_balance(conn, days)
    assert_adds_up_to_pnl(conn, days, date(2026, 11, 7), date(2026, 11, 8), opened)


def open_minute_swap(conn, top_up: bool) -> None:
    """r2 swaps 9 Arizona (50c each) out for Coke (80c each) at $2.00, door open in the 17:30 minute,
    and the poll at 17:30:10 has $2.00 at both ends of the next window, 2 sold. With `top_up`, r0 tops
    up the Arizona at 16:35-16:40 first and may have set that price, so the window can't be read as r2's."""
    from tests.test_machine_state import fulfilled as fulfilled_lines

    open_ledger(conn)
    fulfilled_lines(conn, "r1", datetime(2026, 11, 7, 15, tzinfo=timezone.utc), ("15", "ARIZONA", "primary", "5.00", 10, 0, 0))
    doors = [("r2", (17, 30), (18, 30))]
    if top_up:
        fulfilled_lines(conn, "r0", datetime(2026, 11, 7, 16, 36, tzinfo=timezone.utc), ("15", "ARIZONA", "primary", "1.00", 2, 0, 0))
        doors.append(("r0", (16, 35), (16, 40)))
    fulfilled_lines(conn, "r2", datetime(2026, 11, 7, 18, tzinfo=timezone.utc), ("15", "COKE20", "primary", "8.00", 10, 0, 0))
    conn.execute("update purchase_lines set units_pulled = 9, pulled_sku = 'ARIZONA', slot_reassigned = true"
                 " where request_id = 'r2'")
    for request_id, opened, closed in doors:
        conn.execute("update purchase_requests set door_opened_at = %s, door_closed_at = %s where request_id = %s",
                     (datetime(2026, 11, 7, *opened, tzinfo=timezone.utc),
                      datetime(2026, 11, 7, *closed, tzinfo=timezone.utc), request_id))
    store_poll(conn, "a", "2026-11-07T16:00:00+00:00", {"15": (250, 100, 25000)})
    store_poll(conn, "b", "2026-11-07T17:30:10+00:00", {"15": (200, 100, 25000)})
    store_poll(conn, "c", "2026-11-07T19:00:00+00:00", {"15": (200, 102, 25400)})


def test_sales_the_open_minute_no_longer_reads_as_new_are_estimated_not_costed(conn):
    # #172 review: r0's top-up may have set the $2.00, so the 2 sales may be Arizona or Coke. Main
    # read them as Coke at 80c; that 160c is now an unattributed stock debit, not exact cost of sales,
    # and the estimate is the midpoint of an Arizona (50c) and a Coke (80c): 130c for the 2.
    from vending import stock

    open_minute_swap(conn, top_up=True)
    [sale] = [s for s in stock.stock(conn).sales if s.units]
    assert (sale.product, sale.units, sale.cost_cents) == (None, 2, None) and sale.unattributed
    assert (sale.unattributed_cost_cents, sale.estimated_cost_cents) == (160, 130)
    day = daily(conn, date(2026, 11, 7), date(2026, 11, 7), now=AFTER)[0]
    assert (day.units_sold, day.units_costed, day.cost_of_sales_cents, day.cost_of_sales_complete) == (2, 0, 0, False)
    assert (day.units_estimated, day.estimated_cost_of_sales_cents, day.unattributed_stock_cents) == (2, 130, 160)
    assert day.net_cents is None
    assert_adds_up_to_pnl(conn, [day], date(2026, 11, 7), date(2026, 11, 7))


def test_sales_the_open_minute_reads_as_the_sole_visits_keep_their_exact_cost(conn):
    # The control: with r2 the only visit, its own swap set the $2.00 and the poll fell in its open
    # minute, so the 2 sales are Coke, resolved and exactly costed, with nothing estimated.
    from vending import stock

    open_minute_swap(conn, top_up=False)
    [sale] = [s for s in stock.stock(conn).sales if s.units]
    assert (sale.product, sale.units, sale.cost_cents, sale.estimated_cost_cents) == ("COKE20", 2, 160, None)
    day = daily(conn, date(2026, 11, 7), date(2026, 11, 7), now=AFTER)[0]
    assert (day.units_sold, day.units_costed, day.cost_of_sales_cents, day.cost_of_sales_complete) == (2, 2, 160, True)
    assert (day.units_estimated, day.unattributed_stock_cents) == (0, 0)
    assert_adds_up_to_pnl(conn, [day], date(2026, 11, 7), date(2026, 11, 7))


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


def test_a_receipt_with_no_total_paid_leaves_purchases_and_cash_incomplete_from_its_day_on(conn):
    open_ledger(conn)                                                       # $600.00
    record_request(conn, "r1", OPENED + timedelta(days=5), False, BODY)
    store(conn, "r1", fulfilled("r1", receipt(total_paid=None, unit_cost_paid=None)), NOW)   # Sat 7 Nov
    days = daily(conn, date(2026, 11, 7), date(2026, 11, 14), now=AFTER)
    bought, after = days[0], days[1]
    # The known figures stay, as `ledger.balance` gives them, but say they leave the line out.
    assert (bought.purchases_cents, bought.unpaid_lines, bought.purchases_complete) == (0, 1, False)
    assert (bought.cash_balance_cents, bought.cash_unpaid_lines, bought.cash_complete) == (60000, 1, False)
    assert bought.net_worth_cents is None
    assert any("1 receipt lines have no total_paid, so purchases leave them out" in n for n in bought.notes)
    # The next day bought nothing, but its cash still leaves the line out.
    assert after.purchases_complete and not after.cash_complete and after.net_worth_cents is None
    assert any("the cash balance leaves them out and net worth is unknown" in n for n in after.notes)
    assert_cash_is_the_balance(conn, days)

    first, second = by_week(days, date(2026, 10, 31))
    assert (first.unpaid_lines, first.purchases_complete, first.cash_complete) == (1, False, False)
    assert (second.unpaid_lines, second.purchases_complete, second.cash_complete) == (0, True, False)
    assert first.net_worth_cents is None and second.net_worth_cents is None
    assert any("purchases leave them out" in n for n in first.notes)
    assert any("the cash balance leaves them out" in n for n in second.notes)

    day = as_json(bought)
    assert (day["purchases"], day["purchases_complete"], day["unpaid_lines"]) == ("0.00", False, 1)
    assert (day["cash_balance"], day["cash_complete"], day["cash_unpaid_lines"]) == ("600.00", False, 1)
    assert day["net_worth"] is None and day["exact_cents"]["net_worth"] is None
    week = as_json(second)
    assert (week["cash_complete"], week["net_worth"]) == (False, None)
    table = money_series.table(days).splitlines()
    assert "purchases leave out 1 receipt lines with no total_paid" in table[1]
    assert "cash leaves out 1 receipt lines with no total_paid" in table[2]


def test_the_table_says_when_takings_have_no_poll_behind_them(conn):
    open_ledger(conn)                                                       # no polls at all
    days = daily(conn, date(2026, 11, 6), date(2026, 11, 8), now=AFTER)
    lines = money_series.table(days).splitlines()
    assert lines[1].split()[1] == "0.00"                                    # revenue reads 0.00...
    assert "no polls in the window, so takings are unknown and counted as 0" in lines[1]    # ...and says why
    assert all(money_series.NO_GOOD_POLL in line for line in lines[1:])
    weeks = money_series.table(by_week(days, date(2026, 10, 31))).splitlines()
    assert "no polls in the window" in weeks[1] and "1 of its days had no good poll" in weeks[1]
    assert "2 of its days had no good poll" in weeks[2]


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
    assert short.queries == long.queries < 25   # 20 since the opening filter (#124) reads the ledger twice


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
    return replace(blank, **{"counted_from": money_series._midnight(on), "notes": (),
                             "cash_balance_cents": Fraction(0), "tax_owed_cents": Fraction(0),
                             "inventory_cents": Fraction(0), "inventory_complete": True, **lines})


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


def test_an_unpriced_receipt_makes_net_worth_unknown_even_with_inventory_all_costed():
    priced = made(date(2026, 11, 7), cash_balance_cents=Fraction(60000))
    unpriced = replace(priced, unpaid_lines=1, cash_unpaid_lines=1)
    later_day = made(date(2026, 11, 8), cash_balance_cents=Fraction(60000), cash_unpaid_lines=1)
    assert priced.net_worth_cents == 60000 and priced.inventory_complete
    assert unpriced.net_worth_cents is None and later_day.net_worth_cents is None
    week = by_week([unpriced, later_day], date(2026, 11, 7))[0]
    assert (week.unpaid_lines, week.purchases_complete, week.cash_complete, week.net_worth_cents) == (
        1, False, False, None)


def test_a_week_of_seven_whole_days_is_not_partial_and_weeks_number_from_the_opening():
    days = [made(date(2026, 12, 5) + timedelta(days=n)) for n in range(8)]          # Sat 5 Dec to Sat 12 Dec
    weeks = by_week(days, date(2026, 12, 3))
    assert [(w.week, w.number, w.days, w.partial) for w in weeks] == [
        (date(2026, 12, 5), 2, 7, False), (date(2026, 12, 12), 3, 1, True)]
    assert by_week(days, None)[0].number is None
