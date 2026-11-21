"""The Key dates panel (`vending.key_dates`): the dates, their statuses and where they show."""

from datetime import datetime, timezone
from html import escape

import pytest

from tests.conftest import TEST_CHECKED_AT, TEST_SLOTS
from vending import dashboard, key_dates, ledger
from vending.key_dates import EASTERN
from vending.start import START

LOCK = START   # SLOTS_LOCK_AT's default


def et(month, day, hour=12, minute=0, second=0):
    return datetime(2026, month, day, hour, minute, second, tzinfo=EASTERN)


def counts(approved=0, pending=0, rejected=0, sending=0, unknown=0):
    return {"ok": True, "data": {"by_status": {"approved": approved, "pending": pending, "rejected": rejected,
                                               "sending": sending, "unknown": unknown}}}


def facts(opening=None, plan=None, checked=TEST_CHECKED_AT, requests=(), slots_valid=True):
    return {"opening": opening, "plan": plan, "slots_checked_at": checked, "slots_valid": slots_valid,
            "requests": list(requests)}


# A picked plan for our current slots whose two products are both in the catalog.
PLAN = {"current": True, "products": 2, "missing": 0}


def request(rid, at, off_cycle=False, status="pending"):
    return {"request_id": rid, "submitted_at": at, "off_cycle": off_cycle, "status": status}


def rows(now, f=None, c=None, lock=LOCK):
    built = key_dates.build(now, facts() if f is None else f, counts() if c is None else c, lock)
    return {r["key"]: r for r in built["rows"]}


def check(row, name):
    return next(c for c in row["checks"] if c["key"] == name)


# --- The dates ----------------------------------------------------------------------------------

def test_the_dates_are_in_one_place_and_reuse_the_start():
    assert [kd.key for kd in key_dates.DATES] == [
        "catalog", "fill_request", "stocking", "opening", "first_restock_request", "first_tax"]
    assert all(kd.source for kd in key_dates.DATES)
    assert key_dates.OPENING.day == START.date()
    # The status bar's catalog deadline is the same instant: the end of Wed 25 Nov, Eastern.
    assert dashboard.APPROVALS_DUE == key_dates.CATALOG.due == datetime(2026, 11, 26, 5, tzinfo=timezone.utc)
    assert key_dates.FILL_REQUEST.due == et(11, 29, 23, 59)
    assert key_dates.FIRST_RESTOCK.due == et(12, 4, 0, 0)    # the planner orders through the cutoff minute
    assert key_dates.FIRST_TAX.due == et(12, 5, 23, 59)


def test_each_date_shows_its_day_and_counts_down_before_the_start():
    r = rows(et(11, 20))
    assert [x["when"] for x in r.values()] == [
        "Wed 25 Nov", "Sun 29 Nov, 23:59", "Mon 30 Nov", "Tue 1 to Thu 3 Dec", "Thu 3 Dec, 23:59", "Sat 5 Dec, 23:59"]
    assert r["stocking"]["countdown"] == "in 10 days" and r["first_tax"]["countdown"] == "in 15 days"
    assert r["opening"]["unconfirmed"] and not r["catalog"]["unconfirmed"]
    assert not any(x["passed"] for x in r.values())
    # Days are Eastern calendar days: late on the 24th, the 25th is tomorrow.
    assert rows(et(11, 24, 23, 30))["catalog"]["checks"][0]["pill"] == "due tomorrow"


def test_nothing_recorded_yet_reads_as_due_in_n_days():
    r = rows(et(11, 20), c=counts(approved=3, pending=2))
    cat = check(r["catalog"], "catalog")
    assert (cat["status"], cat["pill"], cat["text"]) == ("due", "due in 5 days", "3 approved, 2 waiting")
    for name in ("plan_picked", "ledger_opening"):
        assert check(r["fill_request"], name)["pill"] == "due in 9 days"
    assert "python -m vending.ledger open" in check(r["fill_request"], "ledger_opening")["text"]
    # Before the opening no request counts: a request then is a test purchase.
    fill = check(r["fill_request"], "fill_request")
    assert fill["status"] == "waiting" and "test purchase" in fill["text"]


