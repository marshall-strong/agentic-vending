"""The live-schedule rehearsal (`vending.simulate.live`): the season's clock in the database, the read-only export,
and a first week run by the worker's own scheduler from the team's picked plan.

`EXAMPLE` is the week replayer's made-up assignment, not ours: a season's slots are always handed in.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

psycopg = pytest.importorskip("psycopg")

from vending.replay.cases import CASE_SLOTS as EXAMPLE  # noqa: E402
from vending.simulate import world  # noqa: E402
from vending.simulate.world import EASTERN, Calendar, Machine  # noqa: E402
from vending.simulate.bridge import SimBridge, Worker  # noqa: E402
from datetime import date  # noqa: E402

CHECKED = datetime(2026, 11, 27, 15, 0, tzinfo=timezone.utc)
SEED = 12      # a seed whose day-one visit finds every drink in stock
NEW = "OLIPOP-VINTAGE_COLA-12CAN"       # a drink the world doesn't have: the export brings it


def open_clock(url: str, schema: str):
    """A connection whose `now()` is the season's: the schema's clock functions before `pg_catalog`'s."""
    from vending import db
    from vending.simulate import dbclock

    return psycopg.connect(url, autocommit=True, options=db.session_options(url, dbclock.search_path(schema)))


# --- the database's clock -----------------------------------------------------------------------------------

def test_the_database_reads_the_seasons_clock_and_stamps_defaults_with_it(schema_url):
    from tests.conftest import open_in
    from vending import config
    from vending.simulate import dbclock

    url, schema = schema_url
    at = datetime(2027, 1, 5, 17, 30, tzinfo=timezone.utc)
    with open_clock(url, schema) as conn:
        dbclock.install(conn, schema)
        assert conn.execute("select now()").fetchone()[0].year == datetime.now().year   # empty: the real clock
        dbclock.set(conn, at)
        assert conn.execute("select now(), clock_timestamp(), transaction_timestamp()").fetchone() == (at, at, at)
        assert dbclock.reads_season_time(conn)
        config.set_value(conn, "model_run_cap_cents", 150, "test")         # an insert: the column's default
        assert conn.execute("select updated_at from config where key = 'model_run_cap_cents'").fetchone()[0] == at
        assert conn.execute("select pg_catalog.clock_timestamp() < %s", (at,)).fetchone()[0]
    with open_in(url, schema) as plain:          # without the path, Postgres's own clock, as anywhere else
        assert not dbclock.reads_season_time(plain)


# --- the export -----------------------------------------------------------------------------------------------

def _plan() -> tuple[dict, dict]:
    """A picked plan on the example slots: Red Bull, Coca-Cola and a drink the world lacks, under the $400 cap."""
    rb, coke = "REDBULL-ORIGINAL-8_4CAN", "COCA-ORIGINAL-20BOT"
    day_one = ([{"slot": s, "sku": rb, "units": 8, "price_cents": 400} for s in EXAMPLE[:5]]
               + [{"slot": s, "sku": coke, "units": 6, "price_cents": 325} for s in EXAMPLE[5:10]]
               + [{"slot": s, "sku": NEW, "units": 4, "price_cents": 300} for s in EXAMPLE[10:]])
    from vending import sourcing
    packs = [{"sku": rb, "units_per_pack": 24, "expected_cents": 3499, "max_cents": sourcing.default_max(3499)},
             {"sku": coke, "units_per_pack": 24, "expected_cents": 5495, "max_cents": sourcing.default_max(5495)},
             {"sku": NEW, "units_per_pack": 12, "expected_cents": 2400, "max_cents": sourcing.default_max(2400)}]
    return {"day_one": day_one}, {"packs": packs}


