"""Turn the picked first-fill plan into the day-one purchase request (#214).

    python -m vending.fill                                     # preview: lines, cost, hash; sends nothing
    python -m vending.fill --submit --hash HASH --by NAME      # send it, if it still hashes to HASH

The team picks one plan in `first_fill_plans` (`python -m vending.plan_store pick`).
This command reads that plan, turns each of its slots into the agent's desired
state (product, sale price, fill to the plan's units), and runs it through the
agent's own path: `planning.plan` and the planner for the preview, and
`submit_desired_state` inside the write gate for the submit. So the request is
planned, hashed, recorded in `purchase_submissions` and `purchase_requests`, and
read back by `python -m vending.sync_requests`, exactly as the agent's would be.

The plan's own pack prices are a model's estimates, so they are never sent.
Every product is bought the way the team's sourcing table says, and a product
whose table row has no checked price is refused (#202).

On top of the planner's gates, the command refuses, with every reason at once
and nothing sent:

- `plan_assignment`: the plan was picked for another slot assignment than ours now;
- `no_checked_price`: a product with no sourcing row, or one with no checked price;
- `test_opening`: the ledger's opening is from before the fall cut, the test opening of 11 Nov (#215);
- `fill_already_sent`: a request since the opening was accepted and not rejected, so the fill has gone.

The submit needs `--hash`, the hash the preview printed, and refuses
(`plan_changed`) when the plan now hashes to anything else: a changed plan,
price, pack or balance can't slip through. Who submitted it, and the plan's ID,
go in `action_log` (tool `fill.submit`) with the submit's reply.

The planner's store cutoffs time purchases for a Friday visit. The fill is
stocked on Mon 30 Nov (David, 20 Nov), so the command also warns about each
line whose store's cutoff for that day has passed: an Amazon order placed after
it arrives late and is stocked at the next scheduled visit (David, 17 Nov).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import set_json_loads

from vending import db, ledger, plan_rounds, planner, planning, settings, slots, sourcing, start, submissions
from vending.bridge import Bridge
from vending.pricing import Refusal
from vending.tools import desired_state as schema
from vending.tools import submit as submit_tool
from vending.write_gate import Refused, run_write

TOOL = "fill.submit"
EASTERN = ZoneInfo("America/New_York")
# Staff buy and stock the fill on Mon 30 Nov (David, 20 Nov: planning/2026-11-20-david-update.md).
DAY_ONE_VISIT = date(2026, 11, 30)
FRIDAY = 4
# Codes this command adds to the planner's.
PLAN_ASSIGNMENT = "plan_assignment"
NO_CHECKED_PRICE = "no_checked_price"
TEST_OPENING = "test_opening"
ALREADY_SENT = "fill_already_sent"


class NoPick(LookupError):
    """No plan is picked, so there is nothing to submit."""


@dataclass(frozen=True)
class Picked:
    id: int
    label: str
    loaded_at: datetime
    loaded_by: str
    slots: tuple[str, ...]          # the assignment it was loaded for
    round: plan_rounds.Round | None  # None when it was loaded for another assignment


@dataclass(frozen=True)
class Preview:
    picked: Picked
    plan: planner.Plan | None       # None when the planner could not run
    gathered: planning.Gathered | None
    refusals: tuple[Refusal, ...]   # the planner's and this command's, all at once
    warnings: tuple[str, ...]
    blocked: str | None = None      # what a person must do before anything can be planned

    @property
    def ok(self) -> bool:
        return self.plan is not None and not self.refusals and self.blocked is None


def picked(conn: psycopg.Connection, ours: tuple[str, ...]) -> Picked:
    """The picked plan, checked against `ours` when it was loaded for the same assignment."""
    with conn.cursor() as cur:
        set_json_loads(json.loads, context=cur)   # the plan loader's decoder, as the dashboard reads it
        row = cur.execute("select id, label, loaded_at, loaded_by, plan, projection, slots from first_fill_plans"
                          " where picked").fetchone()
    if row is None:
        raise NoPick("No first-fill plan is picked; pick one with python -m vending.plan_store pick ID --by NAME.")
    plan_id, label, loaded_at, loaded_by, plan, projection, stored = row
    same = set(stored) == set(ours)
    checked = plan_rounds.parse_round(label, plan, projection, ours, saved_at=loaded_at) if same else None
    return Picked(plan_id, label, loaded_at, loaded_by, tuple(stored), checked)


def desired_state(checked: plan_rounds.Round) -> schema.DesiredState:
    """Each of the plan's slots as the agent would ask for it. No sourcing: the team's table is used."""
    return schema.DesiredState(slots=[
        schema.DesiredSlot(slot=s.slot, product=s.sku, sale_price=_price(s.price_cents), fill_to=s.units)
        for s in checked.slots.values()])


def checks(conn: psycopg.Connection, pick: Picked, ours: tuple[str, ...]) -> list[Refusal]:
    """This command's own refusals. The planner's come from `planning.plan`."""
    refusals = []
    if pick.round is None:
        refusals.append(Refusal(PLAN_ASSIGNMENT, (
            f"plan #{pick.id} (round {pick.label}) was loaded for another slot assignment than ours now; load and "
            "pick a plan for the current assignment (python -m vending.plan_store load DIR --round N --by NAME)")))
    else:
        refusals.extend(_unpriced(conn, pick.round))
    opened = ledger.opening(conn)
    if opened is not None and opened.at < start.fall_cut():
        refusals.append(Refusal(TEST_OPENING, (
            f"the ledger's opening is dated {opened.at.astimezone(EASTERN):%Y-%m-%d}, before the fall cut, so it is "
            "the test opening, not the real one (#215); reset it and record the real opening first")))
    if opened is not None:
        sent = conn.execute(
            "select request_id, status from purchase_requests where submitted_at >= %s and status <> 'rejected'"
            " order by submitted_at", (opened.at,)).fetchall()
        if sent:
            named = ", ".join(f"{rid} ({status})" for rid, status in sent)
            refusals.append(Refusal(ALREADY_SENT, (
                f"request {named} was sent since the ledger's opening and wasn't rejected, so the day-one fill has "
                "gone; it can be sent again only if the bridge rejects it")))
    # The agent's submit refuses this too; here it also stops a preview from looking clean.
    if unresolved := submissions.unresolved(conn):
        refusals.append(unresolved)
    return refusals


def _unpriced(conn: psycopg.Connection, checked: plan_rounds.Round) -> list[Refusal]:
    """A refusal per slot whose product has no sourcing row, no store, or no checked price (#202)."""
    skus = sorted({s.sku for s in checked.slots.values() if s.sku})
    primary = {o.sku: o for o in sourcing.options_for(conn, skus) if o.rank == "primary"}
    found = []
    for s in checked.slots.values():
        o = primary.get(s.sku) if s.sku else None
        if s.sku is None:
            why = "the plan gives no SKU for it"
        elif o is None:
            why = f"{s.sku} has no row in the sourcing table, so no store or checked price"
        elif not o.store or o.expected_cents is None:
            why = f"{s.sku}'s sourcing row has no checked price"
        else:
            continue
        found.append(Refusal(NO_CHECKED_PRICE, (
            f"slot {s.slot}: {why}; look it up and record it (python -m vending.sourcing set "
            f"{s.sku or 'SKU'} --store ... --pack N --price P --by NAME; #202)"), s.slot))
    return found


def day_one_warnings(plan: planner.Plan, now: datetime, visit: date = DAY_ONE_VISIT,
                     cutoffs=None) -> list[str]:
    """A warning for each store whose cutoff for `visit` has passed, with the lines that buy from it.

    The planner's cutoffs are for a Friday visit (in-store Thursday 23:59, Amazon Wednesday 23:59). The
    same lead time before `visit` is its cutoff: for Mon 30 Nov, in-store Sun 29 Nov and Amazon Sat 28 Nov.
    """
    cutoffs = cutoffs or planner.Config(slots=()).cutoffs
    late: dict[tuple[str, datetime], list[str]] = {}
    for line in plan.lines:
        if line.purchase is None:
            continue
        for option in (line.purchase.primary, line.purchase.alternate):
            cutoff = cutoffs.get(option.store) if option else None
            if cutoff is None:
                continue
            day = visit - timedelta(days=(FRIDAY - cutoff.weekday) % 7)
            deadline = datetime.combine(day, cutoff.at, EASTERN) + timedelta(seconds=59)
            if now > deadline:
                late.setdefault((option.store, deadline), []).append(line.slot)
    return [f"past the {store} cutoff for the {visit:%a %d %b} stocking ({deadline:%a %d %b %H:%M} ET) for slots "
            f"{', '.join(sorted(set(names)))}; those lines will likely miss it and be stocked at the next scheduled "
            "visit"
            for (store, deadline), names in sorted(late.items())]


def preview(conn: psycopg.Connection, bridge: Bridge, now: datetime | None = None,
            visit: date = DAY_ONE_VISIT) -> Preview:
    """The picked plan planned on live data, as `preview_desired_state` would, plus this command's checks."""
    ours = slots.our_slots(conn)
    pick = picked(conn, ours)
    own = checks(conn, pick, ours)
    if pick.round is None:
        return Preview(pick, None, None, tuple(own), ())
    try:
        plan, gathered = planning.plan(conn, desired_state(pick.round), bridge, now)
    except planning.NotReady as blocked:
        return Preview(pick, None, None, tuple(own), (), blocked=str(blocked))
    return Preview(pick, plan, gathered, _merge(own, plan.refusals),
                   (*plan.warnings, *day_one_warnings(plan, gathered.now, visit), *_plan_warnings(pick.round)))


def submit(conn: psycopg.Connection, bridge: Callable[[], Bridge], connect: Callable[[], psycopg.Connection],
           by: str, expected_plan_hash: str, now: datetime | None = None) -> submit_tool.SubmitReply:
    """Send the picked plan through the agent's submit, inside the write gate. Raises `Refused` with every reason.

    The checks run again inside the gate's transaction, holding the halt lock, so nothing can change between
    them and the send. With any refusal, the planner still runs so its refusals are reported too, but nothing
    is sent.
    """
    if not by.strip():
        raise ValueError("--by needs your name")
    if not expected_plan_hash.strip():
        raise ValueError("--hash needs the plan hash the preview printed")
    arguments = {"by": by.strip(), "expected_plan_hash": expected_plan_hash.strip()}

    def action(conn: psycopg.Connection) -> submit_tool.SubmitReply:
        ours = slots.our_slots(conn)
        pick = picked(conn, ours)
        arguments.update(plan_id=pick.id, label=pick.label)
        own = checks(conn, pick, ours)
        if pick.round is None:
            raise Refused(own)
        desired = desired_state(pick.round)
        if own:
            try:
                plan, _ = planning.plan(conn, desired, bridge(), now)
            except planning.NotReady:
                raise Refused(own) from None
            raise Refused(list(_merge(own, plan.refusals)))
        return submit_tool.submit_desired_state(desired, bridge, connect, now, expected_plan_hash.strip())(conn)

    # The gate logs `arguments` after the action has filled in the plan's ID.
    return run_write(conn, TOOL, arguments, action)


def _merge(own: list[Refusal], planned: tuple[Refusal, ...]) -> tuple[Refusal, ...]:
    """Ours first, then the planner's, without its `no_sourcing` for a slot ours already names."""
    named = {r.slot for r in own if r.code == NO_CHECKED_PRICE}
    return (*own, *(r for r in planned if not (r.code == "no_sourcing" and r.slot in named)))


def _plan_warnings(checked: plan_rounds.Round) -> list[str]:
    missing = [s for s in checked.ours if s not in checked.slots]
    return [f"the plan has no product for slot{'s' if len(missing) > 1 else ''} {', '.join(missing)}"] if missing else []


def _price(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


# --- Output --------------------------------------------------------------------------------

def render(p: Preview) -> str:
    """The preview as a person reads it before submitting."""
    pick = p.picked
    out = [f"First-fill plan #{pick.id}, round {pick.label}, loaded {pick.loaded_at:%Y-%m-%d %H:%M} UTC "
           f"by {pick.loaded_by}."]
    if p.blocked:
        out.append(f"Can't plan yet: {p.blocked}")
    if p.plan is not None:
        out += ["", _table(p.plan)]
        cost = p.plan.cost
        if cost is not None:
            out += ["", f"Total: expected {_usd(cost.expected_cents)}, at most {_usd(cost.max_cents)}"
                        + (f" (visit fee {_usd(cost.visit_fee_cents)} included)" if cost.visit_fee_cents else "")
                        + f". Spendable {_usd(cost.balance_cents)}; {_usd(cost.balance_left_at_max_cents)} "
                          "left at most."]
    for title, items in (("Warnings", p.warnings), ("Notes", p.gathered.notes if p.gathered else ())):
        if items:
            out += ["", f"{title}:", *(f"- {w}" for w in items)]
    if p.refusals:
        out += ["", f"REFUSED ({len(p.refusals)}): nothing can be sent until each is fixed.",
                *(f"- {r.code}: {r.message}" for r in p.refusals)]
    if p.ok:
        out += ["", f"Plan hash: {p.plan.plan_hash}",
                f"To send it: python -m vending.fill --submit --hash {p.plan.plan_hash} --by NAME"]
    else:
        out += ["", "Plan hash: none (refused plans have no hash)"]
    return "\n".join(out)


def _table(plan: planner.Plan) -> str:
    header = ("Slot", "Product", "Load", "Store", "Packs", "Unit cost (exp / max)", "Price", "Alternate")
    rows = [header]
    for ln in plan.lines:
        if ln.purchase is None:
            rows.append((ln.slot, ln.product, str(ln.units_to_load), "closet", "-", "-",
                         _usd(ln.sale_price_cents), "-"))
            continue
        o, alt = ln.purchase.primary, ln.purchase.alternate
        rows.append((ln.slot, ln.product, str(ln.units_to_load), o.store, f"{o.packs} x {o.units_per_pack}",
                     f"{_unit(o.expected_price_per_pack_cents, o.units_per_pack)} / "
                     f"{_unit(o.max_price_per_pack_cents, o.units_per_pack)}",
                     _usd(ln.sale_price_cents),
                     f"{alt.store} {alt.packs} x {alt.units_per_pack}" if alt else "-"))
    widths = [max(len(r[i]) for r in rows) for i in range(len(header))]
    return "\n".join("  ".join(cell.ljust(w) for cell, w in zip(r, widths)).rstrip() for r in rows)


def _unit(pack_cents: int, units: int) -> str:
    return f"{pack_cents / units / 100:.2f}"


def _usd(cents: int) -> str:
    return plan_rounds.format_cents(cents)


def main(argv: list[str] | None = None, *, bridge: Callable[[], Bridge] = Bridge,
         connect: Callable[[], psycopg.Connection] = db.connect, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vending.fill", description=__doc__.split("\n")[0])
    parser.add_argument("--submit", action="store_true", help="send the request; without it, preview only")
    parser.add_argument("--hash", help="the plan hash the preview printed (required with --submit)")
    parser.add_argument("--by", help="your name, for the log (required with --submit)")
    parser.add_argument("--visit", type=date.fromisoformat, default=DAY_ONE_VISIT,
                        help=f"the stocking day, for the cutoff warnings (default {DAY_ONE_VISIT})")
    args = parser.parse_args(argv)
    if args.submit and not (args.hash and args.hash.strip()):
        parser.error("--submit needs --hash HASH, from the preview")
    if args.submit and not (args.by and args.by.strip()):
        parser.error("--submit needs --by NAME")
    try:
        with connect() as conn:
            if not args.submit:
                print(render(preview(conn, bridge(), now, args.visit)))
                return 0
            try:
                reply = submit(conn, bridge, connect, args.by, args.hash, now)
            except Refused as refused:
                print(f"Refused; nothing was sent ({len(refused.refusals)}):", file=sys.stderr)
                for r in refused.refusals:
                    print(f"- {r.code}: {r.message}", file=sys.stderr)
                return 1
            print(reply.message)
            for w in reply.warnings:
                print(f"- warning: {w}")
            if reply.success:
                print("The hourly python -m vending.sync_requests reads its status and receipts from the bridge.")
            return 0 if reply.success else 1
    except (NoPick, plan_rounds.RoundError, slots.SlotsNotRecorded, settings.SettingsError, ValueError) as error:
        print(error, file=sys.stderr)
        return 2
    except submit_tool.AnswerNotStored as error:
        print(error, file=sys.stderr)
        return 1
    except psycopg.Error as error:
        print(f"{type(error).__name__}: {str(error).strip().splitlines()[0]}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
