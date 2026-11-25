"""Report and record development API spend before the real ledger opening."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg

from vending import config, db, harness, ledger

EASTERN = ZoneInfo(ledger.TIMEZONE)
PURPOSES = ("live", "test")         # the harness's purposes
DETAIL = re.compile(r"^(?P<model>\S+): (?P<tokens_in>\d+) tokens in, (?P<tokens_out>\d+) out\b")
# What the harness appends to a reference: " pending" or " estimate: <why>" while the cost is still the worst case,
# " refused: <why>" for a request the API turned down, or " <message id>[ (note)]" once corrected.
UNSETTLED = re.compile(r" (pending|estimate: .*)$")
SETTLED = re.compile(r"(^| )(msg_\S+( \(.*\))?|refused: .*)$")
TURN = re.compile(r" turn \d+$")
DEMO_SOURCE = "from run notes, not the ledger"
DB_SOURCE = "database"
DEFAULT_CAP_CENTS = 8500
CATEGORIES = ("agent runs", "price search", "simulations/bench", "manual")


@dataclass(frozen=True)
class Call:
    """One model call, or for run notes, one run's calls taken together (`count` of them)."""
    at: datetime
    purpose: str
    model: str
    label: str              # the run's label, without "turn N"
    run: str                # what ties one run's calls together
    cost_cents: Fraction
    confirmed: bool         # False: still the worst case, never corrected
    tokens_in: int | None = None    # every input token, cached or not; None: not known
    tokens_out: int | None = None
    count: int | None = 1           # None: the notes don't say how many calls
    category: str = "agent runs"


@dataclass
class Bucket:
    """One day or one week (`start` is the day, or the Saturday it began), Eastern time.

    Every figure but the last two is development spend. `by_purpose`, `by_model`
    and `by_label` split the confirmed spend; the unconfirmed spend is its own
    figure, so each split plus it adds up to the total.
    """
    start: date
    calls: int = 0
    calls_unknown: int = 0          # run notes that don't say how many calls
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_unknown: int = 0         # calls whose tokens aren't known
    confirmed_cents: Fraction = Fraction(0)
    unconfirmed_cents: Fraction = Fraction(0)
    by_category: dict[str, Fraction] = field(default_factory=dict)
    unconfirmed_by_category: dict[str, Fraction] = field(default_factory=dict)
    by_purpose: dict[str, Fraction] = field(default_factory=dict)
    by_model: dict[str, Fraction] = field(default_factory=dict)
    by_label: dict[str, Fraction] = field(default_factory=dict)
    cumulative_cents: Fraction = Fraction(0)    # development spend to the end of this bucket
    after_opening_test_cents: Fraction = Fraction(0)
    after_opening_test_calls: int = 0
    after_opening_test_unconfirmed_cents: Fraction = Fraction(0)

    @property
    def total_cents(self) -> Fraction:
        return self.confirmed_cents + self.unconfirmed_cents

    def add(self, call: Call) -> None:
        if call.count is None:
            self.calls_unknown += 1
        else:
            self.calls += call.count
        if call.tokens_in is None or call.tokens_out is None:
            self.tokens_unknown += call.count or 1
        else:
            self.tokens_in += call.tokens_in
            self.tokens_out += call.tokens_out
        self.by_category[call.category] = self.by_category.get(call.category, Fraction(0)) + call.cost_cents
        if not call.confirmed:
            self.unconfirmed_by_category[call.category] = (
                self.unconfirmed_by_category.get(call.category, Fraction(0)) + call.cost_cents)
            self.unconfirmed_cents += call.cost_cents
            return
        self.confirmed_cents += call.cost_cents
        for split, key in ((self.by_purpose, call.purpose), (self.by_model, call.model), (self.by_label, call.label)):
            split[key] = split.get(key, Fraction(0)) + call.cost_cents


@dataclass
class Run:
    key: str
    first_at: datetime
    purpose: str
    label: str
    models: list[str]
    calls: int | None
    tokens_in: int | None
    tokens_out: int | None
    confirmed_cents: Fraction
    unconfirmed_cents: Fraction
    category: str
    after_opening: bool             # a test run after the opening: shouldn't happen, so flagged, not development

    @property
    def total_cents(self) -> Fraction:
        return self.confirmed_cents + self.unconfirmed_cents