def _export() -> dict:
    from vending import sourcing
    plan, projection = _plan()
    rows = []
    for i, p in enumerate(projection["packs"]):
        rows.append({"sku": p["sku"], "rank": "primary", "store": "Amazon", "units_per_pack": p["units_per_pack"],
                     "expected_cents": p["expected_cents"], "max_cents": sourcing.default_max(p["expected_cents"]),
                     "url": f"https://www.amazon.com/dp/B0TEST{i:04d}", "pack_description": None, "checked_at": CHECKED.isoformat(),
                     "checked_by": "Test Checker", "note": None, "updated_at": CHECKED.isoformat(), "stock": None})
    # Plan #8's case on 25 Nov: an alternate whose most-we-pay is 2 cents over the pick's, so the fill's first
    # submit is refused (pick_deviation) and a person sends it with --accept-deviations.
    coke = rows[1]
    rows.append(dict(coke, rank="alternate", store="Walmart", url="https://www.walmart.com/ip/coke/123",
                     max_cents=coke["max_cents"] + 2))
    catalog = [{"sku": p.sku, "name": p.name, "line": p.line, "flavor": p.flavor, "size": p.size, "delisted_at": None}
               for p in world.PRODUCTS if p.sku in {r["sku"] for r in rows}]
    catalog.append({"sku": NEW, "name": "Olipop Vintage Cola 12 oz can", "line": "Olipop", "flavor": "Vintage Cola",
                    "size": "12 oz can", "delisted_at": None})
    return {"version": 1, "exported_at": CHECKED.isoformat(),
            "assignment": {"device_id": "vmc1", "slots": list(EXAMPLE)},
            "plan": {"id": 8, "loaded_at": CHECKED.isoformat(), "loaded_by": "Test Loader", "label": "7c",
                     "source_note": "test", "plan": plan, "projection": projection, "slots": list(EXAMPLE),
                     "picked": True},
            "sourcing": rows, "price_lookups": [], "catalog": catalog}


def test_the_export_reads_only_our_rows_in_a_read_only_transaction(schema_url):
    from psycopg.types.json import Jsonb

    from tests.conftest import TEST_SLOTS, open_in
    from vending import sourcing
    from vending.simulate import export

    url, schema = schema_url
    plan, projection = _plan()
    with open_in(url, schema) as conn:
        plan_id = conn.execute("insert into first_fill_plans (loaded_by, label, source_note, plan, projection, slots,"
                               " picked) values ('t', '7c', 's', %s, %s, %s, true) returning id",
                               (Jsonb(plan), Jsonb(projection), list(TEST_SLOTS))).fetchone()[0]
        conn.execute("insert into catalog_products (sku, name, line, flavor, size, last_response) values"
                     " ('COCA-ORIGINAL-20BOT', 'Coke', 'Coca-Cola', 'Original', '20 oz bottle', '{\"x\": 1}')")
        sourcing.set_option(conn, sourcing.Option("COCA-ORIGINAL-20BOT", "primary", "Amazon", 24, 5495, 6319,
                                                  "https://www.amazon.com/dp/B004I405P4", None, CHECKED, "t", None),
                            CHECKED)
    with export.read_only_connection(url + f"?options=-csearch_path%3D{schema}") as ro:
        data = export.read(ro, plan_id)
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            ro.execute("insert into config (key, value, updated_by) values ('x', '1', 'x')")
    assert data["plan"]["id"] == plan_id and data["plan"]["label"] == "7c"
    assert data["assignment"]["slots"] == list(TEST_SLOTS) and "response" not in data["assignment"]
    assert [r["sku"] for r in data["sourcing"]] == ["COCA-ORIGINAL-20BOT"]
    assert data["catalog"] == [{"sku": "COCA-ORIGINAL-20BOT", "name": "Coke", "line": "Coca-Cola",
                                "flavor": "Original", "size": "20 oz bottle", "delisted_at": None}]
    json.dumps(data)        # it is written as JSON
    # Sol, #253 finding 4: a plan loaded for another assignment is refused before its slots are read.
    with open_in(url, schema) as conn:
        other = conn.execute("insert into first_fill_plans (loaded_by, label, source_note, plan, projection, slots)"
                             " values ('t', '6', 's', %s, %s, %s) returning id",
                             (Jsonb(plan), Jsonb(projection), list(TEST_SLOTS[:10]))).fetchone()[0]
    with export.read_only_connection(url + f"?options=-csearch_path%3D{schema}") as ro:
        with pytest.raises(LookupError, match="another slot assignment"):
            export.read(ro, other)


