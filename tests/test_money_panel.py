"""The dashboard's money panel (#48): balance and spend from the ledger, committed spend from
pending requests, and what it tells Needs attention."""

from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

from tests.test_dashboard import fulfilled, page_conn  # noqa: F401  (a fixture)
from vending import dashboard, ledger, money_panel

OPENED = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 12, 9, 17, 0, tzinfo=timezone.utc)


def body(*lines, off_cycle=False):
    return {"off_cycle": off_cycle, "lines": list(lines)}


def line(packs, max_price, alt=None):
    out = {"slot": "15", "source": "purchase",
           "primary": {"sku": "RB", "store": "Amazon", "packs": packs, "units_per_pack": 24,
                       "max_price_per_pack": max_price, "sale_price": "3.50", "change_product": True}}
    if alt:
        out["alternate"] = {"sku": "RB", "store": "Walmart", "packs": alt[0], "units_per_pack": 24,
                            "max_price_per_pack": alt[1], "sale_price": "3.50", "change_product": True}
    return out


def pending(conn, request_id, b, fee=None):
    conn.execute("insert into purchase_requests (request_id, submitted_at, off_cycle, request_body, visit_fee_cents)"
                 " values (%s, %s, %s, %s, %s)", (request_id, NOW - timedelta(hours=1), b["off_cycle"], Jsonb(b), fee))


def test_before_the_opening_the_panel_says_how_to_record_it(page_conn):
    snap = dashboard.snapshot(page_conn, NOW)
    assert snap["money"]["ok"] and snap["money"]["data"]["opened"] is False
    page = dashboard.render(snap)
    assert "No opening entry in the ledger yet" in page and "python -m vending.ledger open" in page
    assert "No balance or profit shown yet" not in page
    assert not [a for a in snap["attention"] if "ledger" in a["text"]]


def test_balance_spend_and_profit_come_from_the_ledger(conn, page_conn):
    ledger.record(conn, "opening", 60000, "opening capital", "test", at=OPENED)
    fulfilled(conn, "pr-1", "15", "RB", units_loaded=11, paid="34.99", at=OPENED + timedelta(days=1))
    ledger.record_tokens(conn, "0.5100", "claude-opus-5-5", 1000, 200, "msg_1", at=OPENED + timedelta(days=2))
    data = dashboard.snapshot(page_conn, NOW)["money"]["data"]
    assert data["opened"] is True
    assert (data["opening"], data["receipts"], data["tokens"]) == ("600.00", "34.99", "0.51")
    assert data["balance"] == "564.50" and data["spendable"] == "564.50"     # no sales, so no tax set aside
    assert data["pnl"]["units_sold"] == 0 and data["pnl"]["net"] == "-0.51"
    page = dashboard.render(dashboard.snapshot(page_conn, NOW))
    assert "$564.50" in page and "Where the money has gone" in page and "#113" in page


def test_committed_is_the_worst_case_of_each_pending_line_plus_the_visit_fee(conn, page_conn):
    ledger.record(conn, "opening", 60000, "opening capital", "test", at=OPENED)
    pending(conn, "pr-2", body(line(2, "40.50", alt=(3, "30.00")), line(1, "25.00")))      # 90.00 + 25.00
    pending(conn, "pr-3", body(line(1, "10.00"), off_cycle=True), fee=2500)                 # 10.00 + 25.00
    data = dashboard.snapshot(page_conn, NOW)["money"]["data"]
    assert data["committed"] == {"requests": 2, "unsettled": 0, "max": "150.00", "unpriced_lines": 0}
    assert data["spendable_after_pending"] == "450.00"
    assert "could cost up to $150.00" in dashboard.render(dashboard.snapshot(page_conn, NOW))


def test_pending_requests_over_spendable_are_a_problem(conn, page_conn):
    ledger.record(conn, "opening", 10000, "opening capital", "test", at=OPENED)
    pending(conn, "pr-4", body(line(3, "40.50")))
    snap = dashboard.snapshot(page_conn, NOW)
    assert any(a["level"] == "bad" and "$121.50" in a["text"] for a in snap["attention"])
    assert dashboard.verdict(snap["attention"]) == "problem"


def test_dropping_under_the_proposed_floor_needs_checking(conn, page_conn):
    ledger.record(conn, "opening", 10000, "opening capital", "test", at=OPENED)
    pending(conn, "pr-5", body(line(1, "60.00")))
    items = dashboard.snapshot(page_conn, NOW)["attention"]
    assert any(a["level"] == "warn" and "$40.00 stays spendable" in a["text"] for a in items)


def test_pending_requests_without_an_opening_need_checking(conn, page_conn):
    pending(conn, "pr-6", body(line(1, "40.50")))
    items = dashboard.snapshot(page_conn, NOW)["attention"]
    assert any(a["level"] == "warn" and "no opening entry" in a["text"] for a in items)


def test_model_spend_over_the_proposed_nine_dollars_is_worth_knowing(conn, page_conn):
    ledger.record(conn, "opening", 60000, "opening capital", "test", at=OPENED)
    ledger.record_tokens(conn, "9.0100", "claude-opus-5-5", 1, 1, "msg_2", at=OPENED + timedelta(days=1))
    items = dashboard.snapshot(page_conn, NOW)["attention"]
    assert any(a["level"] == "info" and "$9.01" in a["text"] for a in items)


def test_dev_spend_before_the_opening_is_a_note_not_in_the_balance(conn, page_conn):
    ledger.record_tokens(conn, "2.0000", "claude-opus-5-5", 1, 1, "msg_dev", at=OPENED - timedelta(days=3))
    ledger.record(conn, "opening", 60000, "opening capital", "test", at=OPENED)
    data = dashboard.snapshot(page_conn, NOW)["money"]["data"]
    assert data["balance"] == "600.00" and data["tokens"] == "0.00"
    assert any("development spend" in n for n in data["notes"])


