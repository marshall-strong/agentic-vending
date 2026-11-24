"""The dashboard's plan history: one card per week (Saturday to Friday, Eastern, the ledger's weeks), newest first,
the last `WEEKS_SHOWN`, from the ledger's opening.

Each card holds the week's plans and every update to them:

- **The day-one fill** (`first_fill_plans`: the picked plan, else the latest), in the week its fill request was sent
  (the first request standing since the opening and sent before the start, as Key dates reads it), else the opening's
  week. Its request is that fill request.
- **Each live run that left a plan** (`agent_runs`), newest first: when, the model, how it ended, the model cost
  (`cost_usd`), the purchase request it sent (`purchase_requests`: what it paid once fulfilled, else the most it can
  cost, the money panel's worst case), the plan's reason, the weekly update and the notes.
- **Each slot of ours the plan changed** from the plan before it (the previous live plan since the opening, else the
  day-one fill), what it was and what it is now (product, price, fill), the agent's reason, and **the inputs the run
  read for that slot**: each tool call in `agent_steps` whose arguments name the slot or the product it held or
  takes, with the reply. Nothing is inferred: a reason that cites nothing is shown as it is, and a slot no call named
  lists no inputs. The run's other calls are listed by tool name.
- **The report card of the week before** (the snapshot's `report_cards`), as the report card graded it.
- Runs that left no plan (failed, halted, still running) by number.

The live page follows live runs only, from the opening (docs/dashboard.md, "What the live page leaves out"): dry runs
and replays are counted, not shown. Only selections in the stored assignment appear (`ours`, as the snapshot read
it): a change to another selection isn't listed, a poll reply keeps only the slot's own row, and any other reply goes
through the dashboard's own payload filter (`dashboard._scope_payload`). Text from before the current assignment is
replaced, and a failed call's reply is never shown. Reads only, inside the dashboard's read-only snapshot.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from html import escape
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import set_json_loads

from vending import ledger, money_panel
from vending.price_check_panel import et_day, et_moment

WEEKS_SHOWN = 8
WEEK = timedelta(days=7)
# Characters of a text reply kept as an input.
REPLY_CHARS = 200
# Scalar fields of a JSON reply kept as an input.
REPLY_FIELDS = 8
_SLOT_ARGS = ("slot", "selection", "into_slot")
RUN_TONE = {"running": "info", "submitted": "ok", "skipped": "muted", "halted": "warn", "failed": "bad"}
REQUEST_TONE = {"pending": "info", "fulfilled": "ok", "rejected": "bad"}


def _eastern():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/New_York")
    except Exception:   # Windows without the tzdata package
        return timezone.utc


def _start(week: date) -> datetime:
    return datetime.combine(week, time.min, _eastern())


# --- Reading ------------------------------------------------------------------------------------

def read(conn: psycopg.Connection, now: datetime, ours: tuple[str, ...], assignment_at: datetime | None, *,
         old_text: str, cards: list[dict] | None = None) -> dict:
    """The panel's data. `ours` is the stored assignment the snapshot read; `assignment_at` when it was first read
    (text from runs before it is replaced by `old_text`); `cards` the snapshot's report cards, when they read."""
    opened = ledger.opening(conn)
    if opened is None or opened.at > now:
        return {"opened": False, "opens_at": opened.at if opened else None, "weeks": []}
    zone = _eastern()
    first = ledger.week_of(opened.at.astimezone(zone))
    current = ledger.week_of(now.astimezone(zone))
    weeks = [current - WEEK * n for n in range(WEEKS_SHOWN) if current - WEEK * n >= first]
    since = max(_start(weeks[-1]), opened.at)
    cur = conn.cursor(row_factory=dict_row)
    columns = ("id, started_at, finished_at, mode, model, status, plan, team_update, notes, cost_usd, request_id")
    runs = cur.execute(f"select {columns} from agent_runs where mode = 'live' and started_at >= %s"
                       " and started_at <= %s order by started_at, id", (since, now)).fetchall()
    before = cur.execute("select id, plan from agent_runs where mode = 'live' and plan is not null"
                         " and started_at >= %s and started_at < %s order by started_at desc, id desc limit 1",
                         (opened.at, since)).fetchone()
    hidden = cur.execute("select count(*) as n from agent_runs where mode <> 'live' and started_at >= %s"
                         " and started_at <= %s", (since, now)).fetchone()["n"]
    fill = _fill(conn, ours, opened.at, now)
    request_ids = [r["request_id"] for r in runs if r["request_id"]]
    if fill and fill.get("request_id"):
        request_ids.append(fill["request_id"])
    requests = _requests(conn, request_ids)
    steps = _steps(conn, [r["id"] for r in runs if r["plan"] is not None])

    from vending import dashboard as d   # here: the dashboard imports this module

    base: tuple[str, Any] | None = None
    if before is not None:
        base = (f"#{before['id']}", d._by_slot(before["plan"]))
    elif fill is not None and fill.get("entries") is not None:
        base = ("day-one fill", fill["entries"])
    built: list[dict] = []
    for run in runs:
        older = assignment_at is None or run["started_at"] < assignment_at
        if run["plan"] is None:
            built.append({"id": run["id"], "started_at": run["started_at"], "status": run["status"], "plan": False})
            continue
        now_slots = d._by_slot(run["plan"])
        changes = _changes(now_slots, base[1] if base else None, ours, steps.get(run["id"], []), older, old_text)
        plan = run["plan"] if isinstance(run["plan"], dict) else {}
        built.append({
            "id": run["id"], "started_at": run["started_at"], "finished_at": run["finished_at"], "plan": True,
            "mode": run["mode"], "model": run["model"], "status": run["status"], "cost_usd": run["cost_usd"],
            "visit": plan.get("visit"), "reason": old_text if older and plan.get("reason") else plan.get("reason"),
            "update": None if older else run["team_update"], "notes": None if older else run["notes"],
            "compared_with": base[0] if base else None, "changes": changes,
            "other_calls": [] if older else _other_calls(steps.get(run["id"], []), changes),
            "request": requests.get(run["request_id"]) if run["request_id"] else None,
            "request_id": run["request_id"]})
        base = (f"#{run['id']}", now_slots)
    _name_products(conn, built, fill)
    grades = {c.get("week"): c.get("grade_text") for c in cards or [] if isinstance(c, dict)}
    fill_week = None
    if fill is not None:
        sent = (requests.get(fill.get("request_id")) or {}).get("submitted_at")
        fill_week = ledger.week_of((sent or opened.at).astimezone(zone))
        fill["request"] = requests.get(fill.get("request_id")) if fill.get("request_id") else None
        fill.pop("entries", None)
    out = []
    for week in weeks:
        start, end = _start(week), _start(week + WEEK)
        in_week = [r for r in built if start <= r["started_at"] < end]
        out.append({"week": week, "ends": week + timedelta(days=6), "current": week == current,
                    "report_card": grades.get((week - WEEK).isoformat()),
                    "fill": fill if fill is not None and fill_week == week else None,
                    "plans": [r for r in reversed(in_week) if r["plan"]],
                    "other_runs": [{"id": r["id"], "status": r["status"], "started_at": r["started_at"]}
                                   for r in reversed(in_week) if not r["plan"]]})
    return {"opened": True, "opened_at": opened.at, "weeks": out, "hidden_runs": hidden}


