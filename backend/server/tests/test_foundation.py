"""Foundation guarantees every other workstream relies on."""
from __future__ import annotations

import sqlite3
import threading
from datetime import date

import pytest

from starling_server.config import ConfigError, load_settings, validate
from starling_server.db import Database, migrations
from starling_server.db.connection import connect

from .conftest import base_env


def test_development_defaults_load(settings):
    assert settings.env == "development"
    assert settings.isolation == "none"
    assert settings.email_mode == "outbox"
    assert settings.ai_model_quality == "gpt-5.6-luna"
    assert settings.ai_reasoning_effort == "none"
    assert settings.stripe_api_version == "2026-08-26.dahlia"
    assert settings.control_db_path.name == "control.db"


def test_production_refuses_unsafe_configuration(tmp_path):
    env = base_env(tmp_path / "data")
    env.update({"STARLING_ENV": "production", "STARLING_ISOLATION": "none", "EMAIL_MODE": "outbox"})
    env.pop("STARLING_SECRET_KEY")
    settings = load_settings(env)
    problems = "\n".join(validate(settings, "gateway"))
    assert "https" in problems
    assert "STARLING_ISOLATION" in problems
    assert "STARLING_SECRET_KEY" in problems
    assert "EMAIL_MODE" in problems
    assert "placeholders" in problems
    assert "STRIPE_SECRET_KEY" in problems


def test_web_search_settings(tmp_path):
    settings = load_settings(base_env(tmp_path / "data"))
    assert settings.web_search_enabled is True
    assert settings.web_search_model == settings.ai_model_fast  # follows the fast model unless set
    assert settings.web_search_context_size == "low"
    assert settings.web_search_per_account_day == 20 and settings.web_search_timeout_s == 8.0
    env = base_env(tmp_path / "data")
    env.update({"WEB_SEARCH": "false", "WEB_SEARCH_PER_ACCOUNT_DAY": "0"})
    off = load_settings(env)
    assert off.web_search_enabled is False and off.web_search_per_account_day == 0
    env = base_env(tmp_path / "data")
    env["WEB_SEARCH_CONTEXT_SIZE"] = "huge"
    with pytest.raises(ConfigError):
        load_settings(env)
    env = base_env(tmp_path / "data")
    env["WEB_SEARCH_MODEL"] = "a-model-with-no-price"
    assert any("a-model-with-no-price" in p for p in validate(load_settings(env), "services"))
    env["WEB_SEARCH"] = "false"  # an unpriced search model matters only while search is on
    assert not any("a-model-with-no-price" in p for p in validate(load_settings(env), "services"))
    env = base_env(tmp_path / "data")
    env["WEB_SEARCH_TIMEOUT_S"] = "0"
    assert "WEB_SEARCH_TIMEOUT_S must be above 0" in validate(load_settings(env), "services")


def test_stripe_key_must_match_mode(tmp_path):
    env = base_env(tmp_path / "data")
    env.update({"STRIPE_MODE": "test", "STRIPE_SECRET_KEY": "sk_live_" + "x" * 24, "STRIPE_WEBHOOK_SECRET": "whsec_" + "y" * 24})
    problems = validate(load_settings(env), "gateway")
    assert any("not a test key" in p for p in problems)


def test_bad_values_are_rejected(tmp_path):
    env = base_env(tmp_path / "data")
    env["STARLING_ENV"] = "staging"
    with pytest.raises(ConfigError):
        load_settings(env)
    env = base_env(tmp_path / "data")
    env["STARLING_SECRET_KEY"] = "c2hvcnQ"  # "short"
    with pytest.raises(ConfigError):
        load_settings(env)


def test_plans_file_is_consistent(settings):
    plans = settings.plans
    assert [p.code for p in plans.public_plans()] == ["free", "starter", "pro"]
    assert plans.free.monthly_credits == 25 and not plans.free.is_paid
    starter = plans.by_lookup_key("starling_starter_monthly_usd")
    assert starter is not None and starter.amount_cents == 900 and starter.monthly_credits == 250
    # Fair-use ceilings stay below plan revenue.
    for plan in plans.paid_plans():
        assert plan.cost_ceiling_micros < plan.amount_cents * 10_000


def test_ai_price_rounds_up(settings):
    price = settings.ai_pricing.price_for("gpt-5.6-luna", date(2026, 9, 14))
    assert price is not None
    # 13,000 input (1,000 cached) + 800 output tokens:
    # 12,000 x $0.20/M + 1,000 x $0.02/M + 800 x $1.20/M = $0.00338
    assert price.cost_micros(13_000, 1_000, 800) == 3_380
    assert price.cost_micros(1, 0, 0) == 1  # never rounds spend down to zero
    assert settings.ai_pricing.price_for("gpt-5.6-luna", date(2020, 1, 1)) is None


def test_migrations_are_idempotent(tmp_path):
    path = tmp_path / "control.db"
    conn = connect(path)
    try:
        assert migrations.apply(conn) == migrations.latest_version()
        assert migrations.apply(conn) == migrations.latest_version()
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    for table in ("accounts", "usage_reservations", "credit_ledger", "billing_events", "ai_calls", "devices", "request_errors"):
        assert table in tables


