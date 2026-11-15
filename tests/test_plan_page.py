"""The day-one planning page: what it shows from the round files, the catalog pipeline from the
database, the Interloper overlap, and what it must never show."""

from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from tests.conftest import open_in
from tests.test_plan_rounds import day_one, projection, write
from vending import plan_page
from vending.slots import SLOTS


def interloper_dir(tmp_path, drinks=None):
    d = tmp_path / "interloper"
    d.mkdir()
    (d / "2026-11-14.json").write_text(json.dumps({
        "observed": "2026-11-14", "observed_by": "Sean",
        "drinks": drinks or [
            {"position": "45", "name": "Coca-Cola", "size": "12 oz can", "price_cents": 150, "match": ["coca", "cola"]},
            {"position": "50", "name": "Mountain Dew", "size": "12 oz can", "price_cents": 150, "match": ["mountain", "dew"]}]}),
        encoding="utf-8")
    return d


def rounds(tmp_path, catalog_requests=None):
    root = tmp_path / "rounds"
    root.mkdir()
    reqs = catalog_requests or [{"rank": 1, "name": "Coca-Cola", "size": "12 oz can (12-pack)", "why": "cheap"},
                                {"rank": 2, "name": "Red Bull Sugarfree", "size": "8.4 oz can", "why": "variety"}]
    write(root, "round1", "1", {"day_one": day_one(), "catalog_requests": reqs}, projection())
    write(root, "round2", "2", {"day_one": day_one(**{"15": {"price_cents": 375}}), "catalog_requests": reqs},
          projection(per_slot_gp=1500))
    return root


def test_page_shows_every_round_and_hatches_the_other_teams_slots(tmp_path):
    page = plan_page.build(rounds(tmp_path), None, interloper_dir(tmp_path))
    html = plan_page.render(page)
    assert 'id="tab-1"' in html and 'id="tab-2" checked' in html     # the latest round opens first
    assert "Round 2" in html and "$300" in html                       # base GP 20 x $15.00
    theirs = re.findall(r'<span class="cell theirs"[^>]*>(.*?)</span></span>', html)
    assert len(theirs) == 2 * 20                                       # 20 other selections per round face
    assert all(re.fullmatch(r'<span class="num">\d\d', t) for t in theirs)   # a number and nothing else
    for slot in ("11", "24", "54"):
        assert f'id="1-s{slot}"' not in html and f'id="2-s{slot}"' not in html
    assert "1 of 20 slots changed since round 1" in html
    assert "<script" not in html                                       # the page runs no script


def test_round_text_is_escaped(tmp_path):
    root = tmp_path / "rounds"
    root.mkdir()
    write(root, "round1", "1", {"day_one": day_one(**{"15": {"why": "<img src=x onerror=alert(1)>"}}),
                                "catalog_requests": [{"rank": 1, "name": "<b>bold</b>", "size": "12 oz can"}]},
          projection())
    html = plan_page.render(plan_page.build(root, None, tmp_path / "none"))
    assert "<img src=x" not in html and "&lt;img src=x" in html
    assert "<b>bold</b>" not in html


def test_without_a_database_the_requests_show_as_wanted_and_unchecked(tmp_path):
    page = plan_page.build(rounds(tmp_path), None, interloper_dir(tmp_path))
    assert not page.pipeline.checked
    assert {q.stage for q in page.pipeline.by_round["2"]} == {"wanted"}
    assert "No database was read" in plan_page.render(page)


def test_interloper_shows_only_drinks_we_also_sell(tmp_path):
    page = plan_page.build(rounds(tmp_path), None, interloper_dir(tmp_path))
    rows = plan_page.overlap(page.rounds[-1], page.interloper)
    # Our round sells Red Bull and Coke ("SIM-COKE" -> the projection names it Coca-Cola) but no Mountain Dew.
    names = [r["drink"]["name"] for r in rows]
    assert "Mountain Dew" not in names


def test_interloper_gap_only_when_sizes_match(tmp_path):
    root = tmp_path / "rounds"
    root.mkdir()
    proj = projection()
    proj["packs"][1]["name"] = "Coca-Cola"
    write(root, "round1", "1", {"day_one": day_one()}, proj)
    d = interloper_dir(tmp_path, [
        {"name": "Coca-Cola", "size": "12 oz can", "price_cents": 150, "match": ["coca", "cola"]},
        {"name": "Red Bull", "size": "16 oz can", "price_cents": 400, "match": ["red", "bull"]}])
    html = plan_page.render(plan_page.build(root, None, d))
    assert "$0.50 dearer" in html          # our $2.00 Coke can against its $1.50 can
    assert "different size" in html        # our 8.4 oz Red Bull against a 16 oz can


def test_pipeline_matches_requests_and_the_catalog(tmp_path, schema_url):
    with open_in(*schema_url) as conn:
        conn.execute("insert into product_requests (name, size, words, status, bridge_id) values"
                     " ('Red Bull Sugarfree', '8.4 oz can', '{red,bull,sugarfree,8.4,oz}', 'pending', 'b1'),"
                     " ('Gatorade Cool Blue', '20 oz bottle', '{gatorade,cool,blue,20,oz}', 'rejected', 'b2')")
        conn.execute("insert into catalog_products (sku, name, size, last_response) values"
                     " ('COKE-12CAN', 'Coca-Cola', '12 oz can', '{}')")
    page = plan_page.build(rounds(tmp_path), lambda: open_in(*schema_url), interloper_dir(tmp_path))
    assert page.pipeline.checked and not page.errors
    by = {q.name: q for q in page.pipeline.by_round["2"]}
    assert by["Coca-Cola"].stage == "approved" and by["Coca-Cola"].status == "in catalog"
    assert by["Red Bull Sugarfree"].stage == "filed" and by["Red Bull Sugarfree"].status == "pending"
    assert by["Gatorade Cool Blue"].stage == "rejected" and by["Gatorade Cool Blue"].team_only


def test_a_database_error_is_shown_and_the_page_still_renders(tmp_path):
    import psycopg

    def broken():
        raise psycopg.OperationalError("connection refused")
    page = plan_page.build(rounds(tmp_path), broken, interloper_dir(tmp_path))
    assert page.errors == ["Catalog requests weren't read: connection refused"]
    assert "connection refused" in plan_page.render(page)


def test_server_answers_local_hosts_only_and_sends_a_strict_csp(tmp_path):
    root = rounds(tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), plan_page._handler(root, use_db=False))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        with urllib.request.urlopen(base + "/") as reply:
            assert reply.status == 200
            assert reply.headers["Content-Security-Policy"] == "default-src 'none'; style-src 'unsafe-inline'"
            assert "Day-one plan" in reply.read().decode()
        request = urllib.request.Request(base + "/", headers={"Host": "evil.example"})
        try:
            urllib.request.urlopen(request)
            raise AssertionError("expected 403")
        except urllib.error.HTTPError as error:
            assert error.code == 403
        try:
            urllib.request.urlopen(base + "/state.json")
            raise AssertionError("expected 404")
        except urllib.error.HTTPError as error:
            assert error.code == 404
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_every_slot_row_is_one_of_ours(tmp_path):
    html = plan_page.render(plan_page.build(rounds(tmp_path), None, interloper_dir(tmp_path)))
    rows = re.findall(r'<tr id="2-s(\d\d)"', html)
    assert rows == list(SLOTS)
