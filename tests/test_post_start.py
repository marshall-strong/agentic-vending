"""From the start (`vending.start.START`) a poll carries all 40 selections, and nothing may show the
other team's per-selection data or a machine-wide total that includes their sales (`docs/integrity.md`).
The end-to-end check is `tests/test_post_start_canary.py`; these test each guard on its own."""

import pytest

pytest.importorskip("psycopg")

from tests.conftest import store_poll  # noqa: E402
from vending import db_explorer, pages  # noqa: E402

# Machine-wide VA1 totals no single selection adds up to, so they can't show by chance.
TOTAL_CENTS, TOTAL_UNITS = 987654, 98765


def test_the_public_explorer_hides_the_machine_wide_totals_of_every_poll(schema_url, conn):
    _, schema = schema_url
    store_poll(conn, "s1", "2026-12-04T15:00:00+00:00", {"15": (250, 3)})
    conn.execute("update polls set total_paid_cents = %s, total_paid_units = %s", (TOTAL_CENTS, TOTAL_UNITS))
    request = db_explorer.Request(schema, "polls", 0)
    status, page = db_explorer.render(conn, request, pages.HIDE, (), pages.ONLY)
    assert status == 200 and "(hidden)" in page and "s1" in page
    assert str(TOTAL_CENTS) not in page and str(TOTAL_UNITS) not in page
    # Without the rule they would be there: the test sees the page it means to.
    assert str(TOTAL_CENTS) in db_explorer.render(conn, request)[1]
