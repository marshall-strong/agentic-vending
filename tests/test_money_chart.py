"""Tests for the money chart page: the data it carries comes from the money series and the ledger's
opening, development spend stays out, unknown profit stays unknown, and the page inlines the data
safely. On a throwaway schema with synthetic polls, receipts and entries."""

import json
import re
from contextlib import nullcontext
from datetime import date, datetime, timezone

import pytest

psycopg = pytest.importorskip("psycopg")

from tests.test_ledger import EASTERN, deliver, open_ledger, sell  # noqa: E402
from vending import ledger, money_chart, money_series  # noqa: E402

AFTER = datetime(2027, 1, 31, tzinfo=timezone.utc)


def inlined(html: str) -> dict:
    """The JSON the page carries in place of /*DATA*/null."""
    found = re.search(r"const REAL = (.*?);\nconst MOCK", html, re.S)
    assert found, "the page's data line moved"
    return json.loads(found.group(1).replace("<\\/", "</"))


def test_the_data_is_the_money_series_from_the_opening(conn):
    ledger.record_tokens(conn, "0.80", "model", 10, 5, "test dry run", at=datetime(2026, 10, 30, 12, tzinfo=EASTERN))
    open_ledger(conn)                                     # Saturday 31 Oct, 20:00 Eastern
    deliver(conn, "r1")                                   # Sat 7 Nov
    sell(conn)                                            # 5 sold, seen on Sun 8 Nov
    ledger.record_tokens(conn, "0.40", "model", 10, 5, "live run", at=datetime(2026, 11, 8, 12, tzinfo=EASTERN))

    payload = money_chart.data(conn, end=date(2026, 11, 8), now=AFTER)
    opened = ledger.opening(conn)
    assert payload["opening"] == {"at": opened.at.astimezone(timezone.utc).isoformat(), "day": "2026-10-31",
                                  "amount": ledger.money(opened.amount_cents),
                                  "exact_cents": str(opened.amount_cents)}
    days = payload["daily"]
    assert days[0]["date"] == "2026-10-31" and days[-1]["date"] == "2026-11-08"      # from the opening's day
    expected = money_series.daily(conn, date(2026, 10, 31), date(2026, 11, 8), AFTER)
    assert days == [money_series.as_json(d) for d in expected]
    # Development spend before the opening is in no figure; the live call is.
    assert sum(float(d["token_spend"]) for d in days) == pytest.approx(0.40)
    assert "0.80" not in json.dumps(payload)


def test_no_opening_means_no_data(conn):
    assert money_chart.data(conn, now=AFTER) is None


def test_unknown_profit_reaches_the_page_as_null(conn):
    open_ledger(conn)
    sell(conn)                                            # 5 sold with no receipts: not costed
    day = money_chart.data(conn, date(2026, 11, 8), date(2026, 11, 8), now=AFTER)["daily"][0]
    assert (day["net"], day["gross_profit"], day["costs_complete"]) == (None, None, False)


def test_the_page_inlines_the_data_and_cant_be_closed_early():
    nasty = ["</script><script>alert(1)</script>", "<!--<script>", "a & b > c", "line\N{LINE SEPARATOR}sep\N{PARAGRAPH SEPARATOR}end"]
    payload = {"opening": {"at": "2026-11-30T15:00:00+00:00", "amount": "600.00"},
               "daily": [{"date": "2026-11-30", "notes": nasty}]}
    html = money_chart.page(payload)
    assert "/*DATA*/null" not in html
    carried = re.search(r"const REAL = (.*?);\nconst MOCK", html, re.S).group(1)
    for raw in ("<", ">", "&", "\N{LINE SEPARATOR}", "\N{PARAGRAPH SEPARATOR}"):
        assert raw not in carried, f"{raw!r} reached the script as itself"
    lt, gt = chr(92) + "u003c", chr(92) + "u003e"     # the text of the escapes, backslash first
    assert f"{lt}!--{lt}script{gt}" in carried
    assert inlined(html) == payload


def test_without_data_the_page_keeps_its_mock_data():
    html = money_chart.page(None)
    assert "const REAL = /*DATA*/null;" in html and "MOCK DATA" in html


def test_cli_writes_the_page(conn, monkeypatch, capsys, tmp_path):
    open_ledger(conn)
    sell(conn)
    monkeypatch.setattr(money_chart.db, "connect", lambda: nullcontext(conn))
    out = tmp_path / "chart.html"
    assert money_chart.main(["--html", str(out), "--end", "2026-11-08"]) == 0
    assert "days from the ledger" in capsys.readouterr().out
    assert inlined(out.read_text(encoding="utf-8"))["daily"][-1]["date"] == "2026-11-08"

    assert money_chart.main(["--json", "--start", "2026-11-08", "--end", "2026-11-08"]) == 0
    assert [d["date"] for d in json.loads(capsys.readouterr().out)["daily"]] == ["2026-11-08"]


