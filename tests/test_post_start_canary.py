"""A canary for the start (3 Dec 2026): a whole-machine poll goes in, and the other team's data must
not come out anywhere we show or use it (`docs/integrity.md`).

David said on 17 Nov that after the start our polls carry "the full, raw DEX response": all 40
selections, the other team's 20 too (`docs/planning/2026-11-17-david-answers.md`). This stores two
polls through the history sync's own `store`: one before the start, with the other team's
selections unsold, and one after, where every one of them is priced at a sentinel $9.87 and has
sentinel counters. The machine-wide totals (VA1) include them. Then it reads every path that shows
or uses polls and checks that no sentinel, and no machine-wide total, is in what comes back:

- the agent's MCP tool replies (`get_machine_state`, `get_sales_history`, `get_pnl`, `get_account_balance`),
- the ledger and the stock count,
- the public dashboard (`snapshot(public=True)`, its JSON, and the page and JSON as served),
- every table of the public /db explorer, as served,
- `scripts/bridge.py` printing the poll, with our slots from the stored assignment.

The local dashboard and `slot_counts.latest` still show the other team's counts on main; #146
(use_server_slots) cuts `slot_counts.latest` to our assignment. Those two are strict xfails, so
they fail loudly once #146 lands and the marks should come off.
"""

import json
from datetime import datetime, timezone

import pytest

pytest.importorskip("psycopg")

from tests.conftest import open_in  # noqa: E402
from tests.test_pages import _get, fresh, reader, site  # noqa: E402, F401 (fixtures)
from tests.test_post_start import (  # noqa: E402, F401 (script is a fixture)
    OUR_PRICE, OURS, SENTINEL_PRICE, SENTINEL_UNITS, THEIRS, leaked, machine_poll, script)
from tests.test_server import call  # noqa: E402
from vending import dashboard, db, db_explorer, ledger, server, slot_counts, stock  # noqa: E402
from vending.slots import our_slots  # noqa: E402
from vending.start import START  # noqa: E402
from vending.sync_history import store  # noqa: E402

BEFORE, AFTER = "2026-12-02T15:00:00+00:00", "2026-12-04T15:00:00+00:00"
NOW = datetime(2026, 12, 5, 15, 0, tzinfo=timezone.utc)
OURS_BEFORE, OURS_AFTER = 10, 13
POLL = machine_poll(OURS_AFTER, SENTINEL_PRICE, SENTINEL_UNITS, door=7)
# The machine-wide VA1 totals of the poll from the start: the total minus ours is theirs.
VA1_CENTS = len(OURS) * OUR_PRICE * OURS_AFTER + len(THEIRS) * SENTINEL_PRICE * SENTINEL_UNITS
VA1_UNITS = len(OURS) * OURS_AFTER + len(THEIRS) * SENTINEL_UNITS
TOTALS = (VA1_CENTS, VA1_UNITS, len(THEIRS) * SENTINEL_UNITS, len(THEIRS) * SENTINEL_PRICE * SENTINEL_UNITS)


def clean(text: str) -> None:
    found = leaked(text, *TOTALS)
    assert not found, f"the other team's data or a machine-wide total shows: {found}"


@pytest.fixture
def machine(schema_url, conn, monkeypatch):
    """The two polls stored, a ledger opening, and a photo count of one of ours and one of theirs.
    The MCP server and `vending.db.connect` use the test schema."""
    assert datetime.fromisoformat(BEFORE) < START <= datetime.fromisoformat(AFTER)
    assert set(our_slots(conn)) == set(OURS) and not set(OURS) & set(THEIRS)
    assert store(conn, {"session_id": "before", "device_id": "vmc1", "started_at": BEFORE},
                 machine_poll(OURS_BEFORE, 200, 0, door=5)) == "ok"
    assert store(conn, {"session_id": "after", "device_id": "vmc1", "started_at": AFTER}, POLL) == "ok"
    assert conn.execute("select total_paid_cents from polls where session_id = 'after'").fetchone()[0] == VA1_CENTS
    assert conn.execute("select count(*) from poll_selections where session_id = 'after'").fetchone()[0] == 40
    ledger.record(conn, "opening", 60_000, "opening capital", "canary", datetime(2026, 11, 30, tzinfo=timezone.utc))
    for selection in ("15", THEIRS[0]):
        conn.execute("insert into slot_counts (count_id, counted_at, counted_by, source, device_id, selection,"
                     " product, units, exact) values ('c1', %s, 'canary', 'photo', 'vmc1', %s, 'Drink', 5, true)",
                     ("2026-12-01T15:00:00+00:00", selection))
    monkeypatch.setattr(server, "connect", lambda: open_in(*schema_url))
    monkeypatch.setattr(server, "_conn", None)
    monkeypatch.setattr(db, "connect", lambda: open_in(*schema_url))
    yield conn
    if server._conn is not None:
        server._conn.close()


