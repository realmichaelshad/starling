"""Services workstream fixtures: a scripted fake AI provider, seeded control data and the services app."""
from __future__ import annotations

import asyncio
import itertools
import socket
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from starling_server.billing import entitlements
from starling_server.config import Settings, load_settings
from starling_server.db import Database
from starling_server.services import tokens
from starling_server.services.app import create_app
from starling_server.services.metering import day_key, month_key

from .fake_openai import FakeOpenAI

PRIMARY_KEY = "test-primary-k3y-aaaa1111"
SECONDARY_KEY = "test-secondary-k3y-bbbb2222"
AI_BASE_URL = "https://ai-provider.invalid/v1"
NOW = int(datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc).timestamp())
PROMPT = "Write a short post about release notes."

_real_getaddrinfo = socket.getaddrinfo


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    """Services tests never resolve a real host name (loopback stays usable for the entrypoint test)."""

    def guarded(host, *args, **kwargs):
        if host in ("127.0.0.1", b"127.0.0.1", "localhost", b"localhost"):
            return _real_getaddrinfo(host, *args, **kwargs)
        raise OSError("services tests must not resolve external host names")

    monkeypatch.setattr(socket, "getaddrinfo", guarded)


class FakeClock:
    """Wall clock, monotonic clock and sleep in one: sleeping advances time instantly and is recorded."""

    def __init__(self, now: float) -> None:
        self.now = float(now)
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += max(0.0, seconds)
        await asyncio.sleep(0)


def chat_body(content: str = PROMPT, *, lane: str = "starling-quality", stream: bool = False, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": lane,
        "messages": [
            {"role": "system", "content": "You are a helpful writing assistant."},
            {"role": "user", "content": content},
        ],
    }
    if stream:
        body["stream"] = True
    body.update(extra)
    return body


@dataclass(frozen=True)
class Account:
    id: str
    period_id: str
    plan: str
    ceiling_micros: int


