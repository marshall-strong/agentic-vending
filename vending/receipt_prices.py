"""Refresh our sourcing prices from what the worker really paid (#202).

    python -m vending.receipt_prices              # apply every stored receipt, oldest first
    python -m vending.receipt_prices --dry-run    # say what would change, change nothing

`python -m vending.sync_requests` calls `apply` for each fulfilled request it reads, so prices
refresh after every fill with no person and no web access. The command above re-applies the
receipts already stored, for a database that had receipts before this existed. It never calls
the bridge.

**The rule.** `sourcing_options` stores a pack's price (`expected_cents`) for a product, a store
and a pack size. A receipt line says what the worker bought: `packs_bought` packs of
`units_per_pack` units of `sku` at `store`, for `total_paid` in all. Purchases are tax-exempt and
the bottle deposit is not a concern (David, 12 Nov), so `total_paid` is the shelf price of those
packs. The pack price is `total_paid / packs_bought`, rounded half up to the cent. Lines of one
request for the same product, store and pack size are added together first, so two slots of one
drink give one price.

The price goes into every row with the same SKU, store and pack size, primary or alternate, with
`checked_at` set to when the worker resolved the line and `checked_by` set to `receipt`. The note
names the request. The row's store, pack, link, description and max stay as they were. Each change
goes through `vending.sourcing.set_option`, so `action_log` keeps the values it replaced.

**Skipped, with the reason logged** (never guessed):
- a line the worker loaded from the closet or couldn't buy (nothing was paid);
- a substitution: the receipt's SKU isn't the one we asked for in that choice;
- a missing SKU, store, pack size or amount; zero packs; a pack size or units that don't add up;
- no sourcing row for that SKU, store and pack size (a person adds one first: we don't invent a
  store link or a max);
- a pack price above the row's max (a person raises the max: the worker may pay only up to it).

**Kept, quietly:** a row checked after the receipt (a newer hand price wins), and a row this
receipt already set to this price. So re-reading a receipt changes nothing, and an older receipt
read after a newer one does nothing. A corrected receipt (same request, new amount) replaces the
price it set.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

from vending import planner, sourcing

if TYPE_CHECKING:
    import psycopg

BOUGHT = ("primary", "alternate")


@dataclass
class Applied:
    """What one request's receipt did to the sourcing table. Each entry is a sentence for a person."""
    updated: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


