"""One shared password in front of the container's pages.

    /login     the password page (GET shows it, POST checks it)
    /logout    forget this browser's sign-in

Everything the worker serves needs the password except `/health` (sync outcomes only) and the
two pages above. A browser that gives the right password gets a cookie good for `DAYS` days.

Why not Microsoft sign-in: the institute's tenant makes an administrator approve every app
before anyone can sign in to it (15 Nov 2026), so Container Apps' built-in sign-in can't be
used without IT. Why not a Container App secret: our deploy logins can't store secrets.

So the code holds only a scrypt hash of the password (`PASSWORD_HASH`). The password is random,
the hash is slow to try, and wrong guesses are slowed down, so the hash can sit in a public
repository. To change the password:

    python -m vending.gate hash          # asks for the new password, prints the line to paste

Changing it signs every browser out. The cookie is signed with a key derived from the pages'
database setting (already a Container App secret) and the hash, so nothing new has to be stored.
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import os
import sys
import threading
import time
from html import escape
from http.cookies import CookieError, SimpleCookie
from urllib.parse import parse_qs, quote, urlsplit

from vending import settings

# scrypt:N:r:p:salt:hash, all base64. Set by Kevin, 15 Nov 2026.
PASSWORD_HASH = ("scrypt:32768:8:1:SjkoTco36+Cxd2DZCynahg==:"
                 "+tfj3rezR96wAN4HNmxJDqR8jIgHQ+T7zv9TdldXMNdaY3SPyDWSyn/KByYHbe8WnYo8yKqqrAomK+zgGJlTwQ==")
# Tests switch the gate off for the tests that aren't about it (tests/conftest.py).
ENABLED = True

COOKIE = "vend_session"
DAYS = 30
LOGIN, LOGOUT = "/login", "/logout"
OPEN = ("/health", LOGIN, LOGOUT)
# The pages' own database setting (`vending.pages.READER`); named here so the gate needs no driver.
KEY_SETTING = "READONLY_DATABASE_URL"
# After a wrong password is checked, the same client waits this long before its next guess is.
WRONG_DELAY_SECONDS = 1.0
# scrypt checks running at once (each takes ~32 MB for a moment), and how long one waits for a turn.
CHECKS_AT_ONCE = 2
WAIT_SECONDS = 5
# Clients remembered for the per-client delay; past this, finished delays are dropped first.
MAX_CLIENTS = 10_000
MAX_FORM_BYTES = 2048
# Seconds a client has to send the whole password form, however slowly it trickles in.
FORM_SECONDS = 10
CSP = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"

_checking = threading.BoundedSemaphore(CHECKS_AT_ONCE)
_wrong_lock = threading.Condition()
_next_try: dict[str, float] = {}
# Clients whose password is being checked right now: each client has at most one check at a time.
_in_flight: set[str] = set()
# Used only when the database setting is missing (a local run): sign-ins then last until a restart.
_fallback_key = os.urandom(32)


def _scrypt(password: str, n: int, r: int, p: int, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=64 * 1024 * 1024)


def make_hash(password: str, n: int = 2**15, r: int = 8, p: int = 1) -> str:
    salt = os.urandom(16)
    digest = _scrypt(password, n, r, p, salt)
    return f"scrypt:{n}:{r}:{p}:{base64.b64encode(salt).decode()}:{base64.b64encode(digest).decode()}"


def password_matches(password: str, stored: str | None = None) -> bool:
    _, n, r, p, salt, digest = (stored or PASSWORD_HASH).split(":")
    expected = base64.b64decode(digest)
    return hmac.compare_digest(_scrypt(password, int(n), int(r), int(p), base64.b64decode(salt)), expected)


class Busy(Exception):
    """Every password-check slot is taken."""


class TooSoon(Exception):
    """This client's last guess was checked and wrong less than `WRONG_DELAY_SECONDS` ago."""


def check(password: str, client: str = "") -> bool:
    """Whether `password` is right. Each client has at most one check at a time: a request that
    arrives while the same client's check runs waits for it to finish. After a wrong guess the
    client gets TooSoon until `WRONG_DELAY_SECONDS` have passed, so guesses sent all at once, or
    queued while the server is busy, still get one check per delay. Busy when no check slot frees
    up within `WAIT_SECONDS`; that doesn't count as a guess. The delay is per client address, so a
    stranger elsewhere can't slow the team, but one behind the same address (a shared NAT) can."""
    deadline = time.monotonic() + WAIT_SECONDS
    with _wrong_lock:
        while client in _in_flight:
            left = deadline - time.monotonic()
            if left <= 0:
                raise Busy
            _wrong_lock.wait(left)
        if _next_try.get(client, 0.0) > time.monotonic():
            raise TooSoon
        _in_flight.add(client)
    checked = right = False
    try:
        if not _checking.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise Busy
        try:
            checked = True
            right = bool(password) and len(password) <= 256 and password_matches(password)
        finally:
            _checking.release()
    finally:
        with _wrong_lock:
            _in_flight.discard(client)
            if checked and not right:  # a check that raised counts as a wrong guess
                if len(_next_try) >= MAX_CLIENTS:
                    _forget_expired(time.monotonic())
                _next_try[client] = time.monotonic() + WRONG_DELAY_SECONDS
            _wrong_lock.notify_all()
    return right


def _forget_expired(now: float) -> None:
    """Make room: drop clients whose delay is over, and only if that frees nothing, everyone."""
    for key in [k for k, until in _next_try.items() if until <= now]:
        del _next_try[key]
    if len(_next_try) >= MAX_CLIENTS:
        _next_try.clear()


