"""The weekly P&L page: the weeks add up to `get_pnl`, unknown stays unknown, every value is
escaped, and the page only reads."""

from __future__ import annotations

import http.client
import threading
import re
from dataclasses import replace
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
    assert pr.parse("/") == pr.Query(None, None, "simple")
    assert pr.parse("/?start=&end=") == pr.Query(None, None, "simple")
    assert pr.parse("/?start=2026-11-01&end=2026-11-09") == pr.Query(date(2026, 11, 1), date(2026, 11, 9), "simple")
    with pytest.raises(pr.BadRequest):
        pr.parse("/?start=yesterday")
    with pytest.raises(LookupError):
        pr.parse("/favicon.ico")


def test_a_date_far_outside_the_run_is_refused():
    for path in ("/?start=0001-01-01", "/?end=9999-12-31", "/?start=0001-01-08&end=2026-11-09"):
        with pytest.raises(pr.BadRequest):
            pr.parse(path)


def test_money_is_exact_to_the_cent_and_unknown_is_not_zero():
    assert pr.amount(Fraction(6185, 11)) == "$5.62"          # 562.27 cents, rounded only when shown
    assert pr.amount(-2500) == "-$25.00"
    assert pr.amount(Fraction(-123456789, 1000)) == "-$1,234.57"
    assert pr.amount(Fraction(-1, 3)) == "$0.00"             # a third of a cent short is not -$0.00
    assert pr.amount(123456700) == "$1,234,567.00"
    assert pr.dollars("-1234.56") == "-$1,234.56" and pr.dollars("0.00") == "$0.00"
    shown = pr.amount(Fraction(0), known=False, note="0 of 5 units costed")
    assert "unknown" in shown and "0.00" not in shown and "0 of 5 units costed" in shown


def test_a_week_with_an_uncosted_sale_shows_unknown_cost_and_profit():
    row = pr.week_row(week(units_costed=3, cost_of_sales_cents=Fraction(300)), date(2026, 11, 1), date(2026, 11, 30),
                      (Fraction(0), False))
    assert row.count('class="unknown"') == 3                 # cost of sales, profit and the running total
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


def test_the_view_is_simple_by_default_and_anything_else_is_refused():
    assert pr.parse("/?view=detailed").view == "detailed"
    assert pr.parse("/?view=").view == "simple"
    for bad in ("/?view=wide", "/?view=<b>", "/?view=Simple"):
        with pytest.raises(pr.BadRequest):
            pr.parse(bad)
    assert pr.parse("/?preset=bogus&x=<b>") == pr.Query(None, None, "simple")   # unknown names are ignored


def headers(body: str) -> list[str]:
    return re.findall(r'<th scope="col">(.*?)</th>', body)


def test_each_view_shows_exactly_its_columns():
    report = pr.Report(None, None, PRINTED, totals(), [week()])
    simple = ["Week (Sat to Fri, Eastern)", "Units sold", "Revenue ex tax", "Cost of sales", "Visit fees",
              "Token spend", "Profit", "Running total"]
    assert headers(pr.render(report)[1]) == simple
    detailed = ["Week (Sat to Fri, Eastern)", "Units sold", "Takings (incl. tax)", "Sales tax set aside",
                "Revenue ex tax", "Cost of sales", "Shrinkage", "Visit fees", "Token spend", "Adjustments",
                "Profit", "Running total"]
    for view, columns in (("simple", simple), ("detailed", detailed)):
        body = pr.render(replace(report, view=view))[1]
        assert headers(body) == columns
        rows = re.findall(r"<tr[^>]*>.*?</tr>", body.split("<tbody>")[1].split("</tfoot>")[0])
        assert len(rows) == 2                                # the week and the total
        for row in rows:
            assert row.count("<td") + row.count("<th") == len(columns)


def test_the_running_total_adds_up_the_weeks():
    weeks = [week(week=date(2026, 10, 31), takings_cents=0, tax_cents=0, units_sold=0, units_costed=0,
                  cost_of_sales_cents=Fraction(0), adjustments_cents=Fraction(-350)),
             week(), week(week=date(2026, 11, 14), units_costed=4)]
    running = pr.running_totals(weeks)
    assert running[0] == (Fraction(-350), True)
    assert running[1] == (weeks[0].net_cents + weeks[1].net_cents, True)
    assert running[2][1] is False                            # unknown from the first unknown week on


