"""Print a score table for one decider on a past week, or on the team's test cases.

    python -m vending.replay --week 2026-10-02 --decider baseline
    python -m vending.replay --week 2026-10-02 --unit-cost 0.80 --balance 600 --reasons
    python -m vending.replay --cases
    python -m vending.replay --case tuesday-sellout --reasons

Reads stored polls only, on a read-only connection; it never polls the machine.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction

import psycopg

from vending import db
from vending.pricing import to_cents
from vending.replay.cases import all_cases
from vending.replay.agent import AgentReplay, save_updates
from vending.replay.deciders import Baseline, Decider, get_decider
from vending.replay.model import DEFAULT_EFFORT, DEFAULT_KEY_ENV, DEFAULT_MODEL, ModelDecider, spend_log
from vending.replay.scoring import DEMAND_ASSUMPTIONS, PASS, SKIP, Score, score
from vending.replay.state import EASTERN, NoData, WeekState, replay_week


def run_week(conn: psycopg.Connection, day: date, decider: Decider, *,
             unit_cost_cents: int | None = None, balance_cents: int | None = None
             ) -> tuple[WeekState, WeekState | None, Score]:
    """Replay the week from `day`, ask `decider`, and score it against the week after."""
    state = replay_week(conn, day)
    try:
        next_week = replay_week(conn, day + timedelta(days=7))
    except NoData:
        next_week = None
    if unit_cost_cents is not None:
        state = replace(state, slots=tuple(replace(s, unit_cost_cents=unit_cost_cents) for s in state.slots))
    if balance_cents is not None:
        state = replace(state, balance_cents=balance_cents)
    measure = getattr(decider, "uses_database", True)
    before = _writes(conn) if measure else None
    answer = decider.decide(state)
    after = _writes(conn) if measure else None
    writes = None if before is None or after is None else after - before
    return state, next_week, score(state, answer, next_week, writes)


def _writes(conn: psycopg.Connection) -> int | None:
    """Write tool calls logged so far, or None if there's no action log."""
    if conn.execute("select to_regclass('action_log')").fetchone()[0] is None:
        return None
    return conn.execute("select count(*) from action_log where kind = 'write'").fetchone()[0]


# --- Printing ----------------------------------------------------------------

def money(cents: int | Fraction | None) -> str:
    if cents is None:
        return "?"
    whole = int(abs(Fraction(cents)) + Fraction(1, 2))
    return f"{'-' if cents < 0 else ''}${whole // 100}.{whole % 100:02d}"


def num(value) -> str:
    return "?" if value is None else str(value)


def _when(moment: datetime) -> str:
    return f"{moment.astimezone(EASTERN):%a %d %b %Y %H:%M}"


def render(title: str, state: WeekState, next_week: WeekState | None, result: Score,
           *, assumed: list[str] = (), reasons: bool = False) -> str:
    out = [title]
    if state.polls:
        days = (state.polls[1] - state.polls[0]).total_seconds() / 86400
        out.append(f"Polls: {_when(state.polls[0])} to {_when(state.polls[1])} Eastern ({days:.1f} days).")
    if next_week is not None and next_week.polls:
        days = (next_week.polls[1] - next_week.polls[0]).total_seconds() / 86400
        out.append(f"Scored against the following week's polls: {_when(next_week.polls[0])} to "
                   f"{_when(next_week.polls[1])} ({days:.1f} days).")
    elif next_week is not None:
        out.append(f"Scored against the following week: {_when(next_week.start)} to {_when(next_week.end)}.")
    out += [f"Assumed: {a}" for a in assumed]
    out += [f"Note: {n}" for n in (*state.notes, *result.notes)]
    if next_week is not None:
        out += [f"Note, following week: {n}" for n in next_week.notes if "receipts" not in n]
    out.append("")
    out.append("Hard checks")
    width = max(len(c.name) for c in result.checks)
    out += [f"  {c.status.upper():4}  {c.name:{width}}  {c.detail}" for c in result.checks]
    if result.plan is not None:
        out.append(f"\nPlan: {result.plan.visit} visit. {result.plan.reason}")
    if result.slots:
        out.append("")
        header = ("Slot", "Price", "Sold", "/day", "Plan", "Demand", "Sells", "Short", "Unsold", "Margin", "Notes")
        rows = []
        for o in result.slots:
            s, sp = state.slot(o.slot), result.plan.slot(o.slot)
            planned = "keep" if sp.keep else f"{sp.fill_to} @ {money(sp.price_cents)}"
            if sp.product and not sp.keep:
                planned += f" {sp.product}"
            rows.append((o.slot, money(s.price_cents), num(s.units_sold), num(s.units_per_day), planned,
                         num(o.demand), num(o.sold), num(o.stockout), num(o.unsold),
                         money(o.margin_cents), "; ".join(o.notes)))
        widths = [max(len(str(r[i])) for r in (header, *rows)) for i in range(len(header))]
        line = lambda r: "  ".join(str(v).ljust(w) for v, w in zip(r, widths)).rstrip()
        out += [line(header), *(line(r) for r in rows)]
        t = result.totals
        margin = money(t.margin_cents) if t.margin_cents is not None else "? (needs every scored slot's unit cost; try --unit-cost)"
        out.append("")
        out.append(f"Scored {t.scored} of {len(result.slots)} slots. Short {t.stockout_units} units in "
                   f"{t.stockout_slots} slots. Unsold {t.unsold_units} units. Revenue ex tax "
                   f"{money(t.revenue_ex_tax_cents)}. Margin {margin}"
                   + (f", after the {money(t.fee_cents)} midweek fee." if t.fee_cents else "."))
        out.append("Price, Sold and /day are last week's. Plan is units on the rail after the visit @ price. "
                   "Demand is what actually sold the following week; Sells is what the plan would have sold; "
                   "Short is demand the rail couldn't meet. ? = unknown.")
    if reasons and result.plan is not None:
        out.append("\nReasons")
        out += [f"  {sp.slot}: {sp.reason}" for sp in result.plan.slots]
    out.append("\nDemand assumptions")
    out += [f"  - {a}" for a in DEMAND_ASSUMPTIONS]
    return "\n".join(out)


