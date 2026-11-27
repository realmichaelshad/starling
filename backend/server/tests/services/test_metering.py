"""Metering and limits: reservations, call caps, fair use, background share, budgets, pause and exact costs."""
from __future__ import annotations

import json
import uuid
from datetime import date

import pytest

from starling_server.services.metering import FREE_PAUSED_MESSAGE, PAUSE_MESSAGE

from . import fake_openai as ai
from .conftest import NOW, chat_body

MIDNIGHT_S = 12 * 3600  # NOW is 12:00 UTC
NEXT_MONTH_S = 15 * 86_400 + 12 * 3600  # 2026-09-15 12:00 -> 2026-10-01 00:00


def error_code(response) -> str:
    return response.json()["error"]["code"]


def test_tenant_a_token_cannot_bill_tenant_b_reservation(services):
    tenant_a, tenant_b = services.add_account(), services.add_account()
    reservation_b = services.add_reservation(tenant_b)
    response = services.chat(tenant_a, request_id=reservation_b)
    assert response.status_code == 409 and error_code(response) == "reservation_inactive"
    assert services.fake.requests == []
    assert services.rows("SELECT * FROM ai_calls") == []
    row = services.one("SELECT ai_calls, cost_micros FROM usage_reservations WHERE id = ?", (reservation_b,))
    assert row == {"ai_calls": 0, "cost_micros": 0}
    assert services.chat(tenant_b, request_id=reservation_b).status_code == 200  # negative control
    assert services.one("SELECT ai_calls FROM usage_reservations WHERE id = ?", (reservation_b,))["ai_calls"] == 1


@pytest.mark.parametrize("state", ["reserved", "committed", "released", "expired"])
def test_reservation_that_is_not_active_is_refused_without_upstream_call(services, state):
    account = services.add_account()
    response = services.chat(account, request_id=services.add_reservation(account, state=state))
    assert response.status_code == 409 and error_code(response) == "reservation_inactive"
    assert services.fake.requests == []


def test_active_reservation_past_its_hard_wall_or_unknown_is_refused(services):
    account = services.add_account()
    assert services.chat(account, request_id=services.add_reservation(account, expires_in=0)).status_code == 409
    assert services.chat(account, request_id=str(uuid.uuid4())).status_code == 409
    assert services.fake.requests == []


def test_call_cap_per_request_counts_every_attempt(make_services):
    services = make_services(overrides={"AI_MAX_CALLS_PER_REQUEST": "3"})
    account = services.add_account()
    reservation = services.add_reservation(account)
    assert services.chat(account, request_id=reservation).status_code == 200
    services.fake.queue(ai.rate_limited(retry_after="1"), ai.ok_json())
    assert services.chat(account, request_id=reservation).status_code == 200  # two attempts
    response = services.chat(account, request_id=reservation)
    assert response.status_code == 429 and error_code(response) == "call_cap_exceeded"
    assert len(services.fake.requests) == 3
    assert services.chat(account, request_id=services.add_reservation(account)).status_code == 200  # negative control


