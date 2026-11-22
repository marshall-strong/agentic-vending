"""Turn the picked first-fill plan into the day-one purchase request (#214).

    python -m vending.fill                                     # preview: lines, cost, hash; sends nothing
    python -m vending.fill --submit --hash HASH --by NAME      # send it, if it still hashes to HASH
    python -m vending.fill --submit --hash HASH --late --by NAME   # send it after the deadline anyway
    python -m vending.fill --submit --hash HASH --accept-deviations --by NAME   # packs differ from the pick

The team picks one plan in `first_fill_plans` (`python -m vending.plan_store pick`).
This command reads that plan, turns each of its slots into the agent's desired
state (product, sale price, fill to the plan's units), and runs it through the
agent's own path: `planning.plan` and the planner for the preview, and
`submit_desired_state` inside the write gate for the submit. So the request is
planned, hashed, recorded in `purchase_submissions` and `purchase_requests`, and
read back by `python -m vending.sync_requests`, exactly as the agent's would be.

The plan's own pack prices are a model's estimates, so they are never sent.
Every product is bought the way the team's sourcing table says, and a product
whose primary or alternate row has no checked price is refused (#202).

On top of the planner's gates, the command refuses, with every reason at once
and nothing sent:

- `plan_assignment`: the plan was picked for another slot assignment than ours now;
- `no_checked_price`: a product with no sourcing row, or a primary or alternate row with no checked price.
  The request sends the alternate too, so an unchecked one is refused, not dropped: dropping it would send a
  request the person never saw. Check its price (`sourcing set SKU --alternate`) or remove it;
- `stale_price`: a primary or alternate row for a product in the plan was checked more than a week ago
  (`sourcing.Option.is_stale`). The planner only warns; the fill sends both to the stocker, so it refuses;
- `settings_busy`, `receipts_busy`, `inputs_busy` (submit only): someone is changing `config` (the halt switch
  or a `config set`), the receipt sync is storing receipts, or someone is changing another input (the pick, the
  slot assignment, the sourcing table, our catalog copy or the ledger) as the submit starts. Nothing is saved or
  sent; try again in a minute. The submit never waits for a lock while it holds the halt row, so a `halt on`
  can't queue behind it (see `hold_inputs`);
- `test_opening`: the ledger's opening is the 11 Nov test opening (#215): that entry itself
  (`ledger.is_test_opening`: entry 1, dated 11 Nov in Eastern time, with its exact detail), or any opening dated
  on or before 11 Nov. Not the fall cut, which can move;
- `fill_already_sent`: a request since the opening, or any request this command sent, was accepted and not
  rejected by the bridge, so the fill has gone. This command's own attempts carry `purpose = 'fill'` on their
  `purchase_submissions` row, saved before the POST and kept by `settle` and `submissions resolve`. So they
  count whenever they were sent, and neither an opening recorded again after the fill nor an interruption
  before `action_log` is written can hide one;
- `pick_deviation` (submit only): a choice the stocker may use, the primary or the alternate, buys other
  packs, units or a higher most-we-pay cost than the pick (`deviations`), for example a checked 16-pack where
  the pick bought 12-packs. The preview lists each one. `--accept-deviations` sends it anyway, and the log
  records the flag and the list;
- `inputs_changed` (submit only): the sourcing rows or the opening stored now are not the ones the planner
  was given. The submit holds those tables, so this is a second line.

The checked-price and opening guards run on the rows and opening the planner was actually given
(`input_refusals`), not on a second read of the tables.

The planner itself refuses (`Can't plan yet`) while the ledger's opening is dated after now: no money is
ours before the opening.

The plan buys each product once for all its slots, as the picked round counts it: units summed per
product, then rounded up to whole packs. So the request has one purchase line per product, on its first
slot, sized for all of them, and a back-stock line for each other slot that loads from that purchase. Its
`note` names every slot's load, since the bridge's purchase line has no load target. See "Pooled
purchases" in `vending.planner`.

The submit needs `--hash`, the hash the preview printed, and refuses
(`plan_changed`) when the plan now hashes to anything else. The hash covers the
request (its lines, the worst-case cost and the visit fee), the picked plan's ID
and our slots (the server's assignment), so a hash from one plan or assignment
never sends another (`approval_hash`). It does not cover spendable cash or the
price ceiling: those are checked again at the send, as below. The cash it checks
leaves out takings from the polls (`count_takings=False`, `planning.gather`), so
a history backfill after the last check can't change it (round 4, finding 2). Who submitted it,
and the plan's ID, go in `action_log` (tool `fill.submit`) with the submit's reply.

The submit holds the pick, the slot assignment, the sourcing table, our catalog
copy, the ledger, `config` and the receipt sync's lock for its whole transaction
(`hold_inputs`): a `plan_store pick`, a new assignment, a `sourcing set`, a
re-dated opening, a `config set` or a receipt read back by the sync waits until
the send is over, a few seconds plus the POST. It takes them all at once, before
it saves anything, and without waiting: if any is busy it refuses instead.

The submit's transaction is READ COMMITTED (the write gate sets it and checks it),
so a fill that waited on the halt row for another reads what that one committed.
Right before the POST, the duplicate checks (`fill_already_sent`,
`submission_unresolved`) run again on the side connection, which reads what is
committed now.

Once the attempt is saved, right before the POST, it runs the whole preview
again (`_final_check`): the planner over the current rows, every guard, the
deviations and the balance. It then reads the clock once more, and checks the
deadline, the quotes' age and the cash at that reading: a quote can turn a week
old, or a ledger entry dated a little later come due, while the planner runs. The pick and our slots must be the ones it planned
(`pick_changed`), the hash the approved one (`plan_changed`) and the request
body byte for byte the one about to be sent (`request_changed`). Any refusal
marks the saved attempt `not_filed`, and nothing is sent.

The request is due by 23:59 ET on Sun 29 Nov (`key_dates.FILL_REQUEST`). The
submit checks the clock before it saves the attempt, and again in the final
check, right before the POST. From midnight on it refuses (`fill_deadline`)
unless `--late` is given; a refusal at the second check marks the saved attempt
`not_filed`, since nothing was sent. A late request may be denied, but it may
not be, so a person can still choose to send it. The log records `late`. The
preview warns once the deadline has passed.

The planner's store cutoffs time purchases for a Friday visit. The fill is
stocked on Mon 30 Nov (David, 20 Nov), so the command also warns about each
line whose store's cutoff for that day has passed: an Amazon order placed after
it arrives late and is stocked at the next scheduled visit (David, 17 Nov).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import set_json_loads

from vending import (db, key_dates, ledger, plan_rounds, planner, planning, purchase_requests, settings, slots,
                     sourcing, submissions)
from vending.bridge import Bridge
from vending.pricing import Refusal
from vending.tools import desired_state as schema
from vending.tools import submit as submit_tool
from vending.write_gate import Refused, run_write

TOOL = "fill.submit"
EASTERN = ZoneInfo("America/New_York")
# Staff buy and stock the fill on Mon 30 Nov (David, 20 Nov), as the Key dates panel lists it.
DAY_ONE_VISIT = key_dates.STOCKING.day
FRIDAY = 4
# Codes this command adds to the planner's.
PLAN_ASSIGNMENT = "plan_assignment"
NO_CHECKED_PRICE = "no_checked_price"
TEST_OPENING = "test_opening"
ALREADY_SENT = "fill_already_sent"
PICK_CHANGED = "pick_changed"
PLAN_CHANGED = "plan_changed"
PAST_DEADLINE = "fill_deadline"
INPUTS_CHANGED = "inputs_changed"
DEVIATION = "pick_deviation"
REQUEST_CHANGED = "request_changed"
CANNOT_PLAN = "cannot_plan"
SETTINGS_BUSY = "settings_busy"
RECEIPTS_BUSY = "receipts_busy"
INPUTS_BUSY = "inputs_busy"
STALE_PRICE = "stale_price"
LEDGER_ENTRY_DUE = "ledger_entry_due"
PURPOSE = "fill"            # `purchase_submissions.purpose` for the fill's attempts


def fill_deadline() -> datetime:
    """When the fill request is late: the end of its deadline minute (23:59 ET Sun 29 Nov), from the Key dates
    panel's list, so the date is written down once."""
    kd = key_dates.FILL_REQUEST
    return datetime.combine(kd.day, kd.at, EASTERN) + timedelta(minutes=1)


