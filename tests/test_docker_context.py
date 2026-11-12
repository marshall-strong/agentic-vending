"""The container image carries `docs/agent/`, which `get_project_rules` reads at run time.

`.dockerignore` leaves out `docs/` and `*.md`, then lets `docs/agent/` back in. These
tests apply a simplified copy of Docker's rules (each pattern is a path glob from the
context root, `*` does not cross `/`, a pattern also covers everything under a matched
folder, and the last matching pattern wins), so a change that drops the notes from the
image, or lets the rest of `docs/` in, fails here instead of in the deployed container.

The copy does not handle character classes such as `[cod]` or a leading `./`. For today's
patterns it agrees with a real BuildKit export of the context (2026-11-11: under `docs/`,
exactly the ten `docs/agent/*.md` files). Recheck that way if you add such a pattern.
"""

from __future__ import annotations

import re
from pathlib import Path

from vending.tools.project_rules import RULES_DIR

ROOT = Path(__file__).resolve().parents[1]


def ignore_rules() -> list[tuple[bool, re.Pattern[str]]]:
    rules = []
    for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        keep = line.startswith("!")
        pattern = line.lstrip("!").strip("/")
        glob = re.escape(pattern).replace(r"\*\*", ".*").replace(r"\*", "[^/]*").replace(r"\?", "[^/]")
        rules.append((keep, re.compile(glob)))
    return rules


def in_context(path: str) -> bool:
    """True when `docker build` sends `path` (relative, with `/`) to the builder."""
    parts = path.split("/")
    prefixes = ["/".join(parts[: i + 1]) for i in range(len(parts))]
    included = True
    for keep, glob in ignore_rules():
        if any(glob.fullmatch(prefix) for prefix in prefixes):
            included = keep
    return included


def test_every_rules_note_is_in_the_build_context():
    notes = sorted(p.name for p in RULES_DIR.glob("*.md"))
    assert "README.md" in notes and "rules-card.md" in notes
    missing = [n for n in notes if not in_context(f"docs/agent/{n}")]
    assert missing == []


def test_the_rest_of_docs_and_other_markdown_stay_out():
    others = [
        p.relative_to(ROOT).as_posix()
        for p in (ROOT / "docs").rglob("*")
        if p.is_file() and RULES_DIR not in p.parents
    ]
    assert others, "expected other files under docs/"
    assert [p for p in others if in_context(p)] == []
    assert not in_context("README.md")
    assert not in_context(".env")


def test_the_dockerfile_copies_the_rules_notes_next_to_the_code():
    # project_rules.py finds the notes at <app>/docs/agent, beside <app>/vending.
    lines = [line.split() for line in (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines()]
    assert ["COPY", "vending/", "vending/"] in lines
    assert ["COPY", "docs/agent/", "docs/agent/"] in lines