def client_of(forwarded_for: str | None, peer: str) -> str:
    """Who is asking: the last address Azure's ingress put in X-Forwarded-For (a client can't
    forge that one), or the socket's peer on a local run."""
    if forwarded_for:
        last = forwarded_for.rsplit(",", 1)[-1].strip()
        if last:
            return last
    return peer


def _key() -> bytes:
    base = settings.get(KEY_SETTING)
    if not base:
        return _fallback_key
    return hashlib.sha256(b"vending.gate|" + PASSWORD_HASH.encode() + b"|" + base.encode()).digest()


def _sign(expires: int) -> str:
    return hmac.new(_key(), f"{COOKIE}|{expires}".encode(), hashlib.sha256).hexdigest()


def token(now: float | None = None) -> str:
    expires = int((time.time() if now is None else now) + DAYS * 86400)
    return f"{expires}.{_sign(expires)}"


def token_valid(value: str, now: float | None = None) -> bool:
    expires, _, signature = value.partition(".")
    if not expires.isdigit() or not signature:
        return False
    if int(expires) < (time.time() if now is None else now):
        return False
    return hmac.compare_digest(signature, _sign(int(expires)))


def signed_in(cookie_header: str | None) -> bool:
    if not ENABLED:
        return True
    if not cookie_header:
        return False
    try:
        jar = SimpleCookie(cookie_header)
    except CookieError:
        return False
    return COOKIE in jar and token_valid(jar[COOKIE].value)


def needs_password(path: str) -> bool:
    return ENABLED and path not in OPEN


def session_cookie() -> str:
    return f"{COOKIE}={token()}; Max-Age={DAYS * 86400}; Path=/; HttpOnly; Secure; SameSite=Lax"


def cleared_cookie() -> str:
    return f"{COOKIE}=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Lax"


def safe_next(value: str | None) -> str:
    """Where to go after signing in: a path on this site, never another site. Only printable ASCII,
    because it goes into a Location header (urlsplit quietly drops tabs and line breaks, so a
    check on its result alone would let `%0d%0a` add headers)."""
    if not value or any(not 0x21 <= ord(c) <= 0x7E for c in value):
        return "/dashboard"
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/dashboard"
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return "/dashboard"
    return value


def login_location(target: str) -> str:
    return f"{LOGIN}?next={quote(target, safe='/')}"


def next_from_query(target: str) -> str:
    return safe_next((parse_qs(urlsplit(target).query).get("next") or [None])[0])


def parse_form(body: bytes) -> tuple[str, str]:
    """(password, next) from a posted form."""
    form = parse_qs(body.decode("utf-8", "replace"), max_num_fields=5)
    return (form.get("password") or [""])[0], safe_next((form.get("next") or [None])[0])


def login_page(next_path: str, message: str = "") -> str:
    note = f'<p class="bad" role="alert">{escape(message)}</p>' if message else ""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Sign in · Agentic Vending</title>
<style>
  :root {{ --bg: #f5f6f8; --fg: #1b1f24; --muted: #5a6370; --line: #dce0e5; --panel: #ffffff;
          --accent: #2f6fde; --on-accent: #ffffff; --bad: #b42318; color-scheme: light; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg: #111418; --fg: #e8eaed; --muted: #9aa3ae; --line: #2a3038; --panel: #181c21;
            --accent: #6d9cf0; --on-accent: #0b1220; --bad: #f97066; color-scheme: dark; }}
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; background: var(--bg); color: var(--fg);
         font: 16px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; padding: 16px; }}
  form {{ width: 100%; max-width: 22rem; background: var(--panel); border: 1px solid var(--line);
          border-radius: 12px; padding: 1.5rem; display: grid; gap: .9rem; }}
  h1 {{ margin: 0; font-size: 1.4rem; }}
  p {{ margin: 0; }}
  .muted {{ color: var(--muted); font-size: .9rem; }}
  .bad {{ color: var(--bad); font-weight: 600; }}
  label {{ display: grid; gap: .3rem; font-weight: 500; }}
  input {{ font: inherit; padding: .6rem .7rem; border-radius: 8px; border: 1px solid var(--line);
           background: var(--bg); color: var(--fg); width: 100%; }}
  input:focus-visible, button:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}
  button {{ font: inherit; font-weight: 600; padding: .6rem; border-radius: 8px; border: 0;
            background: var(--accent); color: var(--on-accent); cursor: pointer; }}
</style>
</head>
<body>
<form method="post" action="{LOGIN}">
  <h1>Agentic Vending</h1>
  <p class="muted">The team's agent dashboard. Enter the shared password to continue.</p>
  {note}
  <label>Password<input type="password" name="password" autocomplete="current-password" required autofocus></label>
  <input type="hidden" name="next" value="{escape(next_path)}">
  <button type="submit">Sign in</button>
  <p class="muted">This browser stays signed in for {DAYS} days.</p>
</form>
</body>
</html>
"""


def main(argv: list[str]) -> int:
    if argv != ["hash"]:
        print("usage: python -m vending.gate hash", file=sys.stderr)
        return 2
    first = getpass.getpass("New password: ")
    if len(first) < 16:
        print("Use at least 16 characters.", file=sys.stderr)
        return 1
    if getpass.getpass("Again: ") != first:
        print("The two didn't match.", file=sys.stderr)
        return 1
    print(f'PASSWORD_HASH = "{make_hash(first)}"')
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
