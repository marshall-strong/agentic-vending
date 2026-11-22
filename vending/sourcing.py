"""Our sourcing table: where to buy each catalog product, in what pack, and the most to pay.

    python -m vending.sourcing                          # every row, with how old its price is
    python -m vending.sourcing set SKU --store Amazon --pack 24 --price 34.99 \\
        --url https://www.amazon.com/dp/... --by NAME   # add or replace a product's primary option
    python -m vending.sourcing set SKU --alternate ...  # the same for its alternate
    python -m vending.sourcing remove SKU [--alternate] --by NAME
    python -m vending.sourcing import FILE.csv --by NAME

The catalog names the products we may order but has no prices or packs, and
every purchase line needs a store, a pack size, a max price per pack and, for
Amazon, a link. People look those up and record them here; the agent has no
tool that browses stores (`docs/planning/tool-list.md`). The planner reads this
table through `table(conn)` when the agent gives no sourcing of its own.

`--price` is what the pack cost when someone looked (`expected`). `--max` is the
most the worker may pay; left out, it is the price plus 15%, rounded up to the
next 50 cents, as the team's 11 Nov price list did. `--checked` is the day the
price was seen (default now), so a price older than `STALE_AFTER` can be
flagged. Every change is logged in `action_log` with the values it replaced.

A row is checked against the same limits as the live purchase-request API and
`vending.planner`, so a stored row is never refused for its shape. Nothing here
reads or stores anything about the other team.
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from vending import planner
from vending.pricing import to_cents

if TYPE_CHECKING:
    import psycopg

RANKS = ("primary", "alternate")
STALE_AFTER = timedelta(days=7)
# `checked_by` on a price taken from a purchase receipt (`vending.receipt_prices`), not seen by a person.
RECEIPT_BY = "receipt"
# The team's rule of thumb (price list, 11 Nov): max = price + 15%, rounded up to 50 cents.
DEFAULT_MARKUP = Decimal("1.15")
MAX_ROUNDING_CENTS = 50
EASTERN = ZoneInfo("America/New_York")
CSV_COLUMNS = ("sku", "rank", "store", "units_per_pack", "price", "max", "url", "pack_description",
               "checked", "note")


class SourcingError(ValueError):
    """A row that can't be stored, or a change that isn't allowed. The message says why."""


@dataclass(frozen=True)
class Option:
    """One stored way to buy a product."""
    sku: str
    rank: str
    store: str
    units_per_pack: int
    expected_cents: int | None
    max_cents: int
    url: str | None
    pack_description: str | None
    checked_at: datetime
    checked_by: str
    note: str | None = None

    def unit_cents(self) -> Decimal | None:
        """The expected price per unit, or None when no price is known."""
        return None if self.expected_cents is None else Decimal(self.expected_cents) / self.units_per_pack

    def is_stale(self, now: datetime) -> bool:
        return now - self.checked_at > STALE_AFTER

    @property
    def from_receipt(self) -> bool:
        """The price is what the worker paid on a receipt (`vending.receipt_prices`), not one a person saw."""
        return self.checked_by == RECEIPT_BY


def cents(text: str, what: str) -> int:
    """Exact cents from a dollar amount like "34.99". Refuses anything inexact or not positive."""
    try:
        value = to_cents(text.strip().lstrip("$"))
    except (ValueError, AttributeError):
        raise SourcingError(f"{what} must be a dollar amount like 34.99, not {text!r}") from None
    if value <= 0:
        raise SourcingError(f"{what} must be above $0.00, not {text!r}")
    return value


def day(moment: datetime) -> str:
    """The Eastern date of `moment`, like 2026-11-27. Connections return UTC, so 11:30 PM ET would read as the
    next day without this."""
    return f"{moment.astimezone(EASTERN):%Y-%m-%d}"


def default_max(expected_cents: int) -> int:
    """The price plus 15%, rounded up to the next 50 cents: 34.99 -> 40.50."""
    raised = Decimal(expected_cents) * DEFAULT_MARKUP / MAX_ROUNDING_CENTS
    return int(raised.to_integral_value(rounding=ROUND_CEILING)) * MAX_ROUNDING_CENTS


def checked_at(text: str | None, now: datetime) -> datetime:
    """When a price was seen: a date (noon Eastern that day, or now if it is today and before noon),
    a full ISO time, or now."""
    if not text:
        return now
    try:
        day = date.fromisoformat(text)
    except ValueError:
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            raise SourcingError(f"checked must be a date like 2026-11-11, not {text!r}") from None
        return moment if moment.tzinfo else moment.replace(tzinfo=EASTERN)
    # Today's date before noon would otherwise land in the future and be refused (Kevin, #132).
    # Clamp only today: a later date must stay in the future so `problems` refuses it.
    noon = datetime.combine(day, time(12), EASTERN)
    return min(noon, now) if day == now.astimezone(EASTERN).date() else noon


def problems(o: Option, now: datetime) -> list[str]:
    """Everything wrong with a row, by the purchase API's and the planner's rules. Empty when it's fine."""
    found = []
    if o.rank not in RANKS:
        found.append(f"rank must be primary or alternate, not {o.rank!r}")
    if o.store not in planner.STORES:
        found.append(f"store {o.store!r} is not one of {', '.join(sorted(planner.STORES))}")
    if o.store == planner.AMAZON:
        if not o.url:
            found.append("an Amazon option needs its link (--url)")
        elif not planner._is_amazon_url(o.url):
            found.append(f"url {o.url!r} is not an Amazon link")
    if not 1 <= o.units_per_pack <= planner.MAX_UNITS_PER_PACK:
        found.append(f"units per pack must be 1 to {planner.MAX_UNITS_PER_PACK}, not {o.units_per_pack}")
    if not 0 < o.max_cents <= planner.MAX_PRICE_PER_PACK_CENTS:
        found.append(f"max must be above $0.00 and at most ${planner.MAX_PRICE_PER_PACK_CENTS // 100}.00 a pack")
    if o.expected_cents is not None and o.expected_cents <= 0:
        found.append("price must be above $0.00")
    elif o.expected_cents is not None and o.expected_cents > o.max_cents:
        found.append(f"max {_money(o.max_cents)} is below the price {_money(o.expected_cents)}, "
                     "so the worker couldn't buy it")
    if o.pack_description is not None and not 1 <= len(o.pack_description) <= planner.MAX_PACK_DESCRIPTION:
        found.append(f"pack description must be 1 to {planner.MAX_PACK_DESCRIPTION} characters")
    if not o.checked_by.strip():
        found.append("--by needs your name")
    if o.checked_at > now + timedelta(minutes=5):
        found.append(f"checked {day(o.checked_at)} is in the future")
    return found


