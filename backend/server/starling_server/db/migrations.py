"""Control database schema.

Migrations are append-only: never edit a released migration, add a new one. Each runs
inside its own transaction and is recorded in `schema_migrations`. See
docs/architecture/CONTRACTS.md section 11 for the meaning of every table.
"""
from __future__ import annotations

import sqlite3
import time

MIGRATION_1 = """
CREATE TABLE accounts (
    id                  TEXT PRIMARY KEY,
    tenant_seq          INTEGER NOT NULL UNIQUE,
    email               TEXT NOT NULL,
    email_norm          TEXT NOT NULL UNIQUE,
    allowance_key       TEXT NOT NULL,
    email_verified_at   INTEGER,
    password_hash       TEXT NOT NULL,
    password_changed_at INTEGER NOT NULL,
    auth_epoch          INTEGER NOT NULL DEFAULT 1,
    status              TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'suspended', 'deleting', 'deleted')),
    is_admin            INTEGER NOT NULL DEFAULT 0 CHECK (is_admin IN (0, 1)),
    totp_secret_enc     TEXT,
    totp_enabled_at     INTEGER,
    signup_ip_prefix    TEXT,
    created_at          INTEGER NOT NULL,
    deleted_at          INTEGER
) STRICT;
CREATE INDEX accounts_allowance_key ON accounts (allowance_key);

CREATE TABLE email_tokens (
    id          TEXT PRIMARY KEY,
    account_id  TEXT NOT NULL REFERENCES accounts (id),
    purpose     TEXT NOT NULL CHECK (purpose IN ('verify', 'reset', 'email_change')),
    token_hash  TEXT NOT NULL UNIQUE,
    new_email   TEXT,
    created_at  INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL,
    used_at     INTEGER
) STRICT;
CREATE INDEX email_tokens_account ON email_tokens (account_id, purpose);

CREATE TABLE web_sessions (
    id_hash              TEXT PRIMARY KEY,
    account_id           TEXT REFERENCES accounts (id),
    auth_epoch           INTEGER NOT NULL DEFAULT 0,
    csrf_secret          TEXT NOT NULL,
    created_at           INTEGER NOT NULL,
    last_seen_at         INTEGER NOT NULL,
    idle_expires_at      INTEGER NOT NULL,
    abs_expires_at       INTEGER NOT NULL,
    admin_elevated_until INTEGER,
    ip_prefix            TEXT,
    user_agent           TEXT,
    revoked_at           INTEGER
) STRICT;
CREATE INDEX web_sessions_account ON web_sessions (account_id);
CREATE INDEX web_sessions_expiry ON web_sessions (abs_expires_at);

CREATE TABLE rate_limits (
    bucket       TEXT NOT NULL,
    key          TEXT NOT NULL,
    window_start INTEGER NOT NULL,
    count        INTEGER NOT NULL,
    PRIMARY KEY (bucket, key, window_start)
) STRICT;

CREATE TABLE connect_requests (
    id             TEXT PRIMARY KEY,
    account_id     TEXT REFERENCES accounts (id),
    code_challenge TEXT NOT NULL,
    state_hash     TEXT NOT NULL,
    extension_id   TEXT NOT NULL,
    redirect_uri   TEXT NOT NULL,
    device_label   TEXT NOT NULL,
    code_hash      TEXT UNIQUE,
    created_at     INTEGER NOT NULL,
    approved_at    INTEGER,
    expires_at     INTEGER NOT NULL,
    redeemed_at    INTEGER
) STRICT;
CREATE INDEX connect_requests_expiry ON connect_requests (expires_at);

CREATE TABLE devices (
    id                TEXT PRIMARY KEY,
    account_id        TEXT NOT NULL REFERENCES accounts (id),
    token_hash        TEXT NOT NULL UNIQUE,
    token_prefix      TEXT NOT NULL,
    label             TEXT NOT NULL,
    extension_id      TEXT NOT NULL,
    extension_version TEXT,
    auth_epoch        INTEGER NOT NULL,
    created_at        INTEGER NOT NULL,
    last_used_at      INTEGER,
    idle_expires_at   INTEGER NOT NULL,
    abs_expires_at    INTEGER NOT NULL,
    revoked_at        INTEGER,
    revoked_reason    TEXT
) STRICT;
CREATE INDEX devices_account ON devices (account_id, revoked_at);

CREATE TABLE consents (
    account_id       TEXT NOT NULL REFERENCES accounts (id),
    kind             TEXT NOT NULL CHECK (kind IN ('tos', 'privacy', 'extension_disclosure', 'support_access')),
    version          TEXT NOT NULL,
    passive_learning INTEGER NOT NULL DEFAULT 0 CHECK (passive_learning IN (0, 1)),
    granted_at       INTEGER NOT NULL,
    revoked_at       INTEGER,
    PRIMARY KEY (account_id, kind, version)
) STRICT;

CREATE TABLE stripe_customers (
    account_id  TEXT PRIMARY KEY REFERENCES accounts (id),
    customer_id TEXT NOT NULL UNIQUE,
    livemode    INTEGER NOT NULL CHECK (livemode IN (0, 1)),
    created_at  INTEGER NOT NULL,
    deleted_at  INTEGER
) STRICT;

CREATE TABLE checkout_sessions (
    session_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts (id),
    price_id   TEXT NOT NULL,
    status     TEXT NOT NULL CHECK (status IN ('open', 'complete', 'expired')),
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
) STRICT;
CREATE INDEX checkout_sessions_account ON checkout_sessions (account_id, status);

CREATE TABLE price_map (
    price_id    TEXT PRIMARY KEY,
    lookup_key  TEXT,
    plan_code   TEXT NOT NULL,
    interval    TEXT NOT NULL,
    currency    TEXT NOT NULL,
    unit_amount INTEGER NOT NULL,
    active      INTEGER NOT NULL CHECK (active IN (0, 1)),
    livemode    INTEGER NOT NULL CHECK (livemode IN (0, 1))
) STRICT;
CREATE INDEX price_map_lookup ON price_map (lookup_key);

CREATE TABLE subscriptions (
    subscription_id         TEXT PRIMARY KEY,
    account_id              TEXT NOT NULL REFERENCES accounts (id),
    customer_id             TEXT NOT NULL,
    status                  TEXT NOT NULL,
    price_id                TEXT,
    plan_code               TEXT,
    current_period_start    INTEGER,
    current_period_end      INTEGER,
    cancel_at_period_end    INTEGER NOT NULL DEFAULT 0 CHECK (cancel_at_period_end IN (0, 1)),
    cancel_at               INTEGER,
    canceled_at             INTEGER,
    ended_at                INTEGER,
    schedule_id             TEXT,
    scheduled_price_id      TEXT,
    pending_update          INTEGER NOT NULL DEFAULT 0 CHECK (pending_update IN (0, 1)),
    latest_invoice_id       TEXT,
    latest_invoice_status   TEXT,
    first_payment_failed_at INTEGER,
    disputed_at             INTEGER,
    livemode                INTEGER NOT NULL CHECK (livemode IN (0, 1)),
    synced_at               INTEGER NOT NULL
) STRICT;
CREATE INDEX subscriptions_account ON subscriptions (account_id, status);
CREATE INDEX subscriptions_customer ON subscriptions (customer_id);

CREATE TABLE entitlement_periods (
    id                    TEXT PRIMARY KEY,
    account_id            TEXT NOT NULL REFERENCES accounts (id),
    source                TEXT NOT NULL CHECK (source IN ('free', 'subscription', 'grant')),
    subscription_id       TEXT,
    plan_code             TEXT NOT NULL,
    period_start          INTEGER NOT NULL,
    period_end            INTEGER NOT NULL,
    allowance             INTEGER NOT NULL,
    cost_ceiling_micros   INTEGER NOT NULL,
    granted_by_invoice_id TEXT UNIQUE,
    created_at            INTEGER NOT NULL,
    UNIQUE (account_id, source, period_start)
) STRICT;
CREATE INDEX entitlement_periods_account ON entitlement_periods (account_id, period_end);

CREATE TABLE usage_reservations (
    id                TEXT PRIMARY KEY,
    account_id        TEXT NOT NULL REFERENCES accounts (id),
    idempotency_key   TEXT NOT NULL,
    payload_hash      TEXT NOT NULL,
    route             TEXT NOT NULL,
    mode              TEXT NOT NULL,
    state             TEXT NOT NULL
                      CHECK (state IN ('reserved', 'active', 'committed', 'released', 'expired')),
    period_id         TEXT,
    credits           INTEGER NOT NULL,
    exposure_at       INTEGER,
    ai_calls          INTEGER NOT NULL DEFAULT 0,
    cost_micros       INTEGER NOT NULL DEFAULT 0,
    result_json       TEXT,
    result_expires_at INTEGER,
    created_at        INTEGER NOT NULL,
    heartbeat_at      INTEGER,
    expires_at        INTEGER NOT NULL,
    finished_at       INTEGER,
    release_reason    TEXT,
    UNIQUE (account_id, idempotency_key)
) STRICT;
CREATE INDEX usage_reservations_state ON usage_reservations (state, expires_at);
CREATE INDEX usage_reservations_account ON usage_reservations (account_id, state);

CREATE TABLE credit_ledger (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id     TEXT NOT NULL REFERENCES accounts (id),
    period_id      TEXT,
    kind           TEXT NOT NULL
                   CHECK (kind IN ('commit', 'admin_grant', 'admin_debit', 'upgrade_proration', 'refund_adjust')),
    amount         INTEGER NOT NULL,
    reservation_id TEXT,
    actor          TEXT,
    reason         TEXT,
    created_at     INTEGER NOT NULL
) STRICT;
CREATE UNIQUE INDEX credit_ledger_one_commit ON credit_ledger (reservation_id) WHERE kind = 'commit';
CREATE INDEX credit_ledger_account ON credit_ledger (account_id, period_id);

CREATE TABLE usage_counters (
    account_id          TEXT NOT NULL REFERENCES accounts (id),
    period_id           TEXT NOT NULL,
    committed           INTEGER NOT NULL DEFAULT 0,
    reserved            INTEGER NOT NULL DEFAULT 0,
    granted             INTEGER NOT NULL DEFAULT 0,
    cost_micros         INTEGER NOT NULL DEFAULT 0,
    background_micros   INTEGER NOT NULL DEFAULT 0,
    released_with_spend INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, period_id)
) STRICT;

CREATE TABLE ai_calls (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id          TEXT,
    reservation_id      TEXT,
    bucket              TEXT NOT NULL CHECK (bucket IN ('press', 'background', 'system', 'admin')),
    lane                TEXT NOT NULL,
    model               TEXT NOT NULL,
    input_tokens        INTEGER NOT NULL DEFAULT 0,
    cached_input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens       INTEGER NOT NULL DEFAULT 0,
    estimated           INTEGER NOT NULL DEFAULT 0 CHECK (estimated IN (0, 1)),
    cost_micros         INTEGER NOT NULL DEFAULT 0,
    status              TEXT NOT NULL CHECK (status IN ('ok', 'error', 'cancelled', 'refused')),
    error_code          TEXT,
    started_at          INTEGER NOT NULL,
    finished_at         INTEGER
) STRICT;
CREATE INDEX ai_calls_account ON ai_calls (account_id, started_at);
CREATE INDEX ai_calls_reservation ON ai_calls (reservation_id);
CREATE INDEX ai_calls_started ON ai_calls (started_at);

CREATE TABLE budget_counters (
    scope        TEXT NOT NULL CHECK (scope IN ('global_day', 'global_month', 'free_day')),
    window_key   TEXT NOT NULL,
    cost_micros  INTEGER NOT NULL DEFAULT 0,
    limit_micros INTEGER NOT NULL,
    tripped_at   INTEGER,
    PRIMARY KEY (scope, window_key)
) STRICT;

CREATE TABLE billing_events (
    event_id     TEXT PRIMARY KEY,
    type         TEXT NOT NULL,
    livemode     INTEGER NOT NULL CHECK (livemode IN (0, 1)),
    object_id    TEXT,
    account_id   TEXT,
    payload_json TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('received', 'processing', 'processed', 'ignored', 'failed')),
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    received_at  INTEGER NOT NULL,
    processed_at INTEGER
) STRICT;
CREATE INDEX billing_events_status ON billing_events (status, received_at);
CREATE INDEX billing_events_object ON billing_events (object_id, type);

CREATE TABLE admin_audit (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id          TEXT NOT NULL,
    action            TEXT NOT NULL,
    target_account_id TEXT,
    params_json       TEXT,
    ip_prefix         TEXT,
    created_at        INTEGER NOT NULL
) STRICT;
CREATE INDEX admin_audit_created ON admin_audit (created_at);
CREATE INDEX admin_audit_target ON admin_audit (target_account_id);

CREATE TABLE email_outbox (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    to_email   TEXT NOT NULL,
    template   TEXT NOT NULL,
    subject    TEXT NOT NULL,
    body       TEXT,
    created_at INTEGER NOT NULL,
    sent_at    INTEGER,
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
) STRICT;
CREATE INDEX email_outbox_pending ON email_outbox (sent_at);

CREATE TABLE engines (
    account_id   TEXT PRIMARY KEY REFERENCES accounts (id),
    tenant_seq   INTEGER NOT NULL,
    pid          INTEGER,
    listen       TEXT,
    state        TEXT NOT NULL,
    started_at   INTEGER,
    last_used_at INTEGER,
    rss_mb       INTEGER,
    restarts     INTEGER NOT NULL DEFAULT 0
) STRICT;
CREATE INDEX engines_state ON engines (state, last_used_at);

CREATE TABLE tombstones (
    account_hash TEXT PRIMARY KEY,
    customer_id  TEXT UNIQUE,
    deleted_at   INTEGER NOT NULL
) STRICT;

CREATE TABLE service_state (
    key        TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at INTEGER NOT NULL
) STRICT;
"""

