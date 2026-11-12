"""scripts/poll_snapshot.py has its own `.env` reader (it runs with no third-party packages)."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_script():
    spec = importlib.util.spec_from_file_location("poll_snapshot", ROOT / "scripts" / "poll_snapshot.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_load_env_ignores_a_byte_order_mark(tmp_path):
    # What Windows PowerShell 5.1's `Set-Content -Encoding utf8` writes.
    path = tmp_path / ".env"
    path.write_bytes(b"\xef\xbb\xbfBRIDGE_URL=http://h:8080\nDEX_TIER=student\n")
    assert _load_script().load_env(path) == {"BRIDGE_URL": "http://h:8080", "DEX_TIER": "student"}
