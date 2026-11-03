"""`request_catalog_product` (tool #11): ask the instructors to add a drink to the catalog.

    POST /api/v1/product-requests  {name, size, line?, flavor?, note?}

It costs nothing and orders nothing, but each request goes to a person, and
too many open ones get a `429`. So before sending, it refuses, all at once:
a field the bridge would reject, a size that isn't a liquid volume, alcohol or
THC, a product already in the catalog, one we have already requested, and any
request over our own weekly limit (larger while the catalog is still small).

The bridge has no route that lists our requests, so `product_requests` is our
only record, and the duplicate check reads it. Runs inside the write gate, so
the halt lock is held while the request is in flight.

The POST is never retried: a retry after a lost answer could file it twice.
Just before sending, a `sending` row is committed on its own short connection,
outside the gate's transaction, so a crash at any later point (before the answer
is stored, or before the gate commits) leaves it behind. What happens next:

- accepted: the row becomes `pending` with the bridge's ID, in the gate's
  transaction, so it commits with the `action_log` row;
- certainly not sent or refused (no connection, a 4xx, a 429): the row is
  deleted, again on its own connection, because a refusal rolls the gate back;
- no answer (a timeout or a 5xx): the row becomes `unknown`.

A row left `sending` or `unknown` may or may not exist on the bridge. The
duplicate check blocks a resend of it until a person has checked.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable

import psycopg
from psycopg.types.json import Jsonb

from vending.bridge import Bridge, BridgeError
from vending.pricing import Refusal
from vending.tools import WriteReply
from vending.write_gate import Refused

# Seconds to wait for the bridge's answer to the POST. Tests shorten it.
TIMEOUT_SECONDS = 20
# The live schema's limits (ProductRequestCreate, checked 2 Nov 2026).
MAX_FIELD = 100
MAX_NOTE = 500
# Our own limit on requests filed in any 7 days, so the agent can't flood the
# instructors. While the catalog is small there's more worth asking for, so the
# limit is higher until the catalog reaches SMALL_CATALOG products.
# PLACEHOLDERS: these three numbers are guesses, not a team decision. See
# "Open questions for the team" in PR #14 and docs/mcp-server.md.
WEEKLY_LIMIT = 3
STARTUP_WEEKLY_LIMIT = 15
SMALL_CATALOG = 25

# A size the bridge would sell as a drink: "20 oz bottle", "12 fl oz can", "500 mL".
VOLUME = re.compile(r"\d+(?:\.\d+)?\s*(?:fl\.?\s*oz|oz|ounces?|ml|l|liters?|litres?)\b", re.IGNORECASE)
# Soft drinks whose names contain an alcohol word. Removed before the check.
SOFT_DRINKS = re.compile(r"\b(?:root|ginger|birch)\s+beer\b|\bginger\s+ale\b", re.IGNORECASE)
NOT_ALLOWED = re.compile(
    r"\b(?:beer|wine|ale|lager|ipa|stout|vodka|whiske?y|bourbon|rum|tequila|gin|liquor|liqueur|spirits?|"
    r"sake|soju|mead|alcohol|alcoholic|spiked|hard\s+(?:seltzer|cider|tea|lemonade|kombucha)|"
    r"thc|cbd|delta[\s-]?[89]|cannabis|marijuana|hemp)\b",
    re.IGNORECASE,
)


class CatalogRequestReply(WriteReply):
    request_id: str | None = None
    status: str | None = None


def words(*parts: str | None) -> list[str]:
    """The lower-case words of a product description, with "20oz" split and "fl oz" as "oz"."""
    text = " ".join(p for p in parts if p).lower()
    text = re.sub(r"\bfl\.?\s*oz\b", "oz", text)
    text = re.sub(r"(\d)([a-z])", r"\1 \2", text)
    return sorted(set(re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", text)))


def check_fields(name: str, size: str, line: str | None, flavor: str | None, note: str | None) -> list[Refusal]:
    """Every refusal that needs only the request itself."""
    refusals = []
    for field, value in (("name", name), ("size", size), ("line", line), ("flavor", flavor)):
        if value is not None and not 1 <= len(value) <= MAX_FIELD:
            refusals.append(Refusal("bad_field", f"{field} must be 1 to {MAX_FIELD} characters"))
    if note is not None and len(note) > MAX_NOTE:
        refusals.append(Refusal("bad_field", f"note must be at most {MAX_NOTE} characters"))
    if not VOLUME.search(size):
        refusals.append(Refusal("not_a_drink", f"size {size!r} isn't a liquid volume; give one like \"20 oz bottle\" or \"12 oz can\""))
    described = SOFT_DRINKS.sub(" ", " ".join(p for p in (name, line, flavor, size) if p))
    if found := NOT_ALLOWED.search(described):
        refusals.append(Refusal("alcohol_or_thc", f"{found.group(0)!r}: the machine can't sell alcohol or THC products"))
    return refusals


def already_requested(conn: psycopg.Connection, wanted: list[str]) -> list[Refusal]:
    """Our earlier requests whose description covers every word of this one.

    One way only, like the catalog check: "Cherry 20 oz" after "Cherry 20 oz
    bottle" is a duplicate, but "Cherry Zero 20 oz" after "Cherry 20 oz" isn't.
    """
    rows = conn.execute(
        "select bridge_id, name, size, status from product_requests where %s::text[] <@ words order by id",
        (wanted,),
    ).fetchall()
    return [Refusal("already_requested",
                    f"we already asked for {name} ({size}): request {bridge_id or 'with no ID'}, status {status}"
                    + (" (sent, but no answer was stored; a person must check before it is sent again)"
                       if status in ("sending", "unknown") else ""))
            for bridge_id, name, size, status in rows]


def already_in_catalog(catalog: list[dict], wanted: list[str]) -> list[Refusal]:
    """Catalog products whose description covers every word of the request."""
    matches = [p for p in catalog
               if set(wanted) <= set(words(p.get("name"), p.get("flavor"), p.get("size")))]
    if not matches:
        return []
    listed = ", ".join(f"{p['sku']} ({p['name']})" for p in matches)
    return [Refusal("already_in_catalog", f"already in the catalog: {listed}; order it by SKU instead")]


def over_weekly_limit(conn: psycopg.Connection, catalog_size: int) -> list[Refusal]:
    """A refusal if we've already filed this week's share of requests."""
    limit = STARTUP_WEEKLY_LIMIT if catalog_size < SMALL_CATALOG else WEEKLY_LIMIT
    (recent,) = conn.execute(
        "select count(*) from product_requests where requested_at > now() - interval '7 days'").fetchone()
    if recent < limit:
        return []
    why = (f"the catalog has {catalog_size} products, under {SMALL_CATALOG}, so the start-up limit applies"
           if limit == STARTUP_WEEKLY_LIMIT else f"the catalog has {catalog_size} products")
    return [Refusal("request_limit", f"we've filed {recent} product requests in the last 7 days, the limit is "
                                     f"{limit} ({why}); pick the most wanted ones and try again later")]


def request_catalog_product(name: str, size: str, line: str | None, flavor: str | None, note: str | None,
                            bridge: Callable[[], Bridge], connect: Callable[[], psycopg.Connection],
                            ) -> Callable[[psycopg.Connection], CatalogRequestReply]:
    """The write action for the gate. `bridge` is called only once the halt check has passed.

    `connect` opens the short, separate connection that commits the `sending`
    row before the POST; it must reach the same database as the gate's.
    """
    # The bridge refuses an empty optional field, so send it as absent.
    line, flavor, note = (v.strip() or None if v is not None else None for v in (line, flavor, note))
    name, size = name.strip(), size.strip()
    body = {k: v for k, v in {"name": name, "size": size, "line": line, "flavor": flavor, "note": note}.items()
            if v is not None}
    wanted = words(name, flavor, size)

    def action(conn: psycopg.Connection) -> CatalogRequestReply:
        refusals = check_fields(name, size, line, flavor, note) + already_requested(conn, wanted)
        client = bridge()
        try:
            catalog = json.loads(client._get("/api/v1/products")).get("products", [])
        except BridgeError as error:
            if refusals:
                raise Refused(refusals) from None
            return CatalogRequestReply(success=False, message=f"Couldn't read the catalog to check for duplicates ({error}); nothing was sent.")
        refusals += already_in_catalog(catalog, wanted) + over_weekly_limit(conn, len(catalog))
        if refusals:
            raise Refused(refusals)
        row = _save_sending(connect, body, wanted)
        try:
            return _send(conn, client, body, row, connect)
        except Refused:
            _forget(connect, row)
            raise

    return action


def _save_sending(connect: Callable[[], psycopg.Connection], body: dict, wanted: list[str]) -> int:
    """Commit a `sending` row on its own connection, so a crash after this can't lose it.

    The connection commits as the `with` block closes it.
    """
    with connect() as side:
        (row,) = side.execute(
            "insert into product_requests (name, size, line, flavor, note, words, status)"
            " values (%(name)s, %(size)s, %(line)s, %(flavor)s, %(note)s, %(words)s, 'sending') returning id",
            {**_columns(body), "words": wanted},
        ).fetchone()
    return row


def _forget(connect: Callable[[], psycopg.Connection], row: int) -> None:
    """Delete the `sending` row of a request the bridge certainly didn't file."""
    with connect() as side:
        side.execute("delete from product_requests where id = %s and status = 'sending'", (row,))


