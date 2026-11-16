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
    monkeypatch.setattr(gate, "_next_try", {})
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


def test_when_every_check_slot_stays_taken_the_page_says_busy(monkeypatch):
    monkeypatch.setattr(gate, "WAIT_SECONDS", 0.05)
    monkeypatch.setattr(gate, "_next_try", {})
    held = [gate._checking.acquire() for _ in range(gate.CHECKS_AT_ONCE)]
    try:
        with pytest.raises(gate.Busy):
            gate.check("anything", "a")
    finally:
        for _ in held:
            gate._checking.release()


def test_the_login_page_escapes_where_it_sends_you():
    page = gate.login_page('/x"><script>alert(1)</script>', "<b>")
    assert "<script>" not in page and "&lt;b&gt;" in page


def test_the_gate_is_on_by_default():
    assert gate.ENABLED is True
    assert gate.needs_password("/dashboard") and not gate.needs_password("/health")


def test_a_wrong_guess_slows_only_that_client(monkeypatch):
    monkeypatch.setattr(gate, "PASSWORD_HASH", gate.make_hash(PASSWORD, n=2**10))
    monkeypatch.setattr(gate, "_next_try", {})
    assert gate.check("wrong", "stranger") is False
    with pytest.raises(gate.TooSoon):
        gate.check(PASSWORD, "stranger")
    # A teammate elsewhere isn't held up by the stranger's guesses.
    assert gate.check(PASSWORD, "teammate") is True
    monkeypatch.setattr(gate, "_next_try", {"stranger": 0.0})
    assert gate.check(PASSWORD, "stranger") is True


def test_the_client_is_the_last_forwarded_address():
    assert gate.client_of("1.2.3.4, 5.6.7.8", "10.0.0.1") == "5.6.7.8"  # a client can only forge the first
    assert gate.client_of("", "10.0.0.1") == "10.0.0.1"
    assert gate.client_of(None, "127.0.0.1") == "127.0.0.1"


def test_a_guess_right_after_a_wrong_one_is_told_to_wait(site, monkeypatch):
    monkeypatch.setattr(gate, "WRONG_DELAY_SECONDS", 30)
    assert sign_in(site, "not it")[0] == 401
    status, headers, _ = sign_in(site)
    assert status == 429 and "Set-Cookie" not in headers


def test_next_with_line_breaks_or_non_ascii_never_reaches_a_header(site):
    _, headers, _ = sign_in(site)
    cookie = cookie_from(headers)
    for target in ("/x%0d%0aSet-Cookie:%20pwn=1", "/x%0aX-Evil:%201", "/%E2%98%83", "/x%09y"):
        status, headers, _ = ask(site, "GET", f"/login?next={target}", headers={"Cookie": cookie})
        assert status == 303 and headers["Location"] == "/dashboard", target
        assert "pwn" not in str(headers) and "X-Evil" not in headers
    form = urlencode({"password": PASSWORD, "next": "/☃\r\nX-Evil: 1"}).encode()
    status, headers, _ = ask(site, "POST", "/login", form, {"Content-Type": "application/x-www-form-urlencoded"})
    assert status == 303 and headers["Location"] == "/dashboard" and "X-Evil" not in headers
    assert gate.safe_next("/db?table=ledger") == "/db?table=ledger"


def test_a_form_that_trickles_in_too_slowly_is_dropped(site, monkeypatch):
    import socket
    import time

    monkeypatch.setattr(gate, "FORM_SECONDS", 0.5)
    started = time.monotonic()
    with socket.create_connection(("127.0.0.1", site), timeout=10) as s:
        # Promise 100 bytes, send 9, then go quiet: the server answers 408 at the deadline.
        s.sendall(b"POST /login HTTP/1.0\r\nContent-Length: 100\r\n\r\npassword=")
        reply = s.recv(200)
    assert reply.startswith(b"HTTP/1.0 408")
    assert time.monotonic() - started < 5


def test_the_cookie_lasts_the_stated_days():
    value = gate.token(now=1_000_000)
    assert int(value.split(".")[0]) == 1_000_000 + gate.DAYS * 86400
    assert gate.token_valid(value, now=1_000_000 + gate.DAYS * 86400 - 1)
    assert not gate.token_valid(value, now=1_000_000 + gate.DAYS * 86400 + 1)



