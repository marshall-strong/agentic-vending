"""A canary for the start (3 Dec 2026): a whole-machine poll goes in, and the other team's data must
not come out anywhere we show or use it (`docs/integrity.md`).

David said on 17 Nov that after the start our polls carry "the full, raw DEX response": all 40
selections, the other team's 20 too (`docs/planning/2026-11-17-david-answers.md`, PR #164). This
stores two polls through the history sync's own `store`: one before the start, with the other team's
selections unsold, and one after, where every one of them is priced at a sentinel $9.87 and has
sentinel counters. The machine-wide totals (VA1) include them. A fulfilled receipt for one of ours
makes the stock count read the counters. The clock of every served path is frozen after the start.
Then it reads every path that shows or uses polls and checks two things: no sentinel and no
machine-wide total is in what comes back, and the rows it shows are exactly ours, with our values:

- the agent's MCP tool replies (`get_machine_state`, `get_sales_history`, `get_pnl`, `get_account_balance`),
- the ledger and the stock count,
- the public dashboard (`snapshot(public=True)`, its JSON, and the page and JSON as served),
- every table of the explorer, as served at /db and by `python -m vending.db_explorer`,
- `scripts/bridge.py` printing the poll, and `latest --summary`, stdout and stderr, with our slots
  from the stored assignment.

It runs once for each assignment in `tests.test_post_start.ASSIGNMENTS`: four in each row, like a
redraw could give us. #146 (use_server_slots) made every one of them work and cut
`slot_counts.latest` and the local dashboard's counts to our assignment, so every run is a plain
positive test.
"""

import html
import http.client
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

psycopg = pytest.importorskip("psycopg")

from tests.conftest import open_in  # noqa: E402
from tests.test_pages import _get, fresh, reader, site  # noqa: E402, F401 (fixtures)
from tests.test_post_start import (  # noqa: E402, F401 (script is a fixture)
    ASSIGNMENTS, OUR_PRICE, SENTINEL_PRICE, SENTINEL_UNITS, leaked, machine_poll, others, script)
from tests.test_server import call  # noqa: E402
from vending import dashboard, db, db_explorer, ledger, server, slot_counts, stock  # noqa: E402
from vending.slots import our_slots  # noqa: E402
from vending.start import START  # noqa: E402
from vending.sync_history import store  # noqa: E402
from vending.tools import catalog, machine_state, pnl, purchase_requests  # noqa: E402

BEFORE, AFTER = "2026-12-02T15:00:00+00:00", "2026-12-04T15:00:00+00:00"
NOW = datetime(2026, 12, 5, 15, 0, tzinfo=timezone.utc)
RECEIPT_AT = datetime(2026, 12, 1, 15, 0, tzinfo=timezone.utc)
OURS_BEFORE, OURS_AFTER = 10, 13
SOLD = OURS_AFTER - OURS_BEFORE   # each of ours, between the two polls
LOADED = 20                        # the receipt's units, in our first selection
N = 20   # selections per team in every assignment
# The machine-wide VA1 totals of the poll from the start: the total minus ours is theirs.
VA1_CENTS = N * OUR_PRICE * OURS_AFTER + N * SENTINEL_PRICE * SENTINEL_UNITS
VA1_UNITS = N * OURS_AFTER + N * SENTINEL_UNITS
TOTALS = (VA1_CENTS, VA1_UNITS, N * SENTINEL_UNITS, N * SENTINEL_PRICE * SENTINEL_UNITS)


class Leak(AssertionError):
    """The other team's data or a machine-wide total shows."""


@pytest.fixture(params=[pytest.param(ours, id=name) for name, ours in ASSIGNMENTS.items()])
def assignment(request) -> tuple[str, ...]:
    """Our selections in this test, stored as the server's assignment by `machine`."""
    assert len(request.param) == N
    return request.param


def clean(text: str) -> None:
    found = leaked(text, *TOTALS)
    if found:
        raise Leak(f"the other team's data or a machine-wide total shows: {found}")


_REAL = datetime


class _AnyDatetime(type):
    """So `isinstance(value, datetime)` in a module whose `datetime` is `Frozen` still sees every datetime."""

    def __instancecheck__(cls, value):
        return isinstance(value, _REAL)

    def __subclasscheck__(cls, sub):
        return issubclass(sub, _REAL)


class Frozen(datetime, metaclass=_AnyDatetime):
    """`datetime` with its clock stopped at NOW, after the start."""

    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)

    @classmethod
    def utcnow(cls):
        return NOW.replace(tzinfo=None)


