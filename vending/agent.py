"""The weekly agent run (#39): one cycle that drives the tools and ends with a submitted request or a skip.

    python -m vending.agent              # a dry run: every write tool is hidden, nothing is sent
    python -m vending.agent --live       # the real run: the model may call submit_desired_state

One run:

1. If the halt switch is on, the run is recorded as `halted` and nothing else happens.
2. It starts our MCP server (`python -m vending.server`, `VENDING_TOOLS=built`) over stdio and
   offers the model the tools the server lists. A dry run hides every write tool. A live run
   refuses to start while `submit_desired_state` isn't built.
3. The model gets the time, the week's rhythm and its own notes from the last run. It calls tools
   until it answers with a decision (submit or skip), a weekly update for the team and notes for
   next week. Every request goes through `vending.harness`, so its cost is in the ledger before it
   is sent and the run and daily caps hold.
4. The run is recorded in `agent_runs`: `submitted` only when a `submit_desired_state` call
   succeeded, `skipped` when it decided not to (or it was a dry run), `failed` when it hit a cap,
   ran out of turns or broke.

The model, effort and caps are placeholders until the team decides them (see #39 and #51).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import anyio
import psycopg

from vending import agent_runs, db
from vending.config import is_halted
from vending.harness import EASTERN, Harness, HarnessError

DEFAULT_MODEL = "claude-opus-5-5"   # pending a team decision
DEFAULT_EFFORT = "medium"
MAX_TOKENS = 16_000
MAX_TURNS = 15                      # tool rounds; on the last one the model must answer

# The same set as `vending.server.WRITE_TOOLS` (a test checks): a dry run never offers these.
WRITE_TOOLS = frozenset({"submit_desired_state", "set_price", "request_catalog_product"})
SUBMIT = "submit_desired_state"
PREVIEW = "preview_desired_state"

SYSTEM = """\
You run the 20 vending-machine slots a student team operates as a business, one week at a time. \
Profit is revenue (ex 8% sales tax) minus what the drinks cost minus $25 for each midweek visit \
minus what you cost to run, so don't buy what won't sell and don't run out of what does.

The week: the main run is Thursday, before the midnight cutoff for Friday's restock. A student \
worker loads the machine at the free Friday visit. Prices change only at a visit, by hand: set \
sale_price in the plan. A midweek visit (off_cycle) costs $25 and is worth it only when the sales \
it saves are worth more.

Use the tools to look at the machine, sales, catalog, balance and open requests. Before you \
submit, call preview_desired_state with the exact plan and fix every refusal it lists. Submit at \
most once, with the same plan you previewed and its plan_hash as expected_plan_hash. If no change \
is worth what it costs, skip the week. A null in a tool's reply means unknown: never treat it as zero.

When you're done, answer with:
- decision: "submit" if you submitted (or, on a dry run, would submit the plan you previewed), \
"skip" if not.
- reason: one sentence on why.
- update: a short weekly update for the team, under 150 words: what happened, what you're \
changing and why. Plain language; operations should act on it without asking.
- notes: what you want to remember next week, under 200 words."""

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["submit", "skip"]},
        "reason": {"type": "string"},
        "update": {"type": "string"},
        "notes": {"type": "string"},
    },
    "required": ["decision", "reason", "update", "notes"],
    "additionalProperties": False,
}


@dataclass
class Outcome:
    status: str                         # one of agent_runs.ENDINGS
    detail: str
    answer: dict | None = None
    plan: dict | None = None            # the last plan that previewed ok, or the one submitted
    request_id: str | None = None
    tool_calls: list[str] = field(default_factory=list)


def tool_definitions(listed: list[Any], *, live: bool) -> list[dict]:
    """The server's tools as Anthropic tool definitions; a dry run drops every write tool."""
    tools = []
    for t in listed:
        if not live and t.name in WRITE_TOOLS:
            continue
        tools.append({"name": t.name, "description": t.description or "", "input_schema": t.inputSchema})
    return tools


