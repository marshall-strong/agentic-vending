"""The bench's records: what a run ran on, and every answer in it, in `bench_runs` and `bench_results`.

What a run ran on is kept as hashes, so a change in score can be put down to a
change in the inputs: `prompt_hash` (the model's system prompt and plan schema),
`cases_hash` (every case's question, good answer and weeks) and the judge's
`judge_hash`, beside the git commit.

Money is Decimal. A sweep row's cost is a float made from an exact Decimal of a
few digits, so `Decimal(str(cost))` gives the exact figure back.
"""

from __future__ import annotations

import hashlib
import json
import socket
import subprocess
from decimal import Decimal

import psycopg
from psycopg.types.json import Jsonb

from vending import settings
from vending.replay import model
from vending.replay.cases import Case
from vending.replay.model import state_json

CONTROL = "baseline"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def prompt_hash() -> str:
    """What the model is asked: its system prompt and the plan schema it must answer in."""
    return _sha(model.SYSTEM + json.dumps(model.PLAN_SCHEMA, sort_keys=True))


def cases_hash(cases: list[Case]) -> str:
    """What the model is tested on, in order. Editing or adding a case restarts the trend."""
    parts = []
    for c in cases:
        nxt = None if c.next_week is None else state_json(c.next_week)
        parts += [c.name, c.question, c.good_answer,
                  json.dumps(state_json(c.state), sort_keys=True), json.dumps(nxt, sort_keys=True)]
    return _sha("\x1f".join(parts))


def git_info() -> dict:
    """The commit, branch and whether the tree had uncommitted changes. None for any git can't give."""
    def git(*args):
        try:
            done = subprocess.run(["git", "-C", str(settings.REPO_ROOT), *args], capture_output=True,
                                  text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout.strip() if done.returncode == 0 else None
    status = git("status", "--porcelain")
    return {"git_sha": git("rev-parse", "HEAD"), "git_branch": git("rev-parse", "--abbrev-ref", "HEAD"),
            "git_dirty": None if status is None else bool(status)}


def has_tables(conn: psycopg.Connection) -> bool:
    return conn.execute("select to_regclass('bench_runs') is not null").fetchone()[0]


RUN_FIELDS = ("status", "git_sha", "git_branch", "git_dirty", "prompt_hash", "cases_hash", "cases", "configs",
              "repeats", "judge", "judge_hash", "budget_usd", "spent_usd", "note", "source", "results_dir",
              "recorded_by", "host", "started_at", "finished_at", "judge_notes")


def start_run(conn: psycopg.Connection, **fields) -> int:
    """A bench_runs row, written before anything is spent, so a run that dies still shows."""
    fields = {"host": socket.gethostname(), **fields}
    if "judge_notes" in fields:
        fields["judge_notes"] = Jsonb(fields["judge_notes"])
    unknown = set(fields) - set(RUN_FIELDS)
    if unknown:
        raise ValueError(f"not bench_runs columns: {', '.join(sorted(unknown))}")
    cols = list(fields)
    row = conn.execute(f"insert into bench_runs ({', '.join(cols)}) values ({', '.join(['%s'] * len(cols))})"
                       " returning id", [fields[c] for c in cols]).fetchone()
    return row[0]


def finish_run(conn: psycopg.Connection, run_id: int, status: str, spent: Decimal,
               judge_notes: dict | None = None) -> None:
    conn.execute("update bench_runs set status = %s, spent_usd = %s, finished_at = now(),"
                 " judge_notes = coalesce(%s, judge_notes) where id = %s",
                 (status, spent, None if judge_notes is None else Jsonb(judge_notes), run_id))


def _decimal(value) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def record(row: dict, plan) -> dict:
    """A bench_results row from a sweep row (`Row.__dict__`, or a line of results.jsonl)."""
    config, _, rep = row["config"].partition("#")
    return {
        "config": config, "model": row["model"], "effort": row["effort"],
        "repeat": int(row.get("repeat") or (rep or 1)), "case_name": row["case"],
        "control": config == CONTROL, "status": row["status"], "valid": row.get("valid"),
        "failed": list(row.get("failed") or []), "checks": row.get("checks") or {},
        "slots_scored": row.get("scored"), "short": row.get("short"), "unsold": row.get("unsold"),
        "margin": _decimal(row.get("margin")), "cost_usd": _decimal(row.get("cost_usd")) or Decimal(0),
        "input_tokens": row.get("input_tokens"), "output_tokens": row.get("output_tokens"),
        "seconds": _decimal(row.get("seconds")), "max_tokens": row.get("max_tokens"), "plan": plan,
        "decision": None, "reasoning": None, "invented": None, "judge_why": None, "judge_ok": None,   # not judged yet
    }


RESULT_FIELDS = ("config", "model", "effort", "repeat", "case_name", "control", "status", "valid", "failed",
                 "checks", "slots_scored", "short", "unsold", "margin", "cost_usd", "input_tokens",
                 "output_tokens", "seconds", "max_tokens", "plan", "decision", "reasoning", "invented",
                 "judge_why", "judge_ok")


def store_results(conn: psycopg.Connection, run_id: int, records: list[dict]) -> None:
    cols = ["run_id", *RESULT_FIELDS]
    sql = (f"insert into bench_results ({', '.join(cols)}) values ({', '.join(['%s'] * len(cols))})"
           " on conflict (run_id, config, repeat, case_name) do nothing")
    with conn.transaction():
        with conn.cursor() as cur:
            cur.executemany(sql, [[run_id, *(_db(c, r.get(c)) for c in RESULT_FIELDS)] for r in records])


JSON_FIELDS = ("checks", "plan")


def _db(column: str, value):
    return Jsonb(value) if column in JSON_FIELDS and value is not None else value


def set_grade(conn: psycopg.Connection, run_id: int, config: str, repeat: int, case: str, *, decision: int,
              reasoning: int, invented: bool, why: str, ok: bool | None) -> None:
    conn.execute("update bench_results set decision = %s, reasoning = %s, invented = %s, judge_why = %s,"
                 " judge_ok = %s where run_id = %s and config = %s and repeat = %s and case_name = %s",
                 (decision, reasoning, invented, why, ok, run_id, config, repeat, case))


def runs(conn: psycopg.Connection, status: str | None = None) -> list[dict]:
    """Every bench run in the order they ran, or only those with this status.

    By start time, not id: an old sweep imported after newer runs gets the next id
    but belongs where it ran, or it would read as the newest run.
    """
    cur = conn.execute("select id, " + ", ".join(RUN_FIELDS) + " from bench_runs"
                       " where %s::text is null or status = %s order by started_at, id", (status, status))
    return [dict(zip(("id", *RUN_FIELDS), r)) for r in cur.fetchall()]


def results(conn: psycopg.Connection, run_id: int) -> list[dict]:
    cur = conn.execute("select " + ", ".join(RESULT_FIELDS) + " from bench_results where run_id = %s"
                       " order by id", (run_id,))
    return [dict(zip(RESULT_FIELDS, r)) for r in cur.fetchall()]
