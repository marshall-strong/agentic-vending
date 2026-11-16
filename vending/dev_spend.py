"""What building and testing the agent has cost on our Anthropic keys, week by week, until the competition starts.

    python -m vending.dev_spend                          # one row a week, Saturday to Friday, Eastern
    python -m vending.dev_spend --daily                  # one row a day
    python -m vending.dev_spend --json                   # the same, for a page or a script
    python -m vending.dev_spend --html dev-spend.html    # one page with the data inside it, no server
    python -m vending.dev_spend --demo --html demo.html  # from the team's run notes, no database

**This is for the team and the teachers.** No agent tool reads it today. The
agent may know what testing cost, but none of it comes out of the $600, so its
competition figures (the balance, `get_pnl`) count from the opening on.

**Development ends at the ledger's opening entry** (`python -m vending.ledger
open`). A model call before it, live or test, is development spend and doesn't
come out of the $600 (`docs/opening-capital-plan.md`, #104). The opening is
recorded before the day-one fill is fulfilled, about 30 Nov, not on 3 Dec
(`docs/planning/2026-11-12-david-answers.md`). Until it exists, every call so
far is development spend.

Where the figures come from, read only:

- `model_calls`: every request the harness has sent, live or test. A row the
  harness corrected (`ok`) counts at what the API billed. A row left `pending` or
  `error` was never corrected, so it counts at its worst case, and is shown
  apart as **unconfirmed**: it may not have billed at all. So is an `ok` row the
  harness "kept at worst case" (answered by an unpriced model or capacity, or no
  usage reported): its real cost isn't known either. A `refused` row cost
  nothing and is left out.
- `ledger_entries`: a `tokens` entry with no `model_calls` row, written by code
  that predates that table, or by a person (`python -m vending.ledger tokens`).
  Its reference says what it was: the purpose ("live" or "test"), then the run's
  label, then the message id once corrected, or "pending" / "estimate: ..." when
  it never was (unconfirmed, as is one "kept at worst case"). Its detail names the model ("model: N tokens in,
  M out"). An entry whose reference names no purpose is counted as live, since
  only live calls and people's own records reach the ledger.

Tokens are counted only where they are known: an unconfirmed call's tokens are
not, and neither are a run note's. Each figure says how many calls it lacks.

A run is one `Harness` (its `run_id`): an agent run, one sweep config, one
bench. A ledger entry has no run id, so its run is its label (without "turn N")
on one Eastern day.

After the opening, live calls are the competition's money and are not here.
**There should be no test calls after the opening:** testing finishes before it
(Kevin, 15 Nov). If one is recorded anyway, it is kept out of every development
figure and reported on its own as `after_opening_test`, and the page warns about
it, so a stray dry run or sweep in December is seen rather than hidden.

Money is exact (`Fraction` cents) until it is shown, to four decimal places,
since a call can cost a tenth of a cent. `--json` and the page's data give every
amount as an exact decimal string ("0.1234499"), so it is rounded only once,
where it is shown. The demo data is the runs the team
wrote down (`docs/agent.md`, `docs/sample-directive.md`, `docs/benchmark.md`,
`docs/guardrail-evidence.md`) and says so on every view: it is not the ledger.
"""

from __future__ import annotations

import argparse
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

from vending import db, ledger

EASTERN = ZoneInfo(ledger.TIMEZONE)
PURPOSES = ("live", "test")         # the harness's purposes
DETAIL = re.compile(r"^(?P<model>\S+): (?P<tokens_in>\d+) tokens in, (?P<tokens_out>\d+) out\b")
# What the harness appends to a reference: " pending" or " estimate: <why>" while the cost is still the worst case,
# " refused: <why>" for a request the API turned down, or " <message id>[ (note)]" once corrected.
UNSETTLED = re.compile(r" (pending|estimate: .*)$")
SETTLED = re.compile(r"(^| )(msg_\S+( \(.*\))?|refused: .*)$")
TURN = re.compile(r" turn \d+$")
DEMO_SOURCE = "from run notes, not the ledger"
DB_SOURCE = "model_calls and the ledger"


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
        if not call.confirmed:
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


def calls(conn: psycopg.Connection) -> list[Call]:
    """Every model call either table records, each once, oldest first."""
    out: list[Call] = []
    has_calls = _has(conn, "model_calls")
    if has_calls:
        rows = conn.execute(
            "select at, purpose, label, run_id, coalesce(answered_by, model), status, cost_usd,"
            " input_tokens + coalesce(cache_write_tokens, 0) + coalesce(cache_read_tokens, 0), output_tokens, detail"
            " from model_calls where status <> 'refused' order by at, id").fetchall()
        for at, purpose, label, run_id, model, status, cost, tokens_in, tokens_out, detail in rows:
            ok = status == "ok" and "kept at worst case" not in detail
            out.append(Call(at, purpose, model, TURN.sub("", label.strip()), f"run {run_id}", Fraction(cost) * 100, ok,
                            tokens_in if ok else None, tokens_out if ok else None))
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
    out.sort(key=lambda c: c.at)
    return out


def opening_at(conn: psycopg.Connection) -> datetime | None:
    if not _has(conn, "ledger_entries"):
        return None
    opened = ledger.opening(conn)
    return opened.at if opened else None


# --- Summing (pure) ------------------------------------------------------------------