def test_everything_recorded_reads_as_done():
    opening = et(11, 28, 18)
    f = facts(opening=opening, plan=PLAN, requests=[request("R1", et(11, 29, 20))])
    r = rows(et(11, 29, 21), f, counts(approved=20, rejected=1))
    assert check(r["catalog"], "catalog") == {
        "key": "catalog", "label": "Catalog approvals", "status": "done", "pill": "done",
        "text": "all 2 of the plan's products are in the catalog; requests: 20 approved, 0 waiting, 1 rejected"}
    assert [check(r["fill_request"], n)["status"] for n in ("plan_picked", "ledger_opening", "fill_request")] == \
        ["done", "done", "done"]
    assert check(r["fill_request"], "ledger_opening")["text"] == "opened Sat 28 Nov, 18:00"
    assert check(r["fill_request"], "fill_request")["text"] == "sent Sun 29 Nov, 20:00, pending"


def test_an_opening_recorded_ahead_says_when_it_opens():
    r = rows(et(11, 27), facts(opening=et(11, 29, 9)))
    assert check(r["fill_request"], "ledger_opening") == {
        "key": "ledger_opening", "label": "Ledger opening recorded", "status": "done", "pill": "done",
        "text": "opens Sun 29 Nov, 09:00"}


def test_missed_deadlines_read_as_overdue():
    r = rows(et(11, 30, 9), c=counts(approved=10, pending=4))
    assert [check(r["fill_request"], n)["status"] for n in ("plan_picked", "ledger_opening")] == ["overdue", "overdue"]
    # Catalog requests still waiting after 25 Nov aren't overdue on our side: they miss the fill.
    cat = check(r["catalog"], "catalog")
    assert (cat["status"], cat["pill"]) == ("missed", "passed")
    assert "miss the first fill" in cat["text"]
    assert r["catalog"]["passed"] and r["catalog"]["countdown"] == "passed"


def test_a_plan_picked_for_an_earlier_assignment_is_not_done():
    c = check(rows(et(11, 21), facts(plan={**PLAN, "current": False}))["fill_request"], "plan_picked")
    assert c["status"] == "due" and "earlier slot assignment" in c["text"]


def test_a_late_fill_request_says_so():
    f = facts(opening=et(11, 28), requests=[request("R1", et(11, 30, 8))])
    c = check(rows(et(11, 30, 9), f)["fill_request"], "fill_request")
    assert c["status"] == "done" and "after the deadline" in c["text"]


@pytest.mark.parametrize("now, checked, lock, status", [
    (et(11, 25), TEST_CHECKED_AT, LOCK, "waiting"),          # stored; the lock hasn't come
    (et(12, 2), et(12, 1, 0, 5), et(12, 1, 0), "done"),     # a check from the lock confirmed it
    (et(12, 2), et(12, 3, 0, 5), LOCK, "waiting"),          # a check after the snapshot's time doesn't count
    (et(12, 2), None, LOCK, "due"),                          # nothing stored yet
])
def test_the_slot_check_waits_for_the_lock(now, checked, lock, status):
    assert check(rows(now, facts(checked=checked), lock=lock)["opening"], "slots_confirmed")["status"] == status


def test_a_bad_lock_setting_cant_tell():
    c = check(rows(et(11, 25), lock=None)["opening"], "slots_confirmed")
    assert c["status"] == "unknown" and "SLOTS_LOCK_AT" in c["text"]


def test_the_first_restock_request_is_not_the_fill_or_an_off_cycle_visit():
    opening = et(11, 28)
    fill = request("FILL", et(11, 29, 20))
    midweek = request("MID", et(12, 1), off_cycle=True)
    now = et(12, 2)
    c = check(rows(now, facts(opening=opening, requests=[fill, midweek]))["first_restock_request"], "restock_request")
    assert (c["status"], c["pill"]) == ("due", "due tomorrow")
    sent = facts(opening=opening, requests=[fill, midweek, request("WEEK1", et(12, 1, 15))])
    c = check(rows(now, sent)["first_restock_request"], "restock_request")
    assert c == {"key": "restock_request", "label": "Restock request sent", "status": "done", "pill": "done",
                 "text": "sent Tue 1 Dec, 15:00, pending"}


# --- After the start ----------------------------------------------------------------------------

def test_after_the_start_it_shrinks_to_the_next_restock_and_tax_saturday():
    built = key_dates.build(et(12, 9, 10), facts(checked=et(12, 3, 0, 5)), counts(), LOCK)
    assert built["after_start"]
    restock, tax = built["rows"]
    assert (restock["key"], restock["when"], restock["what"]) == (
        "next_restock_request", "Thu 10 Dec, 23:59", "Restock request due. Staff fill it Fri 11 Dec")
    assert (tax["key"], tax["when"], tax["countdown"]) == ("next_tax", "Sat 12 Dec, 23:59", "in 3 days")