def _result_text(result: Any) -> tuple[str, dict | None]:
    """(text for the model, structured reply if the server sent one)."""
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict) and set(structured) == {"result"} and isinstance(structured["result"], dict):
        structured = structured["result"]    # FastMCP wraps a non-object return type
    text = "".join(getattr(c, "text", "") for c in getattr(result, "content", None) or [])
    if not text and structured is not None:
        text = json.dumps(structured, default=str)
    return text or "(no reply)", structured if isinstance(structured, dict) else None


def last_notes(conn: psycopg.Connection) -> str:
    """The notes from the latest finished live or dry run, for the model to read."""
    row = conn.execute("select notes from agent_runs where mode in ('live', 'dry-run') and notes is not null"
                       " and status <> 'running' order by started_at desc, id desc limit 1").fetchone()
    return row[0] if row and row[0] else "(No notes yet: this is the first run.)"


def brief(now: datetime, notes: str, *, live: bool) -> str:
    mode = ("This is the live run: submit_desired_state sends a real purchase request." if live else
            "This is a dry run: the write tools are hidden. Preview the plan you would submit, then answer.")
    return (f"Now: {now.astimezone(EASTERN):%A %d %B %Y %H:%M} Eastern.\n{mode}\n\n"
            f"Your notes from the last run:\n{notes}")


async def converse(harness: Harness, call_tool: Callable[[str, dict], Any], tools: list[dict], *,
                   model: str, effort: str, opening: str, live: bool, max_turns: int = MAX_TURNS,
                   label: str = "") -> Outcome:
    """The tool loop. `call_tool(name, arguments)` is awaited and returns an MCP `CallToolResult`."""
    messages: list[dict] = [{"role": "user", "content": opening}]
    out = Outcome("failed", "")
    submitted = False
    for turn in range(max_turns + 1):
        request = {"model": model, "max_tokens": MAX_TOKENS, "system": SYSTEM, "messages": messages,
                   "tools": tools, "tool_choice": {"type": "auto" if turn < max_turns else "none"},
                   "output_config": {"effort": effort, "format": {"type": "json_schema", "schema": ANSWER_SCHEMA}}}
        try:
            response = await anyio.to_thread.run_sync(
                lambda: harness.call(request, label=f"{label} turn {turn + 1}".strip()))
        except HarnessError as error:      # CapReached included: nothing more can be paid for
            out.detail = f"Stopped on turn {turn + 1}: {error}"
            return out
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "tool_use":
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                out.tool_calls.append(block.name)
                if block.name == SUBMIT and (not live or submitted):
                    text, failed = ("Not sent: " + ("this is a dry run." if not live else
                                    "you already submitted once this run.")), True
                elif not any(t["name"] == block.name for t in tools):
                    text, failed = f"There is no tool called {block.name}.", True
                else:
                    result = await call_tool(block.name, dict(block.input or {}))
                    text, reply = _result_text(result)
                    failed = bool(getattr(result, "isError", False)) or (reply is not None and reply.get("success") is False)
                    if block.name == PREVIEW and reply and reply.get("ok"):
                        out.plan = dict(block.input.get("desired") or {}, plan_hash=reply.get("plan_hash"))
                    if block.name == SUBMIT:
                        submitted = True        # sent, whatever came back: never send twice
                        if not failed:
                            out.plan = dict(block.input.get("desired") or {})
                            out.request_id = (reply or {}).get("request_id")
                        else:
                            out.detail = f"submit_desired_state didn't succeed: {text[:300]}"
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": text, "is_error": failed})
            messages.append({"role": "user", "content": results})
            continue
        if response.stop_reason == "refusal":
            out.detail = "The model declined to answer (refusal)."
            return out
        text = "".join(b.text for b in response.content if b.type == "text")
        try:
            out.answer = json.loads(text)
        except json.JSONDecodeError:
            out.detail = f"The answer wasn't the expected JSON (stop_reason {response.stop_reason})."
            return out
        decision = out.answer.get("decision")
        if submitted and out.request_id is not None:
            out.status, out.detail = "submitted", out.answer.get("reason", "")
        elif submitted:
            out.status = "failed"      # a submit was attempted and didn't succeed; detail says why
            out.detail = out.detail or "submit_desired_state was called but returned no request id."
        elif decision == "submit" and live:
            out.status, out.detail = "failed", "It said submit but never called submit_desired_state."
        elif decision == "submit":
            out.status = "skipped"
            out.detail = ("Dry run: would have submitted the previewed plan. " if out.plan else
                          "Dry run: said submit but no plan previewed ok. ") + out.answer.get("reason", "")
        else:
            out.status, out.detail = "skipped", out.answer.get("reason", "")
        return out
    out.detail = "Ran out of turns without answering."
    return out