def receipt(conn, request_id: str, slot: str, units: int, at: datetime) -> None:
    """A fulfilled purchase request loading `units` into `slot`, as the requests sync stores it."""
    conn.execute("insert into purchase_requests (request_id, submitted_at, off_cycle, request_body, status,"
                 " total_paid, fulfilled_at) values (%s, %s, false, '{}', 'fulfilled', %s, %s)",
                 (request_id, at - timedelta(days=1), Decimal("12.00"), at))
    conn.execute(
        "insert into purchase_lines (request_id, line_no, slot, source, asked, outcome, sku, store, packs_bought,"
        " units_per_pack, units_bought, total_paid, unit_cost_paid, units_loaded, units_lost,"
        " units_to_back_stock, units_from_back_stock, units_pulled, slot_reassigned, resolved_at)"
        " values (%s, 0, %s, 'purchase', '{}', 'primary', 'OUR-SKU', 'Walmart', 1, %s, %s, %s, null, %s, 0, 0, 0, 0,"
        " false, %s)",
        (request_id, slot, units, units, Decimal("12.00"), units, at))


@pytest.fixture
def machine(schema_url, conn, monkeypatch, assignment):
    """`assignment` stored as ours, the two polls stored, a ledger opening, a fulfilled receipt for
    our first selection, and a photo count of one of ours and one of theirs. The MCP server and
    `vending.db.connect` use the test schema, and every served path's clock reads NOW."""
    assert datetime.fromisoformat(BEFORE) < START <= datetime.fromisoformat(AFTER) < NOW
    conn.execute("update slot_assignment set slots = %s where id = 1", (list(assignment),))
    assert our_slots(conn) == assignment
    assert store(conn, {"session_id": "before", "device_id": "vmc1", "started_at": BEFORE},
                 machine_poll(assignment, OURS_BEFORE, 200, 0, door=5)) == "ok"
    assert store(conn, {"session_id": "after", "device_id": "vmc1", "started_at": AFTER},
                 machine_poll(assignment, OURS_AFTER, SENTINEL_PRICE, SENTINEL_UNITS, door=7)) == "ok"
    ledger.record(conn, "opening", 60_000, "opening capital", "canary", datetime(2026, 11, 30, tzinfo=timezone.utc))
    receipt(conn, "r1", assignment[0], LOADED, RECEIPT_AT)
    for selection in (assignment[0], others(assignment)[0]):
        conn.execute("insert into slot_counts (count_id, counted_at, counted_by, source, device_id, selection,"
                     " product, units, exact) values ('c1', %s, 'canary', 'photo', 'vmc1', %s, 'Drink', 5, true)",
                     ("2026-12-01T15:00:00+00:00", selection))
    for module in (dashboard, machine_state, pnl, purchase_requests, catalog):
        monkeypatch.setattr(module, "datetime", Frozen)
    monkeypatch.setattr(server, "connect", lambda: open_in(*schema_url))
    monkeypatch.setattr(server, "_conn", None)
    monkeypatch.setattr(db, "connect", lambda: open_in(*schema_url))
    yield conn
    if server._conn is not None:
        server._conn.close()


def test_the_poll_does_carry_the_sentinels(machine, assignment):
    """The canary is alive: the history sync parsed the sentinels into the stored rows, not just
    the raw bytes, so a path that read them would show them."""
    stored = {s: (price, units, cents) for s, price, units, cents in machine.execute(
        "select selection, price_cents, lifetime_units, lifetime_sales_cents from poll_selections"
        " where session_id = 'after'")}
    assert set(stored) == set(assignment) | set(others(assignment)) and len(stored) == 2 * N
    assert {stored[s] for s in others(assignment)} == {(SENTINEL_PRICE, SENTINEL_UNITS, SENTINEL_PRICE * SENTINEL_UNITS)}
    assert {stored[s] for s in assignment} == {(OUR_PRICE, OURS_AFTER, OUR_PRICE * OURS_AFTER)}
    assert machine.execute("select total_paid_cents, total_paid_units from polls where session_id = 'after'"
                           ).fetchone() == (VA1_CENTS, VA1_UNITS)
    with pytest.raises(Leak):
        clean(str(sorted(stored.items())))


@pytest.mark.parametrize("tool, arguments, rows", [
    ("get_machine_state", {}, "ours"),
    ("get_sales_history", {"start": "2026-12-01T00:00:00Z", "end": "2026-12-05T00:00:00Z"}, "ours"),
    ("get_sales_history", {"start": "2026-12-01T00:00:00Z", "end": "2026-12-05T00:00:00Z", "slots": "first"},
     "first"),
    ("get_pnl", {"start": "2026-11-30", "end": "2026-12-05"}, None),
    ("get_account_balance", {}, None),
])
def test_the_agents_tools(machine, assignment, tool, arguments, rows):
    if arguments.get("slots") == "first":   # one of ours, whichever the assignment is
        arguments = {**arguments, "slots": [assignment[0]]}
    result = call(tool, arguments)
    assert not result.isError, result
    clean(result.model_dump_json())
    if rows is not None:
        expected = set(assignment) if rows == "ours" else {assignment[0]}
        assert {r["slot"] for r in result.structuredContent["data"]} == expected


