"""Simulated seasons on the dashboard's demo copy (`vending.simulate.demo`) and their summaries
(`vending.simulate.summary`).

The pages are served by `vending.worker` at /dashboard/demo/simulations, behind the password, from
committed files only. These tests check the gate, the banner on every page, that only GET is
answered, that no database connection is ever opened, and that the loader refuses a summary that
doesn't say it is simulated or names an assignment `vending.slots` wouldn't accept.
"""

from __future__ import annotations

import copy
import http.client
import json
import subprocess
import sys
from pathlib import Path

import pytest

from vending import gate, worker
from vending.simulate import demo, summary

ROOT = demo.ROOT
PASSWORD = "test-password-for-the-simulations"


def _ask(port: int, method: str, path: str, headers: dict | None = None, body: bytes | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        reply = conn.getresponse()
        return reply.status, dict(reply.getheaders()), reply.read().decode()
    finally:
        conn.close()


@pytest.fixture
def site():
    server = worker.serve_health(worker.State(), 0)
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def no_database(monkeypatch):
    """Any attempt to connect fails the test, through psycopg or the pages' own connector."""
    import psycopg

    from vending import pages

    def refuse(*_a, **_k):
        raise AssertionError("a simulation page tried to open a database connection")

    monkeypatch.setattr(psycopg, "connect", refuse)
    monkeypatch.setattr(pages, "connect", refuse)


def test_the_worker_and_the_pages_agree_on_the_address():
    assert worker.SIMULATIONS == demo.ROOT == "/dashboard/demo/simulations"


def test_every_simulation_page_is_behind_the_password(monkeypatch):
    monkeypatch.setattr(gate, "ENABLED", True)
    monkeypatch.setattr(gate, "PASSWORD_HASH", gate.make_hash(PASSWORD, n=2**10))
    monkeypatch.setenv(gate.KEY_SETTING, "postgresql://reader:secret@example/db")
    server = worker.serve_health(worker.State(), 0)
    port = server.server_address[1]
    try:
        for path in (ROOT, ROOT + "/", f"{ROOT}/2026-11-16-opus-medium", f"{ROOT}/nope"):
            status, headers, body = _ask(port, "GET", path)
            assert status == 303 and headers["Location"].startswith("/login?next=/dashboard/demo/simulations"), path
            assert demo.BANNER not in body
        status, headers, _ = _ask(port, "GET", f"{ROOT}/2026-11-16-opus-medium",
                                  {"Cookie": f"{gate.COOKIE}={gate.token()}"})
        assert status == 200 and headers["Content-Security-Policy"] == demo.CSP
    finally:
        server.shutdown()
        server.server_close()


def test_every_page_carries_the_banner_and_reads_no_database(site, no_database):
    pages = [ROOT, ROOT + "/", *(f"{ROOT}/{s.id}" for s in summary.load_all()), f"{ROOT}/no-such-season",
             f"{ROOT}/2026-11-16-opus-medium/extra", f"{ROOT}/..%2f..%2fslots"]
    assert len(pages) >= 6
    for path in pages:
        status, headers, body = _ask(site, "GET", path)
        assert status in (200, 404), path
        assert demo.BANNER in body and "<title>SIMULATED" in body, path
        assert body.index(demo.BANNER) < body.index("<main>"), path          # first thing on the page
        assert "<script" not in body and headers["Cache-Control"] == "no-store"
    status, _, body = _ask(site, "GET", ROOT)
    assert status == 200
    for s in summary.load_all():
        assert f'href="{ROOT}/{s.id}"' in body and s.name in body.replace("&#x27;", "'")


def test_the_index_opens_with_profit_by_week_after_ai_cost(site, no_database):
    """Slide 13 of the final deck: the first thing under the banner is cumulative profit by week for
    every season with weeks, each with its AI cost; the agent's line and the rule's are told apart."""
    _, _, body = _ask(site, "GET", ROOT)
    main = body[body.index("<main>"):]
    assert main.index('class="card headline"') < main.index("<h1>") < main.index("<table>")
    chart = main[main.index('class="card headline"'):main.index("</section>")]
    assert "Profit so far, week by week, after AI cost" in chart and "<svg" in chart
    charted = [s for s in summary.load_all() if s.weeks]
    assert len(charted) >= 3
    for s in charted:
        line = f'{s.id}">{s.name}</a>: <b>${s.totals["profit_after_model"]:,.2f}</b> after '
        assert line in chart.replace("&#x27;", "'"), s.id
        assert f'<b>${round(s.totals["model_spend"], 2):,.2f}</b> of AI cost' in chart, s.id
        assert f'${s.totals["profit_after_model"]:,.0f}</text>' in chart, s.id        # the line's end label
    assert chart.count("<polyline") == len(charted)
    rules = sum(s.data["decider"] != "model" for s in charted)
    assert chart.count('stroke-dasharray="7 5"') == rules >= 1          # the rule is dashed, the agent isn't
    # Slide 13's three: the agent as first built and after the fixes, against the rule on the same days.
    for sid in ("2026-11-16-opus-medium", "2026-11-16-opus-medium-fixed", "2026-11-16-baseline-wednesday"):
        assert f'href="{ROOT}/{sid}"' in chart
    assert "Not charted, totals only" in chart and "2026-11-16-interloper-0-25" in chart


def test_a_season_page_shows_weeks_report_cards_and_decisions(site, no_database):
    status, _, body = _ask(site, "GET", f"{ROOT}/2026-11-16-opus-medium-round3")
    assert status == 200
    for text in ("Week by week", "Report cards", "The agent's decisions", "$963.69", "W10", "✓ good"):
        assert text in body, text
    status, _, body = _ask(site, "GET", f"{ROOT}/2026-11-16-interloper-0-25")
    assert status == 200 and "$412.11" in body and "$380.33" in body and "not kept for this season" in body


def test_only_get_is_answered(site):
    for path in (ROOT, f"{ROOT}/2026-11-16-opus-medium"):
        status, _, body = _ask(site, "POST", path, headers={"Content-Length": "0"})
        assert status == 405 and demo.BANNER not in body, path
    for method in ("PUT", "DELETE", "PATCH"):
        status, _, _ = _ask(site, method, ROOT, headers={"Content-Length": "0"})
        assert status == 501, method          # BaseHTTPRequestHandler: no handler for the method


def test_the_pages_never_load_the_database_driver():
    code = (f"import sys; from vending.simulate import demo; r = demo.respond({ROOT!r}); "
            f"[demo.respond({ROOT!r} + '/' + s.id) for s in demo.summary.load_all()]; "
            "assert r[0] == 200; assert 'psycopg' not in sys.modules, 'psycopg was imported'; "
            "assert 'vending.pages' not in sys.modules and 'vending.db' not in sys.modules")
    done = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr


def test_the_live_pages_do_not_link_or_show_simulations(site):
    _, _, home = _ask(site, "GET", "/")
    assert ROOT not in home and demo.BANNER not in home
    assert demo.respond("/dashboard") is None and demo.respond("/dashboard/demo") is None
    assert demo.respond("/dashboard/demo/simulationsx") is None


# --- the summaries --------------------------------------------------------------------------------

def test_the_committed_summaries_load_and_stay_small():
    seasons = summary.load_all()
    assert {s.id for s in seasons} >= {"2026-11-16-opus-medium-round3", "2026-11-16-opus-medium"}
    assert len(seasons) <= 9         # the five from 16 Nov, 25 Nov's two live-schedule rehearsals, 27 Nov's final ones
    lines = sum(len(p.read_text(encoding="utf-8").splitlines()) for p in summary.SEASONS.glob("*.json"))
    assert lines < 2100
    for s in seasons:
        assert s.data["simulated"] is True
        assert all(len(d["reason"] or "") <= summary.REASON_CHARS for d in s.data["decisions"])
        if s.weeks:
            assert s.weeks[-1]["cumulative_profit"] == pytest.approx(s.totals["profit_after_model"], abs=0.05)


def _good() -> dict:
    return copy.deepcopy(summary.load(summary.SEASONS / "2026-11-16-opus-medium-round3.json").data)


@pytest.mark.parametrize("breaks, why", [
    (lambda d: d.update(simulated=False), "simulated"),
    (lambda d: d.pop("simulated"), "simulated"),
    (lambda d: d.update(version=99), "version"),
    (lambda d: d.update(id="Has Spaces"), "id"),
    (lambda d: d["totals"].pop("profit_after_model"), "profit_after_model"),
    (lambda d: d.update(assignment=["15", "16"]), "assignment"),
    (lambda d: d.update(assignment=[f"{r}{c}" for r in "1234" for c in "01234"]), "assignment"),
    (lambda d: d["weeks"][0].update(profit="lots"), "profit"),
    (lambda d: d["weeks"][0]["report_card"].update(stock_outs={"grade": "great"}), "report card"),
    (lambda d: d.update(sweep={"knob": "x", "rows": [{"value": "a"}]}), "sweep"),
])
def test_the_loader_refuses_a_summary_it_cannot_trust(tmp_path, breaks, why):
    data = _good()
    breaks(data)
    path = tmp_path / f"{data['id'] if isinstance(data.get('id'), str) else 'x'}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(summary.BadSummary, match=why):
        summary.load(path)


def test_a_bad_summary_turns_the_pages_off_with_a_plain_error(tmp_path, monkeypatch):
    good = _good()
    data = copy.deepcopy(good)
    data["simulated"] = False
    (tmp_path / f"{data['id']}.json").write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(summary, "SEASONS", tmp_path)
    status, _, body, _ = demo.respond(ROOT)
    assert status == 500 and demo.BANNER in body and "can't be read" in body
    with pytest.raises(summary.BadSummary, match="not the file"):
        (tmp_path / "other-name.json").write_text(json.dumps(good), encoding="utf-8")
        summary.load(tmp_path / "other-name.json")


def test_a_summary_is_built_from_a_seasons_full_detail(tmp_path):
    """From the files `report.write` leaves: weeks, report cards, decisions with short reasons, and the
    season's own slots from its day-by-day record when season.json doesn't name them."""
    slots = [f"{r}{c}" for r in "12345" for c in "1234"]     # made up for the test, not ours
    week = {"week": 1, "saturday": "2026-11-28", "class_days": 2, "units_sold": 3, "units_lost": 0.6,
            "revenue_ex_tax": 9.0, "gross_profit": 4.0, "model_spend": 0.1, "profit": 3.9, "cumulative_profit": 3.9,
            "balance": 590.0, "spendable": 588.0, "inventory_at_cost": 12.5,
            "slots": {"11": {"lost": 0.6}, "12": {"lost": 0.2}},
            "report_card": {"stock_outs": {"grade": "watch", "why": "1 of 4 units lost"}}}
    (tmp_path / "season.json").write_text(json.dumps({"decider": "scripted", "seed": 3, "profit_after_model": 3.9,
                                                      "units_sold": 3}), encoding="utf-8")
    (tmp_path / "weeks.json").write_text(json.dumps([week]), encoding="utf-8")
    (tmp_path / "runs.json").write_text(json.dumps([{"at": "2026-11-30T10:00:00-05:00", "kind": "day-one",
                                                     "status": "submitted", "decision": "submit", "cost_usd": 0.0012,
                                                     "reason": "Fill day one. " * 60}]), encoding="utf-8")
    (tmp_path / "daily_slots.csv").write_text("day,slot\n" + "".join(f"2026-11-30,{s}\n" for s in slots),
                                              encoding="utf-8")
    built = summary.build(tmp_path, "test-season", name="Test", strategy="a test")
    assert built["assignment"] == slots and built["weeks"][0]["stock_out_slots"] == 1
    assert built["weeks"][0]["report_card"]["stock_outs"]["grade"] == "watch"
    reason = built["decisions"][0]["reason"]
    assert len(reason) <= summary.REASON_CHARS and reason.endswith("one.")
    summary.write(built, tmp_path / "test-season.json")
    assert summary.load(tmp_path / "test-season.json").data == built


# --- review fixes -------------------------------------------------------------------------------

@pytest.mark.parametrize("breaks", [
    lambda d: d["weeks"][0].update(saturday="not-a-date"),
    lambda d: d["weeks"][0].update(saturday="2026-13-01"),
    lambda d: d["weeks"][0].update(saturday=None),
    lambda d: d["decisions"][0].update(at="yesterday"),
])
def test_the_loader_refuses_dates_a_page_cannot_show(tmp_path, breaks):
    data = _good()
    breaks(data)
    path = tmp_path / f"{data['id']}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(summary.BadSummary):
        summary.load(path)


def test_a_season_that_made_nothing_still_draws(tmp_path, monkeypatch):
    data = _good()
    for w in data["weeks"]:
        w.update(profit=0, cumulative_profit=0)
    data["totals"]["profit_after_model"] = 0
    summary.write(data, tmp_path / f"{data['id']}.json")
    monkeypatch.setattr(summary, "SEASONS", tmp_path)
    for path in (ROOT, f"{ROOT}/{data['id']}"):
        status, _, body, _ = demo.respond(path)
        assert status == 200 and "<svg" in body, path
    low, high, _ = demo._scale([0.0, 0.0])
    assert high > low


def test_any_other_failure_is_a_plain_500_with_no_error_text(monkeypatch, capsys):
    def boom(*_a, **_k):
        raise RuntimeError("secret detail: postgresql://user:hunter2@somewhere")

    monkeypatch.setattr(demo, "index", boom)
    monkeypatch.setattr(demo, "season_page", boom)
    for path in (ROOT, f"{ROOT}/2026-11-16-opus-medium"):
        status, kind, body, csp = demo.respond(path)
        assert status == 500 and kind == demo.HTML and csp == demo.CSP, path
        assert demo.BANNER in body and "hunter2" not in body and "secret" not in body
    assert "hunter2" not in capsys.readouterr().err          # the log names the kind of error, not its text
