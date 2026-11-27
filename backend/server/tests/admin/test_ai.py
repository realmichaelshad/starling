"""Pausing and resuming drafting, the AI connection test, and the `ai-pause`/`ai-resume` CLI.

`service_state['ai_paused']` (CONTRACTS 11) has no other writer: the dashboard and the CLI here are it.
The readers are `usage.service` (the customer's `service.status` and metered reservations) and
`services.metering`, so every test checks the effect a customer would feel, not just the row.
"""
from __future__ import annotations

import argparse
import uuid

import pytest

from starling_server.admin import actions, commands

from starling_server.usage import service as usage_service

from ..core.helpers import context_value, post_form
from .conftest import add_budget, audit_rows, service_state

RESERVE_ARGS = ("h" * 64, "/api/v1/assist", "chat")


@pytest.fixture(autouse=True)
def fake_services(monkeypatch):
    """The AI connection test never leaves the process."""
    from starling_server.services import client as services_client

    outcome = {"value": {"ok": True, "results": [{"model": "gpt-test", "ok": True, "latency_ms": 42,
                                                  "error_code": None}]}}

    async def check(settings, **kwargs):
        return outcome["value"]

    async def status(settings, **kwargs):
        return {"ok": True, "keys": {"primary": {"configured": True, "last4": "Q7wX"},
                                     "secondary": {"configured": False, "last4": None}, "active": "primary"},
                "models": {"quality": "gpt-test", "fast": "gpt-test", "vision": "gpt-test"},
                "upstream": {"base_url": "https://api.test.invalid/v1"}, "reasoning_effort": "low"}

    monkeypatch.setattr(services_client, "ai_check", check)
    monkeypatch.setattr(services_client, "services_status", status)
    return outcome


def reserve(app, account_id, fake_clock):
    return usage_service.reserve(app.state.db, app.state.settings, account_id, str(uuid.uuid4()), *RESERVE_ARGS,
                                 fake_clock.value)


@pytest.fixture
def drafting_customer(app, customer, fake_clock):
    """A customer who can reserve, so a refusal can only come from the pause."""
    app.state.db.write(lambda conn: conn.execute(
        "INSERT INTO consents (account_id, kind, version, passive_learning, granted_at) "
        "VALUES (?, 'extension_disclosure', ?, 0, ?)",
        (customer, app.state.settings.consent_version, fake_clock.value)))
    assert isinstance(reserve(app, customer, fake_clock), usage_service.Reservation)
    return customer


# --------------------------------------------------------------------------- web search


def test_the_ai_page_counts_todays_web_searches_once_each(app, client, owner, customer, fake_clock):
    from .conftest import write

    today = fake_clock.value - fake_clock.value % 86_400
    for reservation, started, cost in (("r1", today + 60, 11_000), ("r1", today + 61, 12_000),
                                       ("r2", today + 120, 13_000), ("r3", today - 60, 99_000)):
        write(app.state.db, "INSERT INTO ai_calls (account_id, reservation_id, bucket, lane, model, cost_micros, "
                            "status, started_at) VALUES (?, ?, 'press', 'starling-search', 'gpt-test', ?, 'ok', ?)",
              customer, reservation, cost, started)
    search = context_value(client.get("/admin/ai"), "ai")["search"]
    assert search["enabled"] is True and search["per_account_day"] == 20
    assert search["searches_today"] == 2  # a retried search is one search; yesterday's is not counted
    assert search["cost_today_micros"] == 36_000
    assert search["price_per_1k_micros"] == 10_000_000  # the shipped ai_pricing.json row


# --------------------------------------------------------------------------- pause and resume


def test_pause_and_resume_toggle_ai_paused_and_what_customers_get(app, client, owner, drafting_customer, fake_clock):
    db, settings = app.state.db, app.state.settings
    before = usage_service.account_state(db, settings, drafting_customer, fake_clock.value)
    assert before["service"] == {"status": "ok", "scope": None, "message": None, "retry_at": None}

    paused = post_form(client, "/admin/ai/pause", {"pause_reason": "Upstream outage",
                                                   "pause_message": "Back in about an hour."}, follow=True)
    assert paused.status_code == 200
    assert service_state(db, usage_service.PAUSE_KEY) == {"paused": True, "message": "Back in about an hour.",
                                                          "retry_after_s": usage_service.DEFAULT_PAUSE_RETRY_S}
    during = usage_service.account_state(db, settings, drafting_customer, fake_clock.value)
    assert during["service"] == {"status": "paused", "scope": "all", "message": "Back in about an hour.",
                                 "retry_at": fake_clock.value + usage_service.DEFAULT_PAUSE_RETRY_S}
    with pytest.raises(usage_service.UsageError) as caught:
        reserve(app, drafting_customer, fake_clock)
    assert (caught.value.http_status, caught.value.code) == (503, "service_paused")
    assert caught.value.retry_after_s == usage_service.DEFAULT_PAUSE_RETRY_S

    resumed = post_form(client, "/admin/ai/resume", {"resume_reason": "Recovered"}, follow=True)
    assert resumed.status_code == 200 and "running again" in resumed.text
    assert service_state(db, usage_service.PAUSE_KEY) is None
    after = usage_service.account_state(db, settings, drafting_customer, fake_clock.value)
    assert after["service"] == {"status": "ok", "scope": None, "message": None, "retry_at": None}
    assert isinstance(reserve(app, drafting_customer, fake_clock), usage_service.Reservation)
    assert [row["action"] for row in audit_rows(db) if row["action"].startswith("ai_")] == ["ai_pause", "ai_resume"]