def render_cases(decider: Decider) -> str:
    header = ("Case", "Checks", "Scored", "Short", "Unsold", "Margin", "Failed")
    rows = []
    for case in all_cases():
        try:
            answer = decider.decide(case.state)
        except ValueError as error:   # a model run refused by the spending cap, or a failed call
            rows.append((case.name, "not scored", "", "", "", "", str(error)))
            continue
        result = score(case.state, answer, case.next_week)
        counts = {status: sum(c.status == status for c in result.checks) for status in ("pass", "fail", "skip")}
        t = result.totals
        rows.append((case.name, " ".join(f"{n} {k}" for k, n in counts.items() if n),
                     num(t and t.scored), num(t and t.stockout_units), num(t and t.unsold_units),
                     money(t.margin_cents) if t else "?",
                     ", ".join(c.name for c in result.checks if c.status not in (PASS, SKIP))))
    widths = [max(len(str(r[i])) for r in (header, *rows)) for i in range(len(header))]
    line = lambda r: "  ".join(str(v).ljust(w) for v, w in zip(r, widths)).rstrip()
    return "\n".join([f"Test cases, decider: {decider.name}", "", line(header), *(line(r) for r in rows), "",
                      "Each case's question and what a good answer looks like: --case NAME.", *spend(decider)])


def render_agent(replay: AgentReplay, weeks: list, updates_file) -> str:
    """One row per week: the agent's score beside the baseline's on the same week."""
    header = ("Week", "Checks", "Scored", "Short", "Unsold", "Margin", "Baseline margin", "Failed", "Tools", "Cost")
    rows = []
    for w in weeks:
        if w.state is None:
            rows.append((f"{w.day:%d %b}", "not run", "", "", "", "", "", w.error, "", ""))
            continue
        r, t = w.result, w.result.totals if w.result else None
        base = score(w.state, Baseline().decide(w.state), w.next_week).totals
        checks = r.checks if r else ()
        counts = {k: sum(c.status == k for c in checks) for k in ("pass", "fail", "skip")}
        rows.append((f"{w.day:%d %b}", " ".join(f"{n} {k}" for k, n in counts.items() if n) or "not scored",
                     num(t and t.scored), num(t and t.stockout_units), num(t and t.unsold_units),
                     money(t.margin_cents) if t else "?", money(base.margin_cents) if base else "?",
                     ", ".join(c.name for c in checks if c.status == "fail"),
                     str(len(w.tool_calls)), f"${w.cost_usd:.4f}"))
    widths = [max(len(str(row[i])) for row in (header, *rows)) for i in range(len(header))]
    line = lambda row: "  ".join(str(v).ljust(w) for v, w in zip(row, widths)).rstrip()
    out = [f"Agent replay, {replay.name}, {len(weeks)} week{'' if len(weeks) == 1 else 's'}", "",
           line(header), *(line(row) for row in rows), ""]
    out += [f"Stopped in the week of {w.day:%d %b}: {w.error}" for w in weeks if w.error and w.state is not None]
    out.append(f"Weekly updates and notes, for people to grade: {updates_file}")
    out.append("Margins use the same demand assumptions as every score table (docs/assumptions.md).")
    return "\n".join([*out, *spend(replay)])


def spend(decider: Decider) -> list[str]:
    """What a model decider's runs cost, for the bottom of the table."""
    runs = getattr(decider, "runs", None)
    if runs is None:
        return []
    total = sum((Decimal(r["cost_usd"]) for r in runs), Decimal(0))
    lines = [f"\nModel cost: ${total:.4f} for {len(runs)} request{'' if len(runs) == 1 else 's'} "
             f"({decider.key_env} paid; caps ${decider.run_cap} a {getattr(decider, 'cap_unit', 'run')}, "
             f"${decider.daily_cap} a day; "
             f"logged in {spend_log()}). A failed request counts at its worst case unless the API rejected it."]
    for r in runs:
        tokens = (f", {r['input_tokens']} tokens in, {r['output_tokens']} out"
                  if "input_tokens" in r else "")
        lines.append(f"  {r['run_id'][:8]}: ${Decimal(r['cost_usd']):.4f}{tokens}, {r['status']}")
    return lines


