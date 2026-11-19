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

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from vending import settings

# Seconds to wait for the test database. Without a limit, a connect to a closed
# port on Windows waits on TCP retries for minutes instead of failing.
CONNECT_TIMEOUT = 3

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}

# The slot assignment the tests use: what `schema_url` stores in `slot_assignment`, and what the pure
# tests pass where code takes our slots. Test data only. Our real slots are whatever the inventory server
# assigns (`vending.slots.our_slots`); no code outside the tests holds a list of them.
TEST_SLOTS: tuple[str, ...] = ("15", "16", "17", "18", "25", "26", "27", "28", "35", "36",
                               "37", "38", "45", "46", "47", "48", "55", "56", "57", "58")


# "Now" for our slot assignment in every test (`vending.slots.clock`, the database clock in the app): before the
# start, so tests don't change behaviour on the day the competition starts. Tests about the start set their own.
TEST_NOW = datetime(2026, 11, 12, 15, tzinfo=timezone.utc)
# When conftest's assignment was last checked: before TEST_NOW, so a test may record at TEST_NOW.
TEST_CHECKED_AT = TEST_NOW - timedelta(days=1)


@pytest.fixture(autouse=True)
def _slots_before_the_start(monkeypatch):
    """Every test sees the slot clock at TEST_NOW, the default lock (3 Dec 2026), our default team and the default
    fall cut, whatever the real date and whatever a developer's `.env` sets. Without this the slot tests would start failing at the
    start, the day a hotfix is most likely."""
    from vending import slots

    monkeypatch.setattr(slots, "clock", lambda conn: TEST_NOW)
    # FALL_CUTOFF is analysis/slots_config.py's fall cut, also a setting a developer's `.env` may hold.
    names = (slots.LOCK_SETTING, slots.TEAM_SETTING, "FALL_CUTOFF")
    for name in names:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(settings, "_FILE", {k: v for k, v in settings._FILE.items() if k not in names})


# Test schemas are thrown away, so their commits needn't wait for the disk. Changes what a crash
# could lose, never what another connection sees.
FAST_COMMIT = "-c synchronous_commit=off"


@pytest.fixture(autouse=True)
def _no_password_gate(request, monkeypatch):
    """The pages' password gate is off except in the tests about it (tests/test_gate.py)."""
    if request.module.__name__.rsplit(".", 1)[-1] != "test_gate":
        from vending import gate

        monkeypatch.setattr(gate, "ENABLED", False)


@pytest.fixture(autouse=True)
def _no_visit_counting(request, monkeypatch):
    """The pages' visit counter writes nothing except in the tests about it (tests/test_visits.py),
    so a test that opens a page never writes to the database `.env` names."""
    if request.module.__name__.rsplit(".", 1)[-1] != "test_visits":
        from vending import visits

        monkeypatch.setattr(visits, "ENABLED", False)


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


# Everything about a schema's objects that a test could change: tables, columns, defaults,
# constraints, indexes, owners, grants, functions, triggers. Read with an empty search_path so
# every name is qualified, then the schema's own name is replaced, so two schemas built by the
# same migrations print the same.
_SHAPE = """
select string_agg(line, E'\\n' order by line) from (
    select 'schema ' || coalesce(nspacl::text, '') || ' ' || nspowner::regrole::text as line
    from pg_namespace where oid = %(s)s::regnamespace
  union all
    select 'rel ' || c.oid::regclass::text || ' ' || c.relkind::text || ' ' || c.relowner::regrole::text
           || ' ' || coalesce(c.relacl::text, '') || ' ' || c.relrowsecurity::text
    from pg_class c where c.relnamespace = %(s)s::regnamespace
  union all
    select 'col ' || a.attrelid::regclass::text || '.' || a.attname || ' ' || a.attnum::text || ' '
           || format_type(a.atttypid, a.atttypmod) || ' ' || a.attnotnull::text || ' '
           || coalesce(pg_get_expr(d.adbin, d.adrelid), '') || ' ' || coalesce(a.attacl::text, '')
    from pg_attribute a join pg_class c on c.oid = a.attrelid
    left join pg_attrdef d on d.adrelid = a.attrelid and d.adnum = a.attnum
    where c.relnamespace = %(s)s::regnamespace and a.attnum > 0 and not a.attisdropped
  union all
    select 'con ' || conrelid::regclass::text || ' ' || conname || ' ' || pg_get_constraintdef(oid)
    from pg_constraint where connamespace = %(s)s::regnamespace
  union all
    select 'idx ' || indexrelid::regclass::text || ' ' || pg_get_indexdef(indexrelid)
    from pg_index i join pg_class c on c.oid = i.indexrelid where c.relnamespace = %(s)s::regnamespace
  union all
    select 'trg ' || tgrelid::regclass::text || ' ' || tgname from pg_trigger t
    join pg_class c on c.oid = t.tgrelid where not t.tgisinternal and c.relnamespace = %(s)s::regnamespace
  union all
    select 'fn ' || p.oid::regprocedure::text from pg_proc p where p.pronamespace = %(s)s::regnamespace
  union all
    select 'type ' || t.oid::regtype::text from pg_type t
    where t.typnamespace = %(s)s::regnamespace and t.typrelid = 0 and t.typelem = 0
  union all
    select 'defacl ' || defaclobjtype::text || ' ' || defaclacl::text from pg_default_acl
    where defaclnamespace = %(s)s::regnamespace
) shape
"""


class _NotReusable(Exception):
    """A schema that goes back to the pool by being dropped."""