def test_the_agents_tools_measure_our_sales(machine, assignment):
    """The sales history does read the poll from the start: our three units each, nothing of theirs."""
    reply = call("get_sales_history", {"start": "2026-12-01T00:00:00Z", "end": "2026-12-05T00:00:00Z"})
    rows = reply.structuredContent["data"]
    assert {r["slot"] for r in rows} == set(assignment) and {r["units"] for r in rows} == {SOLD}


def test_the_machine_state_as_of_after_the_start(machine, assignment):
    from vending.tools import sales_history

    state = machine_state.get_machine_state(machine, NOW)
    assert state.success and {s.slot for s in state.data} == set(assignment)
    clean(state.model_dump_json())
    history = sales_history.get_sales_history(machine, START, NOW, None)
    assert history.success
    clean(history.model_dump_json())


def test_the_ledger_and_the_stock_count(machine, assignment):
    takings = ledger.takings(machine, START, NOW)
    assert takings.cents == N * OUR_PRICE * SOLD
    assert ledger.units_sold(machine, START, NOW) == N * SOLD
    counted = stock.stock(machine, until=NOW)
    # The receipt names our first selection, so the count reads its counters: three sold of twenty.
    assert [(s.slot, s.loaded, s.sold, s.units) for s in counted.slots] == [(assignment[0], LOADED, SOLD, LOADED - SOLD)]
    for found in (takings, ledger.balance(machine, NOW), ledger.pnl(machine, START, NOW),
                  ledger.pnl_by_week(machine, START, NOW), counted):
        clean(repr(found))


def test_the_public_dashboard(machine):
    snap = dashboard.snapshot(machine, NOW, public=True)
    clean(repr(snap))
    clean(dashboard.to_json(snap))
    clean(dashboard.render(snap))


# --- The explorer, as served ----------------------------------------------------------------------

def served_rows(page: str) -> list[dict[str, str]]:
    """The rows of an explorer page, by column name, as the page shows them."""
    found = re.search(r'<table class="rows"><thead><tr>(.*?)</tr></thead><tbody>(.*?)</tbody>', page, re.S)
    if found is None:
        return []
    names = [html.unescape(name) for name in re.findall(r"<th[^>]*>(.*?)</th>", found.group(1))]
    return [dict(zip(names, (html.unescape(re.sub(r"<[^>]+>", "", cell))
                             for cell in re.findall(r"<td>(.*?)</td>", row, re.S))))
            for row in re.findall(r"<tr>(.*?)</tr>", found.group(2), re.S)]


def explorer_shows_only_ours(get, schema: str, tables: list[str], assignment: tuple[str, ...]) -> None:
    """Every table as `get(query)` serves it is clean, and the selection tables show exactly our rows."""
    assert {"polls", "poll_selections", "slot_counts", "action_log"} <= set(tables)
    pages = {}
    for name in tables:
        status, body = get(f"?schema={schema}&table={name}")
        assert status == 200, (name, body)
        clean(body)
        pages[name] = body
    selections = served_rows(pages["poll_selections"])
    assert {(r["session_id"], r["selection"]) for r in selections} == {
        (session, s) for session in ("before", "after") for s in assignment}
    assert {(r["price_cents"], r["lifetime_units"], r["lifetime_sales_cents"])
            for r in selections if r["session_id"] == "after"} == {(str(OUR_PRICE), str(OURS_AFTER),
                                                                    str(OUR_PRICE * OURS_AFTER))}
    assert {(r["count_id"], r["selection"]) for r in served_rows(pages["slot_counts"])} == {("c1", assignment[0])}
    polls = served_rows(pages["polls"])
    assert {r["session_id"] for r in polls} == {"before", "after"}
    assert {(r["total_paid_cents"], r["total_paid_units"]) for r in polls} == {("(hidden)", "(hidden)")}


def test_the_public_pages_as_served(machine, assignment, site, schema_url):
    _, schema = schema_url
    for path in ("/dashboard", "/dashboard/state.json"):
        status, _, body = _get(site, path)
        assert status == 200, body
        clean(body)
    state = json.loads(_get(site, "/dashboard/state.json")[2])
    # The frozen clock is after the start, so the dashboard does read the poll from it.
    assert set(state["counts"]["data"]) == {assignment[0]}
    tables = [t.name for t in db_explorer.tables(machine) if t.schema == schema]
    explorer_shows_only_ours(lambda query: _get(site, "/db" + query)[::2], schema, tables, assignment)


