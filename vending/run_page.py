"""One agent run, followed as it happens: `/dashboard/run/<id>` (and `/run/<id>` on `python -m vending.dashboard`).

    /dashboard/run/<id>             the page
    /dashboard/run/<id>?after=<n>   the same page with only the steps after step id n, for the page's own refresh
    /dashboard/run/<id>.json        the data as JSON (`?after=` works there too)

It shows each step as `agent_steps` records it (the Runs panel's "How it decided", unfolded), the draft plans
as the run previewed them (each `preview_desired_state` call and whether its own preview accepted it), the plan
the run recorded once it has one, the model spend so far against the run cap, and how the run ended. A finished
run shows the same page, complete.

**Refresh.** While the run says `running`, or steps are left to load, the page's script asks every
POLL_SECONDS for the steps after the last one it has (`?after=`), at most STEP_BATCH at a time, appends them,
adds any new drafts, updates a waiting draft's status when its reply comes in, and swaps the figures and the
plan. No websocket. Each refresh reads the run's row, the steps after `after`, a count of all its steps, and
at most one earlier preview call (the baseline a new draft is compared with); never the whole transcript again.

Where each figure comes from:
- **Steps**: `agent_steps`, oldest first. Reasoning is the summary the API returns, never the raw chain of
  thought; a step marked `is_error` shows `dashboard.HIDDEN`, never its text; a failed run's `detail` too. A
  step's text is cut to SHOWN_CHARS on the page and in the JSON; the database keeps it whole.
- **Drafts**: the `desired` argument of each `preview_desired_state` call, after the steps are scoped to our
  assignment, reduced to typed fields (a SKU, a whole fill, a price; a kept slot is just "keep"), and `ok` from
  the reply with the same `tool_use_id` (migration 035). A step recorded before that has no id: its reply is
  paired only when its turn has one preview call and one preview reply. A draft is compared with the draft
  before it; the first, with the last live run's plan, reduced the same way.
- **Spend so far**: the sum of `model_calls.cost_usd` for the run's harness, read by its key
  (`agent_runs.harness_key`, through the `model_calls_run` index). A call still `pending` counts at its worst
  case, as the harness reserved it, so the figure can only overstate. Once the run ends,
  `agent_runs.cost_usd` (what the harness counted) is the run's cost.
- **Cap**: `model_run_cap_cents` in `config` now (the harness's default when it isn't set). A run uses the
  cap it read when it started.

Only live runs: the live page leaves dry runs and replays out, as the Runs panel does. A run from before the
ledger's opening is shown and marked as development spend. Every run and step goes through the Runs panel's
own scoping (`dashboard.scope_run`): our assignment only (`vending.slots.our_slots`), nothing from before it.
Reads only, in one repeatable-read, read-only transaction. No model calls.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from functools import partial
from typing import Any

import psycopg
from psycopg import sql
from psycopg.types.json import set_json_loads

from vending import agent_runs, dashboard, report_card
from vending import stock as stock_mod
from vending.harness import DEFAULT_RUN_CAP_CENTS
from vending.replay.deciders import MAX_FILL
from vending.slots import SlotsNotRecorded, our_slots

ROUTE = "/dashboard/run/"
DEMO_ROUTE = "/dashboard/demo/run/"
POLL_SECONDS = 5
STEP_BATCH = 200          # steps read per request; more load on the next refresh, straight away
SHOWN_CHARS = 1000        # characters of one step's text on the page and in the JSON
PREVIEW = "preview_desired_state"
CAP_KEY = "model_run_cap_cents"
STEP_COLUMNS = "id, turn, at, kind, tool, content, is_error, tool_use_id"


def parse(path: str, prefix: str) -> tuple[int, bool] | None:
    """(run id, JSON wanted) from `<prefix><id>` or `<prefix><id>.json`, or None for any other path."""
    if not path.startswith(prefix):
        return None
    rest = path[len(prefix):]
    as_json = rest.endswith(".json")
    rest = rest[:-5] if as_json else rest
    if not rest.isdigit() or len(rest) > 18 or rest.startswith("0"):
        return None
    return int(rest), as_json


def after_of(query: str) -> int:
    """The `after=` step id from a query string; 0 (from the start) when it is missing or not a number."""
    from urllib.parse import parse_qs

    value = parse_qs(query).get("after", ["0"])[0]
    return int(value) if value.isdigit() and len(value) <= 18 else 0


# --- Data ----------------------------------------------------------------------------------------

def read(conn: psycopg.Connection, run_id: int, now: datetime | None = None, public: bool = True,
         after: int = 0) -> dict:
    """Everything the page shows about run `run_id`, from one consistent moment, with the steps after step id
    `after` (at most STEP_BATCH). Reads only. Raises `dashboard.Missing` when a table or column it needs isn't
    there (a database not yet migrated)."""
    set_json_loads(partial(json.loads, parse_float=Decimal), context=conn)
    with conn.transaction():
        conn.execute("set transaction isolation level repeatable read, read only")
        now = now or conn.execute("select transaction_timestamp()").fetchone()[0]
        return _read(conn, run_id, now, public, after)


def _read(conn: psycopg.Connection, run_id: int, now: datetime, public: bool, after: int) -> dict:
    out: dict[str, Any] = {"as_of": now, "run_id": run_id, "found": False}
    rows = dashboard._rows(conn, sql.SQL(
        "select id, started_at, finished_at, mode, model, status, {}, cost_usd, request_id, team_update, notes, plan"
        " from agent_runs where id = %s and started_at <= %s").format(dashboard._run_detail(public)), (run_id, now))
    if not rows:
        return out
    run = rows[0]
    out["found"] = True
    if run["mode"] != "live":
        # The live page shows live runs only, as the Runs panel does.
        out.update(shown=False, mode=run["mode"])
        return out
    out["shown"] = True
    try:
        opening = dashboard._guarded(conn, lambda: stock_mod.opening_at(conn))
    except dashboard.Missing:
        opening = None
    opened = opening if opening is not None and opening <= now else None
    try:
        ours = our_slots(conn)
    except SlotsNotRecorded:
        ours = ()
    assigned = (dashboard._optional(conn, "select first_read_at from slot_assignment where id = 1") if ours else None)
    assignment_at = assigned["first_read_at"] if assigned else None
    steps = dashboard._rows(conn, f"select {STEP_COLUMNS} from agent_steps where run_id = %s and id > %s"
                                  " order by id limit %s", (run_id, after, STEP_BATCH + 1))
    more = len(steps) > STEP_BATCH
    steps = steps[:STEP_BATCH]
    counts = dashboard._rows(conn, "select count(*) as steps, coalesce(max(turn), 0) as turns,"
                                   " count(*) filter (where kind = 'tool_call') as calls, max(at) as last_at,"
                                   " count(*) filter (where tool = %s and kind = 'tool_call' and id <= %s) as drafts_before"
                                   " from agent_steps where run_id = %s", (PREVIEW, after, run_id))[0]
    # The baseline the first new draft is compared with: the last preview call already shown, else the last plan.
    baseline = None
    if counts["drafts_before"]:
        baseline = dashboard._rows(conn, f"select {STEP_COLUMNS} from agent_steps where run_id = %s and tool = %s"
                                         " and kind = 'tool_call' and id <= %s order by id desc limit 1",
                                   (run_id, PREVIEW, after))[0]
    previous = None if baseline is not None else dashboard._optional(
        conn, "select plan from agent_runs where mode = 'live' and plan is not null"
              " and (started_at, id) < (%s, %s) order by started_at desc, id desc limit 1",
        (run["started_at"], run_id))
    plan = run.pop("plan")
    run["steps"] = steps
    run["stale"] = run["status"] == "running" and now - run["started_at"] > dashboard.STALE_RUN
    # The error text of a failed run or step never leaves the database (`dashboard.hide_errors`).
    if run["status"] == "failed" and run["detail"]:
        run["detail"] = dashboard.HIDDEN
    for step in (*steps, *([baseline] if baseline else [])):
        if step["is_error"]:
            step["content"] = dashboard.HIDDEN
    old = dashboard._older_assignment(run["started_at"], assignment_at)
    dashboard.scope_run(run, set(ours), assignment_at)
    if baseline is not None and not old:
        holder = {"started_at": run["started_at"], "status": run["status"], "detail": None, "steps": [baseline]}
        dashboard.scope_run(holder, set(ours), assignment_at)
        start = _desired(holder["steps"][0]["content"]) if holder["steps"] else None
    else:
        start = previous["plan"] if previous else None
    drafts, replies = ([], []) if old else _drafts(run["steps"], start, ours, counts["drafts_before"])
    for step in run["steps"]:
        if len(step["content"]) > SHOWN_CHARS:
            step["content"] = (step["content"][:SHOWN_CHARS]
                               + f"\n... {len(step['content']):,} characters in all; the database keeps the whole text")
    out.update(
        run=run, after=after, more=more, last=steps[-1]["id"] if steps else after,
        counts={"steps": counts["steps"], "turns": counts["turns"], "calls": counts["calls"]},
        opened_at=opened, before_opening=opened is None or run["started_at"] < opened,
        last_step_at=None if old else counts["last_at"], spend=_spend(conn, run_id), cap=_cap(conn),
        drafts=drafts, replies=replies, plan=None if old else _scope_plan(plan, ours),
    )
    return out


def _spend(conn: psycopg.Connection, run_id: int) -> dict | None:
    """What the run's model calls cost so far (`model_calls`, by the harness's key), or None when that table
    can't be read."""
    try:
        return dashboard._optional(
            conn, "select coalesce(sum(cost_usd), 0) as spent, count(*) as calls,"
                  " count(*) filter (where status = 'pending') as pending from model_calls where run_id = %s",
            (agent_runs.harness_key(run_id),))
    except dashboard.Missing:
        return None


