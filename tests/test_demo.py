"""The demo copy of the dashboard (`vending.demo`): made-up data, no database, behind the same password."""

from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal
from fractions import Fraction

import psycopg
import pytest

from tests.conftest import open_in
from tests.test_dashboard import read_only
from tests.test_gate import PASSWORD, ask, cookie_from, sign_in
from tests.test_visits import BROWSER, drain, events  # noqa: F401  (a fixture)
from vending import dashboard, db, demo, gate, pages, settings, slots, visits, worker
from vending.demo import make

BANNER = 'DEMO: made-up data, not our machine. The live page is <a href="/dashboard">/dashboard</a>'


@pytest.fixture
def no_database(monkeypatch):
    """Every way to a database fails the test, and the pages' read-only login isn't even set up.

    The demo has to answer from its file alone: no connection, no stored assignment, no snapshot.
    """
    def boom(*args, **kwargs):
        raise AssertionError("the demo went to the database")

    def live_filter(*args, **kwargs):
        raise AssertionError("the demo used a live snapshot filter")

    for owner, names in ((psycopg, ("connect",)), (pages, ("connect", "_connect", "_connection", "stored_slots")),
                         (dashboard, ("connect", "_connect", "snapshot", "our_slots")), (db, ("connect",)),
                         (visits, ("connect", "_connect")), (slots, ("our_slots", "stored", "clock"))):
        for name in names:
            monkeypatch.setattr(owner, name, boom)
    monkeypatch.setattr(dashboard, "only_ours", live_filter)
    monkeypatch.setattr(dashboard, "hide_errors", live_filter)
    monkeypatch.delenv(pages.READER, raising=False)
    monkeypatch.delitem(settings._FILE, pages.READER, raising=False)
    assert not pages.configured()


def drawn(page: str) -> list[str]:
    return re.findall(r'<article class="ma-lane(?: ma-changed)?" id="lane-(\w+)"', page)


# --- The loader -----------------------------------------------------------------------------------

def test_the_loader_gives_back_what_a_live_snapshot_holds():
    snap = demo.load()
    assert snap["as_of"] == datetime.fromisoformat("2026-12-09T15:00:00+00:00")     # Wed 9 Dec 10:00 Eastern
    assert isinstance(snap["status"]["next_cutoff"], datetime)
    assert isinstance(snap["spend"]["data"]["model"]["total"], Decimal)
    assert snap["demo"] is True


def test_the_file_round_trips_through_the_loader_exactly():
    on_disk = json.loads(demo.SNAPSHOT.read_text(encoding="utf-8"))
    assert demo.encode(demo.load()) == on_disk


def test_dates_decimals_and_fractions_round_trip():
    value = {"at": datetime.fromisoformat("2026-12-09T15:00:00-05:00"), "paid": Decimal("34.50"),
             "cents": Fraction(7, 3), "ours": ("11", "13"), "n": [1, None, True, "x", 1.5]}
    again = demo.decode(json.loads(json.dumps(demo.encode(value))))
    assert again == {**value, "ours": ["11", "13"]}
    assert again["at"].utcoffset() == value["at"].utcoffset() and isinstance(again["cents"], Fraction)
    assert again["paid"] == Decimal("34.50") and str(again["paid"]) == "34.50"      # exact text, never a float


@pytest.mark.parametrize("bad", [object(), {1: "key"}, {"x": {1, 2}}])
def test_the_encoder_refuses_what_it_cant_store(bad):
    with pytest.raises(TypeError):
        demo.encode(bad)


def test_a_file_that_isnt_a_demo_snapshot_is_refused(tmp_path):
    live = tmp_path / "live.json"
    live.write_text(json.dumps({"as_of": {"$datetime": "2026-12-09T15:00:00+00:00"}}), encoding="utf-8")
    with pytest.raises(demo.DemoError, match="isn't marked demo"):
        demo.load(live)
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")
    for path in (tmp_path / "broken.json", tmp_path / "missing.json"):
        with pytest.raises(demo.DemoError, match="can't be read"):
            demo.load(path)


def test_the_demos_selections_are_its_own_scattered_twenty():
    ours = demo.load()["ours"]["data"]
    assert len(ours) == len(set(ours)) == 20 and all(len(s) == 2 and s[0] in "12345" and s[1] in "12345678" for s in ours)
    for column in "12345678":                                  # no whole column of the machine
        assert not all(f"{shelf}{column}" in ours for shelf in "12345")
    assert len({s[1] for s in ours}) > 4 and len({s[0] for s in ours}) == 5     # spread over columns and shelves


