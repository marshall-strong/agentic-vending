"""A read-only web page for looking at what is in the database: every table, its columns and its rows.

    python -m vending.db_explorer              # http://127.0.0.1:8766
    python -m vending.db_explorer --port 9000

When the page opens it lists every schema and table. Choose a table to see its
columns (type, whether it can be null, default, primary key) and its rows, 100
at a time, highest primary key first (for a numeric id, the newest).

It knows nothing about vending: no pricing, stock, ledger or planner code, only
what Postgres says is there. It reads the database DATABASE_URL points at and
never writes: the session is read-only, so Postgres refuses any write, every
page is one read-only transaction, and a slow query is cancelled after
STATEMENT_TIMEOUT_MS. It never migrates.

It listens on 127.0.0.1 and answers only a Host of 127.0.0.1 or localhost, the
same rule as the agent dashboard, so another site can't rebind its name to us.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import psycopg
from psycopg import sql

from vending import db, settings

HOST = "127.0.0.1"
PORT = 8766
PAGE_ROWS = 100
# Rows past this are counted as "more than"; counting a huge table exactly would be slow.
COUNT_CAP = 100_000
# The furthest a page can start, so a typed URL can't ask Postgres to skip a billion rows.
MAX_OFFSET = 1_000_000
# Characters of a value shown in the grid; the rest is one click away.
CUT = 200
# Characters of a value shown at all. A bigger value says how much was left out.
SHOW_AT_MOST = 20_000
STATEMENT_TIMEOUT_MS = 5_000
# Every transaction on this connection is read-only, and no statement runs longer than the timeout.
OPTIONS = f"-c default_transaction_read_only=on -c statement_timeout={STATEMENT_TIMEOUT_MS}"
# Host headers the server answers, so another site can't rebind its name to us and read the page.
LOCAL_HOSTS = ("127.0.0.1", "localhost")
# Postgres's own schemas. `pg_toast` and the per-session `pg_temp_N` schemas are matched by prefix.
SYSTEM_SCHEMAS = ("pg_catalog", "information_schema")
try:
    from zoneinfo import ZoneInfo
    EASTERN: Any = ZoneInfo("America/New_York")
except Exception:  # no IANA database: show UTC rather than fail the page
    EASTERN = None


def _connect() -> psycopg.Connection:
    url = settings.require("DATABASE_URL", "Copy the DATABASE_URL line from .env.example into .env.")
    return psycopg.connect(url, autocommit=True, connect_timeout=db.CONNECT_TIMEOUT, options=OPTIONS)


# Tests swap this to point at the local test database.
connect: Callable[[], psycopg.Connection] = _connect


# --- Data ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Table:
    schema: str
    name: str
    kind: str          # "table" or "view"


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    nullable: bool
    default: str | None
    primary_key: int | None   # position in the primary key, from 1; None when not part of it


@dataclass(frozen=True)
class Rows:
    columns: tuple[Column, ...]
    rows: tuple[tuple, ...]
    offset: int
    total: int                # capped at COUNT_CAP + 1, which means "more than COUNT_CAP"
    ordered_by: tuple[str, ...]


def tables(conn: psycopg.Connection) -> list[Table]:
    """Every table and view outside Postgres's own schemas, `public` first, then by name."""
    found = conn.execute(
        "select table_schema, table_name, table_type from information_schema.tables"
        " where table_schema <> all(%s) and table_schema not like 'pg\\_toast%%'"
        " and table_schema not like 'pg\\_temp\\_%%'"
        " order by table_schema <> 'public', table_schema, table_name",
        (list(SYSTEM_SCHEMAS),),
    ).fetchall()
    return [Table(schema, name, "view" if kind == "VIEW" else "table") for schema, name, kind in found]


