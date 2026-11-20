"""Load, pick or list first-fill plans for the dashboard."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from vending import db, plan_rounds, settings, slots
from vending.write_gate import log


def load(conn: psycopg.Connection, directory: Path, round_id: str, by: str) -> int:
    """Store the two JSON objects that passed the round loader's checks for the current assignment."""
    files = {rid: (plan, projection) for rid, plan, projection in plan_rounds.discover(directory)}
    if round_id not in files:
        raise plan_rounds.RoundError(f"No plan file for round {round_id} in the folder.")
    plan_file, projection_file = files[round_id]
    with conn.transaction():
        assigned = slots.our_slots(conn)
        checked, plan, projection = plan_rounds.read_round(round_id, plan_file, projection_file, assigned)
        source = " + ".join(name for name in (checked.plan_file, checked.projection_file) if name)
        plan_id = conn.execute(
            "insert into first_fill_plans (loaded_by, label, source_note, plan, projection, slots)"
            " values (%s, %s, %s, %s, %s, %s) returning id",
            (by, round_id, source, Jsonb(plan), Jsonb(projection), list(assigned))).fetchone()[0]
        log(conn, "write", "plan_store.load", {"id": plan_id, "by": by}, "done", f"Loaded first-fill plan #{plan_id}.")
    return plan_id


def pick(conn: psycopg.Connection, plan_id: int, by: str) -> None:
    """Replace the pick and record the change in one transaction."""
    with conn.transaction():
        # Serialize picks even when there is no current pick. Normal readers can still select.
        conn.execute("lock table first_fill_plans in share row exclusive mode")
        if conn.execute("select 1 from first_fill_plans where id = %s", (plan_id,)).fetchone() is None:
            raise LookupError(f"No first-fill plan with ID {plan_id}.")
        conn.execute("update first_fill_plans set picked = false where picked and id <> %s", (plan_id,))
        conn.execute("update first_fill_plans set picked = true where id = %s", (plan_id,))
        log(conn, "write", "plan_store.pick", {"id": plan_id, "by": by}, "done", f"Picked first-fill plan #{plan_id}.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vending.plan_store", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    loading = commands.add_parser("load", help="check a plan and store it")
    loading.add_argument("directory", type=Path)
    loading.add_argument("--round", required=True, dest="round_id")
    picking = commands.add_parser("pick", help="pick a stored plan by ID")
    picking.add_argument("id", type=int)
    for writing in (loading, picking):
        writing.add_argument("--by", required=True, help="your name, for the log")
    commands.add_parser("list", help="list stored plans, newest first")
    args = parser.parse_args(argv)
    if args.command != "list" and not args.by.strip():
        parser.error("--by NAME is required")
    try:
        with db.connect() as conn:
            if args.command == "load":
                plan_id = load(conn, args.directory, args.round_id, args.by.strip())
                print(f"Loaded first-fill plan #{plan_id}.")
            elif args.command == "pick":
                pick(conn, args.id, args.by.strip())
                print(f"Picked first-fill plan #{args.id}.")
            else:
                rows = conn.execute("select id, picked, label, loaded_at, loaded_by, source_note"
                                    " from first_fill_plans order by loaded_at desc, id desc").fetchall()
                print("ID  Status  Label  Loaded (UTC)  By  Source")
                for plan_id, picked, label, at, by, source in rows:
                    print(f"{plan_id}  {'picked' if picked else 'unpicked'}  {label}  {at.isoformat()}  {by}  {source}")
                if not rows:
                    print("No first-fill plans loaded.")
    except (LookupError, ValueError, OSError, settings.SettingsError, slots.SlotsNotRecorded) as error:
        print(error, file=sys.stderr)
        return 2
    except psycopg.Error as error:
        print(f"{type(error).__name__}: {str(error).strip().splitlines()[0]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