@dataclass(frozen=True)
class Report:
    unit: str                       # "week" or "day"
    as_of: datetime
    opening_at: datetime | None     # where development ends; None: the ledger isn't open yet
    buckets: list[Bucket]
    runs: list[Run]                 # newest first
    totals: Bucket                  # development, all of it (start: the first bucket's)
    source: str
    cap_cents: int = DEFAULT_CAP_CENTS
    first_spend_date: date | None = None


# --- Reading -------------------------------------------------------------------------

def parse_reference(reference: str | None) -> tuple[str, str, bool]:
    """(purpose, run label, confirmed) from a ledger entry's reference, as the harness writes it."""
    text = (reference or "").strip()
    unsettled = UNSETTLED.search(text)
    worst_case = "kept at worst case" in text
    if unsettled:
        text = text[:unsettled.start()]
    else:
        text = SETTLED.sub("", text)
    first, _, rest = text.partition(" ")
    if first in PURPOSES:
        return first, TURN.sub("", rest.strip()), unsettled is None and not worst_case
    return "live", TURN.sub("", text.strip()), unsettled is None and not worst_case


def parse_detail(detail: str) -> tuple[str, int | None, int | None]:
    """(model, tokens in, tokens out) from a `tokens` entry's detail: "claude-opus-5-5: 8120 tokens in, 640 out"."""
    found = DETAIL.match(detail.strip())
    if not found:
        return "unknown", None, None
    return found["model"], int(found["tokens_in"]), int(found["tokens_out"])


def _has(conn: psycopg.Connection, table: str) -> bool:
    return conn.execute("select to_regclass(%s)", (table,)).fetchone()[0] is not None


def _price_calls(conn: psycopg.Connection, has_calls: bool) -> tuple[list[Call], set[int]]:
    if not _has(conn, "price_lookups"):
        return [], set()
    # Price memory stores a rounded copy of the harness charge. Legacy rows
    # have no call ID. Pair one lookup with one harness row using the writer's
    # label suffix and cost, at the time the harness settled the call.
    rows = conn.execute("select id, at, sku, store, model, cost_usd, (to_jsonb(p)->>'model_call_id')::bigint"
                        " from price_lookups p"
                        " where cost_usd <> 0 order by at, id").fetchall()
    candidates = defaultdict(list)
    if has_calls:
        for id_, at, finished, label, amount in conn.execute(
                "select id, at, finished_at, label, round(cost_usd, 6) from model_calls"
                " where status <> 'refused' and cost_usd <> 0 order by id"):
            if harness.spend_category(label) == "price search":
                candidates[amount].append((id_, finished or at, label))
    used = {linked for *_, linked in rows if linked is not None}
    out = []
    for id_, at, sku, store, model, cost, linked in rows:
        if linked is not None:
            continue
        suffix = f" {sku} {store}"
        matches = [(abs(when - at), key) for key, when, label in candidates[cost]
                   if key not in used and label.endswith(suffix) and abs(when - at) <= timedelta(seconds=60)]
        if matches:
            used.add(min(matches)[1])
        else:
            out.append(Call(at, "test", model, "Price search", f"lookup {id_}", Fraction(cost) * 100,
                            True, category="price search"))
    return out, used