def test_the_snapshot_covers_every_panel():
    snap = demo.load()
    assert all(snap[k]["ok"] for k in ("plan", "runs", "activity", "recent_calls", "sales", "stock", "counts", "spend",
                                        "visits", "money", "purchase_requests", "catalog_requests", "catalog_counts",
                                        "products"))
    money = snap["money"]["data"]
    assert money["opened"] and money["committed"]["requests"] == 1 and money["visit_fees"] == "25.00"
    assert [r["status"] for r in snap["runs"]["data"]] == ["running", "submitted", "skipped", "submitted", "failed", "submitted"]
    assert snap["runs"]["data"][0]["steps"] and any(r["changes"] for r in snap["runs"]["data"])
    assert snap["plan"]["data"]["slots"] and any(s["changed"] for s in snap["plan"]["data"]["slots"])
    assert {r["outcome"] for r in snap["activity"]["data"]} == {"done", "refused", "failed"}
    assert {r["status"] for r in snap["purchase_requests"]["data"]} == {"fulfilled", "pending"}
    assert snap["catalog_counts"]["data"]["by_status"]["approved"] and snap["stock"]["data"]["closet"]
    assert snap["counts"]["data"] and snap["status"]["halted"] is False and snap["syncs"]["status"] == "ok"
    assert dashboard.verdict(snap["attention"]) == "check"


def test_the_failed_tool_calls_text_is_left_out_like_on_the_public_page():
    snap = demo.load()
    failed = [r for r in snap["activity"]["data"] if r["outcome"] == "failed"]
    assert failed and all(r["detail"] == dashboard.HIDDEN for r in failed)
    assert next(r for r in snap["runs"]["data"] if r["status"] == "failed")["detail"] == dashboard.HIDDEN


# --- The page and its JSON -------------------------------------------------------------------------

def test_the_page_says_it_is_made_up_and_never_refreshes(no_database):
    reply = pages.respond("/dashboard/demo")
    page = reply.body
    assert (reply.status, reply.content_type) == (200, pages.HTML) and reply.csp == pages.CSP_DASHBOARD
    assert BANNER in page and page.index(BANNER) < page.index("<main>")      # the first thing on the page
    assert "<title>DEMO &middot; Check &middot; Agent dashboard</title>" in page
    assert '<meta name="robots" content="noindex">' in page
    assert 'http-equiv="refresh"' not in page and "FROZEN = 1" in page and "fixed snapshot, never refreshes" in page
    assert 'href="/dashboard/demo/state.json">JSON</a>' in page
    assert '<a href="/dashboard">Live dashboard</a>' in page and '<a href="/">Home</a>' in page
    assert "2026-12-09 15:00 UTC" in page        # "as of" is the snapshot's moment, in the server's UTC text


def test_the_page_draws_every_panel_from_the_snapshots_own_selections(no_database):
    page = pages.respond("/dashboard/demo/").body                 # a trailing slash is the same page
    snap = demo.load()
    assert drawn(page) == list(snap["ours"]["data"]) and page.count('class="ma-lane ma-notours"') == 20
    for anchor in ('id="money"', 'id="plan"', 'id="runs"', 'id="calls"', 'id="requests"', 'id="closet"'):
        assert anchor in page, anchor
    assert "Opening" in page or "opening" in page
    assert "How it decided" in page and "pr-001" in page and "pr-002" in page and "GORGIE Electrolyte Water" in page
    assert "COCA-ORIGINAL-20BOT" in page and "from Friday" in page and "6 of 20 slots changed" in page
    assert "Our slots" not in page and "vmc1" not in page


def test_the_viewers_clock_cannot_move_the_pages_times():
    """The live page's script rewrites "3 days ago" from the viewer's own clock. A fixed snapshot must keep
    the times the server wrote."""
    snap = demo.load()
    fixed = dashboard.render(snap, demo=True)
    live = dashboard.render(snap)
    assert "FROZEN = 1" in fixed and "FROZEN = 0" in live
    # Spans stay as written; absolute dates and times are shown in Eastern, not the server's UTC.
    assert "if (FROZEN && (f === 'ago' || f === 'until' || f === 'since')) return;" in fixed
    assert "timeZone: 'America/New_York'" in fixed and "if (live && !FROZEN)" in fixed
    assert "DEMO: made-up" not in live and 'class="demo-banner" role' not in live and 'http-equiv="refresh"' in live
    assert "<title>Check &middot; Agent dashboard</title>" in live


