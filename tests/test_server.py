"""Tests through a real MCP client session, in memory: what the agent would see."""

import importlib
import uuid
import time
from datetime import datetime, timezone

import anyio
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from tests.conftest import open_in, store_poll
from vending import server
from vending.config import set_halted

WRITE_ARGS = {
    "submit_desired_state": {"desired": {"slots": [{"slot": "15", "keep": True}]}},
    "set_price": {"slot": "15", "sale_price": "2.50"},
    "request_catalog_product": {"name": "Coca-Cola Cherry", "size": "20 oz"},
}


def call(tool: str | None, arguments: dict | None = None):
    """List the tools (tool=None) or call one, through an MCP client session."""
    async def run():
        async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
            if tool is None:
                return await client.list_tools()
            return await client.call_tool(tool, arguments or {})
    return anyio.run(run)


@pytest.fixture
def db(schema_url, monkeypatch):
    monkeypatch.setattr(server, "connect", lambda: open_in(*schema_url))
    monkeypatch.setattr(server, "_conn", None)
    with open_in(*schema_url) as conn:
        yield conn
    if server._conn is not None:
        server._conn.close()


def test_lists_all_eleven_tools():
    tools = {t.name: t for t in call(None).tools}
    assert len(tools) == 11
    assert server.WRITE_TOOLS <= tools.keys()
    assert set(WRITE_ARGS) == server.WRITE_TOOLS
    assert tools["get_machine_state"].outputSchema is not None


def test_machine_state_end_to_end(db):
    store_poll(db, "a", "2026-10-22T12:00:00+00:00", {"15": (250, 100)})
    store_poll(db, "b", "2026-10-29T12:00:00+00:00", {"15": (250, 121)})
    store_poll(db, "c", "2026-10-29T12:00:00+00:00", {"15": (250, 121)}, device_id="other")
    result = call("get_machine_state")
    assert not result.isError
    slot = result.structuredContent["data"][0]
    assert (slot["slot"], slot["price"], slot["units_per_day"]) == ("15", "2.50", "3.00")


@pytest.mark.parametrize("tool", sorted(WRITE_ARGS))
def test_every_write_tool_refuses_when_halted(db, tool):
    result = call(tool, WRITE_ARGS[tool])
    assert not result.isError
    assert result.structuredContent["success"] is False
    assert result.structuredContent["refusals"][0]["code"] == "halted"
    assert db.execute("select tool, outcome from action_log").fetchall() == [(tool, "refused")]


@pytest.mark.parametrize(("tool", "says"), [("set_price", "is dropped"), ("submit_desired_state", "not built yet")])
def test_unbuilt_write_tools_still_log_when_running(db, tool, says):
    set_halted(db, False, "test")
    result = call(tool, WRITE_ARGS[tool])
    assert result.isError and says in result.content[0].text
    assert db.execute("select outcome from action_log where tool = %s", (tool,)).fetchall() == [("failed",)]


def test_sales_history_end_to_end(db):
    store_poll(db, "a", "2026-10-22T12:00:00+00:00", {"15": (250, 100)})
    store_poll(db, "b", "2026-10-29T12:00:00+00:00", {"15": (250, 121)})
    result = call("get_sales_history", {"start": "2026-10-22T12:00:00Z", "end": "2026-10-29T12:00:00Z", "slots": ["15"]})
    assert not result.isError
    assert result.structuredContent["data"] == [
        {"slot": "15", "units": 21, "revenue": "52.50", "revenue_ex_tax": "48.61"}]
    assert db.execute("select tool, outcome from action_log").fetchall() == [("get_sales_history", "done")]


def test_catalog_end_to_end(db):
    db.execute("insert into catalog_products (sku, name, line, flavor, size, last_response)"
               " values ('CCC20', 'Coca-Cola Cherry', 'Coca-Cola', 'Cherry', '20 oz', '{}')")
    db.execute("insert into catalog_syncs (product_count) values (1)")
    result = call("get_catalog", {"search": "cherry"})
    assert not result.isError
    assert result.structuredContent["data"] == [
        {"sku": "CCC20", "name": "Coca-Cola Cherry", "line": "Coca-Cola", "flavor": "Cherry", "size": "20 oz"}]
    assert db.execute("select tool, outcome from action_log").fetchall() == [("get_catalog", "done")]


def test_purchase_requests_end_to_end(db):
    from vending.purchase_requests import record
    body = {"off_cycle": False, "lines": []}
    record(db, "old", datetime(2026, 11, 7, 14, tzinfo=timezone.utc), False, body, "abc")
    record(db, "just_sent", datetime.now(timezone.utc), False, body, "def")
    result = call("get_purchase_requests", {"pending_only": True})
    assert not result.isError
    content = result.structuredContent
    # Neither has been read by the sync; only the old one is flagged.
    assert (content["count"], content["pending"], content["not_checked_recently"]) == (2, 2, ["old"])
    assert [r["plan_hash"] for r in content["data"]] == ["def", "abc"]
    assert db.execute("select tool, outcome from action_log").fetchall() == [("get_purchase_requests", "done")]


