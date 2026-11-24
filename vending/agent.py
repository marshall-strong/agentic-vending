"""The weekly agent run (#39): one cycle that drives the tools and ends with a submitted request or a skip.

    python -m vending.agent              # a dry run: every write tool is hidden, no write is sent
    python -m vending.agent --live       # the real run: the model may call submit_desired_state

One run:

1. If the halt switch is on, the run is recorded as `halted` and nothing else happens. It is
   checked again before every model request, so turning it on mid-run stops the run there.
   Only one run goes at a time, in any mode, from any machine on the same database: a run holds
   the run lease (`vending.run_lease`, a row in the database) from start to end. A dropped or
   killed connection never frees it; the run renews it while it goes, and checks it still holds
   it before every model request and before a purchase request is sent. Each paid model request
   checks it again in the transaction that reserves its budget (`run_lease.hold`, through the
   harness's `hold`): it renews it under the row's lock, and goes only if the lease then outlasts
   the request (`LEASE_MARGIN`). A takeover locks the same row, so the two never interleave. A run
   that lost it stops there and is `failed`, and reserves and sends nothing. A run that finds it
   taken is recorded as `skipped` at once, and its detail names the other run. A scheduled run
   (live or dry) passes `authorize`, which runs in the transaction that takes the lease, once
   it is free and before anything is paid for (`vending.agent_schedule`). The run's ending is
   written to `agent_runs` with `FINISH_RETRIES` more tries if it fails. The whole run has a
   deadline (`RUN_DEADLINE`): past it, the run stops and is `failed`, and the harness sends no more
   requests (it checks the deadline after counting a request's tokens and again just before
   sending). A model request already sent ends first: the harness gives it at most
   `REQUEST_DEADLINE` by the wall clock, and sends it only if the lease outlasts that. A request it
   gave up on keeps its worst case, and while its thread may still be going the run doesn't give
   the lease back (it expires by itself), so no other run's request can overlap it. An MCP request
   has `TOOL_TIMEOUT`, and every database call of the run has `DB_TIMEOUT` (statement and lock), so
   nothing can hold the run, or the lease, for ever.
2. It starts our MCP server (`python -m vending.server`, `VENDING_TOOLS=built`) over stdio and
   offers the model the tools the server lists. A dry run hides every write tool. A live run
   refuses to start while `submit_desired_state` isn't built, sends only the plan its last preview
   accepted (with that preview's plan hash), and sends at most once. Two replies prove no byte of the
   request left, and only they allow one more submit, after a fresh preview: our own `plan_changed`
   refusal (no submission number: nothing was sent) and `not_filed` (connecting to the bridge failed).
   No reply to a POST does, even one shaped like a refusal. Before the model starts, a live run
   settles a submission whose POST got a validated `201` but was never settled
   (`vending.submissions.check`); any other open one blocks orders until a person runs
   `python -m vending.submissions resolve`.
   From the start (3 Dec), a live run also gets one tool of its own, not the server's:
   `find_pack_price` (`vending.price_search`), a capped web search for one pack's price on a store's
   own site. It runs here, through the run's harness, because only this process holds the model key
   (the server never gets it). A dry run never gets it: it writes the sourcing table and spends.
3. The system prompt is the rules card (`docs/agent/rules-card.md`, read once at the start; a run
   without it fails before any model request) followed by `SYSTEM`. Nothing in it changes between
   requests, so it is read from cache. The model gets the time, the week's rhythm and its own
   notes from the last run in the first message, after the system prompt. It calls tools until it answers with a decision (submit or skip), a weekly update for the team and notes for
   next week. Every request goes through `vending.harness`, so it is in `model_calls` before it is
   sent and the run and daily caps hold. A live run's spend also goes in the ledger; a dry run is
   test spend, under the test cap, and stays out of the ledger. The client is built from the key
   in `VENDING_ANTHROPIC_API_KEY` and nothing else; a run without it fails before any request.
4. The run is recorded in `agent_runs`: `submitted` once a `submit_desired_state` call
   succeeded (its request id is stored straight away, and the run stays `submitted` whatever
   stops it later), `skipped` when it decided not to (or it was a dry run), `failed` when it hit a cap,
   ran out of turns or broke. The stored plan is the desired state it previewed or sent, plus what
   the dashboard shows beside it: the visit kind, the run's reason and each changed slot's reason.
5. Each step is recorded in `agent_steps` as it happens: the model's reasoning (a readable summary,
   asked for with `thinking: {display: "summarized"}`; the API never returns the raw chain of
   thought), any text, each tool call and each tool result. A step that can't be stored is reported
   once on stderr and the run goes on: the record must never stop a run.

The model, effort and caps are placeholders until the team decides them (see #39 and #51).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import anyio
import psycopg
from psycopg.types.json import Jsonb

from vending import agent_runs, db, ledger, price_search, run_lease, settings, slots
from vending.config import is_halted
from vending.harness import DEFAULT_KEY_ENV, EASTERN, Harness, HarnessError, NotHeld
from vending.tools.project_rules import RULES_DIR

DEFAULT_MODEL = "claude-opus-5-5"   # pending a team decision
DEFAULT_EFFORT = "medium"
MAX_TOKENS = 16_000
MAX_TURNS = 15                      # tool rounds; on the last one the model must answer
REQUEST_TIMEOUT = 300.0             # the SDK's timeout: seconds for each wait for bytes (its default is 10 minutes)
# Seconds one model request may take in all, send to answer, by the wall clock. The harness enforces it (the
# SDK's timeout doesn't: a reply that keeps trickling in resets it), then gives up on the request.
REQUEST_DEADLINE = REQUEST_TIMEOUT
TOOL_TIMEOUT = timedelta(minutes=2)  # one MCP request (a tool call, the tool list); the SDK waits for ever
RUN_DEADLINE = 60 * 60.0            # seconds for a whole run; a placeholder, like the caps (#129)
# Seconds any one database statement of a run may take, or wait for a lock, before it fails: the start,
# each step, the halt and lease checks, the harness's bookkeeping and the final record.
DB_TIMEOUT = 30.0
BUSY = "Not started: another agent run was still going"
LOST_LEASE = ("Stopped before turn {turn}: this run no longer holds the run lease (it expired or another "
              "run took it over), so it sent no more model requests.")
LOST_LEASE_SUBMIT = ("Stopped before submit_desired_state: this run no longer holds the run lease (it expired "
                     "or another run took it over), so nothing was sent and nothing was bought.")
# Seconds between tries at recording a run's ending in agent_runs, after the first try fails.
FINISH_RETRIES = (2.0, 10.0, 30.0)
# How long the run lease must still last when a paid model request is reserved: longer than the request can
# take (`REQUEST_DEADLINE`), plus the reservation's commit and the request's bookkeeping. The harness checks it
# in the reservation's transaction (`run_lease.hold`), and again just before the request is sent.
LEASE_MARGIN = timedelta(seconds=REQUEST_DEADLINE + 2 * DB_TIMEOUT)
NOT_RECORDED = "The run couldn't be recorded in agent_runs ({error}), so it stopped before anything was sent."
# Never passed on to the server: the key the agent pays with, and what the SDK would pick up by itself.
MODEL_KEY_VARS = frozenset({DEFAULT_KEY_ENV, "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"})

# The same set as `vending.server.WRITE_TOOLS` (a test checks): a dry run never offers these.
WRITE_TOOLS = frozenset({"submit_desired_state", "set_price", "request_catalog_product", "set_wildcard_slot"})
SUBMIT = "submit_desired_state"
PREVIEW = "preview_desired_state"
RULES_CARD = RULES_DIR / "rules-card.md"

# Follows the rules card in the system prompt; the card holds the goal, guardrails and integrity rules.
SYSTEM = """\
You run the slots one week at a time. Revenue is counted ex 8% sales tax. Don't buy what won't \
sell and don't run out of what does. The rules card above is always on; call get_project_rules \
for the details behind it (pricing, restocking, money, guardrails).

