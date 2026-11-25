"""A scheduled run waits for model credit (#262). Before the worker claims a time it asks the API, with one free
`messages.count_tokens` call through `vending.harness.check_credit`, whether the run's own key can pay. A key with
no credit is a check that *waits*: nothing is claimed, the worker checks again every `LIVE_RETRY` until the
time's latest start, and `/health` and the dashboard say it is waiting on model credit. Still none then, the time
is `missed`, with a detail that says so. A key the API refuses (401) fails the time at once. No paid call: the
fake clients below answer `count_tokens` only, and any `create` fails the test."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from tests.test_agent_schedule import (  # noqa: F401 - the autouse fixture clears the schedule settings
    DRY, LIVE, Clock, _no_schedule_settings, at)
from vending import agent_schedule, harness, settings

anthropic = pytest.importorskip("anthropic")
import httpx2  # noqa: E402  (anthropic 2.x is built on it)

ET = ZoneInfo("America/New_York")
THU = datetime(2026, 12, 3, tzinfo=ET)          # the first live order: Thu 3 Dec 12:00 ET
CREDIT_MESSAGE = "Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing."


def _status(cls, status, body=None):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages/count_tokens")
    return cls(f"Error code: {status}", response=httpx2.Response(status, request=request), body=body)


def no_credit():
    """The 400 the team key has answered since 25 Nov."""
    return _status(anthropic.BadRequestError, 400,
                   {"type": "error", "error": {"type": "invalid_request_error", "message": CREDIT_MESSAGE}})


def bad_key():
    return _status(anthropic.AuthenticationError, 401,
                   {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}})


def unreachable():
    return anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))


class CreditClient:
    """Answers `count_tokens` with each of `errors` in turn (None: counted), then counts. Never sends a message."""

    def __init__(self, *errors):
        self.errors, self.counted, self.options = list(errors), [], []
        self.messages = self

    def with_options(self, **options):
        self.options.append(options)
        return self

    def count_tokens(self, **body):
        self.counted.append(body)
        error = self.errors.pop(0) if self.errors else None
        if error is not None:
            raise error
        return SimpleNamespace(input_tokens=12)

    def create(self, **_):
        raise AssertionError("the credit check must never send a paid request")


# --- The check itself (no database) -------------------------------------------------------------------------

def test_the_credit_check_is_one_free_count_with_the_runs_model_and_no_retries():
    client = CreditClient()
    harness.check_credit(client, "claude-opus-5-5")
    [body] = client.counted
    assert body["model"] == "claude-opus-5-5" and set(body) == {"model", "messages"}
    assert client.options[-1]["max_retries"] == 0 and client.options[-1]["timeout"] > 0


@pytest.mark.parametrize("error, raised", [
    (no_credit, harness.NoCredit),
    (bad_key, harness.KeyRefused),
    (unreachable, harness.Unreachable),
    (lambda: _status(anthropic.InternalServerError, 500), harness.Unreachable),
    (lambda: _status(anthropic.RateLimitError, 429), harness.Unreachable),
])
def test_each_answer_is_its_own_error_and_never_quotes_the_api(error, raised):
    with pytest.raises(raised) as caught:
        harness.check_credit(CreditClient(error()), "claude-opus-5-5")
    assert "invalid x-api-key" not in str(caught.value) and "Plans & Billing" not in str(caught.value)


def test_another_refusal_is_left_to_the_run(monkeypatch):
    """A 404 (say, a model name the API doesn't know) isn't about credit: the run's own request records it."""
    with pytest.raises(harness.HarnessError) as caught:
        harness.check_credit(CreditClient(_status(anthropic.NotFoundError, 404)), "claude-opus-5-5")
    assert type(caught.value) is harness.HarnessError
    monkeypatch.setenv(harness.DEFAULT_KEY_ENV, "sk-test")
    assert agent_schedule.credit_state(lambda: CreditClient(_status(anthropic.NotFoundError, 404))) is None


def test_the_scheduler_reads_the_check_with_the_runs_own_key_and_model(monkeypatch):
    from vending import agent

    monkeypatch.setenv(harness.DEFAULT_KEY_ENV, "sk-test")
    client = CreditClient(no_credit(), bad_key(), unreachable(), None)
    monkeypatch.setattr(agent, "model_client", lambda: client)
    state, why = agent_schedule.credit_state()
    assert state == "wait" and why.startswith("no model credit")
    state, why = agent_schedule.credit_state()
    assert state == "refused" and why.startswith(agent_schedule.MODEL_KEY_REFUSED_TAG)
    state, why = agent_schedule.credit_state()
    assert state == "wait" and "couldn't be reached" in why
    assert agent_schedule.credit_state() is None
    assert {b["model"] for b in client.counted} == {agent.DEFAULT_MODEL}


def test_without_the_model_key_the_check_leaves_it_to_the_entry_point(monkeypatch):
    """No key: no check, so the entry point records MODEL KEY MISSING as before."""
    monkeypatch.delenv(harness.DEFAULT_KEY_ENV, raising=False)
    monkeypatch.setattr(settings, "_FILE", {k: v for k, v in settings._FILE.items() if k != harness.DEFAULT_KEY_ENV})

    def never():
        raise AssertionError("no client without the key")
    assert agent_schedule.credit_state(never) is None


def test_no_count_tokens_call_outside_the_harness():
    """The source guard covers the free count too: only `vending.harness` talks to the API."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for path in (p for d in ("vending", "scripts", "analysis") for p in (root / d).rglob("*.py")):
        if path == root / "vending/harness.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Attribute) and node.attr == "count_tokens":
                assert "messages" not in ast.unparse(node).split(".")[:-1], path


# --- With the database ----------------------------------------------------------------------------------------

psycopg = pytest.importorskip("psycopg")

from tests.test_agent_schedule import FakeAgent, attention, db_mode, rows, scheduler, tick  # noqa: E402
from tests.test_live_schedule import fresh_slots  # noqa: E402

LIVE3 = agent_schedule.Schedule("live", (agent_schedule.Entry(3, datetime.min.time().replace(hour=12),
                                                              datetime.min.time().replace(hour=20)),), THU.date())


@pytest.fixture
def live_ready(conn):
    db_mode(conn, "live")
    fresh_slots(conn, at(THU, 12))


def checker(*errors):
    """The scheduler's credit check through the real `check_credit`, on a fake client; `.client` counts the calls."""
    client = CreditClient(*errors)

    def check():
        return agent_schedule.credit_state(lambda: client)
    check.client = client
    return check


@pytest.fixture(autouse=True)
def _a_key(monkeypatch):
    monkeypatch.setenv(harness.DEFAULT_KEY_ENV, "sk-test")


def test_no_credit_at_noon_waits_and_the_run_starts_late_once_credit_comes(schema_url, conn, live_ready):
    """Thu 3 Dec: no credit at 12:00, 12:05 and 12:10; credit by 12:15. Nothing is claimed while it waits, /health
    and the dashboard say it waits on model credit since 12:00, and the run goes live 15 minutes late."""
    check = checker(no_credit(), no_credit(), no_credit())
    agent = FakeAgent(schema_url)
    s = scheduler(schema_url, agent, LIVE3, clock=Clock(at(THU, 11)), credit_check=check)
    tick(s, at(THU, 12))
    assert agent.calls == [] and rows(conn) == []
    waiting = s.public()["waiting"]
    assert "waiting on model credit since 12:00 PM ET" in waiting and "live run" in waiting
    assert "no model credit" in waiting
    assert ("warn", waiting) in attention(conn, at(THU, 12), syncs={"agent_schedule": s.public()})
    assert s.next_due(at(THU, 12)) == at(THU, 12) + agent_schedule.LIVE_RETRY
    for minute in (5, 10):
        tick(s, at(THU, 12, minute))
        assert agent.calls == [] and rows(conn) == []
        assert "since 12:00 PM ET" in s.public()["waiting"]      # since the first refusal, not the latest
    tick(s, at(THU, 12, 15))
    assert [live for _, live in agent.calls] == [True] and s.public()["waiting"] is None
    [(_, mode, outcome, _, detail)] = rows(conn)
    assert (mode, outcome) == ("live", "skipped")
    assert detail.startswith("Started 15 min late: it waited on model credit from 12:00 PM ET.")
    assert len(check.client.counted) == 4


def test_a_short_wait_still_says_how_late(schema_url, conn, live_ready):
    agent = FakeAgent(schema_url)
    s = scheduler(schema_url, agent, LIVE3, clock=Clock(at(THU, 11)), credit_check=checker(no_credit()))
    tick(s, at(THU, 12))
    tick(s, at(THU, 12, 5))
    assert rows(conn)[0][4].startswith("Started 5 min late: it waited on model credit from 12:00 PM ET.")


def test_no_credit_until_the_latest_start_is_a_missed_live_run_that_names_it(schema_url, conn, live_ready):
    check = checker(*[no_credit()] * 200)
    agent = FakeAgent(schema_url)
    s = scheduler(schema_url, agent, LIVE3, clock=Clock(at(THU, 11)), credit_check=check)
    tick(s, at(THU, 12))
    tick(s, at(THU, 19, 55))
    tick(s, at(THU, 20))
    assert agent.calls == []
    [(_, mode, outcome, _, detail)] = rows(conn)
    assert (mode, outcome) == ("live", "missed")
    assert detail.startswith("LIVE RUN MISSED") and "no model credit" in detail
    lines = [line for line in attention(conn, at(THU, 20, 5)) if line[1].startswith("The scheduled ")]
    assert lines and lines[0][0] == "bad" and "no model credit" in lines[0][1]
    assert s.public()["waiting"] is None


def test_a_dry_run_waits_for_credit_too_and_its_miss_says_so(schema_url, conn):
    db_mode(conn, "dry-run")
    agent = FakeAgent(schema_url)
    wed = datetime(2026, 12, 2, tzinfo=ET)
    s = scheduler(schema_url, agent, DRY, clock=Clock(at(wed, 13)), credit_check=checker(*[no_credit()] * 200))
    tick(s, at(wed, 14))
    assert agent.calls == [] and rows(conn) == []
    waiting = s.public()["waiting"]
    assert "dry run" in waiting and "waiting on model credit since 2:00 PM ET" in waiting
    tick(s, at(wed, 22))
    [(_, mode, outcome, _, detail)] = rows(conn)
    assert (mode, outcome) == ("dry-run", "missed")
    assert detail.startswith("DRY RUN MISSED") and "no model credit" in detail
    lines = [line for line in attention(conn, at(wed, 22, 5)) if line[1].startswith("The scheduled ")]
    assert lines and "no model credit" in lines[0][1]


def test_a_live_time_before_the_start_still_waits_for_credit_as_a_dry_run(schema_url, conn, live_ready):
    """Wed 2 Dec is before the start: claimed live, it would run as a dry run, so its wait says dry run."""
    agent = FakeAgent(schema_url)
    wed = datetime(2026, 12, 2, tzinfo=ET)
    s = scheduler(schema_url, agent, LIVE, clock=Clock(at(wed, 13)), credit_check=checker(no_credit()))
    tick(s, at(wed, 14))
    assert "dry run" in s.public()["waiting"]
    tick(s, at(wed, 14, 5))
    [(_, mode, _, _, detail)] = rows(conn)
    assert mode == "dry-run" and "waited on model credit" in detail and "not live" in detail


def test_a_refused_key_fails_the_time_at_once_and_never_starts_the_agent(schema_url, conn, live_ready):
    check = checker(bad_key())
    agent = FakeAgent(schema_url)
    s = scheduler(schema_url, agent, LIVE3, clock=Clock(at(THU, 11)), credit_check=check)
    tick(s, at(THU, 12))
    assert agent.calls == []
    [(_, mode, outcome, run_id, detail)] = rows(conn)
    assert (mode, outcome, run_id) == ("live", "failed", None)
    assert detail.startswith(agent_schedule.MODEL_KEY_REFUSED_TAG) and "401" in detail
    assert "invalid x-api-key" not in detail
    assert s.public()["waiting"] is None
    tick(s, at(THU, 12, 5))     # no loop on a bad key: the time has its row, so nothing is checked again
    assert len(check.client.counted) == 1 and agent.calls == []
    lines = [line for line in attention(conn, at(THU, 12, 5)) if line[1].startswith("The scheduled ")]
    assert lines[0][0] == "bad" and "MODEL KEY REFUSED" in lines[0][1]


def test_an_unreachable_api_waits_and_the_run_goes_once_it_answers(schema_url, conn, live_ready):
    agent = FakeAgent(schema_url)
    s = scheduler(schema_url, agent, LIVE3, clock=Clock(at(THU, 11)), credit_check=checker(unreachable()))
    tick(s, at(THU, 12))
    assert agent.calls == [] and "couldn't be reached" in s.public()["waiting"]
    tick(s, at(THU, 12, 5))
    assert [live for _, live in agent.calls] == [True]


def test_a_time_the_database_turns_off_is_never_checked(schema_url, conn):
    check = checker(no_credit())
    agent = FakeAgent(schema_url)
    s = scheduler(schema_url, agent, LIVE3, clock=Clock(at(THU, 11)), credit_check=check)
    tick(s, at(THU, 12))
    assert check.client.counted == [] and rows(conn)[0][2] == "skipped"


def test_with_credit_the_run_starts_on_time_after_one_check(schema_url, conn, live_ready):
    check = checker()
    agent = FakeAgent(schema_url)
    s = scheduler(schema_url, agent, LIVE3, clock=Clock(at(THU, 11)), credit_check=check)
    tick(s, at(THU, 12))
    assert [live for _, live in agent.calls] == [True] and len(check.client.counted) == 1
    assert rows(conn)[0][4] == FakeAgent(schema_url).detail


def test_a_midweek_run_waits_for_credit_the_same_way(schema_url, conn):
    """A midweek wake-up's run is claimed by the same `_claim`, so it waits for credit the same way."""
    from tests.test_midweek_run import DRY9, MON, REASON, MidweekAgent, schedule_rows, wake

    db_mode(conn, "dry-run")
    wake(conn, at(MON, 9, 5))
    agent = MidweekAgent(schema_url)
    s = scheduler(schema_url, agent, DRY9, clock=Clock(at(MON, 9)), credit_check=checker(no_credit()))
    tick(s, at(MON, 9, 10))
    assert agent.calls == [] and schedule_rows(conn) == []
    assert "waiting on model credit since 9:10 AM ET" in s.public()["waiting"]
    tick(s, at(MON, 9, 15))
    [(live, occasion)] = agent.calls
    assert live is False and REASON in occasion
    [(_, kind, _, _, _, _, detail)] = schedule_rows(conn)
    assert kind == "midweek" and "waited on model credit from 9:10 AM ET" in detail