def options(conn: psycopg.Connection) -> list[Option]:
    """Every stored row, by product, primary first."""
    rows = conn.execute(
        "select sku, rank, store, units_per_pack, expected_cents, max_cents, url, pack_description,"
        " checked_at, checked_by, note from sourcing_options order by sku, rank desc").fetchall()
    return [Option(*row) for row in rows]


def options_for(conn: psycopg.Connection, skus: list[str]) -> list[Option]:
    """The stored rows for `skus` only, filtered in the query, by product, primary first."""
    rows = conn.execute(
        "select sku, rank, store, units_per_pack, expected_cents, max_cents, url, pack_description,"
        " checked_at, checked_by, note from sourcing_options where sku = any(%s) order by sku, rank desc",
        (list(skus),)).fetchall()
    return [Option(*row) for row in rows]


def in_catalog(conn: psycopg.Connection, skus: list[str]) -> set[str]:
    """Which of `skus` are in our catalog copy and not delisted."""
    return {sku for (sku,) in conn.execute(
        "select sku from catalog_products where sku = any(%s) and delisted_at is null", (list(skus),))}


def listed_options(conn: psycopg.Connection) -> list[Option]:
    """Every stored row whose product is still in the catalog.

    A product the catalog has since delisted is left out, so the planner refuses it as unknown
    rather than buying it from an old row (Kevin, #132).
    """
    rows = options(conn)
    listed = in_catalog(conn, [o.sku for o in rows])
    return [o for o in rows if o.sku in listed]