def test_cli_without_an_opening_offers_the_demo(conn, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(money_chart.db, "connect", lambda: nullcontext(conn))
    assert money_chart.main(["--html", str(tmp_path / "x.html")]) == 1
    err = capsys.readouterr().err
    assert "python -m vending.ledger open" in err and "--demo" in err
    assert not (tmp_path / "x.html").exists()


def test_cli_demo_needs_no_database(monkeypatch, capsys, tmp_path):
    def no_database():
        raise AssertionError("--demo must not connect")
    monkeypatch.setattr(money_chart.db, "connect", no_database)
    out = tmp_path / "demo.html"
    assert money_chart.main(["--demo", "--html", str(out)]) == 0
    assert "MOCK DATA" in out.read_text(encoding="utf-8")
    with pytest.raises(SystemExit):
        money_chart.main(["--demo", "--json"])
    with pytest.raises(SystemExit):
        money_chart.main([])


_FIELDS = ("revenue_ex_tax", "cost_of_sales", "visit_fees", "token_spend", "sales_tax",
           "shrinkage", "adjustments", "takings", "cash_balance", "tax_owed", "inventory",
           "net_worth", "gross_profit", "net")

# A small DOM, enough for the page's script to build every view. `width` is the chart's CSS width.
_STUB = r'''
class Element {
  constructor() { this.children=[]; this.style={}; this.dataset={}; this.attrs={}; this.parentElement=this; }
  setAttribute(k,v) { this.attrs[k]=v; if(k === "data-k") this.dataset.k=v; }
  append(...xs) { this.children.push(...xs); this.lastChild=this.children[this.children.length-1]; }
  appendChild(x) { this.append(x); return x; }
  replaceChildren(...xs) { this.children=[]; this.append(...xs); }
  addEventListener() {}
  getBoundingClientRect() { return {left:0, top:0, width:900}; }
}
const elements = {};
const document = {getElementById: id => elements[id] ||= new Element(),
  createElement: () => new Element(), createElementNS: () => new Element(),
  createDocumentFragment: () => new Element(), querySelectorAll: () => []};
const location = {hash: "", toString: () => "http://localhost/"};
const history = {replaceState() {}};
document.getElementById("chart").clientWidth = WIDTH;
const assert = (ok, message) => { if (!ok) throw Error(message); };
const walk = (e, out = []) => { out.push(e); for (const c of e.children || []) walk(c, out); return out; };
const svgNow = () => elements.chart.children[0];
const texts = () => walk(svgNow()).filter(e => e.textContent != null).map(e => e.textContent);
'''


def _day_rows(n: int = 10, first: date = date(2026, 11, 14)) -> list[dict]:
    from datetime import timedelta
    rows = []
    for i in range(n):
        exact = dict.fromkeys(_FIELDS, "0")
        exact.update(revenue_ex_tax="100", takings="100", cash_balance="60000", net_worth="60000",
                     gross_profit="100", net="100", token_spend="49/100")
        rows.append({"date": (first + timedelta(days=i)).isoformat(),
                     "counted_from": "2026-11-14T00:00:00Z", "whole": True,
                     "units_sold": 0, "cost_of_sales_complete": True, "shrinkage_complete": True, "costs_complete": True,
                     "exact_cents": exact, "events": [], "notes": []})
    return rows


def _page_runner(tmp_path):
    """run(payload, checks, width=900): the page's script with `payload` inlined, then `checks`, under node."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")

    def run(data, checks, width=900):
        script = money_chart.page(data).split("<script>", 1)[1].split("</script>", 1)[0]
        path = tmp_path / "page.js"
        path.write_text(_STUB.replace("WIDTH", str(width)) + script + "\n" + checks, encoding="utf-8")
        result = subprocess.run([node, str(path)], capture_output=True, text=True, encoding="utf-8")
        assert result.returncode == 0, result.stderr
    return run


OPENING = {"at": "2026-11-14T00:00:00Z", "day": "2026-11-14", "amount": "600.00", "exact_cents": "60000"}


def test_page_javascript_regressions(tmp_path):
    """Run the exported script with a small DOM, including its other views."""
    run = _page_runner(tmp_path)
    rows = _day_rows()
    rows[0]["notes"] = ['<img src=x onerror="alert(1)">']
    rows[1]["cost_of_sales_complete"] = rows[1]["costs_complete"] = False
    rows[1]["exact_cents"].update(gross_profit=None, net=None, net_worth=None)
    rows[2]["exact_cents"].update(shrinkage="20", adjustments="30", net="110")
    rows[7]["exact_cents"].update(shrinkage="20", adjustments="30", net="110")
    # No exact_cents on the opening: the page falls back to the amount, as for a page written before it had them.
    payload = {"opening": {"at": "2026-11-14T00:00:00Z", "day": "2026-11-14", "amount": "600.00"}, "daily": rows}
    checks = r'''
assert(PB === -1, "payback before investment");
assert(DF.cogs[1] === null && DF.costs[1] === null, "unknown costs");
assert(sumFlow("net", 0, 9) === null, "partial profit");
assert(Math.abs(sumFlow("net", 2, 9) - 8.2) < 1e-9, "clean range");
S.m = "r7"; assert(val("net", 8) !== null, "rolling recovery");
assert(weeks[0].profit === null && Math.abs(weeks[1].profit - 3.1) < 1e-9, "weekly profit and unknown");
assert($(sumFlow("api", 0, 1)) === "$0.01", "fractional cents");
assert(elements.table.innerHTML.includes("&lt;img"), "escaped event table");
assert(dayTip(days[0]).includes("&lt;img"), "escaped tooltip");
assert(row('<img src=x>', 'x').includes("&lt;img"), "escaped run label");
render("worth");
const worthLine = elements.chart.children[0].children.find(e => e.attrs.stroke === "var(--ink)" && e.attrs.d);
assert(worthLine && (worthLine.attrs.d.match(/M/g) || []).length === 2, "net worth gap");
render("weekly"); render("brain");
'''
    run(payload, checks)
    clean = [{**r, "exact_cents": dict(r["exact_cents"])} for r in rows[:1]]
    clean[0]["exact_cents"].update(revenue_ex_tax="594", gross_profit="594", net="594")
    run({**payload, "daily": clean}, 'if ($(sumFlow("net", 0, 0)) !== "$5.94") throw Error("exact net");\n')
    shrink = [{**r, "exact_cents": dict(r["exact_cents"])} for r in rows[:2]]
    shrink[1].update(cost_of_sales_complete=True, shrinkage_complete=False, costs_complete=False)
    shrink[1]["exact_cents"].update(gross_profit="100", net=None, cash_balance="59000")
    run({**payload, "daily": shrink}, 'if (DF.cogs[1] === null || DF.costs[1] !== null || weeks[0].shrink !== null) throw Error("unknown shrinkage");\n')
    recovered = [*shrink, {**clean[0], "date": "2026-11-16"}]
    run({**payload, "daily": recovered}, 'if (PB !== 2 || PB_EARLY) throw Error("payback after investment");\n')
    run({**payload, "daily": rows[:1]}, 'render("worth"); render("weekly"); render("brain");\n')


def test_an_export_from_mid_history_invents_no_change_or_payback(tmp_path):
    """Starting after the opening, the balance before the first day is unknown, and cash already back above
    the opening capital is "paid back by" the first day, not "not yet" and not a $100 first-day change."""
    run = _page_runner(tmp_path)
    rows = _day_rows(4, date(2026, 12, 10))
    for r, cash in zip(rows, ("70000", "70500", "71000", "71000")):
        r["exact_cents"].update(cash_balance=cash, net_worth=cash)
    run({"opening": OPENING, "daily": rows}, r'''
assert(!fromOpening, "export starts after the opening");
assert(PB === 0 && PB_EARLY, "paid back by the first day");
assert(paybackTile(N_DAYS_LAST)[1] === "By Dec 10", "payback tile: " + paybackTile(N_DAYS_LAST));
S.m = "day";
assert(val("cash", 0) === null && val("worth", 0) === null, "no first-day change: " + val("cash", 0));
assert(val("cash", 1) === 5, "a later change is exact");
S.m = "r7"; assert(val("cash", 3) === null, "rolling change reaching before the export");
S.m = "cum"; S.rb = true; setRange(0, 3, "custom"); assert(val("cash", 2) === null, "restart at the export's first day");
S.rb = false; S.m = "cum"; render("overlay");
assert(elements.tiles.children[0].children[2].textContent.includes("isn't in this export"), "net worth tile change");
assert(texts().includes("Paid back by Dec 10"), "chart marks payback by the first day");
''')
    # The same export starting below the opening capital pays back the usual way, on the day it gets back.
    rows[0]["exact_cents"].update(cash_balance="59000", net_worth="59000")
    run({"opening": OPENING, "daily": rows}, 'assert(PB === 1 && !PB_EARLY, "payback " + PB);\n')
    # From the opening, the first day's change is against the opening capital.
    rows[0]["date"], rows[0]["exact_cents"]["cash_balance"] = "2026-11-14", "60250"
    run({"opening": OPENING, "daily": rows[:1]}, 'S.m = "day"; assert(val("cash", 0) === 2.5, "first-day change " + val("cash", 0));\n')


def test_weekly_bars_net_to_the_profit_dot_and_mark_unknown_weeks(tmp_path):
    run = _page_runner(tmp_path)
    rows = _day_rows(9)        # Sat 14 Nov to Sun 22 Nov: two weeks
    for r in rows:
        # net = 100 revenue - 30 drinks - 49/100 AI - 50 shrinkage + 1 adjustment; the 8 of tax is in none of it
        r["exact_cents"].update(takings="108", sales_tax="8", cost_of_sales="30", gross_profit="70",
                                shrinkage="50", adjustments="1", net="2051/100")
    rows[8]["cost_of_sales_complete"] = rows[8]["costs_complete"] = False
    rows[8]["exact_cents"].update(cost_of_sales=None, gross_profit=None, net=None)
    run({"opening": OPENING, "daily": rows}, r'''
render("weekly");
const w = weeks[0];
assert(Math.abs(w.up - w.down - w.profit) < 1e-9, `bars ${w.up} - ${w.down} vs dot ${w.profit}`);
assert(w.tax > 0 && !elements.legend.innerHTML.includes("Sales tax"), "tax is not in the stack");
assert(weeks[1].profit === null && weeks[1].unknown.includes("cogs"), "unknown week");
const all = walk(svgNow());
assert(all.some(e => e.attrs["stroke-dasharray"] === "4 3" && e.attrs.fill === "none"), "dashed unknown box");
assert(texts().includes("incomplete") && texts().includes("?"), "week marked incomplete");
assert(all.filter(e => e.attrs.r === 5).length === 1, "one profit dot, none for the unknown week");
assert(elements.table.innerHTML.includes("(incomplete)"), "table marks the week");
''')


def test_value_labels_keep_cents(tmp_path):
    run = _page_runner(tmp_path)
    rows = _day_rows(3)
    rows[2]["exact_cents"].update(revenue_ex_tax="74504", gross_profit="74504", net="74504",
                                  cash_balance="134704", net_worth="134704")
    run({"opening": OPENING, "daily": rows}, r'''
S.s = new Set(["rev", "worth"]); render("overlay");
assert(texts().some(t => t.includes("Revenue $747.04")), "D end label: " + texts().join(" | "));
assert(texts().some(t => t.includes("Net worth $1,347.04")), "D balance label");
assert(elements.tiles.children[4].children[2].textContent.includes("drinks $0.00"), "tile breakdown has cents");
render("worth"); assert(texts().includes("Net worth $1,347.04"), "worth label");
render("weekly"); assert(texts().includes("$747.04"), "weekly profit label: " + texts().join(" | "));
''')


def test_charts_fit_a_phone(tmp_path):
    """At 330 px the SVGs are drawn in CSS pixels (so text stays 11 px) with fewer, non-overlapping ticks."""
    run = _page_runner(tmp_path)
    rows = _day_rows(63)
    run({"opening": OPENING, "daily": rows}, r'''
for (const view of ["overlay", "worth", "brain", "weekly"]) {
  render(view);
  const svg = svgNow();
  assert(svg.attrs.viewBox.startsWith("0 0 330 "), view + " viewBox " + svg.attrs.viewBox);
  const dates = walk(svg).filter(e => e.attrs["text-anchor"] === "middle" && /^[A-Z][a-z]{2} \d+$/.test(e.textContent || ""));
  const xs = dates.map(e => +e.attrs.x).sort((p, q) => p - q);
  assert(xs.every((x, i) => !i || x - xs[i - 1] >= 46), view + " ticks overlap: " + xs);
}
render("overlay");
assert(texts().every(t => !/^Revenue \$/.test(t)), "phone end labels are values only");
''', width=330)


def test_every_chart_has_an_accessible_name(tmp_path):
    run = _page_runner(tmp_path)
    run({"opening": OPENING, "daily": _day_rows()}, r'''
for (const view of ["overlay", "worth", "weekly", "brain"]) {
  render(view);
  const svgs = [svgNow(), ...(view === "overlay" ? [elements.strip.children[0]] : [])];
  for (const svg of svgs) assert((svg.attrs["aria-label"] || "").length > 20, view + " chart has no name");
}
''')


def test_a_one_day_range_stays_one_day(tmp_path):
    run = _page_runner(tmp_path)
    run({"opening": OPENING, "daily": _day_rows()}, r'''
applyRangeKey("2026-11-16_2026-11-16"); renderD();
assert(S.a === 2 && S.b === 2 && S.rk === "custom", `range ${S.a}-${S.b}`);
assert(rangeLabel().startsWith("Nov 16, 2026") && rangeLabel().endsWith("· 1 day"), rangeLabel());
assert(rangeParam() === "2026-11-16_2026-11-16", "the link keeps one day");
const dots = walk(svgNow()).filter(e => e.attrs.r === 3.5);
assert(dots.length === S.s.size, "each series shows its one day as a dot: " + dots.length);
setRange(5, 5, "custom"); assert(S.a === 5 && S.b === 5, "drag-zoom setRange keeps one day");
''')
