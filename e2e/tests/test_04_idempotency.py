"""Journey 4: idempotency, provider failures, disconnects, concurrency and rate limits.

Every case here is about what the customer is charged when something goes wrong. The account
drafting through this journey is on a paid plan, because the free plan's own rate limits
(3 a minute, 10 a day) are journey 5's subject and would otherwise decide these results.
"""
from __future__ import annotations

import threading
import time
import uuid

import pytest

from e2e.tests import billing_flow
from e2e.tests.client import Control, stream_body

CHAT = "Write me a post about the release notes I read this morning."
EMPTY_COMPOSER = {"action": "post", "draft_text": "", "intent": "enhance"}
DRAFT_COMPOSER = {
    "action": "post",
    "draft_text": "I replaced our retry logic this week and only the timeout mattered.",
    "intent": "enhance",
}


@pytest.fixture
def pro_account(ready_account, harness: Control):
    """A customer on Pro, subscribed once per session through the fake Checkout page.

    The journey uses three of them. Rate limits are per account and per minute (Pro allows ten),
    and every case here is a burst, so one account per group of cases keeps the limit out of
    the way of everything except the case that is about the limit.
    """

    def factory(name: str):
        account = ready_account(name)
        if not account.extras.get("subscribed"):
            billing_flow.subscribe(account, harness, "pro")
            account.extras["subscribed"] = True
        return account

    return factory


def terminal_state(harness: Control, account, key: str, timeout_s: float = 60.0) -> dict:
    """Wait until the reservation for `key` has finished, whatever way it finished."""
    return harness.wait_for(
        lambda: _finished(harness, account.id, key),
        timeout_s,
        f"reservation {key} to finish",
    )


def _error_count(harness: Control, account_id: str) -> int:
    """`request_errors` counts, not rows: one row per minute per route, mode and code."""
    return sum(int(row["count"]) for row in harness.request_errors(account_id))


def _finished(harness: Control, account_id: str, key: str) -> dict | None:
    row = harness.one(
        "SELECT * FROM usage_reservations WHERE account_id = ? AND (idempotency_key = ? OR idempotency_key LIKE ?) "
        "ORDER BY created_at DESC LIMIT 1",
        (account_id, key, f"{key}#%"),
    )
    return row if row is not None and row["state"] not in ("reserved", "active") else None


# --------------------------------------------------------------------------- idempotency


def test_a_replayed_key_returns_the_stored_result_and_charges_nothing_extra(pro_account, harness: Control):
    account = pro_account("requests")
    key = str(uuid.uuid4())
    body = stream_body("chat", CHAT)

    first = account.device.stream(body, key=key)
    assert first.final is not None, first.events
    before = harness.counters(account.id)

    replay = account.device.stream(body, key=key)
    assert replay.status == 200
    # CONTRACTS 6.5: a replay on the stream route is `usage` and `final` only, with no
    # `accepted` line, because nothing is accepted a second time.
    kinds = [event.get("event") for event in replay.events]
    assert kinds == ["usage", "final"], kinds
    assert replay.final["response"] == first.final["response"]
    assert not replay.of("token"), "a replay repeats the result, never the tokens"

    after = harness.counters(account.id)
    assert after["committed"] == before["committed"], "a replay must not charge again"
    assert len(harness.rows(
        "SELECT id FROM usage_reservations WHERE account_id = ? AND idempotency_key = ?", (account.id, key)
    )) == 1


def test_the_same_key_with_a_different_body_is_refused(pro_account, harness: Control):
    account = pro_account("requests")
    key = str(uuid.uuid4())
    assert account.device.stream(stream_body("chat", CHAT), key=key).final is not None
    before = harness.counters(account.id)

    mismatch = account.device.post("/assist/stream", stream_body("chat", "A different message entirely."), key=key)

    assert mismatch.status_code == 409
    assert mismatch.json()["error"]["code"] == "idempotency_mismatch"
    assert harness.counters(account.id)["committed"] == before["committed"]


def test_a_request_id_can_be_polled_while_it_runs_and_after_it_finishes(pro_account, harness: Control):
    account = pro_account("requests")
    key = str(uuid.uuid4())
    harness.ai_fail("slow", delay_s=4.0, times=1)
    result: dict = {}

    def run() -> None:
        result["stream"] = account.device.stream(stream_body("chat", CHAT), key=key)

    worker = threading.Thread(target=run, name="assist-stream", daemon=True)
    worker.start()
    try:
        pending = harness.wait_for(
            lambda: _poll(account, key, 202), 30.0, "GET /requests/{key} to answer 202 while it runs"
        )
        assert pending["state"] == "pending" and pending["request_id"] == key
    finally:
        worker.join(timeout=120)
    assert result["stream"].final is not None, result["stream"].events

    done = harness.wait_for(lambda: _poll(account, key, 200), 30.0, "GET /requests/{key} to answer 200")
    assert done["state"] == "done"
    assert done["response"] == result["stream"].final["response"]
    assert done["usage"]["used"] == harness.counters(account.id)["committed"]

    unknown = account.device.get(f"/requests/{uuid.uuid4()}")
    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "not_found"


