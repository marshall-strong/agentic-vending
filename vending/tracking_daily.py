"""The daily tracking check, sent by the worker (#218), and the latest verdicts for the dashboard and briefing.

    python -m vending.tracking_daily     # what the worker runs each round: send today's check if it is due

The worker runs this as the last job of every round (`vending.worker`), after the syncs, so the newest
polls and receipts are in. It sends at most one check a day through `vending.tracking.send`, the same
code as the manual command, so the request and the answer go in `tracking_checks` as before.

**Off until the team turns it on.** `TRACKING_DAILY` unset or `off`: the job does nothing and says so.
`on`: it sends. A check goes to the course's bridge and spends the day's one check, and the team may
want to send the first ones by hand from 30 Nov. So it is off by default, like the agent schedule.

**When.** From `TRACKING_DAILY_AT` (default 06:00 Eastern) until 21:00 Eastern, in the first round that
finds the books settled. Early morning is when they are: no visit happens overnight, the overnight
rounds have synced yesterday's receipts, and David's Saturday 23:59 tax calculation is in by Sunday
morning. The check is as of the newest good poll of ours taken before the current hour began: the
bridge folds polls into its books on the hour, so that poll is already folded in. The send's facts
are decided at the send itself, under its reservation lock, with the clock of that moment, so
a job that runs long cannot send on the facts of when it started: not after the 21:00 dispatch
deadline, not under yesterday's date, and not while another replica holds the reservation lock or
another's transient refusal is still backing off. The poll is picked and the figures are computed
under that lock too, in the reservation's own transaction: the books of that moment, so a ledger
correction or a newer poll committed meanwhile is in them, and a job that starts the evening
before cannot send the previous evening's figures under the new day's date. The pick, the tie check,
every figure and the reservation share one repeatable-read snapshot. The window and the Eastern day
are checked once more just before the request: past them, the reservation is marked `not_sent`.

**One request a night.** The request is never started later than 21:00 Eastern: by then it is given
up by the client within seconds, hours before midnight, so the bridge cannot check it on the next
day (`tracking.DISPATCH_DEADLINE`). A request from the previous day that is still unanswered may
still reach the bridge after midnight, so while it has no answer it blocks the new day's check
until the morning window opens at 06:00 (`tracking.MORNING_FROM`): one request reaches the bridge
for that night, not two.

**Never twice a day.** Before it sends, the job asks the database whether today's check is used or may
be (`tracking.blocking`): any send that day, by the worker or a person, that the bridge answered, or
that got no usable answer. `send(..., once_a_day=True)` checks the day again under the send's lock.
All of it is in `tracking_checks`, so a worker restart changes nothing. A send the bridge refused
before checking (for example `409 not_reconciled_yet`) leaves the day open, so the next round tries
again. A refusal that settles on its own (`not_reconciled_yet`, `tracking_not_started`) backs off:
an hour after the first, doubling to four, at most `MAX_TRANSIENT` a day. Any other refusal or a
send with no answer stops after `MAX_TRIES` tries. The tries, the transient cap and the backoff are
all counted under the send's lock, so two replicas cannot together exceed them or slip past the
wait. However it retries, one successful send a day is the most there ever is.

**Skips.** No ledger opening yet, or no poll of ours since it: the job logs a line and exits 0. The
manual command refuses the same way (`tracking.CantCompute`). A slot no counted receipt has loaded, when
that is the only reason the figures can't be worked out (`tracking.NotLoaded`, #254): the round is a skip.
It sends nothing and guesses nothing, records the skip in `tracking_skips` (one row a day, with its slots
and rounds), logs a line and exits 0. The dashboard shows a Check line until a check is sent after it
(`attention`). After the opening, any other figures that can't be worked out, a refusal or a send with no
usable answer fail the job, so /health shows it.

Its log lines are our own words: the verdicts, `tracking.said` for a refusal, never the bridge's text.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from datetime import datetime, time, timedelta
from html import escape
from typing import Any

import psycopg

from vending import db, ledger, settings, tracking
from vending.slots import DEVICE_ID, SlotsNotRecorded
from vending.start import EASTERN

MODE_SETTING = "TRACKING_DAILY"
AT_SETTING = "TRACKING_DAILY_AT"
DEFAULT_AT = time(6, 0)
# No send from this time on, Eastern: the dispatch deadline (`tracking.DISPATCH_DEADLINE`). A request
# started by 21:00 is given up by the client within seconds, over three hours before midnight, so the
# bridge cannot check it on the next Eastern day; the send re-checks it under its lock.
LAST_SEND = tracking.DISPATCH_DEADLINE
MAX_TRIES = 3                    # tries a day that don't settle on their own
MAX_TRANSIENT = 8                # transient refusals a day, hard cap (the backoff bounds it lower)
BACKOFF = timedelta(hours=1)     # after the first transient refusal, before the next try
BACKOFF_CAP = timedelta(hours=4) # the backoff doubles from `BACKOFF` up to this
WORKER = "worker (daily)"        # `sent_by` on the worker's sends
STALE_POLL = timedelta(hours=6)  # an older newest poll means the history sync has stopped
DAILY_TRIES = tracking.DailyTries(WORKER, MAX_TRIES, MAX_TRANSIENT, BACKOFF, BACKOFF_CAP)
CHECKS_SHOWN = 7
NOT_LOADED = "not_loaded"           # `tracking_skips.reason`: a slot no counted receipt has loaded (#254)
SKIP_SHOWN = timedelta(hours=26)    # a skip older than this is not shown: the job is off, or the day moved on

# Where the agent looks next for each figure.
LOOKUPS = {"cash": "get_account_balance", "sales_tax_owed": "get_account_balance",
           "coil": "get_machine_state", "back_stock": "get_machine_state"}
NAMES = {"cash": "cash", "sales_tax_owed": "sales tax owed", "coil": "coil (units in our slots)",
         "back_stock": "back stock (the closet)"}


def enabled() -> bool:
    """Whether the worker sends: `TRACKING_DAILY` is `on`. Unset or `off` is off; anything else is wrong."""
    value = (settings.get(MODE_SETTING) or "off").strip().lower()
    if value not in ("on", "off"):
        raise settings.SettingsError(f"{MODE_SETTING} is {value!r}; use on or off")
    return value == "on"


def send_from() -> time:
    """The first Eastern time of day the worker sends: `TRACKING_DAILY_AT` (HH:MM), default 06:00."""
    value = (settings.get(AT_SETTING) or "").strip()
    if not value:
        return DEFAULT_AT
    try:
        at = time.fromisoformat(value)
    except ValueError:
        at = None
    if at is None or at.tzinfo is not None or at >= LAST_SEND:
        raise settings.SettingsError(f"{AT_SETTING} is {value!r}; give an Eastern time like 06:00, before "
                                     f"{LAST_SEND:%H:%M}")
    return at


def settled_poll(conn: psycopg.Connection, now: datetime, opened_at: datetime) -> tuple[str, datetime] | None:
    """(session id, started_at) of the newest good poll of ours taken before the current hour began and at
    or after the ledger's opening, or None."""
    hour = now.replace(minute=0, second=0, microsecond=0)
    return conn.execute("select session_id, started_at from polls where device_id = %s and crc_ok"
                        " and started_at < %s and started_at >= %s order by started_at desc, session_id desc"
                        " limit 1", (DEVICE_ID, hour, opened_at)).fetchone()


