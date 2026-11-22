"""The weekly report card and the incident log (#48), computed in code. No model calls.

**Report card.** One card per display week of the ledger: Saturday to Friday, Eastern
(`ledger.week_of`), from the ledger's opening on. The week's plan is the live run whose purchase
request led to the visit that loads the week (`_Reader.plan_for`): matched by `request_id`, the
latest receipt before the week wins, else a request still waiting on its receipt, else a rejected
one. The stored plan is validated first (`validate_plan`, fixed sentences only). It is scored with the
replayer's own scoring (`vending.replay.scoring.score`), against what our slots really sold in the
week, next to the replayer's baseline (restock each slot to last week's sales at last week's price,
`deciders.Baseline`) scored the same way:

- **Hard checks** pass, fail or skip: a valid plan, catalog, prices (floor and ceiling, the
  wildcard's own ceiling for the wildcard slot) and balance. A skip means the data the check needs
  isn't there; it never counts as a pass. The replayer's "no writes" check is left out: a live run
  is meant to write its request.
- **Outcome**, as the replayer scores it: units sold, slots that sold out (estimated: the rail ran
  empty in the model; demand may have been higher), unsold units on the rail (they keep their cost, so
  they are counted, not charged) and margin.
- **Against the baseline**: the plan's margin less the baseline's on the shared slots. A slot the
  visit swapped is left out (its sales are a product the baseline never stocked); a shared slot with
  unknown unit cost makes the comparison unavailable.

A week that fails a hard check gets a failing grade, whatever it earned. The week holding the
opening isn't graded: the first fill goes in partway through it. The current week is graded on its
sales so far and says so. A closed week whose polls cover too little of it isn't graded either.

The scoring needs each slot's product, stock and unit cost, which a replayed week doesn't have
(`replay.state`). They come from the receipts (`vending.stock`): the product and units just before
the visit, and the unit cost after it when the product is unchanged. Polls are read only up to the
page's `now`. The catalog and balance are read as they stood when the plan was made; the ceilings
are the planner's settings now. Every displayed sentence is a fixed template filled with validated
values; one week that fails to score leaves the others.

**Incident log.** From the opening on, newest first: refused and failed tool calls (`action_log`),
halt switch flips (`action_log`, tool `halt`), live runs that failed, halted or were left running,
live runs whose model was told a tool call failed (`agent_steps`, counted, never quoted), and weeks
that failed a hard check. Each has a one-line note. A failure's stored text is never read here: the
row says `error: True` and `dashboard.hide_errors` puts its fixed message in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from html import escape
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from vending import capacity, config, ledger, stock, tracking
from vending.replay import scoring
from vending.pricing import to_cents
from vending.replay.deciders import MAX_FILL, Baseline
from vending.replay.state import COVERAGE_SLACK, NoData, SlotWeek, WeekState, replay_week
from vending.slots import SlotsNotRecorded, our_slots

if TYPE_CHECKING:
    import psycopg

EASTERN = ZoneInfo(ledger.TIMEZONE)
WEEK = timedelta(days=7)
WEEKS_SHOWN = 8
INCIDENTS_SHOWN = 50
# How long before the oldest week shown a run's plan can still be the one that week loaded: a plan is
# made in the week before, and a request can wait a week for its visit.
PLAN_LOOKBACK = timedelta(days=14)
NOTE_CHARS = 160
# A live run still `running` this long after it started is taken as dead (the dashboard's STALE_RUN).
STALE_RUN = timedelta(hours=2)
# The four tracking-check verdicts (`vending.tracking.RESULTS`), in the order the check gives them.
TRACKING_RESULTS = tracking.RESULTS
# Every slot needs a reason to pass the replayer's "valid plan" check, but the agent writes reasons
# only for the slots its plan changes (`vending.agent.recorded_plan`).
NO_REASON = "(no reason recorded: the plan leaves this slot as it was)"
# A replayed week's own note, which the receipts answer here.
_REPLAY_UNKNOWN_NOTE = "Product, stock and unit cost are unknown"

GRADES = {
    "beat": "Beat the baseline",
    "tied": "Level with the baseline",
    "behind": "Behind the baseline",
    "fail": "Failed a hard check",
    "no_plan": "No plan matched",
    "rejected": "Request rejected",
    "unscored": "Not scored",
    "incomplete": "Incomplete data: not graded",
    "opening": "Opening week: not graded",
}


# --- Report card -------------------------------------------------------------------------------

def report_cards(conn: psycopg.Connection, now: datetime) -> dict:
    """The report card panel's data: newest week first, at most `WEEKS_SHOWN`."""
    opened = ledger.opening(conn)
    if opened is None or now < opened.at:
        return {"opened": False}
    try:
        ours = our_slots(conn)
    except SlotsNotRecorded:
        return {"opened": True, "assignment_missing": True, "weeks": []}
    first = ledger.week_of(opened.at.astimezone(EASTERN))
    current = ledger.week_of(now.astimezone(EASTERN))
    starts = [current - WEEK * n for n in range(WEEKS_SHOWN) if current - WEEK * n >= first]
    reader = _Reader(conn, ours, now, opened.at, since=_start(starts[-1]) - PLAN_LOOKBACK)
    weeks = [_isolated_card(reader, week, opening_week=week == first, final=week < current) for week in starts]
    return {"opened": True, "weeks": weeks}


def _isolated_card(reader: _Reader, week: date, *, opening_week: bool, final: bool) -> dict:
    """One week's card in its own savepoint: a failure scoring it leaves the other weeks standing.
    The error's text is never shown, only that the week couldn't be scored."""
    try:
        with reader.conn.transaction():
            return _card(reader, week, opening_week=opening_week, final=final)
    except Exception:   # noqa: BLE001 - one week never takes the panel down
        card = _blank(week, final)
        card["notes"].append("This week couldn't be scored: the scoring failed. The other weeks are unaffected.")
        return _graded(card)


