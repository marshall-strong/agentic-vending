"""`get_project_rules`: our notes on the project brief, from `docs/agent/`.

With no topic it lists the topics and their one-line summaries, read from
`docs/agent/README.md`. With a topic it returns that file's text. It reads only
files directly in `docs/agent/` whose names are in that list: a topic is a
plain name such as `pricing`, never a path, so `..`, slashes and anything not
listed are refused with the list of valid topics.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel

from vending.tools import Reply

RULES_DIR = Path(__file__).resolve().parents[2] / "docs" / "agent"
_LINE = re.compile(r"^- `([a-z0-9-]+)`: (.+)$")
_NAME = re.compile(r"^[a-z0-9-]+$")


class Topic(BaseModel):
    topic: str
    summary: str


class ProjectRules(Reply):
    topic: str | None = None
    text: str | None = None
    data: list[Topic] = []


def topics(rules_dir: Path = RULES_DIR) -> list[Topic]:
    """The topics listed in the folder's README.md, in order."""
    lines = (rules_dir / "README.md").read_text(encoding="utf-8").splitlines()
    return [Topic(topic=m[1], summary=m[2]) for m in map(_LINE.match, lines) if m]


def get_project_rules(topic: str | None = None, rules_dir: Path = RULES_DIR) -> ProjectRules:
    listed = topics(rules_dir)
    names = [t.topic for t in listed]
    if topic is None or not topic.strip():
        return ProjectRules(success=True, count=len(listed), data=listed,
                            message=f"{len(listed)} topics. Call get_project_rules with one of: {', '.join(names)}.")
    name = topic.strip().lower()
    if not _NAME.match(name) or name not in names:
        return ProjectRules(success=False, topic=topic,
                            message=f"No topic {topic!r}. A topic is a plain name, one of: {', '.join(names)}.")
    text = (rules_dir / f"{name}.md").read_text(encoding="utf-8")
    return ProjectRules(success=True, count=1, topic=name, text=text, message=f"The {name} topic.")
