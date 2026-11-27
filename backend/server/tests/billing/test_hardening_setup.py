"""Adversarial setup, configuration and secret handling.

`stripe-setup` runs against a live Stripe account, so it has to be safe to run twice, it
must never quietly change what a customer is already being charged, and neither it nor any
billing path may write a key where it can be read later.
"""
from __future__ import annotations

import json
import logging
from dataclasses import replace
from pathlib import Path

import pytest

from starling_server.billing import mirror, processor, reconcile, service, setup
from starling_server.config import ConfigError, ensure_valid, load_settings, validate

from .conftest import SECRET_KEY, WEBHOOK_SECRET, billing_env, make_account, make_client
from .fake_stripe import START


# --------------------------------------------------------------------------- idempotence


def test_setup_run_three_times_creates_everything_once(db, bsettings, fake, stripe):
    reports = [setup.run_setup(db, bsettings, stripe) for _ in range(3)]
    assert not any(report.problems for report in reports), [r.problems for r in reports]
    assert reports[0].created and not reports[1].created and not reports[2].created
    assert fake.creates["price"] == len(bsettings.plans.paid_plans())
    assert fake.creates["portal_configuration"] == 2
    assert fake.creates["product"] == 1
    assert reports[0].env == reports[1].env == reports[2].env
    with db.read() as conn:
        rows = conn.execute("SELECT price_id, plan_code, unit_amount, currency FROM price_map ORDER BY plan_code").fetchall()
    assert [row["plan_code"] for row in rows] == ["pro", "starter"]
    for row in rows:
        plan = bsettings.plans.get(row["plan_code"])
        assert (row["unit_amount"], row["currency"]) == (plan.amount_cents, plan.currency)


@pytest.mark.parametrize(
    ("field", "value"),
    [("unit_amount", 1234), ("currency", "eur"), ("interval", "year")],
)
def test_setup_reports_a_price_that_no_longer_matches_and_changes_nothing(db, bsettings, fake, stripe, field, value):
    plan = bsettings.plans.get("starter")
    kwargs = {"unit_amount": plan.amount_cents, "currency": plan.currency, "interval": plan.interval}
    kwargs[field] = value
    drifted = fake.add_price(lookup_key=plan.stripe_lookup_key, **kwargs)
    report = setup.run_setup(db, bsettings, stripe)
    assert report.problems, "a Stripe price that does not match plans.toml must be reported"
    assert any("archive it in Stripe" in problem for problem in report.problems)
    assert fake.prices[drifted["id"]][field if field != "interval" else "recurring"] in (
        value, {"interval": value, "interval_count": 1})
    verify = setup.verify(db, bsettings, stripe)
    assert verify.problems and not verify.ok
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM price_map WHERE plan_code = 'starter'").fetchone()[0] == 0


def test_verify_fills_the_mirror_from_stripe(db, bsettings, fake, stripe):
    setup.run_setup(db, bsettings, stripe)
    db.write(lambda conn: conn.execute("DELETE FROM price_map"))
    report = setup.verify(db, bsettings, stripe)
    assert report.ok, report.problems
    with db.read() as conn:
        mapped = {row["plan_code"]: row["price_id"] for row in conn.execute("SELECT plan_code, price_id FROM price_map")}
    assert mapped == report.prices


def test_a_replaced_price_keeps_its_old_subscribers_on_their_plan(db, bsettings, fake, stripe):
    """price_map rows are never deleted, so a price change does not strand existing customers."""
    setup.run_setup(db, bsettings, stripe)
    plan = bsettings.plans.get("starter")
    with db.read() as conn:
        old_price = mirror.price_id_for_plan(conn, bsettings, plan)
    # The owner makes a new price in the Dashboard and moves the lookup key to it.
    fake.add_price(unit_amount=plan.amount_cents, currency=plan.currency, interval=plan.interval,
                   lookup_key=plan.stripe_lookup_key)
    assert setup.verify(db, bsettings, stripe).ok
    with db.read() as conn:
        assert mirror.plan_for_price(conn, bsettings, old_price) == "starter", "an old price must keep its plan"
        new_price = mirror.price_id_for_plan(conn, bsettings, plan)
    assert new_price != old_price, "checkout must sell the price that now holds the lookup key"
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM price_map WHERE plan_code = 'starter'").fetchone()[0] == 2
    # Archiving the superseded price in Stripe must not stop anyone from subscribing.
    fake.prices[old_price]["active"] = False
    account = make_account(db)
    result = service.create_checkout(db, bsettings, stripe, account, "starter", now=fake.now)
    assert result.kind == "checkout"
    assert fake.sessions[result.session_id]["_price"] == new_price


