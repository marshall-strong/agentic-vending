"""`python -m vending.product_requests`: a person settles a request left `sending` or `unknown`."""

import pytest

from tests.conftest import open_in
from vending import product_requests


@pytest.fixture
def cli(schema_url, monkeypatch):
    def no_migrate(conn):
        raise AssertionError("product_requests must never migrate")

    monkeypatch.setattr(product_requests.db, "connect", lambda: open_in(*schema_url))
    monkeypatch.setattr(product_requests.db, "migrate", no_migrate)
    return product_requests.main


def add(conn, status, bridge_id=None, name="Coca-Cola Cherry"):
    return conn.execute(
        "insert into product_requests (name, size, words, status, bridge_id)"
        " values (%s, '20 oz bottle', '{20,cherry,coca,cola,oz}', %s, %s) returning id",
        (name, status, bridge_id),
    ).fetchone()[0]


def test_list_shows_only_rows_waiting_for_a_person(cli, conn, capsys):
    waiting = add(conn, "unknown")
    add(conn, "pending", "np_1", name="Sprite")
    assert cli(["list"]) == 0
    out = capsys.readouterr().out
    assert f"{waiting}  " in out and "Coca-Cola Cherry" in out and "Sprite" not in out


def test_resolve_with_the_bridge_id_records_it_and_logs_who(cli, conn, capsys):
    row = add(conn, "sending")
    assert cli(["resolve", str(row), "--bridge-id", "np_ABC", "--by", "kevin"]) == 0
    assert conn.execute("select bridge_id, status from product_requests").fetchall() == [("np_ABC", "pending")]
    assert "np_ABC" in capsys.readouterr().out
    assert conn.execute("select kind, detail from action_log where tool = 'product_requests.resolve'").fetchone() == (
        "write", f"request {row} (Coca-Cola Cherry, 20 oz bottle) recorded as filed under np_ABC, pending, by kevin")


def test_resolve_not_filed_removes_the_row(cli, conn):
    row = add(conn, "unknown")
    assert cli(["resolve", str(row), "--not-filed", "--by", "kevin"]) == 0
    assert conn.execute("select count(*) from product_requests").fetchone() == (0,)


def test_a_settled_row_cannot_be_changed(cli, conn, capsys):
    row = add(conn, "pending", "np_1")
    assert cli(["resolve", str(row), "--not-filed", "--by", "kevin"]) == 1
    assert "nothing to settle" in capsys.readouterr().err
    assert conn.execute("select status from product_requests").fetchone() == ("pending",)
    assert conn.execute("select count(*) from action_log where tool = 'product_requests.resolve'").fetchone() == (0,)


def test_a_bridge_id_already_on_another_row_is_refused(cli, conn, capsys):
    add(conn, "pending", "np_1", name="Sprite")
    row = add(conn, "unknown")
    assert cli(["resolve", str(row), "--bridge-id", "np_1", "--by", "kevin"]) == 1
    assert "already on another row" in capsys.readouterr().err
    assert conn.execute("select status from product_requests where id = %s", (row,)).fetchone() == ("unknown",)


def test_resolve_needs_a_name_and_one_outcome(cli):
    with pytest.raises(SystemExit):
        cli(["resolve", "1", "--not-filed"])
    with pytest.raises(SystemExit):
        cli(["resolve", "1", "--by", "kevin"])