def calls(conn: psycopg.Connection) -> list[Call]:
    """Every model call either table records, each once, oldest first."""
    has_calls = _has(conn, "model_calls")
    out, linked_price_calls = _price_calls(conn, has_calls)
    has_spend = _has(conn, "model_spend")
    if has_calls:
        shadow = (" and not exists (select 1 from model_spend s where s.id::text = to_jsonb(m)->>'spend_id')"
                  if has_spend else "")
        rows = conn.execute(
            "select id, at, purpose, label, run_id, coalesce(answered_by, model), status, cost_usd,"
            " input_tokens + coalesce(cache_write_tokens, 0) + coalesce(cache_read_tokens, 0), output_tokens, detail"
            " from model_calls m where status <> 'refused'"
            f"{shadow} order by at, id").fetchall()
        for id_, at, purpose, label, run_id, model, status, cost, tokens_in, tokens_out, detail in rows:
            ok = status == "ok" and "kept at worst case" not in detail
            category = "price search" if id_ in linked_price_calls else harness.spend_category(label)
            # The price-search label may include a product or selection.
            name = "Price search" if category == "price search" else TURN.sub("", label.strip())
            out.append(Call(at, purpose, model, name, f"run {run_id}", Fraction(cost) * 100, ok,
                            tokens_in if ok else None, tokens_out if ok else None, category=category))
    if _has(conn, "ledger_entries"):
        orphan = " and not exists (select 1 from model_calls c where c.ledger_entry = e.id)" if has_calls else ""
        rows = conn.execute("select at, amount_cents, detail, reference from ledger_entries e"
                            f" where kind = 'tokens'{orphan} order by at, id").fetchall()
        for at, amount, detail, reference in rows:
            purpose, label, ok = parse_reference(reference)
            model, tokens_in, tokens_out = parse_detail(detail)
            day = at.astimezone(EASTERN).date()
            out.append(Call(at, purpose, model, label, f"ledger {day} {purpose} {label}", -Fraction(amount), ok,
                            tokens_in if ok else None, tokens_out if ok else None))
    if has_spend:
        for at, purpose, category, label, run, model, cost, ok, tin, tout in conn.execute(
                "select at, purpose, category, label, run_id, model, cost_usd, confirmed, tokens_in, tokens_out"
                " from model_spend where not refused order by at, id"):
            out.append(Call(at, purpose, model, label, f"run {run}", Fraction(cost) * 100, ok,
                            tin if ok else None, tout if ok else None, category=category))
    if _has(conn, "development_spend"):
        for at, category, model, label, run, cost, ok, tin, tout in conn.execute(
                "select at, category, model, label, run_id, cost_usd, confirmed, tokens_in, tokens_out"
                " from development_spend order by at, source_id"):
            out.append(Call(at, "test", model, label, run, Fraction(cost) * 100, ok, tin, tout,
                            count=None if category == "manual" else 1, category=category))
    out.sort(key=lambda c: c.at)
    return out


def opening_at(conn: psycopg.Connection) -> datetime | None:
    if not _has(conn, "ledger_entries"):
        return None
    opened = ledger.real_opening(conn)
    return opened.at if opened else None


# --- Summing (pure) ------------------------------------------------------------------

def _start(moment: datetime, unit: str) -> date:
    local = moment.astimezone(EASTERN)
    return ledger.week_of(local) if unit == "week" else local.date()


def summarise(recorded: list[Call], opened: datetime | None, unit: str, as_of: datetime,
              source: str = DB_SOURCE, cap_cents: int = DEFAULT_CAP_CENTS) -> Report:
    """Book each call to its Eastern day or week: development before `opened`, test calls after it apart.

    Quiet days and weeks are included, from the first call to the opening (or to
    `as_of` while the ledger isn't open), and on to the last test call after it.
    """
    if unit not in ("week", "day"):
        raise ValueError(f"unit must be week or day, not {unit!r}")
    step = timedelta(days=7 if unit == "week" else 1)
    kept = sorted((c for c in recorded if not ledger.competition_call(c.purpose, c.at, opened)),
                  key=lambda c: c.at)
    if not kept:
        buckets = [Bucket(_start(opened, unit))] if opened else []
        return Report(unit, as_of, opened, buckets, [], Bucket(_start(as_of, unit)), source, cap_cents)
    end = _start(opened if opened else as_of, unit)
    end = max(end, *(_start(c.at, unit) for c in kept))
    buckets: dict[date, Bucket] = {}
    day = min(_start(kept[0].at, unit), _start(opened, unit)) if opened else _start(kept[0].at, unit)
    while day <= end:
        buckets[day] = Bucket(day)
        day += step
    totals = Bucket(next(iter(buckets)))
    grouped: dict[tuple[str, str, bool], list[Call]] = defaultdict(list)
    for c in kept:
        bucket = buckets[_start(c.at, unit)]
        if ledger.before_opening(c.at, opened):
            bucket.add(c)
            totals.add(c)
        else:
            bucket.after_opening_test_cents += c.cost_cents
            bucket.after_opening_test_calls += c.count or 0
            if not c.confirmed:
                bucket.after_opening_test_unconfirmed_cents += c.cost_cents
        grouped[(c.run, c.category, opened is not None and c.at >= opened)].append(c)
    first_spend = next((c.at.astimezone(EASTERN).date() for c in kept
                        if c.cost_cents and ledger.before_opening(c.at, opened)), None)
    running = Fraction(0)
    for bucket in buckets.values():
        running += bucket.total_cents
        bucket.cumulative_cents = running
    runs = [_run(key[0], group, opened) for key, group in grouped.items()]
    runs.sort(key=lambda r: r.first_at, reverse=True)
    return Report(unit, as_of, opened, list(buckets.values()), runs, totals, source, cap_cents, first_spend)


