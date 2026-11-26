"""`scripts/swap_model_key.ps1` and its checks, `scripts/check_model_key.py` (#262). Nothing here reaches Azure or the
API: the key check runs on a fake client, and the script itself is read, not run."""

import importlib.util
import re
from pathlib import Path

import pytest

from tests.test_credit_wait import CreditClient, bad_key, no_credit, unreachable

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "swap_model_key.ps1"
KEY = "sk-ant-api03-" + "x" * 40     # not a key: the shape the check reads


def _module():
    spec = importlib.util.spec_from_file_location("check_model_key", ROOT / "scripts" / "check_model_key.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check = _module()


@pytest.fixture(autouse=True)
def _own_key_env(monkeypatch):
    """`check_key` sets the key's variable in its own process; the test puts it back."""
    from vending.harness import DEFAULT_KEY_ENV

    monkeypatch.setenv(DEFAULT_KEY_ENV, "sk-before")


@pytest.mark.parametrize("text, read", [
    (KEY, KEY), (KEY + "\n", KEY), ("\n  " + KEY + "  \r\n\n", KEY), ("﻿" + KEY, KEY),
    ("", None), ("hello", None), (KEY + "\n" + KEY, None), ("sk-ant- x", None),
])
def test_the_key_file_holds_the_key_alone(tmp_path, text, read):
    path = tmp_path / "key.txt"
    path.write_text(text, encoding="utf-8")
    assert check.read_key(path) == read


def _paths(tmp_path):
    key_file = tmp_path / "key.txt"
    key_file.write_text(KEY + "\n", encoding="utf-8")
    return key_file, tmp_path / ".env", tmp_path / "out", tmp_path / "token"


def test_an_unreadable_key_file_or_env_stops_at_once(tmp_path, capsys):
    key_file, env, out, token = _paths(tmp_path)
    key_file.write_text("two\nlines", encoding="utf-8")
    assert check.main(["take", str(key_file), str(env), str(out), str(token)]) == check.USAGE
    key_file.write_text(KEY, encoding="utf-8")
    assert check.main(["take", str(key_file), str(env), str(out), str(token)]) == check.USAGE     # no .env
    assert check.main(["take", str(key_file)]) == check.USAGE
    assert KEY not in capsys.readouterr().out and not out.exists()


# --- The script itself (read, not run) ----------------------------------------------------------------------

def test_the_script_sets_the_secret_the_container_reads_from_a_file():
    text = SCRIPT.read_text(encoding="utf-8")
    # No hard-coded secret name: the live app reads `anthropic-api-key`, a -SetSecrets deploy maps
    # `vending-anthropic-api-key`, so the script reads the name from the app's own mapping (#262).
    assert "[string]$SecretName," in text and "vending-anthropic-api-key\"" not in text
    assert "env[?name=='$KeyEnv'].secretRef" in text and "$SecretName = $reads[0]" in text
    assert '--secrets "$SecretName=@$keyCopy"' in text          # az reads the value from the file
    assert "Remove-Item -Recurse -Force $dir" in text            # in a finally: the copy never stays
    assert "check_model_key.py" in text and "revision restart" in text and "model_key" in text
    # The key is never asked for, never on a command line, never printed.
    assert "Read-Host" not in text and "Get-Content" not in text
    assert not re.search(r"Write-(Host|Output)[^\n]*\$key(?!File|Copy|Env)", text, re.IGNORECASE)


def test_the_deploy_doc_says_how_to_swap_the_key():
    doc = (ROOT / "docs" / "deploy.md").read_text(encoding="utf-8")
    assert r".\scripts\swap_model_key.ps1 -KeyFile" in doc


# --- With the database ----------------------------------------------------------------------------------------

psycopg = pytest.importorskip("psycopg")

from tests.conftest import open_in  # noqa: E402

LEASE = ("update agent_run_lease set token = gen_random_uuid(), run_id = 7,"
         " expires_at = clock_timestamp() + interval '1 hour' where one")


@pytest.fixture
def connect(schema_url):
    return lambda: open_in(*schema_url)


def _take(tmp_path, connect, client):
    key_file, env, out, token = _paths(tmp_path)
    code = check.main(["take", str(key_file), str(env), str(out), str(token)], client=client, connect=connect)
    return code, out, token


def held(conn):
    return conn.execute("select token is not null and expires_at > clock_timestamp() from agent_run_lease").fetchone()[0]


def test_the_swap_holds_the_run_lease_until_it_gives_it_back(tmp_path, connect, conn, capsys):
    """While the swap holds the lease no run starts; a live scheduled time waits for it (`lease_busy`)."""
    from vending import agent_schedule, run_lease

    code, out, token = _take(tmp_path, connect, CreditClient())
    assert code == 0 and out.read_bytes() == KEY.encode() and held(conn)
    assert agent_schedule.lease_busy(conn) is not None
    assert isinstance(run_lease.take(conn, deadline=60), run_lease.Busy)
    assert check.main(["renew", "unused", str(token)], connect=connect) == 0
    assert check.main(["give-back", "unused", str(token)], connect=connect) == 0
    assert not held(conn) and agent_schedule.lease_busy(conn) is None
    assert check.main(["renew", "unused", str(token)], connect=connect) == check.BUSY     # gone: renew says so
    assert KEY not in capsys.readouterr().out


@pytest.mark.parametrize("error, code", [(no_credit, 5), (bad_key, 6), (unreachable, 7)])
def test_a_key_without_credit_or_refused_gives_the_lease_back_and_writes_nothing(tmp_path, connect, conn, capsys,
                                                                                error, code):
    got, out, token = _take(tmp_path, connect, CreditClient(error()))
    assert got == code and not out.exists() and not token.exists() and not held(conn)
    said = capsys.readouterr().out
    assert "STOP" in said and KEY not in said and "Not swapped" in said


def test_a_held_lease_stops_before_the_key_is_checked(tmp_path, connect, conn):
    conn.execute(LEASE)
    client = CreditClient()
    code, out, _ = _take(tmp_path, connect, client)
    assert code == check.BUSY and client.counted == [] and not out.exists()
    assert conn.execute("select run_id from agent_run_lease").fetchone()[0] == 7      # the run's lease is untouched


def test_a_running_scheduled_time_stops_the_swap(tmp_path, connect, conn, capsys):
    conn.execute("insert into agent_schedule (due_at, latest_at, mode, outcome) values"
                 " ('2026-12-03T17:00Z', '2026-12-04T01:00Z', 'live', 'running')")
    client = CreditClient()
    code, out, _ = _take(tmp_path, connect, client)
    assert code == check.BUSY and client.counted == [] and not held(conn)
    assert "Thu 03 Dec 12:00 PM ET says running" in capsys.readouterr().out
