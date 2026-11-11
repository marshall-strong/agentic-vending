"""What a person saw in the machine, recorded once per count. See `db/migrations/010_slot_counts.sql`.

    python -m vending.slot_counts record db/counts/2026-11-09-photo.json --by kevin
    python -m vending.slot_counts record FILE --by kevin --dry-run   # check the file, write nothing
    python -m vending.slot_counts show                               # the latest count, with sales since

The polls never say which product is in a selection, and receipts only name
what we stocked ourselves, so a count is the only record of what staff loaded.
A count file is JSON: `count_id`, `counted_at` (with a time zone), `source`
(`photo` or `door`), `device_id`, an optional `note`, and `selections`, each
with `selection`, `product` (null when empty), `units` (null when it couldn't be
counted), `price_on_tag` (exact text such as "2.50", or null) and an optional
`note`. List every selection you looked at, ours or not, so a change in which
slots are ours loses nothing; a selection left out keeps its last count. A photo
count is never exact; a door count is.

Recording writes the whole count in one transaction and logs it to
`action_log`, like the halt switch. It doesn't go through the write gate: a
count describes the machine, it changes nothing in it, so it is allowed while
halted. A count id can be recorded once.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from vending import db
from vending.counters import rises
from vending.slots import DEVICE_ID

ROWS = range(1, 6)
COLUMNS = range(1, 9)
SELECTIONS = tuple(f"{r}{c}" for r in ROWS for c in COLUMNS)
SOURCES = ("photo", "door")


class CountError(ValueError):
    """The count file isn't valid. Every problem is listed at once."""


@dataclass(frozen=True)
class Seen:
    selection: str
    product: str | None
    units: int | None
    price_on_tag: Decimal | None
    note: str | None


@dataclass(frozen=True)
class Count:
    count_id: str
    counted_at: datetime
    source: str
    device_id: str
    note: str | None
    selections: tuple[Seen, ...]

    @property
    def exact(self) -> bool:
        return self.source == "door"


def parse(data: Any) -> Count:
    """A count from its JSON shape, or `CountError` listing every problem."""
    problems: list[str] = []
    if not isinstance(data, dict):
        raise CountError("a count file must be a JSON object")
    count_id = data.get("count_id")
    if not isinstance(count_id, str) or not count_id.strip():
        problems.append("count_id must be non-empty text")
    counted_at = None
    try:
        counted_at = datetime.fromisoformat(str(data.get("counted_at")))
        if counted_at.tzinfo is None:
            problems.append("counted_at needs a time zone, such as -05:00")
    except ValueError:
        problems.append("counted_at must be an ISO date and time, such as 2026-11-09T11:30:00-05:00")
    source = data.get("source")
    if source not in SOURCES:
        problems.append(f"source must be one of {', '.join(SOURCES)}")
    device_id = data.get("device_id", DEVICE_ID)
    if device_id != DEVICE_ID:
        problems.append(f"device_id must be {DEVICE_ID!r}, the machine the dashboard reads")
    if data.get("note") is not None and not isinstance(data.get("note"), str):
        problems.append("note must be text")
    seen: list[Seen] = []
    entries = data.get("selections")
    if not isinstance(entries, list) or not entries:
        problems.append("selections must be a non-empty list")
        entries = []
    names = [e.get("selection") if isinstance(e, dict) else None for e in entries]
    for dup in sorted({n for n in names if n is not None and names.count(n) > 1}):
        problems.append(f"selection {dup} appears more than once")
    for entry in entries:
        if not isinstance(entry, dict):
            problems.append("each selection must be an object")
            continue
        name = str(entry.get("selection"))
        if name not in SELECTIONS:
            problems.append(f"{name!r} isn't a selection in this machine (11 to 58)")
            continue
        if entry.get("note") is not None and not isinstance(entry.get("note"), str):
            problems.append(f"{name}: note must be text")
        product = entry.get("product")
        if product is not None and (not isinstance(product, str) or not product.strip()):
            problems.append(f"{name}: product must be text or null")
        units = entry.get("units")
        if units is not None and (isinstance(units, bool) or not isinstance(units, int) or units < 0):
            problems.append(f"{name}: units must be a whole number from 0, or null")
        if product is None and units:
            problems.append(f"{name}: units are counted but no product is named")
        price = entry.get("price_on_tag")
        exact_price = None
        if price is not None:
            if not isinstance(price, str):
                problems.append(f"{name}: price_on_tag must be exact text such as \"2.50\", not a number")
            else:
                try:
                    exact_price = Decimal(price)
                    if exact_price < 0 or exact_price != exact_price.quantize(Decimal("0.01")):
                        problems.append(f"{name}: price_on_tag must be dollars and cents, such as \"2.50\"")
                except InvalidOperation:
                    problems.append(f"{name}: price_on_tag {price!r} isn't a price")
        seen.append(Seen(name, product.strip() if isinstance(product, str) else None,
                         units, exact_price, entry.get("note")))
    if problems:
        raise CountError("; ".join(problems))
    return Count(count_id.strip(), counted_at, source, device_id, data.get("note"),
                 tuple(sorted(seen, key=lambda s: s.selection)))


