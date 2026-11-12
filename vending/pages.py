"""The pages anyone can open at the Container App's address: the agent dashboard and the DB explorer.

    /dashboard               the agent dashboard (`vending.dashboard`)
    /dashboard/state.json    the same data as JSON
    /db                      the DB explorer (`vending.db_explorer`)

    python -m vending.pages check                            # is the pages' login really read-only?
    python -m vending.pages grant vending_reader --by NAME   # as the tables' owner: let that role read them

`vending.worker` serves these beside `/` and `/health` (issue #53). Anyone with
the link can look, and nobody can change anything. There is no sign-in.

Read-only in three layers, so that a bug in our code still can't write:

 1. **A login that can only read.** The pages connect with their own setting,
    `READONLY_DATABASE_URL`, never with the syncs' `DATABASE_URL`. Before a page
    is served, the login is asked what it may do: if it can insert, update,
    delete or truncate any table, or create one, the pages refuse to serve and
    say so in the log. So pointing the setting at the syncs' login switches the
    pages off instead of weakening them. Until the setting exists, the pages
    answer "not set up yet".
 2. **A read-only session.** Every transaction on the connection is read-only,
    and each page is one `repeatable read, read only` transaction.
 3. **Nothing to send.** GET only, no form, no SQL box. The only things a URL can
    choose are a table Postgres already lists and a row offset.

Load limits, because the address is public: at most `MAX_CONNECTIONS` page
connections at once (the server's 50 are shared with the other team), a request
that can't get one within `WAIT_SECONDS` is told the page is busy, every
statement is cancelled after `STATEMENT_TIMEOUT_MS`, and the explorer shows 100
rows a page. Hammering the pages makes the pages slow, not the syncs.

What the pages leave out: the error text of failures (`HIDE`, and
`dashboard.hide_errors`), because a database error names the host and the user.
Their own errors are answered with a plain sentence, and the detail goes to the
container's log. No table holds a password or a key.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from html import escape
from urllib.parse import urlsplit

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from vending import dashboard, db, db_explorer, settings

READER = "READONLY_DATABASE_URL"
DASHBOARD, STATE, EXPLORER = "/dashboard", "/dashboard/state.json", "/db"
MAX_CONNECTIONS = 2
# Seconds a request waits for one of those connections before it is told the page is busy.
WAIT_SECONDS = 5
STATEMENT_TIMEOUT_MS = 5_000
OPTIONS = f"-c default_transaction_read_only=on -c statement_timeout={STATEMENT_TIMEOUT_MS}"
# The statement timeout runs on the server, so it can't free a request whose connection has gone
# quiet (a failover, a half-open socket). These client-side limits make a dead connection fail
# within seconds instead of holding one of the page connections for 15 minutes or more.
KEEPALIVES = {"keepalives": 1, "keepalives_idle": 5, "keepalives_interval": 2, "keepalives_count": 3,
              "tcp_user_timeout": STATEMENT_TIMEOUT_MS + 5_000}
# Seconds a passed "this login can't write" check is trusted before it is made again.
RECHECK_SECONDS = 600
# Seconds between two log lines about the same kind of problem, so a stuck page can't flood the log.
LOG_EVERY_SECONDS = 60

HTML, JSON, TEXT = "text/html; charset=utf-8", "application/json", "text/plain; charset=utf-8"
# Neither page loads anything from anywhere else. The dashboard's own script refreshes it in place.
CSP = "default-src 'none'; style-src 'unsafe-inline'"
CSP_DASHBOARD = CSP + "; script-src 'unsafe-inline'; connect-src 'self'"

# What the explorer leaves out, by table name. The dashboard's own list is `dashboard.hide_errors`.
HIDE = {
    "action_log": db_explorer.Hide("outcome", "failed", ("detail", "result"), dashboard.HIDDEN),
    "agent_runs": db_explorer.Hide("status", "failed", ("detail",), dashboard.HIDDEN),
}

NOT_SET_UP = ("This page isn't set up here yet. It needs a database login that can only read "
              f"({READER}); see docs/deploy.md in the repository.")
BUSY = "This page is busy. Try again in a few seconds."
CANT_READ = "This page can't read the database right now. The detail is in the container's log."
NOT_READ_ONLY = ("This page is switched off: its database login is able to write, and a public page "
                 "must not be. The detail is in the container's log.")
BROKEN = "This page hit an error it doesn't handle yet. The detail is in the container's log."


class Busy(Exception):
    """Every page connection is in use."""


class CanWrite(Exception):
    """The pages' login may change something, so they must not be served with it."""


@dataclass(frozen=True)
class Reply:
    status: int
    content_type: str
    body: str
    csp: str = CSP


def configured() -> bool:
    return bool(settings.get(READER))