The week: a student worker loads the machine at the free Friday visit. Order for it by the \
store's cutoff: Amazon items by Wednesday 23:59 (two-day shipping), in-store items by Thursday \
23:59. Past the Amazon cutoff the preview warns, and a late item waits for the next visit; past \
an in-store cutoff it refuses. Prices change only at a visit, by hand: set sale_price in the plan. \
A midweek visit (off_cycle) costs $25 and is worth it only when the sales it saves are worth more. \
One fixed slot is the wildcard (config, never moves): it tests products for the other slots, and \
get_machine_state's wildcard_trial says what to do with each (get_project_rules wildcard).

Timing: if the week's plan buys anything from Amazon, submit it by Wednesday 23:59, with what \
the wildcard trial shows then. Only one request can be open, so a promotion that only qualifies \
on Thursday evening waits a week, unless the week's plan buys nothing from Amazon: then the whole \
request may go in by Thursday 23:59, sourced in-store. Never hold an Amazon order past Wednesday \
for a promotion. The wildcard topic's evidence rules still decide whether a promotion qualifies.

Use the tools to look at the machine, sales, catalog, balance and open requests. Before you \
submit, call preview_desired_state with the exact plan and fix every refusal it lists. Submit at \
most once, with the same plan you previewed and its plan_hash as expected_plan_hash. A null in a \
tool's reply means unknown: never treat it as zero.

The briefing's record shows your last closed weeks against the baseline and the incidents since \
the last run. Don't repeat a change from a week that fell behind the baseline or failed a check, or \
a call that was refused, unless your reason says what is different now.

Every restock fills every slot to its column (get_project_rules restocking). The plan has one entry \
per assigned slot: leave fill_to out for a full fill. If the preview refuses insufficient_balance or \
below_spendable_floor, go by most margin per class day first: give an explicit fill_to for a partial \
fill that fits, and keep where no load fits, or choose a cheaper product or pack; then preview \
again. On the first fill every assigned slot needs a line, so never keep one or leave it out: use \
a cheaper product or pack, or a smaller fill_to. The order of entries sets no priority. Name every \
slot left short in the update. Skip the week only when nothing needs loading, a request is still open, \
no visit is left, the halt switch is on, or stock is needed but every feasible plan is refused: report \
the skip with the refusals. Never skip a fill because it seems not worth its cost.

