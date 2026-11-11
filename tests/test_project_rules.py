"""`get_project_rules` and the `docs/agent/` notes it serves. No database needed; the server test is in test_server.py."""

from __future__ import annotations

import pytest

from vending.tools.project_rules import RULES_DIR, get_project_rules, topics


def words(name: str) -> int:
    return len((RULES_DIR / f"{name}.md").read_text(encoding="utf-8").split())


def test_no_topic_lists_every_topic_with_its_summary():
    result = get_project_rules(None)
    assert result.success and result.count == len(result.data) > 0
    assert "pricing" in [t.topic for t in result.data]
    assert all(t.summary for t in result.data)


def test_a_topic_returns_its_file():
    result = get_project_rules(" Pricing ")
    assert result.success and result.topic == "pricing"
    assert result.text == (RULES_DIR / "pricing.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("topic", [
    "../README", "..", "../../vending/server", "pricing/../money", "docs/agent/pricing", "/etc/passwd",
    "C:\\Windows\\win.ini", "..\\README", "pricing.md", "README", "readme", "nope", "pric ing", "pricing\x00",
])
def test_paths_and_unknown_names_are_refused(topic):
    result = get_project_rules(topic)
    assert not result.success and result.text is None
    assert "one of: rules-card" in result.message


def test_the_list_and_the_files_match():
    listed = [t.topic for t in topics()]
    on_disk = sorted(p.stem for p in RULES_DIR.glob("*.md") if p.name != "README.md")
    assert len(listed) == len(set(listed))
    assert sorted(listed) == on_disk


def test_each_file_opens_with_its_listed_summary():
    for t in topics():
        lines = (RULES_DIR / f"{t.topic}.md").read_text(encoding="utf-8").splitlines()
        assert lines[0].startswith("# ") and lines[1] == "" and lines[2] == t.summary, t.topic


def test_topic_files_are_short():
    for t in topics():
        assert words(t.topic) < 600, t.topic


def test_the_rules_card_is_short_and_covers_the_guardrails():
    assert words("rules-card") <= 250
    card = (RULES_DIR / "rules-card.md").read_text(encoding="utf-8").lower()
    for phrase in ("halt switch", "price floor", "ceiling", "balance is the cap", "integrity", "ignore other slots' prices",
                   "get_project_rules", "never from memory", "skip"):
        assert phrase in card, phrase