def test_an_exported_drink_the_world_lacks_gets_its_lines_rate_or_the_default():
    from vending.simulate import export
    data = _export()
    products, left = export.products(data)
    [new] = [p for p in products if p["sku"] == NEW]
    assert new["rate"] == world.UNKNOWN_LINE_RATE and new["reference_cents"] == 300 and left == []
    assert {c["sku"] for c in export.catalog(data)} >= {NEW, "REDBULL-ORIGINAL-8_4CAN"}
    # Sol, #253 finding 5: a drink the team's catalog has delisted is left out of the bridge's answer, a world one too.
    for c in data["catalog"]:
        if c["sku"] in (NEW, "REDBULL-ORIGINAL-8_4CAN"):
            c["delisted_at"] = CHECKED.isoformat()
    assert not {NEW, "REDBULL-ORIGINAL-8_4CAN"} & {c["sku"] for c in export.catalog(data)}


# --- a first week on the live schedule ------------------------------------------------------------------------

def test_the_first_week_runs_from_the_picked_plan_and_the_workers_scheduler(schema_url, monkeypatch, tmp_path):
    from vending import db
    from vending.simulate import dashcheck, dbclock, export, live, season
    from vending.simulate.clock import patched

    url, schema = schema_url
    monkeypatch.setattr(db, "connect", lambda **_: open_clock(url, schema))
    monkeypatch.setenv("VENDING_TOOLS", "built")
    data = _export()
    opts = {"budget": Decimal(1), "run_cap": Decimal(1), "min_run": Decimal(0)}
    added = []
    try:
        with open_clock(url, schema) as conn, live.environment():
            conn.execute("delete from slot_assignment")
            st = season.new_state(SEED, "scripted", "claude-opus-5-5", "medium", False, 3, export.assignment(data),
                                  schedule="live", catalog=export.catalog(data))
            st.products, _ = export.products(data)
            st.live["unit_prices"] = export.unit_prices(data)
            added = world.register(st.products)
            world.register_prices(st.live["unit_prices"])
            assert st.clock.now() == live.FILL_AT
            assert not [e for e in st.events if e.kind in ("agent_run", "check_in", "ledger_open")]
            with patched(st.clock):
                season.setup(conn, st, opts["budget"], opts["run_cap"],
                             prices=lambda c, _now: export.load(c, data, season.BY))
                live.configure(conn)
                steps = live.day_one(conn, st, data, say=lambda *_: None)
            assert steps["ledger_open"]["exit"] == 0 and steps["submit"]["exit"] == 0, steps
            assert steps["refused_first"]["codes"] == ["pick_deviation"] and "--accept-deviations" in steps["submit"]["argv"]
            [filed] = st.bridge.requests.values()
            assert filed.submitted_at == live.FILL_AT
            assert conn.execute("select purpose from purchase_submissions").fetchall() == [("fill",)]
            opts["driver"] = live.Driver(conn, st, opts, say=lambda *_: None,
                                         dashboard_url=dbclock.with_clock(url, schema))
            stop = datetime(2026, 12, 5, tzinfo=EASTERN)
            st = season.run_season(conn, st, opts, tmp_path / "checkpoint.pickle", stop, say=lambda *_: None)
            rows = conn.execute("select due_at, kind, mode, outcome from agent_schedule order by due_at").fetchall()
            got = [(d.astimezone(EASTERN).strftime("%a %H:%M"), k, m, o) for d, k, m, o in rows]
            # Wed 2 Dec 14:00 is before the start: claimed live, run as a dry run. Thu 3 Dec noon is the first live.
            assert got[0] == ("Wed 14:00", "weekly", "dry-run", "skipped"), got
            assert got[1][:3] == ("Thu 12:00", "weekly", "live"), got
            assert got[1][3] in ("submitted", "skipped"), got
            detail = conn.execute("select detail from agent_schedule order by due_at limit 1").fetchone()[0]
            assert detail.startswith("Run as a dry run, not live: the competition starts")
            # The day-one fill was loaded, and every scheduled run went through the run lease.
            assert any(e["kind"] == "visit" for e in st.log)
            assert conn.execute("select token from agent_run_lease").fetchone() == (None,)
            # The round's jobs: a tracking check each day once the fill is loaded, and the price check as a dry run.
            days = conn.execute("select eastern_day, status from tracking_checks order by id").fetchall()
            # Stocked Mon 30 Nov (the fill went out Saturday, before Amazon's cutoff): a check each day from then.
            assert [(str(d), s) for d, s in days] == [(f"2026-{d}", 200) for d in ("11-30", "12-01", "12-02", "12-03",
                                                                                     "12-04")], days
            assert conn.execute("select due_at, mode from price_check_runs").fetchall() == [
                (datetime(2026, 12, 1, 12, tzinfo=EASTERN), "dry-run")]
            # The dashboard, as /dashboard and /dashboard/run/<id> read it, on the season's clock.
            runs = conn.execute("select id, mode from agent_runs order by id").fetchall()
            found = dashcheck.check(dbclock.with_clock(url, schema), [r for r, _ in runs], label="test")
            assert found["errors"] == [] and found["panels"] and found["snapshot_seconds"] >= 0
            assert found["as_of"].startswith("2026-12-05")
            assert all("error" not in r for r in found["runs"]) and len(found["runs"]) == len(runs)
            # Sol, #253 finding 8: a dry run's page is a 404 on the deployed route, and says so here.
            assert [r["status"] for r in found["runs"]] == [200 if m == "live" else 404 for _, m in runs]
            summary = live.summary(conn, st)
            assert summary["counts"]["weekly"] == 2 and summary["day_one"]["plan_id"] == 8
        # Sol, #253 finding 2: a plain --resume takes the live schedule from the checkpoint, not --schedule's default.
        from vending.simulate import __main__ as cli
        for name in ("DATABASE_URL", "DEX_BASE_URL", "DEX_API_KEY"):
            monkeypatch.setenv(name, "unset-by-the-test")
        assert cli.main(["--database-url", f"{url}?options=-csearch_path%3D{schema}", "--resume",
                         "--out", str(tmp_path), "--stop-at", "2026-12-07"]) == 0
        assert json.loads((tmp_path / "season.json").read_text(encoding="utf-8"))["run_day"] == "live schedule"
    finally:
        for sku in added:
            world.BY_SKU.pop(sku, None)