def _poll(account, key: str, expected: int) -> dict | None:
    response = account.device.get(f"/requests/{key}")
    return response.json() if response.status_code == expected else None


# --------------------------------------------------------------------------- provider failures


@pytest.mark.parametrize("kind", ["server_error", "rate_limited", "invalid_key"])
def test_a_failing_provider_releases_the_reservation_and_charges_nothing(
    pro_account, harness: Control, kind: str
):
    """An enhance press while the provider is broken: an error event, nothing charged.

    The press is an enhance of the writer's own draft, because that path refuses to invent a
    replacement when the model is unreachable, which is the refusal the contract describes.
    """
    account = pro_account("failures")
    before = harness.counters(account.id)
    errors_before = _error_count(harness, account.id)
    key = str(uuid.uuid4())
    harness.ai_fail(kind, times=60)

    result = account.device.stream(
        stream_body("enhance_composer", composer=DRAFT_COMPOSER), key=key
    )

    assert result.status == 200, result.events
    assert result.error is not None, [event.get("event") for event in result.events]
    assert result.error["code"] in ("ai_unavailable", "service_paused"), result.error
    assert result.final is None
    assert result.usage is not None and result.usage["used"] == before["committed"]

    reservation = terminal_state(harness, account, key)
    assert reservation["state"] == "released", reservation["state"]
    assert reservation["exposure_at"] is None
    assert reservation["cost_micros"] == 0, "a failed call costs nothing"
    assert harness.counters(account.id)["committed"] == before["committed"]
    assert harness.one("SELECT id FROM credit_ledger WHERE reservation_id = ?", (reservation["id"],)) is None

    failed = harness.ai_calls(reservation_id=reservation["id"])
    assert failed and all(row["status"] != "ok" for row in failed), failed

    harness.wait_for(
        lambda: _error_count(harness, account.id) > errors_before, 20.0, "a request_errors row"
    )
    row = harness.request_errors(account.id)[-1]
    assert row["route"] == "/api/v1/assist/stream" and row["mode"] == "enhance_composer"
    assert row["reserved"] == 1 and row["count"] >= 1
    assert row["code"] in ("ai_unavailable", "service_paused")
    assert set(row) >= {"minute", "account_id", "route", "mode", "code", "http_status", "reserved", "count"}


def test_the_provider_failure_was_really_upstream(pro_account, harness: Control):
    """Negative control for the three cases above: without the flag the same press succeeds."""
    account = pro_account("requests")
    key = str(uuid.uuid4())
    result = account.device.stream(stream_body("enhance_composer", composer=DRAFT_COMPOSER), key=key)
    assert result.final is not None and result.error is None
    assert terminal_state(harness, account, key)["state"] == "committed"


def test_a_chat_press_is_not_charged_when_every_ai_call_fails(pro_account, harness: Control):
    """A provider outage never costs the customer a credit, in chat as in every other mode.

    This was found by these journeys: the engine used to answer an outage with its canned "I couldn't
    produce a version of that ..." text and `ok: true`, and the gateway charged for it. Two rules now
    prevent that. The engine reports the outage as an error (and marks a genuinely gated answer
    `withheld`), and the gateway only charges a usable result when an AI call for it succeeded.
    """
    account = pro_account("failures")
    before = harness.counters(account.id)
    key = str(uuid.uuid4())
    harness.ai_fail("server_error", times=60)

    result = account.device.stream(stream_body("chat", CHAT), key=key)

    assert result.final is None, "an outage is not a final answer"
    assert result.error is not None, [event.get("event") for event in result.events]
    assert result.error["code"] == "ai_unavailable"
    reservation = terminal_state(harness, account, key)
    assert reservation["state"] == "released"
    assert reservation["cost_micros"] == 0, "nothing was spent at the provider"
    assert all(row["status"] != "ok" for row in harness.ai_calls(reservation_id=reservation["id"]))
    assert harness.counters(account.id)["committed"] == before["committed"], "no credit was charged"


def test_a_provider_that_cuts_the_stream_mid_answer_is_billed_by_the_exposure_rule(
    pro_account, harness: Control
):
    """The provider drops the connection after a couple of chunks (`cut_stream`).

    The engine may still finish with the partial text (it flags the unfinished sentence) or give
    up. Either way the charge follows the one rule in CONTRACTS 7: the request is charged when,
    and only when, something was shown to the reader. The interrupted call itself is recorded,
    with estimated usage or an error, because the provider never sent a usage block.
    """
    account = pro_account("failures")
    before = harness.counters(account.id)
    key = str(uuid.uuid4())
    harness.ai_fail("cut_stream", after_chunks=2, times=60)

    result = account.device.stream(stream_body("enhance_composer", composer=DRAFT_COMPOSER), key=key)

    assert result.status == 200
    assert harness.ai_requests()["totals"]["by_outcome"].get("cut_stream", 0) >= 1, "the provider never cut anything"

    reservation = terminal_state(harness, account, key)
    calls = harness.ai_calls(reservation_id=reservation["id"])
    assert calls, "an interrupted call is still recorded"
    assert any(row["status"] != "ok" or row["estimated"] == 1 for row in calls), calls

    charged = harness.counters(account.id)["committed"] - before["committed"]
    if reservation["exposure_at"] is not None:
        assert reservation["state"] == "committed" and charged == 1, reservation
    else:
        assert reservation["state"] == "released" and charged == 0, reservation


