"""Owner dashboard fixtures: an elevated owner driving the real routes, and row seeding helpers.

The website templates come from `tests/core/conftest.py`: one stand-in per template that dumps every
`web.render.REQUIRED_CONTEXT` key as JSON, so route tests read the context with `context_value`
instead of parsing the website workstream's real HTML.

`owner` is a signed-in, CLI-granted, TOTP-enrolled and elevated admin on `client`; `customer` is an
ordinary verified account with real entitlements (no Stripe), so `usage.service.account_state` and
`usage.grants` behave exactly as they do for a live free account.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

import pytest

from starling_server.auth import admin, totp
from starling_server.db import Database

from ..conftest import PUBLIC_URL
from ..core.conftest import FakeClock, env, fake_clock, web_dir  # noqa: F401  (fixtures used by name)
from ..core.helpers import PASSWORD, context_value, create_user, post_form

OWNER_EMAIL = "owner@example.com"  # the only address in STARLING_OWNER_EMAILS (tests/conftest.py)
CUSTOMER_EMAIL = "customer@example.com"
DAY = 86_400

# A prompt-shaped string planted in usage_reservations.result_json: no admin page may ever show it.
PLANTED_DRAFT = "PLANTED-DRAFT-thanks-for-the-kind-words-about-our-launch"


# --------------------------------------------------------------------------- the owner's browser


def enroll_and_elevate(client, fake_clock: FakeClock, next_path: str = "/admin") -> bytes:
    """Confirm the authenticator and elevate, as `tests/core/test_admin.py` does."""
    secret = context_value(client.get("/admin/totp"), "secret")
    key = totp.decode_secret(secret)
    enrolled = post_form(client, "/admin/totp", {"password": PASSWORD, "code": totp.totp(key, fake_clock.value)})
    assert enrolled.status_code == 303, enrolled.text
    fake_clock.advance(totp.STEP_S)  # the enrollment code cannot be replayed
    elevated = post_form(client, "/admin/elevate", {"code": totp.totp(key, fake_clock.value), "next": next_path})
    assert elevated.status_code == 303, elevated.text
    return key


@dataclass
class Owner:
    account_id: str
    email: str
    totp_key: bytes


@pytest.fixture
def owner(app, client, settings, fake_clock) -> Owner:
    create_user(client, OWNER_EMAIL)
    admin.grant_admin(app.state.db, settings, OWNER_EMAIL)
    key = enroll_and_elevate(client, fake_clock)
    with app.state.db.read() as conn:
        account_id = conn.execute("SELECT id FROM accounts WHERE email_norm = ?", (OWNER_EMAIL,)).fetchone()["id"]
    return Owner(account_id, OWNER_EMAIL, key)


@pytest.fixture
def plain_admin(app, client, settings, fake_clock) -> Owner:
    """Granted admin who has not enrolled an authenticator yet."""
    create_user(client, OWNER_EMAIL)
    admin.grant_admin(app.state.db, settings, OWNER_EMAIL)
    with app.state.db.read() as conn:
        account_id = conn.execute("SELECT id FROM accounts WHERE email_norm = ?", (OWNER_EMAIL,)).fetchone()["id"]
    return Owner(account_id, OWNER_EMAIL, b"")


# --------------------------------------------------------------------------- customers


def make_customer(db: Database, *, email: str = CUSTOMER_EMAIL, now: int, verified: bool = True,
                  status: str = "active", tenant_seq: int | None = None) -> str:
    """A verified account row with no subscription: `entitlements` gives it the real free plan."""
    account_id = str(uuid.uuid4())
    seq = tenant_seq if tenant_seq is not None else int(uuid.uuid4().int % 900_000) + 1000

    def write(conn) -> None:
        conn.execute(
            "INSERT INTO accounts (id, tenant_seq, email, email_norm, allowance_key, email_verified_at, "
            "password_hash, password_changed_at, status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'x', 0, ?, ?)",
            (account_id, seq, email, email.lower(), f"ak-{seq}", now - 10 * DAY if verified else None, status,
             now - 30 * DAY),
        )

    db.write(write)
    return account_id


@pytest.fixture
def customer(app, fake_clock) -> str:
    return make_customer(app.state.db, now=fake_clock.value)


@pytest.fixture
def second_browser(app, client):
    """Another browser on the same app; `client` owns the lifespan."""
    from fastapi.testclient import TestClient

    return TestClient(app, base_url=PUBLIC_URL)


# --------------------------------------------------------------------------- seeding control rows


def write(db: Database, sql: str, *params: Any) -> None:
    db.write(lambda conn: conn.execute(sql, params))


def add_price(db: Database, price_id: str, plan_code: str, unit_amount: int, *, interval: str = "month",
              currency: str = "usd", livemode: int = 0) -> None:
    write(db, "INSERT INTO price_map (price_id, lookup_key, plan_code, interval, currency, unit_amount, active, livemode) "
              "VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
          price_id, f"lk_{price_id}", plan_code, interval, currency, unit_amount, livemode)


def add_subscription(db: Database, account_id: str, *, subscription_id: str, status: str = "active",
                     price_id: str | None = None, plan_code: str = "starter", now: int, livemode: int = 0,
                     disputed_at: int | None = None, cancel_at_period_end: int = 0) -> None:
    write(db,
          "INSERT INTO subscriptions (subscription_id, account_id, customer_id, status, price_id, plan_code, "
          "current_period_start, current_period_end, cancel_at_period_end, disputed_at, livemode, synced_at) "
          "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
          subscription_id, account_id, f"cus_{subscription_id}", status, price_id, plan_code,
          now - 5 * DAY, now + 25 * DAY, cancel_at_period_end, disputed_at, livemode, now)


def add_ledger(db: Database, account_id: str, *, kind: str, amount: int, created_at: int,
               period_id: str = "period-1", actor: str = "system", reason: str = "test") -> None:
    write(db, "INSERT INTO credit_ledger (account_id, period_id, kind, amount, actor, reason, created_at) "
              "VALUES (?, ?, ?, ?, ?, ?, ?)",
          account_id, period_id, kind, amount, actor, reason, created_at)


def add_ai_call(db: Database, account_id: str | None, *, started_at: int, cost_micros: int = 1_000,
                bucket: str = "press", lane: str = "quality", model: str = "test-model") -> None:
    write(db, "INSERT INTO ai_calls (account_id, bucket, lane, model, cost_micros, status, started_at) "
              "VALUES (?, ?, ?, ?, ?, 'ok', ?)",
          account_id, bucket, lane, model, cost_micros, started_at)


def add_budget(db: Database, scope: str, window_key: str, *, cost_micros: int, limit_micros: int = 0,
               tripped_at: int | None = None) -> None:
    write(db, "INSERT INTO budget_counters (scope, window_key, cost_micros, limit_micros, tripped_at) "
              "VALUES (?, ?, ?, ?, ?)",
          scope, window_key, cost_micros, limit_micros, tripped_at)


def add_request_error(db: Database, *, route: str, code: str, http_status: int, at: int, count: int = 1,
                      reserved: int = 0, account_id: str = "", mode: str = "") -> None:
    write(db, "INSERT INTO request_errors (minute, account_id, route, mode, code, http_status, reserved, count, "
              "first_at, last_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
          at - at % 60, account_id, route, mode, code, http_status, reserved, count, at, at)


def add_reservation(db: Database, account_id: str, *, created_at: int, state: str = "committed",
                    result_json: str | None = None, period_id: str = "period-1", cost_micros: int = 0) -> str:
    reservation_id = str(uuid.uuid4())
    write(db,
          "INSERT INTO usage_reservations (id, account_id, period_id, idempotency_key, payload_hash, route, mode, "
          "credits, state, cost_micros, result_json, created_at, expires_at, finished_at, release_reason) "
          "VALUES (?, ?, ?, ?, ?, '/api/v1/assist', 'chat', 1, ?, ?, ?, ?, ?, ?, ?)",
          reservation_id, account_id, period_id, str(uuid.uuid4()), "h" * 64, state, cost_micros, result_json,
          created_at, created_at + 300, created_at + 10,
          # as the usage service writes them: every released row says why
          state if state in ("released", "expired") else None)
    return reservation_id


def add_service_state(db: Database, key: str, value: Any, updated_at: int) -> None:
    write(db, "INSERT INTO service_state (key, value_json, updated_at) VALUES (?, ?, ?) "
              "ON CONFLICT (key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at",
          key, value if isinstance(value, str) else json.dumps(value), updated_at)


def audit_rows(db: Database, action: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM admin_audit" + (" WHERE action = ?" if action else "") + " ORDER BY id"
    with db.read() as conn:
        return [dict(row) for row in conn.execute(sql, (action,) if action else ())]


def service_state(db: Database, key: str) -> Any:
    with db.read() as conn:
        row = conn.execute("SELECT value_json FROM service_state WHERE key = ?", (key,)).fetchone()
    return None if row is None else json.loads(row["value_json"])
