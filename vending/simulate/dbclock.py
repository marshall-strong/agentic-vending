"""The simulated clock inside the scratch database, so SQL that reads the time reads the season's time.

The real scheduler, the run lease, the submit tool's checks before a send, the tracking check and the dashboard
decide by the database's clock (`now()`, `clock_timestamp()`, `transaction_timestamp()`), not by Python's. A
simulated season is weeks ahead of the wall clock, so on its own the database would say it is still November.

`install` adds functions with those names to the season's schema, each reading `sim_clock` (one row) and
falling back to the real clock while it is empty. `search_path` puts the schema before `pg_catalog`, so an
unqualified `now()` finds the season's function first; Postgres searches `pg_catalog` first only when the path
leaves it out. `set` moves the clock. Column defaults (`default now()`) were bound to `pg_catalog.now` when the
tables were made, so `install` points them at the season's `now()` too.

Only the scratch database gets this. Nothing here runs against any other database (`season.check_scratch`).
"""

from __future__ import annotations

from datetime import datetime

import psycopg
from psycopg import sql

TABLE = "sim_clock"
# Each function and whether Postgres's own is fixed for a transaction (stable) or reads the clock each call.
FUNCTIONS = {"now": "stable", "transaction_timestamp": "stable", "statement_timestamp": "stable",
             "clock_timestamp": "volatile"}


def install(conn: psycopg.Connection, schema: str = "public") -> None:
    """Add `sim_clock` and the clock functions to `schema`. Empty, they read the real clock."""
    conn.execute(sql.SQL("create table if not exists {}.{} (one boolean primary key default true check (one),"
                         " at timestamptz not null)").format(sql.Identifier(schema), sql.Identifier(TABLE)))
    for name, volatility in FUNCTIONS.items():
        conn.execute(sql.SQL(
            "create or replace function {schema}.{name}() returns timestamptz language sql {vol} as $$"
            " select coalesce((select at from {schema}.{table}), pg_catalog.{name}()) $$").format(
            schema=sql.Identifier(schema), name=sql.Identifier(name), vol=sql.SQL(volatility),
            table=sql.Identifier(TABLE)))
    # Column defaults were bound to `pg_catalog.now` when the tables were made: point them at the season's.
    for table, column in conn.execute(
            "select c.relname, a.attname from pg_attrdef d join pg_class c on c.oid = d.adrelid"
            " join pg_attribute a on a.attrelid = d.adrelid and a.attnum = d.adnum"
            " where c.relnamespace = %s::regnamespace and c.relkind = 'r'"
            " and pg_get_expr(d.adbin, d.adrelid) in ('now()', 'pg_catalog.now()')", (schema,)).fetchall():
        conn.execute(sql.SQL("alter table {}.{} alter column {} set default {}.now()").format(
            sql.Identifier(schema), sql.Identifier(table), sql.Identifier(column), sql.Identifier(schema)))


def search_path(schema: str = "public") -> str:
    """The startup option that makes the season's functions win over `pg_catalog`'s."""
    return f"-c search_path={schema},pg_catalog"


def schema_of(url: str) -> str:
    """The schema a URL's own options put first (`-c search_path=...`), else public."""
    from psycopg.conninfo import conninfo_to_dict

    for part in (conninfo_to_dict(url).get("options") or "").split("-c"):
        key, _, value = part.strip().partition("=")
        if key == "search_path" and value:
            return value.split(",")[0].strip()
    return "public"


def with_clock(url: str, schema: str | None = None) -> str:
    """`url` with `search_path` added to its startup options (kept after any it already has). `schema`: the one
    the URL's options name, else public."""
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    schema = schema or schema_of(url)
    params = conninfo_to_dict(url)
    params["options"] = " ".join(o for o in (params.get("options") or "", search_path(schema)) if o)
    return make_conninfo(**params)


def set(conn: psycopg.Connection, at: datetime) -> None:  # noqa: A001 - the module's verb, `dbclock.set`
    """Move the database's clock to `at`."""
    conn.execute(sql.SQL("insert into {} (one, at) values (true, %s) on conflict (one) do update set at = excluded.at")
                 .format(sql.Identifier(TABLE)), (at,))


def installed(conn: psycopg.Connection) -> bool:
    """Whether the connection's schema has the season's clock (`install`)."""
    return conn.execute("select to_regclass(%s) is not null", (TABLE,)).fetchone()[0]


def reads_season_time(conn: psycopg.Connection) -> bool:
    """Whether this connection's `now()` is the season's (the path and the functions are in place)."""
    try:
        with conn.transaction():
            row = conn.execute(sql.SQL("select now() = (select at from {}) from {}").format(
                sql.Identifier(TABLE), sql.Identifier(TABLE))).fetchone()
    except psycopg.Error:
        return False
    return bool(row and row[0])
