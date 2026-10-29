"""The agent's tools. Each module holds one tool's logic as a plain function that
takes a database connection; `vending.server` registers them with MCP.

Every tool replies with the build plan's envelope, `{success, message, count, data}`,
where a failure message names the tool that fixes it.
"""

from __future__ import annotations

from pydantic import BaseModel


class Reply(BaseModel):
    success: bool
    message: str
    count: int = 0


class RefusalOut(BaseModel):
    code: str
    message: str
    slot: str | None = None


class WriteReply(Reply):
    """What every write tool returns. On a refusal, `refusals` lists every reason at once."""
    refusals: list[RefusalOut] = []


class NotBuilt(Exception):
    """A registered tool whose logic doesn't exist yet. The client sees this as a tool error."""

    def __init__(self, tool: str, waiting_on: str):
        super().__init__(f"{tool} is not built yet ({waiting_on}).")
