"""The weekly P&L page: the weeks add up to `get_pnl`, unknown stays unknown, every value is
escaped, and the page only reads."""

from __future__ import annotations

import http.client
import threading
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction

import pytest

psycopg = pytest.importorskip("psycopg")

from vending import ledger  # noqa: E402  (after the importorskip)
from vending import pnl_report as pr  # noqa: E402
from vending.tools.pnl import Pnl, get_pnl  # noqa: E402

PRINTED = datetime(2026, 11, 11, 9, 30, tzinfo=pr.EASTERN)


def week(**overrides) -> ledger.WeekPnl:
    fields = dict(week=date(2026, 11, 7), takings_cents=1250, tax_cents=93, units_sold=5, units_costed=5,
                  cost_of_sales_cents=Fraction(6185, 11), shrinkage_units=0, shrinkage_cents=Fraction(0),
                  shrinkage_complete=True, visit_fees_cents=0, visits=0, token_spend_cents=Fraction(0),
                  adjustments_cents=Fraction(0))
    fields.update(overrides)
    return ledger.WeekPnl(**fields)


def totals(**overrides) -> Pnl:
    fields = dict(success=True, message="ok", start=datetime(2026, 11, 7, 5, tzinfo=timezone.utc),
                  end=datetime(2026, 11, 14, 4, 59, tzinfo=timezone.utc), revenue="12.50", tax_set_aside="0.93",
                  revenue_ex_tax="11.57", units_sold=5, units_costed=5, cost_of_sales="5.62",
                  cost_of_sales_complete=True, shrinkage="0.00", shrinkage_units=0, visit_fees="0.00",
                  token_spend="0.00", adjustments="0.00", net="5.95")
    fields.update(overrides)
    return Pnl(**fields)


# --- Pure: the query, the cells, escaping, print styles -----------------------------------------

def test_the_range_defaults_to_the_whole_run_and_refuses_a_bad_date():
    assert pr.parse("/") == (None, None)
    assert pr.parse("/?start=&end=") == (None, None)
    assert pr.parse("/?start=2026-11-01&end=2026-11-09") == (date(2026, 11, 1), date(2026, 11, 9))
    with pytest.raises(pr.BadRequest):
        pr.parse("/?start=yesterday")
    with pytest.raises(LookupError):
        pr.parse("/favicon.ico")


def test_a_date_far_outside_the_run_is_refused():
    for path in ("/?start=0001-01-01", "/?end=9999-12-31", "/?start=0001-01-08&end=2026-11-09"):
        with pytest.raises(pr.BadRequest):
            pr.parse(path)


def test_money_is_exact_to_the_cent_and_unknown_is_not_zero():
    assert pr.amount(Fraction(6185, 11)) == "5.62"           # 562.27 cents, rounded only when shown
    assert pr.amount(-2500) == "-25.00"
    shown = pr.amount(Fraction(0), known=False, note="0 of 5 units costed")
    assert "unknown" in shown and "0.00" not in shown and "0 of 5 units costed" in shown


def test_a_week_with_an_uncosted_sale_shows_unknown_cost_and_profit():
    row = pr.week_row(week(units_costed=3, cost_of_sales_cents=Fraction(300)), date(2026, 11, 1), date(2026, 11, 30))
    assert row.count('class="unknown"') == 2                 # cost of sales and profit
    assert "3 of 5 units costed" in row


def test_a_week_is_cut_to_the_range():
    assert pr.week_label(week(), date(2026, 11, 9), date(2026, 11, 11)) == "Mon 2026-11-09 to Wed 2026-11-11"


def test_every_value_is_escaped():
    report = pr.Report(None, None, PRINTED, totals(notes=["<script>alert(1)</script>"]), [week()])
    code, body = pr.render(report)
    assert code == 200
    assert "<script>" not in body and "&lt;script&gt;alert(1)&lt;/script&gt;" in body
    _, failed = pr.render(pr.Report(date(2026, 11, 9), None, PRINTED,
                                    totals(success=False, message='<img src=x onerror="x">')))
    assert "<img" not in failed and "&lt;img" in failed


def test_the_page_prints_without_its_form():
    _, body = pr.render(pr.Report(None, None, PRINTED, totals(), [week()]))
    assert "@media print" in body and '<form class="no-print"' in body
    assert "Printed 2026-11-11 09:30 EST" in body and "Sat 2026-11-07 to Fri 2026-11-13 (Eastern days)" in body


# --- Database -----------------------------------------------------------------------------------

from tests.conftest import CONNECT_TIMEOUT, open_in  # noqa: E402
from tests.test_ledger import OPENED, deliver, open_ledger, sell  # noqa: E402

END = date(2026, 11, 9)


def seed(conn):
    """Two weeks: an adjustment in the first (Sat 31 Oct), then a delivery, a paid midweek
    visit, 5 sales and a model call in the second (Sat 7 Nov)."""
    open_ledger(conn)
    ledger.record(conn, "adjustment", -350, "bottle deposit", "marshall", OPENED + timedelta(days=1))
    deliver(conn, "r1")
    deliver(conn, "r2", off_cycle=True, resolved_at="2026-11-07T18:00:00Z")
    sell(conn, units=5, price=250)
    ledger.record_tokens(conn, "0.40", "m", 10, 5, "msg_01", at=OPENED + timedelta(days=7, hours=20))


