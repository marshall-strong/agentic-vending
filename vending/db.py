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
    """Apply every migration not yet applied. Returns the names applied."""
    with conn.transaction():
        conn.execute(
            "create table if not exists schema_migrations ("
            " version text primary key,"
            " applied_at timestamptz not null default now())"
        )
    done = {row[0] for row in conn.execute("select version from schema_migrations")}
    applied = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        if path.name in done:
            continue
        with conn.transaction():
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