def columns(conn: psycopg.Connection, table: Table) -> tuple[Column, ...]:
    found = conn.execute(
        "select a.attname, format_type(a.atttypid, a.atttypmod), not a.attnotnull,"
        # `indkey` is an int2vector, numbered from 0; `with ordinality` numbers the key from 1.
        # Past `indnkeyatts` come INCLUDE columns, which are stored in the index but aren't the key.
        " pg_get_expr(d.adbin, d.adrelid),"
        " (select k.n from unnest(i.indkey) with ordinality k(att, n)"
        "  where k.att = a.attnum and k.n <= i.indnkeyatts)"
        " from pg_attribute a"
        " join pg_class c on c.oid = a.attrelid"
        " join pg_namespace n on n.oid = c.relnamespace"
        " left join pg_attrdef d on d.adrelid = a.attrelid and d.adnum = a.attnum"
        " left join pg_index i on i.indrelid = c.oid and i.indisprimary"
        " where n.nspname = %s and c.relname = %s and a.attnum > 0 and not a.attisdropped"
        " order by a.attnum",
        (table.schema, table.name),
    ).fetchall()
    return tuple(Column(name, kind, nullable, default, pk) for name, kind, nullable, default, pk in found)


# Types fetched as Python values. Everything else comes as text, cut in Postgres, so a 1 GB value
# never crosses the wire.
NATIVE_TYPES = ("boolean", "smallint", "integer", "bigint", "real", "double precision", "uuid", "date",
                "interval")
# `time` isn't here: Postgres allows 24:00:00, which Python's `time` can't hold, and text shows it fine.
NATIVE_PREFIXES = ("numeric", "timestamp")
ALIAS = "t"   # the table's alias in the row query, so ORDER BY means its columns, not ours


def _native(column: Column) -> bool:
    return column.type in NATIVE_TYPES or column.type.startswith(NATIVE_PREFIXES)


def _pick(column: Column) -> sql.Composable:
    name = sql.Identifier(ALIAS, column.name)
    if column.type == "bytea":
        # A bytea value can be megabytes and means nothing on a page: fetch only its size.
        return sql.SQL("octet_length({})").format(name)
    if _native(column):
        return name
    # Text as Postgres writes it (so a JSON price stored as 2.50 stays 2.50), and at most one
    # character past what the page shows, so the page can say it was cut.
    return sql.SQL("left({}::text, {})").format(name, sql.Literal(SHOW_AT_MOST + 1))


class _Lenient:
    """A date/time loader that falls back to Postgres's own text for values Python can't hold:
    'infinity', years past 9999, intervals over `timedelta`'s range."""

    def load(self, data: Any) -> Any:
        try:
            return super().load(data)   # type: ignore[misc]
        except (psycopg.DataError, ValueError, OverflowError):
            return bytes(data).decode("utf-8", "replace")


def _lenient_dates(conn: psycopg.Connection) -> None:
    from psycopg.types import datetime as dt

    for name, base in (("date", dt.DateLoader), ("timestamp", dt.TimestampLoader),
                       ("timestamptz", dt.TimestamptzLoader), ("interval", dt.IntervalLoader)):
        conn.adapters.register_loader(name, type(f"Lenient{base.__name__}", (_Lenient, base), {}))


def _snapshot(conn: psycopg.Connection):
    """One repeatable-read, read-only transaction, unless the caller already opened one."""
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        return nullcontext()
    return _ReadOnlyTransaction(conn)


class _ReadOnlyTransaction:
    def __init__(self, conn: psycopg.Connection):
        self.tx = conn.transaction()
        self.conn = conn

    def __enter__(self):
        self.tx.__enter__()
        self.conn.execute("set transaction isolation level repeatable read, read only")
        return self

    def __exit__(self, *exc):
        return self.tx.__exit__(*exc)


def rows(conn: psycopg.Connection, table: Table, offset: int = 0) -> Rows:
    """One page of rows, in descending primary-key order when there is a key, from one moment."""
    with _snapshot(conn):
        cols = columns(conn, table)
        key = sorted((c for c in cols if c.primary_key), key=lambda c: c.primary_key)
        target = sql.SQL("{} as {}").format(sql.Identifier(table.schema, table.name), sql.Identifier(ALIAS))
        picked = sql.SQL(", ").join(_pick(c) for c in cols) if cols else sql.SQL("null")
        order = (sql.SQL(" order by ") + sql.SQL(", ").join(
            sql.SQL("{} desc").format(sql.Identifier(ALIAS, c.name)) for c in key)) if key else sql.SQL("")
        total = conn.execute(
            sql.SQL("select count(*) from (select 1 from {} limit %s) s").format(target), (COUNT_CAP + 1,)
        ).fetchone()[0]
        found = conn.execute(
            sql.SQL("select {} from {}{} limit %s offset %s").format(picked, target, order), (PAGE_ROWS, offset)
        ).fetchall()
    return Rows(cols, tuple(tuple(r) for r in found), offset, total, tuple(c.name for c in key))


