"""Database connection and migrations.

Migrations are plain SQL files in `db/migrations/`, applied in name order, each
in its own transaction, and recorded in `schema_migrations` so each runs once.

    python -m vending.db migrate
"""

from __future__ import annotations

import sys

import psycopg

from vending import settings

MIGRATIONS = settings.REPO_ROOT / "db" / "migrations"
# Any fixed number will do; it only has to be the same for every run.
MIGRATION_LOCK = 4_200_001


def connect() -> psycopg.Connection:
    url = settings.require(
        "DATABASE_URL",
        "Copy the DATABASE_URL line from .env.example into .env "
        "(local: start the database with `docker compose up -d db`).",
    )
    # Autocommit, so every `conn.transaction()` block is a real transaction that
    # commits on its own. Without it, psycopg wraps everything in one implicit
    # transaction and a long backfill would lose all its work on any error.
    return psycopg.connect(url, autocommit=True)


def migrate(conn: psycopg.Connection) -> list[str]:
    """Apply every migration not yet applied. Returns the names applied.

    Two runs can start together (the hourly sync and a manual one), so each
    transaction takes the same advisory lock and re-checks before applying.
    """
    with conn.transaction():
        conn.execute("select pg_advisory_xact_lock(%s)", (MIGRATION_LOCK,))
        conn.execute(
            "create table if not exists schema_migrations ("
            " version text primary key,"
            " applied_at timestamptz not null default now())"
        )
    applied = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        with conn.transaction():
            conn.execute("select pg_advisory_xact_lock(%s)", (MIGRATION_LOCK,))
            if conn.execute("select 1 from schema_migrations where version = %s", (path.name,)).fetchone():
                continue
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute("insert into schema_migrations (version) values (%s)", (path.name,))
        applied.append(path.name)
    return applied


def main(argv: list[str]) -> int:
    if argv != ["migrate"]:
        print("usage: python -m vending.db migrate", file=sys.stderr)
        return 2
    with connect() as conn:
        applied = migrate(conn)
    print("applied: " + ", ".join(applied) if applied else "already up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
