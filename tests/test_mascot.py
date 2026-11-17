"""Honcho, the dashboard's mascot: well-formed SVG, one face per verdict, on the page."""
import xml.etree.ElementTree as ET

import pytest

from vending import dashboard, mascot
from tests.test_dashboard import page_conn  # noqa: F401  (a fixture)

SVG = "{http://www.w3.org/2000/svg}"


@pytest.mark.parametrize("mood", mascot.MOODS)
def test_each_mood_is_well_formed_decorative_svg(mood):
    root = ET.fromstring(mascot.svg(mood, size=60))
    assert root.tag == f"{SVG}svg" and root.get("aria-hidden") == "true"   # the verdict pill says it aloud
    assert root.get("height") == "60" and root.get("class") == f"mascot {mood}"
    assert root.find(f"{SVG}title").text.startswith("Honcho, our head agent: ")      # the hover tooltip


def test_each_mood_draws_a_different_face():
    assert len({mascot.svg(m) for m in mascot.MOODS}) == 3
    assert ["#7cc3f0" in mascot.svg(m) for m in mascot.MOODS] == [False, False, True]   # only a problem sweats


def test_every_verdict_the_dashboard_can_give_has_a_face():
    assert set(dashboard._VERDICT_TEXT) == set(mascot.MOODS)


def test_an_unknown_mood_draws_check():
    assert mascot.svg("sideways") == mascot.svg("check")


def test_the_dashboard_header_shows_honcho_with_its_verdict(page_conn):  # noqa: F811
    snap = dashboard.snapshot(page_conn)
    state = dashboard.verdict(dashboard.attention(snap))
    header = dashboard.render(snap).split("</header>")[0]
    assert f'class="mascot {state}"' in header


def test_the_error_page_shows_a_worried_honcho():
    assert 'class="mascot problem"' in dashboard._error_page("connection refused")
