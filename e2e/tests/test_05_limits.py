"""Journey 5: the four ways drafting stops (local, mocked providers).

Three of them belong to one account - the plan allowance, the fair-use cost ceiling - and two
are service wide: the owner's pause and the global AI budget. The last case is the one that
matters most for a night's sleep: raising the budget and restarting the services process
clears the stop at once (CONTRACTS 4.4), rather than waiting for the UTC day to turn.
"""
from __future__ import annotations

import time
import uuid

import pytest

from e2e.tests import billing_flow
from e2e.tests.client import Control, stream_body

DRAFT_COMPOSER = {
    "action": "post",
    "draft_text": "We deleted two build steps nobody ran and the pipeline got four minutes shorter.",
    "intent": "enhance",
}
UPGRADE_PATH = "/pricing?from=extension&reason=limit"


def refusal(account, body=None, key: str | None = None):
    """A metered press that is expected to be refused before the engine is ever asked."""
    response = account.device.post(
        "/assist/stream", body or stream_body("enhance_composer", composer=DRAFT_COMPOSER), key=key
    )
    response.read()
    return response


@pytest.fixture
def free_account(ready_account):
    return ready_account("limits")


@pytest.fixture
def paid_account(ready_account, harness: Control):
    account = ready_account("paidlimits")
    if not account.extras.get("subscribed"):
        billing_flow.subscribe(account, harness, "starter")
        account.extras["subscribed"] = True
    return account


# --------------------------------------------------------------------------- the plan allowance


def test_using_the_whole_free_allowance_gives_limit_reached(free_account, harness: Control, plans: dict):
    """One real draft, then the rest of the allowance removed the way an owner would remove it."""
    account = free_account
    allowance = plans["free"]["monthly_credits"]
    first = account.device.stream(stream_body("enhance_composer", composer=DRAFT_COMPOSER))
    assert first.final is not None, first.events
    used = harness.counters(account.id)["committed"]
    assert used >= 1

    harness.debit_credits(account.id, allowance - used, reason="journey 5: exhaust the free allowance")
    try:
        session = account.device.session()
        assert session["usage"]["remaining"] == 0
        assert account.site.account_usage()["usage"]["remaining"] == 0
        assert "limit" in account.site.account_usage()["notices"]

        response = refusal(account)
        assert response.status_code == 402
        body = response.json()["error"]
        assert body["code"] == "limit_reached"
        assert body["upgrade_path"] == UPGRADE_PATH
        assert body["usage"]["remaining"] == 0 and body["usage"]["allowance"] == allowance
        assert body["usage"] == session["usage"], "the refusal carries the same block as the session"
    finally:
        harness.grant_credits(account.id, allowance - used, reason="journey 5: put the allowance back")
    assert account.device.session()["usage"]["remaining"] > 0  # negative control


def test_the_fair_use_ceiling_gives_fair_use_exceeded(free_account, harness: Control, plans: dict):
    """The period's recorded AI spend is pushed to the plan's ceiling, as a heavy month would."""
    account = free_account
    ceiling = plans["free"]["cost_ceiling_micros"]
    counters = harness.counters(account.id)
    before = counters["cost_micros"]
    harness.set_period_cost(account.id, counters["period_id"], ceiling)
    try:
        response = refusal(account)
        assert response.status_code == 402
        body = response.json()["error"]
        assert body["code"] == "fair_use_exceeded"
        assert body["upgrade_path"] == UPGRADE_PATH
        assert body["usage"]["fair_use_blocked"] is True
        assert account.device.session()["usage"]["fair_use_blocked"] is True
        assert "fair-use" in account.site.account_usage()["notices"]
    finally:
        harness.set_period_cost(account.id, counters["period_id"], before)
    assert account.device.session()["usage"]["fair_use_blocked"] is False  # negative control


# --------------------------------------------------------------------------- service wide