def _connect() -> psycopg.Connection:
    url = settings.require(READER, "It is the read-only login's URL; see docs/deploy.md.")
    return psycopg.connect(url, autocommit=True, connect_timeout=db.CONNECT_TIMEOUT, options=OPTIONS, **KEEPALIVES)


# Tests swap this to point at a throwaway schema.
connect: Callable[[], psycopg.Connection] = _connect

_slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
_read_only_until = 0.0
_logged: dict[str, float] = {}

_OURS = "n.nspname not in ('pg_catalog', 'information_schema') and left(n.nspname, 3) <> 'pg_'"
_WRITABLE = f"""
    select n.nspname || '.' || c.relname
      from pg_class c join pg_namespace n on n.oid = c.relnamespace
     where c.relkind in ('r', 'p', 'v', 'm', 'f') and {_OURS}
       and has_table_privilege(c.oid, 'INSERT, UPDATE, DELETE, TRUNCATE')
    union all
    select 'schema ' || n.nspname || ' (create)' from pg_namespace n
     where {_OURS} and has_schema_privilege(n.oid, 'CREATE')
     order by 1"""
_UNREADABLE = f"""
    select n.nspname || '.' || c.relname
      from pg_class c join pg_namespace n on n.oid = c.relnamespace
     where c.relkind in ('r', 'p', 'v', 'm', 'f') and {_OURS}
       and has_schema_privilege(n.oid, 'USAGE') and not has_table_privilege(c.oid, 'SELECT')
    union all
    select 'schema ' || n.nspname || ' (no usage)'
      from unnest(string_to_array(current_setting('search_path'), ',')) as p(entry)
      join pg_namespace n on n.nspname = replace(trim(both ' "' from p.entry), '$user', current_user)
     where {_OURS} and not has_schema_privilege(n.oid, 'USAGE')
     order by 1"""


def writable(conn: psycopg.Connection) -> list[str]:
    """Every table this login may change and every schema it may create in. Empty for a reader."""
    return [row[0] for row in conn.execute(_WRITABLE)]


def unreadable(conn: psycopg.Connection) -> list[str]:
    """Tables this login can see but not read, and schemas on its search path it may not look
    inside (whose tables it can't even see); a page that needs one would fail."""
    return [row[0] for row in conn.execute(_UNREADABLE)]


def _log(kind: str, message: str) -> None:
    now = time.monotonic()
    if now - _logged.get(kind, -LOG_EVERY_SECONDS) >= LOG_EVERY_SECONDS:
        _logged[kind] = now
        print(f"pages: {message}", file=sys.stderr, flush=True)


@contextmanager
def _connection() -> Iterator[psycopg.Connection]:
    """One of the few page connections, on a login shown to be read-only."""
    global _read_only_until
    if not _slots.acquire(timeout=WAIT_SECONDS):
        raise Busy
    try:
        with connect() as conn:
            if time.monotonic() >= _read_only_until:
                found = writable(conn)
                if found:
                    raise CanWrite(", ".join(found[:5]) + (f" and {len(found) - 5} more" if len(found) > 5 else ""))
                _read_only_until = time.monotonic() + RECHECK_SECONDS
            yield conn
    finally:
        _slots.release()