def _run(key: str, group: list[Call], opened: datetime | None) -> Run:
    models = Counter(c.model for c in group)
    known = all(c.tokens_in is not None and c.tokens_out is not None for c in group)
    return Run(
        key, group[0].at, group[0].purpose, group[0].label, [m for m, _ in models.most_common()],
        None if any(c.count is None for c in group) else sum(c.count for c in group),
        sum(c.tokens_in for c in group) if known else None, sum(c.tokens_out for c in group) if known else None,
        sum((c.cost_cents for c in group if c.confirmed), Fraction(0)),
        sum((c.cost_cents for c in group if not c.confirmed), Fraction(0)),
        group[0].category, opened is not None and group[0].at >= opened)


def spend_cap(conn: psycopg.Connection) -> int:
    if not _has(conn, "config"):
        return DEFAULT_CAP_CENTS
    return config.get_value(conn, "dev_spend_cap_cents", DEFAULT_CAP_CENTS)


def weekly(conn: psycopg.Connection, now: datetime | None = None) -> Report:
    """Development spend per Eastern week, Saturday to Friday."""
    return summarise(calls(conn), opening_at(conn), "week", now or ledger.db_now(conn), cap_cents=spend_cap(conn))


def daily(conn: psycopg.Connection, now: datetime | None = None) -> Report:
    """Development spend per Eastern calendar day."""
    return summarise(calls(conn), opening_at(conn), "day", now or ledger.db_now(conn), cap_cents=spend_cap(conn))


def chart_reports(conn: psycopg.Connection, now: datetime) -> tuple[Report, Report]:
    recorded, opened, cap = calls(conn), opening_at(conn), spend_cap(conn)
    return (summarise(recorded, opened, "week", now, cap_cents=cap),
            summarise(recorded, opened, "day", now, cap_cents=cap))


# --- Demo: the runs the team wrote down ------------------------------------------------

def _et(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=EASTERN)


# The first model sweep, 11 Nov from 17:35 ET (docs/benchmark.md, "The first run"): ten cases a config,
# $/decision as reported, $2.43 in all. Its token counts weren't written down.
SWEEP = (("opus@low", "claude-opus-5-5", "0.0492"), ("sonnet@high", "claude-sonnet-5-5", "0.0279"),
         ("haiku@medium", "claude-haiku-5-5", "0.0016"), ("opus@medium", "claude-opus-5-5", "0.0570"),
         ("opus@high", "claude-opus-5-5", "0.0609"), ("sonnet@low", "claude-sonnet-5-5", "0.0214"),
         ("sonnet@medium", "claude-sonnet-5-5", "0.0217"), ("haiku@high", "claude-haiku-5-5", "0.0019"),
         ("haiku@low", "claude-haiku-5-5", "0.0014"))