def test_the_live_schedule_needs_the_seasons_clock(schema_url):
    from tests.conftest import open_in
    from vending.simulate import season

    url, schema = schema_url
    with open_in(url, schema) as conn:
        conn.execute("delete from slot_assignment")
        st = season.new_state(11, "scripted", "m", "medium", False, 3, EXAMPLE, schedule="live")
        with pytest.raises(SystemExit, match="season's clock"):
            season.setup(conn, st, Decimal(1), Decimal(1))


def test_the_scripted_rule_sends_a_midweek_runs_refill_off_cycle():
    from vending import agent_schedule
    from vending.simulate.scripted import MIDWEEK_MARK, Scripted

    assert MIDWEEK_MARK in agent_schedule.MIDWEEK_OCCASION
    rule = Scripted()
    rule.create(model="m", messages=[{"role": "user", "content": "Opening. " + agent_schedule.midweek_occasion("x")}])
    rule.slots = list(EXAMPLE)
    assert rule.midweek and rule._desired([])["off_cycle"] is True
    weekly = Scripted()
    weekly.create(model="m", messages=[{"role": "user", "content": "Opening."}])
    weekly.slots = list(EXAMPLE)
    assert "off_cycle" not in weekly._desired([])


def test_the_live_settings_last_only_as_long_as_the_season(monkeypatch):
    import os

    from vending.simulate import live

    monkeypatch.setenv("AGENT_SCHEDULE_MODE", "off")
    monkeypatch.delenv("MIDWEEK_WAKE", raising=False)
    with live.environment():
        assert os.environ["AGENT_SCHEDULE_MODE"] == "live" and os.environ["MIDWEEK_WAKE"] == "on"
    assert os.environ["AGENT_SCHEDULE_MODE"] == "off" and "MIDWEEK_WAKE" not in os.environ


