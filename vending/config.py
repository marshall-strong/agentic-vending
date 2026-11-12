"""The `config` table: settings the code reads at run time, starting with the halt flag.

    python -m vending.config show
    python -m vending.config set capacity_overrides '{"MONSTER-WHITE-16CAN": 8}' --by marshall
    python -m vending.config set price_ceiling_cents 600 --by marshall

The halt flag fails closed: a missing row, or anything that isn't JSON
`false`, counts as halted. Every other key is a JSON value the planner reads
through `vending.planning.planner_config`. Only the keys in `KNOWN` can be
set, each checked for type and range before it is stored, because a wrong
value here is charged for real: `midweek_fee_cents 25` (dollars typed) would
bill 25 cents a visit, and a ceiling over the API's $10.00 would break every
plan. Every change is logged with who made it. The CLI never migrates.
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

from vending.planner import MAX_SALE_PRICE_CENTS
from vending.pricing import PRICE_STEP_CENTS


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


def _ceiling(value: Any) -> str | None:
    problem = _whole(value, PRICE_STEP_CENTS, MAX_SALE_PRICE_CENTS, "cents")
    if problem:
        return problem + f" (the API refuses any sale price over {MAX_SALE_PRICE_CENTS} cents)"
    if value % PRICE_STEP_CENTS:
        return f"must be a multiple of {PRICE_STEP_CENTS} cents, not {value}"
    return None


def _overrides(value: Any) -> str | None:
    if not isinstance(value, dict):
        return f'must be a JSON object of SKU to units, like {{"MONSTER-WHITE-16CAN": 8}}, not {json.dumps(value)}'
    for sku, units in value.items():
        if not sku.strip():
            return "has a blank SKU"
        problem = _whole(units, 1, 30, "units")
        if problem:
            return f"{sku} {problem}"
    return None


KNOWN: dict[str, Setting] = {
    "capacity_overrides": Setting("units per column for products a person counted; replaces the whole "
                                  "object, wins over the loading chart (default none)", _overrides),
    "price_ceiling_cents": Setting("highest sale price the planner allows (default 600)", _ceiling),
    "tax_percent": Setting("sales tax inside the shelf price (default 8)", lambda v: _whole(v, 0, 100, "percent")),
    "midweek_fee_cents": Setting("what a midweek visit costs (default 2500)", _fee),
    "model_run_cap_cents": Setting("most one agent cycle or replay may spend on model calls (default 100)",
                                   lambda v: _whole(v, 1, 60000, "cents")),
    "model_daily_cap_cents": Setting("most all model calls may spend in one Eastern day (default 400)",
                                     lambda v: _whole(v, 1, 60000, "cents")),
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
    saved silently, so a misspelt key can't sit there doing nothing.
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
            try:
                set_value(conn, args.key, value, args.by)
            except ValueError as error:
                print(error, file=sys.stderr)
                return 1
            print(f"{args.key} = {json.dumps(value)}")
            return 0
        for key, value, at, by in conn.execute("select key, value, updated_at, updated_by from config order by key"):
            unused = "" if key in KNOWN or key == "halted" else "  [no longer used]"
            print(f"{key} = {json.dumps(value)}  ({at:%Y-%m-%d %H:%M}, {by}){unused}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