# --- Page ---------------------------------------------------------------------------------------

def _e(value: Any) -> str:
    return escape(str(value), quote=True)


def _link(**params: Any) -> str:
    return "/?" + urlencode({k: v for k, v in params.items() if v is not None})


def _time(value: datetime) -> str:
    if value.tzinfo is None:
        return _e(value.isoformat(sep=" "))
    try:
        utc = value.astimezone(timezone.utc)
        shown = utc.astimezone(EASTERN) if EASTERN else utc
    except OverflowError:   # near year 1 or 9999, a conversion falls outside what Python can hold
        return _e(value.isoformat(sep=" "))
    return (f'<span title="{_e(utc.isoformat(sep=" "))}">'
            f'{_e(shown.strftime("%Y-%m-%d %H:%M:%S %Z"))}</span>')


def _text(text: str) -> str:
    """Up to CUT characters inline, the rest one click away, never more than SHOW_AT_MOST."""
    if len(text) <= CUT and "\n" not in text:
        return _e(text)
    cut = len(text) > SHOW_AT_MOST
    full = text[:SHOW_AT_MOST] + (f"\n… cut at {SHOW_AT_MOST:,} characters" if cut else "")
    head = text[:CUT].split("\n", 1)[0]
    return f'<details><summary>{_e(head)}…</summary><pre>{_e(full)}</pre></details>'


def cell(value: Any, column: Column) -> str:
    """One value as HTML. Escaped, whatever it holds."""
    if value is None:
        return '<span class="null">NULL</span>'
    if column.type == "bytea":
        return f'<span class="meta">{value:,} bytes</span>'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return _time(value)
    if isinstance(value, (dict, list)):
        return _text(json.dumps(value, indent=1, ensure_ascii=False, default=str))
    if isinstance(value, (Decimal, int, float, date)):
        return _e(value)
    if value == "":
        return '<span class="meta">(empty)</span>'
    return _text(str(value))


def _index(found: list[Table], chosen: Table | None) -> str:
    out, schema = [], None
    for t in found:
        if t.schema != schema:
            if schema is not None:
                out.append("</ul></details>")
            schema = t.schema
            out.append(f'<details open><summary>{_e(schema)}</summary><ul>')
        here = ' aria-current="page"' if chosen == t else ""
        badge = ' <span class="badge">view</span>' if t.kind == "view" else ""
        out.append(f'<li><a href="{_e(_link(schema=t.schema, table=t.name))}"{here}>{_e(t.name)}</a>{badge}</li>')
    if schema is not None:
        out.append("</ul></details>")
    return "".join(out) or '<p class="meta">No tables yet. Run <code>python -m vending.db migrate</code>.</p>'


def _columns_html(cols: tuple[Column, ...]) -> str:
    body = "".join(
        f"<tr><td><code>{_e(c.name)}</code></td><td>{_e(c.type)}</td>"
        f"<td>{'yes' if c.nullable else 'no'}</td>"
        f"<td>{'<code>' + _e(c.default) + '</code>' if c.default else ''}</td>"
        f"<td>{'yes' if c.primary_key else ''}</td></tr>"
        for c in cols)
    return ('<table class="cols"><thead><tr><th>Column</th><th>Type</th><th>Can be null</th>'
            f'<th>Default</th><th>Primary key</th></tr></thead><tbody>{body}</tbody></table>')


def _total(page: Rows) -> str:
    return f"more than {COUNT_CAP:,}" if page.total > COUNT_CAP else f"{page.total:,}"