def _cap(conn: psycopg.Connection) -> dict:
    row = dashboard._optional(conn, "select value from config where key = %s", (CAP_KEY,))
    try:
        cents = int(row["value"]) if row is not None else DEFAULT_RUN_CAP_CENTS
    except (TypeError, ValueError):
        cents = None
    return {"usd": None if cents is None else Decimal(cents) / 100, "set": row is not None}


def _typed(entry: dict) -> dict:
    """A plan entry's fields as the page may show them: a catalog SKU, a whole fill, a shelf price. A kept slot
    is only "keep": the price or fill it carries doesn't apply. Anything else is left out, so no text rides
    along (`report_card.validate_plan`'s rules)."""
    if entry.get("keep") is True:
        return {"keep": True}
    out: dict[str, Any] = {}
    product = entry.get("product")
    if isinstance(product, str) and report_card._SKU.fullmatch(product):
        out["product"] = product
    fill = entry.get("fill_to")
    if isinstance(fill, int) and not isinstance(fill, bool) and 0 <= fill <= MAX_FILL:
        out["fill_to"] = fill
    price = report_card._price_text(entry.get("sale_price")) if entry.get("sale_price") is not None else None
    if price is not None:
        out["sale_price"] = price
    return out


def _slots(plan: Any, ours: tuple[str, ...]) -> list[dict] | None:
    """A plan's entries for our slots, in our order, typed (`_typed`), with each reason scoped to our
    assignment; None if it isn't a plan."""
    if not isinstance(plan, dict) or not isinstance(plan.get("slots"), list):
        return None
    by_slot = {str(e.get("slot")): e for e in plan["slots"] if isinstance(e, dict)}
    out = []
    for slot in ours:
        if slot in by_slot:
            reason = by_slot[slot].get("reason")
            out.append({"slot": slot, **_typed(by_slot[slot]),
                        "reason": dashboard._scope_text(reason, set(ours)) if isinstance(reason, str) else None})
    return out


