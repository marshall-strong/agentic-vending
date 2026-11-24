"""The dashboard's forecast panel: for each week, what the agent expected each of our slots to sell when it
made the plan that week's visit loaded, against what sold. Computed in code; no model calls.

**The weeks are the report card's** (`vending.report_card`): Saturday to Friday, Eastern, from the ledger's
opening, the last 8, each matched to its plan through the run's purchase request. So a week has the same
plan here as on its card, and the same rules: the opening week has no agent plan, a rejected request loaded
nothing, a slot the visit swapped is left out of the comparison (#172: the forecast was the old product's
rate), and a closed week whose polls cover too little or too much of it shows its figures without a verdict
(here, without its average miss).

**The forecast** is the demand estimate the agent planned with: each slot's units per class day from
`get_machine_state` (`vending.demand`: the slot's own in-stock rate, or the fall prior for its price), stored
with the plan as `forecast` when the run ends (`vending.agent.demand_forecast`). Plans recorded before that
have none, and their weeks say "No forecast recorded". The panel never makes one up afterwards.

**Expected** is that rate times the class days the week's polls cover, each day weighted by the share of a
day's sales that falls between the two polls (`demand.HOUR_SHARE`: most sell 8 to 11 AM). So a week still in
progress, or one with a gap in its polls, is compared over the same stretch its sales were counted over. It
is demand, not capped by the fill: a slot that was empty at the week's last poll sold all it had, and is
marked, since its demand may have been higher than its sales.

**Sold** is what the polls' counters say our slots sold, as the report card counts it (the replayer's
`get_sales_history`). Unknown stays unknown: a slot with no rate or no sales figure has no difference and is
left out of the week's totals.

Integrity: only our selections, as the stored assignment names them now (`vending.slots.our_slots`), are
listed; a forecast entry for any other selection is ignored and never shown.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from fractions import Fraction
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

import psycopg

from vending import class_days, demand, ledger
from vending.replay.state import COVERAGE_SLACK
from vending.slots import SlotsNotRecorded, our_slots

EASTERN = ZoneInfo(ledger.TIMEZONE)
WEEK = timedelta(days=7)
TENTH = Decimal("0.1")

# Short labels: the people reading this follow the repo (Kevin, 24 Nov), so no explanatory text.
STATUS_TEXT = {
    "opening": "Opening week: first fill, no agent plan",
    "no_plan": "No plan matched",
    "rejected": "Request rejected: nothing loaded",
    "unread": "Couldn't be read",
    "no_forecast": "No forecast recorded",
    "no_sales": "No sales from the polls yet",
}


def read(conn: psycopg.Connection, now: datetime, cards: dict | None) -> dict:
    """The panel's data from the report card panel's (`cards`, None when that panel couldn't be read).

    Reads the matched runs' stored forecasts and plans, and our slot assignment, in the caller's snapshot."""
    if cards is None:
        return {"opened": None}
    if not cards.get("opened"):
        return {"opened": False}
    try:
        ours = our_slots(conn)
    except SlotsNotRecorded:
        return {"opened": True, "assignment_missing": True, "weeks": []}
    if cards.get("assignment_missing"):
        return {"opened": True, "assignment_missing": True, "weeks": []}
    run_ids = [int(c["run_id"]) for c in cards["weeks"] if c.get("run_id") is not None]
    plans = {}
    if run_ids:
        plans = {run_id: plan for run_id, plan in conn.execute(
            "select id, plan from agent_runs where id = any(%s)", (run_ids,))}
    return {"opened": True, "weeks": [week(card, plans.get(card.get("run_id")), ours) for card in cards["weeks"]]}


def week(card: dict, plan: Any, ours: tuple[str, ...]) -> dict:
    """One week's forecast against its sales, from its report card and its plan as stored."""
    out = {"week": card["week"], "ends": card["ends"], "final": card["final"], "run_id": card.get("run_id"),
           "planned_at": card.get("planned_at"), "status": "compared", "polls": None, "class_days": None,
           "rows": [], "summary": None, "notes": []}
    status = _status(card, plan)
    if status:
        out["status"] = status
        return out
    by_slot, span = card["by_slot"], card["polls"]["this"]
    start, end = span
    share = class_day_share(start, end)
    out["polls"], out["class_days"] = [start, end], str(_tenth(share))
    rates = _rates(plan["forecast"], ours)
    entries = {str(e.get("slot")): e for e in plan.get("slots", []) if isinstance(e, dict)}
    swapped = set(card.get("swapped") or [])
    for slot in ours:
        if slot not in by_slot:
            continue
        rate, basis = rates.get(slot, (None, None))
        sold, empty = by_slot[slot]["sold"], by_slot[slot]["empty"]
        row = {"slot": slot, "product": by_slot[slot].get("product"), "rate": None if rate is None else str(rate),
               "basis": basis, "expected": None, "sold": sold, "difference": None, "empty": empty,
               "counted": False, "note": None}
        pulled = entries.get(slot, {}).get("fill_to") == 0 and not entries.get(slot, {}).get("keep")
        if slot in swapped:
            row["note"] = "swapped at the visit: rate was the old product's; not compared (#172)"
        elif slot not in rates:
            row["note"] = "no rate recorded for this slot"
        elif rate is None:
            row["note"] = "rate unknown at plan time"
        else:
            expected = Fraction(0) if pulled else Fraction(rate) * share
            row["expected"] = str(_tenth(expected))
            if pulled:
                row["note"] = "pulled by the plan (fill_to 0)"
            if sold is None:
                row["note"] = "sold unknown from the polls"
            else:
                row["difference"] = str(_tenth(sold - expected))
                row["counted"], row["_expected"] = True, expected
        out["rows"].append(row)
    counted = [r for r in out["rows"] if r["counted"]]
    exact = [r.pop("_expected") for r in counted]
    if counted:
        expected, sold = sum(exact, Fraction(0)), sum(r["sold"] for r in counted)
        misses = sum((abs(r["sold"] - e) for r, e in zip(counted, exact)), Fraction(0))
        thin = card["final"] and _thin(start, end)
        out["summary"] = {
            "slots": len(counted), "expected": str(_tenth(expected)), "sold": sold,
            "difference": str(_tenth(sold - expected)),
            # How far off a slot was, on average: the mean of |sold - expected|. Withheld for a thin week, as
            # the report card withholds its grade.
            "average_off": None if thin else str(_tenth(misses / len(counted))),
            "empty": sum(1 for r in counted if r["empty"]),
        }
        if thin:
            days = (end - start) / timedelta(days=1)
            out["notes"].append(f"Polls cover {days:.1f} days, not 7: mean |diff| withheld")
    if not card["final"]:
        out["notes"].append("In progress: expected and sold cover the polls so far")
    return out


def _status(card: dict, plan: Any) -> str | None:
    """Why a week has no comparison, or None when it has one."""
    if card["grade"] == "opening":
        return "opening"
    if card.get("run_id") is None:
        return "no_plan" if card["grade"] == "no_plan" else "unread"
    if card.get("request") == "rejected":
        return "rejected"
    if not isinstance(plan, dict) or not isinstance(plan.get("forecast"), dict):
        return "no_forecast"
    if not card.get("by_slot") or not (card.get("polls") or {}).get("this"):
        return "no_sales"
    return None


def _rates(forecast: dict, ours: tuple[str, ...]) -> dict[str, tuple[Decimal | None, str | None]]:
    """Each of our slots' recorded units per class day and where it came from. A rate that isn't a number is
    unknown; an entry for a selection that isn't ours is dropped."""
    out = {}
    for entry in forecast.get("slots") or []:
        if not isinstance(entry, dict) or str(entry.get("slot")) not in ours:
            continue
        try:
            rate = Decimal(str(entry["units_per_class_day"])) if entry.get("units_per_class_day") is not None else None
        except (InvalidOperation, ValueError):
            rate = None
        if rate is not None and (not rate.is_finite() or rate < 0):
            rate = None
        basis = entry.get("basis") if isinstance(entry.get("basis"), str) else None
        out[str(entry["slot"])] = (rate, basis)
    return out


def class_day_share(start: datetime, end: datetime) -> Fraction:
    """Class days between two moments, each counted by the share of a day's sales that falls between them
    (`demand.HOUR_SHARE`, by the Eastern hour). A whole class day is 1; weekends and breaks are 0."""
    if end <= start:
        return Fraction(0)
    s, e = start.astimezone(EASTERN), end.astimezone(EASTERN)
    total, day = Fraction(0), s.date()
    while day <= e.date():
        if class_days.is_class_day(day):
            begin = demand.share_left(s.hour, s.minute) if day == s.date() else Fraction(1)
            finish = demand.share_left(e.hour, e.minute) if day == e.date() else Fraction(0)
            total += max(begin - finish, Fraction(0))
        day += timedelta(days=1)
    return total


def _thin(start: datetime, end: datetime) -> bool:
    """The report card's rule: polls spanning too little or too much of a week (`report_card._thin`)."""
    span = end - start
    return span < WEEK - COVERAGE_SLACK or span > WEEK + COVERAGE_SLACK


def _tenth(value: Fraction | int) -> Decimal:
    value = Fraction(value)
    return (Decimal(value.numerator) / Decimal(value.denominator)).quantize(TENTH, ROUND_HALF_UP)


# --- HTML ----------------------------------------------------------------------------------------

def _day(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d:%a} {d.day} {d:%b}"


def _when(at: Any) -> str:
    if isinstance(at, datetime):
        local = at.astimezone(EASTERN)
        return f"{local:%a} {local.day} {local:%b} {local:%H:%M} Eastern"
    return str(at)


def _signed(text: str) -> str:
    return text if text.startswith("-") or Decimal(text) == 0 else f"+{text}"


def html(panel: dict) -> str:
    if not panel["ok"]:
        return f'<p class="empty">Not available: {escape(panel["why"])}</p>'
    data = panel["data"]
    if data["opened"] is None:
        return '<p class="empty">Not available: the report card couldn\'t be read</p>'
    if not data["opened"]:
        return '<p class="empty">No data before the ledger opening</p>'
    if data.get("assignment_missing"):
        return '<p class="empty">Slot assignment unavailable</p>'
    return "".join(_week_html(w, i == 0) for i, w in enumerate(data["weeks"]))


def _week_html(w: dict, first: bool) -> str:
    title = f'{_day(w["week"])} – {_day(w["ends"])}'
    state = "" if w["final"] or w["status"] == "opening" else ' <span class="pill info">in progress</span>'
    run = f' <span class="sub">run #{int(w["run_id"])}</span>' if w["run_id"] is not None else ""
    if w["status"] != "compared":
        return (f'<article class="rc-card fc-week"><h3>{escape(title)}{state}{run} '
                f'<span class="pill muted">{escape(STATUS_TEXT[w["status"]])}</span></h3></article>')
    s = w["summary"]
    if s is None:
        figures = '<span class="sub">no slot with both a rate and sales</span>'
    else:
        figures = "".join(
            f'<span class="fc-fig"><span class="label">{label}</span>{value}</span>' for label, value in (
                ("Expected (est.)", escape(s["expected"])), ("Sold", str(int(s["sold"]))),
                ("Diff", escape(_signed(s["difference"]))),
                ("Mean |diff| / slot", escape(s["average_off"]) if s["average_off"] is not None else "withheld"),
                ("Slots", str(int(s["slots"]))), ("Empty", str(int(s["empty"])))))
    rows = []
    for r in w["rows"]:
        expected = escape(r["expected"]) if r["expected"] is not None else "&mdash;"
        sold = str(int(r["sold"])) if r["sold"] is not None else "unknown"
        diff = escape(_signed(r["difference"])) if r["difference"] is not None else "&mdash;"
        rate = escape(r["rate"]) if r["rate"] is not None else "unknown"
        # The basis's kind ("own", "fall prior") on the row; its whole sentence on hover.
        basis = (f'<small title="{escape(r["basis"])}">{escape(r["basis"].split(":", 1)[0])}</small>'
                 if r["basis"] else "")
        empty = ' <span class="pill warn">empty</span>' if r["empty"] else ""
        rows.append(f'<tr><th scope="row">{escape(r["slot"])}</th><td class="fc-sku"><code>{escape(r["product"] or "?")}'
                    f'</code></td><td class="fc-rate">{rate}{basis}</td><td>{expected}</td><td>{sold}{empty}</td>'
                    f'<td>{diff}</td><td class="fc-note">{escape(r["note"] or "")}</td></tr>')
    table = ('<div class="fc-wrap"><table class="rc-outcome fc-table"><thead><tr><th scope="col">Slot</th>'
             '<th scope="col">SKU</th><th scope="col">Rate / class day</th><th scope="col">Expected (est.)</th>'
             '<th scope="col">Sold</th><th scope="col">Diff</th><th scope="col">Note</th></tr></thead>'
             f'<tbody>{"".join(rows)}</tbody></table></div>')
    meta = ""
    if w["polls"]:
        meta = (f'<p class="sub">planned <time>{escape(_when(w["planned_at"]))}</time> &middot; polls '
                f'<time>{escape(_when(w["polls"][0]))}</time> &rarr; <time>{escape(_when(w["polls"][1]))}</time> '
                f'&middot; {escape(w["class_days"])} class days</p>')
    notes = "".join(f'<li>{escape(n)}</li>' for n in w["notes"])
    notes = f'<ul class="rc-notes">{notes}</ul>' if notes else ""
    return (f'<article class="rc-card fc-week"><details{" open" if first else ""}><summary><b>{escape(title)}</b>'
            f'{state}{run}<span class="fc-figs">{figures}</span></summary>{meta}{table}{notes}</details></article>')


STYLE = """
.fc-week summary { cursor:pointer; font-size:14px }
.fc-figs { display:flex; flex-wrap:wrap; gap:4px 16px; margin-top:4px; font-variant-numeric:tabular-nums }
.fc-fig { display:flex; flex-direction:column; font-size:15px; font-weight:600 }
.fc-wrap { overflow-x:auto }
.fc-table { width:100% }
.fc-table td.fc-sku { text-align:left }
.fc-table td.fc-rate small { display:block; color:var(--muted); font-size:11px; font-weight:400; max-width:22em; margin-left:auto }
.fc-table td.fc-note { text-align:left; color:var(--muted); min-width:10em; overflow-wrap:anywhere }
"""