def test_the_poll_does_carry_the_sentinels(machine):
    """The canary is alive: the stored poll holds the other team's sentinels and the totals."""
    stored = str(machine.execute("select * from poll_selections").fetchall()) + bytes(
        machine.execute("select raw from polls where session_id = 'after'").fetchone()[0]).decode()
    assert leaked(stored, *TOTALS)


@pytest.mark.parametrize("tool, arguments", [
    ("get_machine_state", {}),
    ("get_sales_history", {"start": "2026-12-01T00:00:00Z", "end": "2026-12-05T00:00:00Z"}),
    ("get_sales_history", {"start": "2026-12-01T00:00:00Z", "end": "2026-12-05T00:00:00Z", "slots": ["15"]}),
    ("get_pnl", {"start": "2026-11-30", "end": "2026-12-05"}),
    ("get_account_balance", {}),
])
def test_the_agents_tools(machine, tool, arguments):
    result = call(tool, arguments)
    assert not result.isError, result
    clean(result.model_dump_json())


def test_the_agents_tools_measure_our_sales(machine):
    """The sales history does read the poll from the start: our three units each, nothing of theirs."""
    reply = call("get_sales_history", {"start": "2026-12-01T00:00:00Z", "end": "2026-12-05T00:00:00Z"})
    rows = reply.structuredContent["data"]
    assert {r["slot"] for r in rows} == set(OURS) and {r["units"] for r in rows} == {OURS_AFTER - OURS_BEFORE}


def test_the_machine_state_as_of_after_the_start(machine):
    from vending.tools import machine_state, sales_history

    state = machine_state.get_machine_state(machine, NOW)
    assert state.success and {s.slot for s in state.data} == set(OURS)
    clean(state.model_dump_json())
    history = sales_history.get_sales_history(machine, START, NOW, None)
    assert history.success
    clean(history.model_dump_json())


def test_the_ledger_and_the_stock_count(machine):
    takings = ledger.takings(machine, START, NOW)
    assert takings.cents == len(OURS) * OUR_PRICE * (OURS_AFTER - OURS_BEFORE)
    assert ledger.units_sold(machine, START, NOW) == len(OURS) * (OURS_AFTER - OURS_BEFORE)
    for found in (takings, ledger.balance(machine, NOW), ledger.pnl(machine, START, NOW),
                  ledger.pnl_by_week(machine, START, NOW), stock.stock(machine, until=NOW)):
        clean(repr(found))


def test_the_public_dashboard(machine):
    snap = dashboard.snapshot(machine, NOW, public=True)
    clean(repr(snap))
    clean(dashboard.to_json(snap))
    clean(dashboard.render(snap))


def test_the_public_pages_as_served(machine, site, schema_url):
    _, schema = schema_url
    for path in ("/dashboard", "/dashboard/state.json"):
        status, _, body = _get(site, path)
        assert status == 200, body
        clean(body)
    tables = [t.name for t in db_explorer.tables(machine) if t.schema == schema]
    assert {"polls", "poll_selections", "slot_counts", "action_log"} <= set(tables)
    for name in tables:
        status, _, body = _get(site, f"/db?schema={schema}&table={name}")
        assert status == 200, (name, body)
        clean(body)


@pytest.mark.parametrize("argv", [["latest"], ["raw", "after"], ["poll", "--live"]])
def test_the_bridge_script_with_the_stored_assignment(machine, script, capsys, monkeypatch, argv):
    script.poll = POLL
    monkeypatch.setattr(script, "our_selections", script.stored_selections)
    assert script.main(argv) == 0
    out, err = capsys.readouterr()
    clean(out)
    assert "PA1*15*250*" in out and "EA2*DO*7*107" in out and "no selection is printed" not in err


@pytest.mark.xfail(strict=True, reason="slot_counts.latest reads every selection counted; "
                                       "#146 (use_server_slots) cuts it to our assignment")
def test_slot_counts_latest(machine):
    clean(json.dumps(slot_counts.latest(machine), default=str))


@pytest.mark.xfail(strict=True, reason="the local dashboard's counts come from slot_counts.latest; "
                                       "#146 (use_server_slots) cuts it to our assignment")
def test_the_local_dashboard(machine):
    clean(dashboard.to_json(dashboard.snapshot(machine, NOW)))
