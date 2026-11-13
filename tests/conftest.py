"""Shared fixtures. Database tests each run in a throwaway schema in the local
database, and are skipped when DATABASE_URL isn't set, isn't local, or the
database is down.

`settings` falls back to `.env`, and a real `.env` can point at the instructors'
shared Azure server, where these tests would create and drop schemas. So every
test that connects gets its URL from `local_database_url()`, which refuses a
remote URL before anything connects.

Nothing here imports `psycopg` at module level, so the pure tests (`test_evadts`,
`test_planner`, `test_pricing`) collect and run on a machine without it.
"""

from __future__ import annotations

import uuid

import pytest

from vending import settings

# Seconds to wait for the test database. Without a limit, a connect to a closed
# port on Windows waits on TCP retries for minutes instead of failing.
CONNECT_TIMEOUT = 3

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def local_database_url() -> str:
    """`DATABASE_URL`, or skip the test unless every host it names is this machine.

    The URL goes through the same parser the connection would use, so a second
    host, a `?host=` override or the `host=... dbname=...` form can't get past.
    With no host at all, `PGHOST` would decide, so that is refused too.
    """
    from psycopg import ProgrammingError
    from psycopg.conninfo import conninfo_to_dict

    url = settings.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set")
    try:
        params = conninfo_to_dict(url)
    except ProgrammingError:
        # The parser's message can quote the URL, password included.
        pytest.skip("DATABASE_URL could not be parsed, so it can't be shown to be local")
    hosts = params.get("host", "").split(",")
    if params.get("hostaddr"):
        hosts += params["hostaddr"].split(",")
    remote = [host for host in hosts if host not in LOCAL_HOSTS]
    if remote:
        pytest.skip(f"DATABASE_URL points at {remote[0] or 'no host'}, not a local database; "
                    "refusing to run database tests against it")
    return url


@pytest.fixture(scope="session")
def database_url():
    """A local DATABASE_URL, checked once per run so a down database skips every test fast."""
    psycopg = pytest.importorskip("psycopg")
    url = local_database_url()
    try:
        psycopg.connect(url, connect_timeout=CONNECT_TIMEOUT).close()
    except psycopg.OperationalError:
        pytest.skip("database not reachable (docker compose up -d db)")
    return url


@pytest.fixture
def schema_url(database_url):
    """(url, schema) for a fresh, migrated schema, dropped after the test."""
    import psycopg

    from vending import db

    url = database_url
    admin = psycopg.connect(url, autocommit=True, connect_timeout=CONNECT_TIMEOUT)
    schema = f"test_{uuid.uuid4().hex[:8]}"
    admin.execute(f"create schema {schema}")
    with open_in(url, schema) as conn:
        db.migrate(conn)
    yield url, schema
    admin.execute(f"drop schema {schema} cascade")
    admin.close()


def open_in(url: str, schema: str):
    """A connection in `schema`, in UTC like every connection the app opens (`db.session_options`)."""
    import psycopg

    from vending import db

    return psycopg.connect(url, autocommit=True, connect_timeout=CONNECT_TIMEOUT,
                           options=db.session_options(url, f"-c search_path={schema}"))


@pytest.fixture
def conn(schema_url):
    with open_in(*schema_url) as connection:
        yield connection


def store_poll(conn, session_id: str, started_at: str, selections: dict[str, tuple],
               device_id: str = "vmc1") -> None:
    """Store a synthetic poll through the history sync's own `store`.
    `selections` maps slot -> (price_cents or None, lifetime_units), or to
    (price_cents, lifetime_units, lifetime_sales_cents) when the cents counter
    is not units at today's price (the price changed between polls)."""
    from tests.test_evadts import build
    from vending.sync_history import store

    records = ["ID1*TEST-MACHINE*0000**EVS3.0*", "ID4*2*1840*5", "VA1*0*0*0*0"]
    for slot, (price, units, *sales_cents) in selections.items():
        sales_cents = sales_cents[0] if sales_cents else units * (price or 0)
        records += [f"PA1*{slot}*{'' if price is None else price}*", f"PA2*{units}*{sales_cents}*0*0*0*0"]
    meta = {"session_id": session_id, "device_id": device_id, "started_at": started_at}
    assert store(conn, meta, build(records)) == "ok"
