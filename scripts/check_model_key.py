"""The checks `scripts/swap_model_key.ps1` makes around a swap of the model key (#262).

    python scripts/check_model_key.py take <key file> <.env> <out file> <token file>
    python scripts/check_model_key.py renew <.env> <token file>
    python scripts/check_model_key.py give-back <.env> <token file>

`take`:

1. **Holds the run lease for the swap.** In the team database (`DATABASE_URL` from the `.env` given, parsed as the
   app parses it) it takes the agent run lease (`vending.run_lease.take`), as an agent run does, and refuses while
   an agent run holds it or a scheduled time says `running` (`agent_schedule`): a restart mid-run would cut the run
   off, and a scheduled time is never run again. While the swap holds the lease, no run can start: a live scheduled
   time waits for it, without being claimed (`vending.agent_schedule.lease_busy`), and starts after the swap. The
   lease's token goes in `<token file>`; `renew` keeps it while the revision restarts, `give-back` frees it. If the
   script dies, the lease expires by itself (`run_lease.TTL`).
2. **Checks the new key can pay.** One free `messages.count_tokens` call with the new key and the agent's model,
   through `vending.harness.check_credit` (never a paid request). If it can't, the lease is given back.
3. Writes the key, trimmed, to `<out file>` for `az ... --secrets name=@<out file>`, so it is never on a command
   line.

Prints what it checked, never the key or any part of it. Exit codes: 0 done; 2 wrong arguments or an unreadable
key file; 3 an agent run is going (or, for `renew`, the swap no longer holds the lease); 4 the database couldn't be
read; 5 the key has no credit; 6 the API refused the key; 7 the API couldn't be reached or refused the check for
another reason.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vending.settings import read_env_file  # noqa: E402

EASTERN = ZoneInfo("America/New_York")
READY, USAGE, BUSY, NO_DATABASE, NO_CREDIT, REFUSED, UNCHECKED = 0, 2, 3, 4, 5, 6, 7
# The most the swap may hold the lease: the checks, the az steps and the wait for the restarted revision
# (swap_model_key.ps1 waits 600 s by default). `run_lease.GRACE` is added on top.
SWAP_SECONDS = 20 * 60


def read_key(path: Path) -> str | None:
    """The key in `path`: its one non-blank line, trimmed. None if it isn't one line that looks like a key."""
    try:
        lines = [line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    except (OSError, UnicodeDecodeError):
        return None
    if len(lines) != 1 or not lines[0].startswith("sk-ant-") or any(c.isspace() for c in lines[0]):
        return None
    return lines[0]


def _et(moment) -> str:
    local = moment.astimezone(EASTERN)
    return f"{local:%a %d %b} {local.hour % 12 or 12}:{local:%M %p} ET"


def running(conn) -> str | None:
    """Why the key mustn't be swapped now: a scheduled time says `running`. None when none does."""
    row = conn.execute("select due_at from agent_schedule where outcome = 'running' order by due_at").fetchone()
    if row is None:
        return None
    return (f"the scheduled time due {_et(row[0])} says running; a restart would cut it off, and a scheduled time "
            "is never run again")


def take_lease(conn) -> tuple[int, str, uuid.UUID | None]:
    """Take the run lease for the swap, in one transaction with its row locked: (code, what happened, token)."""
    from vending import run_lease

    taken = run_lease.take(conn, deadline=SWAP_SECONDS, authorize=running)
    if isinstance(taken, run_lease.Busy):
        who = f"agent run {taken.run_id}" if taken.run_id is not None else "an agent run"
        return BUSY, f"not now: {who} holds the run lease; a restart would cut it off. Try again when it ends.", None
    if isinstance(taken, run_lease.Refused):
        return BUSY, f"not now: {taken.detail}. Try again when it ends.", None
    return READY, "no agent run is going; the swap holds the run lease, so none starts until it is done", taken.token


def _connect(url: str):
    import psycopg

    from vending import db

    return psycopg.connect(url, autocommit=True, connect_timeout=db.CONNECT_TIMEOUT,
                           options=db.session_options(url, "-c statement_timeout=10000"))


def check_key(key: str, client=None) -> tuple[int, str]:
    """One free count_tokens call with `key`. `client` (a test's) stands in for the real one."""
    from vending import agent, harness

    # The harness checks the client pays with the key in VENDING_ANTHROPIC_API_KEY, so this process's is the new key.
    os.environ[harness.DEFAULT_KEY_ENV] = key
    try:
        harness.check_credit(client if client is not None else harness.anthropic_client(api_key=key),
                             agent.DEFAULT_MODEL)
    except harness.NoCredit:
        return NO_CREDIT, "the new key has no credit: the API says its credit balance is too low. Not swapped."
    except harness.KeyRefused as error:
        return REFUSED, f"the new key was refused: {error}. Not swapped."
    except harness.HarnessError as error:
        return UNCHECKED, f"the new key couldn't be checked: {error}. Not swapped."
    return READY, f"the new key has credit (a free count_tokens call for {agent.DEFAULT_MODEL} was answered)"


def _url(env_file: Path) -> str | None:
    if not env_file.exists():
        print(f"{env_file} does not exist, so the team database can't be read. Not swapped.")
        return None
    url = read_env_file(env_file).get("DATABASE_URL")
    if not url:
        print(f"{env_file} has no DATABASE_URL, so the team database can't be read. Not swapped.")
    return url or None


def take(key_file: Path, env_file: Path, out_file: Path, token_file: Path, client=None, connect=None) -> int:
    import psycopg

    key = read_key(key_file)
    if key is None:
        print(f"{key_file} must hold the key alone, on one line (it starts sk-ant-). Not swapped.")
        return USAGE
    url = None if connect is not None else _url(env_file)
    if connect is None and url is None:
        return USAGE
    connect = connect or (lambda: _connect(url))
    try:
        with connect() as conn:
            code, said, token = take_lease(conn)
    except (psycopg.Error, ValueError) as error:
        print(f"STOP: couldn't read the team database ({type(error).__name__}). Not swapped.")
        return NO_DATABASE
    print(("OK: " if code == READY else "STOP: ") + said)
    if code != READY:
        return code
    code, said = check_key(key, client)
    print(("OK: " if code == READY else "STOP: ") + said)
    if code != READY:
        give_back(connect, token)
        return code
    token_file.write_text(str(token), encoding="ascii")
    # Bytes, so no newline is added and no platform encoding gets a say (as scripts/write_secret_files.py).
    out_file.write_bytes(key.encode("utf-8"))
    return READY


def renew(connect, token: uuid.UUID) -> int:
    from vending import run_lease

    with connect() as conn:
        return READY if run_lease.renew(conn, token) else BUSY


def give_back(connect, token: uuid.UUID) -> None:
    from vending import run_lease

    try:
        with connect() as conn:
            run_lease.give_back(conn, token)
        print("OK: the swap gave back the run lease")
    except Exception as error:      # noqa: BLE001 - it expires by itself
        print(f"The run lease couldn't be given back ({type(error).__name__}); it expires by itself within 10 min.")


def main(argv: list[str], client=None, connect=None) -> int:
    command, args = (argv[0], argv[1:]) if argv else ("", [])
    if command == "take" and len(args) == 4:
        return take(*(Path(a) for a in args), client=client, connect=connect)
    if command in ("renew", "give-back") and len(args) == 2:
        try:
            token = uuid.UUID(Path(args[1]).read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            print(f"No lease token in {args[1]}.")
            return USAGE
        if connect is None:
            url = _url(Path(args[0]))
            if url is None:
                return USAGE
            connect = lambda: _connect(url)  # noqa: E731
        if command == "renew":
            try:
                return renew(connect, token)
            except Exception as error:      # noqa: BLE001 - the script goes on waiting; the lease lasts 10 min
                print(f"The run lease couldn't be renewed just now ({type(error).__name__}).")
                return NO_DATABASE
        give_back(connect, token)
        return READY
    print(__doc__)
    return USAGE


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