def _fill(conn: psycopg.Connection, ours: tuple[str, ...], opened_at: datetime, now: datetime) -> dict | None:
    """The day-one fill: its plan (cut to our selections) and its request, as Key dates finds it."""
    from vending import plan_rounds
    from vending.start import START

    try:
        with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            set_json_loads(json.loads, context=cur)     # the plan loader's decoder, as `dashboard._first_fill` uses
            row = cur.execute("select id, loaded_at, label, plan, projection, slots, picked from first_fill_plans"
                              " order by picked desc, loaded_at desc, id desc limit 1").fetchone()
    except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn):
        return None
    if row is None:
        return None
    request = conn.execute("select request_id from purchase_requests where status <> 'rejected'"
                           " and submitted_at >= %s and submitted_at <= %s and submitted_at < %s"
                           " order by submitted_at, request_id limit 1", (opened_at, now, START)).fetchone()
    out = {"plan_id": row["id"], "loaded_at": row["loaded_at"], "picked": row["picked"], "lines": [],
           "message": None, "entries": None, "request_id": request[0] if request else None}
    if not ours or set(row["slots"]) != set(ours):
        out["message"] = "planned for an earlier assignment"
        return out
    try:
        checked = plan_rounds.parse_round(row["label"], row["plan"], row["projection"], ours, saved_at=row["loaded_at"])
    except Exception:   # noqa: BLE001 - a plan that doesn't parse is said, not drawn
        out["message"] = "the stored plan doesn't parse"
        return out
    lines = [s for s in checked.slots.values() if s.slot in ours]
    out["lines"] = [{"slot": s.slot, "sku": s.sku, "product": s.product, "price": _dollars(s.price_cents),
                     "units": s.units} for s in lines]
    out["entries"] = {s.slot: {"slot": s.slot, "product": s.sku, "sale_price": _dollars(s.price_cents),
                               "fill_to": s.units} for s in lines}
    out["expected_cents"], out["most_cents"] = checked.fill_expected_cents, checked.fill_most_cents
    return out