When you're done, answer with:
- decision: "submit" if you submitted (or, on a dry run, would submit the plan you previewed), \
"skip" if not.
- reason: one sentence on why.
- update: a short weekly update for the team, under 150 words: what happened, what you're \
changing and why. Plain language; operations should act on it without asking.
- notes: what you want to remember next week, under 200 words.
- slot_reasons: for each slot your plan changes, the slot and one sentence on why. Add each slot a \
preview duplicate warning names, changed or not, with why it keeps that drink. Leave out other slots \
you keep as they are; an empty list when you change nothing and no warning names a slot."""

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["submit", "skip"]},
        "reason": {"type": "string"},
        "update": {"type": "string"},
        "notes": {"type": "string"},
        "slot_reasons": {"type": "array", "items": {
            "type": "object",
            "properties": {"slot": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["slot", "reason"],
            "additionalProperties": False,
        }},
    },
    "required": ["decision", "reason", "update", "notes", "slot_reasons"],
    "additionalProperties": False,
}


@dataclass
class Outcome:
    status: str                         # one of agent_runs.ENDINGS
    detail: str
    answer: dict | None = None
    plan: dict | None = None            # the last plan that previewed ok, or the one submitted
    request_id: str | None = None       # set once a submit succeeded: the run is `submitted` from then on
    submit_sent: bool = False           # a submit may have reached the bridge: never send another
    submits: int = 0                    # submit calls sent to the server, at most MAX_SUBMITS
    submit_refused: bool = False        # a submit came back proving nothing left (`_nothing_left`)
    needs_preview: bool = False         # ...and no preview has succeeded since, so a retry must wait
    unresolved: str | None = None       # a submit whose result is unknown: money may have been spent
    turn: int = 0                       # the model response being worked through
    plan_turn: int | None = None        # the response whose preview set `plan`: a submit must come later
    tool_calls: list[str] = field(default_factory=list)
    recorded: bool = True               # False when its ending couldn't be written to agent_runs, even on retry


# The first submit, and one retry after a reply proving no byte of the request left.
MAX_SUBMITS = 2
# The only refusal from before the POST that allows the retry: our own preview's hash moved.
RETRY_CODES = frozenset({"plan_changed"})
# The submit reply's `status` when connecting to the bridge failed (`vending.tools.submit`): nothing left.
NOT_CONNECTED = "not_filed"
RETRY_HINT = ("\n\nNothing was sent, so this run may submit once more: preview again, read it, then submit "
              "the plan it accepts.")
NO_RETRY_HINT = "\n\nNothing was sent. This run has used its one retry, so it can't submit again."

UNRESOLVED = ("SUBMISSION UNRESOLVED: the bridge may have accepted it. A person must settle it with "
              "`python -m vending.submissions resolve` before anyone submits again. ")


class LeaseLost(Exception):
    """The run no longer holds the run lease: it stops where it is. `Outcome` already says why."""


class RulesCardMissing(RuntimeError):
    """The rules card can't be read: the agent doesn't run without it."""


def system_prompt(card_path: Path | None = None) -> str:
    """The rules card, then `SYSTEM`. Raises `RulesCardMissing` rather than run without the card."""
    path = card_path or RULES_CARD
    try:
        card = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise RulesCardMissing(f"The rules card {path} can't be read ({error}); the agent doesn't run "
                               "without its rules.") from error
    if not card:
        raise RulesCardMissing(f"The rules card {path} is empty; the agent doesn't run without its rules.")
    return f"{card}\n\n{SYSTEM}"


class NoModelKey(RuntimeError):
    """`VENDING_ANTHROPIC_API_KEY` isn't set: nothing can pay for a model request."""


def model_client() -> Any:
    """An Anthropic client that pays with the team's key, read from `VENDING_ANTHROPIC_API_KEY` only.

    Never a bare `anthropic.Anthropic()`: that reads `ANTHROPIC_API_KEY`, then a bearer token or a
    login profile, so a run could bill whoever is signed in on that machine. The harness checks
    the client again (`harness.check_client`) before the first request.
    """
    import anthropic

    key = (settings.get(DEFAULT_KEY_ENV) or "").strip()
    if not key:
        raise NoModelKey(f"{DEFAULT_KEY_ENV} isn't set, so no model request was made. The README says "
                         "where the team's key lives and how to set it.")
    return anthropic.Anthropic(api_key=key, max_retries=0, timeout=REQUEST_TIMEOUT)


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


def recorded_plan(plan: dict | None, answer: dict | None) -> dict | None:
    """The plan as `agent_runs` stores it: the desired state, plus what the dashboard shows beside it.

    The dashboard reads the replayer's plan shape: a `visit`, a `reason` for the whole plan and a
    `reason` on each slot. The desired state carries none of those, so they are added here from
    the model's final answer. A run that never answered still stores its plan, without reasons.
    """
    if plan is None:
        return None
    answer = answer or {}
    given = answer.get("slot_reasons")
    reasons = {str(r["slot"]): r["reason"] for r in (given if isinstance(given, list) else [])
               if isinstance(r, dict) and r.get("slot") is not None and r.get("reason")}
    slots = plan.get("slots")
    if isinstance(slots, list):
        slots = [{**s, "reason": reasons[str(s["slot"])]} if isinstance(s, dict) and str(s.get("slot")) in reasons
                 else s for s in slots]
    recorded = {**plan, "slots": slots, "visit": "midweek" if plan.get("off_cycle") else "scheduled"}
    if answer.get("reason"):
        recorded["reason"] = answer["reason"]
    return recorded