def _start(week: date) -> datetime:
    return datetime.combine(week, time.min, EASTERN)


@dataclass(frozen=True)
class _Match:
    """A live run's plan and the visit its purchase request led to."""
    run_id: int
    started_at: datetime
    plan: Any
    request: str                    # fulfilled (a receipt is in), submitted (none yet) or rejected
    week: date                      # the week whose start the visit loads
    visit_at: datetime | None = None    # just before the visit: the door opening, else the receipt time
    loads: tuple[tuple[str, str | None], ...] = ()   # (slot, product) the receipt loaded
    superseded: tuple[int, ...] = ()    # other runs' plans for the same week this one replaced


def _visit_week(started: datetime, off_cycle: bool) -> date:
    """The week a request with no receipt yet is meant to load: a Friday request's is the week after
    the Friday the planner buys for when the run started (Thursday 23:59 Eastern is the cutoff, as on
    the plan panel); a midweek one's is the week after the run's."""
    from vending import planner
    local = started.astimezone(EASTERN)
    week = ledger.week_of(local) + WEEK
    if not off_cycle and planner._past_cutoff(local, planner.IN_STORE_CUTOFF, ledger.WEEK_STARTS):
        week += WEEK
    return week


def _visits(lines: list[dict]) -> dict[str, tuple[datetime, tuple[tuple[str, str | None], ...]]]:
    """Each fulfilled request's visit from its receipt: (just before it, (slot, product) loaded)."""
    out: dict[str, tuple[datetime, list]] = {}
    for line in lines:
        opened, closed = line["door_opened_at"], line["door_closed_at"]
        at = opened if opened is not None and closed is not None else line["at"]
        if at is None:
            continue
        first, loads = out.get(line["request_id"], (at, []))
        if line["outcome"] in stock.LOADING and line["units_loaded"]:
            loads.append((line["slot"], line["sku"]))
        out[line["request_id"]] = (min(first, at), loads)
    return {rid: (at, tuple(loads)) for rid, (at, loads) in out.items()}


class _Reader:
    """What every week's card reads once: the runs with plans and their requests, the stock
    movements, the catalog, the capacities and the ceilings."""

    def __init__(self, conn: psycopg.Connection, ours: tuple[str, ...], now: datetime, opened: datetime,
                 since: datetime):
        self.conn, self.ours, self.now, self.opened = conn, ours, now, opened
        # Receipts in by `now`: a later one is a visit the page can't have seen yet.
        lines = [line for line in stock.receipt_lines(conn) if line["at"] is not None and line["at"] <= now]
        self.moves = stock.load_movements(conn, lines, until=now)
        visits = _visits(lines)
        self.matches = []
        for run_id, started, plan, request_id, status, off_cycle in conn.execute(
                "select r.id, r.started_at, r.plan, r.request_id, q.status, q.off_cycle from agent_runs r"
                " left join purchase_requests q on q.request_id = r.request_id where r.mode = 'live'"
                " and r.status = 'submitted' and r.plan is not null and r.started_at >= %s and r.started_at <= %s"
                " order by r.started_at, r.id", (since, now)):
            if request_id in visits:
                at, loads = visits[request_id]
                self.matches.append(_Match(run_id, started, plan, "fulfilled",
                                           ledger.week_of(at.astimezone(EASTERN)) + WEEK, at, loads))
            else:
                self.matches.append(_Match(run_id, started, plan, "rejected" if status == "rejected" else "submitted",
                                           _visit_week(started, bool(off_cycle))))
        self.catalog = conn.execute("select sku, size, first_seen_at, delisted_at from catalog_products").fetchall()
        self.rejected = conn.execute(
            "select sku, requested_at from product_requests where status = 'rejected' and sku is not null").fetchall()
        self.overrides = config.get_value(conn, "capacity_overrides", {}) or {}
        self.ceilings, self.notes = self._ceilings()
        self._weeks: dict[date, WeekState | None] = {}
        self._slots: dict[datetime, dict[str, stock.SlotCount]] = {}

    def _ceilings(self) -> tuple[tuple[tuple[str, int], ...], list[str]]:
        from vending.planning import NotReady, planner_config
        try:
            cfg = planner_config(self.conn, stale_wildcard_unset=True)
        except NotReady:
            return (), ["The planner's settings couldn't be read, so every price is checked against $6.50."]
        return tuple((s, cfg.ceiling_for(s)) for s in self.ours), []

    def week(self, week: date) -> WeekState | None:
        """The replayed week starting on `week`, or None when the polls can't rebuild it. The
        replayer's reason isn't kept: the card says so in its own words."""
        if week not in self._weeks:
            try:
                self._weeks[week] = replay_week(self.conn, week, until=self.now)
            except NoData:
                self._weeks[week] = None
        return self._weeks[week]

    def plan_for(self, week: date) -> _Match | None:
        """The plan loaded at the start of `week`, matched through its purchase request: the run whose
        request's receipt is the latest visit before the week (a paid midweek visit after a Friday
        request supersedes it), else the latest still waiting on a receipt, else the latest rejected."""
        found = [m for m in self.matches if m.week == week]
        for request in ("fulfilled", "submitted", "rejected"):
            picks = [m for m in found if m.request == request]
            if picks:
                pick = max(picks, key=lambda m: (m.visit_at or m.started_at, m.run_id))
                return replace(pick, superseded=tuple(m.run_id for m in found if m is not pick))
        return None

    def slots_at(self, moment: datetime) -> dict[str, stock.SlotCount]:
        """The slots just before `moment`, replayed once per moment."""
        if moment not in self._slots:
            self._slots[moment] = {s.slot: s for s in stock.replay(self.moves, moment, inclusive=False).slots}
        return self._slots[moment]

    def catalog_at(self, moment: datetime) -> frozenset[str] | None:
        listed = frozenset(sku for sku, _size, seen, gone in self.catalog
                           if seen <= moment and (gone is None or gone > moment))
        return listed or None

    def capacity_of(self, sku: str | None) -> int | None:
        if sku is None:
            return None
        size = next((size for s, size, _seen, _gone in self.catalog if s == sku), None)
        return capacity.for_product(sku, size, self.overrides)


