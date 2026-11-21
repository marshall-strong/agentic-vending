"""The dashboard's Key dates panel: what is due next before the start, and whether it is done.

Every date is in `DATES` below, with where it came from. To move one, edit it there: the panel,
`/state.json` and the status bar's catalog deadline (`dashboard.APPROVALS_DUE`) all read it.
Times are Eastern, the machine's zone. The opening day is `vending.start.START`, and the slot
check waits for `vending.slots.lock_at()` (`SLOTS_LOCK_AT`), so neither is written down twice.

Before the start each date shows with a status computed from our own records where they exist:

- **Catalog approvals**: the catalog requests' counts (`product_requests`).
- **First-fill plan picked**: a picked row in `first_fill_plans` (`vending.plan_store pick`), made for
  the current slot assignment.
- **Ledger opening recorded**: the opening entry (`vending.ledger open`).
- **Fill request sent**: the first purchase request, not rejected, sent at or after the ledger's
  opening. A request before the opening is a test purchase, so with no opening it can't tell.
- **Slots confirmed at the lock**: the stored assignment's `last_checked_at` at or after the lock.
- **Restock request sent**: a purchase request that isn't off-cycle, isn't rejected, isn't the
  fill request, and was sent in the week to the cutoff.

A date with no record behind it (the stocking day, the tax calculation) only counts down.

From the start (`START`) the panel shrinks to the next restock request and the next tax Saturday,
plus the slot check while it is still not done, since every reader of our slots refuses until it is.

Times are judged on the snapshot's clock: the database's, like the rest of the dashboard. The read
runs inside the dashboard's read-only snapshot; nothing here writes.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from html import escape
from typing import TYPE_CHECKING, Any

from vending.planner import IN_STORE_CUTOFF
from vending.start import EASTERN, START

if TYPE_CHECKING:
    import psycopg

# Where the dates come from.
ANSWERS_17NOV = "David's answers, 17 Nov (docs/planning/2026-11-17-david-answers.md)"
EMAIL_20NOV = "David's email to both teams, 20 Nov"

# A deadline's minute, as the bridge gives them: received by 23:59 Eastern.
DEADLINE = time(23, 59)
# The weekly rhythm after the start: the restock request is due Thursday 23:59 (the planner's in-store
# cutoff), staff restock on the Friday after it, and the sales tax is calculated Saturday 23:59.
RESTOCK_WEEKDAY = 4      # Friday, as datetime.weekday()
TAX_WEEKDAY = 5          # Saturday
TAX_AT = DEADLINE


@dataclass(frozen=True)
class KeyDate:
    """One date. `day` is the day in Eastern; `at` a deadline's time that day, or None for the whole day.
    `earliest` marks a day that isn't confirmed: it may be as early as that. `checks` name the
    records that say whether it is done (`CHECKS`)."""
    key: str
    day: date
    what: str
    source: str
    at: time | None = None
    earliest: date | None = None
    checks: tuple[str, ...] = ()

    @property
    def due(self) -> datetime:
        """When it has passed: the deadline's minute, or the end of a whole day."""
        if self.at is None:
            return datetime.combine(self.day + timedelta(days=1), time(0), EASTERN)
        return datetime.combine(self.day, self.at, EASTERN)


# --- The dates: edit here ---------------------------------------------------------------------

CATALOG = KeyDate(
    "catalog", date(2026, 11, 25),
    "Every product in the first fill requested and approved in the catalog. A request naming a product "
    "that isn't approved is denied", ANSWERS_17NOV, checks=("catalog",))
FILL_REQUEST = KeyDate(
    "fill_request", date(2026, 11, 29),
    "First fill request due. Record the ledger opening before it, or the fill falls outside the books",
    ANSWERS_17NOV, at=DEADLINE, checks=("plan_picked", "ledger_opening", "fill_request"))
STOCKING = KeyDate(
    "stocking", date(2026, 11, 30),
    "Staff empty the machine, buy and stock our first-fill request, and reset the interval counters. The "
    "bridge's tracking check starts answering", EMAIL_20NOV)
OPENING = KeyDate(
    "opening", START.date(),
    "Opening day. The email says both \"not until 3 Dec\" and \"1 or 2 Dec\"; our code starts on 3 Dec",
    EMAIL_20NOV, earliest=date(2026, 12, 1), checks=("slots_confirmed",))
FIRST_RESTOCK = KeyDate(
    "first_restock_request", date(2026, 12, 3),
    "First restock request due, whatever the opening day. Staff fill it Fri 4 Dec; then a restock visit every Friday",
    ANSWERS_17NOV + "; " + EMAIL_20NOV, at=DEADLINE, checks=("restock_request",))