# --------------------------------------------------------------------------- disconnects


def test_a_client_that_leaves_before_any_token_is_not_charged(pro_account, harness: Control):
    account = pro_account("failures")
    before = harness.counters(account.id)
    key = str(uuid.uuid4())
    harness.ai_fail("slow", delay_s=8.0, times=2)

    result = account.device.stream(stream_body("chat", CHAT), key=key, stop_after_tokens=0)
    assert [event.get("event") for event in result.events] == ["status"], result.events

    reservation = terminal_state(harness, account, key, timeout_s=90)
    assert reservation["exposure_at"] is None, "no token ever reached the client"
    assert reservation["state"] == "released"
    assert reservation["release_reason"] in ("client_disconnected", "cancelled"), reservation["release_reason"]
    assert harness.counters(account.id)["committed"] == before["committed"]


def test_a_client_that_leaves_after_tokens_is_charged(pro_account, harness: Control):
    account = pro_account("failures")
    before = harness.counters(account.id)
    key = str(uuid.uuid4())

    result = account.device.stream(stream_body("chat", CHAT), key=key, stop_after_tokens=1)
    assert result.of("token"), "the test needs at least one token before disconnecting"

    reservation = terminal_state(harness, account, key, timeout_s=90)
    assert reservation["exposure_at"] is not None, "a token was shown, so the request is exposed"
    assert harness.counters(account.id)["committed"] == before["committed"] + 1
    assert harness.one("SELECT kind FROM credit_ledger WHERE reservation_id = ?", (reservation["id"],))["kind"] == "commit"


# --------------------------------------------------------------------------- concurrency


def test_a_third_request_for_one_account_at_the_same_time_is_busy(pro_account, harness: Control):
    account = pro_account("concurrency")
    harness.ai_fail("slow", delay_s=6.0, times=3)
    results: list = []
    lock = threading.Lock()

    def run() -> None:
        outcome = account.device.stream(stream_body("chat", CHAT), key=str(uuid.uuid4()))
        with lock:
            results.append(outcome)

    workers = [threading.Thread(target=run, name=f"assist-{index}", daemon=True) for index in range(3)]
    for index, worker in enumerate(workers):
        worker.start()
        time.sleep(0.4)  # one active, one queued, one over the queue
    for worker in workers:
        worker.join(timeout=180)

    codes = [result.error["code"] if result.error else "ok" for result in results]
    # One request holds the slot; one may wait for it and still get through; anything past the
    # single queue place is refused at once.
    assert codes.count("busy") >= 1, codes
    assert "ok" in codes, codes
    busy = next(result for result in results if result.error and result.error["code"] == "busy")
    assert [event.get("event") for event in busy.events][0] == "status"
    assert busy.usage is not None, "a refused request still reports the account's usage"


# --------------------------------------------------------------------------- rate limits


def test_the_free_plans_rate_limit_answers_with_retry_after(ready_account, harness: Control, plans: dict):
    """Four metered requests in a minute on the free plan: the fourth is refused.

    The requests are enhance presses with an empty draft, which the engine refuses at once, so
    the limit is reached without four drafts being written.
    """
    account = ready_account("ratelimit")
    limit = plans["free"]["rate_per_minute"]
    for _ in range(limit):
        result = account.device.stream(stream_body("enhance_composer", composer=EMPTY_COMPOSER))
        assert result.status == 200, result.events

    refused = account.device.post("/assist/stream", stream_body("enhance_composer", composer=EMPTY_COMPOSER))
    assert refused.status_code == 429, refused.text[:300]
    body = refused.json()["error"]
    assert body["code"] == "rate_limited"
    assert 1 <= body["retry_after_s"] <= 60
    assert refused.headers.get("retry-after") == str(body["retry_after_s"])

    reservations = harness.reservations(account.id)
    assert len(reservations) == limit, "the refused request creates no reservation"
    assert harness.counters(account.id)["committed"] == 0, "refused drafts are never charged"


def test_negative_control_the_rate_limit_assertion_can_fail(pro_account, plans: dict):
    """The same four presses on a plan with a higher limit are all accepted."""
    account = pro_account("concurrency")
    assert plans["pro"]["rate_per_minute"] > plans["free"]["rate_per_minute"]
    for _ in range(plans["free"]["rate_per_minute"] + 1):
        response = account.device.post("/assist/stream", stream_body("enhance_composer", composer=EMPTY_COMPOSER))
        assert response.status_code == 200, response.text[:200]
        response.close()