def run(conn: psycopg.Connection, bridge: Callable[[], Any] | None = None,
        now: datetime | Callable[[], datetime] | None = None,
        log: Callable[[str], None] = print) -> int:
    """Send today's check if it is due and not done. 0: sent, or nothing to do; 1: not sent and it should
    have been; 2: a setting is wrong; 3: sent, but it may have used the day's check with no verdicts.

    `now` pins the clock for the job's own checks (default the database clock). The send itself is
    stamped and re-checked with the clock of each moment (`vending.tracking.send`): under its
    reservation lock it decides the Eastern day, the window, whether the send is past the 21:00
    dispatch deadline, the day's tries and their backoff, and whether the day's check is used or
    may be, and the answer carries its own time. The poll is picked and the figures are computed
    under that lock too, in the reservation's own transaction, so the check is as of the books of
    the send's moment - a ledger correction or a newer poll committed while the job was on its way
    is in them, and figures prepared the evening before are not sent the next morning. What the
    job checks before the lock is only that there is anything to do today. A callable `now` is
    asked once for the job's checks and again for each stamp of the send (a test's way to move the
    clock mid-send)."""
    try:
        if not enabled():
            log(f"tracking_daily: off. Set {MODE_SETTING}=on to send one check a day.")
            return 0
        start = send_from()
    except settings.SettingsError as error:
        log(f"tracking_daily: {error}")
        return 2
    if now is None:
        now = lambda: tracking.clock(conn)       # each stamp is the clock of its moment
    if callable(now):
        stamp, now = now, now()
    else:
        stamp = lambda: now
    local = now.astimezone(EASTERN)
    if not start <= local.time() < LAST_SEND:
        log(f"tracking_daily: not due; it sends from {start:%H:%M} to {LAST_SEND:%H:%M} Eastern.")
        return 0
    day = local.date()
    done = tracking.blocking(conn, day, now)
    if done:
        log(f"tracking_daily: today's check ({day}) is used or may be (#{done[0][0]}). Nothing sent.")
        return 0
    tries, transient, last_transient = tries_today(conn, day)
    if tries >= MAX_TRIES:
        log(f"tracking_daily: {tries} tries today that don't settle on their own, so no more until tomorrow. "
            "See: python -m vending.tracking show")
        return 1
    wait = tracking.backoff_wait(transient, BACKOFF, BACKOFF_CAP) if transient else None
    if transient >= MAX_TRANSIENT or (transient and now < last_transient + wait):
        log(f"tracking_daily: the books aren't ready yet ({transient} transient refusals today); backing off "
            f"until {(last_transient + wait).astimezone(EASTERN):%H:%M} Eastern.")
        return 0
    opened = ledger.opening(conn)
    if opened is None or opened.at > now:
        log("tracking_daily: skipped, the ledger has no opening yet. " + ledger.OPEN_HINT)
        return 0
    found = settled_poll(conn, now, opened.at)
    if found is None:
        log("tracking_daily: skipped, no poll of ours since the ledger's opening is in the bridge's books yet.")
        return 0
    picked: dict = {}

    def prepare(c: psycopg.Connection, moment: datetime) -> tracking.Figures:
        """The figures of the send's moment, inside the reservation's transaction: the newest settled
        poll of that moment, and the books as of it, so whatever committed while this job was on its
        way (a ledger correction, a newer poll, the day itself turning over) is what the check
        reports. The pre-lock `found` above was only the check that there is anything to do today.
        It runs in the send's one repeatable-read transaction, so `tracking.figures` reads there too:
        the pick, the tie check and every figure see the same snapshot."""
        opening = ledger.opening(c)
        if opening is None:                     # cannot happen: the opening is append-only
            raise tracking.CantCompute(f"the ledger's opening is gone. {ledger.OPEN_HINT}")
        found = settled_poll(c, moment, opening.at)
        if found is None:                       # cannot happen either, and the answer is the same
            raise tracking.CantCompute("no poll of ours since the ledger's opening is in the bridge's books yet")
        picked["session"] = found[0]
        return tracking.figures(c, found[0])

    try:
        outcome = tracking.send(conn, (bridge or _bridge)(), prepare, WORKER, now=stamp, once_a_day=True,
                                window=(start, LAST_SEND), daily_tries=DAILY_TRIES, stale=STALE_POLL)
    except tracking.NotLoaded as error:
        # No guess about an unloaded slot (#254): a skip with its reason, not a failed job, until it is loaded.
        try:
            record_skip(conn, stamp(), error.slots)
        except psycopg.Error as failure:
            log(f"tracking_daily: not sent. {error} Recording the skip failed ({type(failure).__name__}).")
            return 1
        log(f"tracking_daily: skipped, nothing sent until the slot is loaded. {error}")
        return 0
    except (tracking.CantCompute, SlotsNotRecorded) as error:
        log(f"tracking_daily: not sent. {error}")
        return 1
    except tracking.AlreadyChecked as error:
        log(f"tracking_daily: {error}")
        return 0
    except (tracking.WindowClosed, tracking.DispatchClosed) as error:
        log(f"tracking_daily: {error}")
        return 1
    except tracking.StaleFigures as error:
        log(f"tracking_daily: {error}")
        return 1
    except tracking.LockBusy as error:
        # The lock is taken without waiting, so a replica stuck holding it cannot strand this one:
        # skip the round, and the next round tries again.
        log(f"tracking_daily: {error} The next round tries again.")
        return 0
    except tracking.BackingOff as error:
        log(f"tracking_daily: {error}.")
        return 0
    except tracking.TooManyTries as error:
        log(f"tracking_daily: {error}. See: python -m vending.tracking show")
        return 1
    except settings.SettingsError as error:
        log(f"tracking_daily: {error}")
        return 2
    except psycopg.Error as error:
        # Its message can quote what the bridge sent, so only its class.
        log(f"tracking_daily: recording the check failed ({type(error).__name__}); it may have been sent "
            "and may have used today's check. See: python -m vending.tracking show")
        return 3
    if outcome.results is not None:
        session = picked.get("session", "?")
        outside = [k for k in tracking.RESULTS if outcome.results[k] == "outside"]
        log(f"tracking_daily: check #{outcome.id} as of poll {session}: {_verdicts(outcome.results)}."
            + (f" OUTSIDE on {', '.join(outside)}: it is in the incident log." if outside else ""))
        return 0
    words = tracking.said(outcome.status, outcome.error_type)
    if outcome.used_check is False:
        log(f"tracking_daily: check #{outcome.id} refused before checking ({outcome.status or 'no answer'}: "
            f"{words}). Today's check is still there; the next round tries again.")
        # Not folded in yet, or the books haven't begun: both settle on their own.
        return 0 if outcome.error_type in ("not_reconciled_yet", "tracking_not_started") else 1
    log(f"tracking_daily: check #{outcome.id}: {outcome.status or 'no answer'}: {words}. "
        "It may have used today's check, so none is sent again today.")
    return 3 if outcome.status is None or outcome.error_type == "malformed_answer" else 1