def test_presets_are_sat_to_fri_weeks_in_eastern_time():
    utc = timezone.utc
    # Fri 13 Nov 23:30 Eastern is already Saturday in UTC; it is still the week of Sat 7 Nov.
    late_friday = {label: (start, end) for label, start, end in pr.presets(datetime(2026, 11, 14, 4, 30, tzinfo=utc))}
    assert late_friday["This week"] == (date(2026, 11, 7), date(2026, 11, 13))
    assert late_friday["Last 4 weeks"] == (date(2026, 10, 17), date(2026, 11, 13))
    assert late_friday["Whole run"] == (None, None)
    # Ten past midnight on Saturday, Eastern: a new week.
    saturday = {label: (start, end) for label, start, end in pr.presets(datetime(2026, 11, 14, 5, 10, tzinfo=utc))}
    assert saturday["This week"] == (date(2026, 11, 14), date(2026, 11, 20))
    assert saturday["Last 4 weeks"] == (date(2026, 10, 24), date(2026, 11, 20))
    # Across the clock change (1 Nov): Sun 1 Nov 00:30 Eastern is in the week of Sat 31 Oct.
    assert pr.presets(datetime(2026, 11, 1, 4, 30, tzinfo=utc))[0][1:] == (date(2026, 10, 31), date(2026, 11, 6))


def test_the_active_preset_is_marked_and_links_keep_the_view():
    this_week = pr.Report(date(2026, 11, 7), date(2026, 11, 13), PRINTED, totals(), [week()], view="detailed")
    body = pr.render(this_week)[1]
    assert re.search(r'<a href="/\?start=2026-11-07&amp;end=2026-11-13&amp;view=detailed" aria-current="page"[^>]*>'
                     r'This week</a>', body)
    assert body.count('aria-current="page"') == 2            # the preset and the view
    assert '<input type="hidden" name="view" value="detailed">' in body
    whole = pr.render(pr.Report(None, None, PRINTED, totals(), [week()]))[1]
    assert re.search(r'<a href="/" aria-current="page"[^>]*>Whole run</a>', whole)
    assert '<a href="/?view=detailed"' in whole and 'name="view"' not in whole


def test_a_losing_week_is_marked_with_text_not_only_colour():
    loss = week(token_spend_cents=Fraction(5000))
    row = pr.week_row(loss, date(2026, 11, 1), date(2026, 11, 30), (loss.net_cents, True))
    assert '<tr class="loss">' in row and ">Loss<" in row
    gain = pr.week_row(week(), date(2026, 11, 1), date(2026, 11, 30), (week().net_cents, True))
    assert "loss" not in gain.lower()
    unknown = week(units_costed=3, token_spend_cents=Fraction(5000))
    assert "Loss" not in pr.week_row(unknown, date(2026, 11, 1), date(2026, 11, 30), (unknown.net_cents, False))


def test_a_week_short_by_less_than_half_a_cent_is_not_a_loss():
    tiny = week(takings_cents=100, tax_cents=0, units_sold=1, units_costed=1, cost_of_sales_cents=Fraction(301, 3))
    assert tiny.net_cents < 0 and pr.amount(tiny.net_cents) == "$0.00"
    row = pr.week_row(tiny, date(2026, 11, 1), date(2026, 11, 30), (tiny.net_cents, True))
    assert "loss" not in row.lower()
    svg = pr.chart([tiny], date(2026, 11, 1), date(2026, 11, 30))
    assert 'class="bar neg"' not in svg and "(loss)" not in svg


def test_a_week_label_wraps_between_its_days_not_inside_a_date():
    row = pr.week_row(week(), date(2026, 11, 9), date(2026, 11, 11), (week().net_cents, True))
    assert '<span class="day">Mon 2026-11-09</span> to <span class="day">Wed 2026-11-11</span>' in row
    assert ".day { white-space: nowrap; }" in pr.STYLE


def tiles(body: str) -> str:
    return body.split('<section class="tiles"')[1].split("</section>")[0]


def test_the_tiles_show_profit_and_units_and_unknown_stays_unknown():
    shown = tiles(pr.render(pr.Report(None, None, PRINTED, totals(net="-3.50"), [week()]))[1])
    assert "Profit" in shown and "-$3.50" in shown and ">Loss<" in shown and "Units sold" in shown
    unknown = tiles(pr.render(pr.Report(None, None, PRINTED, totals(cost_of_sales_complete=False), [week()]))[1])
    assert unknown.count('class="unknown"') == 1 and "5.95" not in unknown