def table(conn: psycopg.Connection) -> tuple[planner.Sourcing, ...]:
    """The table in the planner's shape: one `Sourcing` per listed product that has a primary option."""
    return to_planner(listed_options(conn))


def to_planner(rows: list[Option]) -> tuple[planner.Sourcing, ...]:
    """`rows` in the planner's shape: one `Sourcing` per product that has a primary option. Pure."""
    by_sku: dict[str, dict[str, Option]] = {}
    for o in rows:
        by_sku.setdefault(o.sku, {})[o.rank] = o
    return tuple(
        planner.Sourcing(product=sku, primary=_purchase_option(ranks["primary"]),
                         alternate=_purchase_option(ranks["alternate"]) if "alternate" in ranks else None)
        for sku, ranks in sorted(by_sku.items()) if "primary" in ranks)


def set_option(conn: psycopg.Connection, o: Option, now: datetime | None = None) -> str:
    """Add or replace one row, logged with what it replaced. Returns "added" or "replaced"."""
    now = now or datetime.now(timezone.utc)
    found = problems(o, now)
    if found:
        raise SourcingError(f"{o.sku} {o.rank}: " + "; ".join(found))
    with conn.transaction():
        if o.sku not in in_catalog(conn, [o.sku]):
            raise SourcingError(f"{o.sku} is not in our catalog copy, or has been delisted; check the SKU, or run "
                                "python -m vending.sync_catalog if it was added recently")
        if o.rank == "alternate" and conn.execute(
                "select 1 from sourcing_options where sku = %s and rank = 'primary'", (o.sku,)).fetchone() is None:
            raise SourcingError(f"{o.sku} has no primary option yet; set the primary before an alternate")
        before = _row(conn, o.sku, o.rank)
        conn.execute(
            "insert into sourcing_options (sku, rank, store, units_per_pack, expected_cents, max_cents, url,"
            " pack_description, checked_at, checked_by, note) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " on conflict (sku, rank) do update set store = excluded.store,"
            " units_per_pack = excluded.units_per_pack, expected_cents = excluded.expected_cents,"
            " max_cents = excluded.max_cents, url = excluded.url, pack_description = excluded.pack_description,"
            " checked_at = excluded.checked_at, checked_by = excluded.checked_by, note = excluded.note,"
            " updated_at = now()",
            (o.sku, o.rank, o.store, o.units_per_pack, o.expected_cents, o.max_cents, o.url,
             o.pack_description, o.checked_at, o.checked_by.strip(), o.note))
        outcome = "replaced" if before else "added"
        _log(conn, "sourcing.set", o.checked_by, {"new": _jsonable(o), "old": before and _jsonable(before)},
             f"sourcing {o.sku} {o.rank} {outcome}: {describe(o)}"
             + (f" (was {describe(before)})" if before else ""))
    return outcome


def import_rows(conn: psycopg.Connection, rows: list[Option], now: datetime | None = None) -> list[str]:
    """Add or replace these rows, all or nothing. Returns each row's "added" or "replaced", in order.

    Every existing row of the file's SKUs is locked first, in (sku, rank) order (`lock_skus`), as a receipt
    refresh locks them. Writing them in the file's order instead could hold one row a refresh wants while
    waiting on another the refresh holds."""
    unknown = sorted({o.sku for o in rows} - in_catalog(conn, [o.sku for o in rows]))
    if unknown:
        raise SourcingError(f"nothing imported. Not in our catalog copy, or delisted: {', '.join(unknown)}")
    with conn.transaction():
        lock_skus(conn, {o.sku for o in rows})
        return [set_option(conn, o, now) for o in rows]


