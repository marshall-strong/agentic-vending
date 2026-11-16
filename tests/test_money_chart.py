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
    assert payload["opening"] == {"at": opened.at.astimezone(timezone.utc).isoformat(),
                                  "amount": ledger.money(opened.amount_cents)}
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
    payload = {"opening": {"at": "2026-11-30T15:00:00+00:00", "amount": "600.00"},
               "daily": [{"date": "2026-11-30", "notes": ["</script><script>alert(1)</script>"]}]}
    html = money_chart.page(payload)
    assert "/*DATA*/null" not in html
    assert "</script><script>alert(1)" not in html
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