def _rows_html(table: Table, page: Rows) -> str:
    if not page.rows:
        return f'<p class="meta">{"No rows." if page.offset == 0 else "No rows on this page."}</p>'
    head = "".join(f'<th title="{_e(c.type)}">{_e(c.name)}</th>' for c in page.columns)
    body = "".join("<tr>" + "".join(f"<td>{cell(v, c)}</td>" for v, c in zip(r, page.columns)) + "</tr>"
                   for r in page.rows)
    first, last = page.offset + 1, page.offset + len(page.rows)
    order = (f"sorted by {', '.join(page.ordered_by)}, highest first" if page.ordered_by
             else "in no particular order (no primary key)")
    pager = []
    if page.offset > 0:
        pager.append(f'<a href="{_e(_link(schema=table.schema, table=table.name, offset=max(0, page.offset - PAGE_ROWS) or None))}">'
                     "← Previous</a>")
    # Past the count cap the total isn't known, so a full page means there may be more.
    more = last < page.total or (page.total > COUNT_CAP and len(page.rows) == PAGE_ROWS)
    if more and page.offset + PAGE_ROWS <= MAX_OFFSET:
        pager.append(f'<a href="{_e(_link(schema=table.schema, table=table.name, offset=page.offset + PAGE_ROWS))}">'
                     "Next →</a>")
    return (f'<p class="meta">Rows {first:,}–{last:,} of {_total(page)}, {_e(order)}.</p>'
            f'<div class="scroll"><table class="rows"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'
            f'<nav class="pager">{" ".join(pager)}</nav>')


STYLE = """
:root { --bg: #ffffff; --fg: #1d1d1f; --muted: #6b6b70; --line: #dcdce0; --head: #f4f4f6; --accent: #0b5cad; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #161618; --fg: #ececee; --muted: #a0a0a8; --line: #34343a; --head: #202024; --accent: #6cb2ff; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg); font: 14px/1.45 system-ui, sans-serif; }
header { padding: 12px 16px; border-bottom: 1px solid var(--line); }
header h1 { margin: 0; font-size: 18px; }
header p { margin: 2px 0 0; color: var(--muted); }
.layout { display: flex; min-height: calc(100vh - 60px); }
aside { flex: 0 0 240px; padding: 12px 16px; border-right: 1px solid var(--line); overflow-y: auto; }
aside ul { list-style: none; margin: 4px 0 12px; padding-left: 12px; }
aside li { margin: 2px 0; }
aside summary { font-weight: 600; cursor: pointer; }
main { flex: 1; min-width: 0; padding: 12px 16px; }
h2 { font-size: 16px; margin: 4px 0 8px; } h3 { font-size: 14px; margin: 16px 0 6px; }
a { color: var(--accent); } a[aria-current] { font-weight: 700; color: var(--fg); }
table { border-collapse: collapse; }
th, td { border: 1px solid var(--line); padding: 4px 8px; text-align: left; vertical-align: top; }
th { background: var(--head); position: sticky; top: 0; }
.rows td { white-space: nowrap; max-width: 40em; overflow: hidden; text-overflow: ellipsis; }
.rows td details[open] { white-space: normal; }
pre { margin: 4px 0 0; white-space: pre-wrap; word-break: break-word; max-width: 60em; }
.scroll { overflow-x: auto; max-height: 70vh; border: 1px solid var(--line); }
.scroll table { border: 0; }
.null, .meta { color: var(--muted); font-style: italic; }
.badge { font-size: 11px; color: var(--muted); border: 1px solid var(--line); border-radius: 4px; padding: 0 4px; }
.pager { margin: 8px 0; display: flex; gap: 16px; }
.error { border: 1px solid #c0392b; padding: 8px 12px; border-radius: 4px; }
@media (max-width: 700px) {
  .layout { display: block; } aside { border-right: 0; border-bottom: 1px solid var(--line); }
}
"""


def page(found: list[Table], chosen: Table | None, body: str) -> str:
    title = f"{chosen.schema}.{chosen.name} · DB explorer" if chosen else "DB explorer"
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>{_e(title)}</title><style>{STYLE}</style></head><body>"
            "<header><h1>DB explorer</h1><p>Read-only. What is in the database, table by table.</p></header>"
            f'<div class="layout"><aside>{_index(found, chosen)}</aside><main>{body}</main></div>'
            "</body></html>")


def table_body(table: Table, page_rows: Rows) -> str:
    kind = ' <span class="badge">view</span>' if table.kind == "view" else ""
    return (f"<h2>{_e(table.schema)}.{_e(table.name)}{kind}</h2>"
            f"<h3>Columns</h3>{_columns_html(page_rows.columns)}"
            f"<h3>Rows</h3>{_rows_html(table, page_rows)}")


WELCOME = '<p class="meta">Choose a table on the left to see its columns and rows.</p>'


class BadRequest(ValueError):
    pass