def _dollars(cents: Any) -> str | None:
    if cents is None:
        return None
    value = Fraction(cents)
    return str((Decimal(value.numerator) / Decimal(value.denominator) / 100).quantize(Decimal("0.01"), ROUND_HALF_UP))


def _requests(conn: psycopg.Connection, ids: list[str]) -> dict[str, dict]:
    """Each request's status and cost: paid once fulfilled (plus a midweek visit's fee), else the most it can cost."""
    if not ids:
        return {}
    rows = conn.cursor(row_factory=dict_row).execute(
        "select request_id, submitted_at, off_cycle, status, total_paid, visit_fee_cents, request_body"
        " from purchase_requests where request_id = any(%s)", (list(set(ids)),)).fetchall()
    out = {}
    for r in rows:
        fee = r["visit_fee_cents"] if r["off_cycle"] and isinstance(r["visit_fee_cents"], int) else None
        most, unpriced = money_panel._worst_case(r["request_body"])
        if r["off_cycle"]:
            most += fee if fee is not None else ledger.DEFAULT_MIDWEEK_FEE_CENTS
        out[r["request_id"]] = {
            "id": r["request_id"], "submitted_at": r["submitted_at"], "status": r["status"],
            "off_cycle": r["off_cycle"],
            "paid": None if r["total_paid"] is None else _dollars(Fraction(Decimal(r["total_paid"])) * 100),
            "visit_fee": _dollars(fee) if fee is not None else None,
            "most": None if unpriced else _dollars(most)}
    return out


def _steps(conn: psycopg.Connection, run_ids: list[int]) -> dict[int, list[dict]]:
    """Each run's tool calls and replies, oldest first. Empty before migration 014 adds `agent_steps`."""
    if not run_ids:
        return {}
    try:
        with conn.transaction():
            rows = conn.cursor(row_factory=dict_row).execute(
                "select run_id, kind, tool, content, is_error from agent_steps where run_id = any(%s)"
                " and kind in ('tool_call', 'tool_result') order by run_id, id", (run_ids,)).fetchall()
    except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn):
        return {}
    out: dict[int, list[dict]] = {}
    for row in rows:
        out.setdefault(row.pop("run_id"), []).append(row)
    return out


def _calls(steps: list[dict]) -> list[dict]:
    """Each tool call with the reply that followed it."""
    out: list[dict] = []
    for step in steps:
        if step["kind"] == "tool_call":
            try:
                args = json.loads(step["content"], parse_float=Decimal)
            except ValueError:
                args = None
            out.append({"tool": step["tool"], "args": args if isinstance(args, dict) else {}, "reply": None,
                        "failed": False, "replied": False})
        elif out and not out[-1]["replied"] and out[-1]["tool"] == step["tool"]:
            out[-1].update(reply=None if step["is_error"] else step["content"], failed=step["is_error"], replied=True)
    return out