def _notice(title: str, text: str) -> str:
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{escape(title)}</title><style>body{{font:16px/1.5 system-ui,sans-serif;max-width:40rem;"
            "margin:3rem auto;padding:0 16px}@media (prefers-color-scheme:dark){body{background:#111418;"
            "color:#e8eaed}a{color:inherit}}</style></head>"
            f'<body><h1>{escape(title)}</h1><p>{escape(text)}</p><p><a href="/">Back to the home page</a></p>'
            "</body></html>")


def _problem(path: str, status: int, text: str) -> Reply:
    if path == STATE:
        return Reply(status, JSON, dashboard.to_json({"error": text}))
    return Reply(status, HTML, _notice("DB explorer" if path == EXPLORER else "Agent dashboard", text))


def respond(target: str) -> Reply | None:
    """The reply to a GET of `target` (path and query), or None when the path isn't one of ours.

    Never raises, and never puts an error's own text in a reply.
    """
    parts = urlsplit(target)
    path = parts.path.rstrip("/")
    if path not in (DASHBOARD, STATE, EXPLORER):
        return None
    request = None
    if path == EXPLORER:
        try:
            request = db_explorer.parse("/?" + parts.query if parts.query else "/")
        except db_explorer.BadRequest as error:
            return Reply(400, TEXT, str(error))
    if not configured():
        return _problem(path, 503, NOT_SET_UP)
    try:
        with _connection() as conn:
            if request is not None:
                status, body = db_explorer.render(conn, request, HIDE, (("Home", "/"), ("Agent dashboard", DASHBOARD)))
                return Reply(status, HTML, body)
            snap = dashboard.snapshot(conn, public=True)
    except Busy:
        return _problem(path, 503, BUSY)
    except CanWrite as error:
        _log("can write", f"not serving {path}: the {READER} login can write ({error}). "
                          "Give it a login with select only; run python -m vending.pages check")
        return _problem(path, 503, NOT_READ_ONLY)
    except (psycopg.Error, settings.SettingsError) as error:
        first = str(error).strip().splitlines()[0] if str(error).strip() else ""
        _log(type(error).__name__, f"{path}: {type(error).__name__}: {first}")
        return _problem(path, 503, CANT_READ)
    except Exception as error:  # noqa: BLE001 - a public page answers, whatever happened
        _log(type(error).__name__, f"{path}: {type(error).__name__}: {error}")
        return _problem(path, 500, BROKEN)
    if path == STATE:
        return Reply(200, JSON, dashboard.to_json(snap))
    page = dashboard.render(snap, json_href=STATE, nav=(("Home", "/"), ("Database", EXPLORER)))
    return Reply(200, HTML, page, CSP_DASHBOARD)


# --- Commands -----------------------------------------------------------------------------------

def check() -> int:
    """Connect as the pages do and say whether that login is fit for a public page."""
    if not configured():
        print(f"{READER} is not set, so the pages answer \"not set up yet\". See docs/deploy.md.")
        return 1
    with connect() as conn:
        who, database, limit = conn.execute(
            "select current_user, current_database(),"
            " (select rolconnlimit from pg_roles where rolname = current_user)").fetchone()
        can_write, cant_read = writable(conn), unreadable(conn)
    print(f"login {who} on database {database}"
          + (f", at most {limit} connections" if limit is not None and limit >= 0 else ", no connection limit"))
    if can_write:
        print("NOT read-only, so the pages refuse to serve. It can change: " + ", ".join(can_write))
    else:
        print("read-only: it can't insert, update, delete or truncate any table, or create one")
    if cant_read:
        print("can't read: " + ", ".join(cant_read)
              + "\n  As the tables' owner, run: python -m vending.pages grant " + str(who) + " --by NAME")
    else:
        print("can read every table")
    return 1 if can_write or cant_read else 0


def grant(conn: psycopg.Connection, role: str, by: str) -> str:
    """Let `role` read every table in our schema, now and after later migrations. Returns what was done.

    Run as the tables' owner (the `DATABASE_URL` login). It only grants `usage` on the schema and
    `select` on its tables. Creating the role is for whoever administers the server; see
    docs/deploy.md. If this login may not grant `usage` on the schema, Postgres only warns, so
    `check` is what proves the role can read.
    """
    schema, me = conn.execute("select current_schema(), current_user").fetchone()
    if role == me:
        raise ValueError(f"{role} is the login that owns the tables; the pages need a different one")
    if conn.execute("select 1 from pg_roles where rolname = %s", (role,)).fetchone() is None:
        raise LookupError(f"no role {role} on this server. Ask the server's administrator to create it; "
                          "docs/deploy.md has the two statements")
    target, where = sql.Identifier(role), sql.Identifier(schema)
    done = (f"usage on schema {schema} and select on every table in it granted to {role} by {by}, "
            "for later tables too")
    with conn.transaction():
        # Without usage the role can't see the tables at all. Stock Postgres gives it to everyone on
        # `public`, but a hardened server or another schema may not.
        conn.execute(sql.SQL("grant usage on schema {} to {}").format(where, target))
        conn.execute(sql.SQL("grant select on all tables in schema {} to {}").format(where, target))
        # Tables this login creates later, which is what a migration does.
        conn.execute(sql.SQL("alter default privileges in schema {} grant select on tables to {}").format(where, target))
        if conn.execute("select to_regclass('action_log')").fetchone()[0] is not None:
            conn.execute("insert into action_log (kind, tool, arguments, outcome, detail)"
                         " values ('write', 'pages.grant', %s, 'done', %s)", (Jsonb({"role": role}), done))
    return done


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m vending.pages", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help=f"is the {READER} login read-only, and can it read every table?")
    granting = commands.add_parser("grant", help="as the tables' owner: let a role read every table")
    granting.add_argument("role")
    granting.add_argument("--by", required=True, help="your name, for the log")
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            return check()
        if not args.by.strip():
            parser.error("--by NAME is required")
        with db.connect() as conn:
            print(grant(conn, args.role, args.by.strip()))
        return 0
    except (LookupError, ValueError, settings.SettingsError) as error:
        print(error, file=sys.stderr)
        return 2
    except psycopg.Error as error:
        print(f"{type(error).__name__}: {str(error).strip().splitlines()[0]}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