def lock_skus(conn: psycopg.Connection, skus: set[str]) -> None:
    """Lock every row of these SKUs in one statement, in (sku, rank) order, until the caller's transaction ends.

    Everything that writes several rows at once (`import_rows`, `vending.receipt_prices.apply`) locks them this
    way before touching any, so two of them wait for each other instead of deadlocking."""
    if skus:
        conn.execute("select 1 from sourcing_options where sku = any(%s) order by sku, rank for update",
                     (sorted(skus),))


def locked_row(conn: psycopg.Connection, sku: str, rank: str) -> Option | None:
    """One row as it stands now, locked until the caller's transaction ends, so nobody changes it between
    the caller's checks and its `set_price`. Call it inside `conn.transaction()`."""
    return _row(conn, sku, rank, lock=True)


def set_price(conn: psycopg.Connection, before: Option, expected_cents: int | None, checked_at: datetime,
              checked_by: str, note: str | None, by: str, now: datetime | None = None,
              dry_run: bool = False) -> Option:
    """Change only a row's price and where it came from (`expected_cents`, `checked_at`, `checked_by`,
    `note`), logged in `action_log` like `set_option`. The store, pack, max, link and description stay.

    `before` is the row as the caller checked it, best read with `locked_row` in the same transaction.
    The write happens only if the row still matches `before` in every column, so a change a person made
    meanwhile is never undone; then it raises `SourcingError`. `dry_run` runs every check and writes nothing.
    Returns the row as it is (or would be) after.
    """
    now = now or datetime.now(timezone.utc)
    new = replace(before, expected_cents=expected_cents, checked_at=checked_at, checked_by=checked_by, note=note)
    found = problems(new, now)
    if not in_catalog(conn, [new.sku]):
        found.append("not in our catalog copy, or delisted")
    if found:
        raise SourcingError(f"{new.sku} {new.rank}: " + "; ".join(found))
    if dry_run:
        return new
    with conn.transaction():
        changed = conn.execute(
            "update sourcing_options set expected_cents = %s, checked_at = %s, checked_by = %s, note = %s,"
            " updated_at = now()"
            " where sku = %s and rank = %s and store = %s and units_per_pack = %s and max_cents = %s"
            " and url is not distinct from %s and pack_description is not distinct from %s"
            " and expected_cents is not distinct from %s and checked_at = %s and checked_by = %s"
            " and note is not distinct from %s",
            (new.expected_cents, new.checked_at, new.checked_by.strip(), new.note, before.sku, before.rank,
             before.store, before.units_per_pack, before.max_cents, before.url, before.pack_description,
             before.expected_cents, before.checked_at, before.checked_by, before.note)).rowcount
        if changed != 1:
            raise SourcingError(f"{before.sku} {before.rank}: changed since it was read; left as it is")
        _log(conn, "sourcing.set", by,
             {"sku": new.sku, "rank": new.rank, "new": _jsonable(new), "old": _jsonable(before)},
             f"sourcing {new.sku} {new.rank} replaced: {describe(new)} (was {describe(before)})")
    return new


def remove(conn: psycopg.Connection, sku: str, rank: str, by: str) -> None:
    """Delete one row, logged with its values. A primary can't go while an alternate is left."""
    if not by.strip():
        raise SourcingError("--by needs your name")
    with conn.transaction():
        before = _row(conn, sku, rank)
        if before is None:
            raise SourcingError(f"{sku} has no {rank} option to remove")
        if rank == "primary" and _row(conn, sku, "alternate") is not None:
            raise SourcingError(f"{sku} still has an alternate; remove it first (--alternate), or set a new primary")
        conn.execute("delete from sourcing_options where sku = %s and rank = %s", (sku, rank))
        _log(conn, "sourcing.remove", by, {"old": _jsonable(before)},
             f"sourcing {sku} {rank} removed: {describe(before)}")