def _blank(week: date, final: bool) -> dict:
    return {
        "week": week.isoformat(), "ends": (week + timedelta(days=6)).isoformat(), "final": final,
        "grade": "unscored", "run_id": None, "planned_at": None, "checks": [], "outcome": None,
        "baseline": None, "comparison": None, "notes": [], "request": None, "polls": None,
    }


def _card(reader: _Reader, week: date, *, opening_week: bool, final: bool) -> dict:
    card = _blank(week, final)
    if opening_week:
        card["grade"] = "opening"
        card["notes"].append("The first fill went in during this week, so the first graded week is the next one.")
        return _graded(card)
    match = reader.plan_for(week)
    before, this = reader.week(week - WEEK), reader.week(week)
    card["polls"] = {"last": list(before.polls) if before and before.polls else None,
                     "this": list(this.polls) if this and this.polls else None}
    if before is None:
        # The hard checks still run: they need the plan, the receipts and the ledger, not the sales.
        card["notes"].append("Last week can't be rebuilt from the polls.")
        before = WeekState(start=_start(week - WEEK), end=_start(week),
                           slots=tuple(SlotWeek(s, None, None) for s in reader.ours))
        this = None
    elif this is None:
        card["notes"].append("This week's sales can't be read from the polls yet.")
    if match is None:
        card["grade"] = "no_plan"
        card["notes"].append("No submitted live plan matched this week's visit through its purchase request, "
                             "so there is no plan to score.")
        return _graded(card)
    card["run_id"], card["planned_at"], card["request"] = match.run_id, match.started_at, match.request
    if match.superseded:
        card["notes"].append(f"This plan superseded the plan from run{'s' if len(match.superseded) > 1 else ''} "
                             f"{', '.join(str(int(r)) for r in match.superseded)} for the same week.")
    if match.request == "submitted":
        card["notes"].append("Its purchase request has no receipt yet, so it is scored as if the visit loaded it.")
    elif match.request == "rejected":
        card["notes"].append("Its purchase request was rejected, so the visit loaded nothing from it. "
                             "Only the hard checks are shown.")
    state, swapped = _state(reader, before, week, match)
    problems = validate_plan(match.plan, reader.ours)
    if problems:
        card["checks"] = [{"name": "valid plan", "status": scoring.FAIL, "detail": " ".join(problems)}] + [
            {"name": name, "status": scoring.SKIP, "detail": "No valid plan to check."} for name, _ in scoring.CHECKS]
        card["grade"] = "fail"
        card["notes"] += reader.notes
        return _graded(card)
    for slot in swapped:
        card["notes"].append(f"Slot {slot}'s product was swapped at the visit: this week's sales are the new "
                             "product's, so the slot is left out of the comparison with the baseline.")
    answer, notes, unknown_fill = _as_replay_plan(match.plan, reader, state)
    card["notes"] += notes
    if match.request == "rejected":
        scored = scoring.score(state, answer, None, unknown_fill=unknown_fill)
        card["checks"] = [{"name": c.name, "status": c.status, "detail": c.detail}
                          for c in scored.checks if c.name != "no writes"]
        card["grade"] = "fail" if any(c["status"] == scoring.FAIL for c in card["checks"]) else "rejected"
        card["notes"] += reader.notes
        return _graded(card)
    if this is not None and not final:
        card["notes"].append("In progress: scored on the sales so far. The grade can change until Friday ends.")
    scored = scoring.score(state, answer, this, unknown_fill=unknown_fill)
    card["checks"] = [{"name": c.name, "status": c.status, "detail": c.detail}
                      for c in scored.checks if c.name != "no writes"]
    if scored.totals is not None:
        card["outcome"] = _outcome(scored)
        base = scoring.score(state, Baseline().decide(state), this)
        card["baseline"] = _outcome(base) if base.totals is not None else None
        card["comparison"] = _compare(scored, base, swapped)
    thin = final and this is not None and _thin(this)
    if thin:
        days = (this.polls[1] - this.polls[0]) / timedelta(days=1)
        card["notes"].append(f"The polls cover {days:.1f} days of this week, not 7, so the figures are shown but "
                             "the grade is withheld.")
    if any(c["status"] == scoring.FAIL for c in card["checks"]):
        card["grade"] = "fail"
    elif card["comparison"] and card["comparison"]["result"]:
        card["grade"] = "incomplete" if thin else card["comparison"]["result"]
    card["notes"] += reader.notes
    return _graded(card)


def _thin(week: WeekState) -> bool:
    """Whether a week's sales span too little or too much of it to grade: the replayer's own bounds."""
    if week.polls is None:
        return True
    span = week.polls[1] - week.polls[0]
    return span < WEEK - COVERAGE_SLACK or span > WEEK + COVERAGE_SLACK


def _graded(card: dict) -> dict:
    card["grade_text"] = GRADES[card["grade"]]
    return card