@dataclass
class _Paid:
    sku: str
    store: str
    units_per_pack: int
    packs: int = 0
    total: Decimal = Decimal(0)
    at: datetime | None = None
    lines: list[int] = field(default_factory=list)

    def pack_cents(self) -> int:
        return int((self.total * 100 / self.packs).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def note_for(request_id: str, paid: _Paid) -> str:
    lines = ", ".join(str(n) for n in paid.lines)
    return (f"receipt {request_id}: paid ${paid.total:.2f} for {paid.packs} pack{'' if paid.packs == 1 else 's'}"
            f" (line{'' if len(paid.lines) == 1 else 's'} {lines})")


def from_this_receipt(o: sourcing.Option, request_id: str) -> bool:
    return o.from_receipt and (o.note or "").startswith(f"receipt {request_id}:")


def apply(conn: psycopg.Connection, request_id: str, now: datetime | None = None, dry_run: bool = False) -> Applied:
    """Refresh sourcing prices from one fulfilled request's stored receipt lines. Safe to call again."""
    now = now or datetime.now(timezone.utc)
    result = Applied()
    status = conn.execute("select status from purchase_requests where request_id = %s", (request_id,)).fetchone()
    if status is None or status[0] != "fulfilled":
        return result
    groups = _paid_groups(conn, request_id, result)
    for paid in groups.values():
        _apply_group(conn, request_id, paid, now, dry_run, result)
    return result


def apply_all(conn: psycopg.Connection, now: datetime | None = None, dry_run: bool = False,
              log=print) -> Applied:
    """`apply` for every fulfilled request, oldest first, logging each sentence."""
    total = Applied()
    for (request_id,) in conn.execute(
            "select request_id from purchase_requests where status = 'fulfilled'"
            " order by fulfilled_at nulls first, request_id").fetchall():
        one = apply(conn, request_id, now, dry_run)
        for kind in ("updated", "kept", "skipped"):
            getattr(total, kind).extend(getattr(one, kind))
        for text in one.updated + one.skipped:
            log(text)
    return total


def _paid_groups(conn: psycopg.Connection, request_id: str, result: Applied) -> dict[tuple, _Paid]:
    groups: dict[tuple, _Paid] = {}
    rows = conn.execute(
        "select line_no, source, asked, outcome, sku, store, packs_bought, units_per_pack, units_bought,"
        " total_paid, resolved_at from purchase_lines where request_id = %s order by line_no",
        (request_id,)).fetchall()
    for line_no, source, asked, outcome, sku, store, packs, units_per_pack, units, total, at in rows:
        if source != "purchase" or outcome not in BOUGHT:
            continue   # loaded from the closet, or not bought: nothing was paid
        why = _unusable(asked, outcome, sku, store, packs, units_per_pack, units, total)
        if why:
            result.skipped.append(f"{request_id} line {line_no}: no price taken: {why}")
            continue
        paid = groups.setdefault((sku, store, units_per_pack), _Paid(sku, store, units_per_pack))
        paid.packs += packs
        paid.total += total
        paid.at = at if paid.at is None else max(paid.at, at)
        paid.lines.append(line_no)
    return groups


def _unusable(asked, outcome, sku, store, packs, units_per_pack, units, total) -> str | None:
    """Why a bought line can't give a pack price, or None when it can."""
    choice = (asked or {}).get(outcome)
    wanted = choice.get("sku") if isinstance(choice, dict) else None
    if not sku:
        return "the receipt names no SKU"
    if wanted is None:
        return f"the request has no {outcome} choice to compare it with"
    if wanted != sku:
        return f"a substitution: we asked for {wanted} as the {outcome}, the receipt says {sku}"
    if store not in planner.STORES:
        return f"store {store!r} is not one we buy from"
    if not packs or packs <= 0:
        return "zero packs bought"
    if not units_per_pack or units_per_pack <= 0:
        return "the receipt gives no pack size"
    if units != packs * units_per_pack:
        return f"{units} units bought isn't {packs} packs of {units_per_pack}"
    if total is None or total <= 0:
        return "the receipt gives no amount paid"
    return None


def _apply_group(conn: psycopg.Connection, request_id: str, paid: _Paid, now: datetime, dry_run: bool,
                 result: Applied) -> None:
    cents = paid.pack_cents()
    where = f"{paid.sku} at {paid.store}, {paid.units_per_pack} a pack"
    rows = [o for o in sourcing.options_for(conn, [paid.sku])
            if o.store == paid.store and o.units_per_pack == paid.units_per_pack]
    if not rows:
        result.skipped.append(
            f"{request_id}: paid {_money(cents)} a pack for {where}, but the sourcing table has no row for that "
            f"store and pack; add one with python -m vending.sourcing set {paid.sku} ... --by NAME")
        return
    for o in rows:
        if from_this_receipt(o, request_id) and o.checked_at == paid.at and o.expected_cents == cents:
            result.kept.append(f"{o.sku} {o.rank}: already {_money(cents)} from {request_id}")
            continue
        if o.checked_at > paid.at or (o.checked_at == paid.at and not from_this_receipt(o, request_id)):
            result.kept.append(f"{o.sku} {o.rank}: kept {_money(o.expected_cents) if o.expected_cents else 'no price'}"
                               f" checked {o.checked_at:%Y-%m-%d} by {o.checked_by}, newer than {request_id}")
            continue
        if cents > o.max_cents:
            result.skipped.append(
                f"{request_id}: paid {_money(cents)} a pack for {where}, above the {o.rank} row's max "
                f"{_money(o.max_cents)}; a person should raise the max (python -m vending.sourcing set)")
            continue
        new = sourcing.Option(sku=o.sku, rank=o.rank, store=o.store, units_per_pack=o.units_per_pack,
                              expected_cents=cents, max_cents=o.max_cents, url=o.url,
                              pack_description=o.pack_description, checked_at=paid.at,
                              checked_by=sourcing.RECEIPT_BY, note=note_for(request_id, paid))
        if not dry_run:
            try:
                sourcing.set_option(conn, new, now)
            except sourcing.SourcingError as error:
                result.skipped.append(f"{request_id}: {where}: no price taken: {error}")
                continue
        was = _money(o.expected_cents) if o.expected_cents is not None else "no price"
        result.updated.append(f"{o.sku} {o.rank}: {_money(cents)} a pack from {request_id} "
                              f"(was {was}, checked {o.checked_at:%Y-%m-%d})")


def _money(value: int) -> str:
    return f"${value // 100}.{value % 100:02d}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vending.receipt_prices", description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="say what would change, change nothing")
    args = parser.parse_args(argv)

    from vending import db

    with db.connect() as conn:
        behind = db.pending(conn)
        if behind:
            print(f"The database is missing migrations: {', '.join(behind)}. Check DATABASE_URL points where you "
                  "mean, then run: python -m vending.db migrate", file=sys.stderr)
            return 2
        total = apply_all(conn, dry_run=args.dry_run)
    verb = "would update" if args.dry_run else "updated"
    print(f"done: {verb} {len(total.updated)} prices, kept {len(total.kept)}, skipped {len(total.skipped)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