@pytest.mark.parametrize("now, restock, tax", [
    (et(12, 3, 0, 0), "Thu 3 Dec, 23:59", "Sat 5 Dec, 23:59"),       # the start: the first restock request
    (et(12, 3, 23, 59, 30), "Thu 3 Dec, 23:59", "Sat 5 Dec, 23:59"),  # the planner still orders for this Friday
    (et(12, 4, 0, 0), "Thu 10 Dec, 23:59", "Sat 5 Dec, 23:59"),       # the cutoff's minute has passed
    (et(12, 5, 23, 59), "Thu 10 Dec, 23:59", "Sat 12 Dec, 23:59"),    # so has the tax minute
    (et(12, 6, 1), "Thu 10 Dec, 23:59", "Sat 12 Dec, 23:59"),
])
def test_the_weekly_dates_roll_over_at_their_minute(now, restock, tax):
    built = key_dates.build(now, facts(checked=LOCK), counts(), LOCK)
    assert [r["when"] for r in built["rows"]] == [restock, tax]


def test_on_the_first_day_the_fill_request_isnt_this_weeks_restock():
    f = facts(opening=et(11, 28), checked=et(12, 3, 0, 5), requests=[request("FILL", et(11, 29, 20))])
    restock = key_dates.build(et(12, 3, 9), f, counts(), LOCK)["rows"][0]
    assert check(restock, "restock_request")["status"] == "due"


def test_an_unconfirmed_assignment_after_the_start_stays_on_top():
    built = key_dates.build(et(12, 3, 9), facts(checked=TEST_CHECKED_AT), counts(), LOCK)
    assert [r["key"] for r in built["rows"]] == ["opening", "next_restock_request", "next_tax"]
    c = check(built["rows"][0], "slots_confirmed")
    assert c["status"] == "overdue" and "python -m vending.slots check" in c["text"]


# --- When the records can't be read -------------------------------------------------------------

def test_unread_records_cant_tell_but_the_dates_still_show():
    built = key_dates.build(et(11, 20), None, counts(approved=1), LOCK)
    assert built["unread"] and len(built["rows"]) == 6
    statuses = {c["key"]: c["status"] for r in built["rows"] for c in r["checks"]}
    assert statuses == {"catalog": "done", "plan_picked": "unknown", "ledger_opening": "unknown",
                        "fill_request": "unknown", "slots_confirmed": "unknown", "restock_request": "unknown"}
    page = key_dates.html({"ok": True, "data": built})
    assert escape(key_dates.READ_FAILED) in page and "can&#x27;t tell" in page


def test_a_failed_read_keeps_its_text_out_of_the_panel(capsys):
    def isolate(read):
        raise RuntimeError("password=hunter2 host=db.internal")
    p = key_dates.panel(None, et(11, 20), None, TEST_SLOTS, counts(), LOCK, isolate)
    assert p["ok"] and p["data"]["unread"]
    assert "hunter2" not in repr(p) and "hunter2" not in key_dates.html(p)
    assert "key dates" in capsys.readouterr().err


# --- The section --------------------------------------------------------------------------------

def test_the_section_escapes_everything_it_draws():
    built = key_dates.build(et(11, 20), facts(), counts(), LOCK)
    built["rows"][0]["what"] = "<script>alert(1)</script>"
    built["rows"][0]["source"] = '"><img src=x>'
    page = key_dates.html({"ok": True, "data": built})
    assert "<script>" not in page and "<img" not in page and "&lt;script&gt;" in page


def test_a_snapshot_from_before_the_panel_draws_no_section():
    assert key_dates.html(None) == ""


def test_the_section_marks_each_status_and_the_unconfirmed_day():
    f = facts(opening=et(11, 28), plan=PLAN)
    page = key_dates.html({"ok": True, "data": key_dates.build(et(11, 29), f, counts(pending=1), LOCK)})
    assert page.startswith('<section id="dates"')
    assert '<span class="pill ok">done</span> <b>Ledger opening recorded</b>' in page
    assert '<span class="pill info">due today</span> <b>Fill request sent</b>' in page
    assert '<span class="pill warn">unconfirmed</span>' in page
    assert 'class="kd-row kd-past"' in page     # 25 Nov has passed


# --- In the dashboard ---------------------------------------------------------------------------

@pytest.fixture
def page_conn(schema_url):
    """A connection like the dashboard's own: read-only, in the test schema."""
    from tests.test_dashboard import read_only
    with read_only(*schema_url) as conn:
        yield conn


