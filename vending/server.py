"""The MCP server: the agent's 10 tools, over stdio.

    python -m vending.server

Each tool's logic is in `vending.tools`; this file only registers it. Every
tool definition is sent on every model request and paid for in tokens, so keep
the docstrings (which become the descriptions) short. For the live run, set
`VENDING_TOOLS=built` so only tools that work are registered.

Every call is logged to `action_log`. Read tools go through `_read`. Write
tools go through `_write`, which runs them through `vending.write_gate.run_write`:
halt check, then the write, then the log row, in one transaction. Don't register
a write tool any other way; `tests/test_server.py` checks that every tool in
`WRITE_TOOLS` refuses when halted.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from typing import Any, TypeVar

import psycopg
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel
from pydantic_core import to_jsonable_python

from vending import db, settings
from vending.tools import NotBuilt, Reply, RefusalOut, WriteReply, machine_state, sales_history, stubs
from vending.tools.desired_state import DesiredState
from vending.write_gate import Refused, log, run_write

T = TypeVar("T", bound=BaseModel)

mcp = FastMCP("vending")

# Tests swap this to point at a throwaway schema.
connect: Callable[[], psycopg.Connection] = db.connect

WRITE_TOOLS = {"submit_desired_state", "set_price", "request_catalog_product"}

TOOLS = settings.get("VENDING_TOOLS", "all")
if TOOLS not in ("all", "built"):
    raise settings.SettingsError(f"VENDING_TOOLS must be 'all' or 'built', not {TOOLS!r}")


def _tool(*, built: bool) -> Callable[[Callable], Callable]:
    """Register a tool, unless it's a stub and VENDING_TOOLS=built."""
    if built or TOOLS == "all":
        return mcp.tool()
    return lambda fn: fn


def _read(tool: str, fn: Callable[..., T], **arguments: Any) -> T:
    logged = to_jsonable_python(arguments)
    with connect() as conn:
        try:
            result = fn(conn, **arguments)
        except NotBuilt as error:
            log(conn, "read", tool, logged, "failed", str(error))
            raise ToolError(str(error)) from None
        except Exception as error:
            log(conn, "read", tool, logged, "failed", f"{type(error).__name__}: {error}")
            raise
        log(conn, "read", tool, logged, "done", result.message, result)
        return result


def _write(tool: str, arguments: dict[str, Any], action: stubs.Action) -> WriteReply:
    try:
        with connect() as conn:
            return run_write(conn, tool, to_jsonable_python(arguments), action)
    except Refused as refusal:
        return WriteReply(
            success=False,
            message=f"{tool} refused; nothing was written.",
            count=len(refusal.refusals),
            refusals=[RefusalOut(code=r.code, message=r.message, slot=r.slot) for r in refusal.refusals],
        )
    except NotBuilt as error:
        raise ToolError(str(error)) from None


@_tool(built=True)
def get_machine_state() -> machine_state.MachineState:
    """Each of our 20 slots: price, lifetime units, units sold and units/day over the last 7 days, from the latest stored poll."""
    return _read("get_machine_state", machine_state.get_machine_state)


@_tool(built=False)  # set built=True when sales_history.py has its logic
def get_sales_history(start: datetime, end: datetime, slots: list[str] | None = None) -> sales_history.SalesHistory:
    """Units and ex-tax revenue per slot between two times (UTC). Omit slots for all 20."""
    return _read("get_sales_history", sales_history.get_sales_history, start=start, end=end, slots=slots)


@_tool(built=False)
def get_catalog(search: str | None = None) -> Reply:
    """Approved single-item products, our last unit cost and sourcing for each. Optional text search."""
    return _read("get_catalog", stubs.get_catalog, search=search)


@_tool(built=False)
def get_account_balance() -> Reply:
    """Balance from the ledger, sales tax set aside, and the spendable balance."""
    return _read("get_account_balance", stubs.get_account_balance)


@_tool(built=False)
def preview_desired_state(desired: DesiredState) -> Reply:
    """Plan a desired state for all 20 slots without submitting: lines, cost, floors and every refusal."""
    return _read("preview_desired_state", stubs.preview_desired_state, desired=desired)


@_tool(built=False)
def submit_desired_state(desired: DesiredState) -> WriteReply:
    """Submit a desired state for all 20 slots as a purchase request. Same input as preview_desired_state."""
    return _write("submit_desired_state", {"desired": desired}, stubs.submit_desired_state(desired))


@_tool(built=False)
def get_purchase_requests(pending_only: bool = False) -> Reply:
    """Our purchase requests: status, decision note, the plan sent, and per-line receipts."""
    return _read("get_purchase_requests", stubs.get_purchase_requests, pending_only=pending_only)


@_tool(built=False)
def set_price(slot: str, sale_price: str) -> WriteReply:
    """Change one slot's price remotely between visits. sale_price like "2.50"."""
    return _write("set_price", {"slot": slot, "sale_price": sale_price}, stubs.set_price(slot, sale_price))


@_tool(built=False)
def get_pnl(start: date | None = None, end: date | None = None) -> Reply:
    """Profit and loss: ex-tax revenue, cost of goods, visit fees, token spend, inventory at cost."""
    return _read("get_pnl", stubs.get_pnl, start=start, end=end)


@_tool(built=False)
def request_catalog_product(brand: str, flavor: str, size: str) -> WriteReply:
    """Ask the instructors to add a single-item drink to the catalog, e.g. Coca-Cola, Cherry, 20 oz."""
    return _write("request_catalog_product", {"brand": brand, "flavor": flavor, "size": size},
                  stubs.request_catalog_product(brand, flavor, size))


def main() -> None:
    with db.connect() as conn:
        db.migrate(conn)
    mcp.run()  # stdio


if __name__ == "__main__":
    main()