def record_skip(conn: psycopg.Connection, at: datetime, slots: tuple[str, ...],
                reason: str = NOT_LOADED) -> None:
    """Count one skipped round in `tracking_skips`: one row per Eastern day and reason, with the latest slots."""
    conn.execute(
        "insert into tracking_skips (eastern_day, reason, slots, first_at, last_at) values (%s, %s, %s, %s, %s)"
        " on conflict (eastern_day, reason) do update set slots = excluded.slots,"
        " last_at = greatest(tracking_skips.last_at, excluded.last_at), rounds = tracking_skips.rounds + 1",
        (at.astimezone(EASTERN).date(), reason, list(slots), at, at))


def latest_skip(conn: psycopg.Connection, now: datetime) -> dict | None:
    """The newest skip up to `now`, from the last `SKIP_SHOWN`, with no check sent at or after its latest round;
    else None. A check sent after it means the slots were loaded, so the skip is over."""
    row = conn.execute(
        "select s.reason, s.slots, s.first_at, s.last_at, s.rounds from tracking_skips s"
        " where s.last_at <= %s and s.last_at > %s"
        " and not exists (select 1 from tracking_checks c where c.sent_at >= s.last_at and c.sent_at <= %s)"
        " order by s.last_at desc, s.id desc limit 1", (now, now - SKIP_SHOWN, now)).fetchone()
    if row is None:
        return None
    reason, slots, first_at, last_at, rounds = row
    # Since the first skip after the last check sent before this one: the skip may run over several days.
    since = conn.execute(
        "select min(first_at) from tracking_skips where reason = %s and last_at <= %s and first_at >"
        " coalesce((select max(sent_at) from tracking_checks where sent_at < %s), '-infinity')",
        (reason, last_at, first_at)).fetchone()[0]
    return {"reason": reason, "slots": list(slots), "since": since or first_at, "last_at": last_at,
            "rounds": rounds}