def test_a_portal_configuration_that_allows_plan_switching_is_refused(db, bsettings, fake, stripe):
    report = setup.run_setup(db, bsettings, stripe)
    manage_id = report.env["STRIPE_PORTAL_CONFIGURATION_ID"]
    fake.portal_configurations[manage_id]["features"]["subscription_update"] = {"enabled": True}
    problems = setup.portal_problems(fake.portal_configurations[manage_id], setup.ROLE_MANAGE, {})
    assert any("plan switching must be off" in problem for problem in problems)
    settings = replace(bsettings, stripe_portal_configuration_id=manage_id)
    assert not setup.verify(db, settings, stripe).ok


def test_an_upgrade_configuration_missing_the_current_price_is_refused(db, bsettings, fake, stripe):
    report = setup.run_setup(db, bsettings, stripe)
    upgrade_id = report.env["STRIPE_PORTAL_UPGRADE_CONFIGURATION_ID"]
    fake.portal_configurations[upgrade_id]["features"]["subscription_update"]["products"] = []
    settings = replace(
        bsettings,
        stripe_portal_configuration_id=report.env["STRIPE_PORTAL_CONFIGURATION_ID"],
        stripe_portal_upgrade_configuration_id=upgrade_id,
    )
    verify = setup.verify(db, settings, stripe)
    assert not verify.ok and any("does not offer the current price" in problem for problem in verify.problems)


def test_a_key_that_does_not_match_the_mode_is_refused_before_anything_is_created(db, bsettings, fake):
    live = replace(bsettings, stripe_mode="live")
    client = make_client(live, fake)
    try:
        report = setup.run_setup(db, live, client)
    finally:
        client.close()
    assert any("not a live key" in problem for problem in report.problems)
    assert fake.creates["price"] == 0 and fake.creates["portal_configuration"] == 0


# --------------------------------------------------------------------------- secrets


def _all_log_text(records: list[logging.LogRecord]) -> str:
    parts = []
    for record in records:
        parts.append(str(record.getMessage()))
        parts.append(json.dumps(getattr(record, "args", None), default=str))
    return "\n".join(parts)


def test_no_billing_path_writes_a_key_to_the_database_or_the_log(billing, caplog):
    caplog.set_level(logging.DEBUG)
    account = make_account(billing.db)
    ids = billing.subscribe(account, "starter")
    service.create_portal(billing.db, billing.settings, billing.client, account, now=billing.now)
    billing.fake.refund(ids["charge"], amount=100)
    billing.sync()
    reconcile.reconcile_events(billing.db, billing.settings, billing.client, now=billing.now)
    reconcile.full_resync(billing.db, billing.settings, billing.client, now=billing.now)
    setup.verify(billing.db, billing.settings, billing.client)
    secrets = [SECRET_KEY, WEBHOOK_SECRET, SECRET_KEY[8:], WEBHOOK_SECRET[6:]]
    logged = _all_log_text(caplog.records)
    for secret in secrets:
        assert secret not in logged, "a secret reached the log"
    with billing.db.read() as conn:
        dump = "\n".join(
            str(row) for table in
            ("billing_events", "subscriptions", "stripe_customers", "price_map", "checkout_sessions", "service_state")
            for row in conn.execute(f"SELECT * FROM {table}").fetchall()
        )
    for secret in secrets:
        assert secret not in dump, "a secret reached the database"


def test_a_stripe_error_never_carries_the_key_into_an_owner_facing_string(billing):
    account = make_account(billing.db)
    billing.subscribe(account, "starter")
    billing.fake.fail_next("GET", "/v1/subscriptions", status=500, times=3)
    failed = reconcile.resync_account(billing.db, billing.settings, billing.client, account, now=billing.now)
    assert SECRET_KEY not in failed["message"] and "injected failure" not in failed["message"]