def _request(conn, rid, at, off_cycle=False, status="pending"):
    conn.execute("insert into purchase_requests (request_id, submitted_at, off_cycle, request_body, status)"
                 " values (%s, %s, %s, '{}', %s)", (rid, at, off_cycle, status))


def test_the_snapshot_reads_our_records_for_the_checks(conn, page_conn, tmp_path, monkeypatch):
    from tests.test_dashboard import _first_fill_plan
    monkeypatch.setattr(dashboard, "lock_at", lambda: LOCK)
    _first_fill_plan(conn, tmp_path)
    ledger.record(conn, "opening", 60000, "opening capital", "test", at=et(11, 28))
    _request(conn, "TEST-OLD", et(11, 20))                        # before the opening: a test purchase
    _request(conn, "REJ", et(11, 28, 12), status="rejected")      # rejected: not the fill
    _request(conn, "FILL", et(11, 29, 20))
    snap = dashboard.snapshot(page_conn, et(11, 29, 21))
    r = {x["key"]: x for x in snap["key_dates"]["data"]["rows"]}
    assert [check(r["fill_request"], n)["status"] for n in ("plan_picked", "ledger_opening", "fill_request")] == \
        ["done", "done", "done"]
    assert check(r["fill_request"], "fill_request")["text"] == "sent Sun 29 Nov, 20:00, pending"
    assert check(r["opening"], "slots_confirmed")["status"] == "waiting"
    # It is in the JSON too, and the page draws it between the status bar and the machine.
    assert '"key_dates"' in dashboard.to_json(snap)
    page = dashboard.render(snap)
    assert page.index('aria-label="Status"') < page.index('id="dates"') < page.index('id="vm"')


def test_a_missing_table_costs_only_the_checks(conn, page_conn, capsys):
    conn.execute("drop table first_fill_plans")
    snap = dashboard.snapshot(page_conn, et(11, 20), public=True)
    data = snap["key_dates"]["data"]
    assert data["unread"] and len(data["rows"]) == 6
    assert "first_fill_plans" not in dashboard.to_json(snap["key_dates"])
    assert "first_fill_plans" not in key_dates.html(snap["key_dates"])
    assert "first_fill_plans" in capsys.readouterr().err
    assert 'id="dates"' in dashboard.render(snap)


def test_the_first_restock_after_the_fill_counts_from_the_database(conn, page_conn, monkeypatch):
    monkeypatch.setattr(dashboard, "lock_at", lambda: LOCK)
    ledger.record(conn, "opening", 60000, "opening capital", "test", at=et(11, 28))
    _request(conn, "FILL", et(11, 29, 20))
    _request(conn, "WEEK1", et(12, 1, 15))
    snap = dashboard.snapshot(page_conn, et(12, 2))
    r = {x["key"]: x for x in snap["key_dates"]["data"]["rows"]}
    assert check(r["first_restock_request"], "restock_request")["text"] == "sent Tue 1 Dec, 15:00, pending"


def test_after_a_rejected_fill_the_first_restock_still_counts(conn, page_conn, monkeypatch):
    # The Sunday fill was rejected, so the first request standing is Thursday's restock. A request from
    # the start on is never the fill, so it counts as this week's restock and the fill isn't done.
    monkeypatch.setattr(dashboard, "lock_at", lambda: LOCK)
    ledger.record(conn, "opening", 60000, "opening capital", "test", at=et(11, 28))
    _request(conn, "FILL", et(11, 29, 20), status="rejected")
    _request(conn, "WEEK1", et(12, 3, 10))
    snap = dashboard.snapshot(page_conn, et(12, 3, 12))
    r = {x["key"]: x for x in snap["key_dates"]["data"]["rows"]}
    c = check(r["next_restock_request"], "restock_request")
    assert c["status"] == "done" and "Thu 3 Dec, 10:00" in c["text"]
    f = facts(opening=et(11, 28), requests=[request("WEEK1", et(12, 3, 10))])
    assert key_dates._fill(f) is None


def test_an_invalid_assignment_checked_after_the_lock_is_not_confirmed(conn, page_conn, monkeypatch):
    # Every reader of our slots refuses a row for another device, so Key dates mustn't call it confirmed.
    monkeypatch.setattr(dashboard, "lock_at", lambda: LOCK)
    conn.execute("update slot_assignment set device_id = 'vmc2', last_checked_at = %s", (et(12, 3, 0, 5),))
    snap = dashboard.snapshot(page_conn, et(12, 3, 9))
    assert not snap["ours"]["ok"]
    rows = snap["key_dates"]["data"]["rows"]
    assert rows[0]["key"] == "opening"
    c = check(rows[0], "slots_confirmed")
    assert c["status"] == "overdue" and "python -m vending.slots record" in c["text"]
    c = check(rows_of(key_dates.build(et(11, 25), facts(checked=et(12, 3, 0, 5), slots_valid=False), counts(), LOCK),
                      "opening"), "slots_confirmed")
    assert c["status"] == "due" and "isn't valid" in c["text"]