def demo_calls() -> list[Call]:
    """The documented runs before the opening, one entry per run (the sweep: per config).

    Runs 1 and 3 have no time of day in the notes: they are placed late on the day
    the notes give, in the order the notes list them (run 2 started 00:02 on 12 Nov).
    """
    def cents(dollars: str) -> Fraction:
        return ledger.dollars_to_cents(dollars)

    out = []
    for i, (config, model, per_case) in enumerate(SWEEP):
        out.append(Call(_et("2026-11-11T17:35") + timedelta(minutes=i), "test", model,
                        f"sweep 20261111-173503 {config}", f"sweep {config}", cents(per_case) * 10, True, count=10,
                        category="simulations/bench"))
    opus = "claude-opus-5-5"
    out += [
        # docs/agent.md: the first runs against the real model, 11 and 12 Nov, Opus at medium.
        Call(_et("2026-11-11T23:30"), "test", opus, "agent run 1, dry run", "agent run 1",
             cents("0.80"), True, count=8),
        # docs/sample-directive.md: live, but before the opening, so development spend.
        Call(_et("2026-11-12T00:02"), "live", opus, "agent run 2, live test (pr_v8hB_h3gQatc)", "agent run 2",
             cents("0.40"), True, count=None),
        Call(_et("2026-11-12T00:30"), "test", opus, "agent run 3, dry run", "agent run 3",
             cents("0.08"), True, count=None),
        # docs/guardrail-evidence.md: halted by a person before its second turn, 06:34 UTC.
        Call(datetime(2026, 11, 12, 6, 34, 25, tzinfo=timezone.utc), "test", opus, "agent run 4, halt switch test",
             "agent run 4", cents("0.024956"), True, count=1),
    ]
    return out


def demo(unit: str, as_of: datetime) -> Report:
    return summarise(demo_calls(), None, unit, as_of, DEMO_SOURCE)


# --- Showing -------------------------------------------------------------------------

def usd(cents: Fraction) -> str:
    """Cents as dollars to four places, rounded half up: a call can cost a tenth of a cent."""
    value = (Decimal(cents.numerator) / Decimal(cents.denominator) / 100).quantize(Decimal("0.0001"), ROUND_HALF_UP)
    return f"${value:,.4f}"