def _skip_text(skip: dict) -> str:
    slots = skip["slots"]
    named = f"slot{'s' if len(slots) > 1 else ''} {', '.join(slots)}"
    return (f"No tracking check sent since {_when(skip['since'])}: no counted receipt has loaded {named}, so "
            f"{'their' if len(slots) > 1 else 'its'} count isn't known and nothing is guessed. Each round is "
            f"skipped until {'they are' if len(slots) > 1 else 'it is'} loaded (last {_when(skip['last_at'])}).")


def _bridge():
    from vending.bridge import Bridge

    return Bridge()


def tries_today(conn: psycopg.Connection, day) -> tuple[int, int, datetime | None]:
    """(tries, transient, last_transient_at) of the worker's attempts on the Eastern day `day`.
    `tries` counts the sends and the refusals that don't settle on their own, toward `MAX_TRIES`;
    `transient` counts the refusals that settle on their own (`tracking.TRANSIENT_REFUSALS`), toward
    `MAX_TRANSIENT`, and `last_transient_at` is when the newest one went out (the backoff waits after it)."""
    rows = conn.execute("select sent_at, used_check, error_type from tracking_checks"
                        " where eastern_day = %s and sent_by = %s and error_type is distinct from %s"
                        " order by sent_at, id", (day, WORKER, tracking.NOT_SENT)).fetchall()
    transient = [(sent, used, error) for sent, used, error in rows
                 if used is False and error in tracking.TRANSIENT_REFUSALS]
    return len(rows) - len(transient), len(transient), transient[-1][0] if transient else None


