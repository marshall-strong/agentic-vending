"""One agent run, followed as it happens: `/dashboard/run/<id>` (and `/run/<id>` on `python -m vending.dashboard`).

    /dashboard/run/<id>        the page; while the run says `running` its script fetches the page again
                               every POLL_SECONDS and swaps it in (no websocket)
    /dashboard/run/<id>.json   the same data as JSON

It shows each step as `agent_steps` records it (the Runs panel's "How it decided", unfolded), the draft plans
as the run previewed them (each `preview_desired_state` call and whether its preview accepted it), the plan the
run recorded once it has one, the model spend so far against the run cap, and how the run ended. A finished
run shows the same page, complete, and stops polling.

Where each figure comes from:
- **Steps**: `agent_steps`, oldest first. Reasoning is the summary the API returns, never the raw chain of
  thought; a step marked `is_error` shows `dashboard.HIDDEN`, never its text; a failed run's `detail` too.
- **Drafts**: the `desired` argument of each `preview_desired_state` call in the steps, after the steps are
  scoped to our assignment, and `ok` from its reply. A call stored cut short (`agent_runs.MAX_CALL_TEXT`)
  can't be read slot by slot and says so.
- **Spend so far**: the sum of `model_calls.cost_usd` for the run's harness (its rows are labelled
  `agent run <id> turn <n>`; a price lookup through the same harness shares its `run_id`). A call still
  `pending` counts at its worst case, as the harness reserved it, so the figure can only overstate. Once the
  run ends, `agent_runs.cost_usd` (what the harness counted) is the run's cost.
- **Cap**: `model_run_cap_cents` in `config` now (the harness's default when it isn't set). A run uses the
  cap it read when it started.

Only live runs: the live page leaves dry runs and replays out, as the Runs panel does. A run from before the
ledger's opening is shown and marked as development spend. Every run and step goes through the Runs panel's
own scoping (`dashboard.scope_run`): our assignment only (`vending.slots.our_slots`), nothing from before it.
Reads only, in one repeatable-read, read-only transaction. No model calls.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from functools import partial
from typing import Any

import psycopg
from psycopg import sql
from psycopg.types.json import set_json_loads

from vending import dashboard
from vending import stock as stock_mod
from vending.harness import DEFAULT_RUN_CAP_CENTS
from vending.slots import SlotsNotRecorded, our_slots

ROUTE = "/dashboard/run/"
DEMO_ROUTE = "/dashboard/demo/run/"
POLL_SECONDS = 5
PREVIEW = "preview_desired_state"
CAP_KEY = "model_run_cap_cents"
PLAN_FIELDS = ("product", "fill_to", "sale_price", "keep", "reason")


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


# --- Data ----------------------------------------------------------------------------------------

def read(conn: psycopg.Connection, run_id: int, now: datetime | None = None, public: bool = True) -> dict:
    """Everything the page shows about run `run_id`, from one consistent moment. Reads only."""
    set_json_loads(partial(json.loads, parse_float=Decimal), context=conn)
    with conn.transaction():
        conn.execute("set transaction isolation level repeatable read, read only")
        now = now or conn.execute("select transaction_timestamp()").fetchone()[0]
        return _read(conn, run_id, now, public)


def _read(conn: psycopg.Connection, run_id: int, now: datetime, public: bool) -> dict:
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
    try:
        steps = dashboard._rows(conn, "select turn, at, kind, tool, content, is_error from agent_steps"
                                      " where run_id = %s order by id", (run_id,))
    except dashboard.Missing:
        steps = []
    previous = dashboard._optional(
        conn, "select plan from agent_runs where mode = 'live' and plan is not null"
              " and (started_at, id) < (%s, %s) order by started_at desc, id desc limit 1",
        (run["started_at"], run_id))
    plan = run.pop("plan")
    run["steps"] = steps
    run["stale"] = run["status"] == "running" and now - run["started_at"] > dashboard.STALE_RUN
    # The error text of a failed run or step never leaves the database (`dashboard.hide_errors`).
    if run["status"] == "failed" and run["detail"]:
        run["detail"] = dashboard.HIDDEN
    for step in steps:
        if step["is_error"]:
            step["content"] = dashboard.HIDDEN
    old = dashboard._older_assignment(run["started_at"], assignment_at)
    dashboard.scope_run(run, set(ours), assignment_at)
    out.update(
        run=run, opened_at=opened, before_opening=opened is None or run["started_at"] < opened,
        last_step_at=max((s["at"] for s in steps), default=None) if run["steps"] else None,
        spend=_spend(conn, run_id), cap=_cap(conn),
        drafts=[] if old else _drafts(run["steps"], None if previous is None else previous["plan"], ours),
        plan=None if old else _scope_plan(plan, ours),
    )
    return out


def _spend(conn: psycopg.Connection, run_id: int) -> dict | None:
    """What the run's model calls cost so far (`model_calls`), or None when that table can't be read."""
    try:
        row = dashboard._optional(
            conn, "select coalesce(sum(cost_usd), 0) as spent, count(*) as calls,"
                  " count(*) filter (where status = 'pending') as pending"
                  " from model_calls where run_id in (select run_id from model_calls where label like %s)",
            (f"agent run {int(run_id)} turn %",))
    except dashboard.Missing:
        return None
    return row


def _cap(conn: psycopg.Connection) -> dict:
    row = dashboard._optional(conn, "select value from config where key = %s", (CAP_KEY,))
    try:
        cents = int(row["value"]) if row is not None else DEFAULT_RUN_CAP_CENTS
    except (TypeError, ValueError):
        cents = None
    return {"usd": None if cents is None else Decimal(cents) / 100, "set": row is not None}


def _slots(plan: Any, ours: tuple[str, ...]) -> list[dict] | None:
    """A plan's entries for our slots, in our order, with the fields the page shows; None if it isn't a plan."""
    if not isinstance(plan, dict) or not isinstance(plan.get("slots"), list):
        return None
    by_slot = {str(e.get("slot")): e for e in plan["slots"] if isinstance(e, dict)}
    out = []
    for slot in ours:
        if slot in by_slot:
            entry = {k: by_slot[slot][k] for k in PLAN_FIELDS if k in by_slot[slot]}
            out.append({"slot": slot, **dashboard._scope_text(entry, set(ours))})
    return out


def _scope_plan(plan: Any, ours: tuple[str, ...]) -> dict | None:
    slots = _slots(plan, ours)
    if slots is None:
        return None
    reason = plan.get("reason") if isinstance(plan.get("reason"), str) else None
    return {"visit": "midweek" if plan.get("off_cycle") or plan.get("visit") in ("midweek", "off_cycle")
            else "scheduled", "reason": dashboard._scope_text(reason, set(ours)), "slots": slots}


def _drafts(steps: list[dict], previous: Any, ours: tuple[str, ...]) -> list[dict]:
    """Each `preview_desired_state` call, oldest first: its plan for our slots, what moved from the draft
    before it (the first, from the last live plan), and whether its preview accepted it."""
    out, before = [], previous
    for i, step in enumerate(steps):
        if step["kind"] != "tool_call" or step["tool"] != PREVIEW:
            continue
        reply = next((s for s in steps[i + 1:] if s["kind"] == "tool_result" and s["tool"] == PREVIEW), None)
        draft = {"turn": step["turn"], "at": step["at"], "status": _preview_status(reply), "slots": None,
                 "changes": [], "unreadable": None}
        try:
            desired = json.loads(step["content"], parse_float=Decimal)
        except ValueError:
            desired, draft["unreadable"] = None, "arguments not readable (stored cut short, or omitted)"
        else:
            desired = desired.get("desired") if isinstance(desired, dict) else None
        slots = _slots(desired, ours)
        if slots is None:
            draft["unreadable"] = draft["unreadable"] or "no desired state in the arguments"
        else:
            draft["slots"] = slots
            draft["visit"] = "midweek" if desired.get("off_cycle") else "scheduled"
            if before is not None:
                draft["changes"] = dashboard._plan_changes(desired, before, None, ours)
            before = desired
        out.append(draft)
    return out


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
    """The page. While the run is in progress (and not in the demo) its script polls the page itself."""
    links = "".join(f' &middot; <a href="{_e(href)}">{_e(label)}</a>' for label, href in nav)
    live = running(data) and not demo
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
    reload = (f'<noscript><meta http-equiv="refresh" content="{POLL_SECONDS}"></noscript>' if live else "")
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            + ('<meta name="robots" content="noindex">' if demo else "") + reload
            + f'<title>{dashboard.DEMO_TITLE if demo else ""}Run #{int(data["run_id"])} &middot; {_e(status)}'
            f' &middot; Agent dashboard</title><style>{dashboard.STYLE}{STYLE}</style></head><body>{banner}'
            f'<main data-run="{int(data["run_id"])}" data-running="{int(live)}">{head}{body}</main>'
            + (f'<script>{SCRIPT % (POLL_SECONDS * 1000, json.dumps(dashboard.visits.REFRESH_HEADER))}</script>' if live else "")
            + '</body></html>')


def _fig(label: str, value: str, sub: str = "", tone: str = "") -> str:
    return (f'<div class="rp-fig{" " + tone if tone else ""}"><span class="label">{label}</span>'
            f'<span class="sv">{value}</span>' + (f'<span class="sub">{sub}</span>' if sub else "") + '</div>')


def _run_html(data: dict) -> str:
    run, now = data["run"], data["as_of"]
    end = run["finished_at"] or now
    pills = [dashboard._run_pill(run)]
    if data["before_opening"]:
        pills.append(_pill("before the ledger opening: development spend, not the $600", "muted"))
    figs = [
        _fig("Status", " ".join(pills), f'{_e(run["mode"])} &middot; {_e(run["model"] or "model not recorded")}'),
        _fig("Started", _time(run["started_at"], "etday"),
             ("ran " if run["finished_at"] else "running ") + _span(end - run["started_at"])),
        _spend_fig(data),
        _fig("Steps", str(len(run["steps"])),
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
    steps = (dashboard._steps_html(run["id"], run["steps"], open_=True) if run["steps"]
             else '<p class="empty">No steps recorded yet.</p>')
    return (f'<section class="rp-top"><div class="rp-figs">{"".join(figs)}</div>{ended}</section>'
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


def _plan_html(data: dict) -> str:
    plan, drafts = data["plan"], data["drafts"]
    out = []
    if plan is not None:
        out.append(f'<p class="sub">recorded &middot; {_e(plan["visit"])} visit'
                   + (f' &middot; {_e(plan["reason"])}' if plan["reason"] else "") + '</p>')
        out.append(_plan_table(plan["slots"]))
    elif drafts and drafts[-1]["slots"] is not None:
        last = drafts[-1]
        out.append(f'<p class="sub">latest draft &middot; turn {int(last["turn"])} &middot; '
                   f'{_pill(last["status"], _DRAFT_TONE[last["status"]])}</p>')
        out.append(_plan_table(last["slots"], frozenset(c["slot"] for c in last["changes"])))
    else:
        out.append('<p class="empty">No plan yet.</p>')
    if drafts:
        items = []
        for n, d in enumerate(drafts, 1):
            if d["slots"] is None:
                moved = f'<span class="dim">{_e(d["unreadable"])}</span>'
            elif d["changes"]:
                moved = "".join(_pill(f'{c["slot"]} {lab}', dashboard._change_tone(lab))
                                for c in d["changes"] for lab in c["labels"])
            else:
                moved = '<span class="dim">no slot changes</span>'
            items.append(f'<li><b>Draft {n}</b> &middot; turn {int(d["turn"])} &middot; {_time(d["at"], "etclock")} '
                         f'{_pill(d["status"], _DRAFT_TONE[d["status"]])}<div class="chips">{moved}</div></li>')
        out.append(f'<h3>Drafts ({len(drafts)})</h3><ol class="rp-drafts">{"".join(items)}</ol>')
    return "".join(out)


# Polls while the run is in progress: fetch this page, swap <main> in, keep open <details> and the scroll.
# Stops when the page says the run is over. A failed fetch says so and tries again.
SCRIPT = """(function () {
  var every = %d, header = %s;
  function openKeys(root) {
    var keys = [];
    root.querySelectorAll('details[data-key]').forEach(function (d) { if (!d.open) keys.push(d.dataset.key); });
    return keys;
  }
  function tick() {
    var main = document.querySelector('main'), headers = {};
    headers[header] = '1';
    fetch(location.pathname + location.search, {headers: headers, cache: 'no-store', credentials: 'same-origin'})
      .then(function (r) { if (!r.ok) throw new Error(String(r.status)); return r.text(); })
      .then(function (text) {
        var next = new DOMParser().parseFromString(text, 'text/html').querySelector('main[data-run]');
        if (!next) throw new Error('signed out');
        var closed = openKeys(main), y = window.scrollY;
        next.querySelectorAll('details[data-key]').forEach(function (d) {
          if (closed.indexOf(d.dataset.key) >= 0) d.open = false;
        });
        main.replaceWith(next);
        document.title = new DOMParser().parseFromString(text, 'text/html').title || document.title;
        window.scrollTo(0, y);
        if (next.dataset.running === '1') setTimeout(tick, every);
      })
      .catch(function (error) {
        var live = document.getElementById('live');
        if (live) { live.textContent = 'refresh failed (' + error.message + '); trying again'; live.className = 'failed'; }
        setTimeout(tick, every);
      });
  }
  setTimeout(tick, every);
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