def last_notes(conn: psycopg.Connection, *, live: bool) -> str:
    """The notes from the latest finished run since the real ledger opening and our current slot assignment,
    labelled with where they came from.

    A live run reads only live runs' notes, so a dry run's "would submit" is never taken as done.
    Notes carry over only from a run that started at or after both the real opening (`ledger.real_opening`)
    and the moment our slots (the server's assignment) were first read (`slots.assigned_at`). A run before
    either may name slots that aren't ours (#216). The assignment's time matters even after the opening:
    an opening backdated in a recovery could fall before the assignment (Sol review of #222, round 4,
    finding 3). With no real opening (none yet, or only the 11 Nov test one) or no stored assignment, no
    notes carry over.
    """
    opened = ledger.real_opening(conn)
    if opened is None:
        return ("(No notes: the real ledger opening isn't recorded yet, so earlier runs' notes are left out. "
                "They may name slots that aren't ours.)")
    assigned = slots.assigned_at(conn)
    if assigned is None:
        return ("(No notes: our slot assignment isn't recorded, so earlier runs' notes are left out. "
                "They may name slots that aren't ours.)")
    modes = ["live"] if live else ["live", "dry-run"]
    row = conn.execute("select mode, started_at, notes from agent_runs"
                       " where mode = any(%s) and notes is not null and status <> 'running' and started_at >= %s"
                       " order by started_at desc, id desc limit 1", (modes, max(opened.at, assigned))).fetchone()
    if not row:
        return "(No notes yet: this is the first run since the ledger opening and our current slot assignment.)"
    mode, started, notes = row
    kind = "the live run" if mode == "live" else "a dry run (nothing it planned was sent)"
    return f"(From {kind} on {started.astimezone(EASTERN):%A %d %B %H:%M} Eastern.)\n{notes}"


def brief(now: datetime, notes: str, *, live: bool, startup: str = "") -> str:
    mode = ("This is the live run: submit_desired_state sends a real purchase request." if live else
            "This is a dry run: the write tools are hidden. Preview the plan you would submit, then answer.")
    block = f"{startup}\n\n" if startup else ""
    return (f"Now: {now.astimezone(EASTERN):%A %d %B %Y %H:%M} Eastern.\n{mode}\n\n{block}"
            f"Your notes from the last run:\n{notes}")


async def startup_read(call_tool: Callable, record_step: Callable) -> str:
    """One real MCP read before the model loop, never a fabricated model tool-use turn."""
    metadata: dict[str, Any] = {"source": "startup", "tool": "get_briefing"}
    failed = False
    try:
        result = await call_tool("get_briefing", {})
        text, reply = _result_text(result)
        # Some MCP clients expose only text content. Read that envelope too.
        if reply is None:
            try:
                reply = json.loads(text)
            except (ValueError, TypeError):
                reply = None
        if (getattr(result, "isError", False) or not isinstance(reply, dict)
                or reply.get("success") is not True or not all(
                    isinstance(reply.get(k), str) and reply[k] for k in ("computed_at", "snapshot_id", "text"))):
            raise ValueError("get_briefing did not return a usable snapshot")
        metadata.update(computed_at=reply["computed_at"], snapshot_id=reply["snapshot_id"], success=True)
        opening = (f"Trusted startup read from get_briefing (computed_at={reply['computed_at']}, "
                   f"snapshot_id={reply['snapshot_id']}):\n{reply['text']}")
    except Exception as error:
        failed = True
        # Errors may contain arbitrary transport/source text. Keep them in the run record only.
        metadata.update(success=False, error=f"{type(error).__name__}: {error}"[:500])
        opening = ("Startup read get_briefing failed. Use the existing pull tools for machine state, sales, "
                   "P&L, catalog and pending requests. Call get_account_balance every run before planning.")
    await anyio.to_thread.run_sync(lambda: record_step(
        1, "tool_result", json.dumps(metadata, sort_keys=True), tool="get_briefing", is_error=failed))
    return opening


