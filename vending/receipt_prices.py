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
`checked_at` set to when the worker resolved the line and `checked_by` set to `receipt`. For
Amazon the listing must match too: the link the request asked for and the row's link must name the
same ASIN (or, without one, be the same link). A receipt from an old listing never prices its
replacement. The note names the request. Only the price, `checked_at`, `checked_by` and the note
change: the row's store, pack, link, description and max stay as they were. Each row is locked
while it is checked and written (`vending.sourcing.locked_row` and `set_price`), so a change a
person makes at the same time is never undone. `action_log` keeps the values each change replaced.

The purchase request is locked for the whole of `apply`, so a sync storing a newer revision of its
receipt waits until `apply` is done; `apply` never writes from a revision that is no longer stored.

**Skipped, with the reason logged** (never guessed):
- a line the worker loaded from the closet or couldn't buy (nothing was paid);
- a substitution: the receipt's SKU isn't the one we asked for in that choice;
- a missing SKU, store, pack size or amount; zero packs; a pack size or units that don't add up;
- an amount that can't be a pack price: above the most any row may hold, or under half a cent;
- no sourcing row for that SKU, store and pack size (and Amazon listing) (a person adds one first:
  we don't invent a store link or a max);
- a pack price above the row's max (a person raises the max: the worker may pay only up to it);
- a row the sourcing checks refuse (a time in the future, a delisted product). A dry run runs the
  same checks, so it never promises a change the write then refuses. `apply_all --dry-run` plays
  each receipt on the rows as the receipts before it would have left them.

**Kept, quietly:** a row checked after the receipt (a newer hand price wins), and a row this
receipt already set to this price. So re-reading a receipt changes nothing, and an older receipt
read after a newer one does nothing. A corrected receipt (same request, new amount) replaces the
price it set.

**Taken back.** A corrected receipt can stop giving a price it gave before: the line is now
unavailable, a substitution, or above the max. The bridge can also withdraw a fill (a rejection
newer than the receipt, `vending.purchase_requests.store`). Then each row still priced from that
request gets the price it would have without it: the newest price another fulfilled receipt gives
for that row as its receipt stands now, if it is newer than the last hand price; otherwise that
hand price, from `action_log` (the row as a person left it before receipts priced it). With
neither, the price becomes unknown (the planner then plans with the max) until a person checks it.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

from vending import planner, sourcing

if TYPE_CHECKING:
    import psycopg

BOUGHT = ("primary", "alternate")
# The most a pack may cost in any sourcing row, in dollars. A line above it can't be a price we keep.
MOST_PER_PACK = Decimal(planner.MAX_PRICE_PER_PACK_CENTS) / 100
# An Amazon product link names its listing by ASIN: /dp/B0..., /gp/product/B0..., and similar.
_ASIN = re.compile(r"/(?:dp|gp/product|gp/aw/d|o/ASIN)/([A-Z0-9]{10})(?=[/?#]|$)", re.IGNORECASE)


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
    listing: tuple[str, str] | None = None      # Amazon only: the listing the request asked for
    packs: int = 0
    total: Decimal = Decimal(0)
    at: datetime | None = None
    lines: list[int] = field(default_factory=list)

    def pack_cents(self) -> int:
        """Cents a pack, rounded half up. Raises `ArithmeticError` for an amount Decimal can't round."""
        return int((self.total * 100 / self.packs).quantize(Decimal(1), rounding=ROUND_HALF_UP))


class _Rows:
    """The sourcing rows as `apply` sees them, each locked until its write.

    For a dry run it also remembers the writes it would have made, so `apply_all --dry-run` checks each
    receipt against the rows as the receipts before it would have left them, as the real run does."""

    def __init__(self, dry_run: bool):
        self.dry_run = dry_run
        self.written: dict[tuple[str, str], sourcing.Option] = {}
        self.hand: dict[tuple[str, str], sourcing.Option] = {}    # a hand row a dry-run write replaced

    def locked(self, conn: psycopg.Connection, sku: str, rank: str) -> sourcing.Option | None:
        return self.written.get((sku, rank)) or sourcing.locked_row(conn, sku, rank)

    def set_price(self, conn: psycopg.Connection, o: sourcing.Option, cents: int | None, at: datetime, by: str,
                  note: str | None, now: datetime) -> sourcing.Option:
        new = sourcing.set_price(conn, o, cents, at, by, note, by=sourcing.RECEIPT_BY, now=now, dry_run=self.dry_run)
        if self.dry_run:
            if not o.from_receipt and new.from_receipt:
                self.hand[(o.sku, o.rank)] = o
            self.written[(o.sku, o.rank)] = new
        return new

    def priced_by(self, conn: psycopg.Connection, request_id: str) -> list[tuple[str, str]]:
        """Rows that may still be priced from `request_id`. The caller checks each again, locked."""
        prefix = _prefix(request_id)
        stored = conn.execute(
            "select sku, rank from sourcing_options where checked_by = %s and left(note, %s) = %s",
            (sourcing.RECEIPT_BY, len(prefix), prefix)).fetchall()
        return sorted({*map(tuple, stored), *self.written})


def note_for(request_id: str, paid: _Paid) -> str:
    lines = ", ".join(str(n) for n in paid.lines)
    return (f"{_prefix(request_id)} paid ${paid.total:.2f} for {paid.packs} pack{'' if paid.packs == 1 else 's'}"
            f" (line{'' if len(paid.lines) == 1 else 's'} {lines})")


def from_this_receipt(o: sourcing.Option, request_id: str) -> bool:
    return o.from_receipt and (o.note or "").startswith(_prefix(request_id))


def listing(url: str | None) -> tuple[str, str] | None:
    """Which Amazon listing a link names: its ASIN when it has one, else the link as stored."""
    if not url:
        return None
    found = _ASIN.search(url)
    return ("asin", found.group(1).upper()) if found else ("url", url.strip())


def apply(conn: psycopg.Connection, request_id: str, now: datetime | None = None, dry_run: bool = False,
          rows: _Rows | None = None) -> Applied:
    """Refresh sourcing prices from one request's stored receipt lines. Safe to call again.

    A fulfilled request prices rows; a rejected one (a withdrawn fill) only takes back the prices it set. A dry
    run makes the same checks and writes nothing, so it says what the write would do. `rows` carries a dry run's
    writes from one request to the next (`apply_all`)."""
    now = now or datetime.now(timezone.utc)
    rows = rows or _Rows(dry_run)
    result = Applied()
    with conn.transaction():
        # Locked until this apply ends, so a newer revision of the receipt can't be stored while it runs.
        status = conn.execute("select status from purchase_requests where request_id = %s for update",
                              (request_id,)).fetchone()
        if status is None or status[0] not in ("fulfilled", "rejected"):
            return result
        confirmed: set[tuple[str, str]] = set()     # (sku, rank) rows this receipt still prices
        if status[0] == "fulfilled":
            for paid in _paid_groups(conn, request_id, result).values():
                _apply_group(conn, request_id, paid, now, rows, result, confirmed)
        _take_back(conn, request_id, now, rows, result, confirmed)
    return result


def apply_all(conn: psycopg.Connection, now: datetime | None = None, dry_run: bool = False,
              log=print) -> Applied:
    """`apply` for every fulfilled or withdrawn request, oldest first, logging each sentence."""
    total, rows = Applied(), _Rows(dry_run)
    for (request_id,) in conn.execute(
            "select request_id from purchase_requests where status in ('fulfilled', 'rejected')"
            " order by fulfilled_at nulls first, request_id").fetchall():
        one = apply(conn, request_id, now, dry_run, rows)
        for kind in ("updated", "kept", "skipped"):
            getattr(total, kind).extend(getattr(one, kind))
        for text in one.updated + one.skipped:
            log(text)
    return total


def _prefix(request_id: str) -> str:
    return f"receipt {request_id}:"


def priced_lines(conn: psycopg.Connection, request_id: str) -> list[tuple]:
    """The stored receipt lines of a request, as far as prices go: every field `apply` reads.

    The same list before and after a read means the read can't have changed what `apply` decides."""
    return conn.execute(
        "select line_no, source, asked, outcome, sku, store, packs_bought, units_per_pack, units_bought,"
        " total_paid, resolved_at from purchase_lines where request_id = %s order by line_no",
        (request_id,)).fetchall()


def _paid_groups(conn: psycopg.Connection, request_id: str, result: Applied) -> dict[tuple, _Paid]:
    groups: dict[tuple, _Paid] = {}
    for line in priced_lines(conn, request_id):
        line_no, source, asked, outcome, sku, store, packs, units_per_pack, units, total, at = line
        if source != "purchase" or outcome not in BOUGHT:
            continue   # loaded from the closet, or not bought: nothing was paid
        why = _unusable(asked, outcome, sku, store, packs, units_per_pack, units, total)
        if why:
            result.skipped.append(f"{request_id} line {line_no}: no price taken: {why}")
            continue
        named = listing(asked[outcome].get("url")) if store == "Amazon" else None
        paid = groups.setdefault((sku, store, units_per_pack, named), _Paid(sku, store, units_per_pack, named))
        paid.packs += packs
        paid.total += total
        paid.at = at if paid.at is None else max(paid.at, at)
        paid.lines.append(line_no)
    return groups


def _matches(o: sourcing.Option, paid: _Paid) -> bool:
    """The row is for what this receipt bought: the same store and pack, and for Amazon the same listing."""
    return (o.sku == paid.sku and o.store == paid.store and o.units_per_pack == paid.units_per_pack
            and (o.store != "Amazon" or (paid.listing is not None and listing(o.url) == paid.listing)))


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


def _cents(paid: _Paid) -> int:
    """The pack price in cents, or 0 when it isn't one."""
    try:
        return paid.pack_cents()
    except ArithmeticError:
        return 0


def _apply_group(conn: psycopg.Connection, request_id: str, paid: _Paid, now: datetime, rows: _Rows,
                 result: Applied, confirmed: set[tuple[str, str]]) -> None:
    lines = ", ".join(str(n) for n in paid.lines)
    cents = _cents(paid)
    if cents <= 0:
        result.skipped.append(f"{request_id} line{'' if len(paid.lines) == 1 else 's'} {lines}: no price taken: "
                              f"paid {paid.total} for {paid.packs} packs, which isn't a pack price in cents")
        return
    where = f"{paid.sku} at {paid.store}, {paid.units_per_pack} a pack"
    candidates = [o for o in sourcing.options_for(conn, [paid.sku]) if _matches(o, paid)]
    if not candidates:
        what = "store, pack and Amazon listing" if paid.store == "Amazon" else "store and pack"
        result.skipped.append(
            f"{request_id}: paid {_money(cents)} a pack for {where}, but the sourcing table has no row for that "
            f"{what}; add one with python -m vending.sourcing set {paid.sku} ... --by NAME")
        return
    for seen in candidates:
        # Every check below is on the row as it stands, locked until the write, never on `seen`.
        with conn.transaction():
            o = rows.locked(conn, seen.sku, seen.rank)
            if o is None or not _matches(o, paid):
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
                rows.set_price(conn, o, cents, paid.at, sourcing.RECEIPT_BY, note_for(request_id, paid), now)
            except sourcing.SourcingError as error:
                result.skipped.append(f"{request_id}: {where}: no price taken: {error}")
                continue
            confirmed.add((o.sku, o.rank))
            result.updated.append(f"{o.sku} {o.rank}: {_money(cents)} a pack from {request_id} "
                                  f"(was {_price(o)}, checked {sourcing.day(o.checked_at)})")


def _take_back(conn: psycopg.Connection, request_id: str, now: datetime, rows: _Rows, result: Applied,
               confirmed: set[tuple[str, str]]) -> None:
    """Rows still priced from this request that its receipt, as now stored, no longer prices."""
    for sku, rank in rows.priced_by(conn, request_id):
        if (sku, rank) in confirmed:
            continue
        with conn.transaction():
            o = rows.locked(conn, sku, rank)
            if o is None or not from_this_receipt(o, request_id):
                continue
            instead = _without(conn, o, request_id, now, rows)
            if instead is not None:
                cents, at, by, note = instead
                source = f"from {note.split(':')[0]}" if by == sourcing.RECEIPT_BY else f"by {by}"
                said = f"put back {_price_of(cents)} checked {sourcing.day(at)} {source}"
            else:
                cents, at, by, note = None, o.checked_at, sourcing.RECEIPT_BY, \
                    f"receipt {request_id} taken back: price unknown until a person checks it"
                said = "price unknown until a person checks it (python -m vending.sourcing set)"
            try:
                rows.set_price(conn, o, cents, at, by, note, now)
            except sourcing.SourcingError as error:
                result.skipped.append(f"{request_id}: {sku} {rank}: its receipt no longer gives "
                                      f"{_price(o)}, but it can't be taken back: {error}")
                continue
            result.updated.append(f"{sku} {rank}: the receipt of {request_id} no longer gives {_price(o)}; {said}")


def _without(conn: psycopg.Connection, o: sourcing.Option, request_id: str, now: datetime, rows: _Rows) \
        -> tuple[int | None, datetime, str, str | None] | None:
    """The row's price if `request_id` had never priced it: (cents, checked_at, checked_by, note), or None.

    That is the newest price another fulfilled receipt gives this row as that receipt is stored now, when it is
    newer than the last hand price; otherwise the last hand price. Receipts are recomputed, never read from
    `action_log`, so a receipt corrected since it last priced the row gives its corrected price."""
    hand = _hand_price(conn, o, rows)
    best: tuple[datetime, int, str] | None = None
    for (other,) in conn.execute(
            "select r.request_id from purchase_requests r"
            " where r.status = 'fulfilled' and r.request_id <> %s"
            " and exists (select 1 from purchase_lines l where l.request_id = r.request_id and l.sku = %s)"
            " order by r.fulfilled_at nulls first, r.request_id", (request_id, o.sku)).fetchall():
        for paid in _paid_groups(conn, other, Applied()).values():
            cents = _cents(paid) if _matches(o, paid) else 0
            if not 0 < cents <= o.max_cents or paid.at > now + timedelta(minutes=5):
                continue
            if hand is not None and paid.at <= hand[1]:
                continue        # the hand price is newer: this receipt never priced the row
            if best is None or paid.at > best[0]:     # ties go to the first applied, as in `apply_all`
                best = (paid.at, cents, note_for(other, paid))
    if best is not None:
        return best[1], best[0], sourcing.RECEIPT_BY, best[2]
    return hand


def _hand_price(conn: psycopg.Connection, o: sourcing.Option, rows: _Rows) \
        -> tuple[int | None, datetime, str, str | None] | None:
    """The row as a person last left it before a receipt priced it: (cents, checked_at, checked_by, note), or None.

    That is the `old` of the newest `action_log` entry where a receipt price replaced one that wasn't (or the
    dry run's own such write). None when there is none, or it was for another store, pack or Amazon listing."""
    simulated = rows.hand.get((o.sku, o.rank))
    if simulated is not None:
        old = {"store": simulated.store, "units_per_pack": simulated.units_per_pack, "url": simulated.url,
               "expected_cents": simulated.expected_cents, "checked_at": simulated.checked_at.isoformat(),
               "checked_by": simulated.checked_by, "note": simulated.note}
    else:
        row = conn.execute(
            "select arguments->'old' from action_log where tool = 'sourcing.set'"
            " and arguments->>'sku' = %s and arguments->>'rank' = %s"
            " and arguments->'new'->>'checked_by' = %s"
            " and coalesce(arguments->'old'->>'checked_by', '') <> %s"
            " order by id desc limit 1",
            (o.sku, o.rank, sourcing.RECEIPT_BY, sourcing.RECEIPT_BY)).fetchone()
        old = row[0] if row else None
    if not old or old.get("store") != o.store or old.get("units_per_pack") != o.units_per_pack:
        return None
    if o.store == "Amazon" and listing(old.get("url")) != listing(o.url):
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
