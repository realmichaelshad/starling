"""Server core pages rendered through the website's real templates and Jinja environment.

The other core tests use minimal templates so they never depend on the website workstream. This
integration test uses `web/templates` with `web/templating.py` (StrictUndefined, as the app factory
builds it), so a context key a real template reads but the gateway does not send fails here.
Skipped when the website is not installed next to the server.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from starling_server.app import create_app
from starling_server.auth import admin, totp
from starling_server.config import DEFAULT_WEB_DIR, load_settings

from ..conftest import PUBLIC_URL, base_env
from . import helpers as h

REAL_WEB = DEFAULT_WEB_DIR
LEGAL = ("privacy", "terms", "refunds", "subprocessors", "acceptable-use", "contact")
WRONG = "wrong horse battery"

pytestmark = pytest.mark.skipif(
    not (REAL_WEB / "templating.py").is_file() or not (REAL_WEB / "templates" / "base.html").is_file(),
    reason="website templates are not installed",
)


@pytest.fixture
def real_site(tmp_path, fake_clock, caplog):
    env = base_env(tmp_path / "data")
    env["STARLING_WEB_DIR"] = str(REAL_WEB)
    app = create_app(load_settings(env))
    assert app.state.templates is not None
    caplog.set_level(logging.ERROR, logger="starling_server")
    with TestClient(app, base_url=PUBLIC_URL) as client:
        yield app, client


def form_token(client, signed_in: bool = False) -> str:
    return h.csrf_token(client.get("/account/security" if signed_in else "/login", follow_redirects=False))


def errors_logged(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR]


def test_public_and_sign_in_pages_render_with_the_real_templates(real_site, caplog):
    _, client = real_site
    docs = sorted(f"/docs/{p.stem}" for p in (REAL_WEB / "templates" / "docs").glob("*.html") if not p.stem.startswith("_"))
    for path in ["/", "/pricing", *docs, *(f"/{name}" for name in LEGAL), "/signup", "/login", "/forgot-password",
                 "/verify-email/sent", "/extension/removed"]:
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers["content-type"].startswith("text/html")
    assert client.get("/verify-email", params={"token": "x" * 43}).status_code == 400
    assert client.get("/reset-password", params={"token": "x" * 43}).status_code == 400
    missing = client.get("/docs/missing-page")
    assert missing.status_code == 404 and missing.headers["content-type"].startswith("text/html")
    assert client.get("/connect", params={"client_id": "unknown"}).status_code == 400
    assert h.post_form(client, "/signup", {"email": "bad", "password": "short"}, token=form_token(client)).status_code == 400
    assert h.post_form(client, "/login", {"email": "nobody@example.com", "password": WRONG}, token=form_token(client)).status_code == 400
    assert h.post_form(client, "/forgot-password", {"email": "nobody@example.com"}, token=form_token(client)).status_code == 200
    assert errors_logged(caplog) == []


def test_account_connect_and_admin_pages_render_with_the_real_templates(real_site, fake_clock, caplog):
    app, client = real_site
    assert h.post_form(client, "/signup", {"email": "owner@example.com", "password": h.PASSWORD}, token=form_token(client)).status_code == 303
    verify_token = h.link_token(client, "owner@example.com", "/verify-email")
    assert client.get("/verify-email", params={"token": verify_token}).status_code == 200
    # The link, opened in the browser that signed up, signs that browser in: no second sign-in.
    assert h.post_form(client, "/verify-email", {"token": verify_token}, token=form_token(client)).status_code == 303
    assert client.get("/account", follow_redirects=False).status_code == 200

    for path in ("/account", "/account/billing", "/account/devices", "/account/security", "/account/data", "/pricing", "/",
                 "/verify-email/sent"):
        assert client.get(path).status_code == 200, path
    assert h.post_form(client, "/account/security/email", {"new_email": "x@example.com", "email_password": WRONG},
                       token=form_token(client, True)).status_code == 400
    assert h.post_form(client, "/account/security/password", {"current_password": WRONG, "new_password": "short"},
                       token=form_token(client, True)).status_code == 400
    assert h.post_form(client, "/account/delete", {"delete_password": h.PASSWORD, "delete_confirmation": "no"},
                       token=form_token(client, True)).status_code == 400

    verifier, challenge = h.pkce_pair()
    page = client.get("/connect", params=h.connect_params(challenge), follow_redirects=False)
    assert page.status_code == 200
    request_id = re.search(r'name="connect_request_id" value="([^"]+)"', page.text).group(1)
    state = re.search(r'name="connect_state" value="([^"]+)"', page.text).group(1)
    approved = h.post_form(client, "/connect/approve", {"connect_request_id": request_id, "connect_state": state},
                           token=h.csrf_token(page))
    assert approved.status_code == 303
    assert h.redeem(client, parse_qs(urlsplit(approved.headers["location"]).query)["code"][0], verifier).status_code == 200
    assert client.get("/account/devices").status_code == 200

    admin.grant_admin(app.state.db, app.state.settings, "owner@example.com")
    assert client.get("/admin/totp").status_code == 200
    row = h.account_row(app, "owner@example.com")
    secret = totp.decrypt_secret(app.state.settings.secret_key, row["id"], row["totp_secret_enc"])
    code = totp.totp(totp.decode_secret(secret), fake_clock.value)
    assert h.post_form(client, "/admin/totp", {"password": WRONG, "code": code}, token=form_token(client, True)).status_code == 400
    assert h.post_form(client, "/admin/totp", {"password": h.PASSWORD, "code": code}, token=form_token(client, True)).status_code == 303
    assert client.get("/admin/elevate").status_code == 200
    assert h.post_form(client, "/admin/elevate", {"code": "000000", "next": "/admin"}, token=form_token(client, True)).status_code == 400
    assert errors_logged(caplog) == []


def test_static_files_are_compressed_and_cached(real_site):
    _, client = real_site
    page = client.get("/")
    css = re.search(r'href="(/static/css/site\.css\?v=[^"]+)"', page.text).group(1)
    response = client.get(css, headers={"Accept-Encoding": "gzip"})
    assert response.status_code == 200
    assert response.headers["content-encoding"] == "gzip"
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    font = client.get("/static/fonts/ibm-plex-sans-variable.woff2", headers={"Accept-Encoding": "gzip"})
    assert font.status_code == 200 and "content-encoding" not in font.headers  # compressed already
    assert font.headers["cache-control"] == "public, max-age=3600"
    assert "starling-wordmark-small.png" in page.text  # the header no longer ships the 83 KB wordmark



def test_the_writing_pages_render_with_the_real_templates(real_site, caplog):
    """/write and /write/voice before and after consent, the offline page, the manifest and the
    service worker, through the real templates and static files (no engine is asked for anything:
    the pages only reach an engine to list writing samples once consent is given)."""
    app, client = real_site
    assert h.post_form(client, "/signup", {"email": "writer@example.com", "password": h.PASSWORD},
                       token=form_token(client)).status_code == 303
    verify_token = h.link_token(client, "writer@example.com", "/verify-email")
    assert h.post_form(client, "/verify-email", {"token": verify_token}, token=form_token(client)).status_code == 303

    shared = client.get("/write", params={"text": "https://x.com/dana_example/status/1851234567890123456?s=20"})
    assert shared.status_code == 200
    assert "Before your first draft" in shared.text and 'action="/write/consent"' in shared.text
    assert "data-write-form" not in shared.text, "nothing to send from until the person agrees"
    assert 'rel="manifest" href="/manifest.webmanifest"' in shared.text
    assert 'name="apple-mobile-web-app-capable" content="yes"' in shared.text
    assert 'class="tabbar"' in shared.text and 'href="/write" aria-current="page"' in shared.text
    assert client.get("/write/voice").status_code == 200

    agreed = h.post_form(client, "/write/consent", {"agree": "yes", "next": "/write"}, token=form_token(client, True))
    assert agreed.status_code == 303
    page = client.get("/write")
    assert page.status_code == 200
    for marker in ("data-write-form", "data-handled-by-script", 'name="kind"', "data-draft-template",
                   "Take it back", "Teach Starling your voice", "/static/js/write.js?v="):
        assert marker in page.text, marker
    assert "<script>" not in page.text and " style=" not in page.text

    offline = client.get("/offline")
    assert offline.status_code == 200 and "You're offline" in offline.text
    assert "writer@example.com" not in offline.text and "csrf_token" not in offline.text
    manifest = client.get("/manifest.webmanifest").json()
    for icon in manifest["icons"]:
        assert client.get(icon["src"]).status_code == 200, icon["src"]
    worker = client.get("/sw.js")
    assert worker.status_code == 200 and worker.headers["service-worker-allowed"] == "/"
    assert errors_logged(caplog) == []