def _names_slot(args: dict, slot: str, skus: set[str]) -> bool:
    """Whether a call is about `slot`: a slot argument, when it has one, decides (so a call about another
    selection, ours or not, is never this slot's input); else an argument naming the slot's product."""
    named = [str(args[k]) for k in _SLOT_ARGS if args.get(k) is not None]
    if named:
        return all(n == slot for n in named)
    return any(isinstance(v, str) and v in skus for v in args.values())


def _input(call: dict, slot: str, ours: set[str]) -> dict:
    """One call as an input to `slot`'s decision: its scalar arguments and what came back, scoped to our selections."""
    from vending import dashboard as d

    args = {k: v for k, v in call["args"].items() if isinstance(v, (str, int, Decimal, bool)) or v is None}
    out = {"tool": call["tool"], "args": args, "failed": call["failed"], "reply": None, "omitted": False}
    if call["failed"] or call["reply"] is None:
        return out
    try:
        value = json.loads(call["reply"], parse_float=Decimal)
    except ValueError:
        value = None
    if call["tool"] in d._POLL_REPLY_TOOLS:
        scoped = d._scope_poll_reply(value, ours) if value is not None else d._DROP
        rows = [] if scoped is d._DROP else [r for r in scoped["data"] if str(r.get("slot")) == slot]
        if rows:
            out["reply"] = {k: v for k, v in rows[0].items() if k != "slot"}
        else:
            out["omitted"] = True       # a poll reply that can't be cut to this slot's row isn't shown
        return out
    if value is None:
        out["reply"] = call["reply"][:REPLY_CHARS]
        return out
    scoped = d._scope_payload(value, ours)
    if scoped is d._DROP or not isinstance(scoped, dict):
        out["omitted"] = True
        return out
    for key in ("structuredContent", "result"):
        if isinstance(scoped.get(key), dict):
            scoped = scoped[key]
    fields = {k: v for k, v in scoped.items() if isinstance(v, (str, int, Decimal, bool)) and k != "slot"}
    out["reply"] = dict(list(fields.items())[:REPLY_FIELDS]) or None
    out["omitted"] = not fields
    return out


def _view(entry: dict | None) -> dict | None:
    if entry is None:
        return None
    return {"product": entry.get("product"), "price": None if entry.get("sale_price") is None else str(entry["sale_price"]),
            "fill_to": entry.get("fill_to"), "keep": bool(entry.get("keep"))}


def _changes(now_slots: dict, before: dict | None, ours: tuple[str, ...], steps: list[dict], older: bool,
             old_text: str) -> list[dict]:
    """Each slot of ours whose ask moved from the plan before (every planned slot when there is none), in `ours`'
    order, with what drove it: the run's calls that named the slot or its product."""
    from vending import dashboard as d

    calls = [] if older else _calls(steps)
    out = []
    for slot in ours:
        new, old = now_slots.get(slot), (before or {}).get(slot)
        if before is not None and d._what(new) == d._what(old):
            continue
        if before is None and new is None:
            continue
        labels = (d._changes(new, old) if new is not None else ["dropped"]) if before is not None else ["new"]
        skus = {e.get("product") for e in (new, old) if e and isinstance(e.get("product"), str)}
        inputs = [_input(c, slot, set(ours)) for c in calls if _names_slot(c["args"], slot, skus)]
        reason = new.get("reason") if new else None
        out.append({"slot": slot, "labels": labels, "old": _view(old), "new": _view(new),
                    "reason": old_text if older and reason else reason, "inputs": inputs})
    return out


def _other_calls(steps: list[dict], changes: list[dict]) -> list[dict]:
    """The run's calls that named none of the changed slots, by tool, in the order first called."""
    named: list[tuple[str, dict]] = []
    counts: dict[str, int] = {}
    for call in _calls(steps):
        hit = any(_names_slot(call["args"], c["slot"], {e["product"] for e in (c["new"], c["old"])
                                                         if e and isinstance(e.get("product"), str)})
                  for c in changes)
        if not hit:
            counts[call["tool"]] = counts.get(call["tool"], 0) + 1
            if counts[call["tool"]] == 1:
                named.append((call["tool"], call))
    return [{"tool": tool, "n": counts[tool]} for tool, _ in named]


