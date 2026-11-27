"""Overview figures computed from seeded rows, against numbers worked out by hand.

Every expected value below is derived in the comment beside it, so a change in the SQL has to be
argued for rather than blessed. The rules are CONTRACTS 7 (usage), 8 (Stripe) and 11 (tables).
"""
from __future__ import annotations

from fractions import Fraction

import pytest

from starling_server.admin import alerts, metrics
from starling_server.usage import service as usage_service

from ..core.helpers import context_value
from .conftest import (add_ai_call, add_budget, add_ledger, add_price, add_reservation, add_request_error,
                       add_service_state, add_subscription, audit_rows, make_customer, write)

DAY = 86_400


@pytest.fixture(autouse=True)
def fake_services(monkeypatch):
    from starling_server.services import client as services_client

    async def unreachable(settings, **kwargs):
        return {"ok": False, "error": {"code": "services_unreachable", "message": "not running"}}

    monkeypatch.setattr(services_client, "services_status", unreachable)
    monkeypatch.setattr(services_client, "ai_check", unreachable)


@pytest.fixture
def seeded(app, fake_clock, owner):
    """Seven subscriptions, credits, AI calls, failures and webhooks with known totals."""
    db, now = app.state.db, fake_clock.value
    accounts = {name: make_customer(db, email=f"{name}@example.com", now=now) for name in "abcdefg"}

    add_price(db, "price_m", "starter", 900, interval="month")
    add_price(db, "price_y", "starter", 9_000, interval="year")
    add_price(db, "price_w", "pro", 100, interval="week")
    add_price(db, "price_eur", "pro", 1_900, interval="month", currency="eur")

    # Counted: test mode (livemode 0) and status active or past_due.
    add_subscription(db, accounts["a"], subscription_id="sub_a", price_id="price_m", plan_code="starter", now=now)
    add_subscription(db, accounts["b"], subscription_id="sub_b", price_id="price_y", plan_code="starter", now=now,
                     cancel_at_period_end=1)
    add_subscription(db, accounts["c"], subscription_id="sub_c", price_id="price_w", plan_code="pro", now=now,
                     status="past_due")
    add_subscription(db, accounts["d"], subscription_id="sub_d", price_id=None, plan_code="pro", now=now)
    add_subscription(db, accounts["g"], subscription_id="sub_g", price_id="price_m", plan_code="pro", now=now,
                     disputed_at=now - 3_600)
    # Not counted: the live-mode subscription and the canceled one.
    add_subscription(db, accounts["e"], subscription_id="sub_e", price_id="price_m", plan_code="starter", now=now,
                     livemode=1)
    add_subscription(db, accounts["f"], subscription_id="sub_f", price_id="price_m", plan_code="starter", now=now,
                     status="canceled")

    # Credits: commits only, by window. `day_start` is now - 1600 for START_TIME.
    for amount, at in ((3, now - 100), (5, now - 2 * DAY), (7, now - 10 * DAY), (11, now - 40 * DAY)):
        add_ledger(db, accounts["a"], kind="commit", amount=amount, created_at=at)
    add_ledger(db, accounts["a"], kind="admin_grant", amount=99, created_at=now - 100)  # never counted as use

    # AI: 4 calls today, 2 more inside 30 days, 1 outside. Spend comes from budget_counters.
    day_key, month_key = usage_service.budget_window_keys(now)
    add_budget(db, "global_day", day_key, cost_micros=2_500_000, limit_micros=10_000_000)
    add_budget(db, "global_month", month_key, cost_micros=40_000_000, limit_micros=90_000_000)
    for at in (now - 60, now - 120, now - 180, now - 240, now - 5 * DAY, now - 6 * DAY, now - 40 * DAY):
        add_ai_call(db, accounts["a"], started_at=at)

    # Released requests that still spent money, inside 30 days.
    add_reservation(db, accounts["a"], created_at=now - 3 * DAY, state="released", cost_micros=900)
    add_reservation(db, accounts["a"], created_at=now - 4 * DAY, state="expired", cost_micros=900)
    add_reservation(db, accounts["a"], created_at=now - 5 * DAY, state="released", cost_micros=0)  # no spend
    add_reservation(db, accounts["a"], created_at=now - 40 * DAY, state="released", cost_micros=900)  # too old

    # Failures on the metered routes, last 24 hours.
    add_request_error(db, route="/api/v1/assist", code="ai_unavailable", http_status=503, at=now - 3_600, count=6,
                      reserved=1, account_id=accounts["a"], mode="chat")
    add_request_error(db, route="/api/v1/assist/stream", code="rate_limited", http_status=429, at=now - 7_200, count=4,
                      reserved=0, account_id=accounts["a"], mode="chat")
    add_request_error(db, route="/api/v1/session", code="signed_out", http_status=401, at=now - 1_000, count=50)
    add_request_error(db, route="/api/v1/assist", code="busy", http_status=409, at=now - 2 * DAY, count=99)  # window

    # 36 drafting requests in the window, so requests = 36 + 4 refusals before a reservation = 40.
    for index in range(36):
        add_reservation(db, accounts["a"], created_at=now - 1_800 - index)

    write(db, "INSERT INTO billing_events (event_id, type, livemode, payload_json, status, attempts, received_at, "
              "processed_at) VALUES ('evt_1', 'invoice.paid', 0, '{}', 'processed', 1, ?, ?)", now - 900, now - 890)
    write(db, "INSERT INTO billing_events (event_id, type, livemode, payload_json, status, attempts, last_error, "
              "received_at) VALUES ('evt_2', 'charge.refunded', 0, '{}', 'failed', 3, 'timed out', ?)", now - 7_200)
    write(db, "INSERT INTO billing_events (event_id, type, livemode, payload_json, status, attempts, received_at) "
              "VALUES ('evt_3', 'customer.subscription.updated', 0, '{}', 'received', 0, ?)", now - 3_600)
    write(db, "INSERT INTO billing_events (event_id, type, livemode, payload_json, status, attempts, received_at) "
              "VALUES ('evt_old', 'invoice.paid', 0, '{}', 'ignored', 1, ?)", now - 3 * DAY)

    add_service_state(db, "backup:last", {"at": now - 7_200, "ok": True, "archive": "/data/backups/starling-01.tar.age",
                                          "size_bytes": 1_048_576, "verified": True, "error": None}, now - 7_200)
    return accounts