def test_the_chart_has_one_bar_per_week_and_escaped_labels(monkeypatch):
    weeks = [week(week=date(2026, 10, 31), adjustments_cents=Fraction(-5000)), week(),
             week(week=date(2026, 11, 14), units_costed=4)]
    svg = pr.chart(weeks, date(2026, 10, 31), date(2026, 11, 20))
    assert svg.startswith("<svg") and 'role="img"' in svg and "<title" in svg and "aria-labelledby" in svg
    assert svg.count('class="bar ') == 3
    assert svg.count('class="bar neg"') == 1 and svg.count('class="bar pos"') == 1
    assert svg.count('class="bar unknown"') == 1
    assert 'class="zero"' in svg
    zero = float(re.search(r'class="zero"[^>]*y1="([\d.]+)"', svg).group(1))
    bars = [(kind, float(y), float(h)) for kind, y, h in
            re.findall(r'class="bar (neg|pos)" x="[\d.]+" y="([\d.]+)" width="[\d.]+" height="([\d.]+)"', svg)]
    (_, loss_y, loss_h), (_, gain_y, gain_h) = bars
    assert loss_y == pytest.approx(zero, abs=0.1)           # a loss hangs below the zero line
    assert gain_y + gain_h == pytest.approx(zero, abs=0.1)  # a profit stands on it
    assert gain_h / loss_h == pytest.approx(float(abs(weeks[1].net_cents / weeks[0].net_cents)), rel=0.01)
    scale = re.findall(r'<text class="scale"[^>]*>(.*?)</text>', svg)
    assert scale[0] == "$5.95" and scale[-1] == "-$44.05"  # the scale gives the top and bottom in dollars
    monkeypatch.setattr(pr, "week_label", lambda w, first, last: "<b>&")
    escaped = pr.chart(weeks, date(2026, 10, 31), date(2026, 11, 20))
    assert "<b>" not in escaped and "&lt;b&gt;&amp;" in escaped
    assert pr.chart([], date(2026, 10, 31), date(2026, 11, 20)) == ""


def test_the_page_says_how_to_print_without_a_script():
    _, body = pr.render(pr.Report(None, None, PRINTED, totals(), [week()]))
    assert "<script" not in body and "Press Ctrl+P to print" in body
    assert "prefers-color-scheme: dark" in body
    printed = body.split("@media print")[1].split("}")[0]
    for token in ("--error", "--link", "--band", "--ink", "--bg", "--loss"):
        assert f"{token}:" in printed                    # a dark screen doesn't print pale text on white


def test_the_same_note_with_a_different_timestamp_is_shown_once():
    notes = ["no poll at or before 2026-10-24T13:00:00+00:00, so takings start at the first poll after it",
             "no poll at or before 2026-10-24T09:00:00-04:00, so takings start at the first poll after it",
             "no poll at or before 2026-10-25T09:00:00-04:00, so takings start at the first poll after it",
             "no sales in the window"]
    body = pr.render(pr.Report(None, None, PRINTED, totals(notes=notes), [week()]))[1]
    items = re.findall(r"<li>(.*?)</li>", body.split('<ul class="notes">')[1])
    assert items == [
        "The machine&#x27;s sales counters were first read after Sat 2026-10-24 09:00 EDT, so takings count from "
        "that first reading; any sale before it is not counted.",
        "The machine&#x27;s sales counters were first read after Sun 2026-10-25 09:00 EDT, so takings count from "
        "that first reading; any sale before it is not counted.",
        "No sales in the window."]


def test_notes_are_plain_sentences():
    assert pr.plain_note("no polls in the window, so takings are unknown and counted as 0") == (
        "The machine's sales counters were not read in this range, so takings are unknown and counted as $0.00.")
    assert pr.plain_note("2 entries are dated before x") == "2 entries are dated before x."
    assert pr.plain_note("no poll at or before soon, so takings start at the first poll after it") == (
        "No poll at or before soon, so takings start at the first poll after it.")   # not a time: kept as given


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

    code, body = pr.render(replace(report, view="detailed"))
    assert code == 200
    for figure in (t.revenue, t.tax_set_aside, t.revenue_ex_tax, t.cost_of_sales, t.net):
        assert f'>{pr.dollars(figure)}</td>' in body
    assert report.balance is not None and pr.dollars(report.balance.tax_owed) in body
    assert 'class="unknown"' not in body
    assert "rounded to the cent on its own" in body       # so a column can miss its total by a cent


def test_the_running_total_ends_at_the_ledger_total(conn):
    seed(conn)
    report = pr.load(conn, None, END)
    p = ledger.pnl(conn, report.totals.start, report.totals.end)
    running = pr.running_totals(report.weeks)
    assert [r for r, _ in running] == [sum(w.net_cents for w in report.weeks[:i + 1]) for i in range(len(report.weeks))]
    assert running[-1] == (p.net_cents, True) and ledger.money(running[-1][0]) == report.totals.net
    _, body = pr.render(report)
    last_row = body.split("</tbody>")[0].rsplit("<tr", 1)[1]
    assert last_row.endswith(f'<td class="num">{pr.dollars(report.totals.net)}</td></tr>')
    assert '<tr class="loss">' in body                       # the first week only lost the deposit


