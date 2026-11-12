"""Tests for reading settings files. Every value here is made up."""

import pytest

from vending.settings import _clean_value, read_env_file


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("plain", "plain"),
        ("  padded  ", "padded"),
        ('"quoted"', "quoted"),
        ("'single'", "single"),
        ("pa55'", "pa55'"),  # a quote that is part of the value stays
        ("pa55\"", "pa55\""),
        ("value # local", "value"),
        ("value\t# local", "value"),
        ("pa#ss", "pa#ss"),  # a # inside a word is not a comment
        ('"has # inside" # comment', "has # inside"),
        ('"unclosed', '"unclosed'),
        ("", ""),
    ],
)
def test_clean_value(raw, expected):
    assert _clean_value(raw) == expected


def test_read_env_file(tmp_path):
    path = tmp_path / "settings"
    path.write_text(
        "# a comment line\n"
        "\n"
        "DATABASE_URL=postgresql://user:pa55'@localhost/db # local\n"
        'VENDING_DB_PASSWORD="made up"\n'
        "not a setting\n",
        encoding="utf-8",
    )
    assert read_env_file(path) == {
        "DATABASE_URL": "postgresql://user:pa55'@localhost/db",
        "VENDING_DB_PASSWORD": "made up",
    }


def test_read_env_file_ignores_a_byte_order_mark(tmp_path):
    # What Windows PowerShell 5.1's `Set-Content -Encoding utf8` writes.
    path = tmp_path / "settings"
    path.write_bytes(b"\xef\xbb\xbfDATABASE_URL=postgresql://h/db\nDEX_TIER=student\n")
    assert read_env_file(path) == {"DATABASE_URL": "postgresql://h/db", "DEX_TIER": "student"}