def _start(moment: datetime, unit: str) -> date:
    local = moment.astimezone(EASTERN)
    return ledger.week_of(local) if unit == "week" else local.date()


def summarise(recorded: list[Call], opened: datetime | None, unit: str, as_of: datetime,
              source: str = DB_SOURCE) -> Report:
    """Book each call to its Eastern day or week: development before `opened`, test calls after it apart.

    Quiet days and weeks are included, from the first call to the opening (or to
    `as_of` while the ledger isn't open), and on to the last test call after it.
    """
    if unit not in ("week", "day"):
        raise ValueError(f"unit must be week or day, not {unit!r}")
    step = timedelta(days=7 if unit == "week" else 1)
    kept = sorted((c for c in recorded if opened is None or c.at < opened or c.purpose == "test"), key=lambda c: c.at)
    if not kept:
        buckets = [Bucket(_start(opened, unit))] if opened else []
        return Report(unit, as_of, opened, buckets, [], Bucket(_start(as_of, unit)), source)
    end = _start(opened if opened else as_of, unit)
    end = max(end, *(_start(c.at, unit) for c in kept))
    buckets: dict[date, Bucket] = {}
    day = min(_start(kept[0].at, unit), _start(opened, unit)) if opened else _start(kept[0].at, unit)
    while day <= end:
        buckets[day] = Bucket(day)
        day += step
    totals = Bucket(next(iter(buckets)))
    grouped: dict[tuple[str, bool], list[Call]] = defaultdict(list)
    for c in kept:
        bucket = buckets[_start(c.at, unit)]
        if opened is None or c.at < opened:
            bucket.add(c)
            totals.add(c)
        else:
            bucket.after_opening_test_cents += c.cost_cents
            bucket.after_opening_test_calls += c.count or 0
            if not c.confirmed:
                bucket.after_opening_test_unconfirmed_cents += c.cost_cents
        grouped[(c.run, opened is not None and c.at >= opened)].append(c)
    running = Fraction(0)
    for bucket in buckets.values():
        running += bucket.total_cents
        bucket.cumulative_cents = running
    runs = [_run(key[0], group, opened) for key, group in grouped.items()]
    runs.sort(key=lambda r: r.first_at, reverse=True)
    return Report(unit, as_of, opened, list(buckets.values()), runs, totals, source)


def _run(key: str, group: list[Call], opened: datetime | None) -> Run:
    models = Counter(c.model for c in group)
    known = all(c.tokens_in is not None and c.tokens_out is not None for c in group)
    return Run(
        key, group[0].at, group[0].purpose, group[0].label, [m for m, _ in models.most_common()],
        None if any(c.count is None for c in group) else sum(c.count for c in group),
        sum(c.tokens_in for c in group) if known else None, sum(c.tokens_out for c in group) if known else None,
        sum((c.cost_cents for c in group if c.confirmed), Fraction(0)),
        sum((c.cost_cents for c in group if not c.confirmed), Fraction(0)),
        opened is not None and group[0].at >= opened)


def weekly(conn: psycopg.Connection, now: datetime | None = None) -> Report:
    """Development spend per Eastern week, Saturday to Friday (the ledger's week)."""
    return summarise(calls(conn), opening_at(conn), "week", now or ledger.db_now(conn))


def daily(conn: psycopg.Connection, now: datetime | None = None) -> Report:
    """Development spend per Eastern calendar day."""
    return summarise(calls(conn), opening_at(conn), "day", now or ledger.db_now(conn))


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
                        f"sweep 20261111-173503 {config}", f"sweep {config}", cents(per_case) * 10, True, count=10))
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
        "as_of": report.as_of.astimezone(EASTERN).isoformat(timespec="seconds"),
        "opening_at": report.opening_at.astimezone(EASTERN).isoformat(timespec="seconds") if report.opening_at else None,
        "totals": _bucket_json(t) | {"runs": sum(1 for r in report.runs if not r.after_opening)},
        "buckets": [_bucket_json(b) for b in report.buckets],
        "runs": [{"first_at": r.first_at.astimezone(EASTERN).isoformat(timespec="seconds"), "purpose": r.purpose,
                  "label": r.label, "models": r.models, "calls": r.calls, "tokens_in": r.tokens_in,
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
        return "\n".join(out + ["", "No model calls recorded yet."])
    rows = [(f"{b.start:%a %d %b}", _count(b.calls, b.calls_unknown), usd(b.total_cents),
             usd(b.by_purpose.get("live", Fraction(0))), usd(b.by_purpose.get("test", Fraction(0))),
             usd(b.unconfirmed_cents) if b.unconfirmed_cents else "", usd(b.cumulative_cents))
            for b in report.buckets if report.opening_at is None or b.start <= _start(report.opening_at, report.unit)]
    t = report.totals
    rows.append(("Total", _count(t.calls, t.calls_unknown), usd(t.total_cents),
                 usd(t.by_purpose.get("live", Fraction(0))), usd(t.by_purpose.get("test", Fraction(0))),
                 usd(t.unconfirmed_cents) if t.unconfirmed_cents else "", ""))
    out += [""] + _table((unit, "Calls", "Spend", "Live", "Test", "Unconfirmed", "So far"), rows)
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


def main(argv: list[str] | None = None, conn: psycopg.Connection | None = None, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vending.dev_spend", description=__doc__.split("\n")[0])
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
            weeks, days = weekly(conn, now), daily(conn, now)
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