MIGRATION_2 = """
CREATE INDEX usage_reservations_account_created ON usage_reservations (account_id, created_at);
CREATE INDEX usage_reservations_result_expiry ON usage_reservations (result_expires_at)
    WHERE result_json IS NOT NULL;
"""

# Refusals and failures of the extension API, aggregated per minute (db/request_errors.py).
MIGRATION_3 = """
CREATE TABLE request_errors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    minute      INTEGER NOT NULL,
    account_id  TEXT NOT NULL DEFAULT '',
    route       TEXT NOT NULL,
    mode        TEXT NOT NULL DEFAULT '',
    code        TEXT NOT NULL,
    http_status INTEGER NOT NULL,
    reserved    INTEGER NOT NULL DEFAULT 0 CHECK (reserved IN (0, 1)),
    count       INTEGER NOT NULL DEFAULT 1,
    first_at    INTEGER NOT NULL,
    last_at     INTEGER NOT NULL,
    UNIQUE (minute, account_id, route, mode, code, http_status, reserved)
) STRICT;
CREATE INDEX request_errors_last ON request_errors (last_at);
CREATE INDEX request_errors_account ON request_errors (account_id, last_at);
"""

# Sign-in identities from other providers (auth/identities.py). One Google account per Starling
# account; the provider's stable subject id is the key, never the email address, which can change.
MIGRATION_4 = """
CREATE TABLE account_identities (
    provider     TEXT NOT NULL CHECK (provider IN ('google')),
    subject      TEXT NOT NULL,
    account_id   TEXT NOT NULL REFERENCES accounts (id),
    email        TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    last_used_at INTEGER NOT NULL,
    PRIMARY KEY (provider, subject),
    UNIQUE (account_id, provider)
) STRICT;
"""