def _name_products(conn: psycopg.Connection, runs: list[dict], fill: dict | None) -> None:
    """The catalog's name beside each SKU the cards show (ours only)."""
    skus = {v["product"] for r in runs if r["plan"] for c in r["changes"] for v in (c["old"], c["new"])
            if v and isinstance(v.get("product"), str)}
    skus |= {x["sku"] for x in (fill or {}).get("lines", []) if isinstance(x.get("sku"), str)}
    if not skus:
        return
    try:
        with conn.transaction():
            names = dict(conn.execute("select sku, name from catalog_products where sku = any(%s)",
                                      (sorted(skus),)).fetchall())
    except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn):
        return
    for r in runs:
        for c in r.get("changes", []):
            for v in (c["old"], c["new"]):
                if v and v.get("product") in names:
                    v["name"] = names[v["product"]]
    for x in (fill or {}).get("lines", []):
        x["name"] = names.get(x["sku"]) or x["product"]


# --- Page ---------------------------------------------------------------------------------------

def _e(value: Any) -> str:
    return "" if value is None else escape(str(value))


def _pill(text: str, tone: str) -> str:
    return f'<span class="pill {tone}">{_e(text)}</span>'


def _money(value: Any) -> str:
    if value is None:
        return '<span class="dim">not recorded</span>'
    return f"${Decimal(value).quantize(Decimal('0.01'), ROUND_HALF_UP)}"


def _request_html(req: dict | None, request_id: str | None) -> str:
    if request_id is None:
        return '<span class="dim">none sent</span>'
    if req is None:
        return f'<code>{_e(request_id)}</code> <span class="dim">not in purchase_requests</span>'
    cost = ""
    if req["status"] == "fulfilled":
        cost = f'paid ${_e(req["paid"])}' if req["paid"] is not None else '<span class="dim">paid: unknown</span>'
        if req["visit_fee"]:
            cost += f' + ${_e(req["visit_fee"])} visit fee'
    elif req["status"] == "rejected":
        cost = "$0.00"
    else:
        cost = (f'at most ${_e(req["most"])}' if req["most"] is not None else
                '<span class="dim">at most: unknown (a line has no price)</span>')
    kind = " &middot; midweek" if req["off_cycle"] else ""
    return (f'<code>{_e(req["id"])}</code> {_pill(req["status"], REQUEST_TONE.get(req["status"], "muted"))}{kind} '
            f'&middot; {cost}')


def _entry_html(v: dict | None) -> str:
    if v is None:
        return '<span class="dim">&mdash;</span>'
    if v["keep"]:
        return "keep"
    parts = []
    if v.get("product"):
        name = v.get("name")
        parts.append((f'{_e(name)} <code>{_e(v["product"])}</code>' if name else f'<code>{_e(v["product"])}</code>'))
    else:
        parts.append('<span class="dim">same product</span>')
    parts.append(f'${_e(v["price"])}' if v["price"] is not None else '<span class="dim">price unchanged</span>')
    if v["fill_to"] is not None:
        parts.append("pull" if v["fill_to"] == 0 else f'fill {_e(v["fill_to"])}')
    return " &middot; ".join(parts)


def _value(v: Any) -> str:
    return _e(v) if not isinstance(v, bool) else ("yes" if v else "no")


def _input_html(i: dict) -> str:
    args = ", ".join(f"{_e(k)}={_value(v)}" for k, v in i["args"].items())
    head = f'<code>{_e(i["tool"])}({args})</code>'
    if i["failed"]:
        return f'{head} <span class="bad-text">failed</span>'
    if i["omitted"]:
        return f'{head} <span class="dim">reply not shown</span>'
    if i["reply"] is None:
        return f'{head} <span class="dim">no reply recorded</span>'
    if isinstance(i["reply"], dict):
        body = ", ".join(f"{_e(k)} {_value(v)}" for k, v in i["reply"].items())
    else:
        body = _e(i["reply"])
    return f'{head} &rarr; {body}'


