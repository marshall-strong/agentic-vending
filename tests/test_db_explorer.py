"""The DB explorer: what it shows, what it refuses, and that it can't write."""

from __future__ import annotations

import http.client
import threading
from datetime import datetime, timezone
from decimal import Decimal

import pytest

psycopg = pytest.importorskip("psycopg")

from vending import db_explorer as dx  # noqa: E402  (after the importorskip)
from vending.db_explorer import BadRequest, Column, Request, Table  # noqa: E402

TEXT = Column("note", "text", True, None, None)
BYTES = Column("blob", "bytea", True, None, None)


# --- Pure: values, URLs, escaping ---------------------------------------------------------------

def test_null_is_not_an_empty_string():
    assert "NULL" in dx.cell(None, TEXT)
    assert "NULL" not in dx.cell("", TEXT)
    assert "(empty)" in dx.cell("", TEXT)


def test_every_value_is_escaped():
    shown = dx.cell("<script>alert(1)</script>", TEXT)
    assert "<script>" not in shown and "&lt;script&gt;" in shown
    shown = dx.cell({"x": "<b>"}, Column("doc", "jsonb", True, None, None))
    assert "<b>" not in shown and "&lt;b&gt;" in shown


def test_bytea_shows_only_its_size():
    assert dx.cell(1234, BYTES) == '<span class="meta">1,234 bytes</span>'


def test_long_text_is_cut_with_the_rest_one_click_away():
    long = "a" * (dx.CUT + 50)
    shown = dx.cell(long, TEXT)
    assert shown.startswith("<details>") and long in shown


def test_a_huge_value_says_how_much_was_left_out():
    huge = "b" * (dx.SHOW_AT_MOST + 10)
    shown = dx.cell(huge, TEXT)
    assert f"cut at {dx.SHOW_AT_MOST:,} characters" in shown
    assert "b" * (dx.SHOW_AT_MOST + 1) not in shown


def test_timestamps_show_eastern_with_utc_on_hover():
    shown = dx.cell(datetime(2026, 11, 11, 17, 30, tzinfo=timezone.utc), Column("at", "timestamptz", False, None, None))
    assert "12:30:00 EST" in shown and 'title="2026-11-11 17:30:00+00:00"' in shown


def test_numbers_stay_exact():
    assert dx.cell(Decimal("2.50"), Column("n", "numeric", False, None, None)) == "2.50"


@pytest.mark.parametrize("target, expected", [
    ("/", Request(None, None, 0)),
    ("/?schema=public&table=polls", Request("public", "polls", 0)),
    ("/?schema=public&table=polls&offset=200", Request("public", "polls", 200)),
    ("/?schema=public&table=a%20b%3Bdrop", Request("public", "a b;drop", 0)),
])
def test_parse_reads_the_request(target, expected):
    assert dx.parse(target) == expected


@pytest.mark.parametrize("target", [
    "/?table=polls",                                   # no schema
    "/?schema=public&table=polls&offset=-1",
    "/?schema=public&table=polls&offset=1e3",
    "/?schema=public&table=polls&offset=²",            # a digit to str.isdigit, not to int
    f"/?schema=public&table=polls&offset={dx.MAX_OFFSET + 1}",
    "/?schema=public&table=polls&table=x",             # a parameter twice
    "/?schema=public&table=polls&sql=select",          # unknown parameter
    "/?offset=100",                                    # offset with no table
])
def test_parse_refuses_malformed_requests(target):
    with pytest.raises(BadRequest):
        dx.parse(target)


def test_parse_404s_other_paths():
    with pytest.raises(LookupError):
        dx.parse("/state.json")


def test_page_escapes_table_names():
    odd = Table("public", "<img src=x>", "table")
    html = dx.page([odd], odd, "")
    assert "<img src=x>" not in html and "&lt;img src=x&gt;" in html


# --- Against the local test database ------------------------------------------------------------

@pytest.fixture
def explorer(schema_url):
    """A connection the way the explorer opens one: read-only, with the statement timeout."""
    url, schema = schema_url
    with psycopg.connect(url, autocommit=True, options=dx.OPTIONS) as conn:
        yield conn, schema


def _ours(conn, schema):
    return {t.name: t for t in dx.tables(conn) if t.schema == schema}


def test_lists_every_migrated_table_and_no_system_schemas(explorer):
    conn, schema = explorer
    found = _ours(conn, schema)
    assert {"polls", "poll_selections", "config", "action_log", "purchase_requests", "ledger_entries"} <= set(found)
    assert not [t for t in dx.tables(conn) if t.schema in dx.SYSTEM_SCHEMAS or t.schema.startswith("pg_")]


