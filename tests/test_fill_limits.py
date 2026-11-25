"""The day-one fill's own limits (the parent's PR C, 25 Nov): `python -m vending.fill` refuses a request whose
most-we-pay is over the $400 cap (`over_fill_cap`, docs/opening-capital-plan.md), and `--accept-deviations` doesn't
lift it; and it refuses a fill that leaves a slot of ours without a line (`fill_incomplete`), so it can't send 17 of
20 slots by accident. Uses the picked plan and fake bridge from `tests/test_fill.py`."""

import pytest

psycopg = pytest.importorskip("psycopg")

from tests.conftest import TEST_SLOTS, open_in  # noqa: E402
from tests.test_desired_state import fake  # noqa: E402,F401  (a fixture)
from tests.test_fill import ALT16, NOW, codes, option, plan_json, previewed, run, seed  # noqa: E402
from tests.test_server import db  # noqa: E402,F401  (a fixture)
from vending import fill, plan_rounds, sourcing  # noqa: E402


def preview(schema_url, fake):  # noqa: F811
    with open_in(*schema_url) as conn:
        return fill.preview(conn, fake.client(), NOW)


def test_a_request_over_the_fill_cap_is_refused_even_with_accept_deviations(db, fake, schema_url, capsys,  # noqa: F811
                                                                           monkeypatch):
    """The request's most-we-pay is $162.00. With the cap at $161.00 it is refused, though the $600 opening less
    the $53 floor would allow it. A Walmart 16-pack alternate also differs from the pick, so the submit carries
    --accept-deviations: that lifts pick_deviation only, never the cap."""
    seed(db, fake)
    sourcing.set_option(db, option("COKE20", "alternate", **ALT16), NOW)
    monkeypatch.setattr(plan_rounds, "FILL_CAP_CENTS", 16100)
    p = preview(schema_url, fake)
    assert p.plan.cost.max_cents == 16200 and p.deviations
    assert [r.code for r in p.refusals] == ["over_fill_cap"] and not p.ok
    assert "$162.00" in p.refusals[0].message and "$161.00" in p.refusals[0].message
    out, plan_hash = previewed(schema_url, fake, capsys)
    assert plan_hash is None and "over_fill_cap" in out
    assert run(schema_url, fake, "--submit", "--hash", "0" * 64, "--by", "Test Submitter",
               "--accept-deviations") == 1
    capsys.readouterr()
    assert "over_fill_cap" in codes(db) and fake.posts == []


def test_a_request_at_the_cap_is_sent(db, fake, schema_url, monkeypatch, capsys):  # noqa: F811
    seed(db, fake)
    monkeypatch.setattr(plan_rounds, "FILL_CAP_CENTS", 16200)        # the request's most-we-pay, exactly
    _, plan_hash = previewed(schema_url, fake, capsys)
    assert plan_hash
    monkeypatch.setattr(plan_rounds, "FILL_CAP_CENTS", 16199)
    assert [r.code for r in preview(schema_url, fake).refusals] == ["over_fill_cap"]


def test_a_plan_that_leaves_one_of_our_slots_out_is_refused(db, fake, schema_url, capsys):  # noqa: F811
    seed(db, fake)
    plan = plan_json()
    plan["day_one"] = plan["day_one"][:-1]                            # the last slot has no line
    db.execute("update first_fill_plans set plan = %s where picked", (psycopg.types.json.Jsonb(plan),))
    p = preview(schema_url, fake)
    [refusal] = [r for r in p.refusals if r.code == "fill_incomplete"]
    assert TEST_SLOTS[-1] in refusal.message and not p.ok
    assert run(schema_url, fake, "--submit", "--hash", "0" * 64, "--by", "Test Submitter",
               "--accept-deviations") == 1
    capsys.readouterr()
    assert "fill_incomplete" in codes(db) and fake.posts == []
