from datetime import date, datetime, time, timedelta
from fractions import Fraction

import pytest

from tests.test_ledger import EASTERN, UNIT_COST, deliver, open_ledger, sell
from tests.conftest import store_poll
from vending import ledger, money_series


def test_daily_series_reconciles_and_marks_events(conn):
    open_ledger(conn)
    deliver(conn, "r1")
    deliver(conn, "r2", off_cycle=True)
    sell(conn)
    ledger.record_tokens(conn, "0.0421", "model", 10, 5, "run-1",
                         at=datetime(2026, 11, 8, 12, tzinfo=EASTERN))
    ledger.record(conn, "adjustment", -25, "deposit", "tester",
                  datetime(2026, 11, 8, 13, tzinfo=EASTERN))
    days = money_series.daily(conn, date(2026, 11, 7), date(2026, 11, 9))
    restock, sale, empty = days
    assert restock.purchases_cents == 2474 and restock.visit_fees_cents == 2500
    assert {e.kind for e in restock.events} == {"restock_visit", "midweek_visit", "receipt"}
    assert sum(e.amount_cents for e in restock.events if e.kind == "receipt") == 2474
    assert sale.units_sold == 5 and sale.cost_of_sales_cents == 5 * UNIT_COST
    assert sale.cost_of_sales_complete and sale.token_spend_cents == Fraction(421, 100)
    assert [(e.kind, e.reference) for e in sale.events] == [("tokens", "run-1")]
    assert empty.cash_balance_cents == sale.cash_balance_cents
    start = datetime.combine(date(2026, 11, 7), time.min, EASTERN) - timedelta(microseconds=1)
    end = datetime.combine(date(2026, 11, 10), time.min, EASTERN) - timedelta(microseconds=1)
    pnl = ledger.pnl(conn, start, end)
    for field, expected in {"takings_cents": pnl.takings.cents, "sales_tax_cents": pnl.takings.tax_cents,
                            "revenue_ex_tax_cents": pnl.revenue_ex_tax_cents,
                            "units_sold": pnl.units_sold, "cost_of_sales_cents": pnl.cost_of_sales_cents,
                            "shrinkage_cents": pnl.shrinkage_cents, "visit_fees_cents": pnl.visit_fees_cents,
                            "token_spend_cents": pnl.token_spend_cents, "adjustments_cents": pnl.adjustments_cents,
                            "purchases_cents": pnl.receipts_cents, "net_cents": pnl.net_cents}.items():
        assert money_series.cumulative(days, field)[-1] == expected
    assert sale.net_worth_cents == sale.cash_balance_cents + sale.inventory_cents


def test_missing_cost_and_price_changes_are_visible(conn):
    open_ledger(conn)
    store_poll(conn, "one", "2026-11-07T17:00:00Z", {"15": (250, 100)})
    store_poll(conn, "two", "2026-11-08T17:00:00Z", {"15": (255, 101)})
    day = money_series.daily(conn, date(2026, 11, 8), date(2026, 11, 8))[0]
    assert day.units_sold == 1 and not day.cost_of_sales_complete
    assert day.inventory_cents is None and day.net_worth_cents is None
    assert any(event.kind == "price_change" for event in day.events)
    assert any("no known cost" in note for note in day.notes)


def test_tax_rounding_and_eastern_midnight(conn):
    open_ledger(conn)
    ledger.record(conn, "adjustment", 17, "previous day", "tester",
                  datetime(2026, 11, 7, 0, 0, tzinfo=EASTERN) - timedelta(microseconds=1))
    store_poll(conn, "one", "2026-11-07T12:00:00Z", {"15": (5, 0)})
    store_poll(conn, "two", "2026-11-08T05:00:00Z", {"15": (5, 1)})
    store_poll(conn, "three", "2026-11-09T05:00:00Z", {"15": (5, 2)})
    days = money_series.daily(conn, date(2026, 11, 7), date(2026, 11, 9))
    assert [d.units_sold for d in days] == [0, 1, 1]
    assert [d.sales_tax_cents for d in days] == [0, 0, 1]
    assert sum(d.sales_tax_cents for d in days) == 1
    assert sum(d.adjustments_cents for d in days) == 0


def test_cli_json_and_invalid_range(conn, monkeypatch, capsys):
    import json
    from contextlib import nullcontext
    open_ledger(conn)
    monkeypatch.setattr(money_series.db, "connect", lambda: nullcontext(conn))
    assert money_series.main(["--start", "2026-11-07", "--end", "2026-11-07", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["date"] == "2026-11-07"
    with pytest.raises(ValueError):
        money_series.daily(conn, date(2026, 11, 8), date(2026, 11, 7))
