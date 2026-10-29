"""Settings from the environment, falling back to `.env` at the repository root.

Real environment variables win, so the Azure VM can set them without a file.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


class SettingsError(RuntimeError):
    """A required setting is missing."""


def _load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = _clean_value(value)
    return values


def _clean_value(value: str) -> str:
    """Strip one matching pair of quotes, or a trailing ` # comment` from an unquoted value.

    Stripping each quote character on its own would turn `pa55'` into `pa55`,
    and a comment left in a password makes DATABASE_URL and the database disagree.
    """
    value = value.strip()
    if value[:1] in ('"', "'"):
        end = value.find(value[0], 1)
        if end > 0:
            return value[1:end]
        return value
    return re.split(r"\s#", value, maxsplit=1)[0].rstrip()


_FILE = _load_env_file(REPO_ROOT / ".env")


def get(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name) or _FILE.get(name) or default


def require(name: str, hint: str) -> str:
    value = get(name)
    if not value:
        raise SettingsError(f"{name} is not set. {hint}")
    return value
