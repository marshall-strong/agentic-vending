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


def _ready(tmp_path, monkeypatch, client):
    key_file, env_file, out = tmp_path / "key.txt", tmp_path / ".env", tmp_path / "out"
    key_file.write_text(KEY + "\n", encoding="utf-8")
    env_file.write_text("DATABASE_URL=postgresql://nobody@127.0.0.1:1/none\n", encoding="utf-8")
    monkeypatch.setattr(check, "check_database", lambda url: (check.READY, "no agent run is going"))
    return [str(key_file), str(env_file), str(out)], out


@pytest.mark.parametrize("error, code", [(no_credit, 5), (bad_key, 6), (unreachable, 7)])
def test_a_key_without_credit_or_refused_is_never_written(tmp_path, monkeypatch, capsys, error, code):
    args, out = _ready(tmp_path, monkeypatch, None)
    assert check.main(args, client=CreditClient(error())) == code
    assert not out.exists()
    said = capsys.readouterr().out
    assert "STOP" in said and KEY not in said and "Not swapped" in said


def test_a_key_with_credit_is_written_for_az_without_a_newline(tmp_path, monkeypatch, capsys):
    args, out = _ready(tmp_path, monkeypatch, None)
    client = CreditClient()
    assert check.main(args, client=client) == 0
    assert out.read_bytes() == KEY.encode()
    assert len(client.counted) == 1 and set(client.counted[0]) == {"model", "messages"}
    assert KEY not in capsys.readouterr().out


def test_a_busy_database_stops_before_the_key_is_checked(tmp_path, monkeypatch):
    args, out = _ready(tmp_path, monkeypatch, None)
    monkeypatch.setattr(check, "check_database", lambda url: (check.BUSY, "not now: agent run 4 holds the lease"))
    client = CreditClient()
    assert check.main(args, client=client) == check.BUSY
    assert client.counted == [] and not out.exists()


def test_an_unreadable_key_file_or_env_stops_at_once(tmp_path, capsys):
    (tmp_path / "key.txt").write_text("two\nlines", encoding="utf-8")
    assert check.main([str(tmp_path / "key.txt"), str(tmp_path / ".env"), str(tmp_path / "out")]) == check.USAGE
    (tmp_path / "key.txt").write_text(KEY, encoding="utf-8")
    assert check.main([str(tmp_path / "key.txt"), str(tmp_path / ".env"), str(tmp_path / "out")]) == check.USAGE
    assert KEY not in capsys.readouterr().out


# --- The script itself (read, not run) ----------------------------------------------------------------------

def test_the_script_sets_the_secret_the_container_reads_from_a_file():
    text = SCRIPT.read_text(encoding="utf-8")
    deploy = (ROOT / "scripts" / "deploy_container_app.ps1").read_text(encoding="utf-8")
    secret = re.search(r'"VENDING_ANTHROPIC_API_KEY" = "([a-z-]+)"', deploy).group(1)
    assert f'[string]$SecretName = "{secret}"' in text
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

LEASE = ("update agent_run_lease set token = gen_random_uuid(), run_id = null,"
         " expires_at = clock_timestamp() + interval '1 hour' where one")


def test_a_held_lease_or_a_running_time_is_busy(conn):
    assert check.busy(conn) is None
    conn.execute(LEASE)
    assert "holds the run lease" in check.busy(conn)
    conn.execute("update agent_run_lease set expires_at = clock_timestamp() - interval '1 second' where one")
    assert check.busy(conn) is None
    conn.execute("insert into agent_schedule (due_at, latest_at, mode, outcome) values"
                 " ('2026-12-03T17:00Z', '2026-12-04T01:00Z', 'live', 'running')")
    assert "says running" in check.busy(conn)
