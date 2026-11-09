"""Everything `preview_desired_state` and `submit_desired_state` need to run the planner on live data.

`plan_restock` is pure: it takes the current state, the agent's desired state,
the config and the clock, and returns a `Plan`. This module gathers those from
our database and the bridge:

- slots: product, units and unit cost from the receipts (`vending.stock`), the
  current price from the latest good poll;
- the closet, also from the receipts;
- pending requests, from `purchase_requests`;
- the catalog, read live from the bridge (`GET /api/v1/products`), until the
  catalog sync lands and it can come from our own table;
- the spendable balance, from the ledger;
- the planner's settings, from the `config` table. None is required. How many
  of a product fit in a column comes from its catalog size and the loading
  chart (`vending.capacity`), or from `capacity_overrides` where a person
  counted; a product with neither is refused by the planner, not here.

Anything missing raises `NotReady` with the command that fixes it, so the tool
can say exactly what a person has to do before the agent can plan.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

import psycopg

from vending import config, ledger, planner, stock
from vending.bridge import Bridge, BridgeError
from vending.slots import DEVICE_ID, SLOTS
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


def planner_config(conn: psycopg.Connection) -> planner.Config:
    """The planner's settings from `config`, each with its default when unset."""
    overrides = config.get_value(conn, "capacity_overrides", {})
    problem = config.KNOWN["capacity_overrides"].check(overrides)
    if problem:
        # Only a hand edit of the table gets here; `config set` checks first.
        raise NotReady(f"config capacity_overrides {problem}; set it again with "
                       "python -m vending.config set capacity_overrides '{\"SKU\": 8}' --by NAME")
    return planner.Config(
        capacity_overrides=overrides,
        price_ceiling_cents=_int_setting(conn, "price_ceiling_cents", 600),
        tax_percent=_int_setting(conn, "tax_percent", planner.DEFAULT_TAX_PERCENT),
        midweek_fee_cents=_int_setting(conn, "midweek_fee_cents", ledger.DEFAULT_MIDWEEK_FEE_CENTS),
        halted=config.is_halted(conn),
    )


def gather(conn: psycopg.Connection, catalog: list[dict], now: datetime | None = None) -> Gathered:
    """The current state from our database. `catalog` is the bridge's product list."""
    now = now or datetime.now(timezone.utc)
    settings = planner_config(conn)
    notes: list[str] = []

    balance = ledger.balance(conn, now)
    if balance is None:
        raise NotReady(ledger.OPEN_HINT)
    notes.extend(balance.notes)
    spendable = math.floor(balance.spendable_cents)

    counted = stock.stock(conn, until=now)
    notes.extend(counted.warnings)
    by_slot = {s.slot: s for s in counted.slots}
    prices, poll_at = _latest_prices(conn)
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
        )
        for slot in SLOTS
    )
    without_receipts = [s for s in SLOTS if s not in by_slot]
    if without_receipts:
        notes.append(f"no receipts yet for slots {', '.join(without_receipts)}, so their product and stock "
                     "are unknown and counted as empty")
    closet = tuple(planner.ClosetStock(c.product, c.units, c.unit_cost) for c in counted.closet)
    pending = tuple(planner.PendingRequest(r) for (r,) in conn.execute(
        "select request_id from purchase_requests where status = 'pending' order by submitted_at"))
    products = tuple(planner.CatalogProduct(p["sku"], bool(p.get("active", p.get("is_active", True))), p.get("size"))
                     for p in catalog if p.get("sku"))
    current = planner.CurrentState(slots, closet, pending, products, (), spendable)
    return Gathered(current, settings, now, poll_at, tuple(notes))


def read_catalog(bridge: Bridge) -> list[dict]:
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


def to_planner(desired: schema.DesiredState) -> planner.DesiredState:
    """The validated tool input as the planner's dataclasses."""
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
    )


def plan(conn: psycopg.Connection, desired: schema.DesiredState, bridge: Bridge,
         now: datetime | None = None) -> tuple[planner.Plan, Gathered]:
    """Run the planner on live data. Raises `NotReady` when a person has to act first.

    The halt switch is checked before the bridge is read, so that halted with
    the bridge down tells the agent to stop, not to check Tailscale.
    """
    halted = config.is_halted(conn)
    try:
        catalog = read_catalog(bridge)
    except NotReady as blocked:
        if halted:
            raise NotReady(f"{HALTED_HINT} (and {blocked})") from None
        raise
    gathered = gather(conn, catalog, now)
    return planner.plan_restock(gathered.current, to_planner(desired), gathered.config, gathered.now), gathered


def _option(o: schema.PurchaseOption) -> planner.PurchaseOption:
    return planner.PurchaseOption(o.store, o.units_per_pack, o.max_price_per_pack, o.url,
                                  o.expected_price_per_pack, o.pack_description)


def _int_setting(conn: psycopg.Connection, key: str, default: int) -> int:
    value = config.get_value(conn, key)
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _latest_prices(conn: psycopg.Connection) -> tuple[dict[str, int | None], datetime | None]:
    latest = conn.execute(
        "select session_id, started_at from polls where device_id = %s and crc_ok"
        " order by started_at desc limit 1", (DEVICE_ID,)).fetchone()
    if latest is None:
        return {}, None
    rows = conn.execute(
        "select selection, price_cents from poll_selections where session_id = %s and selection = any(%s)",
        (latest[0], list(SLOTS)))
    return {selection: price for selection, price in rows}, latest[1]
