"""Every connection we open runs its session in UTC, so times labelled `UTC` are UTC (#87).

Postgres hands back a `timestamptz` in the session's time zone. A database, role or URL set to
another zone (a Windows Postgres picks up local time) would otherwise make the tools print
Eastern times with a `UTC` label. The database tests stand in for such a server with a
connection URL that asks for `America/New_York`, so nothing global is changed.
"""

from datetime import datetime, timezone

import pytest

psycopg = pytest.importorskip("psycopg")

from psycopg.conninfo import make_conninfo  # noqa: E402

from vending import dashboard, db, db_explorer, pnl_report  # noqa: E402
from vending.tools.catalog import get_catalog  # noqa: E402

NEW_YORK = "-c TimeZone=America/New_York"
STORED = datetime(2026, 11, 12, 6, 0, tzinfo=timezone.utc)   # 01:00 in New York


# --- The options, without a database --------------------------------------------------------

def test_a_plain_url_gets_utc_alone():
    assert db.session_options("postgresql://u@h/d") == db.UTC_SESSION


def test_the_urls_own_options_and_extras_are_kept_with_utc_last():
    url = "postgresql://u@h/d?options=-c%20search_path%3Dx"
    assert db.session_options(url, "-c a=b") == "-c search_path=x -c a=b -c TimeZone=UTC"


def test_utc_comes_after_a_time_zone_the_url_sets_so_it_wins():
    options = db.session_options(f"host=h options='{NEW_YORK}'")
    assert options.index(NEW_YORK) < options.index(db.UTC_SESSION)


# --- Against the local database ------------------------------------------------------------

def _new_york(url: str, schema: str | None = None) -> str:
    """`url` as a server whose sessions default to New York time."""
    options = f"-c search_path={schema} {NEW_YORK}" if schema else NEW_YORK
    return make_conninfo(url, options=options)


def _synced_at(conn) -> str:
    conn.execute("insert into catalog_syncs (synced_at, product_count) values (%s, 0)", (STORED,))
    return get_catalog(conn, None, now=STORED).message


def test_the_bug_reproduces_on_a_new_york_session(schema_url):
    """Without the fix, a stored 06:00 UTC prints as 01:00 UTC: what #87 reported."""
    url, schema = schema_url
    with psycopg.connect(_new_york(url, schema), autocommit=True) as conn:
        assert conn.execute("show timezone").fetchone()[0] == "America/New_York"
        assert "Catalog last synced 2026-11-12 01:00 UTC." in _synced_at(conn)


def test_db_connect_runs_in_utc_and_the_tool_prints_the_true_time(schema_url, monkeypatch):
    url, schema = schema_url
    monkeypatch.setenv("DATABASE_URL", _new_york(url, schema))
    with db.connect() as conn:
        assert conn.execute("show timezone").fetchone()[0] == "UTC"
        assert conn.execute("show search_path").fetchone()[0] == schema   # the URL's own option kept
        assert "Catalog last synced 2026-11-12 06:00 UTC." in _synced_at(conn)


@pytest.mark.parametrize("module", [dashboard, db_explorer, pnl_report], ids=lambda m: m.__name__)
def test_the_read_only_pages_run_in_utc_and_stay_read_only(database_url, monkeypatch, module):
    monkeypatch.setenv("DATABASE_URL", _new_york(database_url))
    with module._connect() as conn:
        assert conn.execute("show timezone").fetchone()[0] == "UTC"
        assert conn.execute("show default_transaction_read_only").fetchone()[0] == "on"