def _scope_plan(plan: Any, ours: tuple[str, ...]) -> dict | None:
    slots = _slots(plan, ours)
    if slots is None:
        return None
    reason = plan.get("reason") if isinstance(plan.get("reason"), str) else None
    return {"visit": "midweek" if plan.get("off_cycle") or plan.get("visit") in ("midweek", "off_cycle")
            else "scheduled", "reason": dashboard._scope_text(reason, set(ours)), "slots": slots}


def _desired(content: str) -> Any:
    try:
        value = json.loads(content, parse_float=Decimal)
    except ValueError:
        return None
    return value.get("desired") if isinstance(value, dict) else None


def _changes(draft: list[dict], before: list[dict] | None, ours: tuple[str, ...]) -> list[dict]:
    """What a draft moved from `before`, from typed entries only (`_typed`)."""
    if before is None:
        return []
    strip = lambda rows: {"slots": [{k: v for k, v in r.items() if k != "reason"} for r in rows]}  # noqa: E731
    return dashboard._plan_changes(strip(draft), strip(before), None, ours)


def _drafts(steps: list[dict], start: Any, ours: tuple[str, ...], numbered: int) -> tuple[list[dict], list[dict]]:
    """The preview calls among `steps`, oldest first, numbered after the `numbered` already shown, each compared
    with the draft before it (the first with `start`); and the status of each preview reply among `steps`, by
    tool-use id, for a draft already shown whose reply came later."""
    results = [s for s in steps if s["kind"] == "tool_result" and s["tool"] == PREVIEW]
    calls = [s for s in steps if s["kind"] == "tool_call" and s["tool"] == PREVIEW]
    by_id = {s["tool_use_id"]: s for s in results if s.get("tool_use_id")}
    before = _slots(start, ours) if start is not None else None
    drafts = []
    for n, call in enumerate(calls, numbered + 1):
        use = call.get("tool_use_id")
        if use:
            reply = by_id.get(use)
        else:   # recorded before migration 035: pair only when the turn can't be confused
            same_turn = [r for r in results if r["turn"] == call["turn"] and not r.get("tool_use_id")]
            alone = sum(1 for c in calls if c["turn"] == call["turn"]) == 1
            reply = same_turn[0] if alone and len(same_turn) == 1 else None
        draft = {"n": n, "turn": call["turn"], "at": call["at"], "use": use, "status": _preview_status(reply),
                 "slots": None, "changes": [], "unreadable": None}
        desired = _desired(call["content"])
        slots = _slots(desired, ours)
        if slots is None:
            draft["unreadable"] = ("arguments not readable (stored cut short, or omitted)" if desired is None
                                   else "no desired state in the arguments")
        else:
            draft.update(slots=slots, visit="midweek" if desired.get("off_cycle") else "scheduled",
                         changes=_changes(slots, before, ours))
            before = slots
        drafts.append(draft)
    shown = {c.get("tool_use_id") for c in calls}
    replies = [{"use": r["tool_use_id"], "status": _preview_status(r)} for r in results
               if r.get("tool_use_id") and r["tool_use_id"] not in shown]
    return drafts, replies