@pytest.fixture
def standalone(schema_url, monkeypatch):
    """`python -m vending.db_explorer`'s own server, on the test schema, as it connects."""
    url, schema = schema_url
    monkeypatch.setattr(db_explorer, "connect", lambda: psycopg.connect(
        url, autocommit=True, options=db.session_options(url, f"{db_explorer.OPTIONS} -c search_path={schema}")))
    httpd = db_explorer.ThreadingHTTPServer(("127.0.0.1", 0), db_explorer.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def get(query: str) -> tuple[int, str]:
        connection = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=20)
        connection.request("GET", "/" + query, headers={"Host": "127.0.0.1"})
        reply = connection.getresponse()
        body = reply.read().decode("utf-8")
        connection.close()
        return reply.status, body

    yield get
    httpd.shutdown()
    httpd.server_close()


def test_the_standalone_explorer_as_served(machine, assignment, standalone, schema_url):
    _, schema = schema_url
    tables = [t.name for t in db_explorer.tables(machine) if t.schema == schema]
    explorer_shows_only_ours(standalone, schema, tables, assignment)


def test_the_standalone_explorer_with_no_assignment_shows_no_selection(machine, standalone, schema_url):
    _, schema = schema_url
    machine.execute("delete from slot_assignment")
    for name in ("poll_selections", "slot_counts"):
        status, body = standalone(f"?schema={schema}&table={name}")
        assert status == 200 and served_rows(body) == [] and "No rows." in body
        clean(body)


# --- scripts/bridge.py ----------------------------------------------------------------------------

@pytest.mark.parametrize("argv", [["latest"], ["raw", "after"], ["poll", "--live"]])
def test_the_bridge_script_with_the_stored_assignment(machine, assignment, script, capsys, monkeypatch, argv):
    script.poll = bytes(machine.execute("select raw from polls where session_id = 'after'").fetchone()[0])
    monkeypatch.setattr(script, "our_selections", script.stored_selections)
    monkeypatch.setattr(script, "stored_poll_time", script.real_stored_poll_time)   # `raw` reads the polls table
    assert script.main(argv) == 0
    out, err = capsys.readouterr()
    clean(out)
    clean(err)
    lines = out.splitlines()
    assert {line.split("*")[1] for line in lines if line.startswith("PA1*")} == set(assignment)
    assert set(lines) == ({"ID4*2", "EA2*DO*7*107", f"PA2*{OURS_AFTER}*{OUR_PRICE * OURS_AFTER}*0*0*0*0"}
                          | {f"PA1*{s}*{OUR_PRICE}" for s in assignment})
    assert "no selection is printed" not in err


def test_the_bridge_summary_with_the_stored_assignment(machine, assignment, script, capsys, monkeypatch):
    script.poll = bytes(machine.execute("select raw from polls where session_id = 'after'").fetchone()[0])
    monkeypatch.setattr(script, "our_selections", script.stored_selections)
    assert script.main(["latest", "--summary"]) == 0
    out, err = capsys.readouterr()
    clean(out)
    clean(err)
    rows = json.loads(out)
    assert [r["slot"] for r in rows] == list(assignment)
    assert {(r["price"], r["lifetime_units"], r["lifetime_sales"]) for r in rows} == {("$2.50", OURS_AFTER, "$32.50")}
    assert "/api/v1/slots" not in script.calls


@pytest.mark.parametrize("session", ["before", "after", "not-stored"])
def test_the_bridge_script_places_raw_by_the_polls_table(machine, assignment, script, capsys, monkeypatch, session):
    """Both stored polls are from the fall cut on (2 and 4 Dec), and one not stored has no known time:
    each prints only ours, though the client's clock says 1 Nov."""
    script.poll = bytes(machine.execute("select raw from polls where session_id = 'after'").fetchone()[0])
    script.now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(script, "our_selections", script.stored_selections)
    monkeypatch.setattr(script, "stored_poll_time", script.real_stored_poll_time)
    assert script.main(["raw", session]) == 0
    out, err = capsys.readouterr()
    clean(out)
    clean(err)
    assert not any(line.startswith("VA1") for line in out.splitlines())
    assert ("unknown" in err) == (session == "not-stored")


# --- Fixed by #146 --------------------------------------------------------------------------------
# #146 cut these to our assignment: our count is there, the other team's is not.

def test_slot_counts_latest(machine, assignment):
    latest = slot_counts.latest(machine)
    clean(json.dumps(latest, default=str))
    assert set(latest) == {assignment[0]} and others(assignment)[0] not in latest


def test_the_local_dashboard(machine, assignment):
    snap = dashboard.snapshot(machine, NOW)
    clean(dashboard.to_json(snap))
    assert set(snap["counts"]["data"]) == {assignment[0]} and others(assignment)[0] not in snap["counts"]["data"]
