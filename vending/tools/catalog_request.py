"""`request_catalog_product` (tool #11): ask the instructors to add a drink to the catalog.

    POST /api/v1/product-requests  {name, size, line?, flavor?, note?}

It costs nothing and orders nothing, but each request goes to a person, and
too many open ones get a `429`. So before sending, it refuses, all at once:
a field the bridge would reject, a size that isn't a liquid volume, alcohol or
THC, a product already in the catalog, one we have already requested, and any
request over our own weekly limit (larger while the catalog is still small).

The bridge has no route that lists our requests, so `product_requests` is our
only record, and the duplicate check reads it. Runs inside the write gate, so
the halt lock is held while the request is in flight; neither bridge call
retries, so the lock is held for at most two timeouts.

The POST is never retried: a retry after a lost answer could file it twice.
Every change to our row is committed on its own short connection, outside the
gate's transaction, so nothing the bridge told us is lost if the gate later
rolls back (a failed log insert, a dropped connection, a crash):

- just before sending: a `sending` row;
- accepted: the bridge's ID and status;
- accepted but the answer has no ID, or no answer at all (a timeout or a
  5xx): `unknown`;
- certainly not sent or refused (no connection, a 4xx, a 429): the row is
  deleted.

A row left `sending` or `unknown` may or may not exist on the bridge. The
duplicate check blocks a resend of it until a person checks the bridge and
runs `python -m vending.product_requests resolve`.
"""

from __future__ import annotations

import re
from collections.abc import Callable

import psycopg
from psycopg.types.json import Jsonb

from vending.bridge import Bridge, BridgeAnswered, BridgeError, BridgeNoAnswer, BridgeUnreachable
from vending.pricing import Refusal
from vending.tools import WriteReply
from vending.write_gate import Refused

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
# Statuses the bridge can give a request that our table stores as they are.
KNOWN_STATUSES = {"pending", "approved", "rejected"}

# A size the bridge would sell as a drink: "20 oz bottle", "12 fl oz can", "500 mL".
VOLUME = re.compile(r"\d+(?:\.\d+)?\s*(?:fl\.?\s*oz|oz|ounces?|ml|l|liters?|litres?)\b", re.IGNORECASE)
# Words that don't tell two drinks apart: how it's packed, or that it's the
# default flavor. The duplicate checks ignore them, so "20 oz" and "20 oz
# bottle" are the same request, and "Coca-Cola" matches "Coca-Cola Original".
IGNORED_WORDS = {"bottle", "bottles", "can", "cans", "pack", "packs", "pk", "original", "classic", "regular"}
# Soft drinks whose names contain an alcohol word. Removed before the check.
SOFT_DRINKS = re.compile(
    r"\b(?:root|ginger|birch)\s+beer\b|\bginger\s+ale\b|\bmocktails?\b"
    r"|\bvirgin\s+(?:mojito|margarita|daiquiri|pi[nñ]a\s+colada|mary)s?\b"
    r"|\bjuice\s+cocktails?\b|\bsangr[ií]a\s+se[nñ]orial\b|\bkola\s+champagne\b",
    re.IGNORECASE,
)
NOT_ALLOWED = re.compile(
    r"\b(?:beer|wine|ale|lager|ipa|stout|vodka|whiske?y|bourbon|rum|tequila|gin|liquor|liqueur|spirits?|"
    r"sake|soju|mead|alcohol|alcoholic|spiked|hard\s+(?:seltzer|cider|tea|lemonade|kombucha)|"
    r"champagne|prosecco|cava|sangria|margaritas?|mojitos?|daiquiris?|mimosas?|martinis?|cocktails?|"
    r"bellinis?|merlot|chardonnay|pinot|cabernet|sauvignon|riesling|moscato|zinfandel|malbec|shiraz|"
    r"brandy|cognac|schnapps|absinthe|vermouth|shandy|radler|"
    r"thc|cbd|delta[\s-]?[89]|cannabis|marijuana|hemp)\b",
    re.IGNORECASE,
)


class CatalogRequestReply(WriteReply):
    request_id: str | None = None
    status: str | None = None