def _preview_status(reply: dict | None) -> str:
    if reply is None:
        return "waiting"
    if reply["is_error"]:
        return "failed"
    try:
        value = json.loads(reply["content"])
    except ValueError:
        return "unknown"
    if isinstance(value, dict) and value.get("ok") is True:
        return "accepted"
    if isinstance(value, dict) and value.get("ok") is False:
        return "refused"
    return "unknown"


# --- HTML ----------------------------------------------------------------------------------------

_e, _pill, _money, _time, _span = dashboard._e, dashboard._pill, dashboard._money, dashboard._time, dashboard._span
_DRAFT_TONE = {"accepted": "ok", "refused": "warn", "failed": "bad", "waiting": "info", "unknown": "muted"}


def running(data: dict) -> bool:
    return bool(data.get("shown")) and data["run"]["status"] == "running" and not data["run"]["stale"]


def html(data: dict, *, nav: tuple[tuple[str, str], ...] = (), json_href: str = "", demo: bool = False) -> str:
    """The page, or with `data["after"]` its refresh: only the steps and drafts after that step. While the run is
    in progress, or steps are left to load (and not in the demo), its script asks for the next ones."""
    links = "".join(f' &middot; <a href="{_e(href)}">{_e(label)}</a>' for label, href in nav)
    live = running(data) and not demo
    more = bool(data.get("more")) and not demo
    head = (f'<header class="top"><h1>Run #{int(data["run_id"])}</h1><span class="meta">Read-only &middot; as of '
            f'{_time(data["as_of"], "etday")} &middot; <span id="live">'
            + ("fixed snapshot" if demo else f"checks every {POLL_SECONDS} s" if live else "not polling")
            + '</span>' + (f' &middot; <a href="{_e(json_href)}">JSON</a>' if json_href else "") + f'{links}</span></header>')
    if not data["found"]:
        body = '<p class="empty">No run with this id.</p>'
    elif not data["shown"]:
        body = f'<p class="empty">Not on the live page: a {_e(data["mode"])} run.</p>'
    else:
        body = _run_html(data)
    banner = (f'<div class="demo-banner" role="note">{_e(dashboard.DEMO_BANNER)}<a href="{dashboard.DEMO_LIVE_HREF}">'
              f'{dashboard.DEMO_LIVE_HREF}</a></div>' if demo else "")
    status = data["run"]["status"] if data.get("shown") else "not found"
    reload = f'<noscript><meta http-equiv="refresh" content="{POLL_SECONDS}"></noscript>' if live else ""
    marks = (f' data-last="{int(data["last"])}" data-after="{int(data["after"])}" data-more="{int(more)}"'
             if data.get("shown") else "")
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            + ('<meta name="robots" content="noindex">' if demo else "") + reload
            + f'<title>{dashboard.DEMO_TITLE if demo else ""}Run #{int(data["run_id"])} &middot; {_e(status)}'
            f' &middot; Agent dashboard</title><style>{dashboard.STYLE}{STYLE}</style></head><body>{banner}'
            f'<main data-run="{int(data["run_id"])}" data-running="{int(live)}"{marks}>{head}{body}</main>'
            + (f'<script>{SCRIPT % (POLL_SECONDS * 1000, json.dumps(dashboard.visits.REFRESH_HEADER))}</script>'
               if live or more else "")
            + '</body></html>')