def test_columns_carry_type_null_default_and_primary_key(explorer):
    conn, schema = explorer
    cols = {c.name: c for c in dx.columns(conn, Table(schema, "config", "table"))}
    assert cols["key"].primary_key == 1 and cols["key"].type == "text" and not cols["key"].nullable
    assert cols["value"].type == "jsonb" and cols["value"].primary_key is None
    assert cols["updated_at"].default == "now()"


def test_pages_newest_first_by_primary_key(conn, explorer):
    ro, schema = explorer
    for i in range(150):
        conn.execute("insert into action_log (kind, tool, arguments, outcome) values ('read', %s, '{}', 'done')",
                     (f"tool{i}",))
    log = Table(schema, "action_log", "table")
    first = dx.rows(ro, log)
    assert first.total == 150 and len(first.rows) == dx.PAGE_ROWS and first.ordered_by == ("id",)
    ids = [r[0] for r in first.rows]
    assert ids == sorted(ids, reverse=True) and ids[0] == max(ids)
    second = dx.rows(ro, log, offset=100)
    assert len(second.rows) == 50 and not set(ids) & {r[0] for r in second.rows}


def test_the_session_refuses_writes(explorer):
    conn, schema = explorer
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        conn.execute(f"insert into {schema}.config (key, value) values ('x', 'true')")


def test_an_unlisted_table_is_a_404_and_never_a_query(explorer):
    conn, schema = explorer
    code, html = dx.render(conn, Request(schema, "config; drop table config", 0))
    assert code == 404 and "No table" in html
    assert "config" in _ours(conn, schema)


def test_odd_values_render(conn, explorer):
    conn.execute("create table oddities (id serial primary key, blob bytea, doc jsonb, note text,"
                 " plain timestamp, amount numeric)")
    conn.execute("insert into oddities (blob, doc, note, plain, amount)"
                 " values ('\\x010203', '{\"price\": 2.50}', '<b>hi</b>', '2026-11-11 09:00', 3.10),"
                 " (null, null, '', null, null)")
    ro, schema = explorer
    code, html = dx.render(ro, Request(schema, "oddities", 0))
    assert code == 200
    assert "3 bytes" in html and "NULL" in html and "(empty)" in html
    assert "&lt;b&gt;hi&lt;/b&gt;" in html and "<b>hi</b>" not in html
    assert "price&quot;: 2.50" in html   # json numbers stay exact, not 2.5
    assert "3.10" in html


def test_a_view_and_a_table_without_a_key(conn, explorer):
    conn.execute("create table loose (n int)")
    conn.execute("insert into loose values (1), (2)")
    conn.execute("create view loose_view as select n from loose")
    ro, schema = explorer
    found = _ours(ro, schema)
    assert found["loose_view"].kind == "view" and found["loose"].kind == "table"
    code, html = dx.render(ro, Request(schema, "loose", 0))
    assert code == 200 and "no primary key" in html and "of 2," in html
    assert dx.render(ro, Request(schema, "loose_view", 0))[0] == 200


def test_a_slow_table_is_stopped(conn, schema_url):
    conn.execute("create view slow as select 1 as x from pg_sleep(2)")
    url, schema = schema_url
    with psycopg.connect(url, autocommit=True,
                         options="-c default_transaction_read_only=on -c statement_timeout=200") as ro:
        code, html = dx.render(ro, Request(schema, "slow", 0))
    assert code == 503 and "was stopped" in html


# --- The HTTP handler ---------------------------------------------------------------------------