def test_the_demo_keeps_its_toggles_under_its_own_storage_keys(no_database):
    """Same origin as the live page: a toggle flipped on the demo must not move the live page's."""
    page, live = pages.respond("/dashboard/demo").body, dashboard.render(demo.load())
    assert "var PFX = 'demo-dash-';" in page and "var PFX = 'dash-';" in live
    for text in (page, live):
        # Every key goes through the prefix: no literal key is left in the script.
        assert not re.search(r"""(?:getItem|save)\(\s*['"]dash-""", text)
        # show, view and changed: read once each, saved four times (the ops layout keeps no shelf key).
        assert len(re.findall(r"PFX \+ '(?:show|view|changed|shelf)'", text)) == 7
    assert "'demo-dash-' + " not in page      # the prefix is a variable, set once


def test_the_json_is_marked_demo_and_is_the_snapshot(no_database):
    reply = pages.respond("/dashboard/demo/state.json")
    assert (reply.status, reply.content_type) == (200, pages.JSON)
    data = json.loads(reply.body)
    assert next(iter(data)) == "demo" and data["demo"] is True
    assert data["as_of"] == "2026-12-09T15:00:00+00:00" and data["ours"]["ok"] and len(data["ours"]["data"]) == 20
    assert "verdict" not in data and data["attention"]


def test_a_broken_snapshot_answers_plainly_without_its_error(monkeypatch, no_database):
    monkeypatch.setattr(demo, "load", lambda *a: (_ for _ in ()).throw(demo.DemoError("secret path C:/x/y")))
    page, state = pages.respond("/dashboard/demo"), pages.respond("/dashboard/demo/state.json")
    assert (page.status, state.status) == (500, 500)
    assert "secret" not in page.body and "secret" not in state.body and json.loads(state.body)["demo"] is True


def test_the_live_page_links_to_the_demo(schema_url, conn, monkeypatch):
    """The live dashboard's header nav has "Demo copy"; the demo's has "Live dashboard" (checked above)."""
    url, schema = schema_url
    monkeypatch.setattr(pages, "connect", lambda: read_only(url, schema))
    monkeypatch.setattr(pages, "writable", lambda c: [])
    monkeypatch.setenv(pages.READER, url)
    page = pages.respond("/dashboard").body
    assert '<a href="/dashboard/demo">Demo copy</a>' in page and "DEMO: made-up" not in page
    assert 'class="demo-banner"' not in page and "FROZEN = 0" in page


# --- Behind the password, through the worker -------------------------------------------------------

@pytest.fixture
def site(monkeypatch, events, no_database):  # noqa: F811
    """The worker's server with the password on and the real pages code, no database anywhere. Counts
    what reaches the loader."""
    monkeypatch.setattr(gate, "ENABLED", True)
    monkeypatch.setattr(gate, "PASSWORD_HASH", gate.make_hash(PASSWORD, n=2**10))
    monkeypatch.setattr(gate, "WRONG_DELAY_SECONDS", 0)
    monkeypatch.setattr(gate, "_next_try", {})
    monkeypatch.setenv(gate.KEY_SETTING, "postgresql://reader:secret@example/db")
    loaded = []
    real = demo.load
    monkeypatch.setattr(demo, "load", lambda *a: loaded.append(1) or real(*a))
    server = worker.serve_health(worker.State(), 0)
    try:
        yield server.server_address[1], loaded
    finally:
        server.shutdown()
        server.server_close()


def signed_in(port: int) -> dict:
    status, headers, _ = sign_in(port)
    assert status == 303
    return {"Cookie": cookie_from(headers), "Sec-Fetch-Mode": "navigate"}


def test_signed_out_requests_never_reach_the_loader(site):
    port, loaded = site
    for path in ("/dashboard/demo", "/dashboard/demo/", "/dashboard/demo?x=1"):
        status, headers, _ = ask(port, "GET", path)
        assert status == 303 and headers["Location"].startswith("/login?next=/dashboard/demo"), path
    status, _, body = ask(port, "GET", "/dashboard/demo/state.json")
    assert status == 401 and "signed out" in json.loads(body)["error"]
    status, _, _ = ask(port, "GET", "/dashboard/demo/state.json", headers={"Cookie": f"{gate.COOKIE}=1.forged"})
    assert status == 401
    assert loaded == []


def test_a_post_to_the_demo_is_405_signed_in_or_out(site):
    port, loaded = site
    for headers in ({}, signed_in(port)):
        for path in ("/dashboard/demo", "/dashboard/demo/state.json"):
            # No body: the server refuses a POST without reading one, and on Windows a body it never read
            # can make the client's send fail with WinError 10053 before the 405 arrives.
            status, _, _ = ask(port, "POST", path, b"", headers)
            assert status == 405, path
    assert loaded == []


