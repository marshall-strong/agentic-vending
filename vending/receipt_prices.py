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
same ASIN, read only from a product path (`/dp/ASIN`, `/gp/product/ASIN`) on an amazon.com host;
any other link must be exactly the row's link. A receipt from an old listing never prices its
replacement. The note names the request. Only the price, `checked_at`, `checked_by` and the note
change: the row's store, pack, link, description and max stay as they were. `action_log` keeps the
values each change replaced.

**Locks.** The purchase request is locked for the whole of `apply`, so a sync storing a newer
revision of its receipt waits until `apply` is done; `apply` never writes from a revision that is
no longer stored. Every sourcing row of the receipt's SKUs is then locked at once, in (sku, rank)
order, before any is checked. Two applies whose receipts list the same products in a different
order lock them in the same order, so they wait for each other instead of deadlocking. Each write
also checks the row still matches what was read (`vending.sourcing.set_price`), so a change a
person makes at the same time is never undone.

**Skipped, with the reason logged** (never guessed):
- a line the worker loaded from the closet or couldn't buy (nothing was paid);
- a substitution: the receipt's SKU isn't the one we asked for in that choice;
- a missing SKU, store, pack size or amount; zero packs; a pack size or units that don't add up;
- an amount that can't be a pack price: above the most any row may hold, or under half a cent;
- an Amazon line whose requested link isn't text;
- no sourcing row for that SKU, store and pack size (and Amazon listing) (a person adds one first:
  we don't invent a store link or a max);
- a pack price above the row's max (a person raises the max: the worker may pay only up to it);
- a row the sourcing checks refuse (a time in the future, a delisted product). A dry run runs the
  same checks, so it never promises a change the write then refuses. `apply_all --dry-run` plays
  each receipt on the rows as the receipts before it would have left them.

**Kept, quietly:** a row checked after the receipt (a newer hand price wins), and a row this
receipt already set to this price. So re-reading a receipt changes nothing, and an older receipt
read after a newer one does nothing.

**Corrections.** A corrected receipt (same request, newer revision) replaces the price it set, but
only while its line is still newer than the row's last hand check. If the correction moves the
line's time back to or before that check, the hand price (from `action_log`) is put back.

**Never taken back.** Only fulfilled receipts are read. A fulfilled receipt is never undone here: a
later rejection is kept out by `vending.purchase_requests.store`, with a warning, and the prices it
set stay. If a correction stops giving a price it gave before (the line is now unavailable or a
substitution), the row keeps that price and `apply` says so; a person checks it.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from vending import planner, sourcing

if TYPE_CHECKING:
    import psycopg

BOUGHT = ("primary", "alternate")
# The most a pack may cost in any sourcing row, in dollars. A line above it can't be a price we keep.
MOST_PER_PACK = Decimal(planner.MAX_PRICE_PER_PACK_CENTS) / 100
# The path of an Amazon product page, which names its listing by ASIN: /dp/B0..., /Name/dp/B0..., /gp/product/B0...
_AMAZON_PRODUCT = re.compile(r"(?:/[^/]+)?/(?:dp|gp/product)/([A-Z0-9]{10})(?:/.*)?", re.IGNORECASE)


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
    """The sourcing rows as `apply` sees them, locked until its transaction ends.

    For a dry run it also remembers the writes it would have made, so `apply_all --dry-run` checks each
    receipt against the rows as the receipts before it would have left them, as the real run does."""

    def __init__(self, dry_run: bool):
        self.dry_run = dry_run
        self.written: dict[tuple[str, str], sourcing.Option] = {}
        self.hand: dict[tuple[str, str], sourcing.Option] = {}    # a hand row a dry-run write replaced

    @staticmethod
    def lock(conn: psycopg.Connection, skus: set[str]) -> None:
        """Lock every row of these SKUs in one statement, in (sku, rank) order, before any is checked."""
        if skus:
            conn.execute("select 1 from sourcing_options where sku = any(%s) order by sku, rank for update",
                         (sorted(skus),))

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

    def still_from(self, conn: psycopg.Connection, request_id: str) -> dict[tuple[str, str], int | None]:
        """Rows whose price is still from `request_id`, with that price. Read only: nothing is locked."""
        prefix = _prefix(request_id)
        found = {(sku, rank): cents for sku, rank, cents in conn.execute(
            "select sku, rank, expected_cents from sourcing_options where checked_by = %s and left(note, %s) = %s",
            (sourcing.RECEIPT_BY, len(prefix), prefix))}
        for key, o in self.written.items():
            if from_this_receipt(o, request_id):
                found[key] = o.expected_cents
            else:
                found.pop(key, None)
        return found


def note_for(request_id: str, paid: _Paid) -> str:
    lines = ", ".join(str(n) for n in paid.lines)
    return (f"{_prefix(request_id)} paid ${paid.total:.2f} for {paid.packs} pack{'' if paid.packs == 1 else 's'}"
            f" (line{'' if len(paid.lines) == 1 else 's'} {lines})")


def from_this_receipt(o: sourcing.Option, request_id: str) -> bool:
    return o.from_receipt and (o.note or "").startswith(_prefix(request_id))


def listing(url: object) -> tuple[str, str] | None:
    """Which Amazon listing a link names: its ASIN when it is an amazon.com product page, else the link as stored.

    The ASIN is read only from the path of an http(s) link on amazon.com or one of its subdomains, so a search
    link (`/s?k=/dp/B0...`) or another site's `/dp/` path is compared as the whole link."""
    if not isinstance(url, str) or not url.strip():
        return None
    text = url.strip()
    try:
        parts = urlsplit(text)
        host = (parts.hostname or "").lower()
    except ValueError:
        return ("url", text)
    if parts.scheme.lower() in ("http", "https") and (host == "amazon.com" or host.endswith(".amazon.com")):
        found = _AMAZON_PRODUCT.fullmatch(parts.path)
        if found:
            return ("asin", found.group(1).upper())
    return ("url", text)


def apply(conn: psycopg.Connection, request_id: str, now: datetime | None = None, dry_run: bool = False,
          rows: _Rows | None = None) -> Applied:
    """Refresh sourcing prices from one fulfilled request's stored receipt lines. Safe to call again.

    A dry run makes the same checks and writes nothing, so it says what the write would do. `rows` carries a dry
    run's writes from one request to the next (`apply_all`)."""
    now = now or datetime.now(timezone.utc)
    rows = rows or _Rows(dry_run)
    result = Applied()
    with conn.transaction():
        # Locked until this apply ends, so a newer revision of the receipt can't be stored while it runs.
        status = conn.execute("select status from purchase_requests where request_id = %s for update",
                              (request_id,)).fetchone()
        if status is None or status[0] != "fulfilled":
            return result
        groups = list(_paid_groups(conn, request_id, result).values())
        rows.lock(conn, {paid.sku for paid in groups})
        seen: set[tuple[str, str]] = set()     # (sku, rank) rows this receipt checked
        for paid in groups:
            _apply_group(conn, request_id, paid, now, rows, result, seen)
        for (sku, rank), cents in sorted(rows.still_from(conn, request_id).items()):
            if (sku, rank) not in seen:
                result.skipped.append(
                    f"{sku} {rank}: still {_price_of(cents)} from {request_id}, but its receipt as stored now gives "
                    "no price for that row; left as it is. A person can check it (python -m vending.sourcing set)")
    return result


def apply_all(conn: psycopg.Connection, now: datetime | None = None, dry_run: bool = False,
              log=print) -> Applied:
    """`apply` for every fulfilled request, oldest first, logging each sentence."""
    total, rows = Applied(), _Rows(dry_run)
    for (request_id,) in conn.execute(
            "select request_id from purchase_requests where status = 'fulfilled'"
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
        named = None
        if store == "Amazon":
            url = asked[outcome].get("url")
            if url is not None and not isinstance(url, str):
                result.skipped.append(f"{request_id} line {line_no}: no price taken: the request's Amazon link is "
                                      f"{url!r}, not a link")
                continue
            named = listing(url)
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
                 result: Applied, seen: set[tuple[str, str]]) -> None:
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
    for candidate in candidates:
        seen.add((candidate.sku, candidate.rank))
        # Every check below is on the row as it stands, locked since `apply` began, never on `candidate`.
        with conn.transaction():
            o = rows.locked(conn, candidate.sku, candidate.rank)
            if o is None or not _matches(o, paid):
                result.kept.append(f"{candidate.sku} {candidate.rank}: changed while {request_id} was read; "
                                   "left as it is")
                continue
            mine = from_this_receipt(o, request_id)
            if mine and o.checked_at == paid.at and o.expected_cents == cents:
                result.kept.append(f"{o.sku} {o.rank}: already {_money(cents)} from {request_id}")
                continue
            # A price this request set is replaced by its correction. Any other wins if it's newer.
            if not mine and o.checked_at >= paid.at:
                result.kept.append(f"{o.sku} {o.rank}: kept {_price(o)} checked {sourcing.day(o.checked_at)}"
                                   f" by {o.checked_by}, newer than {request_id}")
                continue
            if mine and paid.at < o.checked_at and _put_back_hand(conn, request_id, o, paid, now, rows, result):
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
            result.updated.append(f"{o.sku} {o.rank}: {_money(cents)} a pack from {request_id} "
                                  f"(was {_price(o)}, checked {sourcing.day(o.checked_at)})")


def _put_back_hand(conn: psycopg.Connection, request_id: str, o: sourcing.Option, paid: _Paid, now: datetime,
                   rows: _Rows, result: Applied) -> bool:
    """A correction moved this receipt's time back. If the row's last hand check is now as new or newer, put
    that hand price back and return True; otherwise False, and the correction applies as usual."""
    hand = _hand_price(conn, o, rows)
    if hand is None or hand[1] < paid.at:
        return False
    cents, at, by, note = hand
    try:
        rows.set_price(conn, o, cents, at, by, note, now)
    except sourcing.SourcingError as error:
        result.skipped.append(f"{o.sku} {o.rank}: the corrected receipt of {request_id} is dated before {by}'s "
                              f"check of {_price_of(cents)}, but that price can't be put back: {error}")
        return True
    result.updated.append(f"{o.sku} {o.rank}: put back {_price_of(cents)} checked {sourcing.day(at)} by {by}, "
                          f"newer than the corrected receipt of {request_id} (was {_price(o)})")
    return True


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
