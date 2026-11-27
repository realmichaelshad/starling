"""Smaller guarantees: rehash on login, verified-email gate, and the token endpoint rate limit."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.testclient import TestClient

from starling_server.account.routes import user_agent_summary
from starling_server.auth import passwords
from starling_server.auth.deps import CurrentUser, require_verified_user
from starling_server.security import ratelimit
from starling_server.security.errors import StarlingRoute

from ..conftest import PUBLIC_URL
from .helpers import PASSWORD, account_row, create_user, login, pkce_pair, redeem, signup, verify

VERIFIED_PROBE = "/_test/verified-probe"


def test_login_rehashes_passwords_with_outdated_parameters(app, client, settings):
    assert signup(client, "ana@example.com").status_code == 303
    outdated = passwords._hasher(2, 8192, 1).hash(PASSWORD)
    app.state.db.write(lambda c: c.execute("UPDATE accounts SET password_hash = ? WHERE email_norm = 'ana@example.com'", (outdated,)))
    assert login(client, "ana@example.com").status_code == 303
    upgraded = account_row(app, "ana@example.com")["password_hash"]
    assert upgraded != outdated and not passwords.hasher_for(settings).check_needs_rehash(upgraded)
    assert login(client, "ana@example.com").status_code == 303  # the new hash still verifies


def test_verified_email_gate(app, client):
    async def probe(user: CurrentUser = Depends(require_verified_user)):
        return {"account": user.account_id}

    app.add_api_route(VERIFIED_PROBE, probe, methods=["GET"])
    create_user(client, "ana@example.com", verified=False)
    refused = client.get(VERIFIED_PROBE, headers={"Accept": "application/json"}, follow_redirects=False)
    assert refused.status_code == 403
    assert verify(client, "ana@example.com").status_code == 303
    assert client.get(VERIFIED_PROBE, headers={"Accept": "application/json"}).status_code == 200


def test_error_pages_use_templates_with_a_plain_text_fallback(app, client, web_dir):
    missing = client.get("/docs/missing-page")
    assert missing.status_code == 404 and '<p id="template">errors/404.html</p>' in missing.text

    router = APIRouter(route_class=StarlingRoute)

    @router.get("/_test/boom")
    async def boom():
        raise RuntimeError("unexpected failure")

    @router.get("/api/v1/_test/boom")
    async def api_boom():
        raise RuntimeError("unexpected failure")

    app.include_router(router)
    browser = TestClient(app, base_url=PUBLIC_URL, raise_server_exceptions=False)
    server_error = web_dir / "templates" / "errors" / "500.html"
    server_error.write_text('<p id="template">errors/500.html</p><p id="rid">{{ request_id }}</p>', encoding="utf-8")
    page = browser.get("/_test/boom")
    assert page.status_code == 500 and '<p id="template">errors/500.html</p>' in page.text
    assert f'<p id="rid">{page.headers["x-request-id"]}</p>' in page.text
    api = browser.get("/api/v1/_test/boom")
    assert api.status_code == 500 and "errors/500.html" not in api.text  # API errors are not website pages
    server_error.unlink()
    fallback = browser.get("/_test/boom")
    assert fallback.status_code == 500 and fallback.headers["content-type"].startswith("text/plain")


def test_site_context_carries_install_link_only_when_configured(settings):
    import dataclasses

    from starling_server.web.render import site_context

    assert "extension_install_url" not in site_context(settings)
    configured = dataclasses.replace(settings, extension_install_url="https://chromewebstore.google.com/detail/starling")
    site = site_context(configured)
    assert site["extension_install_url"] == "https://chromewebstore.google.com/detail/starling"
    assert {"retention_periods", "refund_policy", "company_name"} <= set(site["legal"])


def test_user_agent_summaries():
    mac_chrome = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    windows_edge = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 Edg/128.0.0.0"
    iphone_safari = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
    assert user_agent_summary(mac_chrome) == "Chrome 128 on macOS"
    assert user_agent_summary(windows_edge) == "Edge 128 on Windows"
    assert user_agent_summary(iphone_safari) == "Safari 17 on iOS"
    assert user_agent_summary(None) == "Unknown browser"


def test_device_token_endpoint_is_rate_limited_per_ip(client, monkeypatch):
    monkeypatch.setattr(ratelimit, "DEVICE_TOKEN_PER_IP", ratelimit.Rule("device_token_ip", 2, 60))
    verifier, _ = pkce_pair()
    for _ in range(2):
        assert redeem(client, "A" * 43, verifier).status_code == 400
    limited = redeem(client, "A" * 43, verifier)
    assert limited.status_code == 429
    body = limited.json()["error"]
    assert body["code"] == "rate_limited" and body["retry_after_s"] >= 1
    assert int(limited.headers["retry-after"]) == body["retry_after_s"]
