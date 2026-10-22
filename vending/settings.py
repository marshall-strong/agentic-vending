"""Settings from the environment, falling back to `.env` at the repository root.

Real environment variables win, so the Azure VM can set them without a file.
"""

from __future__ import annotations

import os
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
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


_FILE = _load_env_file(REPO_ROOT / ".env")


def get(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name) or _FILE.get(name) or default


def require(name: str, hint: str) -> str:
    value = get(name)
    if not value:
        raise SettingsError(f"{name} is not set. {hint}")
    return value