def test_the_tool_is_logged_like_other_reads(db):
    result = call("get_project_rules", {"topic": "../money"})
    assert result.structuredContent["success"] is False
    result = call("get_project_rules", {"topic": "money"})
    assert result.structuredContent["text"].startswith("# Money")
    assert db.execute("select kind, tool, outcome from action_log order by id").fetchall() == [
        ("read", "get_project_rules", "failed"), ("read", "get_project_rules", "done")]

def test_reads_are_logged_with_their_arguments_and_result(db):
    store_poll(db, "a", "2026-10-29T12:00:00+00:00", {"15": (250, 100)})
    call("get_machine_state")
    call("get_pnl", {"start": "2026-10-01"})
    rows = db.execute("select kind, tool, outcome, arguments, result->'data'->0->>'price'"
                      " from action_log order by id").fetchall()
    assert rows[0] == ("read", "get_machine_state", "done", {}, "2.50")
    assert rows[1][:4] == ("read", "get_pnl", "done", {"start": "2026-10-01", "end": None})


def test_a_read_with_no_data_is_logged_as_failed(db):
    result = call("get_machine_state")
    assert result.structuredContent["success"] is False
    assert db.execute("select outcome from action_log").fetchall() == [("failed",)]


def test_the_connection_is_reused_and_reopened(db):
    call("get_account_balance")
    first = server._conn
    call("get_account_balance")
    assert server._conn is first
    first.close()
    call("get_account_balance")
    assert server._conn is not first and not server._conn.closed


def test_built_only_hides_the_stubs(monkeypatch):
    monkeypatch.setenv("VENDING_TOOLS", "built")
    try:
        importlib.reload(server)
        assert [t.name for t in call(None).tools] == ["get_machine_state", "get_sales_history", "get_catalog",
                                                      "get_account_balance", "preview_desired_state",
                                                      "get_purchase_requests", "get_pnl", "get_project_rules",
                                                      "request_catalog_product"]
    finally:
        monkeypatch.delenv("VENDING_TOOLS")
        importlib.reload(server)
    assert len(call(None).tools) == 11


def test_parallel_calls_do_not_collide(db):
    store_poll(db, "a", "2026-10-29T12:00:00+00:00", {"15": (250, 100)})

    async def run():
        results = []

        async def one(client, i):
            if i % 3 == 0:
                results.append(await client.call_tool("set_price", WRITE_ARGS["set_price"]))
            else:
                results.append(await client.call_tool("get_machine_state", {}))

        async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
            with anyio.fail_after(20):
                async with anyio.create_task_group() as group:
                    for i in range(30):
                        group.start_soon(one, client, i)
        return results

    results = anyio.run(run)
    assert len(results) == 30 and not any(r.isError for r in results)
    assert db.execute("select count(*) from action_log").fetchone()[0] == 30


def test_a_connection_that_died_is_replaced(db):
    call("get_account_balance")
    db.execute("select pg_terminate_backend(%s)", (server._conn.info.backend_pid,))
    time.sleep(0.2)
    assert "no opening entry" in call("get_account_balance").content[0].text


def test_calls_with_bad_arguments_are_logged(db):
    assert call("get_sales_history", {"start": "yesterday", "end": "today"}).isError
    assert db.execute("select kind, tool, outcome from action_log").fetchall() == [
        ("read", "get_sales_history", "refused")]


def test_startup_refuses_a_schema_missing_migrations(database_url, monkeypatch):
    schema = f"test_{uuid.uuid4().hex[:8]}"
    with open_in(database_url, "public") as admin:
        admin.execute(f"create schema {schema}")
        try:
            monkeypatch.setattr(server, "connect", lambda: open_in(database_url, schema))
            monkeypatch.setattr(server.mcp, "run", lambda: pytest.fail("started on an unmigrated schema"))
            with pytest.raises(SystemExit) as stopped:
                server.main()
            assert "001_polls.sql" in str(stopped.value) and "python -m vending.db migrate" in str(stopped.value)
            # It only looked: nothing was created.
            with open_in(database_url, schema) as conn:
                assert conn.execute("select to_regclass('schema_migrations')").fetchone() == (None,)
        finally:
            admin.execute(f"drop schema {schema} cascade")


def test_startup_runs_on_a_migrated_schema(schema_url, monkeypatch):
    started = []
    monkeypatch.setattr(server, "connect", lambda: open_in(*schema_url))
    monkeypatch.setattr(server.mcp, "run", lambda: started.append(True))
    server.main()
    assert started == [True]