def test_the_clock_option_goes_after_the_urls_own():
    from psycopg.conninfo import conninfo_to_dict

    from vending.simulate import dbclock

    url = dbclock.with_clock("postgresql://u:p@127.0.0.1:5432/sim?options=-c%20statement_timeout%3D5000", "s1")
    assert conninfo_to_dict(url)["options"] == "-c statement_timeout=5000 -c search_path=s1,pg_catalog"
    assert dbclock.schema_of(url) == "s1" and dbclock.schema_of("postgresql://u@127.0.0.1/sim") == "public"


def test_a_setup_refuses_a_database_that_already_holds_data(schema_url):
    """Sol, #253 finding 1: the season's clock repoints every column default, so setup writes nothing anywhere but a
    freshly migrated database."""
    from tests.conftest import open_in
    from vending.simulate import dbclock, season

    url, schema = schema_url
    with open_clock(url, schema) as conn:
        conn.execute("delete from slot_assignment")
        conn.execute("insert into catalog_products (sku, name, line, flavor, size, last_response) values"
                     " ('X-1', 'x', 'x', 'x', '12 oz can', '{}')")
        st = season.new_state(11, "scripted", "m", "medium", False, 3, EXAMPLE)
        with pytest.raises(SystemExit, match="catalog_products"):
            season.setup(conn, st, Decimal(1), Decimal(1))
        assert not dbclock.installed(conn)
    with open_in(url, schema) as conn:
        assert season.not_fresh(conn, schema) == ["catalog_products"]


def test_a_season_from_before_the_clock_goes_on_as_before(schema_url, monkeypatch, tmp_path):
    """Sol, #253 finding 3: a run-day season whose database was set up before the season's clock existed resumes."""
    from datetime import timedelta

    from tests.conftest import open_in
    from vending import db
    from vending.simulate import dbclock, season
    from vending.simulate.clock import patched

    url, schema = schema_url
    monkeypatch.setattr(db, "connect", lambda **_: open_in(url, schema))
    with open_in(url, schema) as conn:
        conn.execute("delete from slot_assignment")
        st = season.new_state(11, "scripted", "m", "medium", False, 3, EXAMPLE)
        with monkeypatch.context() as m, patched(st.clock):
            m.setattr(dbclock, "install", lambda *_a, **_k: None)      # as setup was before this PR
            m.setattr(dbclock, "set", lambda *_a, **_k: None)
            season.setup(conn, st, Decimal(1), Decimal(1))
        assert not dbclock.installed(conn)
        season.run_season(conn, st, {"budget": 1, "run_cap": 1, "min_run": 0}, tmp_path / "c.pickle",
                          st.poll_at + timedelta(hours=2), say=lambda *_: None)
        assert st.db_clock is False and st.poll_at == world.SIM_START + timedelta(hours=2, minutes=5)