async def converse(harness: Harness, call_tool: Callable[[str, dict], Any], tools: list[dict], out: Outcome, *,
                   model: str, effort: str, system: str, opening: str, live: bool,
                   halted: Callable[[], bool] = lambda: False,
                   on_submit: Callable[[Outcome], None] = lambda out: None, max_turns: int = MAX_TURNS,
                   label: str = "", on_step: Callable[..., None] = lambda *a, **k: None,
                   owned: Callable[[], bool] = lambda: True,
                   local_tools: dict[str, Callable[[dict], tuple[str, bool]]] | None = None) -> None:
    """The tool loop, filling in `out` as it goes so nothing it learned is lost if it stops early.

    `call_tool(name, arguments)` is awaited and returns an MCP `CallToolResult`. `local_tools` are tools this
    process runs itself, not the server: `name -> fn(arguments) -> (text, is_error)`, run in a worker thread.
    `halted()` and `owned()` (the run still holds the run lease) are checked before every model request, and
    `owned()` again just before a submit is sent. `on_submit(out)` runs straight after a submit succeeds.
    `on_step(turn, kind, content, tool=None, is_error=False)` is called for every step, in order.
    """
    async def note(turn: int, kind: str, content: str, **extra: Any) -> None:
        await anyio.to_thread.run_sync(lambda: on_step(turn, kind, content, **extra))

    messages: list[dict] = [{"role": "user", "content": opening}]
    schemas = {t["name"]: t["input_schema"] for t in tools}
    for turn in range(max_turns + 1):
        if await anyio.to_thread.run_sync(halted):
            out.status, out.detail = "halted", f"The halt switch was turned on; stopped before turn {turn + 1}."
            return
        if not await anyio.to_thread.run_sync(owned):
            out.status, out.detail = "failed", LOST_LEASE.format(turn=turn + 1)
            return
        request = {"model": model, "max_tokens": MAX_TOKENS, "system": system, "messages": messages,
                   "tools": tools, "tool_choice": {"type": "auto" if turn < max_turns else "none"},
                   "cache_control": {"type": "ephemeral"},     # each turn re-sends the history: read it from cache
                   # Adaptive is the default on our models; "summarized" returns readable reasoning for
                   # the run record instead of empty thinking blocks. It changes what we see, not the bill.
                   "thinking": {"type": "adaptive", "display": "summarized"},
                   "output_config": {"effort": effort, "format": {"type": "json_schema", "schema": ANSWER_SCHEMA}}}
        try:
            response = await anyio.to_thread.run_sync(
                lambda: harness.call(request, label=f"{label} turn {turn + 1}".strip()))
        except NotHeld as error:            # the lease check at the reservation said no: nothing reserved or sent
            out.status = "failed"
            out.detail = (LOST_LEASE.format(turn=turn + 1) if run_lease.LOST in str(error)
                          else f"Stopped on turn {turn + 1}: {error}")
            return
        except HarnessError as error:      # CapReached included: nothing more can be paid for
            out.status, out.detail = "failed", f"Stopped on turn {turn + 1}: {error}"
            return
        messages.append({"role": "assistant", "content": response.content})
        out.turn = turn
        for block in response.content:
            if block.type == "thinking" and getattr(block, "thinking", ""):
                await note(turn + 1, "thinking", block.thinking)
            elif block.type == "text" and block.text:
                await note(turn + 1, "text", block.text)
            elif block.type == "tool_use":
                await note(turn + 1, "tool_call", json.dumps(block.input or {}, sort_keys=True, default=str),
                           tool=block.name)
        if response.stop_reason == "tool_use":
            results = []
            for block in response.content:
                if block.type == "tool_use":
                    out.tool_calls.append(block.name)
                    try:
                        text, failed = await _use(block, call_tool, schemas, out, live=live, on_submit=on_submit,
                                                  owned=owned, local_tools=local_tools or {})
                    except LeaseLost:
                        return
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": text,
                                    "is_error": failed})
                    await note(turn + 1, "tool_result", text, tool=block.name, is_error=failed)
            messages.append({"role": "user", "content": results})
            continue
        if response.stop_reason == "refusal":
            category = getattr(getattr(response, "stop_details", None), "category", None)
            out.status, out.detail = "failed", f"The model declined to answer (refusal, category {category})."
            return
        if response.stop_reason == "max_tokens":
            # The harness lowers max_tokens to fit what's left of the caps, so this is usually a cap.
            out.status = "failed"
            out.detail = (f"The answer on turn {turn + 1} was cut off at its token limit, so it can't be read. "
                          "The run or daily cap probably left too few output tokens for this turn "
                          "(`python -m vending.usage` shows each request); raise the cap, or use a "
                          "cheaper model or a lower effort.")
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
        elif out.submit_sent or out.submit_refused:
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
               on_submit: Callable[[Outcome], None], owned: Callable[[], bool] = lambda: True,
               local_tools: dict[str, Callable[[dict], tuple[str, bool]]] | None = None) -> tuple[str, bool]:
    """Run one tool call the model asked for: (text for the model, is_error)."""
    name, arguments = block.name, dict(block.input or {})
    if name not in schemas:
        return f"There is no tool called {name}.", True
    if not live and name in WRITE_TOOLS:          # never offered; refused again in case the model makes it up
        return "Not sent: this is a dry run.", True
    if name in (local_tools or {}):
        return await anyio.to_thread.run_sync(lambda: local_tools[name](arguments))
    if name == SUBMIT:
        if out.submit_sent:
            return "Not sent: you already submitted once this run.", True
        if out.submits >= MAX_SUBMITS:
            return "Not sent: this run has used its one retry after a refused submit.", True
        if out.needs_preview:
            return ("Not sent: your last submit was refused. Preview again, then submit the plan it accepts "
                    "in a later turn."), True
        previewed = {k: v for k, v in (out.plan or {}).items() if k != "plan_hash"}
        if out.plan is None or not _same_plan(arguments.get("desired"), previewed):
            return ("Not sent: submit exactly the plan your last preview_desired_state accepted "
                    "(preview it first if you changed anything)."), True
        if out.plan_turn == out.turn:
            return ("Not sent: read the preview's cost, warnings and refusals first, "
                    "then submit in your next turn."), True
        if "expected_plan_hash" in (schemas[name].get("properties") or {}):
            arguments["expected_plan_hash"] = out.plan["plan_hash"]   # refused by the server if anything moved
        if not await anyio.to_thread.run_sync(owned):   # the last check before money can be spent
            out.status, out.detail = "failed", LOST_LEASE_SUBMIT
            raise LeaseLost
        out.submit_sent = True                    # set before sending: cleared only by a reply proving nothing left
        out.submits += 1
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
            out.needs_preview = False
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
            if _nothing_left(reply):
                # Provably nothing left: one more try, after a new preview.
                out.submit_sent, out.submit_refused, out.needs_preview = False, True, True
                text += RETRY_HINT if out.submits < MAX_SUBMITS else NO_RETRY_HINT
        else:
            out.unresolved = text[:500]
            out.detail = UNRESOLVED + "It reported success with no request id: " + out.unresolved
    return text, failed


