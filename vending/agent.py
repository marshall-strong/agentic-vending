"""The weekly agent run (#39): one cycle that drives the tools and ends with a submitted request or a skip.

    python -m vending.agent              # a dry run: every write tool is hidden, no write is sent
    python -m vending.agent --live       # the real run: the model may call submit_desired_state

One run:

1. If the halt switch is on, the run is recorded as `halted` and nothing else happens. It is
   checked again before every model request, so turning it on mid-run stops the run there.
2. It starts our MCP server (`python -m vending.server`, `VENDING_TOOLS=built`) over stdio and
   offers the model the tools the server lists. A dry run hides every write tool. A live run
   refuses to start while `submit_desired_state` isn't built, sends only the plan its last preview
   accepted (with that preview's plan hash), and sends at most once.
3. The model gets the time, the week's rhythm and its own notes from the last run. It calls tools
   until it answers with a decision (submit or skip), a weekly update for the team and notes for
   next week. Every request goes through `vending.harness`, so its cost is in the ledger before it
   is sent and the run and daily caps hold.
4. The run is recorded in `agent_runs`: `submitted` once a `submit_desired_state` call
   succeeded (its request id is stored straight away, and the run stays `submitted` whatever
   stops it later), `skipped` when it decided not to (or it was a dry run), `failed` when it hit a cap,
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
from psycopg.types.json import Jsonb

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
    request_id: str | None = None       # set once a submit succeeded: the run is `submitted` from then on
    submit_sent: bool = False           # a submit reached the server, whatever came back: never send another
    unresolved: str | None = None       # a submit whose result is unknown: money may have been spent
    turn: int = 0                       # the model response being worked through
    plan_turn: int | None = None        # the response whose preview set `plan`: a submit must come later
    tool_calls: list[str] = field(default_factory=list)


UNRESOLVED = ("SUBMISSION UNRESOLVED: the bridge may have accepted it. Check `python -m vending.submissions list` "
              "before anyone submits again. ")


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


def _same_plan(a: Any, b: Any) -> bool:
    """Two `desired` inputs mean the same plan once defaults are filled in."""
    from vending.tools.desired_state import DesiredState
    try:
        return DesiredState.model_validate(a).model_dump() == DesiredState.model_validate(b).model_dump()
    except Exception:
        return False


def last_notes(conn: psycopg.Connection, *, live: bool) -> str:
    """The notes from the latest finished run, labelled with where they came from.

    A live run reads only live runs' notes, so a dry run's "would submit" is never taken as done.
    """
    modes = ["live"] if live else ["live", "dry-run"]
    row = conn.execute("select mode, started_at, notes from agent_runs where mode = any(%s) and notes is not null"
                       " and status <> 'running' order by started_at desc, id desc limit 1", (modes,)).fetchone()
    if not row:
        return "(No notes yet: this is the first run.)"
    mode, started, notes = row
    kind = "the live run" if mode == "live" else "a dry run (nothing it planned was sent)"
    return f"(From {kind} on {started.astimezone(EASTERN):%A %d %B %H:%M} Eastern.)\n{notes}"


def brief(now: datetime, notes: str, *, live: bool) -> str:
    mode = ("This is the live run: submit_desired_state sends a real purchase request." if live else
            "This is a dry run: the write tools are hidden. Preview the plan you would submit, then answer.")
    return (f"Now: {now.astimezone(EASTERN):%A %d %B %Y %H:%M} Eastern.\n{mode}\n\n"
            f"Your notes from the last run:\n{notes}")


async def converse(harness: Harness, call_tool: Callable[[str, dict], Any], tools: list[dict], out: Outcome, *,
                   model: str, effort: str, opening: str, live: bool, halted: Callable[[], bool] = lambda: False,
                   on_submit: Callable[[Outcome], None] = lambda out: None, max_turns: int = MAX_TURNS,
                   label: str = "") -> None:
    """The tool loop, filling in `out` as it goes so nothing it learned is lost if it stops early.

    `call_tool(name, arguments)` is awaited and returns an MCP `CallToolResult`. `halted()` is
    checked before every model request. `on_submit(out)` runs straight after a submit succeeds.
    """
    messages: list[dict] = [{"role": "user", "content": opening}]
    schemas = {t["name"]: t["input_schema"] for t in tools}
    for turn in range(max_turns + 1):
        if await anyio.to_thread.run_sync(halted):
            out.status, out.detail = "halted", f"The halt switch was turned on; stopped before turn {turn + 1}."
            return
        request = {"model": model, "max_tokens": MAX_TOKENS, "system": SYSTEM, "messages": messages,
                   "tools": tools, "tool_choice": {"type": "auto" if turn < max_turns else "none"},
                   "cache_control": {"type": "ephemeral"},     # each turn re-sends the history: read it from cache
                   "output_config": {"effort": effort, "format": {"type": "json_schema", "schema": ANSWER_SCHEMA}}}
        try:
            response = await anyio.to_thread.run_sync(
                lambda: harness.call(request, label=f"{label} turn {turn + 1}".strip()))
        except HarnessError as error:      # CapReached included: nothing more can be paid for
            out.status, out.detail = "failed", f"Stopped on turn {turn + 1}: {error}"
            return
        messages.append({"role": "assistant", "content": response.content})
        out.turn = turn
        if response.stop_reason == "tool_use":
            results = []
            for block in response.content:
                if block.type == "tool_use":
                    out.tool_calls.append(block.name)
                    text, failed = await _use(block, call_tool, schemas, out, live=live, on_submit=on_submit)
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": text,
                                    "is_error": failed})
            messages.append({"role": "user", "content": results})
            continue
        if response.stop_reason == "refusal":
            category = getattr(getattr(response, "stop_details", None), "category", None)
            out.status, out.detail = "failed", f"The model declined to answer (refusal, category {category})."
            return
        text = "".join(b.text for b in response.content if b.type == "text")
        try:
            out.answer = json.loads(text)
        except json.JSONDecodeError:
            out.status, out.detail = "failed", f"The answer wasn't the expected JSON (stop_reason {response.stop_reason})."
            return
        decision, reason = out.answer.get("decision"), out.answer.get("reason", "")
        if out.request_id is not None:
            out.status, out.detail = "submitted", reason
        elif out.submit_sent:
            out.status = "failed"       # the detail already says why
        elif decision == "submit" and live:
            out.status, out.detail = "failed", "It said submit but never called submit_desired_state."
        elif decision == "submit":
            out.status = "skipped"
            out.detail = ("Dry run: would have submitted the previewed plan. " if out.plan else
                          "Dry run: said submit but no plan previewed ok. ") + reason
        else:
            out.status, out.detail = "skipped", reason
        return
    out.status, out.detail = "failed", "Ran out of turns without answering."


async def _use(block: Any, call_tool: Callable[[str, dict], Any], schemas: dict, out: Outcome, *, live: bool,
               on_submit: Callable[[Outcome], None]) -> tuple[str, bool]:
    """Run one tool call the model asked for: (text for the model, is_error)."""
    name, arguments = block.name, dict(block.input or {})
    if name not in schemas:
        return f"There is no tool called {name}.", True
    if not live and name in WRITE_TOOLS:          # never offered; refused again in case the model makes it up
        return "Not sent: this is a dry run.", True
    if name == SUBMIT:
        if out.submit_sent:
            return "Not sent: you already submitted once this run.", True
        previewed = {k: v for k, v in (out.plan or {}).items() if k != "plan_hash"}
        if out.plan is None or not _same_plan(arguments.get("desired"), previewed):
            return ("Not sent: submit exactly the plan your last preview_desired_state accepted "
                    "(preview it first if you changed anything)."), True
        if out.plan_turn == out.turn:
            return ("Not sent: read the preview's cost, warnings and refusals first, "
                    "then submit in your next turn."), True
        if "expected_plan_hash" in (schemas[name].get("properties") or {}):
            arguments["expected_plan_hash"] = out.plan["plan_hash"]   # refused by the server if anything moved
        out.submit_sent = True                    # set before sending: whatever happens next, never send again
        try:
            result = await call_tool(name, arguments)
        except BaseException as error:            # the server died or we were stopped mid-send: it may have gone
            out.unresolved = f"the submit call raised {type(error).__name__}: {error}"[:500]
            raise
    else:
        result = await call_tool(name, arguments)
    text, reply = _result_text(result)
    failed = bool(getattr(result, "isError", False)) or (reply is not None and reply.get("success") is False)
    if name == PREVIEW and not out.submit_sent:
        if reply and reply.get("ok") and not failed and isinstance(arguments.get("desired"), dict):
            out.plan = {**arguments["desired"], "plan_hash": reply.get("plan_hash")}
            out.plan_turn = out.turn
    if name == SUBMIT:
        reply = reply or {}
        if not failed and reply.get("request_id"):
            out.request_id = reply["request_id"]
            out.plan = {**arguments["desired"], "plan_hash": reply.get("plan_hash") or out.plan["plan_hash"]}
            on_submit(out)
        elif failed and (getattr(result, "isError", False) or reply.get("status") == "unknown"):
            out.unresolved = text[:500]
            out.detail = UNRESOLVED + out.unresolved
        elif failed:
            out.detail = f"submit_desired_state was refused, nothing was bought: {text[:300]}"
        else:
            out.unresolved = text[:500]
            out.detail = UNRESOLVED + "It reported success with no request id: " + out.unresolved
    return text, failed


def server_env() -> dict[str, str]:
    """The server's environment: ours without the model key, which it never needs."""
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    return {**env, "VENDING_TOOLS": "built"}


