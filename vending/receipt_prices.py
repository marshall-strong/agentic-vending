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
names the request. Only the price, `checked_at`, `checked_by` and the note change: the row's store,
pack, link, description and max stay as they were. Each row is locked while it is checked and
written (`vending.sourcing.locked_row` and `set_price`), so a change a person makes at the same
time is never undone. `action_log` keeps the values each change replaced.

**Skipped, with the reason logged** (never guessed):
- a line the worker loaded from the closet or couldn't buy (nothing was paid);
- a substitution: the receipt's SKU isn't the one we asked for in that choice;
- a missing SKU, store, pack size or amount; zero packs; a pack size or units that don't add up;
- an amount that can't be a pack price: above the most any row may hold, or under half a cent;
- no sourcing row for that SKU, store and pack size (a person adds one first: we don't invent a
  store link or a max);
- a pack price above the row's max (a person raises the max: the worker may pay only up to it);
- a row the sourcing checks refuse (a time in the future, a delisted product). A dry run runs the
  same checks, so it never promises a change the write then refuses.

**Kept, quietly:** a row checked after the receipt (a newer hand price wins), and a row this
receipt already set to this price. So re-reading a receipt changes nothing, and an older receipt
read after a newer one does nothing. A corrected receipt (same request, new amount) replaces the
price it set.