def _nothing_left(reply: dict) -> bool:
    """Whether a failed submit's reply proves no byte of the request left: our own `plan_changed` and nothing
    else, from before any submission row was saved, or `not_filed` (connecting failed). Nothing else does,
    a reply to the POST shaped like the bridge's refusal included."""
    codes = {r.get("code") for r in reply.get("refusals") or [] if isinstance(r, dict)}
    if reply.get("submission") is None and reply.get("status") is None:
        return bool(codes) and codes <= RETRY_CODES
    return reply.get("status") == NOT_CONNECTED and isinstance(reply.get("submission"), int)


def server_env(context: dict[str, str] | None = None) -> dict[str, str]:
    """The server's environment: ours without any model key, which it never needs, and with this run's own
    context (`run_context`) in place of anything inherited: a run lease or scheduled time left in our environment
    must never reach the server as if it were this run's."""
    from vending.tools.submit import RUN_ENV

    env = {k: v for k, v in os.environ.items() if k.upper() not in MODEL_KEY_VARS and k.upper() not in RUN_ENV}
    return {**env, "VENDING_TOOLS": "built", **(context or {})}


def run_context(token: Any, schedule_due: datetime | None) -> dict[str, str]:
    """What the server must know about this run (`vending.tools.submit.RunContext`): its run lease's token, and for
    a scheduled run its time, as the transaction that took the lease recorded it. The submit checks that exact
    lease and time at the send, never whichever lease is current then."""
    from vending.tools.submit import RUN_LEASE_ENV, SCHEDULE_DUE_ENV

    context = {RUN_LEASE_ENV: str(token)}
    if schedule_due is not None:
        context[SCHEDULE_DUE_ENV] = schedule_due.isoformat()
    return context


def check_submissions(connect: Callable[[], psycopg.Connection], bridge: Callable[[], Any] | None,
                      record_step: Callable[..., None]) -> None:
    """Before a live run's model loop: settle what the bridge can answer for (`submissions.check`).

    It never sends anything, and when in doubt a row stays open, so the submit tool still refuses
    `submission_unresolved`. A run with something to report records it as its first step.
    """
    from vending import submissions
    from vending.bridge import Bridge
    from vending.halt import WAIT_NOTICE_MS

    try:
        with connect() as conn:
            checked = submissions.check(conn, bridge or Bridge, lock_timeout_ms=WAIT_NOTICE_MS)
        if not checked:
            return
        metadata: dict[str, Any] = {"source": "startup", "submissions": [vars(c) for c in checked]}
        failed = any(c.status in submissions.OPEN for c in checked)
    except Exception as error:     # the rows stay as they were; the submit tool still guards them
        metadata, failed = {"source": "startup", "error": f"{type(error).__name__}: {error}"[:500]}, True
    record_step(1, "tool_result", json.dumps(metadata, sort_keys=True), tool="check_submissions", is_error=failed)


def _record_submit(connect: Callable[[], psycopg.Connection], run_id: int, out: Outcome) -> None:
    """Store the request id the moment a submit succeeds, before another model call can fail."""
    with connect() as conn:
        conn.execute("update agent_runs set request_id = %s, plan = %s where id = %s and status = 'running'",
                     (out.request_id, Jsonb(out.plan, dumps=agent_runs._dumps), run_id))


def _settle(out: Outcome) -> None:
    """A run that submitted is `submitted`, whatever stopped it after; an unknown submit says so first."""
    if out.request_id is not None and out.status != "submitted":
        out.detail = f"{agent_runs.SUBMITTED}{out.request_id}{agent_runs.AFTERWARDS}{out.detail or 'stopped'}"
        out.status = "submitted"
    elif out.unresolved is not None and not out.detail.startswith(UNRESOLVED):
        out.status, out.detail = "failed", UNRESOLVED + out.unresolved + (f" Then: {out.detail}" if out.detail else "")


def _bounded(connect: Callable[[], psycopg.Connection], seconds: float) -> Callable[[], psycopg.Connection]:
    """`connect`, with a statement and lock timeout of `seconds` on each connection it opens."""
    return db.with_timeouts(connect, seconds)


def _renew(connect: Callable[[], psycopg.Connection], token: Any) -> bool:
    with connect() as conn:
        return run_lease.renew(conn, token)


