"""Tests through a real MCP client session, in memory: what the agent would see."""

import importlib

import anyio
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from tests.conftest import open_in, store_poll
from vending import server
from vending.config import set_halted

WRITE_ARGS = {
    "submit_desired_state": {"desired": {"slots": [{"slot": "15", "keep": True}]}},
    "set_price": {"slot": "15", "sale_price": "2.50"},
    "request_catalog_product": {"brand": "Coca-Cola", "flavor": "Cherry", "size": "20 oz"},
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
    with open_in(*schema_url) as conn:
        yield conn


def test_lists_all_ten_tools():
    tools = {t.name: t for t in call(None).tools}
    assert len(tools) == 10
    assert server.WRITE_TOOLS <= tools.keys()
    assert set(WRITE_ARGS) == server.WRITE_TOOLS
    assert tools["get_machine_state"].outputSchema is not None


def test_machine_state_end_to_end(db):
    store_poll(db, "a", "2026-10-22T12:00:00+00:00", {"15": (250, 100)})
    store_poll(db, "b", "2026-10-29T12:00:00+00:00", {"15": (250, 121)})
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


@pytest.mark.parametrize("tool", sorted(WRITE_ARGS))
def test_unbuilt_write_tools_still_log_when_running(db, tool):
    set_halted(db, False, "test")
    result = call(tool, WRITE_ARGS[tool])
    assert result.isError and "not built yet" in result.content[0].text
    assert db.execute("select outcome from action_log where tool = %s", (tool,)).fetchall() == [("failed",)]


def test_read_stub_says_not_built_and_is_logged(db):
    result = call("get_account_balance")
    assert result.isError and "not built yet" in result.content[0].text
    assert db.execute("select kind, tool, outcome from action_log").fetchall() == [
        ("read", "get_account_balance", "failed")]


def test_reads_are_logged_with_their_arguments_and_result(db):
    store_poll(db, "a", "2026-10-29T12:00:00+00:00", {"15": (250, 100)})
    call("get_machine_state")
    call("get_pnl", {"start": "2026-10-01"})
    rows = db.execute("select kind, tool, outcome, arguments, result->'data'->0->>'price'"
                      " from action_log order by id").fetchall()
    assert rows[0] == ("read", "get_machine_state", "done", {}, "2.50")
    assert rows[1][:4] == ("read", "get_pnl", "failed", {"start": "2026-10-01", "end": None})


def test_built_only_hides_the_stubs(monkeypatch):
    monkeypatch.setenv("VENDING_TOOLS", "built")
    try:
        importlib.reload(server)
        assert [t.name for t in call(None).tools] == ["get_machine_state"]
    finally:
        monkeypatch.delenv("VENDING_TOOLS")
        importlib.reload(server)
    assert len(call(None).tools) == 10
