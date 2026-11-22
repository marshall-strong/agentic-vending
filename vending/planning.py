"""Everything `preview_desired_state` and `submit_desired_state` need to run the planner on live data.

`plan_restock` is pure: it takes the current state, the agent's desired state,
the config and the clock, and returns a `Plan`. This module gathers those from
our database and the bridge:

- slots: product, units and unit cost from the receipts (`vending.stock`), the
  current price from the latest good poll;
- the closet, also from the receipts;
- pending requests, from `purchase_requests`;
- the catalog, from our copy in `catalog_products` that the catalog sync keeps,
  so a bridge outage doesn't stop planning. Only when we hold no listed product
  (the sync has never run) is it read live from the bridge
  (`GET /api/v1/products`, within `bridge.CATALOG_DEADLINE_SECONDS` in all, since
  submit reads it holding the halt lock), and the reply says so;
- the spendable balance, from the ledger;
- the planner's settings, from the `config` table. None is required. How many
  of a product fit in a column comes from its catalog size and the loading
  chart (`vending.capacity`), or from `capacity_overrides` where a person
  counted; a product with neither is refused by the planner, not here. The
  wildcard slot (`wildcard_slot`, set once) must be one of ours in the stored
  assignment, or nothing can be planned until a person looks.

Anything missing raises `NotReady` with the command that fixes it, so the tool
can say exactly what a person has to do before the agent can plan.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime
from zoneinfo import ZoneInfo

import psycopg

from vending import config, ledger, planner, sourcing, stock, wildcard_trial
from vending.bridge import Bridge, BridgeError
from vending.slots import DEVICE_ID, SlotsNotRecorded, our_slots
from vending.tools import catalog as catalog_tool
from vending.tools import desired_state as schema
from vending.tools.machine_state import STALE_AFTER

HALTED_HINT = ("the halt switch is on, so nothing can be planned or submitted until a person runs: "
               "python -m vending.halt off")


class NotReady(RuntimeError):
    """Something a person must do before the planner can run. The message says what."""


@dataclass(frozen=True)
class Gathered:
    """What the planner was given, and anything worth telling the agent about it."""
    current: planner.CurrentState
    config: planner.Config
    now: datetime
    price_poll_at: datetime | None
    notes: tuple[str, ...]
    sourcing_rows: tuple[sourcing.Option, ...] = ()   # the table rows the planner was given
    opening_at: datetime | None = None                # the ledger's opening the balance counted from
    opening: ledger.Entry | None = None               # that opening's entry, to tell the test one (#215)


def planner_config(conn: psycopg.Connection, *, stale_wildcard_unset: bool = False) -> planner.Config:
    """The planner's settings from `config`, each with its default when unset. Our slots must be recorded.

    A wildcard setting that names a selection not in our assignment raises `NotReady`, as any other bad
    setting does. Only a reader that must still show our other selections (the dashboard's pricing panel)
    passes `stale_wildcard_unset`: that selection is then no wildcard and not ours, never planned for."""
    overrides = config.get_value(conn, "capacity_overrides", {})
    problem = config.KNOWN["capacity_overrides"].check(overrides)
    if problem:
        # Only a hand edit of the table gets here; `config set` checks first.
        raise NotReady(f"config capacity_overrides {problem}; set it again with "
                       "python -m vending.config set capacity_overrides '{\"SKU\": 8}' --by NAME")
    try:
        slots = our_slots(conn)
    except SlotsNotRecorded as error:
        raise NotReady(str(error)) from None
    ceiling = _limit(conn, "price_ceiling_cents", planner.AGREED_CEILING_CENTS)
    return planner.Config(
        wildcard_slot=wildcard_slot(conn, slots, stale_unset=stale_wildcard_unset),
        capacity_overrides=overrides,
        slots=slots,
        price_ceiling_cents=ceiling,
        # Never below the ceiling: a lowered wildcard ceiling under the regular one is the regular one.
        wildcard_ceiling_cents=max(_limit(conn, "wildcard_ceiling_cents", planner.AGREED_WILDCARD_CEILING_CENTS),
                                   ceiling),
        wildcard_purchase_cap_cents=_limit(conn, "wildcard_purchase_cap_cents", planner.AGREED_WILDCARD_CAP_CENTS),
        tax_percent=_int_setting(conn, "tax_percent", planner.DEFAULT_TAX_PERCENT),
        midweek_fee_cents=_int_setting(conn, "midweek_fee_cents", ledger.DEFAULT_MIDWEEK_FEE_CENTS),
        spendable_floor_cents=_limit(conn, "spendable_floor_cents", planner.SPENDABLE_FLOOR_CENTS),
        halted=config.is_halted(conn),
    )


def wildcard_slot(conn: psycopg.Connection, slots: tuple[str, ...], *, stale_unset: bool = False) -> str | None:
    """The configured wildcard slot (None: none yet). Raises `NotReady` unless it is one of `slots`, ours.
    With `stale_unset`, a well-formed setting that names a selection outside `slots` reads as None instead."""
    value = config.get_value(conn, "wildcard_slot")
    if value is None:
        return None
    problem = config.KNOWN["wildcard_slot"].check(value)
    if problem is None and value not in slots and stale_unset:
        return None
    if problem is None and value not in slots:
        problem = f"is {value}, which isn't one of ours in the stored slot assignment"
    if problem:
        # Only a hand edit, or an assignment that changed after it was set, gets here.
        raise NotReady(f"config wildcard_slot {problem}, so no plan can say which slot may go above the ceiling; "
                       "a person checks the slot assignment, deletes the row by hand and sets it again "
                       "(python -m vending.config set wildcard_slot '\"37\"' --by NAME)")
    return value


def gather(conn: psycopg.Connection, catalog: list[dict], now: datetime | None = None, *,
           count_takings: bool = True) -> Gathered:
    """The current state from our database. `catalog` is the product list from `read_catalog`.
    "Now" is the database clock, so the balance matches `get_account_balance` (see `ledger.db_now`), read
    after the pending requests (below). A `now` given (a simulation) is used as it is.
    `count_takings=False` (the day-one fill only) gives the planner the spendable balance without takings
    (`Balance.cash_without_takings_cents`), so no poll, and no history backfill, moves its balance gate."""
    settings = planner_config(conn)
    notes: list[str] = []

    # Pending requests first, then the balance and receipts. The receipt sync fulfills a request in one
    # transaction (its status and its lines together, `purchase_requests.store`), and each read here sees
    # what had committed when it ran. Read in this order, a fulfillment that lands part way through either
    # left the request pending when it was read (so the plan is refused, `request_pending`) or committed
    # before, so the receipts read after it hold its lines. Read the other way round, the stock could miss
    # the delivery and the request no longer be pending, and the same plan would be ordered twice (#217).
    # A repeatable-read snapshot wouldn't help: the write gate's first statement waits for the halt lock,
    # and the snapshot would date from before that wait, so it could miss the submit it waited for.
    pending = tuple(planner.PendingRequest(r) for (r,) in conn.execute(
        "select request_id from purchase_requests where status = 'pending' order by submitted_at"))
    # The clock, for the balance and the stock replay's cutoff, only now (Sol round 5): read before the pending
    # requests, a fulfillment committed in between, with its receipt's `resolved_at` after that reading, was
    # neither pending nor counted (the replay stops at the clock), and the same plan went out twice.
    now = now or ledger.db_now(conn)
    balance = ledger.balance(conn, now)
    if balance is None:
        raise NotReady(ledger.OPEN_HINT)
    if balance.opening_at > now:
        # `ledger.balance` counts the opening whenever it is dated, so a future one would lend
        # money that isn't ours yet. Nothing is spent before the opening.
        opens = balance.opening_at.astimezone(ZoneInfo(ledger.TIMEZONE))
        raise NotReady(f"the ledger's opening is dated {opens:%Y-%m-%d %H:%M} ET, after now, so no money is ours "
                       "to spend yet; wait for it, or re-date the opening if that time is wrong")
    notes.extend(balance.notes)
    if count_takings:
        spendable = math.floor(balance.spendable_cents)
    else:
        spendable = math.floor(balance.cash_without_takings_cents)
        if balance.takings.cents:
            notes.append(f"the balance gate leaves out {ledger.money(balance.takings.cents)} of takings from the "
                         "polls, so a history backfill can't change it")
    opened = ledger.opening(conn)   # the entry the balance counted from: the fill tells the test one by it

    receipts = stock.receipt_lines(conn)    # read once: the stock and the trial's loads share these rows
    counted = stock.stock(conn, until=now, lines=receipts)
    notes.extend(counted.warnings)
    by_slot = {s.slot: s for s in counted.slots}
    prices, poll_at = _latest_prices(conn, settings.slots)
    # A slot the latest poll didn't read keeps the last price a poll did read, so a kept slot can't slip past the
    # ceiling just because one poll left it out (the planner refuses a kept slot whose price was never read).
    prices.update(_last_known_prices(conn, [s for s in settings.slots if prices.get(s) is None]))
    if poll_at is None:
        notes.append("no good poll is stored, so current prices are unknown; every slot's price change "
                     "will look like a change")
    elif now - poll_at > STALE_AFTER:
        notes.append(f"STALE: the latest poll is {(now - poll_at).total_seconds() / 3600:.1f} hours old, so "
                     "stock on the rails is overstated by whatever has sold since and prices may have changed; "
                     "run the history sync (python -m vending.sync_history) before trusting this plan")
    slots = tuple(
        planner.SlotState(
            slot=slot,
            product=by_slot[slot].product if slot in by_slot else None,
            price_cents=prices.get(slot),
            units=by_slot[slot].units if slot in by_slot else 0,
            unit_cost=by_slot[slot].unit_cost if slot in by_slot else None,
            contents_known=slot in by_slot,
            layers=by_slot[slot].layers if slot in by_slot else (),
        )
        for slot in settings.slots
    )
    without_receipts = [s for s in settings.slots if s not in by_slot]
    if without_receipts:
        notes.append(f"no receipts yet for slots {', '.join(without_receipts)}, so their product and stock "
                     "are unknown and counted as empty; the wildcard can't switch product while the plan keeps "
                     "one of them, so list each with the product it should hold")
    # One lot per layer, oldest first: the planner draws them in that order, as the stock replay does.
    closet = tuple(planner.ClosetStock(c.product, units, cost)
                   for c in counted.closet for units, cost in c.layers or ((c.units, c.unit_cost),))
    products = tuple(planner.CatalogProduct(p["sku"], bool(p.get("active", p.get("is_active", True))), p.get("size"))
                     for p in catalog if p.get("sku"))
    # The team's sourcing table, used for any product the agent gives no sourcing for. Read once,
    # so the stale-price warnings describe the same rows the planner used.
    table_rows = tuple(sourcing.listed_options(conn))
    verdict, silent = None, frozenset()
    if settings.wildcard_slot is not None:
        # For the promotion gate: the same verdict and silent slots get_machine_state shows the agent.
        counts = {c.slot: c for c in counted.slots if c.slot in settings.slots}
        loads = stock.loads(conn, now, lines=receipts)
        trial = wildcard_trial.trial(conn, now, counts, loads, counted.sales, settings.slots, settings.wildcard_slot)
        verdict = trial.verdict if trial is not None else None
        report = wildcard_trial.silent_slots(conn, now, counts, loads, counted.sales, settings.slots,
                                             settings.wildcard_slot)
        silent = frozenset(s.slot for s in report.silent)
    current = planner.CurrentState(slots, closet, pending, products, sourcing.to_planner(list(table_rows)),
                                   spendable, verdict, silent)
    return Gathered(current, settings, now, poll_at, tuple(notes), table_rows, balance.opening_at, opened)


def read_catalog(conn: psycopg.Connection, bridge: Bridge,
                 now: datetime | None = None) -> tuple[list[dict], list[str]]:
    """The catalog and any notes about where it came from.

    Our synced copy comes first: each listed product as the bridge last gave it,
    so fields like `active` read the same as a live answer. A copy older than
    `get_catalog`'s stale limit is still used, with a note saying to sync. Only
    when we hold no listed product is the bridge read, which can raise `NotReady`.
    `now` (None: the database clock) only dates the copy.
    """
    synced_at = conn.execute("select max(synced_at) from catalog_syncs").fetchone()[0]
    stored = [row[0] for row in conn.execute(
        "select last_response from catalog_products where delisted_at is null order by sku")]
    if synced_at is not None and stored:
        now = now or ledger.db_now(conn)
        notes = []
        if now - synced_at > catalog_tool.STALE_AFTER:
            notes.append(f"STALE: the catalog was last synced {(now - synced_at).total_seconds() / 3600:.1f} hours "
                         "ago, so products added or dropped since then are not seen; run the catalog sync "
                         f"({catalog_tool.SYNC_COMMAND}) before trusting this plan")
        return stored, notes
    products = _bridge_catalog(bridge)
    return products, [f"our copy of the catalog is empty, so it was read live from the bridge; run the catalog "
                      f"sync ({catalog_tool.SYNC_COMMAND}) so planning doesn't depend on the bridge being up"]


def _bridge_catalog(bridge: Bridge) -> list[dict]:
    """The catalog from the bridge, or `NotReady` saying it couldn't be read or can't be used.

    An empty or reshaped reply is refused too: treating it as a catalog would
    tell the agent to request products that are already approved.
    """
    try:
        products = bridge.products()
    except BridgeError as error:
        raise NotReady(f"couldn't read the catalog from the bridge ({error}), so products can't be checked; "
                       "is Tailscale connected?") from None
    if not products or not all(isinstance(p, dict) and p.get("sku") for p in products):
        raise NotReady("the bridge returned an empty or unreadable catalog, so no product can be checked; "
                       "try again, and ask the instructors if it stays empty")
    return products


def to_planner(desired: schema.DesiredState, *, pool: bool = False) -> planner.DesiredState:
    """The validated tool input as the planner's dataclasses. `pool` is never the agent's: see the planner."""
    return planner.DesiredState(
        slots=tuple(planner.DesiredSlot(s.slot, s.keep, s.product, s.sale_price, s.fill_to) for s in desired.slots),
        off_cycle=desired.off_cycle,
        sourcing=tuple(
            planner.Sourcing(
                s.product,
                _option(s.primary),
                None if s.alternate is None else _option(s.alternate),
                s.extra_units_to_closet,
            )
            for s in desired.sourcing
        ),
        pool=pool,
    )


def plan(conn: psycopg.Connection, desired: schema.DesiredState, bridge: Bridge,
         now: datetime | None = None, *, pool: bool = False,
         count_takings: bool = True) -> tuple[planner.Plan, Gathered]:
    """Run the planner on live data. Raises `NotReady` when a person has to act first. `count_takings`: see
    `gather`; never the agent's.

    The halt switch is checked before the catalog is read, so that halted with
    the bridge down tells the agent to stop, not to check Tailscale.

    With no `now` (a live run), `gather` reads the clock after the pending requests, in the same transaction;
    a `now` given (a simulation) is used throughout as it is.
    """
    halted = config.is_halted(conn)
    try:
        catalog, catalog_notes = read_catalog(conn, bridge, now)
    except NotReady as blocked:
        if halted:
            raise NotReady(f"{HALTED_HINT} (and {blocked})") from None
        raise
    gathered = gather(conn, catalog, now, count_takings=count_takings)
    gathered = replace(gathered, notes=(*catalog_notes, *gathered.notes))
    result = planner.plan_restock(gathered.current, to_planner(desired, pool=pool), gathered.config, gathered.now)
    stale = _stale_table_prices(gathered.sourcing_rows, result, desired, gathered.now)
    if stale:
        result = replace(result, warnings=(*result.warnings, *stale))
    return result, gathered


def _stale_table_prices(rows: tuple[sourcing.Option, ...], result: planner.Plan, desired: schema.DesiredState,
                        now: datetime) -> list[str]:
    """A warning for each sourcing-table price over a week old that this plan uses.

    The plan uses a row in two ways:
    - a product the agent gave no sourcing for is bought as the table says: its primary, and its
      alternate when the line has one;
    - for the agent's own option with no expected price, the planner borrows the expected price of
      the first table row for that product with the same store and pack, primary first
      (`planner._Sources._borrow_expected`), and uses it for the plan's cost.
    Rows the plan doesn't use say nothing.
    """
    given = {s.product: s for s in desired.sourcing}
    by_key = {(o.sku, o.rank): o for o in rows}
    bought: dict[str, set[str]] = {}
    for line in result.lines:
        if line.purchase is not None:
            bought.setdefault(line.product, {"primary"})
            if line.purchase.alternate is not None:
                bought[line.product].add("alternate")
    warnings = []
    for product, ranks in sorted(bought.items()):
        if product not in given:
            for rank in ("primary", "alternate"):
                o = by_key.get((product, rank))
                if rank in ranks and o is not None and o.is_stale(now):
                    warnings.append(_stale_warning(o, now, f"the sourcing table's {rank} price"))
            continue
        entry = given[product]
        for rank, mine in (("primary", entry.primary), ("alternate", entry.alternate)):
            if rank not in ranks or mine is None or mine.expected_price_per_pack is not None:
                continue
            lent = next((o for o in (by_key.get((product, "primary")), by_key.get((product, "alternate")))
                         if o is not None and o.store == mine.store and o.units_per_pack == mine.units_per_pack
                         and o.expected_cents is not None), None)
            if lent is not None and lent.is_stale(now):
                warnings.append(_stale_warning(
                    lent, now, f"your {rank} option gave no expected price, so the plan's cost uses the "
                    f"sourcing table's {lent.rank} price"))
    return warnings


def _stale_warning(o: sourcing.Option, now: datetime, what: str) -> str:
    return (f"{o.sku}: {what}, {sourcing.describe_inline(o)}, was checked {o.checked_at:%Y-%m-%d}, "
            f"{(now - o.checked_at).days} days ago; it may have changed, so check it and update it with "
            f"python -m vending.sourcing set {o.sku} ... --by NAME, or give sourcing with its own expected price")


def _option(o: schema.PurchaseOption) -> planner.PurchaseOption:
    return planner.PurchaseOption(o.store, o.units_per_pack, o.max_price_per_pack, o.url,
                                  o.expected_price_per_pack, o.pack_description)


def _limit(conn: psycopg.Connection, key: str, default: int) -> int:
    """One of the agreed limits from `config`, or `default` when unset. A stored value its check refuses (only a
    hand edit gets one past `config set`, such as a ceiling above the agreed $6.50) raises `NotReady`."""
    value = config.get_value(conn, key)
    if value is None:
        return default
    problem = config.KNOWN[key].check(value)
    if problem:
        raise NotReady(f"config {key} {problem}, so no plan can be checked against it; a person sets it again "
                       f"(python -m vending.config set {key} {default} --by NAME)")
    return value


def _int_setting(conn: psycopg.Connection, key: str, default: int) -> int:
    value = config.get_value(conn, key)
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _latest_prices(conn: psycopg.Connection, slots: tuple[str, ...]) -> tuple[dict[str, int | None], datetime | None]:
    """Each slot's price at the latest good poll. Polls at that same moment that disagree give None: unknown."""
    latest = conn.execute(
        "select max(started_at) from polls where device_id = %s and crc_ok", (DEVICE_ID,)).fetchone()[0]
    if latest is None:
        return {}, None
    rows = conn.execute(
        "select s.selection, array_agg(distinct s.price_cents) from poll_selections s join polls p using (session_id)"
        " where p.device_id = %s and p.crc_ok and p.started_at = %s and s.selection = any(%s) group by s.selection",
        (DEVICE_ID, latest, list(slots)))
    return {selection: prices[0] if len(prices) == 1 else None for selection, prices in rows}, latest


def _last_known_prices(conn: psycopg.Connection, slots: list[str]) -> dict[str, int]:
    """Each slot's price at the latest good poll that read one, for slots the latest poll left unread.

    A poll at that moment that disagrees leaves it out: unknown. A price a newer poll didn't read is still the
    price on the shelf as far as we know, so the planner checks a kept slot against it rather than nothing."""
    if not slots:
        return {}
    rows = conn.execute(
        "select distinct on (s.selection) s.selection, p.started_at,"
        " (select array_agg(distinct s2.price_cents) from poll_selections s2 join polls p2 using (session_id)"
        "  where p2.device_id = %s and p2.crc_ok and p2.started_at = p.started_at and s2.selection = s.selection"
        "  and s2.price_cents is not null)"
        " from poll_selections s join polls p using (session_id)"
        " where p.device_id = %s and p.crc_ok and s.selection = any(%s) and s.price_cents is not null"
        " order by s.selection, p.started_at desc", (DEVICE_ID, DEVICE_ID, list(slots)))
    return {selection: prices[0] for selection, _, prices in rows if prices and len(prices) == 1}