def test_fair_use_ceiling_counts_total_spend(services):
    account = services.add_account()
    services.set_spend(account, cost_micros=account.ceiling_micros - 1)
    assert services.press(account).status_code == 200  # negative control
    services.set_spend(account, cost_micros=account.ceiling_micros, background_micros=account.ceiling_micros // 10)
    response = services.press(account)
    assert response.status_code == 402 and error_code(response) == "fair_use_exceeded"
    assert len(services.fake.requests) == 1


def test_background_calls_are_capped_at_ten_percent_of_the_ceiling(services):
    account = services.add_account()
    cap = account.ceiling_micros * 10 // 100
    services.set_spend(account, cost_micros=cap - 1, background_micros=cap - 1)
    assert services.chat(account, background=True).status_code == 200  # negative control
    services.set_spend(account, cost_micros=cap, background_micros=cap)
    response = services.chat(account, background=True)
    assert response.status_code == 402 and error_code(response) == "fair_use_exceeded"
    assert services.press(account).status_code == 200  # press calls are not limited by the background share
    assert len(services.fake.requests) == 2


def test_background_spend_is_recorded_in_both_counters_on_the_billing_period(services):
    account = services.add_account()
    services.fake.queue(ai.ok_json(prompt=1000, cached=0, completion=100))
    assert services.chat(account, background=True).status_code == 200
    [row] = services.rows("SELECT * FROM ai_calls")
    assert row["bucket"] == "background" and row["reservation_id"] is None and row["cost_micros"] > 0
    counters = services.one("SELECT cost_micros, background_micros FROM usage_counters WHERE account_id = ? AND period_id = ?",
                            (account.id, account.period_id))
    assert counters == {"cost_micros": row["cost_micros"], "background_micros": row["cost_micros"]}


def test_daily_budget_refuses_with_retry_after_until_midnight(services):
    account = services.add_account("starter")
    limit = services.budget_limit("global_day")
    services.set_budget("global_day", limit - 1)
    assert services.press(account).status_code == 200  # negative control
    services.set_budget("global_day", limit)
    response = services.press(account)
    assert response.status_code == 503 and error_code(response) == "budget_exhausted"
    assert response.headers["retry-after"] == str(MIDNIGHT_S)
    assert len(services.fake.requests) == 1
    row = services.one("SELECT tripped_at FROM budget_counters WHERE scope = 'global_day' AND window_key = '2026-09-15'")
    assert row["tripped_at"] == NOW


def test_monthly_budget_refuses_until_the_next_month(services):
    account = services.add_account("starter")
    limit = services.budget_limit("global_month")
    services.set_budget("global_month", limit - 1)
    assert services.press(account).status_code == 200  # negative control
    services.set_budget("global_month", limit)
    response = services.press(account)
    assert response.status_code == 503 and error_code(response) == "budget_exhausted"
    assert response.headers["retry-after"] == str(NEXT_MONTH_S)


def test_a_tripped_window_stays_tripped_below_its_limit(services):
    account = services.add_account("starter")
    services.set_budget("global_day", 0, tripped_at=NOW - 60)
    assert services.press(account).status_code == 503


def test_free_accounts_trip_at_the_free_share_while_paid_accounts_continue(services):
    free, paid = services.add_account("free"), services.add_account("starter")
    trip = services.budget_limit("free_day")
    assert trip == services.settings.ai_daily_budget_micros * services.settings.ai_free_trip_pct // 100
    services.set_budget("global_day", trip - 1)
    assert services.press(free).status_code == 200  # negative control
    services.set_budget("global_day", trip)
    response = services.press(free)
    assert response.status_code == 503 and error_code(response) == "budget_exhausted"
    assert response.json()["error"]["message"] == FREE_PAUSED_MESSAGE
    assert response.headers["retry-after"] == str(MIDNIGHT_S)
    assert services.press(paid).status_code == 200
    tripped = services.one("SELECT tripped_at FROM budget_counters WHERE scope = 'free_day' AND window_key = '2026-09-15'")
    assert tripped["tripped_at"] == NOW


@pytest.mark.parametrize(
    ("value", "paused", "retry_after"),
    [
        ("true", True, "300"),
        ('{"paused": true, "message": "Back soon", "retry_after_s": 120}', True, "120"),
        ("{not json", True, "300"),
        ("false", False, None),
        ('{"paused": false}', False, None),
    ],
)
def test_owner_pause(services, value, paused, retry_after):
    account = services.add_account("starter")
    services.set_service_state("ai_paused", value)
    response = services.press(account)
    if paused:
        assert response.status_code == 503 and error_code(response) == "budget_exhausted"
        assert response.json()["error"]["message"] == PAUSE_MESSAGE
        assert response.headers["retry-after"] == retry_after
        assert services.fake.requests == []
    else:
        assert response.status_code == 200


def test_owner_pause_also_stops_background_calls(services):
    account = services.add_account()
    services.set_service_state("ai_paused", "true")
    assert services.chat(account, background=True).status_code == 503
    services.set_service_state("ai_paused", None)
    assert services.chat(account, background=True).status_code == 200


def test_unknown_or_suspended_accounts_are_unauthorized(services):
    suspended = services.add_account(status="suspended")
    response = services.chat(suspended, request_id=services.add_reservation(suspended))
    assert response.status_code == 401 and error_code(response) == "unauthorized"
    stranger = services.token(str(uuid.uuid4()))
    assert services.chat(token=stranger, background=True).status_code == 401
    assert services.fake.requests == []


def test_every_counter_receives_the_exact_micro_dollar_cost(services):
    account = services.add_account("free")
    reservation = services.add_reservation(account)
    services.fake.queue(ai.ok_json(prompt=1000, cached=200, completion=500))
    assert services.chat(account, chat_body(), request_id=reservation).status_code == 200
    model = services.settings.ai_model_quality
    expected = services.settings.ai_pricing.price_for(model, date(2026, 9, 15)).cost_micros(1000, 200, 500)
    assert expected == 764  # 800 x $0.20/M + 200 x $0.02/M + 500 x $1.20/M
    [row] = services.rows("SELECT * FROM ai_calls")
    assert {key: row[key] for key in ("account_id", "reservation_id", "bucket", "lane", "model", "input_tokens",
                                       "cached_input_tokens", "output_tokens", "estimated", "cost_micros", "status",
                                       "error_code", "started_at", "finished_at")} == {
        "account_id": account.id, "reservation_id": reservation, "bucket": "press", "lane": "starling-quality",
        "model": model, "input_tokens": 1000, "cached_input_tokens": 200, "output_tokens": 500, "estimated": 0,
        "cost_micros": 764, "status": "ok", "error_code": None, "started_at": NOW, "finished_at": NOW,
    }
    assert services.one("SELECT cost_micros, background_micros FROM usage_counters WHERE account_id = ? AND period_id = ?",
                        (account.id, account.period_id)) == {"cost_micros": 764, "background_micros": 0}
    assert services.one("SELECT cost_micros, ai_calls, heartbeat_at FROM usage_reservations WHERE id = ?", (reservation,)) == {
        "cost_micros": 764, "ai_calls": 1, "heartbeat_at": NOW}
    budgets = {row["scope"]: row for row in services.rows("SELECT * FROM budget_counters")}
    assert (budgets["global_day"]["window_key"], budgets["global_day"]["cost_micros"]) == ("2026-09-15", 764)
    assert (budgets["global_month"]["window_key"], budgets["global_month"]["cost_micros"]) == ("2026-09", 764)
    assert (budgets["free_day"]["window_key"], budgets["free_day"]["cost_micros"]) == ("2026-09-15", 764)
    assert budgets["global_day"]["limit_micros"] == services.settings.ai_daily_budget_micros
    assert budgets["global_month"]["limit_micros"] == services.settings.ai_monthly_budget_micros
    assert all(row["tripped_at"] is None for row in budgets.values())


def test_paid_spend_is_not_counted_in_the_free_share(services):
    account = services.add_account("starter")
    services.fake.queue(ai.ok_json(prompt=1000, cached=200, completion=500))
    assert services.press(account).status_code == 200
    scopes = {row["scope"]: row["cost_micros"] for row in services.rows("SELECT scope, cost_micros FROM budget_counters")}
    assert scopes["global_day"] == 764 and scopes.get("free_day", 0) == 0


def test_failed_attempts_are_recorded_without_cost(services):
    account = services.add_account()
    services.fake.queue(ai.error(400, "invalid_prompt"))
    assert services.press(account).status_code == 400
    [row] = services.rows("SELECT status, error_code, cost_micros, estimated, finished_at FROM ai_calls")
    assert row == {"status": "error", "error_code": "invalid_prompt", "cost_micros": 0, "estimated": 0, "finished_at": NOW}
    assert json.loads(json.dumps(row))  # plain values only


def test_background_spend_never_trips_the_free_plan(services):
    """U5: a launch day of Free activations (voice cards: background work) must not pause every
    Free account's presses. Background spend still counts in the global day budget."""
    free = services.add_account("free")
    trip = services.budget_limit("free_day")
    services.fake.queue(ai.ok_json(prompt=1000, cached=200, completion=500))
    assert services.chat(free, background=True).status_code == 200
    scopes = {row["scope"]: row["cost_micros"] for row in services.rows("SELECT scope, cost_micros FROM budget_counters")}
    assert scopes["global_day"] == 764 and scopes.get("free_day", 0) == 0, "background is not Free spend"
    # The day's spend is over the free share, but all of it above the share is background work.
    services.set_budget("global_day", trip + 764)
    services.set_service_state("background_spend_day", json.dumps({"day": "2026-09-15", "cost_micros": 764 + 1}))
    assert services.press(free).status_code == 200, "background alone never trips the free plan"
    services.set_service_state("background_spend_day", json.dumps({"day": "2026-09-15", "cost_micros": 764}))
    response = services.press(free)
    assert response.status_code == 503 and error_code(response) == "budget_exhausted", "press spend still does"