def read_csv(path: Path, by: str, now: datetime) -> list[Option]:
    """Rows from a CSV with `CSV_COLUMNS` headers. Every problem in the file is reported at once."""
    found, errors = [], []
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        missing = [c for c in ("sku", "store", "units_per_pack", "price") if c not in (reader.fieldnames or ())]
        if missing:
            raise SourcingError(f"{path}: missing columns {', '.join(missing)}; expected {', '.join(CSV_COLUMNS)}")
        for line, row in enumerate(reader, start=2):
            try:
                found.append(_from_fields({k: (v or "").strip() for k, v in row.items() if k}, by, now))
            except SourcingError as error:
                errors.append(f"line {line}: {error}")
    if errors:
        raise SourcingError(f"{path}: nothing imported.\n" + "\n".join(errors))
    return found


def describe_inline(o: Option) -> str:
    """For use mid-sentence: "Costco 12 a pack at $11.49 (max $13.50)"."""
    price = f"at {_money(o.expected_cents)}" if o.expected_cents is not None else "at an unknown price"
    return f"{o.store} {o.units_per_pack} a pack {price} (max {_money(o.max_cents)})"


def describe(o: Option) -> str:
    price = _money(o.expected_cents) if o.expected_cents is not None else "price unknown"
    return f"{o.store}, {o.units_per_pack} a pack, {price} (max {_money(o.max_cents)})"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vending.sourcing", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="action")
    sub.add_parser("show", help="every row, with how old its price is")
    setter = sub.add_parser("set", help="add or replace one product's primary (or --alternate) option")
    setter.add_argument("sku")
    setter.add_argument("--alternate", action="store_true", help="set the alternate instead of the primary")
    setter.add_argument("--store", required=True, choices=sorted(planner.STORES))
    setter.add_argument("--pack", type=int, required=True, help="units in one pack")
    setter.add_argument("--price", required=True, help="what the pack cost when you looked, e.g. 34.99")
    setter.add_argument("--max", help="most the worker may pay for a pack (default: price + 15%%, up to 50c)")
    setter.add_argument("--url", help="the product page; required for Amazon")
    setter.add_argument("--description", help="what the worker should look for, up to 100 characters")
    setter.add_argument("--checked", help="the day you saw the price, e.g. 2026-11-11 (default: now)")
    setter.add_argument("--note")
    setter.add_argument("--by", required=True, help="your name, for the log")
    remover = sub.add_parser("remove", help="delete one product's primary (or --alternate) option")
    remover.add_argument("sku")
    remover.add_argument("--alternate", action="store_true")
    remover.add_argument("--by", required=True)
    importer = sub.add_parser("import", help=f"add or replace rows from a CSV ({', '.join(CSV_COLUMNS)})")
    importer.add_argument("file", type=Path)
    importer.add_argument("--by", required=True)
    args = parser.parse_args(argv)

    from vending import db

    now = datetime.now(timezone.utc)
    with db.connect() as conn:
        behind = db.pending(conn)
        if behind:
            print(f"The database is missing migrations: {', '.join(behind)}. Check DATABASE_URL points where you "
                  "mean, then run: python -m vending.db migrate", file=sys.stderr)
            return 2
        try:
            if args.action == "set":
                fields = {"sku": args.sku, "rank": "alternate" if args.alternate else "primary", "store": args.store,
                          "units_per_pack": str(args.pack), "price": args.price, "max": args.max or "",
                          "url": args.url or "", "pack_description": args.description or "",
                          "checked": args.checked or "", "note": args.note or ""}
                o = _from_fields(fields, args.by, now)
                print(f"{set_option(conn, o, now)}: {o.sku} {o.rank}: {describe(o)}")
            elif args.action == "remove":
                rank = "alternate" if args.alternate else "primary"
                remove(conn, args.sku, rank, args.by)
                print(f"removed: {args.sku} {rank}")
            elif args.action == "import":
                rows = read_csv(args.file, args.by, now)
                try:
                    outcomes = import_rows(conn, rows, now)
                except SourcingError as error:
                    raise SourcingError(f"{args.file}: {error}") from None
                print(f"imported {len(rows)} rows from {args.file}: {outcomes.count('added')} added, "
                      f"{outcomes.count('replaced')} replaced")
            else:
                return _show(conn, now)
        except SourcingError as error:
            print(error, file=sys.stderr)
            return 1
    return 0


