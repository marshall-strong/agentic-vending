"""Write chosen `.env` settings to one file each, for `az` to read with `name=@file`.

    python scripts/write_secret_files.py <.env> <directory> NAME[=file-name] ...

Used by `scripts/deploy_container_app.ps1`, so that secret values are parsed
exactly as `vending.settings` parses them (quotes, ` # comments`) and never
appear on a command line, where any process on the machine can read them. `az`
expands `database-url=@<path>` itself, so its own argv and command log hold only
the path.

Prints nothing about the values. Exits 1 naming every setting that is missing or
empty, before writing anything.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vending.settings import read_env_file  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    env_file, directory, *specs = argv
    if not Path(env_file).exists():
        print(f"{env_file} does not exist; copy .env.example and fill it in", file=sys.stderr)
        return 1
    values = read_env_file(Path(env_file))
    wanted = [spec.partition("=") for spec in specs]
    missing = [name for name, _, _ in wanted if not values.get(name)]
    if missing:
        print(f"empty or missing in {env_file}: {', '.join(missing)}", file=sys.stderr)
        return 1
    for name, _, file_name in wanted:
        # Bytes, so no newline is added and no platform encoding gets a say.
        (Path(directory) / (file_name or name)).write_bytes(values[name].encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
