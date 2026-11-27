"""Billing fixtures: Stripe-configured settings, the fake Stripe, clients, accounts and helpers.

Every Stripe call goes to `FakeStripe`; an autouse fixture fails any test that tries to use
a real HTTP transport, so no test can reach the real Stripe API.
"""
from __future__ import annotations

import itertools
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from starling_server.billing import entitlements, mirror, processor, service, setup, webhooks
from starling_server.billing.stripe_client import StripeClient
from starling_server.config import Settings, load_settings
from starling_server.db import Database

from ..conftest import base_env
from .fake_stripe import START, FakeStripe

SECRET_KEY = "sk_" + "test_" + "starlingfakekey" * 2
WEBHOOK_SECRET = "whsec_" + "currentsecret" * 3
PREVIOUS_WEBHOOK_SECRET = "whsec_" + "previoussecret" * 3
ACCOUNT_ID = "acct_fake0001"
DAY = 86_400
_tenant_seq = itertools.count(5000)


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    def refuse(self, request):
        raise AssertionError(f"a real network request was attempted: {request.url}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)


def billing_env(data_dir: Path, **extra: str) -> dict[str, str]:
    env = base_env(data_dir)
    env.update({
        "STRIPE_MODE": "test",
        "STRIPE_SECRET_KEY": SECRET_KEY,
        "STRIPE_ACCOUNT_ID": ACCOUNT_ID,
        "STRIPE_WEBHOOK_SECRET": WEBHOOK_SECRET,
        "STRIPE_API_BASE": "https://stripe.fake.invalid",
    })
    env.update(extra)
    return env


def make_client(settings: Settings, fake: FakeStripe) -> StripeClient:
    return StripeClient(settings, transport=fake.transport, sleep=lambda _seconds: None)


def make_account(db: Database, *, verified: bool = True, created_at: int = START - 40 * DAY, email: str | None = None) -> str:
    account_id = str(uuid.uuid4())
    seq = next(_tenant_seq)
    address = email or f"member{seq}@example.com"
    verified_at = created_at + 60 if verified else None
    db.write(lambda conn: conn.execute(
        "INSERT INTO accounts (id, tenant_seq, email, email_norm, allowance_key, email_verified_at, password_hash, "
        "password_changed_at, status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'x', 0, 'active', ?)",
        (account_id, seq, address, address, f"ak-{seq}", verified_at, created_at),
    ))
    return account_id


@pytest.fixture
def fake() -> FakeStripe:
    return FakeStripe(api_key=SECRET_KEY)


@pytest.fixture
def bsettings(tmp_path) -> Settings:
    return load_settings(billing_env(tmp_path / "data"))


@pytest.fixture
def db(bsettings):
    database = Database(bsettings.control_db_path)
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def stripe(bsettings, fake):
    client = make_client(bsettings, fake)
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def configured(db, bsettings, fake, stripe) -> Settings:
    """`stripe-setup` against the fake; settings carry the resulting portal configuration ids."""
    report = setup.run_setup(db, bsettings, stripe)
    assert not report.problems, report.problems
    return replace(
        bsettings,
        stripe_portal_configuration_id=report.env["STRIPE_PORTAL_CONFIGURATION_ID"],
        stripe_portal_upgrade_configuration_id=report.env["STRIPE_PORTAL_UPGRADE_CONFIGURATION_ID"],
    )


@dataclass
class Billing:
    db: Database
    settings: Settings
    fake: FakeStripe
    client: StripeClient

    @property
    def now(self) -> int:
        return self.fake.now

    def price(self, plan_code: str) -> str:
        key = self.settings.plans.get(plan_code).stripe_lookup_key
        return next(p["id"] for p in self.fake.prices.values() if p.get("lookup_key") == key)

    def deliver(self, *, shuffle_seed: int | None = None, duplicate: bool = False) -> list[webhooks.IntakeResult]:
        results = []
        for body, header in self.fake.deliveries([WEBHOOK_SECRET], shuffle_seed=shuffle_seed, duplicate=duplicate):
            results.append(webhooks.intake(self.db, self.settings, body, header, self.fake.now))
        return results

    def process(self) -> dict[str, int]:
        return processor.process_pending(self.db, self.settings, self.client, self.fake.now)

    def sync(self, **options: Any) -> dict[str, int]:
        self.deliver(**options)
        return self.process()

    def entitlement(self, account_id: str, now: int | None = None) -> entitlements.Entitlement:
        return entitlements.current_entitlement(self.db, self.settings, account_id, self.fake.now if now is None else now)

    def checkout(self, account_id: str, plan_code: str = "starter") -> service.CheckoutResult:
        return service.create_checkout(self.db, self.settings, self.client, account_id, plan_code, now=self.fake.now)

    def subscribe(self, account_id: str, plan_code: str = "starter", outcome: str = "paid", deliver: bool = True) -> dict[str, Any]:
        result = self.checkout(account_id, plan_code)
        assert result.kind == "checkout", result
        ids = self.fake.complete_checkout(result.session_id, outcome=outcome)
        if deliver:
            self.sync()
        return {**ids, "session": result.session_id}

    def one(self, sql: str, *args: Any):
        with self.db.read() as conn:
            return conn.execute(sql, args).fetchone()

    def all(self, sql: str, *args: Any) -> list:
        with self.db.read() as conn:
            return conn.execute(sql, args).fetchall()

    def alerts(self, kind: str | None = None) -> list[dict[str, Any]]:
        with self.db.read() as conn:
            found = mirror.list_alerts(conn)
        return [alert for alert in found if kind is None or alert["kind"] == kind]


@pytest.fixture
def billing(db, configured, fake, stripe) -> Billing:
    return Billing(db, configured, fake, stripe)
