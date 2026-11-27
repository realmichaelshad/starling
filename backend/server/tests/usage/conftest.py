"""Usage workstream fixtures: accounts, a fake entitlement provider and simulated services writes."""
from __future__ import annotations

import dataclasses
import itertools
import json
import uuid
from typing import Any

import pytest

from starling_server.billing import entitlements
from starling_server.billing.entitlements import Entitlement
from starling_server.config import Settings
from starling_server.db import Database
from starling_server.usage import service

NOW = 1_790_000_000
PERIOD_START = NOW - 10 * 86_400
PERIOD_END = PERIOD_START + 30 * 86_400
ROOMY_LIMITS = {"rate_per_minute": 1_000_000, "rate_per_day": 10_000_000}

_tenant_seq = itertools.count(1)


class FakeEntitlements:
    """Stands in for `billing.entitlements.current_entitlement` (owned by the billing workstream)."""

    def __init__(self) -> None:
        self.default: dict[str, Any] = {
            "plan_code": "starter",
            "plan_name": "Starter",
            "status": "active",
            "interval": "month",
            "period_start": PERIOD_START,
            "period_end": PERIOD_END,
            "allowance": 250,
            "cost_ceiling_micros": 4_500_000,
            "cancel_at": None,
            "scheduled_change": None,
            "payment_problem": False,
        }
        self.overrides: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []

    def set(self, account_id: str, **fields: Any) -> None:
        self.overrides.setdefault(account_id, {}).update(fields)

    def period_id(self, account_id: str) -> str:
        return self(None, None, account_id, record=False).period_id

    def __call__(self, db, settings, account_id: str, now: int | None = None, record: bool = True) -> Entitlement:
        if record:
            self.calls.append(account_id)
        fields = {**self.default, **self.overrides.get(account_id, {})}
        period_id = fields.pop("period_id", None) or f"{account_id}:{fields['period_start']}"
        return Entitlement(period_id=period_id, **fields)


@pytest.fixture
def fake_entitlements(monkeypatch) -> FakeEntitlements:
    fake = FakeEntitlements()
    monkeypatch.setattr(entitlements, "current_entitlement", fake)
    monkeypatch.setattr(entitlements, "entitlement_in_transaction", fake)  # same shape: (conn, settings, id, now)
    return fake


def with_plan_limits(settings: Settings, **per_plan: dict[str, Any]) -> Settings:
    by_code = dict(settings.plans.by_code)
    for code, changes in per_plan.items():
        by_code[code] = dataclasses.replace(by_code[code], **changes)
    return dataclasses.replace(settings, plans=dataclasses.replace(settings.plans, by_code=by_code))


@pytest.fixture
def roomy_settings(settings: Settings) -> Settings:
    """The real plans with rate limits out of the way (for tests about other rules)."""
    return with_plan_limits(settings, free=ROOMY_LIMITS, starter=ROOMY_LIMITS, pro=ROOMY_LIMITS)


def make_account(
    db: Database,
    settings: Settings,
    *,
    email: str | None = None,
    verified: bool = True,
    status: str = "active",
    consent: bool = True,
    consent_version: str | None = None,
    passive_learning: bool = False,
    consent_revoked: bool = False,
) -> str:
    account_id = str(uuid.uuid4())
    seq = next(_tenant_seq)
    address = email or f"user{seq}@example.com"

    def write(conn) -> None:
        conn.execute(
            "INSERT INTO accounts (id, tenant_seq, email, email_norm, allowance_key, email_verified_at, "
            "password_hash, password_changed_at, status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'x', 0, ?, ?)",
            (account_id, seq, address, address.lower(), f"ak-{seq}", NOW - 5_000 if verified else None, status, NOW - 9_000),
        )
        if consent:
            conn.execute(
                "INSERT INTO consents (account_id, kind, version, passive_learning, granted_at, revoked_at) "
                "VALUES (?, 'extension_disclosure', ?, ?, ?, ?)",
                (account_id, consent_version or settings.consent_version, int(passive_learning), NOW - 4_000,
                 NOW - 100 if consent_revoked else None),
            )

    db.write(write)
    return account_id


def body_hash(body: Any = None) -> str:
    return service.payload_hash(json.dumps({"mode": "chat", "text": "hello"} if body is None else body).encode())


def new_key() -> str:
    return str(uuid.uuid4())


def record_ai_call(db: Database, reservation_id: str, cost_micros: int = 1_000, now: int = NOW, status: str = "ok") -> None:
    """What the services proxy does per upstream AI call (CONTRACTS 4.1 metering)."""

    def write(conn) -> None:
        row = conn.execute("SELECT account_id, period_id FROM usage_reservations WHERE id = ?", (reservation_id,)).fetchone()
        conn.execute(
            "INSERT INTO ai_calls (account_id, reservation_id, bucket, lane, model, cost_micros, status, started_at, "
            "finished_at) VALUES (?, ?, 'press', 'quality', 'test-model', ?, ?, ?, ?)",
            (row["account_id"], reservation_id, cost_micros, status, now, now),
        )
        conn.execute(
            "UPDATE usage_reservations SET ai_calls = ai_calls + 1, cost_micros = cost_micros + ?, heartbeat_at = ? WHERE id = ?",
            (cost_micros, now, reservation_id),
        )
        conn.execute(
            "INSERT INTO usage_counters (account_id, period_id, cost_micros) VALUES (?, ?, ?) "
            "ON CONFLICT (account_id, period_id) DO UPDATE SET cost_micros = cost_micros + excluded.cost_micros",
            (row["account_id"], row["period_id"], cost_micros),
        )

    db.write(write)


def counters(db: Database, account_id: str, period_id: str) -> dict[str, int]:
    with db.read() as conn:
        row = conn.execute("SELECT * FROM usage_counters WHERE account_id = ? AND period_id = ?", (account_id, period_id)).fetchone()
    names = ("committed", "reserved", "granted", "cost_micros", "background_micros", "released_with_spend")
    return {name: 0 for name in names} if row is None else {name: int(row[name]) for name in names}


def reservation(db: Database, reservation_id: str) -> dict[str, Any]:
    with db.read() as conn:
        row = conn.execute("SELECT * FROM usage_reservations WHERE id = ?", (reservation_id,)).fetchone()
    return dict(row) if row is not None else {}


def ledger(db: Database, account_id: str, kind: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM credit_ledger WHERE account_id = ?" + (" AND kind = ?" if kind else "") + " ORDER BY id"
    with db.read() as conn:
        return [dict(row) for row in conn.execute(sql, (account_id, kind) if kind else (account_id,))]


def set_service_state(db: Database, key: str, value: Any) -> None:
    db.write(lambda conn: conn.execute(
        "INSERT INTO service_state (key, value_json, updated_at) VALUES (?, ?, 0) "
        "ON CONFLICT (key) DO UPDATE SET value_json = excluded.value_json",
        (key, value if isinstance(value, str) else json.dumps(value)),
    ))


def set_counter(db: Database, account_id: str, period_id: str, **values: int) -> None:
    columns = ", ".join(values)
    updates = ", ".join(f"{name} = excluded.{name}" for name in values)
    db.write(lambda conn: conn.execute(
        f"INSERT INTO usage_counters (account_id, period_id, {columns}) VALUES (?, ?, {', '.join('?' * len(values))}) "
        f"ON CONFLICT (account_id, period_id) DO UPDATE SET {updates}",
        (account_id, period_id, *values.values()),
    ))