async def _heartbeat(connect: Callable[[], psycopg.Connection], token: Any) -> None:
    """Renew the lease every `run_lease.HEARTBEAT` seconds until it is lost or the run ends. A renewal that
    fails is tried again next time; the run's own checks fail closed if the lease lapses meanwhile."""
    while True:
        await anyio.sleep(run_lease.HEARTBEAT)
        try:
            if not await anyio.to_thread.run_sync(lambda: _renew(connect, token)):
                return
        except Exception:       # noqa: BLE001 - the database is down: the next renewal tries again
            pass


async def run(*, live: bool, model: str = DEFAULT_MODEL, effort: str = DEFAULT_EFFORT,
              connect: Callable[[], psycopg.Connection] = db.connect, client: Any = None,
              now: Callable[[], datetime] | None = None, open_tools: Callable | None = None,
              max_turns: int = MAX_TURNS, rules_card: Path | None = None,
              deadline: float = RUN_DEADLINE,
              authorize: Callable[[psycopg.Connection], str | None] | None = None,
              bridge: Callable[[], Any] | None = None) -> tuple[int | None, Outcome]:
    """One weekly run, recorded in `agent_runs`. Returns (run id, outcome).

    Holds the run lease throughout (`vending.run_lease`). If another run holds it, this one is recorded
    as `skipped` and nothing else happens. `authorize(conn)`, if given, runs once the lease is free and
    before it is taken, in the same transaction: a reason it returns means the run doesn't start, and
    comes back as a `skipped` outcome with no run id and no `agent_runs` row. A run whose start can't be
    recorded has no run id either. Past `deadline` seconds the run stops and is recorded as `failed`.

    `bridge` builds the client a live run's submission check reads with (`vending.bridge.Bridge` by default).
    The check runs inside the lease, before the model starts."""
    connect = _bounded(connect, DB_TIMEOUT)
    with connect() as conn:
        taken = run_lease.take(conn, deadline=deadline, authorize=authorize)
    if isinstance(taken, run_lease.Refused):
        return None, Outcome("skipped", taken.detail)
    if isinstance(taken, run_lease.Busy):
        with connect() as conn:
            run_id = agent_runs.start(conn, mode="live" if live else "dry-run", model=model)
            out = Outcome("skipped", BUSY + (f" (agent run {taken.run_id})." if taken.run_id is not None else "."))
            agent_runs.finish(conn, run_id, out.status, detail=out.detail, cost_usd=Decimal(0))
        return run_id, out
    if taken.took_over is not None:
        print(f"agent run {taken.took_over} stopped renewing the run lease; recorded it as stopped and took over",
              file=sys.stderr, flush=True)
    harnesses: list = []     # the run's harness, once it has one
    try:
        async with anyio.create_task_group() as beats:
            beats.start_soon(_heartbeat, connect, taken.token)
            try:
                return await _run_owned(live=live, model=model, effort=effort, connect=connect, client=client,
                                        now=now, open_tools=open_tools, max_turns=max_turns, rules_card=rules_card,
                                        deadline=deadline, token=taken.token, harnesses=harnesses,
                                        bridge=bridge, schedule_due=taken.schedule_due)
            finally:
                beats.cancel_scope.cancel()
    finally:
        if any(h.in_flight() for h in harnesses):
            # A request the harness gave up on may still be going: keep the lease, so no other run's request
            # overlaps it. It expires by itself (`run_lease.TTL` after the last renewal).
            print("a model request this run gave up on may still be going, so the run lease is kept until it "
                  "expires", file=sys.stderr, flush=True)
        else:
            _give_back(connect, taken.token)


def _give_back(connect: Callable[[], psycopg.Connection], token: Any) -> None:
    try:
        with connect() as conn:
            run_lease.give_back(conn, token)
    except Exception as error:      # noqa: BLE001 - it expires by itself (`run_lease.TTL`)
        print(f"the run lease couldn't be given back ({type(error).__name__}); it expires by itself",
              file=sys.stderr, flush=True)