class Harness:
    def __init__(self, settings: Settings, fake: Any, clock: FakeClock, db: Database, app: Any, client: TestClient) -> None:
        self.settings = settings
        self.fake = fake
        self.clock = clock
        self.db = db
        self.app = app
        self.client = client
        self._seq = itertools.count(1)

    # ---- tokens

    def token(self, account_id: str) -> str:
        return tokens.mint_engine_token(self.settings.services_token_key, account_id, self.clock())

    def admin_token(self) -> str:
        return tokens.mint_admin_token(self.settings.services_token_key, self.clock())

    def auth(self, account: "Account | None" = None) -> dict[str, str]:
        token = self.token(account.id) if account is not None else self.admin_token()
        return {"Authorization": f"Bearer {token}"}

    # ---- seeding

    def add_account(self, plan: str = "free", *, ceiling_micros: int | None = None, status: str = "active") -> Account:
        account_id = str(uuid.uuid4())
        seq = next(self._seq)
        now = int(self.clock())
        verified = now - 3 * 86_400

        def insert(conn) -> None:
            conn.execute(
                "INSERT INTO accounts (id, tenant_seq, email, email_norm, allowance_key, email_verified_at, "
                "password_hash, password_changed_at, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)",
                (account_id, seq, f"user{seq}@example.com", f"user{seq}@example.com", f"allowance-{seq}",
                 verified, "placeholder-hash", verified, verified),
            )

        self.db.write(insert)
        plan_obj = self.settings.plans.get(plan)
        assert plan_obj is not None
        if plan == self.settings.plans.free.code:
            period_id = entitlements.current_entitlement(self.db, self.settings, account_id, now).period_id
        else:
            period_id = str(uuid.uuid4())
            self.db.write(lambda conn: conn.execute(
                "INSERT INTO entitlement_periods (id, account_id, source, subscription_id, plan_code, period_start, "
                "period_end, allowance, cost_ceiling_micros, granted_by_invoice_id, created_at) "
                "VALUES (?, ?, 'subscription', ?, ?, ?, ?, ?, ?, ?, ?)",
                (period_id, account_id, f"sub_test_{seq}", plan, now - 10 * 86_400, now + 20 * 86_400,
                 plan_obj.monthly_credits, plan_obj.cost_ceiling_micros, f"in_test_{seq}", now),
            ))
        ceiling = plan_obj.cost_ceiling_micros if ceiling_micros is None else ceiling_micros
        self.db.write(lambda conn: conn.execute(
            "UPDATE entitlement_periods SET cost_ceiling_micros = ? WHERE id = ?", (ceiling, period_id)))
        if status != "active":
            self.db.write(lambda conn: conn.execute("UPDATE accounts SET status = ? WHERE id = ?", (status, account_id)))
        return Account(account_id, period_id, plan, ceiling)

    def add_reservation(self, account: Account, *, state: str = "active", expires_in: int = 300) -> str:
        reservation_id = str(uuid.uuid4())
        now = int(self.clock())
        self.db.write(lambda conn: conn.execute(
            "INSERT INTO usage_reservations (id, account_id, idempotency_key, payload_hash, route, mode, state, "
            "period_id, credits, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (reservation_id, account.id, str(uuid.uuid4()), "0" * 64, "/api/v1/assist/stream", "chat", state,
             account.period_id, now, now + expires_in),
        ))
        return reservation_id

    def set_spend(self, account: Account, *, cost_micros: int = 0, background_micros: int = 0) -> None:
        self.db.write(lambda conn: conn.execute(
            "INSERT INTO usage_counters (account_id, period_id, cost_micros, background_micros) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (account_id, period_id) DO UPDATE SET cost_micros = excluded.cost_micros, "
            "background_micros = excluded.background_micros",
            (account.id, account.period_id, cost_micros, background_micros),
        ))

    def budget_limit(self, scope: str) -> int:
        settings = self.settings
        return {
            "global_day": settings.ai_daily_budget_micros,
            "global_month": settings.ai_monthly_budget_micros,
            "free_day": settings.ai_daily_budget_micros * settings.ai_free_trip_pct // 100,
        }[scope]

    def window(self, scope: str) -> str:
        now = int(self.clock())
        return month_key(now) if scope == "global_month" else day_key(now)

    def set_budget(self, scope: str, cost_micros: int, *, tripped_at: int | None = None) -> None:
        self.db.write(lambda conn: conn.execute(
            "INSERT INTO budget_counters (scope, window_key, cost_micros, limit_micros, tripped_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (scope, window_key) DO UPDATE SET cost_micros = excluded.cost_micros, "
            "limit_micros = excluded.limit_micros, tripped_at = excluded.tripped_at",
            (scope, self.window(scope), cost_micros, self.budget_limit(scope), tripped_at),
        ))

    def set_service_state(self, key: str, value_json: str | None) -> None:
        now = int(self.clock())
        if value_json is None:
            self.db.write(lambda conn: conn.execute("DELETE FROM service_state WHERE key = ?", (key,)))
            return
        self.db.write(lambda conn: conn.execute(
            "INSERT INTO service_state (key, value_json, updated_at) VALUES (?, ?, ?) ON CONFLICT (key) DO UPDATE SET "
            "value_json = excluded.value_json, updated_at = excluded.updated_at",
            (key, value_json, now),
        ))

    def rows(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self.db.read() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        rows = self.rows(sql, params)
        return rows[0] if rows else None

    # ---- calls

    def chat(
        self,
        account: Account | None = None,
        body: dict[str, Any] | None = None,
        *,
        request_id: str | None = None,
        background: bool = False,
        token: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        request_headers: dict[str, str] = {}
        bearer = token if token is not None else (self.token(account.id) if account is not None else None)
        if bearer is not None:
            request_headers["Authorization"] = f"Bearer {bearer}"
        if request_id is not None:
            request_headers["X-Starling-Request-Id"] = request_id
        if background:
            request_headers["X-Starling-Bucket"] = "background"
        request_headers.update(headers or {})
        return self.client.post("/v1/chat/completions", json=chat_body() if body is None else body, headers=request_headers)

    def press(self, account: Account, body: dict[str, Any] | None = None, **kwargs: Any) -> httpx.Response:
        return self.chat(account, body, request_id=self.add_reservation(account), **kwargs)


@pytest.fixture
def make_services(env):
    created: list[Harness] = []

    def factory(*, overrides: dict[str, Any] | None = None, fake: Any = None, now: float = NOW,
                **app_kwargs: Any) -> Harness:
        """`fake` is a FakeOpenAI, or a FakeAnthropic with AI_PROVIDER=anthropic in `overrides`."""
        environment = dict(env)
        environment.update({"AI_API_KEY": PRIMARY_KEY, "AI_BASE_URL": AI_BASE_URL})
        for key, value in (overrides or {}).items():
            if value is None:
                environment.pop(key, None)
            else:
                environment[key] = str(value)
        settings = load_settings(environment)
        clock = FakeClock(now)
        fake = fake or FakeOpenAI()
        options: dict[str, Any] = {
            "upstream_transport": fake.transport,
            "clock": clock,
            "monotonic": clock,
            "sleep": clock.sleep,
            "start_background": False,
        }
        options.update(app_kwargs)
        app = create_app(settings, **options)
        client = TestClient(app, base_url="http://services.test")
        client.__enter__()
        harness = Harness(settings, fake, clock, Database(settings.control_db_path), app, client)
        created.append(harness)
        return harness

    yield factory
    for harness in created:
        harness.client.__exit__(None, None, None)
        harness.db.close()


@pytest.fixture
def services(make_services) -> Harness:
    return make_services()