def test_a_range_longer_than_the_page_shows_is_refused(conn):
    seed(conn)
    code, body = pr.render(pr.load(conn, date(2020, 1, 1), END))
    assert code == 400 and f"at most {pr.MAX_WEEKS} weeks" in body


def test_the_week_limit_counts_rows_not_days(conn):
    """A range that starts midweek has a part week at each end, so the limit counts the Sat-to-Fri rows."""
    seed(conn)
    first = date(2026, 11, 11)                                            # a Wednesday, in the week of Sat 7 Nov
    last = date(2026, 11, 7) + timedelta(weeks=pr.MAX_WEEKS - 1, days=6)  # the Friday that ends row MAX_WEEKS
    report = pr.load(conn, first, last)
    assert report.totals.success and len(report.weeks) == pr.MAX_WEEKS
    code, body = pr.render(pr.load(conn, first, last + timedelta(days=1)))   # one day into the next week
    assert code == 400 and f"at most {pr.MAX_WEEKS} weeks" in body


def test_an_unknown_cost_renders_as_unknown(conn):
    open_ledger(conn)
    sell(conn, units=5, price=250)                           # sales, but no receipts at all
    report = pr.load(conn, None, END)
    assert report.totals.net == "11.57"                      # the ledger gives a figure, flagged
    _, body = pr.render(replace(report, view="detailed"))
    # cost and profit in the week and the total, the running total, and the profit tile
    assert body.count('class="unknown"') == 6
    assert "0 of 5 units costed" in body
    assert body.split("<tfoot>")[1].split("</tfoot>")[0].endswith('<span class="unknown">unknown</span></td>'
                                                              '<td class="num"></td></tr>')


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
        assert code == 200 and "Weekly P&amp;L" in body and "$11.57" in body and "$12.50" not in body
        assert get(port, "/?view=wide")[0] == 400
        assert "Takings (incl. tax)" in get(port, f"/?end={END}&view=detailed")[1]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_the_balance_is_as_of_the_end_of_the_range(conn):
    """A past range shows the balance at the end of its last day (Eastern); a range ending today or
    later shows the balance now."""
    open_ledger(conn)                                                                  # $600.00, 1 Nov
    ledger.record(conn, "adjustment", -350, "bottle deposit", "marshall", OPENED + timedelta(days=1))
    # 4 Nov 04:30 UTC is still Tue 3 Nov in Eastern time, so it is inside a range that ends 3 Nov.
    ledger.record(conn, "capital", 1000, "late on the 3rd", "marshall", datetime(2026, 11, 4, 4, 30, tzinfo=timezone.utc))
    ledger.record(conn, "capital", 10000, "top-up", "marshall", datetime(2026, 11, 4, 5, 30, tzinfo=timezone.utc))

    past = pr.load(conn, date(2026, 11, 1), date(2026, 11, 3))
    assert past.balance.balance == "606.50"
    assert past.balance.as_of == datetime(2026, 11, 4, tzinfo=pr.EASTERN) - timedelta(microseconds=1)
    _, body = pr.render(past)
    assert "Balance at the end of Tue 2026-11-03" in body
    assert '<td class="num">$606.50</td>' in body and "706.50" not in body

    for end in (None, datetime.now(pr.EASTERN).date(), datetime.now(pr.EASTERN).date() + timedelta(days=30)):
        current = pr.load(conn, date(2026, 11, 1), end)
        assert current.balance.balance == "706.50", end
        _, body = pr.render(current)
        assert "Balance now" in body and '<td class="num">$706.50</td>' in body


def test_a_range_with_no_end_shows_the_balance_now_whatever_the_clocks_say(conn, monkeypatch):
    """With no end date the balance is now, even when the app's clock is behind the database's."""
    import vending.tools.pnl as tool

    class Behind(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) - timedelta(days=1)

    open_ledger(conn)
    monkeypatch.setattr(tool, "datetime", Behind)
    report = pr.load(conn, None, None)
    assert report.balance_at is None
    assert "Balance now" in pr.render(report)[1]


def test_a_range_that_ends_before_the_opening_shows_no_balance(conn):
    open_ledger(conn)
    report = pr.load(conn, date(2026, 10, 1), date(2026, 10, 20))
    _, body = pr.render(report)
    assert "Balance" not in body and "before the ledger opened" in body