**Taken back.** A corrected receipt can stop giving a price it gave before: the line is now
unavailable, a substitution, or above the max. Then each row still priced from that request gets
back the price it had before that request set it, from `action_log`. With no such entry, the price
becomes unknown (the planner then plans with the max) until a person checks it.
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
# The most a pack may cost in any sourcing row, in dollars. A line above it can't be a price we keep.
MOST_PER_PACK = Decimal(planner.MAX_PRICE_PER_PACK_CENTS) / 100


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
        """Cents a pack, rounded half up. Raises `ArithmeticError` for an amount Decimal can't round."""
        return int((self.total * 100 / self.packs).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def note_for(request_id: str, paid: _Paid) -> str:
    lines = ", ".join(str(n) for n in paid.lines)
    return (f"{_prefix(request_id)} paid ${paid.total:.2f} for {paid.packs} pack{'' if paid.packs == 1 else 's'}"
            f" (line{'' if len(paid.lines) == 1 else 's'} {lines})")


def from_this_receipt(o: sourcing.Option, request_id: str) -> bool:
    return o.from_receipt and (o.note or "").startswith(_prefix(request_id))


def apply(conn: psycopg.Connection, request_id: str, now: datetime | None = None, dry_run: bool = False) -> Applied:
    """Refresh sourcing prices from one fulfilled request's stored receipt lines. Safe to call again.

    A dry run makes the same checks and writes nothing, so it says what the write would do."""
    now = now or datetime.now(timezone.utc)
    result = Applied()
    status = conn.execute("select status from purchase_requests where request_id = %s", (request_id,)).fetchone()
    if status is None or status[0] != "fulfilled":
        return result
    confirmed: set[tuple[str, str]] = set()     # (sku, rank) rows this receipt still prices
    for paid in _paid_groups(conn, request_id, result).values():
        _apply_group(conn, request_id, paid, now, dry_run, result, confirmed)
    _take_back(conn, request_id, now, dry_run, result, confirmed)
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


def _prefix(request_id: str) -> str:
    return f"receipt {request_id}:"


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
    # Checked before any rounding: a Decimal too big to round raises, and that must not stop the sync.
    if total is None or not total.is_finite() or total <= 0:
        return "the receipt gives no amount paid"
    if total > packs * MOST_PER_PACK:
        return f"paid {total} for {packs} pack{'' if packs == 1 else 's'}, above the ${MOST_PER_PACK} a pack any row may hold"
    return None


def _apply_group(conn: psycopg.Connection, request_id: str, paid: _Paid, now: datetime, dry_run: bool,
                 result: Applied, confirmed: set[tuple[str, str]]) -> None:
    lines = ", ".join(str(n) for n in paid.lines)
    try:
        cents = paid.pack_cents()
    except ArithmeticError:
        cents = 0
    if cents <= 0:
        result.skipped.append(f"{request_id} line{'' if len(paid.lines) == 1 else 's'} {lines}: no price taken: "
                              f"paid {paid.total} for {paid.packs} packs, which isn't a pack price in cents")
        return
    where = f"{paid.sku} at {paid.store}, {paid.units_per_pack} a pack"
    rows = [o for o in sourcing.options_for(conn, [paid.sku])
            if o.store == paid.store and o.units_per_pack == paid.units_per_pack]
    if not rows:
        result.skipped.append(
            f"{request_id}: paid {_money(cents)} a pack for {where}, but the sourcing table has no row for that "
            f"store and pack; add one with python -m vending.sourcing set {paid.sku} ... --by NAME")
        return
    for seen in rows:
        # Every check below is on the row as it stands, locked until the write, never on `seen`.
        with conn.transaction():
            o = sourcing.locked_row(conn, seen.sku, seen.rank)
            if o is None or o.store != paid.store or o.units_per_pack != paid.units_per_pack:
                result.kept.append(f"{seen.sku} {seen.rank}: changed while {request_id} was read; left as it is")
                continue
            mine = from_this_receipt(o, request_id)
            if mine and o.checked_at == paid.at and o.expected_cents == cents:
                confirmed.add((o.sku, o.rank))
                result.kept.append(f"{o.sku} {o.rank}: already {_money(cents)} from {request_id}")
                continue
            # A price this request set is replaced by its correction. Any other wins if it's newer.
            if not mine and o.checked_at >= paid.at:
                result.kept.append(f"{o.sku} {o.rank}: kept {_price(o)} checked {sourcing.day(o.checked_at)}"
                                   f" by {o.checked_by}, newer than {request_id}")
                continue
            if cents > o.max_cents:
                result.skipped.append(
                    f"{request_id}: paid {_money(cents)} a pack for {where}, above the {o.rank} row's max "
                    f"{_money(o.max_cents)}; a person should raise the max (python -m vending.sourcing set)")
                continue
            try:
                sourcing.set_price(conn, o, cents, paid.at, sourcing.RECEIPT_BY, note_for(request_id, paid),
                                   by=sourcing.RECEIPT_BY, now=now, dry_run=dry_run)
            except sourcing.SourcingError as error:
                result.skipped.append(f"{request_id}: {where}: no price taken: {error}")
                continue
            confirmed.add((o.sku, o.rank))
            result.updated.append(f"{o.sku} {o.rank}: {_money(cents)} a pack from {request_id} "
                                  f"(was {_price(o)}, checked {sourcing.day(o.checked_at)})")


def _take_back(conn: psycopg.Connection, request_id: str, now: datetime, dry_run: bool, result: Applied,
               confirmed: set[tuple[str, str]]) -> None:
    """Rows still priced from this request that its receipt, as now stored, no longer prices."""
    prefix = _prefix(request_id)
    stale = conn.execute(
        "select sku, rank from sourcing_options where checked_by = %s and left(note, %s) = %s order by sku, rank",
        (sourcing.RECEIPT_BY, len(prefix), prefix)).fetchall()
    for sku, rank in stale:
        if (sku, rank) in confirmed:
            continue
        with conn.transaction():
            o = sourcing.locked_row(conn, sku, rank)
            if o is None or not from_this_receipt(o, request_id):
                continue
            earlier = _before_request(conn, o, request_id)
            if earlier is not None:
                cents, at, by, note = earlier
                said = f"put back {_price_of(cents)} checked {sourcing.day(at)} by {by}"
            else:
                cents, at, by, note = None, o.checked_at, sourcing.RECEIPT_BY, \
                    f"receipt {request_id} taken back: price unknown until a person checks it"
                said = "price unknown until a person checks it (python -m vending.sourcing set)"
            try:
                sourcing.set_price(conn, o, cents, at, by, note, by=sourcing.RECEIPT_BY, now=now, dry_run=dry_run)
            except sourcing.SourcingError as error:
                result.skipped.append(f"{request_id}: {sku} {rank}: its receipt no longer gives "
                                      f"{_price(o)}, but it can't be taken back: {error}")
                continue
            result.updated.append(f"{sku} {rank}: the receipt of {request_id} no longer gives {_price(o)}; {said}")


def _before_request(conn: psycopg.Connection, o: sourcing.Option, request_id: str) \
        -> tuple[int | None, datetime, str, str | None] | None:
    """The row's price before `request_id` first set it: (cents, checked_at, checked_by, note), or None.

    That is the `old` of the newest `action_log` entry that wrote a price from this request over one that
    wasn't, for the same store and pack. Corrections of the same request are passed over. None when the log
    has no such entry, or the old row was for another store or pack."""
    prefix = _prefix(request_id)
    row = conn.execute(
        "select arguments->'old' from action_log where tool = 'sourcing.set'"
        " and arguments->>'sku' = %s and arguments->>'rank' = %s"
        " and arguments->'new'->>'checked_by' = %s and left(arguments->'new'->>'note', %s) = %s"
        " and not (coalesce(arguments->'old'->>'checked_by', '') = %s"
        "          and left(coalesce(arguments->'old'->>'note', ''), %s) = %s)"
        " order by id desc limit 1",
        (o.sku, o.rank, sourcing.RECEIPT_BY, len(prefix), prefix, sourcing.RECEIPT_BY, len(prefix), prefix)
    ).fetchone()
    old = row[0] if row else None
    if not old or old.get("store") != o.store or old.get("units_per_pack") != o.units_per_pack:
        return None
    return old.get("expected_cents"), datetime.fromisoformat(old["checked_at"]), old["checked_by"], old.get("note")


def _price(o: sourcing.Option) -> str:
    return _price_of(o.expected_cents)


def _price_of(cents: int | None) -> str:
    return "no price" if cents is None else _money(cents)


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