def server_env() -> dict[str, str]:
    return {**os.environ, "VENDING_TOOLS": "built"}


async def run(*, live: bool, model: str = DEFAULT_MODEL, effort: str = DEFAULT_EFFORT,
              connect: Callable[[], psycopg.Connection] = db.connect, client: Any = None,
              now: Callable[[], datetime] | None = None, open_tools: Callable | None = None,
              max_turns: int = MAX_TURNS) -> tuple[int, Outcome]:
    """One weekly run, recorded in `agent_runs`. Returns (run id, outcome)."""
    now = now or (lambda: datetime.now(timezone.utc))
    mode = "live" if live else "dry-run"
    with connect() as conn:
        run_id = agent_runs.start(conn, mode=mode, model=model)
        if is_halted(conn):
            out = Outcome("halted", "The halt switch is on: nothing was read, called or sent.")
            agent_runs.finish(conn, run_id, out.status, detail=out.detail, cost_usd=Decimal(0))
            return run_id, out
        notes = last_notes(conn)
    harness = None
    out = Outcome("failed", "")
    try:
        if client is None:
            import anthropic
            client = anthropic.Anthropic()
        harness = Harness(client, connect, purpose="live", by="agent")
        async with (open_tools or _open_server)() as (listed, call_tool):
            names = {t.name for t in listed}
            if live and SUBMIT not in names:
                out.detail = "submit_desired_state isn't built on this server, so a live run can't submit. Use a dry run."
            else:
                out = await converse(harness, call_tool, tool_definitions(listed, live=live), model=model,
                                     effort=effort, opening=brief(now(), notes, live=live), live=live,
                                     max_turns=max_turns, label=f"agent run {run_id}")
    except Exception as error:   # record the run whatever broke; the ledger already holds what was spent
        out.detail = f"{type(error).__name__}: {error}"[:1000]
    finally:
        spent = harness.spent if harness is not None else Decimal(0)
        answer = out.answer or {}
        with connect() as conn:
            agent_runs.finish(conn, run_id, out.status, plan=out.plan, update=answer.get("update"),
                              notes=answer.get("notes"), detail=out.detail, cost_usd=spent,
                              request_id=out.request_id)
    return run_id, out


def _open_server():
    """Start `python -m vending.server` over stdio; yields (tools, call_tool)."""
    from contextlib import asynccontextmanager

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    @asynccontextmanager
    async def opened():
        params = StdioServerParameters(command=sys.executable, args=["-m", "vending.server"], env=server_env())
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            listed = (await session.list_tools()).tools
            yield listed, session.call_tool
    return opened()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m vending.agent", description="Run the agent's weekly cycle once.")
    parser.add_argument("--live", action="store_true", help="let it submit a real purchase request")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--effort", default=DEFAULT_EFFORT, choices=["low", "medium", "high", "max"])
    parser.add_argument("--max-turns", type=int, default=MAX_TURNS)
    args = parser.parse_args(argv)
    run_id, out = anyio.run(lambda: run(live=args.live, model=args.model, effort=args.effort,
                                        max_turns=args.max_turns))
    print(f"run {run_id}: {out.status}. {out.detail}")
    if out.answer:
        print(f"\nUpdate for the team:\n{out.answer.get('update', '')}")
    return 0 if out.status in ("submitted", "skipped", "halted") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
