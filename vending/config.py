"""The `config` table: settings the code reads at run time, starting with the halt flag.

    python -m vending.config show
    python -m vending.config set capacity_overrides '{"MONSTER-WHITE-16CAN": 8}' --by marshall
    python -m vending.config set price_ceiling_cents 650 --by marshall
    python -m vending.config set wildcard_slot '"37"' --by kevin

The halt flag fails closed: a missing row, or anything that isn't JSON
`false`, counts as halted. Every other key is a JSON value the planner reads
through `vending.planning.planner_config`. Only the keys in `KNOWN` can be
set, each checked for type and range before it is stored, because a wrong
value here is charged for real: `midweek_fee_cents 25` (dollars typed) would
bill 25 cents a visit, and a ceiling over the API's $10.00 would break every
plan. Every change is logged with who made it. The CLI never migrates.

`wildcard_slot` is write-once: the wildcard never moves (`docs/agent/wildcard.md`),
so once it is set, `set` refuses any other slot. Changing it takes a person
deleting the row by hand, and saying why in the team's thread.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from vending.planner import (AGREED_CEILING_CENTS, AGREED_WILDCARD_CAP_CENTS, AGREED_WILDCARD_CEILING_CENTS,
                             MAX_SALE_PRICE_CENTS)
from vending.pricing import PRICE_STEP_CENTS
from vending.slots import COLUMNS, SHELVES, SlotsNotRecorded, our_slots


@dataclass(frozen=True)
class Setting:
    """What a key may hold. `check` returns a reason to refuse, or None."""
    about: str
    check: Callable[[Any], str | None]


def _whole(value: Any, low: int, high: int, unit: str) -> str | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return f"must be a whole number of {unit}, not {json.dumps(value)}"
    if not low <= value <= high:
        return f"must be between {low} and {high} {unit}, not {value}"
    return None


def _fee(value: Any) -> str | None:
    problem = _whole(value, 0, 10000, "cents")
    if problem:
        return problem
    if 0 < value < 100:
        return f"{value} cents is less than a dollar; the fee is in cents (2500 is $25.00)"
    return None


def _ceiling(agreed: int) -> Callable[[Any], str | None]:
    """A sale-price ceiling: a multiple of the price step, at most the agreed `agreed` cents (lower is allowed)."""
    def check(value: Any) -> str | None:
        problem = _whole(value, PRICE_STEP_CENTS, agreed, "cents")
        if problem:
            return problem + (f" (the team agreed at most {agreed} cents; lower is allowed, and the API refuses "
                              f"any sale price over {MAX_SALE_PRICE_CENTS} cents)")
        if value % PRICE_STEP_CENTS:
            return f"must be a multiple of {PRICE_STEP_CENTS} cents, not {value}"
        return None
    return check


def _cap(value: Any) -> str | None:
    problem = _whole(value, 100, AGREED_WILDCARD_CAP_CENTS, "cents")
    return problem and problem + f" (the team agreed at most {AGREED_WILDCARD_CAP_CENTS} cents; lower is allowed)"


def _slot(value: Any) -> str | None:
    if not isinstance(value, str):
        return f"must be a slot name in quotes, like '\"37\"', not {json.dumps(value)}"
    # A selection on the machine; whether it is one of ours is checked against the stored assignment in `set`.
    if not (len(value) == 2 and value[0] in SHELVES and value[1] in COLUMNS):
        return f"must be a selection, shelf 1-5 then column 1-8 like \"15\", not {json.dumps(value)}"
    return None


# What the worker's scheduled agent runs do (`vending.agent_schedule`). The lower of this and the container's
# `AGENT_SCHEDULE_MODE` wins, so both must say so before the worker runs the agent.
SCHEDULE_MODES = ("off", "dry-run", "live")


def _schedule_mode(value: Any) -> str | None:
    if value not in SCHEDULE_MODES:
        return f"must be off, dry-run or live, not {json.dumps(value)}"
    return None


# Set once, never changed: the wildcard is a fixed slot.
WRITE_ONCE = frozenset({"wildcard_slot"})


# The loading chart's largest figure is 11 a column; a count far above that is almost always a
# typo (18 for an 8-can column), and every restock without `fill_to` would buy to it.
MAX_OVERRIDE_UNITS = 15


def _overrides(value: Any) -> str | None:
    if not isinstance(value, dict):
        return f'must be a JSON object of SKU to units, like {{"MONSTER-WHITE-16CAN": 8}}, not {json.dumps(value)}'
    for sku, units in value.items():
        if not sku.strip():
            return "has a blank SKU"
        if any(c.isspace() for c in sku):
            # SKUs have no spaces, and the lookup is exact, so this count would never be used.
            return f"SKU {json.dumps(sku)} has a space in it; SKUs have none, so it would never match"
        problem = _whole(units, 1, MAX_OVERRIDE_UNITS, "units")
        if problem:
            return (f"{sku} {problem} (the loading chart's largest figure is 11 a column, "
                    "so check the count)")
    return None


def _sku(sku: Any) -> str | None:
    if not isinstance(sku, str) or not sku.strip():
        return "a blank SKU"
    if any(c.isspace() for c in sku):
        return f"SKU {json.dumps(sku)} has a space in it; SKUs have none, so it would never match"
    return None


def _aliases(value: Any) -> str | None:
    """SKU -> the SKU of the same product that counts for it: one level only, so a lookup is a single step."""
    if not isinstance(value, dict):
        return ('must be a JSON object of SKU to SKU, like {"REDBULL-SUGARFREE-8_4CAN": "REDBULL-SUGARFRE-8_4CAN"}, '
                f"not {json.dumps(value)}")
    for alias, product in value.items():
        if not isinstance(product, str):
            return f"{alias} must be a SKU in quotes, not {json.dumps(product)}"
        for sku in (alias, product):
            problem = _sku(sku)
            if problem:
                return f"has {problem}" if problem.startswith("a blank") else problem
        if alias == product:
            return f"maps {alias} to itself"
        if product in value:
            return f"maps {alias} to {product}, but {product} is itself an alias; map both to the one SKU that counts"
    return None


KNOWN: dict[str, Setting] = {
    "capacity_overrides": Setting("units per column for products a person counted; replaces the whole "
                                  "object, wins over the loading chart (default none)", _overrides),
    "product_aliases": Setting("catalog SKUs that are the same product as another SKU, mapped to that SKU; the "
                               "planner counts them as one drink; replaces the whole object (default none)", _aliases),
    "price_ceiling_cents": Setting("highest sale price the planner allows (default and most 650)",
                                   _ceiling(AGREED_CEILING_CENTS)),
    "wildcard_slot": Setting("the one slot that may sell above the ceiling and tests products; set once after "
                             "the redraw and never changed (default none: no slot above the ceiling)", _slot),
    "wildcard_ceiling_cents": Setting("highest sale price for the one wildcard slot (default and most 800)",
                                      _ceiling(AGREED_WILDCARD_CEILING_CENTS)),
    "wildcard_purchase_cap_cents": Setting("most one purchase for the wildcard slot may cost at the worker's "
                                           "maximum (default and most 8200)", _cap),
    "tax_percent": Setting("sales tax inside the shelf price (default 8)", lambda v: _whole(v, 0, 100, "percent")),
    "midweek_fee_cents": Setting("what a midweek visit costs (default 2500)", _fee),
    "spendable_floor_cents": Setting("spendable every request must leave at its worst case: a midweek fee plus a "
                                     "week of model spend (default 5300)", lambda v: _whole(v, 0, 60000, "cents")),
    "model_run_cap_cents": Setting("most one agent cycle or replay may spend on model calls (default 100)",
                                   lambda v: _whole(v, 1, 60000, "cents")),
    "model_daily_cap_cents": Setting("most live model calls may spend in one Eastern day (default 400)",
                                     lambda v: _whole(v, 1, 60000, "cents")),
    "agent_schedule_mode": Setting("what the worker's scheduled agent runs do: off, dry-run or live; the lower of "
                                   "this and the container's AGENT_SCHEDULE_MODE wins (default off)", _schedule_mode),
    "model_test_daily_cap_cents": Setting("most test calls (replays, sweeps) may spend in one Eastern day "
                                          "(default 2000)", lambda v: _whole(v, 1, 60000, "cents")),
}


def is_halted(conn: psycopg.Connection, *, lock: bool = False) -> bool:
    """True unless the `halted` row holds exactly `false`.

    `lock=True` takes a row lock until the surrounding transaction ends, so a
    write that saw "not halted" finishes before a halt can land, and a halt
    that landed first blocks every write that starts after it.
    """
    sql = "select value from config where key = 'halted'"
    row = conn.execute(sql + (" for update" if lock else "")).fetchone()
    return row is None or row[0] is not False


def set_halted(conn: psycopg.Connection, halted: bool, who: str, *,
               lock_timeout_ms: int | None = None) -> None:
    """Flip the halt flag, and log the flip like any other write.

    An in-flight write holds the flag's row lock, so this waits for it to
    finish. With `lock_timeout_ms`, it gives up after that long instead and
    raises `psycopg.errors.LockNotAvailable`, so a caller can say it's waiting.
    """
    with conn.transaction():
        if lock_timeout_ms is not None:
            conn.execute(f"set local lock_timeout = {int(lock_timeout_ms)}")
        conn.execute(
            "insert into config (key, value, updated_by) values ('halted', %s, %s)"
            " on conflict (key) do update set value = excluded.value,"
            " updated_at = now(), updated_by = excluded.updated_by",
            (Jsonb(halted), who),
        )
        conn.execute(
            "insert into action_log (kind, tool, arguments, outcome, detail)"
            " values ('write', 'halt', %s, 'done', %s)",
            (Jsonb({"halted": halted}), f"halt turned {'on' if halted else 'off'} by {who}"),
        )


def get_value(conn: psycopg.Connection, key: str, default: Any = None) -> Any:
    """One setting's JSON value, or `default` when the row is missing."""
    row = conn.execute("select value from config where key = %s", (key,)).fetchone()
    return default if row is None else row[0]