def test_a_stuck_scheduler_never_strands_an_event_late_in_the_hour(schema_url, monkeypatch, tmp_path):
    """Sol, #253 finding 7: a scheduler that keeps asking for the same moment is held back a minute at a time, and
    an event in the hour's last seconds still runs."""
    from datetime import timedelta

    from vending import db
    from vending.simulate import season
    from vending.simulate.clock import patched

    url, schema = schema_url
    monkeypatch.setattr(db, "connect", lambda **_: open_clock(url, schema))
    with open_clock(url, schema) as conn:
        conn.execute("delete from slot_assignment")
        st = season.new_state(11, "scripted", "m", "medium", False, 3, EXAMPLE)
        with patched(st.clock):
            season.setup(conn, st, Decimal(1), Decimal(1))
        late = st.poll_at + timedelta(minutes=59, seconds=40)
        st.events = [season.Event(late, "catalog_sync")]
        st.schedule = "live"

        class Stuck:
            ticks: list = []

            def next_due(self, now):
                return now

            def tick(self, now):
                self.ticks.append(now)

            def round(self, now, finished=None):
                pass

        driver = Stuck()
        season.run_season(conn, st, {"budget": 1, "run_cap": 1, "min_run": 0, "driver": driver},
                          tmp_path / "c.pickle", st.poll_at + timedelta(hours=1), say=lambda *_: None)
        assert st.events == [] and st.live["stuck"]
        assert len(driver.ticks) < 200          # held back a minute at a time, never a spin


def test_a_round_whose_receipt_sync_failed_sends_no_tracking_check(monkeypatch):
    """Sol, #253 finding 6: as the worker's NEEDS, a receipt sync that didn't finish with 0 holds the day's tracking
    check back; the midweek check needs only the slot check."""
    from types import SimpleNamespace

    from vending import midweek_wake, tracking_daily
    from vending.simulate import live

    sent, woke = [], []
    monkeypatch.setattr(tracking_daily, "run", lambda *a, **k: sent.append(1) or 0)
    monkeypatch.setattr(midweek_wake, "enabled", lambda: True)
    monkeypatch.setattr(midweek_wake, "check", lambda *a, **k: woke.append(1) and False)
    driver = object.__new__(live.Driver)
    driver.st = SimpleNamespace(live={}, clock=SimpleNamespace(now=lambda: live.FILL_AT))
    driver.conn, driver.dashboard_url = None, None
    driver.agents = SimpleNamespace(poke=lambda: None)
    driver._books = lambda now: None
    driver.round(live.FILL_AT, {"slots_check": True, "sync_history": True, "sync_requests": False})
    assert sent == [] and woke == [1]
    assert driver.st.live["rounds"]["skipped"][0]["job"] == "tracking_daily"
    driver.round(live.FILL_AT, {"slots_check": True, "sync_history": True, "sync_requests": True})
    assert sent == [1]


def _amazon_line(slot: str, sku: str = "REDBULL-ORIGINAL-8_4CAN", store: str = "Amazon", alternate=None) -> dict:
    line = {"slot": slot, "source": "purchase",
            "primary": {"sku": sku, "store": store, "packs": 1, "units_per_pack": 24, "max_price_per_pack": "60.00",
                        "sale_price": "4.00"}}
    if alternate:
        line["alternate"] = alternate
    return line