def rows_of(built, key):
    return next(r for r in built["rows"] if r["key"] == key)


def test_the_catalog_check_wants_every_product_in_the_picked_plan():
    # Every request approved, but the picked plan loads a product the catalog doesn't list.
    plan = {"current": True, "products": 2, "missing": 1}
    c = check(rows(et(11, 20), facts(plan=plan), counts(approved=20))["catalog"], "catalog")
    assert (c["label"], c["status"], c["pill"]) == ("Catalog approvals", "due", "due in 5 days")
    assert c["text"].startswith("1 of the plan's 2 products isn't in the catalog")
    c = check(rows(et(11, 26), facts(plan=plan), counts(approved=20))["catalog"], "catalog")
    assert (c["status"], c["pill"]) == ("missed", "passed")
    c = check(rows(et(11, 20), facts(plan={**plan, "missing": 0}), counts(approved=20, pending=1))["catalog"], "catalog")
    assert c["status"] == "done" and c["text"].startswith("all 2 of the plan's products are in the catalog")
    # With no plan picked, it is the queue's count, and says so.
    c = check(rows(et(11, 20), facts(), counts(approved=20))["catalog"], "catalog")
    assert (c["label"], c["status"]) == ("Catalog queue clear", "done")


def test_the_catalog_check_reads_the_picked_plans_products(conn, page_conn, tmp_path, monkeypatch):
    from tests.test_dashboard import _first_fill_plan
    monkeypatch.setattr(dashboard, "lock_at", lambda: LOCK)
    _first_fill_plan(conn, tmp_path)     # Red Bull (SIM-RB) and Coke (SIM-COKE)

    def catalog():
        snap = dashboard.snapshot(page_conn, et(11, 20))
        return check({x["key"]: x for x in snap["key_dates"]["data"]["rows"]}["catalog"], "catalog")

    conn.execute("insert into catalog_products (sku, name, last_response) values ('SIM-RB', 'Red Bull', '{}')")
    c = catalog()
    assert c["status"] == "due" and c["text"].startswith("1 of the plan's 2 products isn't in the catalog")
    conn.execute("insert into catalog_products (sku, name, last_response) values ('SIM-COKE', 'Coke', '{}')")
    assert catalog()["status"] == "done"
    conn.execute("update catalog_products set delisted_at = now() where sku = 'SIM-COKE'")
    assert catalog()["status"] == "due"


@pytest.mark.parametrize("now", [et(12, 3, 23, 58, 59), et(12, 3, 23, 59), et(12, 3, 23, 59, 59), et(12, 4, 0, 0),
                                 et(12, 10, 23, 59, 30), et(12, 11, 0, 0, 1)])
def test_the_restock_deadline_passes_when_the_planners_cutoff_does(now):
    from dataclasses import fields

    from vending import planner
    week_starts = next(f.default for f in fields(planner.Config) if f.name == "week_starts")
    past = planner._past_cutoff(now.astimezone(EASTERN), planner.IN_STORE_CUTOFF, week_starts)
    restock, _ = key_dates.weekly(now)
    # The row shows the cutoff the planner still orders for, and that cutoff hasn't passed for either.
    assert now < restock.due
    this_thursday = now.date() if now.weekday() == 3 else None
    assert (restock.day == this_thursday) == (this_thursday is not None and not past)


def test_a_restock_request_sent_in_the_cutoffs_last_minute_counts_for_that_week():
    f = facts(opening=et(11, 28), checked=LOCK, requests=[request("WEEK1", et(12, 3, 23, 59, 20))])
    restock = key_dates.build(et(12, 3, 23, 59, 40), f, counts(), LOCK)["rows"][0]
    assert restock["when"] == "Thu 3 Dec, 23:59" and not restock["passed"]
    assert check(restock, "restock_request")["status"] == "done"


def test_the_section_says_its_times_are_eastern_after_the_start_too():
    built = key_dates.build(et(12, 9, 10), facts(checked=LOCK), counts(), LOCK)
    assert '<span class="hint">Eastern &middot; after the start' in key_dates.html({"ok": True, "data": built})