def _show(conn: psycopg.Connection, now: datetime) -> int:
    rows = options(conn)
    if not rows:
        print("No sourcing rows yet. Add one with: python -m vending.sourcing set SKU --store ... --by NAME")
        return 0
    for o in rows:
        unit = o.unit_cents()
        per_unit = f", {_money(int(unit.to_integral_value(rounding=ROUND_CEILING)))} a unit" if unit else ""
        age = (now - o.checked_at).days
        stale = "  STALE" if o.is_stale(now) else ""
        source = f"from {o.note or 'a receipt'}" if o.from_receipt else f"by {o.checked_by}"
        print(f"{o.sku:40} {o.rank:9} {describe(o)}{per_unit}; checked {day(o.checked_at)} "
              f"({age} days ago) {source}{stale}")
        if o.url:
            print(f"{'':40} {'':9} {o.url}")
    return 0


def _from_fields(f: dict[str, str], by: str, now: datetime) -> Option:
    """An `Option` from text fields (the CLI's or a CSV row's). Checks the shape, not the catalog."""
    sku = f.get("sku", "")
    if not sku:
        raise SourcingError("sku is empty")
    if by.strip().lower() == RECEIPT_BY:
        raise SourcingError(f"--by {RECEIPT_BY} is kept for prices taken from receipts; give your name")
    try:
        units = int(f.get("units_per_pack", ""))
    except ValueError:
        raise SourcingError(f"{sku}: units_per_pack must be a whole number, not {f.get('units_per_pack')!r}") \
            from None
    expected = cents(f["price"], f"{sku} price")
    o = Option(sku=sku, rank=f.get("rank") or "primary", store=f.get("store", ""), units_per_pack=units,
               expected_cents=expected,
               max_cents=cents(f["max"], f"{sku} max") if f.get("max") else default_max(expected),
               url=f.get("url") or None, pack_description=f.get("pack_description") or None,
               checked_at=checked_at(f.get("checked"), now), checked_by=by, note=f.get("note") or None)
    found = problems(o, now)
    if found:
        raise SourcingError(f"{sku} {o.rank}: " + "; ".join(found))
    return o


def _purchase_option(o: Option) -> planner.PurchaseOption:
    # Ints are cents to the planner (`vending.pricing.to_cents`).
    return planner.PurchaseOption(store=o.store, units_per_pack=o.units_per_pack, max_price_per_pack=o.max_cents,
                                  url=o.url, expected_price_per_pack=o.expected_cents,
                                  pack_description=o.pack_description)


def _row(conn: psycopg.Connection, sku: str, rank: str, lock: bool = False) -> Option | None:
    row = conn.execute(
        "select sku, rank, store, units_per_pack, expected_cents, max_cents, url, pack_description,"
        " checked_at, checked_by, note from sourcing_options where sku = %s and rank = %s"
        + (" for update" if lock else ""), (sku, rank)).fetchone()
    return None if row is None else Option(*row)


def _log(conn: psycopg.Connection, tool: str, by: str, arguments: dict, detail: str) -> None:
    from psycopg.types.json import Jsonb

    conn.execute("insert into action_log (kind, tool, arguments, outcome, detail) values ('write', %s, %s, 'done', %s)",
                 (tool, Jsonb({**arguments, "by": by.strip()}), detail))


def _jsonable(o: Option) -> dict:
    return {"store": o.store, "units_per_pack": o.units_per_pack, "expected_cents": o.expected_cents,
            "max_cents": o.max_cents, "url": o.url, "pack_description": o.pack_description,
            "checked_at": o.checked_at.isoformat(), "checked_by": o.checked_by, "note": o.note}


def _money(value: int) -> str:
    return f"${value // 100}.{value % 100:02d}"


if __name__ == "__main__":
    sys.exit(main())