def words(*parts: str | None) -> list[str]:
    """The lower-case words of a product description, for the duplicate checks.

    Units are normalised ("20oz", "20 ounce" and "20 fl oz" all become "20 oz";
    "500 milliliters" becomes "500 ml"), and IGNORED_WORDS are dropped.
    """
    text = " ".join(p for p in parts if p).lower()
    text = re.sub(r"(\d)([a-z])", r"\1 \2", text)
    text = re.sub(r"\bfl\.?\s*oz\b|\b(?:fluid\s+)?ounces?\b", "oz", text)
    text = re.sub(r"\bmillilit(?:er|re)s?\b", "ml", text)
    text = re.sub(r"\blit(?:er|re)s?\b", "l", text)
    return sorted(set(re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", text)) - IGNORED_WORDS)


def same_drink(wanted: list[str], other: list[str]) -> bool:
    """True when two descriptions have the same words, once ignored words are dropped.

    Exact, not one-way: "Sprite 20 oz" is not "Sprite Cherry 20 oz", nor the
    reverse. Rows stored before a word was added to IGNORED_WORDS still match.
    """
    return set(wanted) - IGNORED_WORDS == set(other) - IGNORED_WORDS


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
    """Our earlier requests for the same drink (see `same_drink`).

    "Cherry 20 oz" after "Cherry 20 oz bottle" is a duplicate; "Cherry Zero
    20 oz" after "Cherry 20 oz" isn't, and nor is "Cherry 20 oz" after "Cherry
    Zero 20 oz". Compared in Python: the table holds one row per request, so
    it stays small.
    """
    rows = [(row, bridge_id, name, size, status)
            for row, bridge_id, name, size, status, stored in conn.execute(
                "select id, bridge_id, name, size, status, words from product_requests order by id")
            if same_drink(wanted, stored)]
    return [Refusal("already_requested",
                    f"we already asked for {name} ({size}): request {bridge_id or 'with no ID'}, status {status}"
                    + (f" (sent, but no answer was stored; a person must check the bridge, then run "
                       f"`python -m vending.product_requests resolve {row}`)" if status in ("sending", "unknown") else ""))
            for row, bridge_id, name, size, status in rows]


def already_in_catalog(catalog: list[dict], wanted: list[str]) -> list[Refusal]:
    """Catalog products that are the same drink as the request (see `same_drink`)."""
    matches = [p for p in catalog if same_drink(wanted, words(p.get("name"), p.get("flavor"), p.get("size")))]
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

    `connect` opens the short, separate connections that record each step;
    they must reach the same database as the gate's.
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
            catalog = client.products()
        except BridgeError as error:
            if refusals:
                raise Refused(refusals) from None
            return CatalogRequestReply(success=False, message=f"Couldn't read the catalog to check for duplicates ({error}); nothing was sent.")
        refusals += already_in_catalog(catalog, wanted) + over_weekly_limit(conn, len(catalog))
        if refusals:
            raise Refused(refusals)
        row = _save_sending(connect, body, wanted)
        try:
            return _send(client, body, row, connect)
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


def _send(client: Bridge, body: dict, row: int, connect: Callable[[], psycopg.Connection]) -> CatalogRequestReply:
    try:
        answer = client.post("/api/v1/product-requests", body)
    except BridgeAnswered as error:
        if error.status == 429:
            raise Refused([Refusal("too_many_open_requests",
                                   "the bridge has too many of our product requests open; wait for earlier ones to be decided")]) from None
        if error.status >= 500:
            return _unknown(connect, row, f"the bridge answered {error.status}", None)
        _forget(connect, row)
        return CatalogRequestReply(success=False, message=f"The bridge refused the request ({error}); nothing was stored.")
    except BridgeUnreachable as error:
        _forget(connect, row)
        return CatalogRequestReply(success=False, message=f"Couldn't reach the bridge ({error}); nothing was sent.")
    except BridgeNoAnswer as error:
        return _unknown(connect, row, str(error), None)
    return _record_answer(connect, row, body, answer)


def _record_answer(connect: Callable[[], psycopg.Connection], row: int, body: dict,
                   answer: object) -> CatalogRequestReply:
    """Store the bridge's answer on its own connection, before anything else can fail."""
    request_id = answer.get("id") if isinstance(answer, dict) else None
    if not request_id:
        return _unknown(connect, row, "the bridge accepted it, but its answer has no request ID", answer)
    status = answer.get("status") if answer.get("status") in KNOWN_STATUSES else "pending"
    with connect() as side:
        side.execute(
            "update product_requests set bridge_id = %s, status = %s, last_response = %s where id = %s",
            (request_id, status, Jsonb(answer), row),
        )
    return CatalogRequestReply(
        success=True, count=1, request_id=request_id, status=status,
        message=f"Requested {body['name']} ({body['size']}): product request {request_id} is "
                f"{status}. If approved it appears in the catalog, maybe under another name.",
    )


def _unknown(connect: Callable[[], psycopg.Connection], row: int, why: str, answer: object) -> CatalogRequestReply:
    with connect() as side:
        side.execute("update product_requests set status = 'unknown', last_response = %s where id = %s",
                     (None if answer is None else Jsonb(answer), row))
    return CatalogRequestReply(
        success=False, status="unknown",
        message=f"Sent, but no usable answer came back ({why}), so the request may or may not exist. "
                "Don't send it again; a person must check the bridge first.",
    )


def _columns(body: dict) -> dict:
    return {k: body.get(k) for k in ("name", "size", "line", "flavor", "note")}