def set_value(conn: psycopg.Connection, key: str, value: Any, who: str) -> None:
    """Set one known setting, after checking its type and range, logged like any other write.

    The halt flag has its own function. An unknown key is refused rather than
    saved silently, so a misspelt key can't sit there doing nothing. A key in
    `WRITE_ONCE` that is already set refuses any other value; setting the same
    value again changes nothing. `wildcard_slot` must also be one of ours in the
    stored slot assignment, checked before either.
    """
    if key == "halted":
        raise ValueError("use set_halted (python -m vending.halt) for the halt flag")
    if key not in KNOWN:
        raise ValueError(f"unknown setting {key!r}; the settings are: "
                         + "; ".join(f"{k} ({s.about})" for k, s in KNOWN.items()))
    problem = KNOWN[key].check(value)
    if problem:
        raise ValueError(f"{key} {problem}")
    if not who.strip():
        raise ValueError("--by needs your name")
    with conn.transaction():
        if key in WRITE_ONCE:
            # The lock makes two first settings take turns, so the second sees the first.
            conn.execute("select pg_advisory_xact_lock(hashtext('config:' || %s))", (key,))
            row = conn.execute("select value from config where key = %s", (key,)).fetchone()
            if row is not None and row[0] != value:
                raise ValueError(
                    f"{key} is already {json.dumps(row[0])} and never changes: the wildcard is a fixed slot. "
                    f"To change it, a person deletes the row by hand (delete from config where key = '{key}') "
                    "and says why in the team's thread, then sets it again")
            # One of ours now, by the stored server assignment: checked before the same value is taken as done,
            # so a slot that has left the assignment is never confirmed.
            try:
                ours = our_slots(conn)
            except SlotsNotRecorded as error:
                raise ValueError(f"{key}: {error}") from None
            if value not in ours:
                raise ValueError(f"{key} {json.dumps(value)} isn't one of ours in the stored slot assignment "
                                 f"({', '.join(ours)}); check it with python -m vending.slots show")
            if row is not None:
                return
        conn.execute(
            "insert into config (key, value, updated_by) values (%s, %s, %s)"
            " on conflict (key) do update set value = excluded.value,"
            " updated_at = now(), updated_by = excluded.updated_by",
            (key, Jsonb(value), who.strip()),
        )
        conn.execute(
            "insert into action_log (kind, tool, arguments, outcome, detail)"
            " values ('write', 'config.set', %s, 'done', %s)",
            (Jsonb({"key": key, "value": value}), f"config {key} set to {json.dumps(value)} by {who.strip()}"),
        )


