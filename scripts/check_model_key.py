"""The checks `scripts/swap_model_key.ps1` makes before it swaps the model key (#262).

    python scripts/check_model_key.py <key file> <.env> <out file>

1. **No agent run going.** Reads the team database (`DATABASE_URL` from the `.env` given, parsed as the app
   parses it) and refuses while an agent run holds the run lease (`agent_run_lease`) or a scheduled time says
   `running` (`agent_schedule`). A restart mid-run would cut the run off, and a scheduled time is never run again.
2. **The new key can pay.** One free `messages.count_tokens` call with the new key and the agent's model, through
   `vending.harness.check_credit` (never a paid request).
3. Writes the key, trimmed, to `<out file>` for `az ... --secrets name=@<out file>`, so it is never on a command
   line.

Prints what it checked, never the key or any part of it. Exit codes: 0 ready to swap; 2 wrong arguments or an
unreadable key file; 3 an agent run is going; 4 the database couldn't be read; 5 the key has no credit; 6 the API
refused the key; 7 the API couldn't be reached or refused the check for another reason.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vending.settings import read_env_file  # noqa: E402

EASTERN = ZoneInfo("America/New_York")
READY, USAGE, BUSY, NO_DATABASE, NO_CREDIT, REFUSED, UNCHECKED = 0, 2, 3, 4, 5, 6, 7


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


def busy(conn) -> str | None:
    """Why the key mustn't be swapped now (an agent run is going), or None."""
    lease = conn.execute("select run_id, expires_at from agent_run_lease"
                         " where token is not null and expires_at > clock_timestamp()").fetchone()
    if lease is not None:
        who = f"agent run {lease[0]}" if lease[0] is not None else "an agent run"
        return f"{who} holds the run lease (until {_et(lease[1])}); a restart would cut it off"
    running = conn.execute("select due_at from agent_schedule where outcome = 'running' order by due_at").fetchone()
    if running is not None:
        return (f"the scheduled time due {_et(running[0])} says running; a restart would cut it off, "
                "and a scheduled time is never run again")
    return None


def check_database(url: str) -> tuple[int, str]:
    import psycopg

    from vending import db

    try:
        with psycopg.connect(url, autocommit=True, connect_timeout=db.CONNECT_TIMEOUT,
                             options=db.session_options(url, "-c statement_timeout=10000")) as conn:
            why = busy(conn)
    except (psycopg.Error, ValueError) as error:
        return NO_DATABASE, f"couldn't read the team database ({type(error).__name__})"
    if why is not None:
        return BUSY, f"not now: {why}. Try again when no agent run is going."
    return READY, "no agent run is going (no run lease held, no scheduled time running)"


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
        return REFUSED, f"the API refused the new key: {error}. Not swapped."
    except harness.HarnessError as error:
        return UNCHECKED, f"the new key couldn't be checked: {error}. Not swapped."
    return READY, f"the new key has credit (a free count_tokens call for {agent.DEFAULT_MODEL} was answered)"


def main(argv: list[str], client=None) -> int:
    if len(argv) != 3:
        print(__doc__)
        return USAGE
    key_file, env_file, out_file = (Path(a) for a in argv)
    key = read_key(key_file)
    if key is None:
        print(f"{key_file} must hold the key alone, on one line (it starts sk-ant-). Not swapped.")
        return USAGE
    if not env_file.exists():
        print(f"{env_file} does not exist, so the team database can't be read. Not swapped.")
        return USAGE
    url = read_env_file(env_file).get("DATABASE_URL")
    if not url:
        print(f"{env_file} has no DATABASE_URL, so the team database can't be read. Not swapped.")
        return USAGE
    for check in (lambda: check_database(url), lambda: check_key(key, client)):
        code, said = check()
        print(("OK: " if code == READY else "STOP: ") + said)
        if code != READY:
            return code
    # Bytes, so no newline is added and no platform encoding gets a say (as scripts/write_secret_files.py).
    out_file.write_bytes(key.encode("utf-8"))
    return READY


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