def test_a_line_it_cant_price_is_counted_as_unpriced_not_guessed(conn, page_conn):
    ledger.record(conn, "opening", 60000, "opening capital", "test", at=OPENED)
    pending(conn, "pr-7", body(line(-2, "10.00"), line(1, "-5.00"), line(1, "ten"),
                               {"source": "back_stock", "slot": "16", "load": {}}))
    got = dashboard.snapshot(page_conn, NOW)["money"]["data"]["committed"]
    assert got == {"requests": 1, "unsettled": 0, "max": "0.00", "unpriced_lines": 3}    # back stock costs nothing


def test_a_json_number_price_is_read_exactly(conn, page_conn):
    # The snapshot parses JSON numbers as Decimal, so 1.10 is exact cents, never a float.
    ledger.record(conn, "opening", 60000, "opening capital", "test", at=OPENED)
    pending(conn, "pr-8", body(line(3, 1.10)))
    got = dashboard.snapshot(page_conn, NOW)["money"]["data"]["committed"]
    assert got["max"] == "3.30" and got["unpriced_lines"] == 0


def test_a_submission_with_no_usable_answer_counts_as_committed(conn, page_conn):
    ledger.record(conn, "opening", 60000, "opening capital", "test", at=OPENED)
    for status, b, fee in (("unknown", body(line(2, "40.00")), None),
                           ("sending", body(line(1, "10.00"), off_cycle=True), 2500),
                           ("refused", body(line(9, "99.00")), None)):
        conn.execute("insert into purchase_submissions (plan_hash, body, visit_fee_cents, status)"
                     " values ('h', %s, %s, %s)", (Jsonb(b), fee, status))
    snap = dashboard.snapshot(page_conn, NOW)
    got = snap["money"]["data"]["committed"]
    assert got == {"requests": 2, "unsettled": 2, "max": "115.00", "unpriced_lines": 0}
    assert any(a["level"] == "warn" and "no usable answer" in a["text"] for a in snap["attention"])
    assert "2 of them sent with no usable answer" in dashboard.render(snap)


def _opened(**over):
    base = {"opened": True, "spendable": "100.00", "spendable_after_pending": "100.00", "tokens": "0.00",
            "committed": {"requests": 0, "unsettled": 0, "max": "0.00", "unpriced_lines": 0}}
    return {**base, **over}


def test_overspent_with_nothing_pending_says_so_plainly():
    items = money_panel.attention(_opened(spendable="-5.00", spendable_after_pending="-5.00"))
    assert items == [("bad", "Spendable is -$5.00: more has gone out than we have.")]


def test_the_bar_adds_up_to_the_money_in_when_tax_was_overpaid():
    # $600 opening, $108 takings ($8 tax due), $20 handed over: owed is -12, spendable counts it back.
    # Balance 688, spendable 700; the bar is the $708 in: $8 tax and $700 spendable.
    d = {"opening": "600.00", "capital": "0.00", "takings": "108.00", "adjustments": "0.00", "receipts": "0.00",
         "visit_fees": "0.00", "tokens": "0.00", "tax_remitted": "20.00", "tax_owed": "-12.00", "spendable": "700.00"}
    bar = money_panel._split_bar(d)
    assert "Tax handed over (net of $12.00 overpaid) $8.00" in bar and "Tax set aside" not in bar
    assert f'class="seg-spend" style="width:{100 * 700 / 708:.2f}%"' in bar
    # Everything handed over was overpaid: the net segment is empty, but the key still says so.
    d.update(takings="0.00", tax_owed="-20.00", spendable="600.00")
    assert "net of $20.00 overpaid" in money_panel._split_bar(d)


def test_the_opening_date_follows_the_viewers_zone(conn, page_conn):
    ledger.record(conn, "opening", 60000, "opening capital", "test", at=datetime(2026, 11, 28, 1, 0, tzinfo=timezone.utc))
    page = dashboard.render(dashboard.snapshot(page_conn, NOW))
    assert '<time datetime="2026-11-28T01:00:00+00:00" data-f="day"' in page
    assert "f === 'day'" in page


def test_the_money_panel_is_on_the_public_page_too(conn, page_conn):
    ledger.record(conn, "opening", 60000, "opening capital", "test", at=OPENED)
    snap = dashboard.snapshot(page_conn, NOW, public=True)
    assert snap["money"]["ok"] and "$600.00" in dashboard.render(snap)


def test_without_our_slot_assignment_the_panel_says_so_and_the_page_still_renders(conn, page_conn):
    ledger.record(conn, "opening", 60000, "opening capital", "test", at=OPENED)
    conn.execute("delete from slot_assignment")
    snap = dashboard.snapshot(page_conn, NOW)
    assert snap["money"]["ok"] and "python -m vending.slots record" in snap["money"]["data"]["problem"]
    assert any(a["level"] == "warn" and "can't give a balance" in a["text"] for a in snap["attention"])
    page = dashboard.render(snap)
    assert "No balance:" in page and 'id="vm"' in page


def test_once_the_ledger_is_open_the_spent_tile_reads_it(conn, page_conn):
    ledger.record(conn, "opening", 60000, "opening capital", "test", at=OPENED)
    fulfilled(conn, "pr-7", "15", "RB", units_loaded=11, paid="34.99", at=OPENED + timedelta(days=1))
    ledger.record_tokens(conn, "0.5100", "claude-opus-5-5", 1, 1, "msg_7", at=OPENED + timedelta(days=2))
    page = dashboard.render(dashboard.snapshot(page_conn, NOW))
    assert "Spent since the opening" in page and "$35.50" in page and "model $0.51" in page