def test_pausing_without_a_message_uses_the_standard_notice(app, client, owner):
    assert post_form(client, "/admin/ai/pause", {"pause_reason": "Maintenance"}).status_code == 303
    stored = service_state(app.state.db, usage_service.PAUSE_KEY)
    assert stored["message"] == usage_service.PAUSED_MESSAGE
    assert audit_rows(app.state.db, "ai_pause")[0]["params_json"].count("Maintenance") == 1


@pytest.mark.parametrize("form", [{"pause_reason": ""}, {"pause_reason": "x" * 201},
                                  {"pause_reason": "ok", "pause_message": "y" * 201}])
def test_a_pause_without_a_usable_reason_or_message_changes_nothing(app, client, owner, form):
    refused = post_form(client, "/admin/ai/pause", form, follow=True)
    assert refused.status_code == 200 and "was not paused" in refused.text
    assert service_state(app.state.db, usage_service.PAUSE_KEY) is None
    assert len(audit_rows(app.state.db, "ai_pause")) == 1  # the refusal is audited exactly once

    assert post_form(client, "/admin/ai/pause", {"pause_reason": "Now valid"}).status_code == 303
    assert service_state(app.state.db, usage_service.PAUSE_KEY)["paused"] is True


def test_resuming_when_nothing_was_paused_says_so(app, client, owner):
    answer = post_form(client, "/admin/ai/resume", {}, follow=True)
    assert "not paused by the owner" in answer.text
    assert [r["params_json"] for r in audit_rows(app.state.db, "ai_resume")][0].count("not_paused") == 1


def test_resuming_while_a_budget_is_used_up_warns_that_drafting_stays_paused(app, client, owner, fake_clock):
    day_key, _ = usage_service.budget_window_keys(fake_clock.value)
    add_budget(app.state.db, "global_day", day_key, cost_micros=10_000_000, limit_micros=10_000_000,
               tripped_at=fake_clock.value)
    assert post_form(client, "/admin/ai/pause", {"pause_reason": "Outage"}).status_code == 303
    answer = post_form(client, "/admin/ai/resume", {"resume_reason": "Upstream fixed"}, follow=True)
    assert "budget is used up" in answer.text
    assert service_state(app.state.db, usage_service.PAUSE_KEY) is None  # the owner pause really was lifted
    assert context_value(client.get("/admin/ai"), "ai")["service"]["status"] == "paused"


# --------------------------------------------------------------------------- the AI page and its test


def test_the_ai_page_shows_keys_models_budgets_and_the_pause(app, client, owner, fake_clock):
    shown = context_value(client.get("/admin/ai"), "ai")
    assert shown["base_url_host"] == "api.test.invalid" and shown["reasoning_effort"] == "low"
    assert shown["services"] == {"ok": True, "error_code": None}
    assert shown["keys"] == [{"slot": "primary", "configured": True, "masked": "…Q7wX", "in_use": True},
                             {"slot": "secondary", "configured": False, "masked": None, "in_use": False}]
    assert shown["pause"] == {"paused": False, "message": None, "since": None} and shown["last_test"] is None
    assert shown["budgets"]["daily_budget_micros"] == app.state.settings.ai_daily_budget_micros

    assert post_form(client, "/admin/ai/pause", {"pause_reason": "Outage", "pause_message": "Soon."}).status_code == 303
    paused = context_value(client.get("/admin/ai"), "ai")["pause"]
    assert paused == {"paused": True, "message": "Soon.", "since": fake_clock.value}