@dataclass(frozen=True)
class Request:
    schema: str | None
    table: str | None
    offset: int


def parse(target: str) -> Request:
    """The page asked for. Refuses anything malformed before a database is touched."""
    parts = urlsplit(target)
    if parts.path != "/":
        raise LookupError(parts.path)
    try:
        query = parse_qs(parts.query, keep_blank_values=True, strict_parsing=bool(parts.query), max_num_fields=10)
    except ValueError as error:
        raise BadRequest("malformed query string") from error
    if any(len(values) > 1 for values in query.values()):
        raise BadRequest("each parameter at most once")
    unknown = set(query) - {"schema", "table", "offset"}
    if unknown:
        raise BadRequest(f"unknown parameter {sorted(unknown)[0]!r}")
    schema, table = query.get("schema", [None])[0], query.get("table", [None])[0]
    if (schema is None) != (table is None):
        raise BadRequest("give both schema and table, or neither")
    raw = query.get("offset", ["0"])[0]
    if not (raw.isascii() and raw.isdigit()) or len(raw) > 7 or int(raw) > MAX_OFFSET:
        raise BadRequest(f"offset must be a whole number from 0 to {MAX_OFFSET:,}")
    if table is None and raw != "0":
        raise BadRequest("offset needs a table")
    return Request(schema, table, int(raw))


def render(conn: psycopg.Connection, request: Request) -> tuple[int, str]:
    """(status, HTML) for a request. Nothing from the URL reaches SQL unless Postgres lists that
    table: an unlisted name is a 404, never a query. The whole page is one read-only transaction."""
    _lenient_dates(conn)
    found: list[Table] = []
    chosen: Table | None = None
    try:
        with _snapshot(conn):
            found = tables(conn)
            if request.table is None:
                return 200, page(found, None, WELCOME)
            chosen = next((t for t in found if t.schema == request.schema and t.name == request.table), None)
            if chosen is None:
                missing = f'<p class="error">No table {_e(request.schema)}.{_e(request.table)}.</p>'
                return 404, page(found, None, missing)
            page_rows = rows(conn, chosen, request.offset)
    except psycopg.errors.QueryCanceled:
        why, code = f"Reading this table took longer than {STATEMENT_TIMEOUT_MS // 1000} seconds, so it was stopped.", 503
    except psycopg.errors.InsufficientPrivilege:
        why, code = "This database user may not read this table.", 403
    else:
        return 200, page(found, chosen, table_body(chosen, page_rows))
    heading = f"<h2>{_e(chosen.schema)}.{_e(chosen.name)}</h2>" if chosen else ""
    return code, page(found, chosen, f'{heading}<p class="error">{_e(why)}</p>')


class Handler(BaseHTTPRequestHandler):
    server_version = "vending-db-explorer"

    def do_GET(self) -> None:  # noqa: N802 (the stdlib's name)
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        if host not in LOCAL_HOSTS:
            return self._send(403, "text/plain; charset=utf-8", "open this page as 127.0.0.1 or localhost")
        try:
            request = parse(self.path)
        except LookupError:
            return self._send(404, "text/plain; charset=utf-8", "not found")
        except BadRequest as error:
            return self._send(400, "text/plain; charset=utf-8", str(error))
        try:
            with connect() as conn:
                code, body = render(conn, request)
        except (psycopg.Error, settings.SettingsError) as error:
            message = str(error).strip().splitlines()[0] if str(error).strip() else type(error).__name__
            return self._send(503, "text/html; charset=utf-8", page([], None, f'<p class="error">{_e(message)}</p>'))
        except Exception as error:  # a value we didn't foresee: answer, and keep the details in the log
            print(f"db_explorer: {self.path}: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
            body = page([], None, '<p class="error">This page hit an error the explorer doesn\'t handle yet. '
                                  "The details are in the explorer's terminal.</p>")
            return self._send(500, "text/html; charset=utf-8", body)
        self._send(code, "text/html; charset=utf-8", body)

    def _send(self, code: int, content_type: str, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 (the stdlib's name)
        pass


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m vending.db_explorer", description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args(argv)
    httpd = ThreadingHTTPServer((HOST, args.port), Handler)
    print(f"DB explorer on http://{HOST}:{args.port}  (read-only; Ctrl+C to stop)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