def test_request_errors_aggregate_per_minute_and_prune(database):
    from starling_server.db import request_errors

    base = 1_757_800_040  # 20 seconds into a minute
    assert base % 60 == 20
    request_errors.record(database, route="/api/v1/assist", code="limit_reached", http_status=402, now=base, account_id="a1")
    request_errors.record(database, route="/api/v1/assist", code="limit_reached", http_status=402, now=base + 30, account_id="a1")
    request_errors.record(database, route="/api/v1/assist", code="busy", http_status=409, now=base + 31, account_id="a1")
    request_errors.record(database, route="/api/v1/assist", code="limit_reached", http_status=402, now=base + 60, account_id="a1")
    request_errors.record(database, route="/api/v1/assist/stream", code="ai_unavailable", http_status=503, now=base,
                          mode="chat", reserved=True)
    database.write(lambda conn: None)  # the writer is FIFO: wait for the queued records
    with database.read() as conn:
        rows = conn.execute(
            "SELECT minute, account_id, route, mode, code, reserved, count, first_at, last_at FROM request_errors "
            "ORDER BY route, minute, code"
        ).fetchall()
    assert [tuple(r) for r in rows] == [
        (base - 20, "a1", "/api/v1/assist", "", "busy", 0, 1, base + 31, base + 31),
        (base - 20, "a1", "/api/v1/assist", "", "limit_reached", 0, 2, base, base + 30),
        (base + 40, "a1", "/api/v1/assist", "", "limit_reached", 0, 1, base + 60, base + 60),
        (base - 20, "", "/api/v1/assist/stream", "chat", "ai_unavailable", 1, 1, base, base),
    ]
    removed = database.write(lambda conn: request_errors.prune(conn, base + request_errors.RETENTION_S + 45))
    assert removed == 3  # only the row last seen at base + 60 is inside the window
    with database.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM request_errors").fetchone()[0] == 1


def test_writer_rolls_back_failed_transactions(database):
    def insert_then_fail(conn):
        conn.execute("INSERT INTO service_state (key, value_json, updated_at) VALUES ('k', '1', 0)")
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        database.write(insert_then_fail)
    with database.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM service_state").fetchone()[0] == 0