def test_setup_output_names_no_secret(db, bsettings, fake, stripe):
    report = setup.run_setup(db, bsettings, stripe)
    text = json.dumps({"created": report.created, "reused": report.reused, "problems": report.problems,
                       "warnings": report.warnings, "env": report.env})
    assert SECRET_KEY not in text and WEBHOOK_SECRET not in text
    assert set(report.env) == {"STRIPE_ACCOUNT_ID", "STRIPE_PORTAL_CONFIGURATION_ID",
                               "STRIPE_PORTAL_UPGRADE_CONFIGURATION_ID"}


# --------------------------------------------------------------------------- production configuration


def production_env(tmp_path, **extra):
    env = billing_env(tmp_path / "data")
    env.update({
        "STARLING_ENV": "production",
        "STRIPE_MODE": "live",
        "STRIPE_SECRET_KEY": "sk_live_" + "x" * 24,
        "STRIPE_API_BASE": "https://api.stripe.com",
        "STARLING_PUBLIC_URL": "https://starling.example",
    })
    env.update(extra)
    return env


def test_production_refuses_to_start_without_a_stripe_key(tmp_path):
    settings = load_settings(production_env(tmp_path, STRIPE_SECRET_KEY=""))
    problems = validate(settings, "gateway")
    assert any("STRIPE_SECRET_KEY is required in production" in problem for problem in problems)
    with pytest.raises(ConfigError):
        ensure_valid(settings, "gateway")


def test_a_free_only_production_starts_without_stripe(tmp_path):
    free_only = Path(__file__).resolve().parents[2] / "config" / "plans-free.toml"
    settings = load_settings(production_env(tmp_path, STRIPE_SECRET_KEY="", STRIPE_WEBHOOK_SECRET="",
                                            PLANS_FILE=str(free_only)))
    assert settings.plans.paid_plans() == []
    assert not any("STRIPE" in problem for problem in validate(settings, "gateway"))


def test_production_refuses_to_start_without_a_webhook_secret(tmp_path):
    settings = load_settings(production_env(tmp_path, STRIPE_WEBHOOK_SECRET=""))
    problems = validate(settings, "gateway")
    assert any("STRIPE_WEBHOOK_SECRET must be set" in problem for problem in problems)
    with pytest.raises(ConfigError):
        ensure_valid(settings, "gateway")


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"STRIPE_WEBHOOK_SECRET": "not-a-secret"}, "STRIPE_WEBHOOK_SECRET must be set"),
        ({"STRIPE_SECRET_KEY": "sk_test_" + "x" * 24}, "STRIPE_MODE is 'live' but STRIPE_SECRET_KEY is not a live key"),
        ({"STRIPE_MODE": "test", "STRIPE_SECRET_KEY": "sk_test_" + "x" * 24}, "STRIPE_MODE must be 'live' in production"),
        ({"STRIPE_SECRET_KEY": "totally-wrong"}, "does not look like a Stripe secret"),
        ({"STRIPE_API_BASE": "https://stripe.fake.invalid"}, "STRIPE_API_BASE must be https://api.stripe.com"),
    ],
)
def test_production_refuses_every_broken_stripe_setting(tmp_path, overrides, expected):
    settings = load_settings(production_env(tmp_path, **overrides))
    problems = validate(settings, "gateway")
    assert any(expected in problem for problem in problems), problems
    with pytest.raises(ConfigError):
        ensure_valid(settings, "gateway")


def test_a_development_gateway_without_stripe_answers_503_rather_than_starting_billing(db, tmp_path, fake):
    """Without a key nothing billing-related runs, and the webhook route says so."""
    from starling_server.billing import webhooks

    settings = load_settings(billing_env(tmp_path / "data2", STRIPE_SECRET_KEY="", STRIPE_WEBHOOK_SECRET=""))
    body, header = fake.delivery({"id": "evt_1", "object": "event", "type": "invoice.paid", "livemode": False,
                                  "created": fake.now, "data": {"object": {"id": "in_1", "object": "invoice"}}},
                                 WEBHOOK_SECRET)
    result = webhooks.intake(db, settings, body, header, fake.now)
    assert result.status_code == 503 and result.body["error"]["code"] == "billing_unconfigured"
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM billing_events").fetchone()[0] == 0