def _deadline_text() -> str:
    kd = key_dates.FILL_REQUEST
    return f"{kd.at:%H:%M} ET {kd.day:%a %d %b}"


def approval_hash(plan_hash: str, plan_id: int, ours: Sequence[str]) -> str:
    """The hash the preview prints and the submit needs: the planner's hash of the request, bound to the picked
    plan's ID and our slots (the server's assignment), so one plan's or assignment's hash can't send another."""
    body = json.dumps({"plan_hash": plan_hash, "plan_id": plan_id, "slots": list(ours)}, sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()


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
    row: tuple = field(default=(), repr=False)   # the stored row as read, to tell if it changed


@dataclass(frozen=True)
class Preview:
    picked: Picked
    plan: planner.Plan | None       # None when the planner could not run
    gathered: planning.Gathered | None
    refusals: tuple[Refusal, ...]   # the planner's and this command's, all at once
    warnings: tuple[str, ...]
    blocked: str | None = None      # what a person must do before anything can be planned
    ours: tuple[str, ...] = ()      # our slots (the server's assignment) when it was planned
    late: bool = False              # past the fill request's deadline
    deviations: tuple[str, ...] = ()   # where the request buys other packs, units or cost than the pick

    @property
    def ok(self) -> bool:
        return self.plan is not None and not self.refusals and self.blocked is None

    @property
    def approval(self) -> str | None:
        """The hash to submit with (`approval_hash`), or None when nothing can be sent."""
        return approval_hash(self.plan.plan_hash, self.picked.id, self.ours) if self.ok else None


def picked(conn: psycopg.Connection, ours: tuple[str, ...]) -> Picked:
    """The picked plan, checked against `ours` when it was loaded for the same assignment."""
    row = _picked_row(conn)
    if row is None:
        raise NoPick("No first-fill plan is picked; pick one with python -m vending.plan_store pick ID --by NAME.")
    plan_id, label, loaded_at, loaded_by, plan, projection, stored = row
    same = set(stored) == set(ours)
    checked = plan_rounds.parse_round(label, plan, projection, ours, saved_at=loaded_at) if same else None
    return Picked(plan_id, label, loaded_at, loaded_by, tuple(stored), checked, row)


def _picked_row(conn: psycopg.Connection) -> tuple | None:
    with conn.cursor() as cur:
        set_json_loads(json.loads, context=cur)   # the plan loader's decoder, as the dashboard reads it
        return cur.execute("select id, label, loaded_at, loaded_by, plan, projection, slots from first_fill_plans"
                           " where picked").fetchone()


def desired_state(checked: plan_rounds.Round) -> schema.DesiredState:
    """Each of the plan's slots as the agent would ask for it. No sourcing: the team's table is used."""
    return schema.DesiredState(slots=[
        schema.DesiredSlot(slot=s.slot, product=s.sku, sale_price=_price(s.price_cents), fill_to=s.units)
        for s in checked.slots.values()])


def checks(conn: psycopg.Connection, pick: Picked, ours: tuple[str, ...],
           except_row: int | None = None) -> list[Refusal]:
    """This command's own refusals. The planner's come from `planning.plan`. `except_row` is the submit's own
    saved attempt, left out of the open-submission check when everything is checked again before the POST."""
    refusals = []
    if pick.round is None:
        refusals.append(Refusal(PLAN_ASSIGNMENT, (
            f"plan #{pick.id} (round {pick.label}) was loaded for another slot assignment than ours now; load and "
            "pick a plan for the current assignment (python -m vending.plan_store load DIR --round N --by NAME)")))
    else:
        skus = sorted({s.sku for s in pick.round.slots.values() if s.sku})
        refusals.extend(_unpriced(sourcing.options_for(conn, skus), pick.round))
    opened = ledger.opening(conn)
    refusals.extend(_test_opening(opened))
    return [*refusals, *duplicates(conn, except_row, opened)]


_UNREAD = object()


def duplicates(conn: psycopg.Connection, except_row: int | None = None, opened=_UNREAD) -> list[Refusal]:
    """`fill_already_sent` and `submission_unresolved`: a fill, or a request since the opening, that the bridge
    didn't reject, or a submission still open. The submit runs these again on its side connection right before
    the POST (round 5, finding 3). `opened`: the opening already read, or read here when not given."""
    if opened is _UNREAD:
        opened = ledger.opening(conn)
    refusals = []
    if sent := _sent(conn, opened.at if opened is not None else None):
        named = ", ".join(f"{rid} ({status})" for rid, status in sent)
        refusals.append(Refusal(ALREADY_SENT, (
            f"request {named} was sent as the fill or since the ledger's opening and wasn't rejected, so the "
            "day-one fill has gone; it can be sent again only if the bridge rejects it")))
    # The agent's submit refuses this too; here it also stops a preview from looking clean.
    if unresolved := submissions.unresolved(conn, except_row):
        refusals.append(unresolved)
    return refusals


def input_refusals(checked: plan_rounds.Round, gathered: planning.Gathered) -> list[Refusal]:
    """The checked-price and opening guards, run on what the planner was actually given: its sourcing rows and
    the opening its balance counted from. Re-reading the tables instead could pass rows the planner never used."""
    skus = {s.sku for s in checked.slots.values() if s.sku}
    rows = [o for o in gathered.sourcing_rows if o.sku in skus]
    return [*_unpriced(rows, checked), *_stale(rows, checked, gathered.now), *_test_opening(gathered.opening)]


def _stale(options: Sequence[sourcing.Option], checked: plan_rounds.Round, now: datetime) -> list[Refusal]:
    """A refusal per primary or alternate row checked more than a week before `now` (`Option.is_stale`). The
    planner only warns about these; the fill sends both choices to the stocker as the day-one purchase, so each
    must be a price checked within the week (round 4, finding 3)."""
    found = []
    for o in options:
        if o.expected_cents is None or not o.is_stale(now):
            continue    # no checked price at all is `no_checked_price`'s
        used = sorted(s.slot for s in checked.slots.values() if s.sku == o.sku)
        found.append(Refusal(STALE_PRICE, (
            f"{o.sku}'s {o.rank} price ({o.store}) was checked {o.checked_at:%Y-%m-%d}, {(now - o.checked_at).days} "
            f"days ago, and the request would send it for slot{'s' if len(used) > 1 else ''} {', '.join(used)}; "
            f"check it again and record it (python -m vending.sourcing set {o.sku}"
            f"{' --alternate' if o.rank == 'alternate' else ''} --store ... --pack N --price P --by NAME)"),
            used[0] if len(used) == 1 else None))
    return found


def _inputs_changed(conn: psycopg.Connection, gathered: planning.Gathered) -> list[Refusal]:
    """A refusal if the sourcing rows or the opening read now are not what the planner was given. The submit holds
    those tables, so this is a second line: it should never fire."""
    if (tuple(sourcing.listed_options(conn)) == tuple(gathered.sourcing_rows)
            and ledger.opening(conn) == gathered.opening):
        return []
    return [Refusal(INPUTS_CHANGED, (
        "the sourcing table or the ledger's opening changed while this submit ran, so what was planned is not "
        "what is stored now. Nothing was sent. Preview again and submit what it shows."))]


def _test_opening(opened: ledger.Entry | None) -> list[Refusal]:
    """A refusal while the opening is the 11 Nov test opening: that entry itself (`ledger.is_test_opening`), or
    any opening dated on or before its day. Not the fall cut: `FALL_CUTOFF` can move (round 3, finding 2)."""
    if opened is None:
        return []
    day = opened.at.astimezone(EASTERN).date()
    if not (ledger.is_test_opening(opened) or day <= ledger.TEST_OPENING_DATE):
        return []
    return [Refusal(TEST_OPENING, (
        f"the ledger's opening is dated {day:%Y-%m-%d} (entry {opened.id}), so it is the 11 Nov test opening, not "
        "the real one (#215); reset it (python -m vending.ledger reset-test-opening, #222) and record the real "
        "opening first"))]


def _sent(conn: psycopg.Connection, opened_at: datetime | None) -> list[tuple[str, str]]:
    """Requests that aren't rejected by the bridge: every one this command sent, and every one since the opening.

    The fill's own are found by the `fill` mark on their submission row, saved before the POST and kept by
    `settle` and `submissions resolve`, so neither an interruption before the log commits nor an opening
    recorded again after the fill can hide one. An accepted fill with no `purchase_requests` row still counts."""
    return conn.execute(
        "select coalesce(r.request_id, s.request_id), coalesce(r.status, 'accepted')"
        " from purchase_submissions s left join purchase_requests r on r.request_id = s.request_id"
        " where s.purpose = %s and s.status = 'accepted' and s.request_id is not null"
        " and r.status is distinct from 'rejected'"
        " union"
        " select request_id, status from purchase_requests where status <> 'rejected' and submitted_at >= %s"
        " order by 1", (PURPOSE, opened_at)).fetchall()


def deviations(plan: planner.Plan, checked: plan_rounds.Round) -> list[str]:
    """Where a choice the stocker may use buys other packs, units or a higher most-we-pay cost than the pick: one
    line for each product's primary, and one for its alternate.

    The pick's packs come from its round's pack list (`plan_rounds`). The planner buys from the sourcing table's
    checked rows, which may have another pack size or price. The stocker may buy the alternate instead of the
    primary, so it is compared on its own too: pack count, pack size, units and its own most-we-pay cost (round 3,
    finding 3). A product the pick has no pack for can't be compared, so it is listed too. A lower cost is not
    listed."""
    picked = {p.sku: p for p in checked.purchases}
    bought: dict[str, list[planner.Purchase]] = {}
    for ln in plan.lines:
        if ln.purchase is not None:
            bought.setdefault(ln.product, []).append(ln.purchase)
    found = []
    for sku in sorted({s.sku for s in checked.slots.values() if s.sku} | set(bought)):
        mine = bought.get(sku, [])
        primary = _bought([p.primary for p in mine])
        pick = picked.get(sku)
        if pick is None:
            found.append(f"{sku}: the pick has no pack for it, so its packs and cost can't be compared; this request "
                         f"buys {primary[-1]}")
            continue
        theirs = (f"{pick.packs} x {pick.units_per_pack} ({pick.packs * pick.units_per_pack} units, at most "
                  f"{_usd(pick.most_cents)})")
        if _differs(primary, pick):
            found.append(f"{sku}: the pick buys {theirs}; this request buys {primary[-1]}")
        alternates = [p.alternate for p in mine if p.alternate is not None]
        if alternates and _differs(alternate := _bought(alternates), pick):
            stores = ", ".join(sorted({a.store for a in alternates}))
            found.append(f"{sku} (alternate, {stores}): the pick buys {theirs}; the alternate buys {alternate[-1]}")
    return found


def _bought(options: Sequence[planner.PlannedPurchase]) -> tuple[int, list[int], int, int, str]:
    """(packs, pack sizes, units, most-we-pay cents, as text) of one choice, summed over the lines that buy it."""
    packs = sum(o.packs for o in options)
    sizes = sorted({o.units_per_pack for o in options})
    units = sum(o.packs * o.units_per_pack for o in options)
    most = sum(o.packs * o.max_price_per_pack_cents for o in options)
    if not options:
        return packs, sizes, units, most, "none"
    bought_as = f"{packs} x {sizes[0]}" if len(sizes) == 1 else f"{packs} packs"
    return packs, sizes, units, most, f"{bought_as} ({units} units, at most {_usd(most)})"


def _differs(choice: tuple[int, list[int], int, int, str], pick: plan_rounds.Purchase) -> bool:
    packs, sizes, units, most, _ = choice
    return ((packs, sizes, units) != (pick.packs, [pick.units_per_pack], pick.packs * pick.units_per_pack)
            or most > pick.most_cents)


def _unpriced(options: Sequence[sourcing.Option], checked: plan_rounds.Round) -> list[Refusal]:
    """A refusal per slot whose product has no sourcing row, no store, or no checked price, and one per
    product whose alternate, which the request would send too, has no checked price (#202). `options` are the
    sourcing rows to judge: the table's, or the ones the planner was given."""
    primary = {o.sku: o for o in options if o.rank == "primary"}
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
    for o in options:
        if o.rank != "alternate" or o.sku not in primary or (o.store and o.expected_cents is not None):
            continue
        used = sorted(s.slot for s in checked.slots.values() if s.sku == o.sku)
        found.append(Refusal(NO_CHECKED_PRICE, (
            f"{o.sku}'s alternate ({o.store or 'no store'}) has no checked price, and the request would send it "
            f"for slot{'s' if len(used) > 1 else ''} {', '.join(used)}; look it up and record it (python -m "
            f"vending.sourcing set {o.sku} --alternate --store ... --pack N --price P --by NAME), or remove it "
            f"(python -m vending.sourcing remove {o.sku} --alternate --by NAME; #202)")))
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
        stocked = [line.slot, *(slot for slot, _ in line.purchase.feeds)]   # a pooled purchase stocks them all
        for option in (line.purchase.primary, line.purchase.alternate):
            cutoff = cutoffs.get(option.store) if option else None
            if cutoff is None:
                continue
            day = visit - timedelta(days=(FRIDAY - cutoff.weekday) % 7)
            deadline = datetime.combine(day, cutoff.at, EASTERN) + timedelta(seconds=59)
            if now > deadline:
                late.setdefault((option.store, deadline), []).extend(stocked)
    return [f"past the {store} cutoff for the {visit:%a %d %b} stocking ({deadline:%a %d %b %H:%M} ET) for slots "
            f"{', '.join(sorted(set(names)))}; those lines will likely miss it and be stocked at the next scheduled "
            "visit"
            for (store, deadline), names in sorted(late.items())]


def preview(conn: psycopg.Connection, bridge: Bridge, now: datetime | None = None,
            visit: date = DAY_ONE_VISIT, *, except_row: int | None = None) -> Preview:
    """The picked plan planned on live data, as `preview_desired_state` would, plus this command's checks.
    `except_row`: see `checks`. The submit runs this again right before its POST (`_final_check`)."""
    ours = slots.our_slots(conn)
    pick = picked(conn, ours)
    own = checks(conn, pick, ours, except_row)
    late = (now or ledger.db_now(conn)) >= fill_deadline()
    deadline = [f"past the fill request deadline ({_deadline_text()}): a late request may be denied; to send it "
                "anyway, add --late to the submit"] if late else []
    if pick.round is None:
        return Preview(pick, None, None, tuple(own), tuple(deadline), ours=ours, late=late)
    try:
        plan, gathered = planning.plan(conn, desired_state(pick.round), bridge, now, pool=True, count_takings=False)
    except planning.NotReady as blocked:
        return Preview(pick, None, None, tuple(own), tuple(deadline), blocked=str(blocked), ours=ours, late=late)
    own = _unique([*own, *input_refusals(pick.round, gathered)])
    return Preview(pick, plan, gathered, _merge(own, plan.refusals),
                   (*deadline, *plan.warnings, *day_one_warnings(plan, gathered.now, visit),
                    *_plan_warnings(pick.round)), ours=ours, late=late,
                   deviations=tuple(deviations(plan, pick.round)))


def submit(conn: psycopg.Connection, bridge: Callable[[], Bridge], connect: Callable[[], psycopg.Connection],
           by: str, expected_plan_hash: str, now: datetime | None = None, *, late: bool = False,
           accept_deviations: bool = False) -> submit_tool.SubmitReply:
    """Send the picked plan through the agent's submit, inside the write gate. Raises `Refused` with every reason.

    The checks run again inside the gate's transaction, holding the halt lock and every lock `hold_inputs`
    takes, until the send is over. Before the attempt is saved, the pick, our slots, the hash and the deadline
    are checked (`_before_send`), and the guards run on the rows the planner was given (`input_refusals`). With
    any refusal, the planner still runs so its refusals are reported too, but nothing is sent. `late` sends
    after the deadline anyway. A request that buys other packs than the pick (`deviations`) is refused unless
    `accept_deviations`.

    Once the attempt is saved, right before the POST, the whole preview runs again and the body must be the
    approved one, byte for byte (`_final_check`); a refusal there marks the attempt `not_filed`.
    """
    if not by.strip():
        raise ValueError("--by needs your name")
    if not expected_plan_hash.strip():
        raise ValueError("--hash needs the plan hash the preview printed")
    expected = expected_plan_hash.strip()
    arguments = {"by": by.strip(), "expected_plan_hash": expected}
    if late:
        arguments["late"] = True
    if accept_deviations:
        arguments["accept_deviations"] = True

    def action(conn: psycopg.Connection) -> submit_tool.SubmitReply:
        hold_inputs(conn)
        ours = slots.our_slots(conn)
        pick = picked(conn, ours)
        arguments.update(plan_id=pick.id, label=pick.label)
        own = checks(conn, pick, ours)
        if pick.round is None:
            raise Refused([*own, *_deadline(conn, now, late)])
        desired = desired_state(pick.round)
        if own:
            try:
                plan, _ = planning.plan(conn, desired, bridge(), now, pool=True, count_takings=False)
            except planning.NotReady:
                raise Refused([*own, *_deadline(conn, now, late)]) from None
            raise Refused([*_merge(own, plan.refusals), *_deadline(conn, now, late)])

        def before_send(conn: psycopg.Connection, plan: planner.Plan,
                        gathered: planning.Gathered) -> list[Refusal]:
            found = [*input_refusals(pick.round, gathered), *_inputs_changed(conn, gathered),
                     *_before_send(conn, plan, pick, ours, expected, now, late)]
            if devs := deviations(plan, pick.round):
                if accept_deviations:
                    arguments["deviations"] = devs
                else:
                    found.append(Refusal(DEVIATION, (
                        f"the request doesn't buy what the pick buys: {'; '.join(devs)}. To send it as planned, "
                        "run the same command with --accept-deviations; or load and pick a revised plan.")))
            return _unique(found)

        def before_post(side: psycopg.Connection, row: int, body: dict) -> list[Refusal]:
            # After the attempt is saved, right before the POST: the duplicate checks on the side connection,
            # which reads what is committed now (round 5, finding 3), then the whole preview again, on this
            # transaction, which still holds every lock `hold_inputs` took (round 3, finding 1).
            return _unique([*duplicates(side, row),
                            *_final_check(conn, bridge, pick, ours, expected, row, body, now, late=late,
                                          accept_deviations=accept_deviations)])

        return submit_tool.submit_desired_state(desired, bridge, connect, now, pool=True, before_send=before_send,
                                                before_post=before_post, purpose=PURPOSE,
                                                count_takings=False)(conn)

    # The gate logs `arguments` after the action has filled in the plan's ID.
    return run_write(conn, TOOL, arguments, action)


def hold_inputs(conn: psycopg.Connection) -> None:
    """Lock, until the submit's transaction ends, everything its checks read that a person or a sync could change.

    Held from the first check through the POST and the settle: a few seconds of local reads, plus the POST,
    which the bridge client bounds by its timeout. While they are held, these wait for the send to end: a pick
    (`plan_store pick`), a new slot assignment (`slots record`), a `sourcing set`, a catalog sync, a ledger entry
    (the opening, a model call's cost), a `config set` (a price ceiling), a `halt on` and the receipt sync's
    `store`, which takes the same advisory lock (`purchase_requests.RECEIPTS_LOCK`). The fill is run once, by a
    person, so a sync that waits a few seconds costs nothing.

    The submit itself never waits for any of them (round 5, finding 1). The write gate already holds the halt
    row, so a wait here would hold the halt gate while it waited: a `halt on` started then would queue behind the
    fill, and the fill would post once the lock it waited for was free (round 4 was the same with `config`). So
    every lock is taken at once, before anything is saved, without waiting (`NOWAIT`, and
    `pg_try_advisory_xact_lock` for the receipt lock). If any is busy, the submit refuses (`settings_busy`,
    `receipts_busy` or `inputs_busy`), the transaction rolls back, nothing is saved or sent, and whatever was
    waiting on the halt row goes through. A halt started after these locks waits for the send.

    Known limit (round 6, finding 2): so a `halt on` issued in the few seconds while a fill holds these locks
    can't stop that one send. By design: the person running the fill is the operator, and the halt is for the
    unattended agent. A halt already on when the fill starts refuses it (`run_write`'s check, `halted`).

    The side connection that saves and settles the attempt writes only `purchase_submissions` and
    `purchase_requests`. Neither is locked here, so it never waits on this transaction."""
    _nowait(conn, "lock table config in share mode nowait", SETTINGS_BUSY,
            "someone is changing settings or the halt switch right now")
    if not conn.execute("select pg_try_advisory_xact_lock(hashtext(%s))",
                        (purchase_requests.RECEIPTS_LOCK,)).fetchone()[0]:
        raise Refused([_busy(RECEIPTS_BUSY, "the receipt sync is storing receipts right now")])
    # What the checks read is what the planner reads (round 2, finding 1), and what the last check reads is what
    # is sent (round 3, finding 1).
    busy = "someone is changing the pick, our slots, the sourcing table, our catalog copy or the ledger right now"
    _nowait(conn, "lock table first_fill_plans in share mode nowait", INPUTS_BUSY, busy)
    _nowait(conn, "select 1 from slot_assignment where id = 1 for share nowait", INPUTS_BUSY, busy)
    _nowait(conn, "lock table sourcing_options, catalog_products, ledger_entries in share mode nowait",
            INPUTS_BUSY, busy)


def _nowait(conn: psycopg.Connection, sql: str, code: str, why: str) -> None:
    try:
        conn.execute(sql)
    except psycopg.errors.LockNotAvailable:
        raise Refused([_busy(code, why)]) from None


def _busy(code: str, why: str) -> Refusal:
    return Refusal(code, (
        f"{why}, so nothing was saved or sent; try again in a minute. If a halt was turned on, check it first "
        "(python -m vending.halt status)"))


def _final_check(conn: psycopg.Connection, bridge: Callable[[], Bridge], pick: Picked, ours: tuple[str, ...],
                 expected: str, row: int, body: dict, now: datetime | None, *, late: bool,
                 accept_deviations: bool) -> list[Refusal]:
    """Everything the preview checks, run again once the attempt is saved, right before the POST.

    The planner runs over the current rows with every guard, the deviations and the balance. Then the plan must
    still be the one approved: the same pick and our slots, the same approval hash, and a request body
    byte-identical to `body`, the one about to be sent. The submit's own saved attempt (`row`) is left out of
    the open-submission check. Any refusal settles the attempt `not_filed`, and nothing is sent.

    The preview reads the clock before it gathers. So the clock is read again at the end, after the last ledger
    read, the last reading before the POST, and the deadline, the quotes' age and the cash are checked at that
    reading (`_at_the_post`, round 5, finding 2; round 6, finding 1)."""
    try:
        p = preview(conn, bridge(), now, except_row=row)
    except NoPick:
        p = None
    found: list[Refusal] = []
    if p is None or p.picked.row != pick.row or p.ours != ours:
        found.append(Refusal(PICK_CHANGED, (
            f"the picked plan or our slots (the server's assignment) changed while this submit ran; it planned "
            f"plan #{pick.id} (round {pick.label}). Nothing was sent. Preview again and submit what it shows.")))
    if p is not None:
        found.extend(p.refusals)
        if p.blocked:
            found.append(Refusal(CANNOT_PLAN, f"{p.blocked}. Nothing was sent."))
        if p.ok and p.approval != expected:
            found.append(Refusal(PLAN_CHANGED, (
                f"the plan now hashes to {p.approval}, not {expected} as previewed. Nothing was sent. Preview "
                "again and submit what it shows.")))
        if p.ok and json.dumps(p.plan.request()) != json.dumps(body):
            found.append(Refusal(REQUEST_CHANGED, (
                "the request planned now is not byte for byte the one about to be sent. Nothing was sent. Preview "
                "again and submit what it shows.")))
        if p.deviations and not accept_deviations:
            found.append(Refusal(DEVIATION, (
                f"the request doesn't buy what the pick buys: {'; '.join(p.deviations)}. To send it as planned, "
                "run the same command with --accept-deviations; or load and pick a revised plan.")))
    if p is not None and p.plan is not None and p.gathered is not None:
        found.extend(_at_the_post(conn, p, now, late))
    else:
        found.extend(_late(now or ledger.db_now(conn), late))
    return _unique(found)


def _at_the_post(conn: psycopg.Connection, p: Preview, now: datetime | None, late: bool) -> list[Refusal]:
    """The deadline, the quotes' age and the cash at the final clock reading, taken after the ledger is read.

    The ledger is read at one clock reading (`at`), along with when its next entry after `at` falls. Then the
    clock is read once more (`last`), with no other read after it before the POST (round 6, finding 1): the
    deadline and the quotes' age are checked at `last`, and a ledger entry dated after `at` but by `last` (a
    debit that came due during the read) refuses the send. The ledger can't gain an entry meanwhile:
    `hold_inputs` holds it."""
    at = now or ledger.db_now(conn)
    cost = p.plan.cost
    balance = ledger.balance(conn, at)
    due = conn.execute("select min(at) from ledger_entries where at > %s", (at,)).fetchone()[0]
    last = now or ledger.db_now(conn)   # the final clock reading, right before the POST
    skus = {s.sku for s in p.picked.round.slots.values() if s.sku}
    fresh = [o for o in p.gathered.sourcing_rows if o.sku in skus and not o.is_stale(p.gathered.now)]
    found = _stale(fresh, p.picked.round, last)
    cash = math.floor(balance.cash_without_takings_cents) if balance is not None else 0
    if cost is not None and cost.max_cents > cash:
        found.append(Refusal("insufficient_balance", (
            f"at {at.astimezone(EASTERN):%H:%M:%S} ET, right before the send, the spendable balance (without "
            f"takings) is {_usd(cash)}, below the request's max cost {_usd(cost.max_cents)}: a ledger entry came "
            "due while the plan was checked. Nothing was sent. Preview again and submit what it shows.")))
    if due is not None and due <= last:
        found.append(Refusal(LEDGER_ENTRY_DUE, (
            f"a ledger entry dated {due.astimezone(EASTERN):%H:%M:%S} ET came due while the balance was read, "
            "right before the send, so the balance checked is not the balance now. Nothing was sent. Preview "
            "again and submit what it shows.")))
    found.extend(_late(last, late))
    return found


def _before_send(conn: psycopg.Connection, plan: planner.Plan, pick: Picked, ours: tuple[str, ...], expected: str,
                 now: datetime | None, late: bool) -> list[Refusal]:
    """The last checks, right before the POST, in the same transaction: the hash, the pick, our slots, the clock."""
    found = []
    # A refused plan has no hash; its refusals already say what is wrong.
    if plan.plan_hash and (now_hash := approval_hash(plan.plan_hash, pick.id, ours)) != expected:
        found.append(Refusal(PLAN_CHANGED, (
            f"the plan now hashes to {now_hash}, not {expected} as previewed: the picked plan, our slots (the "
            "server's assignment), the lines, their worst-case cost or the visit fee changed since. Preview again "
            "and submit what it shows.")))
    if _picked_row(conn) != pick.row or slots.our_slots(conn) != ours:
        found.append(Refusal(PICK_CHANGED, (
            f"the picked plan or our slots (the server's assignment) changed while this submit ran; it planned "
            f"plan #{pick.id} (round {pick.label}). Nothing was sent. Preview again and submit what it shows.")))
    found.extend(_deadline(conn, now, late))
    return found


def _deadline(conn: psycopg.Connection, now: datetime | None, late: bool) -> list[Refusal]:
    """A refusal once the fill request's deadline has passed, unless `late`. The clock is read now, not reused."""
    return _late(now or ledger.db_now(conn), late)


def _late(at: datetime, late: bool) -> list[Refusal]:
    """A refusal if `at` is past the fill request's deadline, unless `late`."""
    if late or at < fill_deadline():
        return []
    return [Refusal(PAST_DEADLINE, (
        f"the fill request was due by {_deadline_text()}, and it is now {at.astimezone(EASTERN):%a %d %b %H:%M} "
        "ET. A late request may be denied. To send it anyway, run the same command with --late."))]


def _unique(refusals: list[Refusal]) -> list[Refusal]:
    """Each refusal once, in order: the same guard can find the same thing twice."""
    seen, found = set(), []
    for r in refusals:
        if (r.code, r.slot, r.message) not in seen:
            seen.add((r.code, r.slot, r.message))
            found.append(r)
    return found


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
    if p.deviations:
        out += ["", f"Differs from the pick ({len(p.deviations)}):", *(f"- {d}" for d in p.deviations),
                "The submit refuses this (pick_deviation) unless you add --accept-deviations, which the log records. "
                "Or load and pick a revised plan."]
    if p.refusals:
        out += ["", f"REFUSED ({len(p.refusals)}): nothing can be sent until each is fixed.",
                *(f"- {r.code}: {r.message}" for r in p.refusals)]
    if p.ok:
        out += ["", f"Plan hash: {p.approval}",
                f"To send it: python -m vending.fill --submit --hash {p.approval}{' --late' if p.late else ''} "
                "--by NAME"]
    else:
        out += ["", "Plan hash: none (refused plans have no hash)"]
    return "\n".join(out)


def _table(plan: planner.Plan) -> str:
    header = ("Slot", "Product", "Load", "Store", "Packs", "Unit cost (exp / max)", "Price", "Alternate")
    rows = [header]
    for ln in plan.lines:
        if ln.purchase is None:
            source = f"from slot {ln.fed_by}" if ln.fed_by else "closet"
            rows.append((ln.slot, ln.product, str(ln.units_to_load), source, "-", "-",
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
    parser.add_argument("--late", action="store_true",
                        help=f"send after the fill request's deadline ({_deadline_text()}) anyway; it may be denied")
    parser.add_argument("--accept-deviations", action="store_true",
                        help="send a request that buys other packs, units or a higher cost than the pick, as the "
                             "preview listed; the log records it")
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
                reply = submit(conn, bridge, connect, args.by, args.hash, now, late=args.late,
                               accept_deviations=args.accept_deviations)
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
