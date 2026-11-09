"""Tests for the guard that keeps database tests off remote servers. Every URL
here is made up, and nothing connects."""

import pytest

pytest.importorskip("psycopg")

# This imports psycopg too, so it comes after the skip.
from tests.conftest import local_database_url  # noqa: E402
from vending import settings  # noqa: E402


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://user:made-up@127.0.0.1:55433/db",
        "postgresql://user:made-up@localhost/db",
        "postgresql://user:made-up@[::1]:5432/db",
        "postgres://user:made-up@localhost/db",
        "host=localhost dbname=db user=user",
        "postgresql://user:made-up@localhost:5432,127.0.0.1:5433/db",
        "postgresql://user:made-up@localhost/db?hostaddr=127.0.0.1",
    ],
)
def test_a_local_url_is_returned_unchanged(monkeypatch, url):
    monkeypatch.setenv("DATABASE_URL", url)
    try:
        assert local_database_url() == url
    except pytest.skip.Exception as refused:
        # Left alone this would report a skip, and a guard that refuses too
        # much would quietly turn the database tests off.
        pytest.fail(f"refused a local URL: {refused}")


@pytest.mark.parametrize(
    ("url", "named"),
    [
        ("postgresql://user:made-up@db.example.invalid:5432/db?sslmode=require", "db.example.invalid"),
        ("host=db.example.invalid dbname=db user=user", "db.example.invalid"),
        ("postgresql://user:made-up@localhost,db.example.invalid/db", "db.example.invalid"),
        ("postgresql://user:made-up@localhost/db?host=db.example.invalid", "db.example.invalid"),
        ("postgresql://user:made-up@localhost/db?hostaddr=203.0.113.9", "203.0.113.9"),
        # Looks local to anything matching on the start of the name.
        ("postgresql://user:made-up@127.0.0.1.example.invalid/db", "127.0.0.1.example.invalid"),
        ("postgresql://user:made-up@localhost.example.invalid/db", "localhost.example.invalid"),
    ],
)
def test_a_remote_url_is_refused_by_name(monkeypatch, url, named):
    monkeypatch.setenv("DATABASE_URL", url)
    with pytest.raises(pytest.skip.Exception, match="not a local database") as refused:
        local_database_url()
    assert named in str(refused.value)
    assert "made-up" not in str(refused.value)


def test_a_url_with_no_host_is_refused(monkeypatch):
    # `PGHOST` would choose the server, and nothing here checks that.
    monkeypatch.setenv("DATABASE_URL", "postgresql:///db")
    with pytest.raises(pytest.skip.Exception, match="no host"):
        local_database_url()


def test_a_url_that_cannot_be_parsed_is_refused_without_quoting_it(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:made-up@localhost/db?nonsense=1")
    with pytest.raises(pytest.skip.Exception, match="could not be parsed") as refused:
        local_database_url()
    assert "made-up" not in str(refused.value)


def test_no_url_at_all_is_a_skip(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(settings, "_FILE", {})
    with pytest.raises(pytest.skip.Exception, match="DATABASE_URL not set"):
        local_database_url()


def test_a_remote_url_in_the_env_file_is_refused(monkeypatch):
    # The real `.env` is where a remote URL actually comes from.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(settings, "_FILE", {"DATABASE_URL": "postgresql://user:made-up@db.example.invalid/db"})
    with pytest.raises(pytest.skip.Exception, match="not a local database"):
        local_database_url()
