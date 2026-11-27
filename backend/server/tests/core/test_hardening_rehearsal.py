"""Rehearsing with Stripe's sandbox on the deployed host, in production mode and in plain sight.

A first deployment should take test payments before real ones (docs/DEPLOYMENT.md). Production
used to refuse `STRIPE_MODE=test` outright, so the only way to do that was
`STARLING_ENV=development` on a public host: no OS isolation between customers, none of the
production checks. `STRIPE_ALLOW_TEST_MODE=true` is the explicit alternative: every production
check still applies, and the site says on every page that payments are not real.
"""
from __future__ import annotations

import dataclasses

from starling_server.admin import alerts
from starling_server.config import load_settings, validate
from starling_server.web.render import site_context

from ..conftest import base_env

TEST_KEY = "sk_test_" + "x" * 24


def production(tmp_path, **extra: str):
    env = dict(base_env(tmp_path / "data"), STARLING_ENV="production", STARLING_ISOLATION="os",
               STARLING_PUBLIC_URL="https://app.starling-hosting.com", STRIPE_MODE="test", STRIPE_SECRET_KEY=TEST_KEY,
               STRIPE_WEBHOOK_SECRET="whsec_" + "y" * 24, **extra)
    return load_settings(env)


def stripe_mode_problems(settings) -> list[str]:
    return [problem for problem in validate(settings, "gateway") if "STRIPE_MODE must be 'live'" in problem]


def test_production_still_refuses_the_sandbox_unless_told(tmp_path):
    settings = production(tmp_path)
    [problem] = stripe_mode_problems(settings)
    assert "STRIPE_ALLOW_TEST_MODE=true" in problem, "the refusal names the way out"
    assert settings.payments_rehearsal is False


def test_the_switch_allows_it_and_nothing_else(tmp_path):
    settings = production(tmp_path, STRIPE_ALLOW_TEST_MODE="true")
    assert stripe_mode_problems(settings) == []
    assert settings.payments_rehearsal is True
    # Every other production check still stands: this is not development by another name.
    problems = validate(settings, "gateway")
    assert any("EMAIL_MODE must be 'smtp'" in p for p in problems)
    assert dataclasses.replace(settings, isolation="none").isolation == "none"
    assert any("STARLING_ISOLATION must be 'os'" in p for p in validate(dataclasses.replace(settings, isolation="none"), "gateway"))


def test_the_switch_means_nothing_outside_production_or_with_live_keys(tmp_path):
    local = load_settings(dict(base_env(tmp_path / "local"), STRIPE_ALLOW_TEST_MODE="true"))
    assert local.payments_rehearsal is False
    live = dataclasses.replace(production(tmp_path, STRIPE_ALLOW_TEST_MODE="true"), stripe_mode="live")
    assert live.payments_rehearsal is False


def test_every_page_is_told_and_nothing_is_indexed(tmp_path):
    rehearsing = production(tmp_path, STRIPE_ALLOW_TEST_MODE="true")
    assert site_context(rehearsing)["payments_test_mode"] is True
    assert "payments_test_mode" not in site_context(production(tmp_path))
    assert "payments_test_mode" not in site_context(load_settings(base_env(tmp_path / "local")))


def test_the_owner_dashboard_keeps_saying_so(tmp_path):
    [alert] = alerts.rehearsal_alerts(production(tmp_path, STRIPE_ALLOW_TEST_MODE="true"), 1_800_000_000)
    assert alert["level"] == "warning" and alert["key"] == alerts.PAYMENTS_REHEARSAL_KEY
    assert "nobody can be charged" in alert["text"] and "STRIPE_ALLOW_TEST_MODE" in alert["text"]
    assert alerts.rehearsal_alerts(production(tmp_path), 1_800_000_000) == []
