"""The deploy script's secret files: parsed as the app parses `.env`, written byte for byte."""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "write_secret_files.py"
spec = importlib.util.spec_from_file_location("write_secret_files", SCRIPT)
write_secret_files = importlib.util.module_from_spec(spec)
spec.loader.exec_module(write_secret_files)


def test_values_match_what_the_app_reads(tmp_path):
    env = tmp_path / ".env"
    env.write_text("DATABASE_URL='postgresql://u:pa55\"@h/db'\n"
                   "DEX_API_KEY=abc123 # from David, 22 Oct\n"
                   "DEX_BASE_URL=http://aihub-pi:8000\n", encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    assert write_secret_files.main([str(env), str(out), "DATABASE_URL=database-url", "DEX_API_KEY"]) == 0
    assert (out / "database-url").read_bytes() == b'postgresql://u:pa55"@h/db'
    assert (out / "DEX_API_KEY").read_bytes() == b"abc123"
    assert sorted(p.name for p in out.iterdir()) == ["DEX_API_KEY", "database-url"]


def test_a_missing_setting_writes_nothing(tmp_path, capsys):
    env = tmp_path / ".env"
    env.write_text("DATABASE_URL=postgresql://h/db\nTS_AUTHKEY=\n", encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    assert write_secret_files.main([str(env), str(out), "DATABASE_URL", "TS_AUTHKEY", "DEX_API_KEY"]) == 1
    assert list(out.iterdir()) == []
    err = capsys.readouterr().err
    assert "TS_AUTHKEY, DEX_API_KEY" in err and "postgresql" not in err
