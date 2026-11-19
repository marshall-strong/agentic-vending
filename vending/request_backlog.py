"""Our backlog of catalog requests: the drinks to ask for next, in order, filed when the bridge has room.

    python -m vending.request_backlog                                   # the backlog, open requests, recent outcomes
    python -m vending.request_backlog add "Snapple Apple" --size "16 oz bottle" --why "..." --by NAME
    python -m vending.request_backlog import db/product_requests/backlog-2026-11-15.json --by NAME
    python -m vending.request_backlog move 7 --to 1 --by NAME
    python -m vending.request_backlog drop 7 --reason "the team decided against it" --by NAME
    python -m vending.request_backlog file --check --by NAME            # what would be filed; sends nothing
    python -m vending.request_backlog file [--max 5] --by NAME          # file from the top until the bridge is full

The bridge refuses new product requests while too many of ours are open (about 24
on 15 Nov 2026), so the next batch waits here until the instructors decide some.
`file` sends them in order through the agent's own write gate and
`request_catalog_product`, so the halt switch, the duplicate checks and the weekly
limit (#130) all apply. It stops at the first `too_many_open_requests`, leaving
the rest queued. A drink that turns out to be in the catalog already, or already
requested, is dropped with that reason.

Every change needs `--by NAME` and is logged in `action_log`. This never migrates.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from vending import db
from vending.bridge import Bridge
from vending.tools import catalog_request as cr
from vending.write_gate import Refused, run_write

FIELDS = ("name", "size", "line", "flavor", "note")
# Refusals that mean the drink is already covered, so it leaves the backlog.
SATISFIED = {"already_requested", "already_in_catalog"}
# Refusals that mean "not now": filing stops and the rest stay queued.
NOT_NOW = {"too_many_open_requests", "halted", "request_limit"}


class BacklogError(ValueError):
    pass


@dataclass(frozen=True)
class Item:
    name: str
    size: str
    line: str | None = None
    flavor: str | None = None
    note: str | None = None
    why: str | None = None
    est_gain_cents: int | None = None
    source: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> Item:
        gain = d.get("est_gain_cents")
        clean = {k: (d.get(k).strip() or None) if isinstance(d.get(k), str) else d.get(k)
                 for k in ("name", "size", "line", "flavor", "note", "why", "source")}
        return cls(name=clean["name"] or "", size=clean["size"] or "", line=clean["line"], flavor=clean["flavor"],
                   note=clean["note"], why=clean["why"], source=clean["source"],
                   est_gain_cents=None if gain in (None, "") else int(gain))


def problems(conn: psycopg.Connection, item: Item) -> list[str]:
    """Why `item` can't join the backlog: a field the bridge would refuse, already requested, or already queued."""
    found = [f"{r.code}: {r.message}" for r in cr.check_fields(item.name, item.size, item.line, item.flavor, item.note)]
    wanted = cr.words(item.name, item.flavor, item.size)
    found += [f"{r.code}: {r.message}" for r in cr.already_requested(conn, wanted)]
    for name, flavor, size in conn.execute(
            "select name, flavor, size from product_request_backlog where status = 'queued'").fetchall():
        if cr.same_drink(wanted, cr.words(name, flavor, size)):
            found.append(f"already_queued: {name} ({size}) is already in the backlog")
    return found


def add(conn: psycopg.Connection, item: Item, by: str, position: int | None = None) -> int:
    """Queue one drink, at `position` (default: the end). Returns its backlog ID."""
    by = _who(by)
    with conn.transaction():
        found = problems(conn, item)
        if found:
            raise BacklogError(f"{item.name} ({item.size}): " + "; ".join(found))
        end = _count_queued(conn) + 1
        (row,) = conn.execute(
            "insert into product_request_backlog (position, name, size, line, flavor, note, why, est_gain_cents,"
            " source, added_by) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) returning id",
            (end, item.name, item.size, item.line, item.flavor, item.note, item.why, item.est_gain_cents,
             item.source, by)).fetchone()
        if position is not None:
            _place(conn, row, position)
        _log(conn, "request_backlog.add", by, {"id": row, **item.__dict__, "position": position}, "queued")
    return row