def _verdicts(results: dict) -> str:
    return ", ".join(f"{k} {results[k]}" for k in tracking.RESULTS)


# --- The latest verdicts: dashboard, briefing, incident log ---------------------------------------

def _check_row(row) -> dict:
    """A `tracking_checks` row as the panel and the briefing read it: our own words and the four
    verdicts, never the bridge's text."""
    id_, sent, by, as_of_time, status, error_type, used, results, checked = row
    verdicts = ({k: results[k] for k in tracking.RESULTS if results.get(k) in tracking.VERDICTS}
                if isinstance(results, dict) else None)
    if verdicts is not None and len(verdicts) != len(tracking.RESULTS):
        verdicts = None
    return {"id": id_, "sent_at": sent, "by": by, "as_of_time": as_of_time,
            "checked_at": tracking._time(checked) if verdicts else None,
            "verdicts": verdicts,
            "outside": [k for k in tracking.RESULTS if verdicts and verdicts[k] == "outside"],
            "said": None if verdicts else tracking.said(status, error_type), "used_check": used}


def latest(conn: psycopg.Connection, now: datetime, limit: int = CHECKS_SHOWN) -> list[dict]:
    """The newest sends up to `now`, newest first: when, by whom, as of which poll, and the verdicts when
    the bridge answered with them. Only our own words and the four verdicts: never the bridge's text."""
    rows = conn.execute(
        "select id, sent_at, sent_by, as_of_time, status, error_type, used_check, results, answer->>'checked_at'"
        " from tracking_checks where sent_at <= %s order by sent_at desc, id desc limit %s", (now, limit)).fetchall()
    return [_check_row(row) for row in rows]


def latest_answered(conn: psycopg.Connection, now: datetime) -> dict | None:
    """The newest send up to `now` that the bridge answered with verdicts, or None: the same shape as a
    `latest` row. Queried for itself, so refusals after it cannot hide an `outside` verdict from the
    briefing and Needs attention (`newest_answered` looks only in the newest `CHECKS_SHOWN` sends)."""
    row = conn.execute(
        "select id, sent_at, sent_by, as_of_time, status, error_type, used_check, results, answer->>'checked_at'"
        " from tracking_checks where results is not null and sent_at <= %s"
        " order by sent_at desc, id desc limit 1", (now,)).fetchone()
    return None if row is None else _check_row(row)


def panel(conn: psycopg.Connection, now: datetime) -> dict:
    """The dashboard's tracking check panel: the latest sends, whether the daily send is on, and when."""
    try:
        on, at, wrong = enabled(), send_from(), None
    except settings.SettingsError:
        on, at, wrong = False, None, True
    return {"on": on, "at": None if at is None else at.strftime("%H:%M"), "wrong_setting": bool(wrong),
            "checks": latest(conn, now), "answered": latest_answered(conn, now), "skip": latest_skip(conn, now)}


def newest_answered(checks: list[dict]) -> dict | None:
    return next((c for c in checks if c["verdicts"]), None)


def attention(data: dict) -> list[tuple[str, str]]:
    """(level, text) for the dashboard's Needs attention: the latest answered check read outside.
    The panel carries it as `answered` (`latest_answered`), not the newest answered one among the
    last `CHECKS_SHOWN` sends, so refusals after an `outside` check cannot hide it."""
    out = []
    if skip := data.get("skip"):
        out.append(("warn", _skip_text(skip)))
    found = data.get("answered") or newest_answered(data["checks"])
    if found is None or not found["outside"]:
        return out
    names = ", ".join(NAMES[k] for k in found["outside"])
    return [("bad", f"The bridge's tracking check #{found['id']}, as of the poll at {_when(found['as_of_time'])}, "
                    f"says our books are outside its tolerance on: {names}. See Tracking check below."), *out]