def _plan_html(r: dict, shown: set[int]) -> str:
    link = (f'<a href="#run-{r["id"]}">#{r["id"]}</a>' if r["id"] in shown else f'#{r["id"]}')
    steps = (f' &middot; <a href="#steps-{r["id"]}">How it decided</a>' if r["id"] in shown else
             ' &middot; <span class="dim">not in the last 10 runs</span>')
    head = (f'<div class="ph-head"><b>{link}</b> {_pill(r["status"], RUN_TONE.get(r["status"], "muted"))} '
            f'{_e(r["mode"])} &middot; {_e(et_moment(r["started_at"]))} &middot; {_e(r["model"] or "model not recorded")}'
            f' &middot; model cost {_money(r["cost_usd"])}{steps}</div>')
    facts = [("Request", _request_html(r["request"], r["request_id"])),
             ("Visit", _e(r["visit"]) if r["visit"] else '<span class="dim">&mdash;</span>')]
    if r["reason"]:
        facts.append(("Plan reason", _e(r["reason"])))
    if r["update"]:
        facts.append(("Weekly update", _e(r["update"])))
    if r["notes"]:
        facts.append(("Notes", _e(r["notes"])))
    facts.append(("Compared with", _e(r["compared_with"]) if r["compared_with"] else '<span class="dim">nothing: first plan</span>'))
    if r["other_calls"]:
        facts.append(("Other calls", ", ".join(f'<code>{_e(c["tool"])}</code>' + (f' &times;{c["n"]}' if c["n"] > 1 else "")
                                                for c in r["other_calls"])))
    dl = '<dl class="ph-facts">' + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in facts) + "</dl>"
    if not r["changes"]:
        table = '<p class="dim ph-none">No slot changes</p>'
    else:
        rows = []
        for c in r["changes"]:
            inputs = ("".join(f"<li>{_input_html(i)}</li>" for i in c["inputs"]) or
                      '<li class="dim">no call named this slot</li>')
            rows.append(f'<tr><td class="ph-slot"><b>{_e(c["slot"])}</b></td>'
                        f'<td>{" ".join(_pill(label, "info") for label in c["labels"])}</td>'
                        f'<td>{_entry_html(c["old"])}</td><td>{_entry_html(c["new"])}</td>'
                        f'<td>{_e(c["reason"]) if c["reason"] else "<span class=dim>no reason recorded</span>"}</td>'
                        f'<td><ul class="ph-inputs">{inputs}</ul></td></tr>')
        table = ('<div class="ph-scroll"><table class="ph-changes"><thead><tr><th>Slot</th><th>Change</th><th>Was</th>'
                 '<th>Now</th><th>Agent&#39;s reason</th><th>Inputs the run read</th></tr></thead><tbody>'
                 f'{"".join(rows)}</tbody></table></div>')
    return f'<div class="ph-run">{head}{dl}{table}</div>'


def _fill_html(f: dict) -> str:
    head = (f'<div class="ph-head"><b>Day-one fill</b> {_pill("picked" if f["picked"] else "latest, not picked", "ok" if f["picked"] else "warn")}'
            f' &middot; plan {_e(f["plan_id"])} loaded {_e(et_moment(f["loaded_at"]))}</div>')
    facts = [("Request", _request_html(f.get("request"), f.get("request_id")))]
    if f["message"]:
        facts.append(("Plan", f'<span class="dim">{_e(f["message"])}</span>'))
    else:
        facts.append(("Lines", str(len(f["lines"]))))
        facts.append(("Fill cost", f'expected ${_e(_dollars(f["expected_cents"]))} &middot; at most '
                                   f'${_e(_dollars(f["most_cents"]))}' if f.get("most_cents") is not None else
                      '<span class="dim">unknown</span>'))
    dl = '<dl class="ph-facts">' + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in facts) + "</dl>"
    lines = ""
    if f["lines"]:
        rows = "".join(f'<tr><td class="ph-slot"><b>{_e(x["slot"])}</b></td><td>{_e(x.get("name") or x["product"])} '
                       f'<code>{_e(x["sku"] or "no SKU yet")}</code></td><td>${_e(x["price"])}</td>'
                       f'<td>{_e(x["units"])}</td></tr>' for x in f["lines"])
        lines = (f'<details class="ph-lines"><summary>{len(f["lines"])} lines</summary><div class="ph-scroll">'
                 '<table class="ph-changes"><thead><tr><th>Slot</th><th>Product</th><th>Price</th><th>Units</th>'
                 f'</tr></thead><tbody>{rows}</tbody></table></div></details>')
    return f'<div class="ph-run ph-fill">{head}{dl}{lines}</div>'


