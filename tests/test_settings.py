"""Tests for reading settings files. Every value here is made up."""

import pytest

from vending.settings import _clean_value, _load_env_file


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


def test_load_env_file(tmp_path):
    path = tmp_path / "settings"
    path.write_text(
        "# a comment line\n"
        "\n"
        "DATABASE_URL=postgresql://user:pa55'@localhost/db # local\n"
        'VENDING_DB_PASSWORD="made up"\n'
        "not a setting\n",
        encoding="utf-8",
    )
    assert _load_env_file(path) == {
        "DATABASE_URL": "postgresql://user:pa55'@localhost/db",
        "VENDING_DB_PASSWORD": "made up",
    }