FIRST_TAX = KeyDate(
    "first_tax", date(2026, 12, 5),
    "First weekly sales tax calculation; then every Saturday at 23:59", EMAIL_20NOV, at=TAX_AT)

DATES: tuple[KeyDate, ...] = (CATALOG, FILL_REQUEST, STOCKING, OPENING, FIRST_RESTOCK, FIRST_TAX)

# ------------------------------------------------------------------------------------------------

CHECK_LABELS = {
    "catalog": "Catalog approvals",
    "plan_picked": "First-fill plan picked",
    "ledger_opening": "Ledger opening recorded",
    "fill_request": "Fill request sent",
    "slots_confirmed": "Slots confirmed at the lock",
    "restock_request": "Restock request sent",
}
# A check's status, and the pill's tone for it.
TONES = {"done": "ok", "due": "info", "overdue": "bad", "missed": "warn", "waiting": "muted", "unknown": "muted"}
# Shown in place of a failed read's text, on every page: the detail goes to the server's log.
READ_FAILED = "the records behind these checks couldn't be read just now; the server's log has the detail"


def read(conn: psycopg.Connection, now: datetime, opening: datetime | None, ours: tuple[str, ...]) -> dict:
    """The records the checks need, read in the caller's snapshot. Our own team's rows only."""
    from psycopg.rows import dict_row

    cur = conn.cursor(row_factory=dict_row)
    picked = cur.execute("select slots from first_fill_plans where picked limit 1").fetchone()
    assignment = cur.execute("select last_checked_at from slot_assignment where id = 1").fetchone()
    requests = []
    if opening is not None:
        requests = cur.execute(
            "select request_id, submitted_at, off_cycle, status from purchase_requests"
            " where status <> 'rejected' and submitted_at >= %s and submitted_at <= %s"
            " order by submitted_at, request_id", (opening, now)).fetchall()
    return {"opening": opening,
            "plan": None if picked is None else {"current": bool(ours) and set(picked["slots"]) == set(ours)},
            "slots_checked_at": assignment and assignment["last_checked_at"],
            "requests": requests}


def _local(moment: datetime) -> datetime:
    return moment.astimezone(EASTERN)


def day_text(day: date, at: time | None = None) -> str:
    """"Wed 25 Nov", or "Sun 29 Nov, 23:59" for a deadline."""
    return f"{day:%a} {day.day} {day:%b}" + (f", {at:%H:%M}" if at is not None else "")


def moment_text(moment: datetime) -> str:
    """A recorded time in Eastern: "Sun 29 Nov, 18:00"."""
    local = _local(moment)
    return day_text(local.date(), local.time())


def days_until(now: datetime, day: date) -> int:
    """Eastern calendar days from `now` to `day`: 0 today, 1 tomorrow, negative once it is past."""
    return (day - _local(now).date()).days


def countdown(n: int) -> str:
    if n == 0:
        return "today"
    if n == 1:
        return "tomorrow"
    if n == -1:
        return "yesterday"
    return f"in {n} days" if n > 0 else f"{-n} days ago"


def _open(kd: KeyDate, now: datetime, text: str) -> dict:
    """Not done yet: due in N days, or overdue once the date has passed."""
    if now < kd.due:
        return {"status": "due", "pill": f"due {countdown(days_until(now, kd.day))}", "text": text}
    return {"status": "overdue", "pill": "overdue", "text": text}


def _done(text: str) -> dict:
    return {"status": "done", "pill": "done", "text": text}


def _catalog(kd: KeyDate, now: datetime, ctx: dict) -> dict:
    counts = ctx["catalog_counts"]
    if not counts or not counts.get("ok"):
        return {"status": "unknown", "pill": "can't tell", "text": "the catalog requests can't be read"}
    by = counts["data"]["by_status"]
    waiting = by["pending"] + by["sending"] + by["unknown"]
    tally = f"{by['approved']} approved, {waiting} waiting" + (f", {by['rejected']} rejected" if by["rejected"] else "")
    if waiting == 0 and by["approved"]:
        return _done(tally)
    if waiting and now >= kd.due:
        return {"status": "missed", "pill": "passed", "text": f"{tally}: the waiting ones miss the first fill"}
    return _open(kd, now, tally if waiting else "nothing approved yet")


def _plan_picked(kd: KeyDate, now: datetime, ctx: dict) -> dict:
    plan = ctx["facts"]["plan"]
    if plan is None:
        return _open(kd, now, "no plan picked: python -m vending.plan_store pick ID")
    if not plan["current"]:
        return _open(kd, now, "the picked plan is for an earlier slot assignment: load and pick a new one")
    return _done("picked, for our current slots")