# Sign-up confirmation that signs the person in (auth/service.py `confirm_signup_code`): the email
# carries a 6-digit code as well as the link, and both are tied to the browser that signed up.
# `browser_hash` is the SHA-256 of a random value kept in that browser's notice cookie; `code_hash`
# an HMAC of the code (a DB copy alone cannot reveal it); `new_password_hash` the password typed
# when an address with an unconfirmed account signs up again, applied only if that same browser
# confirms.
MIGRATION_5 = """
ALTER TABLE email_tokens ADD COLUMN browser_hash TEXT;
ALTER TABLE email_tokens ADD COLUMN code_hash TEXT;
ALTER TABLE email_tokens ADD COLUMN code_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE email_tokens ADD COLUMN code_used_at INTEGER;
ALTER TABLE email_tokens ADD COLUMN new_password_hash TEXT;
CREATE INDEX email_tokens_browser ON email_tokens (browser_hash) WHERE browser_hash IS NOT NULL;
"""

# The website's writing page for phones (web/write.py) records its own disclosure, kind
# `web_disclosure`, beside the extension's. SQLite cannot change a CHECK constraint in place, so the
# table is rebuilt with the same columns and rows. Nothing references `consents`.
MIGRATION_6 = """
CREATE TABLE consents_v6 (
    account_id       TEXT NOT NULL REFERENCES accounts (id),
    kind             TEXT NOT NULL
                     CHECK (kind IN ('tos', 'privacy', 'extension_disclosure', 'support_access', 'web_disclosure')),
    version          TEXT NOT NULL,
    passive_learning INTEGER NOT NULL DEFAULT 0 CHECK (passive_learning IN (0, 1)),
    granted_at       INTEGER NOT NULL,
    revoked_at       INTEGER,
    PRIMARY KEY (account_id, kind, version)
) STRICT;
INSERT INTO consents_v6 (account_id, kind, version, passive_learning, granted_at, revoked_at)
    SELECT account_id, kind, version, passive_learning, granted_at, revoked_at FROM consents;
DROP TABLE consents;
ALTER TABLE consents_v6 RENAME TO consents;
"""

MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, MIGRATION_1),
    (2, MIGRATION_2),
    (3, MIGRATION_3),
    (4, MIGRATION_4),
    (5, MIGRATION_5),
    (6, MIGRATION_6),
)


def current_version(conn: sqlite3.Connection) -> int:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL) STRICT"
    )
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return int(row[0] or 0)


def apply(conn: sqlite3.Connection) -> int:
    """Apply pending migrations; return the resulting schema version."""
    version = current_version(conn)
    for number, sql in MIGRATIONS:
        if number <= version:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in _statements(sql):
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (number, int(time.time())),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        version = number
    return version


def latest_version() -> int:
    return MIGRATIONS[-1][0]


def _statements(sql: str) -> list[str]:
    """Split a migration into statements (the schema contains no semicolons in strings)."""
    return [part.strip() for part in sql.split(";") if part.strip()]