def _state(reader: _Reader, before: WeekState, week: date, match: _Match) -> tuple[WeekState, tuple[str, ...]]:
    """Last week's replayed state, with what the receipts know, and the slots the visit swapped.

    Product and units are the slot's just before the visit, so a plan that changes the product is a
    switch to the scorer. The unit cost is the slot's at the start of this week when the product is
    the same (what is on the rail after the visit), else the old product's. A slot whose product
    before the visit isn't on record takes the product the visit loaded. With no receipt the visit is
    taken as the start of the week. The catalog and balance are read as they stood when the plan was made."""
    start, plan_made = _start(week), match.started_at
    loads = dict(match.loads)
    after, pre_visit = reader.slots_at(start), reader.slots_at(match.visit_at or start)
    slots, swapped = [], []
    for s in before.slots:
        now, then = after.get(s.slot), pre_visit.get(s.slot)
        if then is None:
            slots.append(replace(s, product=now.product if now else None,
                                 unit_cost_cents=now.unit_cost if now else None, stock=None))
            continue
        if s.slot in loads and loads[s.slot] != then.product:
            swapped.append(s.slot)
        cost = now.unit_cost if now is not None and now.product == then.product else then.unit_cost
        slots.append(replace(s, product=then.product, unit_cost_cents=cost, stock=then.units))
    # Before the opening there is no competition balance to check against.
    bal = ledger.balance(reader.conn, plan_made) if plan_made >= reader.opened else None
    spendable = None if bal is None else int(bal.spendable_cents // 1)
    rejected = frozenset(sku for sku, at in reader.rejected if at <= plan_made)
    notes = tuple(n for n in before.notes if not n.startswith(_REPLAY_UNKNOWN_NOTE))
    return replace(before, slots=tuple(slots), balance_cents=spendable, catalog=reader.catalog_at(plan_made),
                   rejected=rejected, ceilings=reader.ceilings, notes=notes), tuple(swapped)


# A product the scorer may name: a catalog SKU's characters only, so plan text can't ride along.
_SKU = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
# Above this a "price" is not a shelf price at all ($1,000.00).
_MAX_PRICE_CENTS = 100_000


def _slot_name(entry: Any) -> str | None:
    slot = entry.get("slot") if isinstance(entry, dict) else None
    if isinstance(slot, int) and not isinstance(slot, bool):
        return str(slot)
    return slot if isinstance(slot, str) else None


def _price_text(value: Any) -> str | None:
    """A stored sale price as the replayer's text, or None when it isn't a shelf price."""
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        return None
    text = str(value)
    try:
        cents = to_cents(text)
    except (ValueError, ArithmeticError):
        return None
    return text if 0 <= cents <= _MAX_PRICE_CENTS else None


def validate_plan(raw: Any, ours: tuple[str, ...]) -> list[str]:
    """Every reason a stored live plan isn't one the scorer can read, before anything adapts it.

    Each reason is a fixed sentence: the only values in one are our own slot numbers, never the
    plan's text or a field's value. An entry for a selection outside our assignment is a failure,
    named without its number."""
    if not isinstance(raw, dict) or not isinstance(raw.get("slots"), list):
        return ["The stored plan isn't in the plan's shape: it has no list of slots."]
    problems: list[str] = []
    seen: list[str] = []
    foreign = shapeless = False
    for entry in raw["slots"]:
        slot = _slot_name(entry)
        if slot is None:
            shapeless = True
        elif slot not in ours:
            foreign = True
        else:
            seen.append(slot)
            problems += _entry_problems(slot, entry)
    head = (["The plan names a selection outside our assignment."] if foreign else []) + (
        ["An entry in the plan's list of slots isn't a slot entry."] if shapeless else [])
    twice = [s for s in ours if seen.count(s) > 1]
    missing = [s for s in ours if s not in seen]
    if twice:
        problems.append(f"Slots listed more than once: {', '.join(twice)}.")
    if missing:
        problems.append(f"Slots missing from the plan: {', '.join(missing)}.")
    if raw.get("off_cycle") is not None and not isinstance(raw["off_cycle"], bool):
        problems.append("off_cycle must be true or false.")
    if raw.get("reason") is not None and not isinstance(raw["reason"], str):
        problems.append("The plan's reason isn't text.")
    return head + problems


def _entry_problems(slot: str, entry: dict) -> list[str]:
    problems = []
    if not isinstance(entry.get("keep", False), bool):
        problems.append(f"Slot {slot}: keep must be true or false.")
    fill = entry.get("fill_to")
    if fill is not None and (isinstance(fill, bool) or not isinstance(fill, int) or not 0 <= fill <= MAX_FILL):
        problems.append(f"Slot {slot}: fill_to must be a whole number from 0 to {MAX_FILL}.")
    if entry.get("sale_price") is not None and _price_text(entry["sale_price"]) is None:
        problems.append(f"Slot {slot}: the sale price isn't a price.")
    product = entry.get("product")
    if product is not None and not (isinstance(product, str) and _SKU.fullmatch(product)):
        problems.append(f"Slot {slot}: the product isn't a catalog SKU.")
    if entry.get("reason") is not None and not isinstance(entry["reason"], str):
        problems.append(f"Slot {slot}: the reason isn't text.")
    return problems


def _as_replay_plan(raw: dict, reader: _Reader, state: WeekState) -> tuple[dict, list[str], frozenset[str]]:
    """A stored live plan that passed `validate_plan` (the desired state, `vending.agent.recorded_plan`)
    in the replayer's shape, its notes, and the slots whose fill isn't known.

    The desired state says `off_cycle` for a midweek visit and may leave `fill_to` out to fill the
    column; the replayer wants `visit` and a number. Where the column's capacity isn't known the
    entry keeps its price and product and only its fill is left out, so every hard check still sees
    it. Only a kept slot gets the default reason: the agent writes reasons only for what it changes."""
    notes, slots, unknown_fill = [], [], set()
    for entry in raw["slots"]:
        slot = _slot_name(entry)
        row = {key: entry[key] for key in ("keep", "fill_to", "sale_price", "product", "reason") if key in entry}
        row["slot"] = slot
        if row.get("keep") is True and not (row.get("reason") or "").strip():
            row["reason"] = NO_REASON
        if row.get("sale_price") is not None:
            row["sale_price"] = _price_text(row["sale_price"])
        if not row.get("keep") and row.get("fill_to") is None:
            held = state.slot(slot)
            column = reader.capacity_of(row.get("product") or (held.product if held else None))
            if column is None:
                notes.append(f"Slot {slot} was to be filled to its column, whose capacity isn't known, "
                             "so its outcome isn't scored.")
                unknown_fill.add(slot)
            else:
                row["fill_to"] = column
        slots.append(row)
    visit = "midweek" if raw.get("off_cycle") or raw.get("visit") in ("midweek", "off_cycle") else "scheduled"
    reason = raw.get("reason") if isinstance(raw.get("reason"), str) else ""
    return {"visit": visit, "reason": reason, "slots": slots}, notes, frozenset(unknown_fill)


def _outcome(scored: scoring.Score) -> dict:
    t = scored.totals
    done = [o for o in scored.slots if o.sold is not None]
    return {
        "scored": t.scored,
        "slots": len(scored.slots),
        "sold": sum(o.sold for o in done),
        "sold_out_slots": sum(1 for o in done if o.sold and not o.unsold),
        "stockout_units": t.stockout_units,
        "unsold_units": t.unsold_units,
        "revenue": ledger.money(t.revenue_ex_tax_cents),
        "margin": ledger.money(t.margin_cents),
        "fee": ledger.money(t.fee_cents) if t.fee_cents else None,
    }


def _compare(plan: scoring.Score, base: scoring.Score, swapped: tuple[str, ...] = ()) -> dict | None:
    """The plan's margin less the baseline's, on the slots both score, less any slot the visit swapped
    (its sales are a product the baseline never stocked). None when there's nothing to compare."""
    if plan.totals is None or base.totals is None:
        return None
    mine = {o.slot: o for o in plan.slots if o.sold is not None}
    theirs = {o.slot: o for o in base.slots if o.sold is not None}
    order = [o.slot for o in plan.slots]
    common = [s for s in order if s in mine and s in theirs and s not in swapped]
    unknown = [s for s in order if any(o.get(s) is not None and o[s].margin_cents is None for o in (mine, theirs))]
    out: dict[str, Any] = {"slots": len(common), "plan_margin": None, "baseline_margin": None,
                           "difference": None, "result": None,
                           "excluded": {"swapped": list(swapped), "unknown_cost": unknown}}
    shared_unknown = [s for s in common if s in unknown]
    if not common or shared_unknown:
        out["why"] = ("no slot is scored for both" if not common else
                      f"unit cost unknown on shared slot{'s' if len(shared_unknown) > 1 else ''} "
                      f"{', '.join(shared_unknown)}")
        return out
    ours = sum(mine[s].margin_cents for s in common) - plan.totals.fee_cents
    baseline = sum(theirs[s].margin_cents for s in common) - base.totals.fee_cents
    # Compared at the cent, as shown: a fraction of a cent isn't a win.
    diff = ledger.money(ours - baseline)
    cents = int(diff.replace("-", "").replace(".", ""))
    out.update(plan_margin=ledger.money(ours), baseline_margin=ledger.money(baseline), difference=diff,
               result="tied" if cents == 0 else "behind" if diff.startswith("-") else "beat")
    return out


# --- Incident log ------------------------------------------------------------------------------

def incidents(conn: psycopg.Connection, now: datetime, weeks: dict | None = None) -> dict:
    """The incident log's data, newest first. `weeks` is the report card panel's data, for weeks that
    failed a hard check (None when that panel couldn't be built). Every note is this code's own
    words; a failure's stored text is never read."""
    opened = ledger.opening(conn)
    if opened is None or now < opened.at:
        return {"opened": False}
    since = opened.at
    rows: list[dict] = []
    # Each source reads only its newest INCIDENTS_SHOWN rows, which is all the merge can show; the
    # total is counted in SQL. A run that failed, halted or stuck is one incident, so its tool
    # errors are left out in SQL too, before the limit.
    calls = ("from action_log where at >= %s and at <= %s and (outcome in ('refused', 'failed') or"
             " (tool = 'halt' and outcome = 'done'))")
    runs = ("from agent_runs where mode = 'live' and started_at >= %s and started_at <= %s"
            " and (status in ('failed', 'halted') or (status = 'running' and started_at < %s))")
    total = conn.execute(f"select count(*) {calls}", (since, now)).fetchone()[0]
    total += conn.execute(f"select count(*) {runs}", (since, now, now - STALE_RUN)).fetchone()[0]
    for _id, at, tool, outcome, detail, args in conn.execute(
            "select id, at, tool, outcome, case when outcome = 'failed' then null else detail end, arguments"
            f" {calls} order by at desc, id desc limit %s", (since, now, INCIDENTS_SHOWN)):
        if tool == "halt" and outcome == "done":
            on = _halt_on(args, detail)
            kind = "halt_on" if on else "halt_off" if on is False else "halt"
            rows.append(_row(at, kind, "Halt switch", _line(detail) or "Halt switch flipped."))
        else:
            rows.append(_row(at, outcome, tool, None if outcome == "failed" else _line(detail) or "Refused.",
                             error=outcome == "failed"))
    for id_, started, status, detail in conn.execute(
            f"select id, started_at, status, case when status = 'failed' then null else detail end {runs}"
            " order by started_at desc, id desc limit %s", (since, now, now - STALE_RUN, INCIDENTS_SHOWN)):
        if status == "failed":
            rows.append(_row(started, "run_failed", f"Run {id_}", None, error=True))
        elif status == "halted":
            rows.append(_row(started, "run_halted", f"Run {id_}", _line(detail) or "Stopped by the halt switch."))
        else:
            rows.append(_row(started, "run_stuck", f"Run {id_}",
                             "Still says running over 2 hours after it started: the process may have died."))
    stepped, errors = _error_steps(conn, since, now)
    total += stepped
    for id_, started, n in errors:
        rows.append(_row(started, "tool_errors", f"Run {id_}",
                         f"The model was told {n} tool call{'s' if n != 1 else ''} failed. See How it decided under Runs."))
    # One row per failed round, as the worker wrote each one (`vending.worker.record_failed_round`,
    # `sync_incidents`, migration 026), so a round stays listed after a later round passes or the
    # worker restarts. A round from before the opening isn't one.
    rounds, rounds_total = _sync_incidents(conn, since, now)
    total += rounds_total
    for round_at, jobs in rounds:
        rows.append(_row(round_at, "sync_failed", "Sync round",
                         f"A sync round failed: {failed_jobs_text(jobs)}."))
    checks, check_total = _tracking_outside(conn, since, now)
    total += check_total
    for id_, at, results in checks:
        outside = ", ".join(k for k in TRACKING_RESULTS if results.get(k) == "outside")
        rows.append(_row(at, "tracking_outside", "Tracking check",
                         f"The bridge said {outside} outside: our figures and its counts disagree. "
                         "Compare with `python -m vending.tracking show`."))
    for card in (weeks or {}).get("weeks", []):
        if card["grade"] != "fail":
            continue
        failed = [c["name"] for c in card["checks"] if c["status"] == scoring.FAIL]
        at = card["planned_at"] or _start(date.fromisoformat(card["week"]))
        rows.append(_row(at, "week_failed", f"Week of {_day(card['week'])}",
                         f"Failed the {', '.join(failed)} check{'s' if len(failed) != 1 else ''}; graded as failing."))
        total += 1
    rows.sort(key=lambda r: r["at"], reverse=True)
    return {"opened": True, "total": total, "rows": rows[:INCIDENTS_SHOWN]}


def _error_steps(conn: psycopg.Connection, since: datetime, now: datetime) -> tuple[int, list[tuple]]:
    """How many live runs that didn't fail, halt or stick were told a tool call failed, and the newest
    `INCIDENTS_SHOWN` of them as (id, started_at, errors)."""
    import psycopg
    where = ("from agent_steps s join agent_runs r on r.id = s.run_id where r.mode = 'live'"
             " and r.started_at >= %s and r.started_at <= %s and s.is_error"
             " and not (r.status in ('failed', 'halted') or (r.status = 'running' and r.started_at < %s))")
    args = (since, now, now - STALE_RUN)
    try:
        with conn.transaction():
            count = conn.execute(f"select count(distinct r.id) {where}", args).fetchone()[0]
            rows = conn.execute(f"select r.id, r.started_at, count(*) {where} group by r.id, r.started_at"
                                " order by r.started_at desc, r.id desc limit %s", (*args, INCIDENTS_SHOWN)).fetchall()
            return count, rows
    except psycopg.errors.UndefinedTable:     # before migration 014
        return 0, []


def _sync_incidents(conn: psycopg.Connection, since: datetime, now: datetime) -> tuple[list, int]:
    """The worker's failed rounds (`sync_incidents`, migration 026), as (round_at, jobs) newest
    first, with their count. Before the migration there is no table, and that is no incidents.
    `jobs` is what the worker wrote from its own round: each job's name, exit code and outcome —
    never error text — so the note here is fixed words by construction.

    The query runs in its own transaction (a savepoint inside the panel's), like `_error_steps`':
    caught outside it, an `UndefinedTable` rolls back to the savepoint instead of aborting the
    panel's transaction, where the next query would fail and the whole log would disappear."""
    import psycopg
    try:
        with conn.transaction():
            rows = conn.execute("select round_at, jobs from sync_incidents"
                                " where round_at >= %s and round_at <= %s"
                                " order by round_at desc, id desc", (since, now)).fetchall()
    except psycopg.errors.UndefinedTable:     # before migration 026
        return [], 0
    return rows, len(rows)


def _tracking_outside(conn: psycopg.Connection, since: datetime, now: datetime) -> tuple[list, int]:
    """The tracking checks (migration 021) whose verdicts name a figure `outside`, as
    (id, answered_at, results) newest first, with their count. Before the migration there is no
    table, and that is no incidents. `results` is the bridge's JSON, so every row is read through
    the shared allowlist (`tracking.verdicts`), and a `results` that isn't the documented four-key
    object at all is skipped: an array or an extra key can neither crash this query nor hide the
    log. The stored request and answer never reach the log.

    The window and the order are the check's event time, `answered_at` falling back to `sent_at` —
    the same moment the banner names (`dashboard._tracking`), so a check sent before the opening
    but answered after it is an incident here and not only there. Like `_sync_incidents`, the
    query runs in its own transaction, so a missing table cannot abort the panel's."""
    import psycopg
    where = ("from tracking_checks where results is not null and jsonb_typeof(results) = 'object'"
             " and coalesce(answered_at, sent_at) >= %s and coalesce(answered_at, sent_at) <= %s")
    try:
        with conn.transaction():
            rows = conn.execute(f"select id, coalesce(answered_at, sent_at) as at, results {where}"
                                " order by coalesce(answered_at, sent_at) desc, id desc",
                                (since, now)).fetchall()
    except psycopg.errors.UndefinedTable:     # before migration 021
        return [], 0
    checks = [(id_, at, v) for id_, at, v in ((r[0], r[1], tracking.verdicts(r[2])) for r in rows)
              if any(v.get(k) == "outside" for k in TRACKING_RESULTS)]
    return checks[:INCIDENTS_SHOWN], len(checks)


def failed_sync_jobs(syncs: dict | None) -> str | None:
    """The failed jobs of the worker's latest sync round (`vending.worker.State.public`), each named
    with its exit code or outcome, or None when every job passed, no round has run yet, or the
    worker handed nothing over (the dashboard run on its own). Fixed words only: the banner
    (`dashboard.build_alerts`), Needs attention and this log all share one wording."""
    if not syncs or not syncs.get("rounds"):
        return None
    return failed_jobs_text(syncs.get("results"))


def failed_jobs_text(results: list | None) -> str | None:
    """The failed jobs of one sync round, each named with its exit code or outcome, or None when
    every job passed. Used for the worker's latest round (`failed_sync_jobs`) and for the stored
    `sync_incidents` rows, whose `jobs` is the same sanitized list."""
    failed = [r for r in results or [] if r.get("exit_code") != 0]
    if not failed:
        return None
    return ", ".join(f"`{r.get('job')}` " + (f"exit {r['exit_code']}" if r.get("exit_code") is not None
                                             else str(r.get("outcome") or "no exit code"))
                     for r in failed)


def sync_round_at(syncs: dict | None) -> datetime | None:
    """When the worker's latest sync round ran, or None when it can't be read."""
    try:
        return datetime.fromisoformat(syncs["last_round_at"])
    except (KeyError, TypeError, ValueError):
        return None


def _row(at: datetime, kind: str, what: str, note: str | None, *, error: bool = False) -> dict:
    return {"at": at, "week": ledger.week_of(at.astimezone(EASTERN)).isoformat(), "kind": kind, "what": what,
            "note": note, "error": error}


def _halt_on(args: Any, detail: str | None) -> bool | None:
    if isinstance(args, dict):
        if isinstance(args.get("halted"), bool):
            return args["halted"]
        if args.get("state") in ("on", "off"):
            return args["state"] == "on"
    return None


def _line(text: str | None) -> str | None:
    """The first line, cut to `NOTE_CHARS`."""
    if not text or not text.strip():
        return None
    line = text.strip().splitlines()[0]
    return line if len(line) <= NOTE_CHARS else line[:NOTE_CHARS - 1].rstrip() + "…"


# --- Page --------------------------------------------------------------------------------------

def _day(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d:%a} {d.day} {d:%b}"


_GRADE_TONE = {"beat": "ok", "tied": "info", "behind": "warn", "fail": "bad", "no_plan": "warn",
               "rejected": "warn", "unscored": "muted", "opening": "muted", "incomplete": "muted"}
_REQUEST_WORDS = {"fulfilled": "request fulfilled", "submitted": "request submitted, no receipt yet",
                  "rejected": "request rejected"}
_CHECK_TONE = {scoring.PASS: "ok", scoring.FAIL: "bad", scoring.SKIP: "muted"}
_KIND = {"refused": ("Refused", "warn"), "failed": ("Failed", "bad"), "halt_on": ("Halt on", "warn"),
         "halt_off": ("Halt off", "info"), "halt": ("Halt switch", "info"), "run_failed": ("Run failed", "bad"),
         "run_halted": ("Run halted", "warn"), "run_stuck": ("Run stuck", "bad"),
         "tool_errors": ("Tool errors", "warn"), "week_failed": ("Week failed", "bad"),
         "sync_failed": ("Sync failed", "bad"), "tracking_outside": ("Tracking outside", "bad")}


def _pill(text: str, tone: str) -> str:
    return f'<span class="pill {tone}">{escape(text)}</span>'


def _figures(o: dict | None) -> str:
    if o is None:
        return "<td>—</td>" * 4
    margin = "$" + o["margin"] if o["margin"] is not None else "unknown"
    return (f'<td>{o["sold"]}</td><td>{o["sold_out_slots"]}</td><td>{o["unsold_units"]}</td>'
            f'<td>{escape(margin)}</td>')


def cards_html(panel: dict) -> str:
    if not panel["ok"]:
        return f'<p class="empty">Not available: {escape(panel["why"])}</p>'
    data = panel["data"]
    if not data["opened"]:
        return ('<p class="empty">No report cards yet. They start at the ledger opening; the first graded week is '
                'the first full week (Saturday to Friday, Eastern) after the first fill.</p>')
    if data.get("assignment_missing"):
        return '<p class="empty">Slot assignment unavailable, so no week can be scored.</p>'
    out = ['<p class="sub lede">Each week (Saturday to Friday, Eastern) scores the plan its visit loaded, '
           "matched through the purchase request, with the week replayer's scoring, against the baseline: restock "
           "each slot to last week's sales at last week's price. A failed hard check fails the week, whatever it "
           "earned. Sold out is estimated: the model's rail ran empty, so demand may have been higher. Unsold "
           "units keep their cost.</p>"]
    for card in data["weeks"]:
        out.append(_card_html(card))
    return "".join(out)


def _card_html(card: dict) -> str:
    title = f'Week of {_day(card["week"])} to {_day(card["ends"])}'
    state = "" if card["final"] or card["grade"] == "opening" else ' <span class="hint">in progress</span>'
    plan = ""
    if card["run_id"] is not None:
        plan = (f'<p class="sub">Plan from run {int(card["run_id"])}, made <time>{escape(_when(card["planned_at"]))}'
                f'</time> ({escape(_REQUEST_WORDS.get(card.get("request"), "request unknown"))}).</p>')
    covered = []
    for label, key in (("last week", "last"), ("this week", "this")):
        span = (card.get("polls") or {}).get(key)
        if span:
            covered.append(f'{label} <time>{escape(_when(span[0]))}</time> to <time>{escape(_when(span[1]))}</time>')
    if covered:
        plan += f'<p class="sub">Polls covered: {"; ".join(covered)}.</p>'
    checks = ""
    if card["checks"]:
        items = "".join(
            f'<li>{_pill(c["status"], _CHECK_TONE.get(c["status"], "muted"))} <b>{escape(c["name"])}</b> '
            f'<span class="sub">{escape(c["detail"])}</span></li>' for c in card["checks"])
        checks = f'<ul class="rc-checks" aria-label="Hard checks">{items}</ul>'
    table = ""
    if card["outcome"] is not None:
        rows = (f'<tr><th scope="row">The plan</th>{_figures(card["outcome"])}</tr>'
                f'<tr><th scope="row">Baseline</th>{_figures(card["baseline"])}</tr>')
        table = ('<table class="rc-outcome"><thead><tr><th scope="col">All scored slots</th><th scope="col">Sold</th>'
                 '<th scope="col">Sold out (estimated)</th><th scope="col">Unsold</th><th scope="col">Margin</th></tr></thead>'
                 f'<tbody>{rows}</tbody></table>')
    comparison = ""
    cmp = card["comparison"]
    if cmp is not None:
        excluded = cmp.get("excluded") or {}
        if cmp["result"] is None:
            comparison = f'<p class="sub">Against the baseline: unavailable ({escape(cmp["why"])}).</p>'
            left_out = [f"slot {s} (swapped at the visit)" for s in excluded.get("swapped", [])]
        else:
            n = int(cmp["slots"])
            gap = "level" if cmp["result"] == "tied" else (
                f'{"ahead" if cmp["result"] == "beat" else "behind"} by ${cmp["difference"].lstrip("-")}')
            comparison = (f'<p class="sub">Against the baseline, on {n} shared slot{"s" if n != 1 else ""}: '
                          f'{escape(gap)} (margin ${escape(cmp["plan_margin"])} against '
                          f'${escape(cmp["baseline_margin"])}).</p>')
            left_out = ([f"slot {s} (swapped at the visit)" for s in excluded.get("swapped", [])]
                        + [f"slot {s} (unit cost unknown)" for s in excluded.get("unknown_cost", [])])
        if left_out:
            comparison += f'<p class="sub">Not in the comparison: {escape("; ".join(left_out))}.</p>'
    notes = "".join(f'<li>{escape(n)}</li>' for n in card["notes"])
    notes = f'<ul class="rc-notes">{notes}</ul>' if notes else ""
    return (f'<article class="rc-card"><h3>{escape(title)}{state} '
            f'{_pill(card["grade_text"], _GRADE_TONE[card["grade"]])}</h3>{plan}{checks}{table}{comparison}{notes}'
            '</article>')


def _when(at: Any) -> str:
    if isinstance(at, datetime):
        local = at.astimezone(EASTERN)
        return f"{local:%a} {local.day} {local:%b} {local:%H:%M} Eastern"
    return str(at)


def incidents_html(panel: dict) -> str:
    if not panel["ok"]:
        return f'<p class="empty">Not available: {escape(panel["why"])}</p>'
    data = panel["data"]
    if not data["opened"]:
        return '<p class="empty">No incidents yet. The log starts at the ledger opening.</p>'
    if not data["rows"]:
        return '<p class="empty">No incidents since the opening.</p>'
    shown, total = len(data["rows"]), data["total"]
    lede = (f'<p class="sub lede">Refused and failed tool calls, halts, runs that failed, a failed sync round, '
            f'tracking checks that came back outside, and weeks that failed a hard check, newest first. Error text '
            f'isn\'t shown. {"Latest " + str(shown) + " of " + str(total) + "." if total > shown else ""}</p>')
    body = []
    for r in data["rows"]:
        label, tone = _KIND.get(r["kind"], (r["kind"], "muted"))
        body.append(f'<tr><td><time>{escape(_when(r["at"]))}</time><small>week of {escape(_day(r["week"]))}</small></td>'
                    f'<td>{_pill(label, tone)}</td><td><code>{escape(r["what"])}</code></td>'
                    f'<td>{escape(r["note"] or "")}</td></tr>')
    return (lede + '<div class="incidents-wrap"><table class="incidents"><thead><tr><th scope="col">When</th>'
            '<th scope="col">What</th><th scope="col">Where</th><th scope="col">Note</th></tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>')


STYLE = """
.rc-card { border:1px solid var(--line); border-radius:8px; padding:10px 12px; margin:0 0 10px }
.rc-card h3 { margin:0 0 6px; font-size:14px; display:flex; flex-wrap:wrap; gap:6px; align-items:center }
.rc-checks, .rc-notes { list-style:none; margin:6px 0; padding:0; font-size:13px }
.rc-checks li, .rc-notes li { margin:3px 0; overflow-wrap:anywhere }
.rc-notes li { color:var(--muted) }
.rc-outcome { border-collapse:collapse; font-variant-numeric:tabular-nums; font-size:13px; margin:6px 0 }
.rc-outcome th, .rc-outcome td { padding:4px 10px 4px 0; text-align:right; border-bottom:1px solid var(--line) }
.rc-outcome th:first-child { text-align:left }
.rc-outcome thead { color:var(--muted); font-size:11px }
.incidents-wrap { overflow-x:auto }
.incidents { width:100%; border-collapse:collapse; font-size:13px }
.incidents th, .incidents td { padding:5px 6px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top }
.incidents thead { color:var(--muted); font-size:11px }
.incidents td:first-child { white-space:nowrap }
.incidents td small { display:block; color:var(--muted) }
.incidents td:last-child { overflow-wrap:anywhere; min-width:14em }
"""
