"""Health, readiness, robots, sitemap, security.txt, public pages and the development outbox."""
from __future__ import annotations

import base64
import sqlite3

from fastapi.testclient import TestClient

from starling_server.app import create_app
from starling_server.config import load_settings, validate

from ..conftest import TEST_SECRET_KEY, base_env
from .helpers import context_value, create_user, post_form

HTTPS = "https://starling.example"


def production_env(tmp_path, web_dir) -> dict[str, str]:
    env = base_env(tmp_path / "production-data")
    env.update(
        {
            "STARLING_ENV": "production",
            "STARLING_PUBLIC_URL": HTTPS,
            "STARLING_ISOLATION": "os",
            "STARLING_WEB_DIR": str(web_dir),
            "STARLING_SERVICES_TOKEN_KEY": base64.urlsafe_b64encode(b"services-token-key-for-tests-32b").decode(),
            "STARLING_SUPPORT_EMAIL": "support@starling.example",
            "EMAIL_MODE": "smtp",
            "SMTP_HOST": "smtp.starling.example",
            "SMTP_FROM": "Starling <no-reply@starling.example>",
            "STRIPE_MODE": "live",
            "STRIPE_SECRET_KEY": "sk_live_fake",
            "STRIPE_WEBHOOK_SECRET": "whsec_fake",
            "STRIPE_ACCOUNT_ID": "acct_fake",
            "OPERATOR_COMPANY_NAME": "Example Ltd",
            "OPERATOR_REGISTERED_ADDRESS": "1 Example Street",
            "OPERATOR_CONTACT_EMAIL": "legal@starling.example",
            "OPERATOR_PRIVACY_CONTACT": "privacy@starling.example",
            "OPERATOR_JURISDICTION": "England and Wales",
            "OPERATOR_GOVERNING_LAW_VENUE": "London",
            "OPERATOR_EFFECTIVE_DATE": "2026-09-14",
            "HOSTING_PROVIDER": "Example Hosting",
            "EMAIL_PROVIDER": "Example Mail",
            "RETENTION_PERIODS": "Account records are deleted within 30 days of account deletion.",
            "REFUND_POLICY": "Payments are refunded in full within 14 days of the first payment.",
        }
    )
    return env


def test_production_hides_dev_routes_and_serves_production_robots(tmp_path, web_dir, client):
    settings = load_settings(production_env(tmp_path, web_dir))
    assert validate(settings, "gateway") == []
    app = create_app(settings)
    try:
        browser = TestClient(app, base_url=HTTPS)
        # Indistinguishable from a route that does not exist: the same 404 page for browsers...
        outbox = browser.get("/_dev/outbox", params={"to": "ana@example.com"})
        missing = browser.get("/_dev/not-a-route")
        assert outbox.status_code == missing.status_code == 404
        assert outbox.headers["content-type"] == missing.headers["content-type"]
        assert '<p id="template">errors/404.html</p>' in outbox.text
        assert '<p id="template">errors/404.html</p>' in missing.text
        # ...and the same JSON body for API clients.
        as_json = {"accept": "application/json"}
        outbox_json = browser.get("/_dev/outbox", params={"to": "ana@example.com"}, headers=as_json)
        missing_json = browser.get("/_dev/not-a-route", headers=as_json)
        assert outbox_json.status_code == missing_json.status_code == 404
        assert outbox_json.json() == missing_json.json()

        robots = browser.get("/robots.txt").text
        assert "Allow: /" in robots
        for path in ("/account", "/connect", "/api/", "/admin"):
            assert f"Disallow: {path}\n" in robots
        assert f"Sitemap: {HTTPS}/sitemap.xml" in robots

        assert browser.get("/readyz").status_code == 200
        app.state.config_problems = ["STRIPE_SECRET_KEY is required in production"]
        # The probe writes to the database and needs no account, so its answer is reused for a few
        # seconds (a flood of probes must not become a flood of writes). Past that window:
        assert browser.get("/readyz").status_code == 200, "still the cached answer"
        app.state.readyz_cache = None
        failing = browser.get("/readyz")
        assert failing.status_code == 503 and failing.json()["checks"]["config"] is False
        assert "STRIPE" not in failing.text
    finally:
        app.state.db.close()
    # Negative control: development serves the outbox.
    assert client.get("/_dev/outbox", params={"to": "ana@example.com"}).status_code == 200


def test_healthz_and_readyz(client):
    assert client.get("/healthz").json() == {"ok": True}
    ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json() == {"ok": True, "checks": {"database": True, "schema": True, "templates": True, "config": True}}
    assert TEST_SECRET_KEY not in ready.text


def test_readyz_fails_without_templates(client, web_dir):
    (web_dir / "templates" / "auth" / "login.html").unlink()
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"]["templates"] is False
    assert "login.html" not in response.text


def test_readyz_fails_when_the_database_cannot_be_written(app, client, monkeypatch):
    def broken(fn, timeout=30.0):
        raise sqlite3.OperationalError("attempt to write a readonly database")

    monkeypatch.setattr(app.state.db, "write", broken)
    response = client.get("/readyz")
    assert response.status_code == 503 and response.json()["checks"]["database"] is False


def test_robots_sitemap_and_security_txt_in_development(client, settings):
    assert client.get("/robots.txt").text == "User-agent: *\nDisallow: /\n"
    sitemap = client.get("/sitemap.xml")
    assert sitemap.headers["content-type"].startswith("application/xml")
    for path in ("/", "/pricing", "/privacy", "/acceptable-use", "/docs/getting-started"):
        assert f"<loc>{settings.public_url}{path}</loc>" in sitemap.text
    assert "/account" not in sitemap.text
    security = client.get("/.well-known/security.txt").text
    assert f"Contact: mailto:{settings.support_email}" in security and "Expires: " in security


def test_public_pages_render_with_the_common_context(client, fake_state):
    for path, template in [
        ("/", "index.html"),
        ("/pricing", "pricing.html"),
        ("/privacy", "legal/privacy.html"),
        ("/acceptable-use", "legal/acceptable-use.html"),
        ("/docs/getting-started", "docs/getting-started.html"),
        ("/extension/removed", "extension_removed.html"),
    ]:
        response = client.get(path)
        assert response.status_code == 200, path
        assert f'<p id="template">{template}</p>' in response.text
        assert '<p id="user">anonymous</p>' in response.text
    assert client.get("/docs/Not_A_Page").status_code == 404
    assert client.get("/docs/missing-page").status_code == 404
    assert context_value(client.get("/pricing"), "state") is None
    assert [plan["code"] for plan in context_value(client.get("/"), "plans")] == ["free", "starter", "pro"]
    rows = context_value(client.get("/subprocessors"), "subprocessors")
    assert any(row["name"] == "Stripe" and row["purpose"] == "Payments and billing" and row["data"] for row in rows)
    create_user(client, "ana@example.com")
    signed_in = client.get("/pricing")
    assert context_value(signed_in, "state")["plan"]["code"] == "free"
    assert '<p id="user">a***@example.com admin=False</p>' in signed_in.text


def test_flash_message_is_shown_once(client, fake_state):
    create_user(client, "ana@example.com")
    assert post_form(client, "/logout", {}).status_code == 303
    assert "You are signed out." in client.get("/privacy").text
    assert "You are signed out." not in client.get("/privacy").text
