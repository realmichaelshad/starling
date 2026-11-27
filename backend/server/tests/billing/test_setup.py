"""`stripe-setup`, `stripe-verify` and the production startup guard."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from starling_server.billing import commands, lifecycle, setup, stripe_client

from .conftest import make_client

STARTER_KEY = "starling_starter_monthly_usd"
PRO_KEY = "starling_pro_monthly_usd"


@pytest.fixture
def fake_factory(fake):
    previous = stripe_client.set_client_factory(lambda settings: make_client(settings, fake))
    try:
        yield
    finally:
        stripe_client.set_client_factory(previous)


def test_setup_creates_prices_and_portal_configurations_once(db, bsettings, fake, stripe):
    first = setup.run_setup(db, bsettings, stripe)
    assert not first.problems
    assert fake.creates == {"product": 1, "price": 2, "portal_configuration": 2}
    second = setup.run_setup(db, bsettings, stripe)
    assert not second.problems and second.created == []
    assert fake.creates == {"product": 1, "price": 2, "portal_configuration": 2}
    assert first.env == second.env

    prices = {p["lookup_key"]: p for p in fake.prices.values() if p["lookup_key"]}
    assert prices[STARTER_KEY]["unit_amount"] == 900 and prices[PRO_KEY]["unit_amount"] == 1900
    assert len({p["product"] for p in fake.prices.values()}) == 1
    manage = fake.portal_configurations[first.env["STRIPE_PORTAL_CONFIGURATION_ID"]]["features"]
    assert manage["subscription_update"]["enabled"] is False
    assert manage["subscription_cancel"] == {"enabled": True, "mode": "at_period_end", "proration_behavior": "none"}
    assert set(manage["customer_update"]["allowed_updates"]) == {"address", "tax_id"}
    assert manage["payment_method_update"]["enabled"] is True and manage["invoice_history"]["enabled"] is True
    upgrade = fake.portal_configurations[first.env["STRIPE_PORTAL_UPGRADE_CONFIGURATION_ID"]]["features"]["subscription_update"]
    assert upgrade["enabled"] is True and upgrade["proration_behavior"] == "always_invoice"
    assert {price for product in upgrade["products"] for price in product["prices"]} == {p["id"] for p in prices.values()}
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM price_map").fetchone()[0] == 2


def test_setup_command_second_run_creates_nothing(bsettings, fake, fake_factory, monkeypatch, capsys):
    monkeypatch.setattr(commands, "get_settings", lambda: bsettings)
    assert commands._setup(None) == 0
    after_first = dict(fake.creates)
    assert commands._setup(None) == 0
    assert dict(fake.creates) == after_first
    output = capsys.readouterr().out
    assert "STRIPE_PORTAL_CONFIGURATION_ID=" in output and "STRIPE_PORTAL_UPGRADE_CONFIGURATION_ID=" in output


def test_setup_never_changes_an_existing_price(db, bsettings, fake, stripe):
    fake.add_price(unit_amount=1000, lookup_key=STARTER_KEY)
    report = setup.run_setup(db, bsettings, stripe)
    assert any("starter" in problem and "1000" in problem for problem in report.problems)
    assert [p["unit_amount"] for p in fake.prices.values() if p["lookup_key"] == STARTER_KEY] == [1000]
    assert fake.creates["price"] == 1  # only the missing pro price

    # Once the owner archives the wrong price, setup creates a matching one and takes the lookup key over.
    for price in fake.prices.values():
        if price["lookup_key"] == STARTER_KEY:
            price["active"] = False
    report = setup.run_setup(db, bsettings, stripe)
    assert not report.problems
    holders = [p for p in fake.prices.values() if p["lookup_key"] == STARTER_KEY]
    assert len(holders) == 1 and holders[0]["unit_amount"] == 900 and holders[0]["active"]


def test_verify_detects_a_price_mismatch(db, configured, fake, stripe):
    assert setup.verify(db, configured, stripe).ok
    fake.add_price(unit_amount=2500, lookup_key=PRO_KEY)
    report = setup.verify(db, configured, stripe)
    assert not report.ok
    assert any("pro" in problem and "2500" in problem for problem in report.problems)


def test_stripe_verify_command_exit_code(configured, fake, fake_factory, monkeypatch, capsys):
    monkeypatch.setattr(commands, "get_settings", lambda: configured)
    assert commands._verify(None) == 0
    fake.add_price(unit_amount=900, currency="eur", lookup_key=STARTER_KEY)
    assert commands._verify(None) == 1
    assert "MISMATCH" in capsys.readouterr().err


def test_verify_checks_the_account_and_the_key_mode(db, configured, stripe):
    report = setup.verify(db, replace(configured, stripe_account_id="acct_someoneelse"), stripe)
    assert any("STRIPE_ACCOUNT_ID" in problem for problem in report.problems)
    report = setup.verify(db, replace(configured, stripe_mode="live"), stripe)
    assert any("not a live key" in problem for problem in report.problems)


def test_restricted_key_skips_only_the_account_check(db, configured, fake, stripe):
    fake.denied_paths.add("/v1/account")
    report = setup.verify(db, replace(configured, stripe_account_id="acct_someoneelse"), stripe)
    assert report.ok and report.warnings
    report = setup.verify(db, replace(configured, stripe_mode="live"), stripe)
    assert any("not a live key" in problem for problem in report.problems)  # the mode check still applies


def test_portal_configuration_with_plan_switching_is_reported(db, configured, fake, stripe):
    manage = fake.portal_configurations[configured.stripe_portal_configuration_id]
    manage["features"]["subscription_update"] = {"enabled": True, "products": [], "proration_behavior": "none"}
    report = setup.verify(db, configured, stripe)
    assert any("plan switching must be off" in problem for problem in report.problems)


def _app(settings, db, client):
    return SimpleNamespace(state=SimpleNamespace(settings=settings, db=db, stripe_client=client, billing_background_jobs=False))


def test_production_startup_refuses_a_mismatch(db, configured, fake, stripe):
    fake.add_price(unit_amount=1000, lookup_key=STARTER_KEY)
    app = _app(replace(configured, env="production"), db, stripe)
    with pytest.raises(RuntimeError, match="plans.toml"):
        asyncio.run(lifecycle.startup(app))
    assert app.state.billing_queue is None


def test_production_startup_passes_when_stripe_matches(db, configured, stripe):
    app = _app(replace(configured, env="production"), db, stripe)

    async def run():
        await lifecycle.startup(app)
        try:
            assert app.state.billing_queue is not None
        finally:
            await lifecycle.shutdown(app)

    asyncio.run(run())