def test_the_connection_test_stores_codes_and_latency_but_never_provider_text(app, client, owner, fake_services,
                                                                              fake_clock):
    passed = post_form(client, "/admin/ai/test", {}, follow=True)
    assert "test passed" in passed.text
    stored = service_state(app.state.db, actions.AI_TEST_KEY)
    assert stored["ok"] is True and stored["latency_ms"] == 42 and stored["model"] == "gpt-test"
    assert stored["error_code"] is None and stored["at"] == fake_clock.value

    fake_services["value"] = {"ok": False, "error": {"code": "ai_auth", "message": "Incorrect API key sk-proj-SECRET"},
                              "results": [{"model": "gpt-test", "ok": False, "latency_ms": 0,
                                           "error_code": "ai_auth"}]}
    failed = post_form(client, "/admin/ai/test", {}, follow=True)
    assert "ai_auth" in failed.text and "rejected the API key" in failed.text
    assert "SECRET" not in failed.text
    stored = service_state(app.state.db, actions.AI_TEST_KEY)
    assert stored == {"at": fake_clock.value, "ok": False, "latency_ms": 0, "model": "gpt-test",
                      "error_code": "ai_auth",
                      "models": [{"model": "gpt-test", "ok": False, "latency_ms": 0, "error_code": "ai_auth"}]}
    assert "SECRET" not in str(audit_rows(app.state.db, "ai_test"))
    assert context_value(client.get("/admin/ai"), "ai")["last_test"]["error_code"] == "ai_auth"


def test_a_services_answer_that_is_not_a_dict_is_recorded_as_a_failure(app, client, owner, fake_services):
    fake_services["value"] = "not a dict"
    answer = post_form(client, "/admin/ai/test", {}, follow=True)
    assert "check_failed" in answer.text
    assert service_state(app.state.db, actions.AI_TEST_KEY)["ok"] is False


# --------------------------------------------------------------------------- the CLI


@pytest.fixture
def cli(monkeypatch, settings):
    monkeypatch.setattr(commands, "get_settings", lambda: settings)
    parser = argparse.ArgumentParser()
    commands.register(parser.add_subparsers(dest="command", required=True))
    return parser


def test_cli_pause_and_resume_write_the_same_row_as_the_dashboard(app, client, cli, capsys, fake_clock):
    paused = cli.parse_args(["ai-pause", "--reason", "Upstream outage", "--message", "Back soon",
                             "--retry-after-s", "900"])
    assert paused.func(paused) == 0
    assert service_state(app.state.db, usage_service.PAUSE_KEY) == {"paused": True, "message": "Back soon",
                                                                    "retry_after_s": 900}
    rows = audit_rows(app.state.db, "ai_pause")
    assert len(rows) == 1 and rows[0]["admin_id"] == "cli" and '"retry_after_s": 900' in rows[0]["params_json"]
    assert "paused for all customers" in capsys.readouterr().out

    resumed = cli.parse_args(["ai-resume", "--reason", "Recovered"])
    assert resumed.func(resumed) == 0
    assert service_state(app.state.db, usage_service.PAUSE_KEY) is None
    assert "running again" in capsys.readouterr().out

    again = cli.parse_args(["ai-resume"])
    assert again.func(again) == 0 and "was not paused" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [["ai-pause", "--reason", ""],
                                  ["ai-pause", "--reason", "ok", "--retry-after-s", "0"],
                                  ["ai-pause", "--reason", "ok", "--retry-after-s", "86401"],
                                  ["ai-pause", "--reason", "ok", "--message", "m" * 201]])
def test_cli_pause_refuses_bad_arguments(app, client, cli, capsys, argv):
    args = cli.parse_args(argv)
    assert args.func(args) == 1
    assert "refused" in capsys.readouterr().err
    assert service_state(app.state.db, usage_service.PAUSE_KEY) is None

    good = cli.parse_args(["ai-pause", "--reason", "ok"])  # the negative control
    assert good.func(good) == 0
    assert service_state(app.state.db, usage_service.PAUSE_KEY)["retry_after_s"] == actions.PAUSE_RETRY_AFTER_S


def test_retry_after_parsing(app):
    assert actions.parse_retry_after_s(None) == actions.PAUSE_RETRY_AFTER_S
    assert actions.parse_retry_after_s("") == actions.PAUSE_RETRY_AFTER_S
    assert actions.parse_retry_after_s(1) == 1
    assert actions.parse_retry_after_s("120") == 120
    assert actions.parse_retry_after_s(actions.MAX_PAUSE_RETRY_AFTER_S) == actions.MAX_PAUSE_RETRY_AFTER_S
    for bad in (0, -1, actions.MAX_PAUSE_RETRY_AFTER_S + 1, True, "abc", "-5", 1.5):
        assert actions.parse_retry_after_s(bad) is None, bad


def test_the_owner_pause_is_read_the_way_usage_service_reads_it(app, drafting_customer, fake_clock):
    """A malformed value counts as paused, and JSON `true` is honoured (CONTRACTS 11)."""
    from starling_server.admin import metrics
    from .conftest import add_service_state

    db = app.state.db
    for stored, paused in (("not json at all", True), ("true", True), ('{"paused": false}', False),
                           ('{"paused": true, "message": "  "}', True)):
        add_service_state(db, usage_service.PAUSE_KEY, stored, fake_clock.value)
        with db.read() as conn:
            assert metrics.ai_pause(conn)["paused"] is paused, stored
        state = usage_service.account_state(db, app.state.settings, drafting_customer, fake_clock.value)
        assert (state["service"]["status"] == "paused") is paused, stored