def _dollars(cents: Fraction) -> str:
    """For JSON: dollars as an exact decimal string, "0.1234499", never a float.

    A figure is rounded once, where it is shown: by `usd` here, and by the page's
    own formatter, which rounds the same string the same way (half up). A float
    here would round twice, and $0.1234499 would show as $0.1235.
    """
    value = cents / 100
    rest = value.denominator
    for prime in (2, 5):
        while rest % prime == 0:
            rest //= prime
    if rest != 1:       # every amount comes from a decimal (cost_usd, amount_cents, a run note), so sums are too
        raise ValueError(f"{value} dollars has no exact decimal form")
    places = 0
    while (10 ** places) % value.denominator:
        places += 1
    return format(Decimal(value.numerator * (10 ** places // value.denominator)).scaleb(-places), "f")


def _bucket_json(b: Bucket) -> dict:
    return {
        "start": b.start.isoformat(), "calls": b.calls, "calls_unknown": b.calls_unknown,
        "tokens_in": b.tokens_in, "tokens_out": b.tokens_out, "tokens_unknown": b.tokens_unknown,
        "total_usd": _dollars(b.total_cents), "confirmed_usd": _dollars(b.confirmed_cents),
        "unconfirmed_usd": _dollars(b.unconfirmed_cents),
        "by_category": {k: _dollars(v) for k, v in b.by_category.items()},
        "unconfirmed_by_category": {k: _dollars(v) for k, v in b.unconfirmed_by_category.items()},
        "by_purpose": {k: _dollars(v) for k, v in sorted(b.by_purpose.items())},
        "by_model": {k: _dollars(v) for k, v in sorted(b.by_model.items())},
        "by_label": {k: _dollars(v) for k, v in sorted(b.by_label.items())},
        "cumulative_usd": _dollars(b.cumulative_cents),
        "after_opening_test_usd": _dollars(b.after_opening_test_cents),
        "after_opening_test_calls": b.after_opening_test_calls,
        "after_opening_test_unconfirmed_usd": _dollars(b.after_opening_test_unconfirmed_cents),
    }


def to_json(report: Report) -> dict:
    t = report.totals
    return {
        "unit": report.unit,
        "source": report.source,
        "cap_usd": _dollars(Fraction(report.cap_cents)),
        "first_spend_date": report.first_spend_date.isoformat() if report.first_spend_date else None,
        "as_of": report.as_of.astimezone(EASTERN).isoformat(timespec="seconds"),
        "opening_at": report.opening_at.astimezone(EASTERN).isoformat(timespec="seconds") if report.opening_at else None,
        "totals": _bucket_json(t) | {"runs": sum(1 for r in report.runs if not r.after_opening)},
        "buckets": [_bucket_json(b) for b in report.buckets],
        "runs": [{"first_at": r.first_at.astimezone(EASTERN).isoformat(timespec="seconds"), "purpose": r.purpose,
                  "label": r.label, "run_id": r.key, "category": r.category, "models": r.models, "calls": r.calls, "tokens_in": r.tokens_in,
                  "tokens_out": r.tokens_out, "total_usd": _dollars(r.total_cents),
                  "unconfirmed_usd": _dollars(r.unconfirmed_cents), "after_opening": r.after_opening}
                 for r in report.runs],
    }


def _table(header: tuple, rows: list[tuple]) -> list[str]:
    widths = [max(len(str(r[i])) for r in (header, *rows)) for i in range(len(header))]
    return ["  ".join(str(v).ljust(w) for v, w in zip(r, widths)).rstrip() for r in (header, *rows)]


def _count(known: int, unknown: int) -> str:
    return f"{known:,}" + (f" + {unknown} not noted" if unknown else "")


def render(report: Report) -> str:
    unit = "Week of" if report.unit == "week" else "Day"
    out = [f"Development spend on our Anthropic keys, by {report.unit}"
           f"{' (Saturday to Friday)' if report.unit == 'week' else ''}, Eastern, as of "
           f"{report.as_of.astimezone(EASTERN):%d %b %Y %H:%M}. Source: {report.source}."]
    if report.opening_at:
        out.append(f"Development ended at the ledger's opening, {report.opening_at.astimezone(EASTERN):%d %b %Y %H:%M}"
                   " Eastern: the competition's money starts there.")
    else:
        out.append("No ledger opening yet, so every call so far is development spend, none of it from the $600.")
    if not report.buckets:
        return "\n".join(out + ["", "No model calls recorded yet.",
                                 f"$0.00 / ${ledger.money(report.cap_cents)} development cap"])
    rows = [(f"{b.start:%a %d %b}", _count(b.calls, b.calls_unknown), usd(b.total_cents),
             usd(b.by_purpose.get("live", Fraction(0))), usd(b.by_purpose.get("test", Fraction(0))),
             usd(b.unconfirmed_cents) if b.unconfirmed_cents else "", usd(b.cumulative_cents))
            for b in report.buckets if report.opening_at is None or b.start <= _start(report.opening_at, report.unit)]
    t = report.totals
    rows.append(("Total", _count(t.calls, t.calls_unknown), usd(t.total_cents),
                 usd(t.by_purpose.get("live", Fraction(0))), usd(t.by_purpose.get("test", Fraction(0))),
                 usd(t.unconfirmed_cents) if t.unconfirmed_cents else "", ""))
    out += [""] + _table((unit, "Calls", "Spend", "Live", "Test", "Unconfirmed", "So far"), rows)
    out += [f"${ledger.money(t.total_cents)} / ${ledger.money(report.cap_cents)} development cap",
            "By category: " + ", ".join(f"{k} {usd(t.by_category.get(k, Fraction(0)))}" for k in CATEGORIES)]
    tokens = f"Tokens: {t.tokens_in:,} in, {t.tokens_out:,} out"
    out += ["", tokens + (f" (not known for {t.tokens_unknown} calls)" if t.tokens_unknown else "") + "."]
    if t.by_model:
        out += ["By model (confirmed): " + ", ".join(f"{m} {usd(c)}" for m, c in
                                                     sorted(t.by_model.items(), key=lambda kv: -kv[1]))]
    dev = [r for r in report.runs if not r.after_opening]
    if dev:
        out += ["", "Runs, newest first"]
        out += _table(("When", "Purpose", "Run", "Model", "Calls", "Cost"),
                      [(f"{r.first_at.astimezone(EASTERN):%d %b %H:%M}", r.purpose, r.label[:44] or "(no label)",
                        ", ".join(r.models), "?" if r.calls is None else r.calls,
                        usd(r.total_cents) + (" (unconfirmed)" if r.unconfirmed_cents else ""))
                       for r in dev[:15]])
        if len(dev) > 15:
            out.append(f"... and {len(dev) - 15} more (--json lists them all)")
    later = [r for r in report.runs if r.after_opening]
    if later:
        cost = sum((r.total_cents for r in later), Fraction(0))
        out += ["", f"Warning: {len(later)} test runs after the opening, {usd(cost)}. Testing should finish "
                "before the opening; these are not in any figure above."]
    if t.unconfirmed_cents:
        out += ["", "Unconfirmed: calls never corrected from their worst case. They may have cost less, or nothing."]
    return "\n".join(out)


# --- The page --------------------------------------------------------------------------

PAGE = Path(__file__).with_name("dev_spend.html")
# Inside <script>, a label's "</script>" would end the element early, and "<!--<script>" would stop the page
# parsing the rest of it. U+2028 and U+2029 end a line in older JavaScript. Outside a string, JSON has none of
# these characters, and inside one the \u escape stands for the same text.
SCRIPT_SAFE = {ord(c): f"\\u{ord(c):04x}" for c in "<>&  "}


def page(weeks: Report, days: Report) -> str:
    """The page with both reports inlined, so it opens from a file with no server and fetches nothing."""
    data = json.dumps({"weekly": to_json(weeks), "daily": to_json(days)}, separators=(",", ":"))
    return PAGE.read_text(encoding="utf-8").replace("/*DATA*/null", data.translate(SCRIPT_SAFE), 1)


def _source_id(*parts: str) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


def _amount(text: str) -> Decimal:
    amount = Decimal(text)
    if not amount.is_finite() or amount < 0:
        raise ValueError("Use a finite, nonnegative dollar amount.")
    return amount


def _input_tokens(row: dict) -> int | None:
    if not row.get("input_tokens"):
        return None
    return sum(int(row.get(key) or 0) for key in ("input_tokens", "cache_write_tokens", "cache_read_tokens"))


def _bench_rows(path: Path):
    """Read legacy sweep/bench answers. Their files hold no judge calls."""
    with path.open(encoding="utf-8-sig") as stream:
        for ordinal, line in enumerate(stream, 1):
            if not line.strip():
                continue
            row = json.loads(line, parse_float=Decimal)
            if row["model"] == "baseline" or _amount(row["cost_usd"]) == 0:
                continue
            yield {"id": str(ordinal), "at": "bench answers", "label": row["case"], "model": row["model"],
                   "status": "ok" if row["status"] in {"scored", "invalid"} else "error",
                   "cost_usd": row["cost_usd"], "input_tokens": row["input_tokens"],
                   "output_tokens": row["output_tokens"]}


def _export_rows(path: Path):
    if path.is_dir():
        path /= "model_calls.csv"
    if path.name == "results.jsonl":
        yield from _bench_rows(path)
        return
    with path.open(newline="", encoding="utf-8-sig") as stream:
        rows = csv.DictReader(stream)
        required = {"id", "at", "label", "model", "status", "cost_usd"}
        if not required.issubset(rows.fieldnames or ()):
            raise ValueError("Use a model_calls.csv export.")
        yield from rows


def _import_rows(path: Path, label: str, who: str, at: datetime, unconfirmed: bool):
    for row in _export_rows(path):
        status = row["status"]
        if status not in ("refused", "pending", "ok", "error"):
            raise ValueError("Invalid call status in CSV.")
        if status == "refused":
            continue
        cost = _amount(row["cost_usd"])
        ok = status == "ok" and row.get("cost_unconfirmed") != "true" and not unconfirmed
        # New exports carry harness run IDs. For older exports the supplied label
        # identifies the run, because scratch databases reuse local row IDs and dates.
        run = row.get("run_id") or label
        id_ = str(int(row["id"]))
        key = _source_id("call", run, id_, row["at"])
        model = row.get("answered_by") or row["model"]
        yield ((key, at, "simulations/bench", run, label, model, cost, ok,
                _input_tokens(row) if ok else None,
                int(row["output_tokens"]) if ok and row.get("output_tokens") else None, who),
               row.get("run_id"), int(id_), row["at"], row.get("spend_id"))


def export_calls(conn: psycopg.Connection, path: Path, *, bench_id: int | None = None) -> None:
    """Write spend fields only. Never export call detail or request contents."""
    where = "" if bench_id is None else " where label like %s"
    params = () if bench_id is None else (f"bench {bench_id} %",)
    rows = conn.execute(
        "select id, at, label, model, effort, status, stop_reason, input_tokens, cache_write_tokens,"
        " cache_read_tokens, output_tokens, max_tokens, worst_case_usd, cost_usd, seconds, run_id, answered_by,"
        " case when position('kept at worst case' in detail) > 0 then 'true' else 'false' end as cost_unconfirmed,"
        " to_jsonb(m)->>'spend_id' as spend_id"
        f" from model_calls m{where} order by id", params if where else None)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(column.name for column in rows.description)
        for row in rows:
            writer.writerow(value.astimezone(EASTERN).isoformat() if isinstance(value, datetime) else value for value in row)


def record_command(path: Path, label: str, who: str, at: datetime) -> str:
    return (f'python -m vending.dev_spend record --from "{path.resolve()}"'
            f' --label "{label}" --by "{who}" --at "{at.isoformat(timespec="seconds")}"')


def _record(argv: list[str], conn: psycopg.Connection | None, now: datetime | None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vending.dev_spend record")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--from", dest="path", type=Path, help="a simulation directory, model_calls.csv or bench results.jsonl")
    source.add_argument("--usd", help="a manual amount in dollars")
    parser.add_argument("--label", required=True, help="a unique run label or receipt label")
    parser.add_argument("--by", required=True, help="who recorded the spend")
    parser.add_argument("--at", help="actual spend time as ISO 8601 with an offset (default now)")
    parser.add_argument("--unconfirmed", action="store_true")
    args = parser.parse_args(argv)
    own = conn is None
    conn = conn or db.connect()
    try:
        at = datetime.fromisoformat(args.at) if args.at else now or ledger.db_now(conn)
        label, who = args.label.strip(), args.by.strip()
        if not label or not who or at.utcoffset() is None:
            raise ValueError("Use a label, a name and a spend time with a UTC offset.")
        if args.path:
            rows = list(_import_rows(args.path, label, who, at, args.unconfirmed))
        else:
            amount = _amount(args.usd)
            key = _source_id("manual", label, who)
            rows = [((key, at, "manual", key, label, "", amount, not args.unconfirmed, None, None, who),
                     None, None, None, None)]
        count = 0
        with conn.transaction():
            for values, run, id_, source_at, spend_id in rows:
                if spend_id and conn.execute("select 1 from model_spend where id = %s",
                                             (spend_id,)).fetchone():
                    continue
                if run and conn.execute(
                        "select 1 from model_calls where id = %s and run_id = %s and at = %s",
                        (id_, run, source_at)).fetchone():
                    continue
                count += conn.execute(
                    "insert into development_spend (source_id, at, category, run_id, label, model, cost_usd,"
                    " confirmed, tokens_in, tokens_out, recorded_by) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                    " on conflict (source_id) do nothing", values).rowcount
        if own:
            conn.commit()
    except (ValueError, ArithmeticError, OSError, csv.Error, KeyError, TypeError) as error:
        parser.error(str(error))
    finally:
        if own:
            conn.close()
    print(f"Recorded {count} new spend rows.")
    return 0


def main(argv: list[str] | None = None, conn: psycopg.Connection | None = None, now: datetime | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["record"]:
        return _record(argv[1:], conn, now)
    parser = argparse.ArgumentParser(prog="python -m vending.dev_spend", description=__doc__,
                                     epilog="Use record --help to import or enter spend.")
    parser.add_argument("--daily", action="store_true", help="one row a day instead of a week")
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    parser.add_argument("--html", type=Path, metavar="PATH", help="write the page, data inside it, to PATH")
    parser.add_argument("--demo", action="store_true", help="use the runs from the team's notes; no database")
    args = parser.parse_args(argv)
    if args.demo:
        now = now or datetime.now(timezone.utc)
        weeks, days = demo("week", now), demo("day", now)
    else:
        own = conn is None
        conn = conn or db.connect()
        try:
            conn.execute("set default_transaction_read_only = on")
            now = now or ledger.db_now(conn)     # the clock the ledger's entries are stamped by
            weeks, days = chart_reports(conn, now)
        finally:
            if own:
                conn.close()
    if args.html:
        args.html.write_text(page(weeks, days), encoding="utf-8")
        print(f"wrote {args.html} ({weeks.source})")
        return 0
    chosen = days if args.daily else weeks
    print(json.dumps(to_json(chosen), indent=1) if args.json else render(chosen))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