def import_file(conn: psycopg.Connection, path: Path, by: str) -> list[int]:
    """Queue every drink in a JSON list, in file order, after what's queued. All or nothing."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise BacklogError(f"{path}: {error}") from None
    if not isinstance(raw, list) or not all(isinstance(d, dict) for d in raw):
        raise BacklogError(f"{path}: expected a JSON list of objects with name, size, line, flavor, note, why")
    items = [Item.from_dict(d) for d in raw]
    by = _who(by)
    with conn.transaction():
        bad, ids = [], []
        for i, item in enumerate(items, 1):
            try:
                ids.append(add(conn, item, by))
            except BacklogError as error:
                bad.append(f"item {i}: {error}")
        if bad:
            raise BacklogError(f"{path}: nothing imported.\n" + "\n".join(bad))
    return ids


def move(conn: psycopg.Connection, row: int, to: int, by: str) -> None:
    by = _who(by)
    with conn.transaction():
        _queued_row(conn, row)
        _place(conn, row, to)
        _log(conn, "request_backlog.move", by, {"id": row, "to": to}, "moved")


def drop(conn: psycopg.Connection, row: int, reason: str, by: str) -> None:
    by, reason = _who(by), (reason or "").strip()
    if not reason:
        raise BacklogError("--reason is required, so the dashboard can say why")
    with conn.transaction():
        _queued_row(conn, row)
        _set_status(conn, row, "dropped", drop_reason=f"{reason} ({by})")
        _log(conn, "request_backlog.drop", by, {"id": row, "reason": reason}, "dropped")


def queued(conn: psycopg.Connection) -> list[dict]:
    return _rows(conn, "where b.status = 'queued' order by b.position")


def summary(conn: psycopg.Connection, recent: int = 20) -> dict:
    """What the dashboard shows: the backlog in order, how many requests are open, and recent outcomes."""
    open_requests = conn.execute(
        "select count(*) from product_requests where status in ('sending', 'pending', 'unknown')").fetchone()[0]
    return {"queued": queued(conn), "open_requests": open_requests,
            "recent": _rows(conn, "where b.status <> 'queued' order by b.status_at desc nulls last, b.id desc limit %s",
                            (recent,))}


@dataclass
class FileOutcome:
    id: int
    name: str
    size: str
    result: str          # filed | unknown | dropped | stopped | failed
    detail: str


def file_next(conn: psycopg.Connection, by: str, bridge: Callable[[], Bridge],
              connect: Callable[[], psycopg.Connection], limit: int | None = None) -> list[FileOutcome]:
    """File queued drinks from the top. Stops at the first "not now" refusal or bridge failure."""
    by = _who(by)
    out: list[FileOutcome] = []
    for row in queued(conn)[:limit]:
        args = {k: row[k] for k in FIELDS}
        action = cr.request_catalog_product(row["name"], row["size"], row["line"], row["flavor"], row["note"],
                                            bridge, connect)
        try:
            reply = run_write(conn, "request_catalog_product", {**args, "backlog_id": row["id"], "by": by}, action)
        except Refused as refusal:
            codes = {r.code for r in refusal.refusals}
            if codes & NOT_NOW:
                out.append(FileOutcome(row["id"], row["name"], row["size"], "stopped", str(refusal)))
                break
            reason = "already requested" if "already_requested" in codes else \
                "already in the catalog" if "already_in_catalog" in codes else f"refused when filing: {refusal}"
            with conn.transaction():
                _set_status(conn, row["id"], "dropped", drop_reason=reason)
            out.append(FileOutcome(row["id"], row["name"], row["size"], "dropped", str(refusal)))
            continue
        if reply.success or reply.status == "unknown":
            request = conn.execute("select id from product_requests where bridge_id = %s", (reply.request_id,)).fetchone() \
                if reply.request_id else None
            with conn.transaction():
                _set_status(conn, row["id"], "filed", product_request_id=request[0] if request else None)
            out.append(FileOutcome(row["id"], row["name"], row["size"], "filed" if reply.success else "unknown",
                                   reply.message))
            if not reply.success:
                break      # a person must settle it (`python -m vending.product_requests`) before more go out
            continue
        out.append(FileOutcome(row["id"], row["name"], row["size"], "failed", reply.message))
        break
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vending.request_backlog", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="action")
    sub.add_parser("show", help="the backlog, open requests and recent outcomes (the default)")
    adder = sub.add_parser("add", help="queue one drink")
    adder.add_argument("name")
    adder.add_argument("--size", required=True, help='a liquid volume and package, e.g. "16 oz bottle"')
    for f in ("line", "flavor", "note", "why", "source"):
        adder.add_argument(f"--{f}")
    adder.add_argument("--gain", help="projected gross profit it adds, in dollars, e.g. 49.69")
    adder.add_argument("--at", type=int, help="position (1 = file first); default: the end")
    adder.add_argument("--by", required=True)
    importer = sub.add_parser("import", help="queue every drink in a JSON file, in order")
    importer.add_argument("file", type=Path)
    importer.add_argument("--by", required=True)
    mover = sub.add_parser("move", help="move a queued drink to a new position")
    mover.add_argument("id", type=int)
    mover.add_argument("--to", type=int, required=True)
    mover.add_argument("--by", required=True)
    dropper = sub.add_parser("drop", help="take a drink off the backlog")
    dropper.add_argument("id", type=int)
    dropper.add_argument("--reason", required=True)
    dropper.add_argument("--by", required=True)
    filer = sub.add_parser("file", help="file from the top until the bridge is full")
    filer.add_argument("--max", type=int, help="file at most this many")
    filer.add_argument("--check", action="store_true", help="show what would be filed; send nothing")
    filer.add_argument("--by", required=True)
    args = parser.parse_args(argv)

    with db.connect() as conn:
        try:
            if args.action in (None, "show"):
                return _show(conn)
            if args.action == "add":
                item = Item.from_dict({**{f: getattr(args, f) for f in ("name", "size", "line", "flavor", "note", "why",
                                                                         "source")},
                                       "est_gain_cents": None if args.gain is None else round(float(args.gain) * 100)})
                print(f"queued as {add(conn, item, args.by, args.at)}")
            elif args.action == "import":
                print(f"queued {len(import_file(conn, args.file, args.by))} from {args.file}")
            elif args.action == "move":
                move(conn, args.id, args.to, args.by)
            elif args.action == "drop":
                drop(conn, args.id, args.reason, args.by)
            elif args.action == "file":
                return _file(conn, args)
        except BacklogError as error:
            print(error, file=sys.stderr)
            return 1
    if args.action in ("move", "drop"):
        with db.connect() as conn:
            _show(conn)
    return 0


def _file(conn: psycopg.Connection, args: argparse.Namespace) -> int:
    from vending.config import is_halted

    rows = queued(conn)[:args.max]
    halted = is_halted(conn)
    print(f"halt switch: {'ON' if halted else 'off'}; open requests: {summary(conn)['open_requests']}; "
          f"{len(rows)} to try")
    if args.check or halted or not rows:
        for r in rows:
            print(f"  {r['position']:2d}. {r['name']} ({r['size']})")
        return 2 if halted else 0
    outcomes = file_next(conn, args.by, lambda: Bridge(), db.connect, args.max)
    for o in outcomes:
        print(f"  {o.result:8} {o.name} ({o.size}): {o.detail}")
    left = len(queued(conn))
    print(f"{sum(o.result == 'filed' for o in outcomes)} filed; {left} still queued")
    return 3 if outcomes and outcomes[-1].result in ("stopped", "failed", "unknown") else 0


def _show(conn: psycopg.Connection) -> int:
    s = summary(conn, recent=10)
    print(f"{s['open_requests']} of our product requests are open (sending, pending or unknown).")
    print(f"Backlog: {len(s['queued'])} queued" + (":" if s["queued"] else "."))
    for r in s["queued"]:
        gain = f"  +${r['est_gain_cents'] / 100:.2f}" if r["est_gain_cents"] is not None else ""
        print(f"  {r['position']:2d}. [{r['id']}] {r['name']} ({r['size']}){gain}" + (f"  {r['why']}" if r["why"] else ""))
    if s["recent"]:
        print("Recently filed or dropped:")
        for r in s["recent"]:
            extra = f" (request {r['request_status'] or '?'})" if r["status"] == "filed" else f": {r['drop_reason']}"
            print(f"  {r['status']:7} [{r['id']}] {r['name']} ({r['size']}){extra}")
    return 0


def _rows(conn: psycopg.Connection, where: str, params: tuple = ()) -> list[dict]:
    cols = ("id", "position", "name", "size", "line", "flavor", "note", "why", "est_gain_cents", "source",
            "added_by", "added_at", "status", "status_at", "product_request_id", "drop_reason")
    sql = (f"select {', '.join('b.' + c for c in cols)}, r.status, r.bridge_id from product_request_backlog b"
           f" left join product_requests r on r.id = b.product_request_id {where}")
    return [dict(zip(cols + ("request_status", "bridge_id"), r)) for r in conn.execute(sql, params).fetchall()]


def _count_queued(conn: psycopg.Connection) -> int:
    return conn.execute("select count(*) from product_request_backlog where status = 'queued'").fetchone()[0]


def _queued_row(conn: psycopg.Connection, row: int) -> None:
    found = conn.execute("select status from product_request_backlog where id = %s for update", (row,)).fetchone()
    if found is None:
        raise BacklogError(f"There is no backlog item {row}.")
    if found[0] != "queued":
        raise BacklogError(f"Backlog item {row} is {found[0]}, not queued.")


def _place(conn: psycopg.Connection, row: int, to: int) -> None:
    """Put `row` at position `to` (clamped to the queue) and renumber the queue 1..n."""
    ids = [r[0] for r in conn.execute(
        "select id from product_request_backlog where status = 'queued' and id <> %s order by position, id",
        (row,)).fetchall()]
    ids.insert(max(0, min(to - 1, len(ids))), row)
    for pos, i in enumerate(ids, 1):
        conn.execute("update product_request_backlog set position = %s where id = %s", (pos, i))


def _set_status(conn: psycopg.Connection, row: int, status: str, drop_reason: str | None = None,
                product_request_id: int | None = None) -> None:
    conn.execute("update product_request_backlog set status = %s, position = null, status_at = now(),"
                 " drop_reason = %s, product_request_id = %s where id = %s",
                 (status, drop_reason, product_request_id, row))
    ids = [r[0] for r in conn.execute(
        "select id from product_request_backlog where status = 'queued' order by position, id").fetchall()]
    for pos, i in enumerate(ids, 1):
        conn.execute("update product_request_backlog set position = %s where id = %s", (pos, i))


def _who(by: str) -> str:
    by = (by or "").strip()
    if not by:
        raise BacklogError("--by needs your name")
    return by


def _log(conn: psycopg.Connection, tool: str, by: str, arguments: dict, detail: str) -> None:
    conn.execute("insert into action_log (kind, tool, arguments, outcome, detail) values ('write', %s, %s, 'done', %s)",
                 (tool, Jsonb({**arguments, "by": by}), detail))


if __name__ == "__main__":
    sys.exit(main())