def _fig(label: str, value: str, sub: str = "", tone: str = "") -> str:
    return (f'<div class="rp-fig{" " + tone if tone else ""}"><span class="label">{label}</span>'
            f'<span class="sv">{value}</span>' + (f'<span class="sub">{sub}</span>' if sub else "") + '</div>')


def _run_html(data: dict) -> str:
    run, now, counts = data["run"], data["as_of"], data["counts"]
    end = run["finished_at"] or now
    pills = [dashboard._run_pill(run)]
    if data["before_opening"]:
        pills.append(_pill("before the ledger opening: development spend, not the $600", "muted"))
    figs = [
        _fig("Status", " ".join(pills), f'{_e(run["mode"])} &middot; {_e(run["model"] or "model not recorded")}'),
        _fig("Started", _time(run["started_at"], "etday"),
             ("ran " if run["finished_at"] else "running ") + _span(end - run["started_at"])),
        _spend_fig(data),
        _fig("Steps", str(int(counts["steps"])),
             f'last {_time(data["last_step_at"], "ago", now)}' if data["last_step_at"] else "none yet"),
    ]
    if run["finished_at"]:
        figs.append(_fig("Ended", _time(run["finished_at"], "etday"),
                         f'request {_e(run["request_id"])}' if run["request_id"] else "no request"))
    elif run["request_id"]:
        figs.append(_fig("Request", _e(run["request_id"])))
    ended = ""
    if run["detail"]:
        ended += f'<p class="rp-detail"><span class="label">Detail</span> {_e(run["detail"])}</p>'
    for label, text in (("Update", run["team_update"]), ("Notes", run["notes"])):
        if text:
            ended += f'<p class="rp-detail"><span class="label">{label}</span> {_e(text)}</p>'
    summary = (f'<summary id="rp-summary">How it decided: {dashboard._plural(int(counts["turns"]), "turn")}, '
               f'{dashboard._plural(int(counts["calls"]), "tool call")}</summary>')
    steps = (f'<details class="steps" open data-key="steps-{int(run["id"])}">{summary}'
             f'<ol id="rp-steps">{"".join(dashboard.step_items(run["steps"]))}</ol></details>')
    if not counts["steps"]:
        steps = f'<p class="empty" id="rp-none">No steps recorded yet.</p>{steps}'
    return (f'<section class="rp-top" id="rp-top"><div class="rp-figs">{"".join(figs)}</div>{ended}</section>'
            f'<div class="grid2"><section id="steps"><h2>Steps</h2>{steps}</section>'
            f'<section id="plan"><h2>Plan</h2>{_plan_html(data)}</section></div>')