def html(panel: dict, shown_runs: set[int] | None = None) -> str:
    """The panel's inner HTML, from a dashboard `_panel` dict, ok or not. `shown_runs` are the runs the Runs panel
    lists, which the cards link to."""
    if not panel["ok"]:
        return f'<p class="empty">Not available: {_e(panel["why"])}</p>'
    d = panel["data"]
    shown = shown_runs or set()
    if not d["opened"]:
        when = f' &middot; opens {_e(et_moment(d["opens_at"]))}' if d.get("opens_at") else ""
        return f'<p class="empty">No plan history: the ledger hasn&#39;t opened{when}</p>'
    out = []
    if d.get("hidden_runs"):
        out.append(f'<p class="sub">{d["hidden_runs"]} dry runs or replays not shown</p>')
    for w in d["weeks"]:
        tag = f' {_pill("this week", "info")}' if w["current"] else ""
        title = f'<h3>{_e(et_day(_start(w["week"])))} &ndash; {_e(et_day(_start(w["ends"])))}{tag}</h3>'
        facts = []
        if w["report_card"]:
            facts.append(("Report card, week before", _e(w["report_card"])))
        if w["other_runs"]:
            facts.append(("Runs with no plan", ", ".join(
                (f'<a href="#run-{r["id"]}">#{r["id"]}</a>' if r["id"] in shown else f'#{r["id"]}')
                + f' {_e(r["status"])}' for r in w["other_runs"])))
        dl = ('<dl class="ph-facts">' + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in facts) + "</dl>") if facts else ""
        body = (_fill_html(w["fill"]) if w["fill"] else "") + "".join(_plan_html(r, shown) for r in w["plans"])
        if not body:
            body = '<p class="dim ph-none">No live plan this week</p>'
        out.append(f'<article class="ph-card">{title}{dl}{body}</article>')
    return "".join(out)


STYLE = """
/* Plan history (`vending.plan_history`). */
.ph-card { border:1px solid var(--line); border-radius:10px; padding:10px 12px; margin:10px 0; background:var(--sunk) }
.ph-card > h3 { margin:0 0 6px; display:flex; gap:8px; align-items:center; flex-wrap:wrap }
.ph-run { border-top:1px solid var(--line); padding:8px 0 4px; margin-top:6px }
.ph-head { font-size:13px; margin-bottom:4px; overflow-wrap:anywhere }
dl.ph-facts { display:grid; grid-template-columns:max-content minmax(0, 1fr); gap:2px 12px; margin:0 0 6px; font-size:12.5px }
dl.ph-facts dt { color:var(--muted); font-weight:600 } dl.ph-facts dd { margin:0; overflow-wrap:anywhere }
.ph-scroll { overflow-x:auto }
table.ph-changes { border-collapse:collapse; width:100%; font-size:12.5px }
.ph-changes th, .ph-changes td { padding:5px 6px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top }
.ph-changes th { font-size:11px; font-weight:600; letter-spacing:.04em; text-transform:uppercase; color:var(--muted) }
.ph-changes td { min-width:70px } .ph-changes td.ph-slot { min-width:30px }
ul.ph-inputs { margin:0; padding-left:14px; min-width:220px } ul.ph-inputs code { white-space:normal; overflow-wrap:anywhere }
.ph-none { margin:4px 0 }
@media (max-width:640px) { dl.ph-facts { grid-template-columns:minmax(0, 1fr) } dl.ph-facts dd { margin-bottom:3px } }
"""