def test_an_owner_pause_stops_drafting_for_everyone(free_account, paid_account, harness: Control):
    harness.set_owner_pause({"paused": True, "message": "Maintenance for a few minutes.", "retry_after_s": 42})
    try:
        for account in (free_account, paid_account):
            response = refusal(account)
            assert response.status_code == 503, account.email
            body = response.json()["error"]
            assert body["code"] == "service_paused"
            assert body["retry_after_s"] == 42
            assert response.headers.get("retry-after") == "42"
            service = account.device.session()["service"]
            assert {k: service[k] for k in ("status", "scope", "message")} == {
                "status": "paused", "scope": "all", "message": "Maintenance for a few minutes."
            }
            assert isinstance(service["retry_at"], int)  # the owner's 42 s, as an instant
            assert "paused for everyone" in account.site.account_usage()["html"]
    finally:
        harness.clear_owner_pause()
    for account in (free_account, paid_account):  # negative control
        assert account.device.session()["service"]["status"] == "ok"


def test_an_exhausted_daily_budget_pauses_everyone_and_raising_it_clears_the_stop(
    free_account, paid_account, harness: Control, services_restart
):
    account = free_account
    spent = harness.global_budget("global_day")["cost_micros"]
    assert spent > 0, "the journeys before this one should have spent something"

    # A budget below what today already cost: the next admitted call is refused and the window
    # is tripped for everyone.
    services_restart(AI_DAILY_BUDGET_USD="0.000001")
    try:
        first = account.device.stream(stream_body("enhance_composer", composer=DRAFT_COMPOSER))
        assert first.error is not None, first.events
        assert first.error["code"] in ("service_paused", "ai_unavailable"), first.error
        harness.wait_for(
            lambda: harness.global_budget("global_day")["tripped_at"] is not None, 20.0, "the daily budget to trip"
        )

        for waiting in (free_account, paid_account):
            response = refusal(waiting)
            assert response.status_code == 503, waiting.email
            body = response.json()["error"]
            assert body["code"] == "service_paused"
            assert body["retry_after_s"] >= 1
            assert waiting.device.session()["service"]["status"] == "paused"
    finally:
        services_restart()

    harness.wait_for(
        lambda: harness.global_budget("global_day")["tripped_at"] is None, 20.0, "the stop to be cleared"
    )
    for account in (free_account, paid_account):
        assert account.device.session()["service"]["status"] == "ok"
    recovered = paid_account.device.stream(stream_body("enhance_composer", composer=DRAFT_COMPOSER))
    assert recovered.final is not None, recovered.events


def test_the_free_trip_percentage_pauses_free_accounts_only(
    free_account, paid_account, harness: Control, services_restart
):
    """`AI_FREE_TRIP_PCT` of the daily budget: free accounts wait, paid ones keep drafting."""
    spent = harness.global_budget("global_day")["cost_micros"]
    assert spent > 0
    # A day budget today's spend is still under, whose free-account share it is already over.
    services_restart(AI_DAILY_BUDGET_USD=f"{spent * 2 / 1_000_000:.6f}", AI_FREE_TRIP_PCT="40")
    try:
        first = free_account.device.stream(stream_body("enhance_composer", composer=DRAFT_COMPOSER))
        assert first.error is not None, first.events
        harness.wait_for(
            lambda: harness.global_budget("free_day")["tripped_at"] is not None, 20.0, "the free share to trip"
        )

        refused = refusal(free_account)
        assert refused.status_code == 503
        assert refused.json()["error"]["code"] == "service_paused"
        assert "Paid plans keep drafting" in refused.json()["error"]["message"]
        free_service = free_account.device.session()["service"]
        assert (free_service["status"], free_service["scope"]) == ("paused", "free")
        assert "Drafting on the Free plan is paused" in free_account.site.account_usage()["html"]

        # The paid account is untouched: this is the whole point of the free trip.
        assert paid_account.device.session()["service"]["status"] == "ok"
        still_drafting = paid_account.device.stream(stream_body("enhance_composer", composer=DRAFT_COMPOSER))
        assert still_drafting.final is not None, still_drafting.events
    finally:
        services_restart()
    harness.wait_for(
        lambda: harness.global_budget("free_day")["tripped_at"] is None, 20.0, "the free stop to be cleared"
    )
    assert free_account.device.session()["service"]["status"] == "ok"


def test_negative_control_the_pause_assertions_can_fail(free_account, harness: Control):
    """Without a pause, the same checks do not hold: the assertions above are not vacuous."""
    assert harness.one("SELECT value_json FROM service_state WHERE key = 'ai_paused'") is None
    session = free_account.device.session()
    assert session["service"]["status"] == "ok"
    response = refusal(free_account)
    assert response.status_code != 503, response.text[:200]