def _spend_fig(data: dict) -> str:
    run, spend, cap = data["run"], data["spend"], data["cap"]
    cap_text = (f'{_money(cap["usd"])} cap' + ("" if cap["set"] else " (default)")) if cap["usd"] is not None \
        else "cap unreadable"
    if run["finished_at"] and run["cost_usd"] is not None:
        spent, sub = run["cost_usd"], f"recorded at the end &middot; {cap_text}"
    elif spend is None:
        return _fig("Model spend", "unknown", f"model_calls unreadable &middot; {cap_text}")
    else:
        spent = spend["spent"]
        sub = (f'{dashboard._plural(int(spend["calls"]), "call")}'
               + (f', {int(spend["pending"])} at worst case' if spend["pending"] else "") + f" &middot; {cap_text}")
    share = (min(spent / cap["usd"], Decimal(1)) if cap["usd"] else None)
    meter = (f'<span class="rp-meter" role="img" aria-label="{share:.0%} of the cap"><i style="width:{share * 100:.1f}%">'
             '</i></span>' if share is not None else "")
    tone = "bad" if share is not None and share >= 1 else "warn" if share is not None and share >= Decimal("0.8") else ""
    return _fig("Model spend", f"{_money(spent)}{meter}", sub, tone)


def _plan_table(slots: list[dict], changed: frozenset[str] = frozenset()) -> str:
    """The plan's slots; a slot in `changed` (what the latest draft moved) is marked."""
    rows = []
    for s in slots:
        keep = "keep" if s.get("keep") else ""
        fill = "column" if not s.get("keep") and s.get("fill_to") is None else _e(s.get("fill_to"))
        price = _money(s.get("sale_price")) if s.get("sale_price") is not None else "&mdash;"
        mark = ' class="rp-changed"' if s["slot"] in changed else ""
        rows.append(f'<tr{mark}><th scope="row">{_e(s["slot"])}</th><td><code>{_e(s.get("product") or "")}</code></td>'
                    f'<td>{fill if not keep else "&mdash;"}</td><td>{price}</td><td>{keep}</td>'
                    f'<td class="rp-why">{_e(s.get("reason") or "")}</td></tr>')
    return ('<div class="fc-wrap"><table class="rc-outcome rp-plan"><thead><tr><th scope="col">Slot</th>'
            '<th scope="col">SKU</th><th scope="col">Fill to</th><th scope="col">Price</th><th scope="col">Keep</th>'
            f'<th scope="col">Reason</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>')


def _draft_item(d: dict) -> str:
    if d["slots"] is None:
        moved = f'<span class="dim">{_e(d["unreadable"])}</span>'
    elif d["changes"]:
        moved = "".join(_pill(f'{c["slot"]} {lab}', dashboard._change_tone(lab)) for c in d["changes"] for lab in c["labels"])
    else:
        moved = '<span class="dim">no slot changes</span>'
    use = f' data-use="{_e(d["use"])}"' if d["use"] else ""
    return (f'<li{use}><b>Draft {int(d["n"])}</b> &middot; turn {int(d["turn"])} &middot; {_time(d["at"], "etclock")} '
            f'{_pill(d["status"], _DRAFT_TONE[d["status"]])}<div class="chips">{moved}</div></li>')


def _plan_html(data: dict) -> str:
    """The recorded plan, else the latest readable draft in this batch; on a refresh with neither, a marker
    that tells the page to keep the plan it shows."""
    plan, drafts = data["plan"], data["drafts"]
    readable = [d for d in drafts if d["slots"] is not None]
    if plan is not None:
        top = (f'<p class="sub">recorded &middot; {_e(plan["visit"])} visit'
               + (f' &middot; {_e(plan["reason"])}' if plan["reason"] else "") + '</p>' + _plan_table(plan["slots"]))
        current = f'<div id="rp-plan">{top}</div>'
    elif readable:
        last = readable[-1]
        top = (f'<p class="sub">latest draft &middot; turn {int(last["turn"])} &middot; '
               f'{_pill(last["status"], _DRAFT_TONE[last["status"]])}</p>'
               + _plan_table(last["slots"], frozenset(c["slot"] for c in last["changes"])))
        current = f'<div id="rp-plan">{top}</div>'
    elif data["after"]:
        current = '<div id="rp-plan" data-keep="1"></div>'
    else:
        current = '<div id="rp-plan"><p class="empty">No plan yet.</p></div>'
    replies = "".join(f'<li data-use="{_e(r["use"])}">{_pill(r["status"], _DRAFT_TONE[r["status"]])}</li>'
                      for r in data["replies"])
    return (current + f'<h3>Drafts</h3><ol class="rp-drafts" id="rp-drafts">{"".join(map(_draft_item, drafts))}</ol>'
            + f'<template id="rp-replies">{replies}</template>')