def record(conn: psycopg.Connection, count: Count, by: str) -> int:
    """Write the count and its log row in one transaction. Returns the selections written."""
    if not by.strip():
        raise ValueError("a count needs the name of whoever recorded it")
    with conn.transaction():
        if conn.execute("select 1 from slot_counts where count_id = %s limit 1", (count.count_id,)).fetchone():
            raise CountError(f"count {count.count_id!r} is already recorded")
        with conn.cursor() as cur:
            cur.executemany(
                "insert into slot_counts (count_id, counted_at, counted_by, source, device_id, selection,"
                " product, units, exact, price_on_tag, note) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [(count.count_id, count.counted_at, by.strip(), count.source, count.device_id, s.selection,
                  s.product, s.units, count.exact, s.price_on_tag, s.note) for s in count.selections])
        stocked = sum(1 for s in count.selections if s.product)
        conn.execute(
            "insert into action_log (kind, tool, arguments, outcome, detail) values ('write', 'slot_counts', %s, 'done', %s)",
            (Jsonb({"count_id": count.count_id, "source": count.source, "selections": len(count.selections)}),
             f"{count.source} count {count.count_id} recorded by {by.strip()}: "
             f"{stocked} of {len(count.selections)} selections hold a product"))
    return len(count.selections)


def latest(conn: psycopg.Connection, device_id: str = DEVICE_ID) -> dict[str, dict]:
    """Each selection's most recent count, with units sold since it from the polls.

    Sales since a count start at the first good poll at or after it (the count already
    reflects what sold before), and add up the rises between consecutive polls; a counter
    that drops (a board swap) starts again from its new value, as in `vending.stock`.
    `sold_since` is None, and `on_hand` is the counted units, when no poll has come in
    since the count, or the poll tables aren't there. Sales between the count and the
    first poll after it can't be seen, so on hand is an estimate, never below 0.
    """
    rows = conn.execute(
        "select distinct on (selection) selection, count_id, counted_at, source, product, units, exact,"
        " price_on_tag, note from slot_counts where device_id = %s"
        " order by selection, counted_at desc, recorded_at desc", (device_id,)).fetchall()
    if not rows:
        return {}
    counters = _counters_since(conn, device_id, min(r[2] for r in rows))
    out: dict[str, dict] = {}
    for selection, count_id, counted_at, source, product, units, exact, price, note in rows:
        sold = None if counters is None else _sold_since(counters.get(selection, []), counted_at)
        on_hand = None if units is None else (units if sold is None else max(0, units - sold))
        out[selection] = {"count_id": count_id, "counted_at": counted_at, "source": source, "product": product,
                          "units_counted": units, "exact": exact, "price_on_tag": price, "note": note,
                          "sold_since": sold, "on_hand": on_hand}
    return out


def _counters_since(conn: psycopg.Connection, device_id: str, since: datetime) -> dict[str, list] | None:
    """Every good poll's lifetime units per selection from `since` on, oldest first, or None
    when the poll tables aren't there (a savepoint keeps the caller's transaction usable)."""
    try:
        with conn.transaction():
            found = conn.execute(
                "select ps.selection, p.started_at, ps.lifetime_units from polls p join poll_selections ps"
                " using (session_id) where p.device_id = %s and p.crc_ok and p.started_at >= %s"
                " order by ps.selection, p.started_at", (device_id, since)).fetchall()
    except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn):
        return None
    out: dict[str, list] = {}
    for selection, at, units in found:
        out.setdefault(selection, []).append((at, units))
    return out


def _sold_since(polls: list[tuple[datetime, int]], counted_at: datetime) -> int | None:
    """Units sold across the polls at or after the count; None when none has come in yet.

    Uses `vending.counters.rises`, the same rule as `vending.stock` and the ledger, so one
    misread poll or a board swap counts the same here as everywhere else.
    """
    after = [(at, units) for at, units in polls if at >= counted_at]
    if not after:
        return None
    return sum(rise for _, rise in rises(after))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m vending.slot_counts", description="Record what is in the machine.")
    sub = parser.add_subparsers(dest="action", required=True)
    rec = sub.add_parser("record", help="record a count from a JSON file")
    rec.add_argument("file", type=Path)
    rec.add_argument("--by", required=True, help="your name, for the log")
    rec.add_argument("--dry-run", action="store_true", help="check the file and write nothing")
    sub.add_parser("show", help="print the latest count with sales since")
    args = parser.parse_args(argv)

    if args.action == "record":
        try:
            count = parse(json.loads(args.file.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, CountError) as error:
            print(f"not recorded: {error}", file=sys.stderr)
            return 1
        stocked = sum(1 for s in count.selections if s.product)
        summary = f"{count.count_id}: {len(count.selections)} selections, {stocked} hold a product, {count.source} count"
        if args.dry_run:
            print(f"valid, nothing written. {summary}")
            return 0
        # Never migrates, like the halt switch: a branch with an unmerged migration
        # must not change the shared database's schema.
        with db.connect() as conn:
            try:
                written = record(conn, count, args.by)
            except psycopg.errors.UndefinedTable:
                print("not recorded: this database has no slot_counts table yet. Run: python -m vending.db migrate",
                      file=sys.stderr)
                return 1
            except CountError as error:
                print(f"not recorded: {error}", file=sys.stderr)
                return 1
        print(f"recorded {written} selections. {summary}")
        return 0

    with db.connect() as conn:
        counts = latest(conn)
    if not counts:
        print("no counts recorded")
        return 0
    for selection in SELECTIONS:
        c = counts.get(selection)
        if c is None:
            continue
        units = "?" if c["units_counted"] is None else str(c["units_counted"])
        sold = "" if c["sold_since"] is None else f", {c['sold_since']} sold since"
        price = "" if c["price_on_tag"] is None else f" ${c['price_on_tag']}"
        print(f"{selection}  {c['product'] or 'empty'}{price}  counted {units}{sold}  ({c['count_id']})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