def _ledger_opening(kd: KeyDate, now: datetime, ctx: dict) -> dict:
    opening = ctx["facts"]["opening"]
    if opening is None:
        return _open(kd, now, "not recorded: python -m vending.ledger open")
    return _done(f"{'opened' if opening <= now else 'opens'} {moment_text(opening)}")


def _fill(facts: dict) -> dict | None:
    return facts["requests"][0] if facts["requests"] else None


def _fill_request(kd: KeyDate, now: datetime, ctx: dict) -> dict:
    facts = ctx["facts"]
    if facts["opening"] is None:
        return {"status": "waiting", "pill": "waiting",
                "text": "counts from the ledger opening: a request before it is a test purchase"}
    fill = _fill(facts)
    if fill is None:
        return _open(kd, now, "no purchase request since the opening")
    late = " (after the deadline)" if fill["submitted_at"] > kd.due else ""
    return _done(f"sent {moment_text(fill['submitted_at'])}{late}, {fill['status']}")


def _slots_confirmed(kd: KeyDate, now: datetime, ctx: dict) -> dict:
    lock, checked = ctx["lock"], ctx["facts"]["slots_checked_at"]
    if lock is None:
        return {"status": "unknown", "pill": "can't tell", "text": "SLOTS_LOCK_AT isn't a time with its offset"}
    if checked is None:
        late = now >= lock
        return {"status": "overdue" if late else "due",
                "pill": "overdue" if late else f"due {countdown(days_until(now, _local(lock).date()))}",
                "text": "no assignment stored: python -m vending.slots record"}
    if checked >= lock:
        return _done(f"confirmed {moment_text(checked)}")
    if now < lock:
        return {"status": "waiting", "pill": "waiting",
                "text": f"stored; the first check at or after the lock ({moment_text(lock)}) confirms it"}
    return {"status": "overdue", "pill": "overdue",
            "text": "not confirmed since the lock: python -m vending.slots check (or confirm, if the bridge is down)"}


def _restock_request(kd: KeyDate, now: datetime, ctx: dict) -> dict:
    facts = ctx["facts"]
    if facts["opening"] is None:
        return _open(kd, now, "no ledger opening yet, so no request counts")
    fill = _fill(facts)
    since = kd.due - timedelta(days=7)
    sent = [r for r in facts["requests"] if not r["off_cycle"] and since < r["submitted_at"] <= kd.due
            and (fill is None or r["request_id"] != fill["request_id"])]
    if sent:
        return _done(f"sent {moment_text(sent[-1]['submitted_at'])}, {sent[-1]['status']}")
    return _open(kd, now, "no restock request this week yet")


CHECKS: dict[str, Callable[[KeyDate, datetime, dict], dict]] = {
    "catalog": _catalog, "plan_picked": _plan_picked, "ledger_opening": _ledger_opening,
    "fill_request": _fill_request, "slots_confirmed": _slots_confirmed, "restock_request": _restock_request,
}
_NEEDS_FACTS = {"plan_picked", "ledger_opening", "fill_request", "slots_confirmed", "restock_request"}


def _next_weekly(now: datetime, weekday: int, at: time) -> date:
    """The next day with `weekday` whose `at` (Eastern) hasn't passed."""
    local = _local(now)
    day = local.date() + timedelta(days=(weekday - local.weekday()) % 7)
    if datetime.combine(day, at, EASTERN) <= local:
        day += timedelta(days=7)
    return day


def weekly(now: datetime) -> tuple[KeyDate, KeyDate]:
    """After the start: the next restock request's deadline and the next tax Saturday."""
    cutoff = _next_weekly(now, IN_STORE_CUTOFF.weekday, IN_STORE_CUTOFF.at)
    visit = cutoff + timedelta(days=(RESTOCK_WEEKDAY - cutoff.weekday()) % 7)
    restock = KeyDate("next_restock_request", cutoff, f"Restock request due. Staff fill it {day_text(visit)}",
                      FIRST_RESTOCK.source, at=IN_STORE_CUTOFF.at, checks=("restock_request",))
    tax = KeyDate("next_tax", _next_weekly(now, TAX_WEEKDAY, TAX_AT), "Weekly sales tax calculation",
                  FIRST_TAX.source, at=TAX_AT)
    return restock, tax


def _row(kd: KeyDate, now: datetime, ctx: dict) -> dict:
    n = days_until(now, kd.day)
    checks = []
    for name in kd.checks:
        if name in _NEEDS_FACTS and ctx["facts"] is None:
            result = {"status": "unknown", "pill": "can't tell", "text": READ_FAILED}
        else:
            result = CHECKS[name](kd, now, ctx)
        checks.append({"key": name, "label": CHECK_LABELS[name], **result})
    # An unconfirmed day shows its range: "Tue 1 to Thu 3 Dec".
    when = (f"{kd.earliest:%a} {kd.earliest.day} to {day_text(kd.day, kd.at)}" if kd.earliest
            else day_text(kd.day, kd.at))
    return {"key": kd.key, "when": when, "due": kd.due, "what": kd.what, "source": kd.source,
            "unconfirmed": kd.earliest is not None, "passed": now >= kd.due,
            "countdown": countdown(n) if now < kd.due else "passed", "checks": checks}


