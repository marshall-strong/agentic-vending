"""The shared password in front of the container's pages (`vending.gate`, served by `vending.worker`)."""

from __future__ import annotations

import http.client
import json
from urllib.parse import urlencode

import pytest

from vending import gate, worker

PASSWORD = "test-password-for-the-gate"


@pytest.fixture
def site(monkeypatch):
    monkeypatch.setattr(gate, "PASSWORD_HASH", gate.make_hash(PASSWORD, n=2**10))
    monkeypatch.setattr(gate, "WRONG_DELAY_SECONDS", 0)
    monkeypatch.setenv(gate.KEY_SETTING, "postgresql://reader:secret@example/db")
    server = worker.serve_health(worker.State(), 0)
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def ask(port: int, method: str, path: str, body: bytes | None = None, headers: dict | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        reply = conn.getresponse()
        return reply.status, dict(reply.getheaders()), reply.read()
    finally:
        conn.close()


def sign_in(port: int, password: str = PASSWORD, next_path: str = "/dashboard"):
    form = urlencode({"password": password, "next": next_path}).encode()
    return ask(port, "POST", "/login", form, {"Content-Type": "application/x-www-form-urlencoded"})


def cookie_from(headers: dict) -> str:
    return headers["Set-Cookie"].split(";", 1)[0]


def test_the_stored_hash_is_a_scrypt_hash_and_not_a_password():
    kind, n, r, p, salt, digest = gate.PASSWORD_HASH.split(":")
    assert kind == "scrypt" and int(n) >= 2**15 and int(r) == 8 and int(p) == 1
    assert len(salt) >= 20 and len(digest) >= 80


def test_every_page_but_health_asks_for_the_password(site):
    for path in ("/", "/dashboard", "/db", "/db?table=ledger"):
        status, headers, _ = ask(site, "GET", path)
        assert status == 303, path
        assert headers["Location"].startswith("/login?next=/")
    status, _, body = ask(site, "GET", "/dashboard/state.json")
    assert status == 401 and "signed out" in json.loads(body)["error"]
    status, _, _ = ask(site, "GET", "/health")
    assert status == 200


def test_the_login_page_is_a_form_with_a_strict_policy(site):
    status, headers, body = ask(site, "GET", "/login?next=/db")
    page = body.decode()
    assert status == 200
    assert '<form method="post" action="/login">' in page and 'value="/db"' in page
    assert "form-action 'self'" in headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]


def test_the_right_password_signs_in_and_the_cookie_opens_the_pages(site):
    status, headers, _ = sign_in(site, next_path="/db")
    assert status == 303 and headers["Location"] == "/db"
    set_cookie = headers["Set-Cookie"]
    for flag in ("HttpOnly", "Secure", "SameSite=Lax", f"Max-Age={gate.DAYS * 86400}"):
        assert flag in set_cookie
    cookie = cookie_from(headers)
    status, _, body = ask(site, "GET", "/", headers={"Cookie": cookie})
    assert status == 200 and b"Agentic Vending" in body
    # Signed in, the login page sends you on.
    status, headers, _ = ask(site, "GET", "/login?next=/dashboard", headers={"Cookie": cookie})
    assert status == 303 and headers["Location"] == "/dashboard"


def test_a_wrong_password_is_refused_and_sets_no_cookie(site):
    status, headers, body = sign_in(site, "not it")
    assert status == 401 and "Set-Cookie" not in headers
    assert "isn&#x27;t right" in body.decode() or "isn't right" in body.decode()
    status, _, _ = sign_in(site, "")
    assert status == 401


def test_a_forged_tampered_or_expired_cookie_is_refused(site):
    _, headers, _ = sign_in(site)
    good = cookie_from(headers)
    name, value = good.split("=", 1)
    expires, signature = value.split(".")
    for bad in (f"{name}={int(expires) + 1}.{signature}",            # moved expiry
                f"{name}={expires}.{'0' * len(signature)}",          # forged signature
                f"{name}=garbage", f"{name}=", "other=1"):
        status, _, _ = ask(site, "GET", "/dashboard", headers={"Cookie": bad})
        assert status == 303, bad
    assert not gate.token_valid(gate.token(now=0))  # issued in 1970, long expired


def test_changing_the_password_or_the_database_secret_signs_everyone_out(site, monkeypatch):
    _, headers, _ = sign_in(site)
    cookie = cookie_from(headers)
    monkeypatch.setenv(gate.KEY_SETTING, "postgresql://reader:rotated@example/db")
    assert ask(site, "GET", "/", headers={"Cookie": cookie})[0] == 303
    monkeypatch.setenv(gate.KEY_SETTING, "postgresql://reader:secret@example/db")
    assert ask(site, "GET", "/", headers={"Cookie": cookie})[0] == 200
    monkeypatch.setattr(gate, "PASSWORD_HASH", gate.make_hash("a different password", n=2**10))
    assert ask(site, "GET", "/", headers={"Cookie": cookie})[0] == 303


def test_after_signing_in_it_only_ever_sends_you_to_this_site(site):
    for target in ("https://evil.example/", "//evil.example/", "/\\evil.example", "javascript:alert(1)", ""):
        status, headers, _ = sign_in(site, next_path=target)
        assert status == 303 and headers["Location"] == "/dashboard", target
    assert gate.next_from_query("/login?next=https://evil.example") == "/dashboard"


def test_logout_clears_the_cookie(site):
    status, headers, _ = ask(site, "GET", "/logout")
    assert status == 303 and headers["Location"] == "/login"
    assert headers["Set-Cookie"].startswith(f"{gate.COOKIE}=;") and "Max-Age=0" in headers["Set-Cookie"]


def test_only_the_login_page_takes_a_post_and_never_a_large_one(site):
    assert ask(site, "POST", "/dashboard", b"x")[0] == 405
    big = b"password=" + b"a" * (gate.MAX_FORM_BYTES + 1)
    assert ask(site, "POST", "/login", big, {"Content-Type": "application/x-www-form-urlencoded"})[0] == 413


def test_password_checks_go_one_at_a_time(monkeypatch):
    monkeypatch.setattr(gate, "WAIT_SECONDS", 0.05)
    gate._checking.acquire()
    try:
        with pytest.raises(gate.Busy):
            gate.check("anything")
    finally:
        gate._checking.release()


def test_the_login_page_escapes_where_it_sends_you():
    page = gate.login_page('/x"><script>alert(1)</script>', "<b>")
    assert "<script>" not in page and "&lt;b&gt;" in page


def test_the_gate_is_on_by_default():
    assert gate.ENABLED is True
    assert gate.needs_password("/dashboard") and not gate.needs_password("/health")


def test_a_wrong_password_is_slowed_down(monkeypatch):
    monkeypatch.setattr(gate, "PASSWORD_HASH", gate.make_hash(PASSWORD, n=2**10))
    slept = []
    monkeypatch.setattr(gate.time, "sleep", slept.append)
    assert gate.check("wrong") is False and slept == [gate.WRONG_DELAY_SECONDS]
    assert gate.check(PASSWORD) is True and slept == [gate.WRONG_DELAY_SECONDS]


def test_the_cookie_lasts_the_stated_days():
    value = gate.token(now=1_000_000)
    assert int(value.split(".")[0]) == 1_000_000 + gate.DAYS * 86400
    assert gate.token_valid(value, now=1_000_000 + gate.DAYS * 86400 - 1)
    assert not gate.token_valid(value, now=1_000_000 + gate.DAYS * 86400 + 1)