def test_signed_in_the_demo_opens_and_is_not_counted_as_a_visit(site, events):  # noqa: F811
    port, loaded = site
    headers = {**signed_in(port), "Cookie": f"{signed_in(port)['Cookie']}; vend_browser={BROWSER}"}
    drain(events)                                              # the two sign-ins are counted, as they should be
    status, answer, body = ask(port, "GET", "/dashboard/demo", headers=headers)
    assert status == 200 and BANNER in body.decode() and "script-src 'unsafe-inline'" in answer["Content-Security-Policy"]
    status, answer, body = ask(port, "GET", "/dashboard/demo/state.json", headers=headers)
    assert status == 200 and json.loads(body)["demo"] is True
    # No visit, and no browser cookie handed out for looking at it.
    assert events.empty() and "Set-Cookie" not in answer
    status, answer, _ = ask(port, "GET", "/dashboard/demo", headers={"Cookie": headers["Cookie"].split(";")[0]})
    assert status == 200 and "Set-Cookie" not in answer and events.empty()
    assert len(loaded) == 3
    assert "/dashboard/demo" not in visits.PAGES and not visits.view("/dashboard/demo", BROWSER)
    assert events.empty()


def test_the_other_paths_under_dashboard_are_still_unknown(site):
    port, _ = site
    status, _, _ = ask(port, "GET", "/dashboard/demo/other", headers=signed_in(port))
    assert status == 404
    status, _, _ = ask(port, "GET", "/dashboard/demox", headers=signed_in(port))
    assert status == 404


# --- The generator ---------------------------------------------------------------------------------

def test_the_generator_refuses_a_database_that_isnt_local():
    for url in ("postgresql://u:p@db.example.net/x", "postgresql://u:p@127.0.0.1,db.example.net/x", "postgresql:///x"):
        with pytest.raises(SystemExit):
            make._require_local(url)
    make._require_local("postgresql://u:p@127.0.0.1:5432/x")


@pytest.mark.parametrize("env, url", [
    ({"PGHOSTADDR": "203.0.113.5"}, "postgresql://u:p@127.0.0.1:5432/x"),   # PGHOSTADDR beats the URL's host
    ({"PGSERVICE": "azure"}, "postgresql://u:p@127.0.0.1:5432/x"),
    ({}, "postgresql://u:p@127.0.0.1:5432/x?service=azure"),
])
def test_the_generator_refuses_an_environment_that_redirects_the_connection(monkeypatch, env, url):
    for name in ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(SystemExit):
        make._require_local(url)


def test_the_week_is_made_up_from_a_fixed_moment():
    assert make.AS_OF == datetime.fromisoformat("2026-12-09T15:00:00+00:00")
    sim = make.simulate()
    assert sim["end"]["31"] == 0 and set(sim["at_visit"]) == set(make.TOPPED_UP)
    assert len(make.SELECTIONS) == 20 and set(make.NOT_BOUGHT) <= set(make.SELECTIONS)
    assert make.poll_times()[-1] == make.LAST_POLL and make.poll_times() == sorted(make.poll_times())


def test_the_made_up_codes_follow_the_catalogs_sku_form():
    """LINE-FLAVOR-SIZEPKG in capitals, as the real catalog writes them (MONSTER-WHITE-16CAN, PURELEAF-LEMON-16_9BOT):
    the flavor cut to 8 characters, "_" for a decimal point. The banner, not the code, says the data is made up."""
    form = re.compile(r"^[A-Z]+-[A-Z_]{1,8}-\d+(?:_\d+)?(?:BOT|CAN|CAR)$")
    skus = set(make.PRODUCTS) | {sku for _, sku, *_ in make.DAY_ONE} | set(make.CAPACITY_OVERRIDES)
    assert skus == set(make.PRODUCTS) and all(form.match(sku) for sku in skus), sorted(skus)
    assert not any("DEMO" in sku for sku in skus)


def test_every_lane_is_filled_to_its_column():
    """Kevin's rule (19 Nov): every restock fills every slot it stocks to the column's capacity. A pulled lane
    (fill 0) is the only exception, and the plan says what replaces it."""
    from vending import capacity

    def column(sku: str) -> int:
        units = capacity.for_product(sku, make.PRODUCTS[sku][3], make.CAPACITY_OVERRIDES)
        assert units, f"{sku} has no capacity: add a count to CAPACITY_OVERRIDES"
        return units

    for plan in (make.plan_one(), make.plan_next()):
        for entry in plan["slots"]:
            assert entry["fill_to"] in (0, column(entry["product"])), entry
            assert "under full" not in entry["reason"]
    assert make.CAPACITY_OVERRIDES == {"PREMIER-CHOCOLAT-11CAR": 9}
    assert [s["slot"] for s in make.plan_next()["slots"] if s["fill_to"] == 0] == ["44"]
    assert "swap" in make.NEXT_CHANGES["44"][3]