def build(now: datetime, facts: dict | None, catalog_counts: dict | None, lock: datetime | None) -> dict:
    """The panel's rows at `now`. `facts` is `read`'s answer, or None when it couldn't be read: the
    checks that need it then say they can't tell, and the dates still show."""
    ctx = {"facts": facts, "catalog_counts": catalog_counts, "lock": lock}
    after_start = now >= START
    if not after_start:
        rows = [_row(kd, now, ctx) for kd in DATES]
    else:
        rows = [_row(kd, now, ctx) for kd in weekly(now)]
        slots = _row(OPENING, now, ctx)
        if any(c["status"] != "done" for c in slots["checks"]):
            rows.insert(0, slots)
    return {"after_start": after_start, "rows": rows, "unread": facts is None}


def panel(conn: psycopg.Connection, now: datetime, opening: datetime | None, ours: tuple[str, ...],
          catalog_counts: dict | None, lock: datetime | None, isolate: Callable[[Callable[[], Any]], Any]) -> dict:
    """The panel for the snapshot. `isolate` runs the read in its own savepoint (`dashboard._isolated`), so a
    failure costs only these checks. The error's text goes to stderr, never to the page or the JSON."""
    try:
        facts = isolate(lambda: read(conn, now, opening, ours))
    except Exception as error:  # noqa: BLE001 - the dates still show when the records can't be read
        cause = error.__cause__ or error
        first = (str(cause).strip().splitlines() or [""])[0]
        print(f"dashboard: key dates: {type(cause).__name__}: {first}", file=sys.stderr, flush=True)
        facts = None
    return {"ok": True, "data": build(now, facts, catalog_counts, lock)}


def html(panel_: dict | None) -> str:
    """The section, or nothing for a snapshot made before the panel existed."""
    if not panel_ or not panel_.get("ok"):
        return ""
    data = panel_["data"]
    items = []
    for row in data["rows"]:
        checks = "".join(
            f'<li><span class="pill {TONES[c["status"]]}">{escape(c["pill"])}</span> '
            f'<b>{escape(c["label"])}</b> <span class="dim">{escape(c["text"])}</span></li>' for c in row["checks"])
        flag = ' <span class="pill warn">unconfirmed</span>' if row["unconfirmed"] else ""
        # The countdown, unless a check's pill already says it ("due in 5 days").
        shown = any(c["status"] == "due" for c in row["checks"])
        sub = "" if shown else f'<span class="sub">{escape(row["countdown"])}</span>'
        items.append(
            f'<li class="kd-row{" kd-past" if row["passed"] else ""}" title="Source: {escape(row["source"])}">'
            f'<span class="kd-when"><b>{escape(row["when"])}</b>{flag}{sub}</span>'
            f'<span class="kd-what">{escape(row["what"])}</span>'
            + (f'<ul class="kd-checks">{checks}</ul>' if checks else "") + "</li>")
    hint = ("after the start: the next restock and the next tax Saturday" if data["after_start"]
            else "Eastern &middot; from David's answers (17 Nov) and email (20 Nov); hover a date for its source")
    unread = f'<p class="honest">{escape(READ_FAILED)}.</p>' if data["unread"] else ""
    return (f'<section id="dates" class="kd"><h2>Key dates <span class="hint">{hint}</span></h2>{unread}'
            f'<ol class="kd-list">{"".join(items)}</ol></section>')


STYLE = """
/* Key dates: one row per date, its checks beside it. */
.kd { padding:10px 16px } .kd > h2 { margin-bottom:6px }
.kd-list { list-style:none; margin:0; padding:0 }
.kd-row { display:grid; grid-template-columns:13em minmax(0, 1fr) minmax(0, 1.1fr); gap:4px 14px; padding:6px 0;
  border-top:1px solid var(--line); font-size:13px; align-items:baseline }
.kd-row:first-child { border-top:0 }
.kd-when { display:flex; flex-direction:column; gap:1px } .kd-when .pill { align-self:flex-start }
.kd-past .kd-when b, .kd-past .kd-what { color:var(--muted) }
.kd-checks { list-style:none; margin:0; padding:0; display:flex; flex-direction:column; gap:3px }
.kd-checks .pill { min-width:5.5em; text-align:center }
@media (max-width:760px) { .kd-row { grid-template-columns:minmax(0, 1fr) } }
"""