def _when(at: datetime | None) -> str:
    if at is None:
        return "unknown"
    local = at.astimezone(EASTERN)
    return f"{local:%a} {local.day} {local:%b} {local:%H:%M} Eastern"


_TONE = {"within": "ok", "outside": "bad"}


_TABLE_HEAD = ('<div class="tracking-wrap"><table class="tracking"><thead><tr><th scope="col">Sent</th>'
              '<th scope="col">As of the poll at</th><th scope="col">Cash</th><th scope="col">Sales tax owed</th>'
              '<th scope="col">Coil</th><th scope="col">Back stock</th></tr></thead>')


def _check_cells(c: dict) -> str:
    if c["verdicts"]:
        return "".join(f'<td><span class="pill {_TONE[c["verdicts"][k]]}">{escape(c["verdicts"][k])}</span></td>'
                       for k in tracking.RESULTS)
    note = c["said"] + ("" if c["used_check"] is False else "; it may have used that day's check")
    return f'<td colspan="4" class="sub">No verdicts: {escape(note)}.</td>'


def _check_row_html(c: dict, note: str = "") -> str:
    marker = f'<small>{escape(note)}</small>' if note else ""
    return (f'<tr><td><time>{escape(_when(c["sent_at"]))}</time><small>by {escape(c["by"])}{marker}</small></td>'
            f'<td><time>{escape(_when(c["as_of_time"]))}</time></td>{_check_cells(c)}</tr>')


def panel_html(panel_: dict) -> str:
    if not panel_["ok"]:
        return f'<p class="empty">Not available: {escape(panel_["why"])}</p>'
    data = panel_["data"]
    if data["wrong_setting"]:
        mode = (f"The daily send is off: <code>{MODE_SETTING}</code> or <code>{AT_SETTING}</code> has a wrong "
                "value (see the worker's log).")
    elif data["on"]:
        mode = (f"The worker sends one check a day, from {escape(data['at'])} Eastern, as of the newest poll "
                "already in the bridge's books.")
    else:
        mode = (f"The daily send is off. A person sends with <code>python -m vending.tracking check --send --by "
                f"NAME</code>, or the team sets <code>{MODE_SETTING}=on</code>.")
    lede = (f'<p class="sub lede">The bridge compares our books with its own, as of one poll: within $1 for money, '
            f'2 units for counts. It never says by how much or which slot. {mode}</p>')
    if skip := data.get("skip"):
        lede += f'<p class="sub">{escape(_skip_text(skip))}</p>'
    checks = data["checks"]
    if not checks:
        return lede + '<p class="empty">No checks sent yet.</p>'
    rows = [_check_row_html(c) for c in checks]
    answered = data.get("answered")
    last = ""
    if answered is not None and answered["id"] not in {c["id"] for c in checks}:
        # Refusals filled the recent list, so the last answered check fell off it: rendered above
        # the list, with its four verdicts, so they stay visible.
        last = (f'<p class="sub">Last answered check, older than the recent sends below:</p>'
                f'{_TABLE_HEAD}<tbody>{_check_row_html(answered)}</tbody></table></div>')
    return (lede + last + _TABLE_HEAD
            + f'<tbody>{"".join(rows)}</tbody></table></div>')


STYLE = """
.tracking-wrap { overflow-x:auto }
.tracking { width:100%; border-collapse:collapse; font-size:13px }
.tracking th, .tracking td { padding:5px 6px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top }
.tracking thead { color:var(--muted); font-size:11px }
.tracking td:first-child { white-space:nowrap }
.tracking td small { display:block; color:var(--muted) }
"""


def main(argv: list[str] | None = None, bridge: Callable[[], Any] | None = None) -> int:
    if argv:
        print("usage: python -m vending.tracking_daily (no arguments)", file=sys.stderr)
        return 2
    try:
        if not enabled():       # off: no database connection at all
            print(f"tracking_daily: off. Set {MODE_SETTING}=on to send one check a day.")
            return 0
    except settings.SettingsError as error:
        print(f"tracking_daily: {error}")
        return 2
    with db.connect() as conn:
        behind = db.pending(conn)
        if behind:
            print(f"tracking_daily: the database is missing migrations: {', '.join(behind)}. A person runs: "
                  "python -m vending.db migrate", file=sys.stderr)
            return 2
        return run(conn, bridge)


if __name__ == "__main__":
    sys.exit(main())