# The page's refresh: ask for the steps after the last one shown, append them and any new drafts, put each late
# preview reply's status on its draft, and swap the figures, the step summary and the plan. Stops once the run has
# ended and nothing is left to load. A failed fetch says so and tries again.
SCRIPT = """(function () {
  var every = %d, header = %s;
  function swap(next, id) {
    var here = document.getElementById(id), there = next.querySelector('#' + id);
    if (here && there) here.replaceWith(there);
  }
  function append(next, id) {
    var here = document.getElementById(id), there = next.querySelector('#' + id);
    if (here && there) while (there.firstChild) here.appendChild(there.firstChild);
  }
  function tick() {
    var main = document.querySelector('main[data-run]'), headers = {};
    headers[header] = '1';
    fetch(location.pathname + '?after=' + main.dataset.last, {headers: headers, cache: 'no-store', credentials: 'same-origin'})
      .then(function (r) { if (!r.ok) throw new Error(String(r.status)); return r.text(); })
      .then(function (text) {
        var doc = new DOMParser().parseFromString(text, 'text/html');
        var next = doc.querySelector('main[data-run]');
        if (!next) throw new Error('signed out');
        if (next.dataset.after !== main.dataset.last) throw new Error('out of step');
        swap(next, 'rp-top'); swap(next, 'rp-summary'); append(next, 'rp-steps'); append(next, 'rp-drafts');
        if (next.querySelector('#rp-steps li')) { var none = document.getElementById('rp-none'); if (none) none.remove(); }
        var late = next.querySelector('#rp-replies');
        if (late) late.content.querySelectorAll('li[data-use]').forEach(function (u) {
          document.querySelectorAll('#rp-drafts li[data-use]').forEach(function (li) {
            var pill = li.querySelector(':scope > .pill');
            if (li.dataset.use === u.dataset.use && pill) pill.outerHTML = u.innerHTML;
          });
        });
        var plan = next.querySelector('#rp-plan');
        if (plan && !plan.hasAttribute('data-keep')) swap(next, 'rp-plan');
        swap(next, 'live');
        main.dataset.last = next.dataset.last; main.dataset.running = next.dataset.running;
        main.dataset.more = next.dataset.more;
        document.title = doc.title || document.title;
        if (next.dataset.more === '1') setTimeout(tick, 0);
        else if (next.dataset.running === '1') setTimeout(tick, every);
      })
      .catch(function (error) {
        var live = document.getElementById('live');
        if (live) { live.textContent = 'refresh failed (' + error.message + '); trying again'; live.className = 'failed'; }
        setTimeout(tick, every);
      });
  }
  var main = document.querySelector('main[data-run]');
  setTimeout(tick, main && main.dataset.more === '1' ? 0 : every);
})();"""

STYLE = """
.rp-figs { display:grid; grid-template-columns:repeat(auto-fit, minmax(170px, 1fr)); gap:10px 16px }
.rp-fig { display:flex; flex-direction:column; gap:2px; min-width:0 }
.rp-fig .sv { font-size:16px; font-weight:600; font-variant-numeric:tabular-nums; display:flex; align-items:center; gap:8px; flex-wrap:wrap }
.rp-fig.warn .sv { color:var(--warn) } .rp-fig.bad .sv { color:var(--bad) }
.rp-meter { display:inline-block; width:80px; height:8px; border-radius:4px; background:var(--sunk); border:1px solid var(--line); overflow:hidden }
.rp-meter i { display:block; height:100%; background:var(--accent) }
.rp-fig.warn .rp-meter i { background:var(--warn) } .rp-fig.bad .rp-meter i { background:var(--bad) }
.rp-detail { margin:8px 0 0; font-size:13px; overflow-wrap:anywhere; white-space:pre-wrap }
.rp-plan td.rp-why { text-align:left; color:var(--muted); min-width:12em; overflow-wrap:anywhere }
.rp-plan td:nth-child(2), .rp-plan th:nth-child(2), .rp-plan th:last-child { text-align:left }
.rp-plan tr.rp-changed > * { background:var(--warn-soft) }
.rp-drafts { list-style:none; margin:0; padding:0; font-size:13px }
.rp-drafts li { padding:6px 0; border-top:1px dashed var(--line) }
.fc-wrap { overflow-x:auto }
"""