async def _run_owned(*, live: bool, model: str, effort: str, connect: Callable[[], psycopg.Connection],
                     client: Any, now: Callable[[], datetime] | None, open_tools: Callable | None,
                     max_turns: int, rules_card: Path | None, deadline: float,
                     token: Any, harnesses: list,
                     bridge: Callable[[], Any] | None = None,
                     schedule_due: datetime | None = None) -> tuple[int | None, Outcome]:
    import time

    stop_at = time.monotonic() + deadline     # the harness sends nothing past it
    now = now or (lambda: datetime.now(timezone.utc))
    run_id = None
    harness = None
    out = Outcome("failed", "")
    warned = []

    def record_step(turn: int, kind: str, content: str, **extra: Any) -> None:
        try:
            with connect() as c:
                agent_runs.step(c, run_id, turn, kind, content, **extra)
        except Exception as error:     # the record must never stop the run
            if not warned:
                warned.append(error)
                print(f"agent run {run_id}: a step couldn't be stored ({type(error).__name__}: {error}); "
                      "the run goes on, but its steps are incomplete", file=sys.stderr, flush=True)

    def halted() -> bool:
        with connect() as c:
            return is_halted(c)

    def owned() -> bool:
        return _renew(connect, token)

    try:
        # The deadline covers the whole run, its start included. A database call can't be cut off mid-way,
        # but each has DB_TIMEOUT; cancellation lands at the next await, and a model request in flight
        # (in a thread) ends first.
        with anyio.move_on_after(deadline) as timer:
            with connect() as conn:
                run_id = agent_runs.start(conn, mode="live" if live else "dry-run", model=model)
                run_lease.attach(conn, token, run_id)
            await anyio.sleep(0)        # already past the deadline: stop here
            if halted():
                out.status, out.detail = "halted", "The halt switch is on: nothing was read, called or sent."
            else:
                system = system_prompt(rules_card)     # before any server is started or request sent
                with connect() as conn:
                    notes = last_notes(conn, live=live)
                if client is None:
                    client = model_client()
                # Only a live run spends the competition's money; a dry run is test spend, under the test cap.
                # Each paid request checks the lease in the transaction that reserves its budget, so a run
                # taken over meanwhile reserves and sends nothing (`run_lease.hold`).
                harness = Harness(client, connect, purpose="live" if live else "test", by="agent",
                                  hold=lambda c: run_lease.hold(c, token, LEASE_MARGIN),
                                  hold_margin=LEASE_MARGIN.total_seconds(), request_deadline=REQUEST_DEADLINE,
                                  stop_at=stop_at)
                harnesses.append(harness)
                if live:    # inside the lease, so no other run's submit overlaps the check
                    await anyio.to_thread.run_sync(lambda: check_submissions(connect, bridge, record_step))
                # The server is told which run it works for: this lease, and this scheduled time if any.
                async with (open_tools or _open_server)(run_context(token, schedule_due)) as (listed, call_tool):
                    if live and SUBMIT not in {t.name for t in listed}:
                        out.detail = ("submit_desired_state isn't built on this server, so a live run can't "
                                      "submit. Use a dry run.")
                    else:
                        startup = await startup_read(call_tool, record_step)
                        offered, local = tool_definitions(listed, live=live), {}
                        if price_search.offered(live=live, now=now()):
                            lookups = price_search.PriceSearch(harness, connect, model=model)
                            offered.append(price_search.TOOL)
                            local[price_search.TOOL_NAME] = lookups.lookup
                        await converse(harness, call_tool, offered, out, model=model,
                                       effort=effort, system=system,
                                       opening=brief(now(), notes, live=live, startup=startup), live=live,
                                       halted=halted,
                                       on_submit=lambda o: _record_submit(connect, run_id, o), max_turns=max_turns,
                                       label=f"agent run {run_id}", on_step=record_step, owned=owned,
                                       local_tools=local)
        if timer.cancelled_caught:
            out.status, out.detail = "failed", f"Stopped at the run's deadline ({deadline / 60:g} min)."
    except Exception as error:   # record the run whatever broke; the ledger already holds what was spent
        out.status, out.detail = "failed", f"{type(error).__name__}: {error}"[:1000]
    except BaseException as error:   # Ctrl-C or cancellation: record it, then let it through
        out.status, out.detail = "failed", f"Interrupted ({type(error).__name__})."
        raise
    finally:
        _settle(out)
        if run_id is None:
            out.status, out.detail = "failed", NOT_RECORDED.format(error=out.detail or "no reason given")
        else:
            _record_ending(connect, run_id, out, harness)
    return run_id, out


def _record_ending(connect: Callable[[], psycopg.Connection], run_id: int, out: Outcome, harness: Any) -> None:
    """Write the run's ending to agent_runs, trying again after each of `FINISH_RETRIES`. If it never lands,
    `out.recorded` is False and the detail says so; the schedule's row keeps it in Needs attention until
    agent_runs has an ending (`vending.dashboard`). A blocking sleep: the run has nothing else to do now."""
    import time

    answer = out.answer or {}
    error = None
    for wait in (0.0, *FINISH_RETRIES):
        if wait:
            time.sleep(wait)
        try:
            with connect() as conn:
                agent_runs.finish(conn, run_id, out.status, plan=recorded_plan(out.plan, out.answer),
                                  update=answer.get("update"),
                                  notes=answer.get("notes"), detail=out.detail,
                                  cost_usd=harness.spent if harness is not None else Decimal(0),
                                  request_id=out.request_id)
            return
        except ValueError:      # it already has an ending: the run that took over the lease recorded it
            out.detail = (f"{out.detail} Its agent_runs row already had an ending, recorded by the run that "
                          "took over its lease.").strip()
            return
        except Exception as caught:      # noqa: BLE001 - each try is bounded by DB_TIMEOUT
            error = caught
            print(f"agent run {run_id}: its ending couldn't be recorded ({type(caught).__name__})",
                  file=sys.stderr, flush=True)
    out.recorded = False
    out.detail = (f"{out.detail} Its ending couldn't be recorded ({type(error).__name__}, "
                  f"{1 + len(FINISH_RETRIES)} tries); agent_runs may still say running.").strip()


def _open_server(context: dict[str, str] | None = None):
    """Start `python -m vending.server` over stdio, with this run's `context` in its environment; yields (tools,
    call_tool)."""
    from contextlib import asynccontextmanager

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    @asynccontextmanager
    async def opened():
        params = StdioServerParameters(command=sys.executable, args=["-m", "vending.server"],
                                       env=server_env(context))
        async with stdio_client(params) as (read, write), \
                ClientSession(read, write, read_timeout_seconds=TOOL_TIMEOUT) as session:
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