def render_case(name: str, decider: Decider, reasons: bool) -> str:
    case = next((c for c in all_cases() if c.name == name), None)
    if case is None:
        raise ValueError(f"No case {name!r}. Cases: {', '.join(c.name for c in all_cases())}.")
    result = score(case.state, decider.decide(case.state), case.next_week)
    title = (f"Case {case.name} ({case.source}), decider: {decider.name}\n"
             f"Question: {case.question}\nA good answer: {case.good_answer}\n"
             "The numbers in this case are made up to pose the question.")
    return "\n".join([render(title, case.state, case.next_week, result, reasons=reasons), *spend(decider)])


def main(argv: list[str], conn: psycopg.Connection | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vending.replay", description=__doc__.splitlines()[0])
    what = parser.add_mutually_exclusive_group(required=True)
    what.add_argument("--week", type=date.fromisoformat, help="first day of the week, such as 2026-10-02 (a Friday)")
    what.add_argument("--cases", action="store_true", help="score the team's test cases")
    what.add_argument("--case", help="one test case in full")
    parser.add_argument("--decider", default="baseline",
                        help="baseline (default), model (costs money; capped), or file:PATH for a JSON plan")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"with --decider model (default {DEFAULT_MODEL})")
    parser.add_argument("--effort", default=DEFAULT_EFFORT, help=f"with --decider model (default {DEFAULT_EFFORT})")
    parser.add_argument("--key-env", default=DEFAULT_KEY_ENV,
                        help=f"variable holding the API key (default {DEFAULT_KEY_ENV})")
    parser.add_argument("--unit-cost", help="assume every unit cost this much, such as 0.80 (replayed weeks only)")
    parser.add_argument("--balance", help="assume this spendable balance, such as 600 (replayed weeks only)")
    parser.add_argument("--reasons", action="store_true", help="print the plan's reason for each slot")
    parser.add_argument("--agent", type=int, metavar="WEEKS",
                        help="run the agent (costs money; capped) for this many weeks from --week, "
                             "with tools, notes and weekly updates")
    args = parser.parse_args(argv)
    try:
        if args.agent is not None:
            if args.week is None:
                raise ValueError("--agent needs --week: the first week to run.")
            if args.week.weekday() != 4:
                raise ValueError("With --agent, --week must be a Friday: Claude is told the restock is that Friday.")
            if args.decider != "baseline" or args.reasons:
                raise ValueError("--agent is its own decider; leave out --decider and --reasons.")
        decider = None if args.agent is not None else (ModelDecider(args.model, args.effort, args.key_env) if args.decider == "model"
                   else get_decider(args.decider))
        if args.cases:
            print(render_cases(decider))
            return 0
        if args.case:
            print(render_case(args.case, decider, args.reasons))
            return 0
        unit_cost = None if args.unit_cost is None else to_cents(args.unit_cost)
        balance = None if args.balance is None else to_cents(args.balance)
        if (unit_cost is not None and unit_cost <= 0) or (balance is not None and balance < 0):
            raise ValueError("--unit-cost must be above zero and --balance can't be negative.")
        if args.agent is not None:
            if not 1 <= args.agent <= 12:
                raise ValueError("--agent takes 1 to 12 weeks.")
            replay = AgentReplay(args.model, args.effort, args.key_env)
    except (ValueError, ArithmeticError) as error:
        print(error, file=sys.stderr)
        return 2
    own = conn is None
    conn = conn or db.connect()
    try:
        conn.execute("set default_transaction_read_only = on")
        if args.agent is not None:
            weeks = replay.run(conn, args.week, args.agent, unit_cost_cents=unit_cost, balance_cents=balance)
            print(render_agent(replay, weeks, save_updates(weeks, replay.name)))
            return 0
        state, next_week, result = run_week(conn, args.week, decider,
                                            unit_cost_cents=unit_cost, balance_cents=balance)
    except (NoData, ValueError) as error:
        print(error, file=sys.stderr)
        return 1
    finally:
        if own:
            conn.close()
    assumed = []
    if unit_cost is not None:
        assumed.append(f"every unit cost {money(unit_cost)} (--unit-cost), not a receipt figure.")
    if balance is not None:
        assumed.append(f"a spendable balance of {money(balance)} (--balance), not the ledger.")
    title = f"Week of {_when(state.start)[:15]} (Eastern), decider: {decider.name}"
    print("\n".join([render(title, state, next_week, result, assumed=assumed, reasons=args.reasons),
                     *spend(decider)]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