@pytest.fixture
def server(schema_url, monkeypatch):
    url, _ = schema_url
    monkeypatch.setattr(dx, "connect", lambda: psycopg.connect(url, autocommit=True, options=dx.OPTIONS))
    httpd = dx.ThreadingHTTPServer(("127.0.0.1", 0), dx.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def _get(port, path, host="127.0.0.1"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("GET", path, headers={"Host": host})
    r = c.getresponse()
    body = r.read().decode("utf-8")
    c.close()
    return r.status, body


def test_handler_serves_the_index_and_a_table(server, schema_url):
    _, schema = schema_url
    code, body = _get(server, "/")
    assert code == 200 and "action_log" in body and "Choose a table" in body
    code, body = _get(server, f"/?schema={schema}&table=config")
    assert code == 200 and "halted" in body


def test_handler_refuses_other_hosts_bad_requests_and_other_paths(server):
    assert _get(server, "/", host="evil.example")[0] == 403
    assert _get(server, "/?schema=x&table=y&offset=abc")[0] == 400
    assert _get(server, "/nope")[0] == 404


# --- Regressions from the blind review ----------------------------------------------------------

def test_the_explorer_connects_read_only_with_the_timeout(monkeypatch):
    assert "default_transaction_read_only=on" in dx.OPTIONS and "statement_timeout=5000" in dx.OPTIONS
    seen = {}
    monkeypatch.setattr(dx.settings, "require", lambda *a, **k: "postgresql://x@127.0.0.1/x")
    monkeypatch.setattr(dx.psycopg, "connect", lambda url, **kw: seen.update(kw) or "conn")
    assert dx._connect() == "conn" and seen["options"] == dx.OPTIONS


def test_paging_goes_on_past_the_count_cap():
    log = Table("public", "big", "table")
    full = dx.Rows((TEXT,), tuple(("x",) for _ in range(dx.PAGE_ROWS)), dx.COUNT_CAP, dx.COUNT_CAP + 1, ("id",))
    assert "Next →" in dx._rows_html(log, full)
    last = dx.Rows((TEXT,), (("x",),), dx.COUNT_CAP, dx.COUNT_CAP + 1, ("id",))
    assert "Next →" not in dx._rows_html(log, last)


def test_dates_python_cannot_hold_still_render(conn, explorer):
    conn.execute("set timezone = 'UTC'")
    conn.execute("create table odd_times (id int primary key, at timestamptz, plain timestamp, day date, span interval)")
    conn.execute("insert into odd_times values"
                 " (1, '0001-01-01 00:00:00+00', 'infinity', '-infinity', '100000000 years'),"
                 " (2, 'infinity', '2026-11-11 09:00', '2026-11-11', '1 day')")
    ro, schema = explorer
    code, html = dx.render(ro, Request(schema, "odd_times", 0))
    assert code == 200
    assert "infinity" in html and "-infinity" in html and "100000000 years" in html
    assert "0001-01-01" in html


def test_include_columns_are_not_part_of_the_key(conn, explorer):
    conn.execute("create table covered (id int, p point, note text, primary key (id) include (p, note))")
    conn.execute("insert into covered values (1, '(1,2)', 'a'), (2, '(3,4)', 'b')")
    ro, schema = explorer
    cols = {c.name: c for c in dx.columns(ro, Table(schema, "covered", "table"))}
    assert cols["id"].primary_key == 1 and cols["p"].primary_key is None and cols["note"].primary_key is None
    page_rows = dx.rows(ro, Table(schema, "covered", "table"))
    assert page_rows.ordered_by == ("id",) and [r[0] for r in page_rows.rows] == [2, 1]


def test_order_is_by_the_table_columns_not_the_shown_text(conn, explorer):
    conn.execute("create table json_keyed (doc jsonb primary key)")
    conn.execute("insert into json_keyed values ('2'), ('3'), ('10')")
    conn.execute("create table awkward (octet_length int primary key, data bytea)")
    conn.execute("insert into awkward values (1, decode('00', 'hex')), (2, decode('0000', 'hex'))")
    ro, schema = explorer
    assert [r[0] for r in dx.rows(ro, Table(schema, "json_keyed", "table")).rows] == ["10", "3", "2"]
    assert dx.rows(ro, Table(schema, "awkward", "table")).rows == ((2, 2), (1, 1))


def test_huge_text_is_cut_in_postgres(conn, explorer):
    conn.execute("create table wordy (id int primary key, note text, tags text[])")
    conn.execute("insert into wordy values (1, repeat('w', %s), array['a', 'b'])", (dx.SHOW_AT_MOST + 5000,))
    ro, schema = explorer
    page_rows = dx.rows(ro, Table(schema, "wordy", "table"))
    assert len(page_rows.rows[0][1]) == dx.SHOW_AT_MOST + 1
    assert page_rows.rows[0][2] == "{a,b}"
    assert "cut at" in dx.render(ro, Request(schema, "wordy", 0))[1]


def test_an_unforeseen_error_still_gets_an_answer(server, monkeypatch, schema_url):
    _, schema = schema_url
    monkeypatch.setattr(dx, "render", lambda conn, request: 1 / 0)
    code, body = _get(server, f"/?schema={schema}&table=config")
    assert code == 500 and "ZeroDivisionError" not in body