def test_writer_serialises_concurrent_increments(database):
    database.write(lambda c: c.execute("INSERT INTO service_state (key, value_json, updated_at) VALUES ('n', '0', 0)"))

    def increment(conn):
        value = int(conn.execute("SELECT value_json FROM service_state WHERE key='n'").fetchone()[0])
        conn.execute("UPDATE service_state SET value_json=? WHERE key='n'", (str(value + 1),))

    threads = [threading.Thread(target=lambda: database.write(increment)) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with database.read() as conn:
        assert conn.execute("SELECT value_json FROM service_state WHERE key='n'").fetchone()[0] == "50"


def test_strict_tables_reject_wrong_types(database):
    with pytest.raises(sqlite3.IntegrityError):
        database.write(lambda c: c.execute(
            "INSERT INTO service_state (key, value_json, updated_at) VALUES ('t', 'v', 'not-an-int')"))


def test_services_token_key_is_shared_in_development_and_required_in_production(tmp_path):
    import hashlib
    import hmac

    dev = load_settings(base_env(tmp_path / "data"))
    expected = hmac.new(dev.secret_key, b"starling-services-token-v1", hashlib.sha256).digest()
    assert dev.services_token_key == expected and not dev.services_token_key_ephemeral
    assert dev.services_url == "http://127.0.0.1:8766"

    env = base_env(tmp_path / "data")
    env.update({"STARLING_ENV": "production", "STARLING_ISOLATION": "os"})
    prod = load_settings(env)
    assert prod.services_token_key == b""
    assert prod.services_url == "unix:/run/starling/services.sock"
    assert any("STARLING_SERVICES_TOKEN_KEY" in p for p in validate(prod, "services"))


def test_app_starts_with_security_headers(client):
    response = client.get("/definitely-not-a-route")
    assert response.status_code == 404
    assert response.headers.get("x-request-id")
    assert response.headers.get("x-content-type-options") == "nosniff"
    assert response.headers.get("x-frame-options") == "DENY"


def test_business_details_are_read_from_the_operator_settings_and_their_old_names(tmp_path):
    """The seven settings were `BUYER_*` before the project was opened. A deployment configured
    then must still start, and the new name wins where both are set."""
    unset = load_settings(base_env(tmp_path / "data")).legal
    assert unset.company_name == "[[OPERATOR_COMPANY_NAME]]" and "company_name" in unset.placeholders()

    env = dict(base_env(tmp_path / "data"), BUYER_COMPANY_NAME="Old Name Ltd", BUYER_JURISDICTION="England and Wales",
               OPERATOR_COMPANY_NAME="  New Name Ltd  ", OPERATOR_EFFECTIVE_DATE="")
    legal = load_settings(env).legal
    assert legal.company_name == "New Name Ltd"
    assert legal.jurisdiction == "England and Wales"
    assert legal.effective_date == "[[OPERATOR_EFFECTIVE_DATE]]", "an empty value is still a missing one"
    assert {"company_name", "jurisdiction"}.isdisjoint(legal.placeholders())


def test_production_requires_business_and_support_details(tmp_path):
    env = base_env(tmp_path / "data")
    env.update({
        "STARLING_ENV": "production",
        "STARLING_SUPPORT_EMAIL": "[[OPERATOR_CONTACT_EMAIL]]",
        "STARLING_EXTENSION_INSTALL_URL": "http://store.example/detail/starling",
    })
    settings = load_settings(env)
    assert {"retention_periods", "refund_policy"} <= set(settings.legal.placeholders())
    problems = "\n".join(validate(settings, "gateway"))
    assert "STARLING_SUPPORT_EMAIL" in problems
    assert "STARLING_EXTENSION_INSTALL_URL" in problems


def test_app_uses_the_website_template_environment(app):
    templates = app.state.templates
    assert templates is not None, "web/templates and web/templating.py must be installed"
    env = templates.env
    assert {"money", "date", "usd_micros", "placeholders"} <= set(env.filters)
    assert {"static", "usage_percent", "legal_values"} <= set(env.globals)
    names = env.list_templates(extensions=["html"])
    assert "index.html" in names
    for name in names:
        env.get_template(name)  # a plain Jinja environment cannot compile these


def test_supervisor_requires_backup_passphrase_and_a_safe_uid_base(tmp_path):
    env = base_env(tmp_path / "data")
    env.update({"STARLING_ENV": "production", "STARLING_ISOLATION": "os", "BACKUP_PASSPHRASE": "short"})
    assert any("BACKUP_PASSPHRASE" in p for p in validate(load_settings(env), "supervisor"))
    env["BACKUP_PASSPHRASE"] = "a passphrase long enough"
    assert not any("BACKUP_PASSPHRASE" in p for p in validate(load_settings(env), "supervisor"))
    env["ENGINE_UID_BASE"] = "10001"  # engines would share uids with the gateway
    with pytest.raises(ConfigError):
        load_settings(env)


def test_production_refuses_the_e2e_endpoints_and_switches(tmp_path):
    env = base_env(tmp_path / "data")
    env.update({
        "STARLING_ENV": "production",
        "STARLING_ISOLATION": "os",
        "STRIPE_API_BASE": "http://127.0.0.1:12111",
        "AI_BASE_URL": "http://127.0.0.1:12112/v1",
        "STARLING_WORLD_OFFLINE": "true",
    })
    settings = load_settings(env)
    assert settings.world_offline is True
    gateway = "\n".join(validate(settings, "gateway"))
    services = "\n".join(validate(settings, "services"))
    assert "STRIPE_API_BASE" in gateway and "STARLING_WORLD_OFFLINE" in gateway
    assert "AI_BASE_URL" in services and "STARLING_WORLD_OFFLINE" in services
    development = load_settings(dict(base_env(tmp_path / "dev"), STARLING_WORLD_OFFLINE="true",
                                     STRIPE_API_BASE="http://127.0.0.1:12111"))
    assert not any("STRIPE_API_BASE" in p or "STARLING_WORLD_OFFLINE" in p for p in validate(development, "gateway"))


def test_api_refusals_are_shaped_and_counted(app, client):
    refused = client.get("/api/v1/session")
    assert refused.status_code == 401
    assert refused.json()["error"]["code"] == "signed_out"
    unknown = client.get("/api/v1/definitely-not-a-route")
    assert unknown.status_code == 404
    assert unknown.json() == {"error": {"code": "not_found", "message": "Not found."}}
    page = client.get("/definitely-not-a-page", headers={"accept": "text/html"})
    assert page.status_code == 404 and page.headers["content-type"].startswith("text/html")
    db = app.state.db
    db.write(lambda conn: None)  # the writer is FIFO: wait for the queued records
    with db.read() as conn:
        rows = [tuple(r) for r in conn.execute(
            "SELECT route, code, http_status, account_id, mode, reserved, count FROM request_errors")]
    assert rows == [("/api/v1/session", "signed_out", 401, "", "", 0, 1)]


def test_gateway_json_logs_carry_no_traceback_or_exception_text():
    import io
    import json as json_module
    import logging

    from starling_server import logs

    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    stream = io.StringIO()
    try:
        logs.configure("INFO", stream)
        logger = logging.getLogger("starling_server.foundation_test")
        try:
            raise ValueError("a customer's draft text")
        except ValueError:
            logger.exception("engine request failed")
        line = stream.getvalue().strip()
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)
        logging.getLogger("uvicorn.access").disabled = False

    record = json_module.loads(line)
    assert record["level"] == "error" and record["logger"] == "starling_server.foundation_test"
    assert record["message"] == "engine request failed" and record["error"] == "ValueError"
    assert record["where"].startswith("test_foundation.py:")
    assert "a customer's draft text" not in line and "Traceback" not in line
