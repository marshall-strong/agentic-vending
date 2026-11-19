"""`get_project_rules` and the `docs/agent/` notes it serves. No database needed; the server test is in test_server.py."""

from __future__ import annotations

import json
from datetime import date

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



def test_interloper_topic_matches_the_latest_observation():
    """The agent can't read analysis/ (it isn't in the image), so the topic carries the newest file's drinks."""
    latest = sorted((RULES_DIR.parent.parent / "analysis" / "interloper").glob("*.json"))[-1]
    data = json.loads(latest.read_text(encoding="utf-8"))
    topic = (RULES_DIR / "interloper.md").read_text(encoding="utf-8")
    day = date.fromisoformat(data["observed"])
    assert f"{day:%a} {day.day} {day:%b}" in topic, "the topic names an older observation"
    for d in data["drinks"]:
        assert f"| {d['position']} | {d['name']} | {d['size']} |" in topic, d["name"]


def test_no_agent_topic_gives_the_interloper_prices():
    """The course staff said its prices may not be an input to ours (David, 17 Nov), so the agent never sees them."""
    topic = (RULES_DIR / "interloper.md").read_text(encoding="utf-8").lower()
    for word in ("$", "¢", "cents", "price range", "cheaper", "dearer", "its price |"):
        assert word not in topic, word
    for path in RULES_DIR.glob("*.md"):
        for line in path.read_text(encoding="utf-8").lower().splitlines():
            if "interloper" in line:
                assert "$" not in line and "may be used" not in line, (path.name, line)


@pytest.mark.parametrize("topic", ["pricing", "rules-card", "competition-and-integrity", "interloper"])
def test_the_interloper_price_rule_is_where_the_agent_reads_pricing_rules(topic):
    text = (RULES_DIR / f"{topic}.md").read_text(encoding="utf-8").lower()
    assert "never use the interloper's prices" in text or "ignore the interloper's prices" in text, topic


def test_the_prompt_gives_each_store_cutoff_the_planner_uses():
    """Simulated seasons (closed PR #151): "the main run is Thursday" missed the Amazon cutoff every week."""
    from vending.agent import SYSTEM
    from vending.planner import AMAZON_CUTOFF, IN_STORE_CUTOFF

    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    for store, cutoff in (("Amazon items", AMAZON_CUTOFF), ("in-store items", IN_STORE_CUTOFF)):
        assert f"{store} by {days[cutoff.weekday]} {cutoff.at:%H:%M}" in SYSTEM, store
    assert "main run is Thursday" not in SYSTEM



def test_the_card_says_the_season_opens_empty_and_day_one_fills_it():
    """Simulated seasons (closed PR #151): with no receipts the agent called the slots unknown and waited."""
    card = (RULES_DIR / "rules-card.md").read_text(encoding="utf-8").lower()
    assert "opens empty" in card and "day one fills it" in card and "end stock counts at cost" in card
    assert "except before the first fill, when our slots are empty" in (RULES_DIR / "inventory-belief.md").read_text(encoding="utf-8").lower()