def main(argv: list[str] | None = None) -> int:
    from vending import db

    parser = argparse.ArgumentParser(prog="python -m vending.config", description="Run-time settings.")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("show", help="every setting")
    setter = sub.add_parser("set", help="set one setting to a JSON value (10, 2500, true, \"text\")")
    setter.add_argument("key")
    setter.add_argument("value", help="JSON; a bare word is taken as a string")
    setter.add_argument("--by", required=True, help="your name, for the log")
    args = parser.parse_args(argv)
    with db.connect() as conn:
        behind = db.pending(conn)
        if behind:
            print(f"The database is missing migrations: {', '.join(behind)}. "
                  "Check DATABASE_URL points where you mean, then run: python -m vending.db migrate",
                  file=sys.stderr)
            return 2
        if args.action == "set":
            try:
                value = json.loads(args.value)
            except json.JSONDecodeError:
                value = args.value
            before = get_value(conn, args.key, None)
            try:
                set_value(conn, args.key, value, args.by)
            except ValueError as error:
                print(error, file=sys.stderr)
                return 1
            print(f"{args.key} = {json.dumps(value)}")
            if args.key in ("capacity_overrides", "product_aliases") and isinstance(before, dict):
                # `set` replaces the whole object, so name any entry it just threw away.
                dropped = {sku: units for sku, units in before.items() if sku not in value}
                if dropped:
                    what = "counts" if args.key == "capacity_overrides" else "aliases"
                    print(f"Removed {what} that were in the old value: {json.dumps(dropped)}. "
                          "To keep them, set the object again with them included.", file=sys.stderr)
            return 0
        for key, value, at, by in conn.execute("select key, value, updated_at, updated_by from config order by key"):
            unused = "" if key in KNOWN or key == "halted" else "  [no longer used]"
            print(f"{key} = {json.dumps(value)}  ({at:%Y-%m-%d %H:%M}, {by}){unused}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