def overview(app, fake_clock):
    return metrics.overview(app.state.db, app.state.settings, fake_clock.value)


# --------------------------------------------------------------------------- revenue and subscribers


def test_mrr_normalizes_every_interval_and_ignores_the_other_stripe_mode(app, fake_clock, seeded):
    figures = overview(app, fake_clock)
    # 900/month + 9000/year + 100/week + pro's plans.toml 1900 (no price row) + 900/month (disputed, still paying)
    expected = Fraction(900) + Fraction(9_000, 12) + Fraction(100) * Fraction(52, 12) + Fraction(1_900) + Fraction(900)
    assert expected == Fraction(58_600, 12)
    assert figures["mrr_cents"] == round(expected) == 4_883
    assert figures["currency"] == "usd"
    assert figures["paid_accounts"] == 5  # a, b, c, d, g; not the live-mode or canceled ones
    # The owner plus the two accounts whose only subscription is live-mode or canceled.
    assert figures["free_accounts"] == 3


def test_subscribers_split_by_plan_and_status(app, fake_clock, seeded):
    rows = {row["plan_code"]: row for row in overview(app, fake_clock)["subscribers"]}
    assert list(rows) == ["starter", "pro"]  # plans.toml order, both shown even when empty
    assert rows["starter"] == {"plan_code": "starter", "plan_name": "Starter", "active": 2, "past_due": 0,
                               "suspended": 0, "canceling": 1}
    assert rows["pro"] == {"plan_code": "pro", "plan_name": "Pro", "active": 1, "past_due": 1, "suspended": 1,
                           "canceling": 0}


def test_a_price_in_another_currency_is_left_out_of_mrr(app, fake_clock, seeded, owner):
    account = make_customer(app.state.db, email="eur@example.com", now=fake_clock.value)
    before = overview(app, fake_clock)["mrr_cents"]
    add_subscription(app.state.db, account, subscription_id="sub_eur", price_id="price_eur", plan_code="pro",
                     now=fake_clock.value)
    after = overview(app, fake_clock)
    assert after["mrr_cents"] == before  # the amount is unknown in usd, so it is not guessed
    assert {row["plan_code"]: row["active"] for row in after["subscribers"]}["pro"] == 2  # but the subscriber counts


# --------------------------------------------------------------------------- credits, AI and failures


def test_credits_count_commits_only_in_each_window(app, fake_clock, seeded):
    assert overview(app, fake_clock)["credits"] == {"today": 3, "last_7d": 8, "last_30d": 15}


def test_ai_calls_and_cost_come_from_the_counters_the_budgets_trip_on(app, fake_clock, seeded):
    ai = overview(app, fake_clock)["ai"]
    assert ai["calls_today"] == 4 and ai["calls_30d"] == 6
    assert ai["cost_today_micros"] == 2_500_000 and ai["cost_month_micros"] == 40_000_000
    assert ai["daily_budget_micros"] == app.state.settings.ai_daily_budget_micros
    assert ai["monthly_budget_micros"] == app.state.settings.ai_monthly_budget_micros
    assert ai["released_with_spend_30d"] == 2


def test_the_failure_rate_uses_the_metered_routes_only(app, fake_clock, seeded):
    failures = overview(app, fake_clock)["failures"]
    # failed = 6 + 4; requests = 36 reservations in the window + the 4 refused before reserving.
    assert failures["failed"] == 10 and failures["requests"] == 40
    assert failures["rate_pct"] == round(10 * 100 / 40, 2) == 25.0
    assert failures["top_codes"] == [{"code": "ai_unavailable", "count": 6}, {"code": "rate_limited", "count": 4}]
    assert failures["window_label"] == "last 24 hours"

    page = metrics.failures_page(app.state.db, fake_clock.value)
    assert page["totals"] == {"requests": 40, "failed": 10, "rate_pct": 25.0}
    # The page lists every extension route, so the 50 signed_out refusals appear without changing the rate.
    assert {row["code"] for row in page["failures"]} == {"ai_unavailable", "rate_limited", "signed_out"}
    assert [row["code"] for row in page["summary"]] == ["ai_unavailable", "rate_limited"]