def _send(conn: psycopg.Connection, client: Bridge, body: dict, row: int,
          connect: Callable[[], psycopg.Connection]) -> CatalogRequestReply:
    request = urllib.request.Request(
        client.base_url + "/api/v1/product-requests", data=json.dumps(body).encode(), method="POST",
        headers={"X-API-Key": client.api_key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            answer = json.loads(response.read())
    except urllib.error.HTTPError as error:
        detail = error.read()[:300].decode(errors="replace")
        if error.code == 429:
            raise Refused([Refusal("too_many_open_requests",
                                   "the bridge has too many of our product requests open; wait for earlier ones to be decided")]) from None
        if error.code >= 500:
            return _unknown(conn, row, f"the bridge answered {error.code}")
        _forget(connect, row)
        return CatalogRequestReply(success=False, message=f"The bridge refused the request ({error.code}: {detail}); nothing was stored.")
    except urllib.error.URLError as error:
        # urllib raises URLError only while connecting or sending, so the bridge never got it.
        _forget(connect, row)
        return CatalogRequestReply(success=False, message=f"Couldn't reach the bridge ({error.reason}); nothing was sent.")
    except (OSError, ValueError) as error:
        # Sent, but the answer timed out, broke off or wasn't JSON.
        return _unknown(conn, row, f"{type(error).__name__}: {error}")
    return _record_accepted(conn, row, body, answer)


def _record_accepted(conn: psycopg.Connection, row: int, body: dict, answer: dict) -> CatalogRequestReply:
    """Store the bridge's ID on our row, in the gate's transaction."""
    status = answer.get("status", "pending")
    conn.execute(
        "update product_requests set bridge_id = %s, status = %s, last_response = %s where id = %s",
        (answer["id"], status, Jsonb(answer), row),
    )
    return CatalogRequestReply(
        success=True, count=1, request_id=answer["id"], status=status,
        message=f"Requested {body['name']} ({body['size']}): product request {answer['id']} is "
                f"{status}. If approved it appears in the catalog, maybe under another name.",
    )


def _unknown(conn: psycopg.Connection, row: int, why: str) -> CatalogRequestReply:
    conn.execute("update product_requests set status = 'unknown' where id = %s", (row,))
    return CatalogRequestReply(
        success=False, status="unknown",
        message=f"Sent, but no usable answer came back ({why}), so the request may or may not exist. "
                "Don't send it again; a person must check the bridge first.",
    )


def _columns(body: dict) -> dict:
    return {k: body.get(k) for k in ("name", "size", "line", "flavor", "note")}