class SchemaPool:
    """Migrated schemas kept for reuse within one pytest process (one per xdist worker).

    Building a schema is most of a database test's time (create, migrate, drop: about 1 s each
    on Docker Desktop). A schema whose objects a test left exactly as the migrations made them
    goes back to the pool with its rows reset to `pristine`'s, a schema migrated once and never
    handed out. A schema the test changed in any other way (a new table, a grant, a dropped
    column) is dropped instead, so the next test still starts from what the migrations make.
    """

    def __init__(self, url: str):
        import psycopg

        from vending import db

        self.url = url
        self.admin = psycopg.connect(url, autocommit=True, connect_timeout=CONNECT_TIMEOUT,
                                     options=db.session_options(url, FAST_COMMIT, "-c search_path="))
        self.free: list[str] = []
        self.pristine = self.build()
        self.shape = self.shape_of(self.pristine)
        tables = [row[0] for row in self.admin.execute(
            "select relname from pg_class where relnamespace = %s::regnamespace and relkind = 'r'",
            (self.pristine,))]
        references = self.admin.execute(
            "select c.relname, r.relname from pg_constraint k"
            " join pg_class c on c.oid = k.conrelid join pg_class r on r.oid = k.confrelid"
            " where k.contype = 'f' and k.connamespace = %s::regnamespace and c.oid <> r.oid",
            (self.pristine,)).fetchall()
        # Children before parents, so every delete passes the foreign keys.
        order, left = [], set(tables)
        while left:
            leaves = sorted(t for t in left if not any(r == t and c in left for c, r in references))
            assert leaves, f"foreign keys form a cycle among {sorted(left)}; the pool can't order the deletes"
            order += leaves
            left -= set(leaves)
        self.order = order
        self.seeded = [t for t in tables
                       if self.admin.execute(f'select exists (select from {self.pristine}."{t}")').fetchone()[0]]
        self.sequences = self.admin.execute(
            "select sequencename, start_value from pg_sequences where schemaname = %s",
            (self.pristine,)).fetchall()
        assert all(row[0] is None for row in self.admin.execute(
            "select last_value from pg_sequences where schemaname = %s", (self.pristine,))), \
            "a migration used a sequence, so resetting them to their start is no longer right"

    def build(self) -> str:
        from vending import db

        schema = f"test_{uuid.uuid4().hex[:8]}"
        self.admin.execute(f"create schema {schema}")
        with open_in(self.url, schema) as conn:
            db.migrate(conn)
        return schema

    def shape_of(self, schema: str) -> str:
        shape = self.admin.execute(_SHAPE, {"s": schema}).fetchone()[0]
        return shape.replace(f"{schema}.", "<schema>.")

    def take(self) -> str:
        return self.free.pop() if self.free else self.build()

    def give_back(self, schema: str, reuse: bool = True) -> None:
        """Reset `schema` for the next test, or drop it.

        Dropped instead when the test failed (a thread it started may still write after the
        reset, as one in tests/test_submit.py can when its join times out), when its objects
        changed, or when checking or resetting it fails for any reason.
        """
        try:
            if not reuse or self.shape_of(schema) != self.shape:
                raise _NotReusable
            statements = [f'delete from {schema}."{t}"' for t in self.order]
            statements += [f'insert into {schema}."{t}" select * from {self.pristine}."{t}"' for t in self.seeded]
            statements += [f"select setval('{schema}.\"{name}\"', {start}, false)" for name, start in self.sequences]
            with self.admin.transaction():
                self.admin.execute(";\n".join(statements))
        except Exception:
            self.admin.execute(f"drop schema if exists {schema} cascade")
            return
        self.free.append(schema)

    def close(self) -> None:
        for schema in [*self.free, self.pristine]:
            self.admin.execute(f"drop schema {schema} cascade")
        self.admin.close()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    """Keep each phase's report on the test, so `schema_url` can tell whether the test failed."""
    report = yield
    setattr(item, f"rep_{report.when}", report)
    return report


@pytest.fixture(scope="session")
def _schema_pool(database_url):
    pool = SchemaPool(database_url)
    yield pool
    pool.close()


@pytest.fixture
def schema_url(database_url, _schema_pool, monkeypatch, request):
    """(url, schema) for a migrated schema with only the seed rows, dropped or reset after the test."""
    from vending import db, slots

    # `migrate` takes one advisory lock for the whole database, so under pytest-xdist every
    # worker's migrations would queue behind every other's. Each test migrates its own schema,
    # so a lock per worker keeps what the lock is for (one migrate at a time in a schema).
    worker = os.environ.get("PYTEST_XDIST_WORKER", "")
    if worker.startswith("gw"):
        monkeypatch.setattr(db, "MIGRATION_LOCK", db.MIGRATION_LOCK + 1 + int(worker[2:]))

    url = database_url
    schema = _schema_pool.take()
    with open_in(url, schema) as conn:
        # A slot assignment as the inventory server would give it (TEST_SLOTS), so tools that read it work
        # as they will live. tests/test_slots.py deletes it to test the refusals.
        conn.execute("insert into slot_assignment (team, device_id, slots, first_read_at, last_checked_at, response)"
                     " values (%s, %s, %s, %s, %s, '{}')",
                     (slots.DEFAULT_TEAM, slots.DEVICE_ID, list(TEST_SLOTS), TEST_CHECKED_AT, TEST_CHECKED_AT))
    yield url, schema
    failed = any(getattr(getattr(request.node, f"rep_{when}", None), "failed", False) for when in ("setup", "call"))
    _schema_pool.give_back(schema, reuse=not failed)


def open_in(url: str, schema: str):
    """A connection in `schema`, in UTC like every connection the app opens (`db.session_options`)."""
    import psycopg

    from vending import db

    return psycopg.connect(url, autocommit=True, connect_timeout=CONNECT_TIMEOUT,
                           options=db.session_options(url, f"-c search_path={schema}", FAST_COMMIT))


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