def test_guesses_sent_all_at_once_get_one_check_between_them(site, monkeypatch):
    """Marshall's #136 review: a burst used to slip past the delay while the first check ran."""
    monkeypatch.setattr(gate, "WRONG_DELAY_SECONDS", 30)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=20) as pool:
        statuses = sorted(pool.map(lambda _: sign_in(site, "not it")[0], range(20)))
    assert statuses.count(401) == 1 and statuses.count(429) == 19


def test_a_double_submitted_right_password_signs_in_twice(monkeypatch):
    """A double-click sends the form twice; the second waits for the first check, then passes."""
    monkeypatch.setattr(gate, "PASSWORD_HASH", gate.make_hash(PASSWORD, n=2**14))
    monkeypatch.setattr(gate, "WRONG_DELAY_SECONDS", 30)
    monkeypatch.setattr(gate, "_next_try", {})
    import threading
    running, most, lock = [0], [0], threading.Lock()
    real = gate.password_matches
    def counted(password, stored=None):
        with lock:
            running[0] += 1
            most[0] = max(most[0], running[0])
        try:
            gate.time.sleep(0.2)
            return real(password, stored)
        finally:
            with lock:
                running[0] -= 1
    monkeypatch.setattr(gate, "password_matches", counted)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(lambda _: gate.check(PASSWORD, "teammate"), range(2))) == [True, True]
    assert most[0] == 1  # one check at a time for the same client


def test_guesses_queued_while_busy_still_wait_out_the_delay(monkeypatch):
    """Two guesses that queue behind other clients' checks are checked one at a time, not together."""
    import threading
    monkeypatch.setattr(gate, "PASSWORD_HASH", gate.make_hash(PASSWORD, n=2**10))
    monkeypatch.setattr(gate, "WRONG_DELAY_SECONDS", 30)
    monkeypatch.setattr(gate, "_next_try", {})
    results = []
    def guess():
        try:
            results.append(gate.check("wrong", "stranger"))
        except gate.TooSoon:
            results.append("too soon")
    held = [gate._checking.acquire() for _ in range(gate.CHECKS_AT_ONCE)]
    first = threading.Thread(target=guess)
    first.start()
    gate.time.sleep(0.3)
    second = threading.Thread(target=guess)
    second.start()
    gate.time.sleep(0.3)
    for _ in held:
        gate._checking.release()
    first.join(); second.join()
    assert sorted(results, key=str) == [False, "too soon"]


def test_a_busy_page_doesnt_count_as_a_guess(monkeypatch):
    monkeypatch.setattr(gate, "PASSWORD_HASH", gate.make_hash(PASSWORD, n=2**10))
    monkeypatch.setattr(gate, "WRONG_DELAY_SECONDS", 30)
    monkeypatch.setattr(gate, "WAIT_SECONDS", 0.05)
    monkeypatch.setattr(gate, "_next_try", {})
    held = [gate._checking.acquire() for _ in range(gate.CHECKS_AT_ONCE)]
    try:
        with pytest.raises(gate.Busy):
            gate.check("wrong", "teammate")
    finally:
        for _ in held:
            gate._checking.release()
    assert gate.check(PASSWORD, "teammate") is True
    assert "teammate" not in gate._in_flight


def test_a_full_list_drops_finished_delays_before_anyone_waiting(monkeypatch):
    monkeypatch.setattr(gate, "PASSWORD_HASH", gate.make_hash(PASSWORD, n=2**10))
    monkeypatch.setattr(gate, "MAX_CLIENTS", 3)
    monkeypatch.setattr(gate, "WRONG_DELAY_SECONDS", 30)
    monkeypatch.setattr(gate, "_next_try", {"old": 0.0, "older": 0.0, "stranger": gate.time.monotonic() + 30})
    assert gate.check("wrong", "new") is False
    assert set(gate._next_try) == {"stranger", "new"}
    with pytest.raises(gate.TooSoon):
        gate.check(PASSWORD, "stranger")
    # Only when every remembered client is still waiting does the list start over.
    monkeypatch.setattr(gate, "MAX_CLIENTS", 2)
    assert gate.check("wrong", "newest") is False
    assert set(gate._next_try) == {"newest"}