def _record_submit(connect: Callable[[], psycopg.Connection], run_id: int, out: Outcome) -> None:
    """Store the request id the moment a submit succeeds, before another model call can fail."""
    with connect() as conn:
        conn.execute("update agent_runs set request_id = %s, plan = %s where id = %s and status = 'running'",
                     (out.request_id, Jsonb(out.plan, dumps=agent_runs._dumps), run_id))


def _settle(out: Outcome) -> None:
    """A run that submitted is `submitted`, whatever stopped it after; an unknown submit says so first."""
    if out.request_id is not None and out.status != "submitted":
        out.detail = f"Submitted purchase request {out.request_id}; afterwards: {out.detail or 'stopped'}"
        out.status = "submitted"
    elif out.unresolved is not None and not out.detail.startswith(UNRESOLVED):
        out.status, out.detail = "failed", UNRESOLVED + out.unresolved + (f" Then: {out.detail}" if out.detail else "")


async def run(*, live: bool, model: str = DEFAULT_MODEL, effort: str = DEFAULT_EFFORT,
              connect: Callable[[], psycopg.Connection] = db.connect, client: Any = None,
              now: Callable[[], datetime] | None = None, open_tools: Callable | None = None,
              max_turns: int = MAX_TURNS) -> tuple[int, Outcome]:
    """One weekly run, recorded in `agent_runs`. Returns (run id, outcome)."""
    now = now or (lambda: datetime.now(timezone.utc))
    with connect() as conn:
        run_id = agent_runs.start(conn, mode="live" if live else "dry-run", model=model)
    harness = None
    out = Outcome("failed", "")

    def halted() -> bool:
        with connect() as c:
            return is_halted(c)

    try:
        if halted():
            out.status, out.detail = "halted", "The halt switch is on: nothing was read, called or sent."
            return run_id, out
        with connect() as conn:
            notes = last_notes(conn, live=live)
        if client is None:
            import anthropic
            client = anthropic.Anthropic()
        harness = Harness(client, connect, purpose="live", by="agent")
        async with (open_tools or _open_server)() as (listed, call_tool):
            if live and SUBMIT not in {t.name for t in listed}:
                out.detail = "submit_desired_state isn't built on this server, so a live run can't submit. Use a dry run."
            else:
                await converse(harness, call_tool, tool_definitions(listed, live=live), out, model=model,
                               effort=effort, opening=brief(now(), notes, live=live), live=live, halted=halted,
                               on_submit=lambda o: _record_submit(connect, run_id, o), max_turns=max_turns,
                               label=f"agent run {run_id}")
    except Exception as error:   # record the run whatever broke; the ledger already holds what was spent
        out.status, out.detail = "failed", f"{type(error).__name__}: {error}"[:1000]
    except BaseException as error:   # Ctrl-C or cancellation: record it, then let it through
        out.status, out.detail = "failed", f"Interrupted ({type(error).__name__})."
        raise
    finally:
        _settle(out)
        answer = out.answer or {}
        with connect() as conn:
            agent_runs.finish(conn, run_id, out.status, plan=out.plan, update=answer.get("update"),
                              notes=answer.get("notes"), detail=out.detail,
                              cost_usd=harness.spent if harness is not None else Decimal(0),
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
    parser.add_argument("--effort", default=DEFAULT_EFFORT, choices=["low", "medium", "high", "xhigh", "max"])
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