def test_amazon_lines_past_their_cutoff_wait_for_the_next_scheduled_visit():
    """Parent, 25 Nov: in the live rehearsal an Amazon line ordered past its cutoff (Sat 23:59 for the Mon 30 Nov
    stocking, Wed 23:59 for a Friday visit) isn't loaded at the visit it was ordered for: David (17 Nov), Amazon items
    that haven't arrived when the store-bought drinks are stocked are stocked at the next scheduled visit."""
    from vending.simulate import live, season

    st = season.new_state(1, "scripted", "m", "medium", False, 3, EXAMPLE, schedule="live")
    cal = Calendar()
    body = {"lines": [_amazon_line("15"), _amazon_line("16", "COCA-ORIGINAL-20BOT", "Walmart")]}
    local = lambda t: t.astimezone(EASTERN)  # noqa: E731
    # The day-one fill sent Saturday 18:00: everything is stocked Mon 30 Nov, nothing late.
    sat = datetime(2026, 11, 28, 18, tzinfo=EASTERN)
    when, late = season.deliveries(cal, st, sat, body, "a")
    assert local(when).date() == date(2026, 11, 30) and late == set()
    # Sent Sunday 20:00: the Walmart line is stocked Monday; the Amazon line is past Saturday's cutoff.
    when, late = season.deliveries(cal, st, datetime(2026, 11, 29, 20, tzinfo=EASTERN), body, "b")
    assert local(when).date() == date(2026, 11, 30) and late == {0}
    # A Wednesday 14:00 order: everything on Friday. A Thursday noon order: Friday, with the Amazon line late.
    when, late = season.deliveries(cal, st, datetime(2026, 12, 9, 14, tzinfo=EASTERN), body, "c")
    assert local(when).date() == date(2026, 12, 11) and late == set()
    when, late = season.deliveries(cal, st, datetime(2026, 12, 10, 12, tzinfo=EASTERN), body, "d")
    assert local(when).date() == date(2026, 12, 11) and late == {0}
    assert live.FILL_AT == datetime(2026, 11, 28, 18, tzinfo=EASTERN)


def test_a_late_amazon_line_goes_to_back_stock_and_its_slot_waits():
    """The worker buys a late Amazon line (the order went in when the worker saw the request) but its goods aren't
    there to load: they go to back stock, and a back-stock line that loads from it waits too. An in-store alternate
    is bought instead when the line has one."""
    m = Machine.empty(EXAMPLE)
    bridge = SimBridge(m, lambda: datetime(2026, 12, 11, 20, tzinfo=timezone.utc))
    walmart = {"sku": "REDBULL-ORIGINAL-8_4CAN", "store": "Walmart", "packs": 1, "units_per_pack": 24,
               "max_price_per_pack": "60.00", "sale_price": "4.00"}
    body = {"off_cycle": False, "lines": [
        _amazon_line("15"),
        {"slot": "16", "source": "back_stock", "load": {"sku": "REDBULL-ORIGINAL-8_4CAN", "units": 8,
                                                         "sale_price": "4.00"}},
        _amazon_line("17", "COCA-ORIGINAL-20BOT"),
        _amazon_line("18", "CELSIUS-SPARKLING_ORANGE-12CAN", alternate={**walmart, "sku": "CELSIUS-SPARKLING_ORANGE-12CAN",
                                                                         "units_per_pack": 12})]}
    from vending.simulate.bridge import Filed
    bridge.requests["r1"] = Filed("r1", body, datetime(2026, 12, 10, 17, tzinfo=timezone.utc))
    events = Worker(3).fulfil(bridge, "r1", datetime(2026, 12, 11, 20, tzinfo=timezone.utc), None, late={0, 2, 3})
    by_slot = {e["slot"]: e for e in events}
    assert by_slot["15"]["units_loaded"] == 0 and by_slot["15"]["outcome"] == "primary" and "late" in by_slot["15"]
    assert m.slots["15"].units == 0 and m.closet["REDBULL-ORIGINAL-8_4CAN"] >= 23
    assert by_slot["16"]["outcome"] == "unavailable" and m.slots["16"].units == 0
    assert by_slot["17"]["units_loaded"] == 0
    assert by_slot["18"]["outcome"] == "alternate" and by_slot["18"]["store"] == "Walmart" and m.slots["18"].units > 0
    receipt = bridge.requests["r1"].lines[0]["receipt"]
    assert receipt["units_loaded"] == 0 and receipt["units_to_back_stock"] == receipt["units_bought"] - receipt["units_lost"]