def _seen(sim: dict, slot: str, at: datetime) -> tuple[int, int]:
    """(units sold, units on hand) at the last poll before `at`: what the agent could see then."""
    poll = max(p for p in make.poll_times() if p <= at)
    sold = sim["sold"].get(poll.replace(minute=0), {}).get(slot, 0)
    fill = next(f for s, _, f, *_ in make.DAY_ONE if s == slot)
    topped = fill - sim["at_visit"][slot] if slot in make.TOPPED_UP and poll >= make.VISIT else 0
    return sold, fill - sold + topped


def test_the_runs_reasons_match_the_made_up_sales():
    """Each reason says only what the polls before its run showed: 'ran out on Saturday morning' means it did."""
    from vending.demo.make import _utc

    sim = make.simulate()
    visit_run, saturday_10_30, monday_run = _utc(5, 14), _utc(5, 15, 31), _utc(7, 14)
    # Saturday 9:00 ET: Coca-Cola and Red Bull down to 1 each, so a $25 visit at noon.
    assert _seen(sim, "11", visit_run)[1] == _seen(sim, "37", visit_run)[1] == 1
    # Both had run out by the 10:30 poll, before the noon top-up.
    assert _seen(sim, "11", saturday_10_30)[1] == _seen(sim, "37", saturday_10_30)[1] == 0
    # Monday 9:00 ET: Red Bull and Pepsi empty, Coca-Cola down to 1 again.
    assert [_seen(sim, s, monday_run)[1] for s in ("37", "31", "11")] == [0, 0, 1]
    assert _seen(sim, "31", _utc(7, 3, 31))[1] == 0 and _seen(sim, "31", _utc(6, 21, 31))[1] > 0  # out on Sunday night
    # The slow lanes: 1 sold each since Thursday; Strawberry sold three times Paradise Paloma.
    assert [_seen(sim, s, monday_run)[0] for s in ("24", "28", "44", "22")] == [1, 1, 1, 3]
    texts = [why for *_, why in make.NEXT_CHANGES.values()]
    assert not any(day in why for why in texts for day in ("Tuesday", "Wednesday", "Thursday's", "6 days"))


def test_fridays_order_and_the_closet_fill_every_lane():
    """What the closet can't fill to the column by Friday is in the pending order, so Friday under-stocks nothing."""
    from vending.demo.make import _utc

    sim, friday = make.simulate(), _utc(11, 15)
    closet: dict[str, int] = {}
    for slot, sku, fill, *_ in make.DAY_ONE:
        if slot not in make.NOT_BOUGHT:
            closet[sku] = closet.get(sku, 0) + 12 - fill - (slot == "22")       # one can of 22 was lost
    for slot in make.TOPPED_UP:
        sku, fill = next((k, f) for s, k, f, *_ in make.DAY_ONE if s == slot)
        closet[sku] += 12 - (fill - sim["at_visit"][slot])
    rate = {s: r for s, _, _, _, r, _ in make.DAY_ONE}
    old = {s: k for s, k, *_ in make.DAY_ONE}
    days = (friday - make.LAST_POLL).total_seconds() / 86400
    need: dict[str, int] = {}
    for e in make.plan_next()["slots"]:
        if e["fill_to"] and e["slot"] not in make.NOT_BOUGHT and e["product"] == old[e["slot"]]:
            left = max(0, sim["end"][e["slot"]] - int(rate[e["slot"]] * days))
            need[e["product"]] = need.get(e["product"], 0) + e["fill_to"] - left
    bought = {sku for sku, *_ in make.FRIDAY_ORDER.values()}
    short = {sku for sku, units in need.items() if closet[sku] < units}
    assert short and short <= bought, short - bought


def test_the_committed_snapshot_is_what_the_generator_makes(schema_url):
    """The file can't drift from the page's own snapshot: change the snapshot's shape or the seed, and this
    fails until `python -m vending.demo.make` is run again."""
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo("America/New_York")
    except Exception:
        pytest.skip("no IANA time zone data (pip install tzdata), so the cutoff can't be made")
    url, schema = schema_url
    with open_in(url, schema) as writer, read_only(url, schema) as reader:
        writer.execute("delete from slot_assignment")             # the fixture's stored assignment isn't the demo's
        text = make.render_file(make.make(writer, reader))
    assert text == demo.SNAPSHOT.read_text(encoding="utf-8"), \
        "vending/demo/snapshot.json is out of date: run python -m vending.demo.make (on a scratch local database)"