def test_webhook_health(app, fake_clock, seeded):
    hooks = overview(app, fake_clock)["webhooks"]
    assert hooks["received_24h"] == 3 and hooks["pending"] == 1 and hooks["failed"] == 1
    assert hooks["oldest_pending_at"] == fake_clock.value - 3_600
    assert hooks["last_processed_at"] == fake_clock.value - 890

    page = metrics.webhooks_page(app.state.db, "failed", fake_clock.value)
    assert [event["event_id"] for event in page["events"]] == ["evt_2"]
    assert page["summary"]["processed"] == 2  # processed plus ignored
    assert metrics.webhooks_page(app.state.db, "nonsense", fake_clock.value)["status_filter"] == "all"


# --------------------------------------------------------------------------- backups and free disk space


def test_the_overview_reports_the_last_backup(app, fake_clock, seeded):
    backup = overview(app, fake_clock)["backup"]
    assert backup == {"at": fake_clock.value - 7_200, "ok": True, "archive": "starling-01.tar.age",
                      "size_bytes": 1_048_576, "verified": True, "error": None}  # the folder is not shown


def test_free_disk_space_is_measured_and_alerted_below_fifteen_percent(app, fake_clock, owner, monkeypatch):
    import shutil
    from collections import namedtuple

    Usage = namedtuple("Usage", "total used free")
    measured = overview(app, fake_clock)["disk"]
    assert measured is not None and 0 <= measured["free_pct"] <= 100
    assert measured["total_bytes"] > 0 and measured["path"]

    def with_free(fraction: float):
        total = 100 * 1024 ** 3
        free = int(total * fraction)
        monkeypatch.setattr(shutil, "disk_usage", lambda path: Usage(total, total - free, free))

    with_free(0.20)  # the negative control: a roomy volume raises nothing
    assert overview(app, fake_clock)["disk"]["free_pct"] == 20.0
    assert [item for item in overview(app, fake_clock)["alerts"] if item["key"] == alerts.DISK_LOW_KEY] == []

    with_free(0.10)
    [warning] = [item for item in overview(app, fake_clock)["alerts"] if item["key"] == alerts.DISK_LOW_KEY]
    assert warning["level"] == "warning" and "10.0% free" in warning["text"]

    with_free(0.03)
    [error] = [item for item in overview(app, fake_clock)["alerts"] if item["key"] == alerts.DISK_LOW_KEY]
    assert error["level"] == "error" and error["created_at"] == fake_clock.value

    def refuse(path):
        raise OSError("no such volume")

    monkeypatch.setattr(shutil, "disk_usage", refuse)
    figures = overview(app, fake_clock)
    assert figures["disk"] is None and [i for i in figures["alerts"] if i["key"] == alerts.DISK_LOW_KEY] == []


# --------------------------------------------------------------------------- the pages themselves


def test_the_overview_page_sends_the_metrics_it_computed(app, client, fake_clock, seeded):
    page = client.get("/admin")
    assert page.status_code == 200
    shown = context_value(page, "metrics")
    assert shown["mrr_cents"] == 4_883 and shown["credits"] == {"today": 3, "last_7d": 8, "last_30d": 15}
    assert shown["failures"]["rate_pct"] == 25.0
    assert set(shown) == {"generated_at", "service", "alerts", "paid_accounts", "free_accounts", "mrr_cents",
                          "currency", "subscribers", "credits", "ai", "failures", "webhooks", "backup", "disk"}


def test_the_audit_page_pages_and_summarises_without_secrets(app, client, fake_clock, owner, customer):
    from ..core.helpers import post_form

    for index in range(51):
        assert post_form(client, f"/admin/customers/{customer}/grant",
                         {"credits": "1", "reason": f"Grant {index}"}).status_code == 303
    first = context_value(client.get("/admin/audit"), "entries")
    assert len(first) == 50 and context_value(client.get("/admin/audit"), "page")["has_next"] is True
    assert first[0]["admin_email"] == owner.email and first[0]["action"] == "grant_credits"
    assert "credits=1" in first[0]["params_summary"] and "reason=Grant 50" in first[0]["params_summary"]
    total = len(audit_rows(app.state.db))  # the grants plus the owner's own grant, enrolment and elevation
    second = client.get("/admin/audit", params={"page": "2"})
    assert context_value(second, "page") == {"number": 2, "has_prev": True, "has_next": False}
    assert len(context_value(second, "entries")) == total - 50 > 0
    assert context_value(second, "entries")[-1]["admin_email"] == "Command line"  # the CLI grant of owner access

    assert metrics.params_summary(None) == "" and metrics.params_summary("not json") == ""
    assert metrics.params_summary('["a"]') == ""
    assert metrics.params_summary('{"b": 2, "a": "x"}') == "a=x, b=2"