def test_the_weeks_match_get_pnl_and_add_up_to_its_totals(conn):
    seed(conn)
    report = pr.load(conn, None, END)
    t = report.totals
    assert t.success
    assert [w.week for w in report.weeks] == [date(2026, 10, 31), date(2026, 11, 7)]   # the quiet week too
    sold = [{"week": w.week.isoformat(), "revenue": ledger.money(w.takings_cents),
             "tax_set_aside": ledger.money(w.tax_cents)} for w in report.weeks if w.takings_cents]
    assert sold == t.weeks
    p = ledger.pnl(conn, t.start, t.end)
    for name in ("takings_cents", "tax_cents"):
        assert sum(getattr(w, name) for w in report.weeks) == getattr(p.takings, name.replace("takings_", ""))
    for name in ("units_sold", "units_costed", "cost_of_sales_cents", "shrinkage_units", "shrinkage_cents",
                 "visit_fees_cents", "visits", "token_spend_cents", "adjustments_cents", "net_cents"):
        assert sum(getattr(w, name) for w in report.weeks) == getattr(p, name), name
    first, second = report.weeks
    assert (first.adjustments_cents, first.net_cents) == (-350, -350)
    assert (second.visits, second.visit_fees_cents, second.token_spend_cents) == (1, 2500, 40)

    code, body = pr.render(report)
    assert code == 200
    for figure in (t.revenue, t.tax_set_aside, t.revenue_ex_tax, t.cost_of_sales, t.net):
        assert f'<td class="num">{figure}</td>' in body
    assert report.balance is not None and report.balance.tax_owed in body
    assert 'class="unknown"' not in body
    assert "rounded to the cent on its own" in body       # so a column can miss its total by a cent


def test_a_range_longer_than_the_page_shows_is_refused(conn):
    seed(conn)
    code, body = pr.render(pr.load(conn, date(2020, 1, 1), END))
    assert code == 400 and f"at most {pr.MAX_WEEKS} weeks" in body


def test_an_unknown_cost_renders_as_unknown(conn):
    open_ledger(conn)
    sell(conn, units=5, price=250)                           # sales, but no receipts at all
    report = pr.load(conn, None, END)
    assert report.totals.net == "11.57"                      # the ledger gives a figure, flagged
    _, body = pr.render(report)
    assert body.count('class="unknown"') == 4                # cost and profit, in the week and the total
    assert "0 of 5 units costed" in body
    assert body.split("<tfoot>")[1].split("</tfoot>")[0].endswith('<span class="unknown">unknown</span></td></tr>')


def test_a_short_range_shows_its_week_and_a_reversed_one_is_refused(conn):
    seed(conn)
    report = pr.load(conn, date(2026, 11, 7), date(2026, 11, 7))
    assert [w.week for w in report.weeks] == [date(2026, 11, 7)]
    code, body = pr.render(pr.load(conn, date(2026, 11, 9), date(2026, 11, 1)))
    assert code == 400 and "is before start" in body


def test_without_an_opening_entry_the_page_says_how_to_record_one(conn):
    code, body = pr.render(pr.load(conn, None, None))
    assert code == 200 and "python -m vending.ledger open" in body


@pytest.fixture
def read_only(schema_url, monkeypatch):
    """The report's own session settings, in the test schema."""
    url, schema = schema_url

    def connect():
        return psycopg.connect(url, autocommit=True, connect_timeout=CONNECT_TIMEOUT,
                               options=f"-c search_path={schema} {pr.OPTIONS}")
    monkeypatch.setattr(pr, "connect", connect)
    return connect


def test_the_session_refuses_writes_and_still_reads_the_report(conn, read_only):
    seed(conn)
    with read_only() as ro:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            ro.execute("insert into ledger_entries (kind, amount_cents, detail, recorded_by)"
                       " values ('capital', 1, 'x', 'x')")
        assert pr.load(ro, None, END).totals.success


def get(port: int, path: str, host: str = "127.0.0.1") -> tuple[int, str]:
    client = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        client.request("GET", path, headers={"Host": host})
        response = client.getresponse()
        return response.status, response.read().decode("utf-8")
    finally:
        client.close()


def test_the_server_answers_only_a_local_host(conn, read_only):
    seed(conn)
    httpd = pr.ThreadingHTTPServer(("127.0.0.1", 0), pr.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    try:
        assert get(port, "/", host="evil.example")[0] == 403
        assert get(port, "/nope")[0] == 404
        assert get(port, "/?start=soon")[0] == 400
        code, body = get(port, f"/?end={END}", host="localhost:8767")
        assert code == 200 and "Weekly P&amp;L" in body and "12.50" in body
    finally:
        httpd.shutdown()
        httpd.server_close()
