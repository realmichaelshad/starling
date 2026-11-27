"""Owner admin identity: CLI grant, TOTP enrollment, elevation, and key rotation (CONTRACTS 13.2)."""
from __future__ import annotations

import argparse
import dataclasses

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from starling_server.app import create_app
from starling_server.auth import admin, commands, sessions, totp
from starling_server.auth.deps import CurrentUser, require_admin
from starling_server.config import load_settings

from ..conftest import PUBLIC_URL, TEST_SECRET_KEY, base_env
from .helpers import PASSWORD, account_row, context_value, create_user, post_form, session_cookie, verify
from .test_units import ROTATED_KEY

PROBE = "/_test/admin-probe"
WRONG = "wrong horse battery"


@pytest.fixture
def admin_probe(app):
    async def probe(user: CurrentUser = Depends(require_admin)):
        return {"admin": user.account_id}

    app.add_api_route(PROBE, probe, methods=["GET"])
    return PROBE


def enroll(client, fake_clock) -> bytes:
    secret = context_value(client.get("/admin/totp"), "secret")
    key = totp.decode_secret(secret)
    response = post_form(client, "/admin/totp", {"password": PASSWORD, "code": totp.totp(key, fake_clock.value)})
    assert response.status_code == 303, response.text
    return key


def elevate(client, key: bytes, fake_clock, next_path: str = PROBE):
    fake_clock.advance(totp.STEP_S)
    return post_form(client, "/admin/elevate", {"code": totp.totp(key, fake_clock.value), "next": next_path})


def test_cli_grant_needs_an_existing_verified_owner_account(app, client, second_client, settings):
    db = app.state.db
    with pytest.raises(admin.AdminError, match="no account"):
        admin.grant_admin(db, settings, "owner@example.com")
    create_user(client, "owner@example.com", verified=False)
    with pytest.raises(admin.AdminError, match="verified"):
        admin.grant_admin(db, settings, "owner@example.com")
    assert verify(client, "owner@example.com").status_code == 303
    create_user(second_client, "guest@example.com")
    with pytest.raises(admin.AdminError, match="STARLING_OWNER_EMAILS"):
        admin.grant_admin(db, settings, "guest@example.com")
    assert admin.grant_admin(db, settings, " Owner@Example.com ").is_admin
    with db.read() as conn:
        actions = [row[0] for row in conn.execute("SELECT action FROM admin_audit ORDER BY id")]
    assert actions == ["admin_grant"]


def test_cli_commands_report_success_and_refusal(app, client, settings, monkeypatch, capsys):
    create_user(client, "owner@example.com")
    monkeypatch.setattr(commands, "get_settings", lambda: settings)
    parser = argparse.ArgumentParser()
    commands.register(parser.add_subparsers(dest="command", required=True))
    granted = parser.parse_args(["admin-grant", "--email", "owner@example.com"])
    assert granted.func(granted) == 0
    refused = parser.parse_args(["admin-grant", "--email", "nobody@example.com"])
    assert refused.func(refused) == 1
    output = capsys.readouterr()
    assert "done" in output.out and "refused" in output.err
    assert account_row(app, "owner@example.com")["is_admin"] == 1
    revoked = parser.parse_args(["admin-revoke", "--email", "owner@example.com"])
    assert revoked.func(revoked) == 0
    assert account_row(app, "owner@example.com")["is_admin"] == 0


def test_admin_needs_cli_grant_totp_and_elevation(app, client, settings, fake_clock, admin_probe):
    create_user(client, "owner@example.com")
    # A verified email listed in STARLING_OWNER_EMAILS is not admin by itself.
    assert client.get(PROBE, follow_redirects=False).status_code == 404
    assert client.get("/admin/totp", follow_redirects=False).status_code == 404

    admin.grant_admin(app.state.db, settings, "owner@example.com")
    needs_totp = client.get(PROBE, follow_redirects=False)
    assert needs_totp.status_code == 303 and needs_totp.headers["location"] == "/admin/totp"

    page = client.get("/admin/totp")
    secret = context_value(page, "secret")
    uri = context_value(page, "otpauth_uri")
    assert uri.startswith("otpauth://totp/") and f"secret={secret}" in uri and "issuer=Starling" in uri
    assert context_value(client.get("/admin/totp"), "secret") == secret  # stable until confirmed
    assert secret not in (account_row(app, "owner@example.com")["totp_secret_enc"] or "")  # encrypted at rest
    key = totp.decode_secret(secret)
    refused = post_form(client, "/admin/totp", {"password": WRONG, "code": totp.totp(key, fake_clock.value)})
    assert refused.status_code == 400
    enrolled = post_form(client, "/admin/totp", {"password": PASSWORD, "code": totp.totp(key, fake_clock.value)})
    assert enrolled.status_code == 303 and enrolled.headers["location"] == "/admin/elevate"

    needs_elevation = client.get(PROBE, follow_redirects=False)
    assert needs_elevation.status_code == 303
    assert needs_elevation.headers["location"].startswith("/admin/elevate?next=")
    replay = post_form(client, "/admin/elevate", {"code": totp.totp(key, fake_clock.value), "next": PROBE})
    assert replay.status_code == 400  # the enrollment code cannot be used twice

    before = session_cookie(client, settings)
    elevated = elevate(client, key, fake_clock)
    assert elevated.status_code == 303 and elevated.headers["location"] == PROBE
    assert session_cookie(client, settings) != before  # the session id rotates on elevation
    assert client.get(PROBE, follow_redirects=False).status_code == 200

    fake_clock.advance(sessions.ADMIN_ELEVATION_S + 1)
    assert client.get(PROBE, follow_redirects=False).status_code == 303


def test_admin_identity_is_rechecked_on_every_request(app, client, settings, fake_clock, admin_probe):
    create_user(client, "owner@example.com")
    admin.grant_admin(app.state.db, settings, "owner@example.com")
    key = enroll(client, fake_clock)
    assert elevate(client, key, fake_clock).status_code == 303
    assert client.get(PROBE, follow_redirects=False).status_code == 200
    app.state.settings = dataclasses.replace(settings, owner_emails=())
    assert client.get(PROBE, follow_redirects=False).status_code == 404
    app.state.settings = settings
    assert client.get(PROBE, follow_redirects=False).status_code == 200
    admin.revoke_admin(app.state.db, settings, "owner@example.com")
    assert client.get(PROBE, follow_redirects=False).status_code == 404
    row = account_row(app, "owner@example.com")
    assert row["totp_secret_enc"] is None and row["totp_enabled_at"] is None


def test_wrong_elevation_codes_are_limited(app, client, settings, fake_clock, admin_probe):
    create_user(client, "owner@example.com")
    admin.grant_admin(app.state.db, settings, "owner@example.com")
    key = enroll(client, fake_clock)
    for _ in range(4):
        assert post_form(client, "/admin/elevate", {"code": "000000", "next": PROBE}).status_code == 400
    fake_clock.advance(totp.STEP_S)
    limited = post_form(client, "/admin/elevate", {"code": totp.totp(key, fake_clock.value), "next": PROBE})
    assert limited.status_code == 400 and "Too many attempts" in limited.text
    assert client.get(PROBE, follow_redirects=False).status_code == 303


def test_totp_secret_survives_key_rotation_and_is_reencrypted(tmp_path, web_dir, fake_clock):
    data_dir = tmp_path / "rotation"

    def app_for(**extra: str):
        env = base_env(data_dir)
        env["STARLING_WEB_DIR"] = str(web_dir)
        env.update(extra)
        return create_app(load_settings(env))

    old_app = app_for()
    with TestClient(old_app, base_url=PUBLIC_URL) as browser:
        create_user(browser, "owner@example.com")
        admin.grant_admin(old_app.state.db, old_app.state.settings, "owner@example.com")
        key = enroll(browser, fake_clock)
        cookies = dict(browser.cookies)
        old_row = account_row(old_app, "owner@example.com")

    rotating = app_for(STARLING_SECRET_KEY=ROTATED_KEY, STARLING_SECRET_KEY_PREVIOUS=TEST_SECRET_KEY)
    new_key = rotating.state.settings.secret_key
    assert totp.decrypt_secret(new_key, old_row["id"], old_row["totp_secret_enc"]) is None
    with TestClient(rotating, base_url=PUBLIC_URL, cookies=cookies) as browser:
        assert browser.get("/admin/elevate", follow_redirects=False).status_code == 200  # sessions survive rotation
        assert elevate(browser, key, fake_clock, next_path="/account").status_code == 303
        new_cipher = account_row(rotating, "owner@example.com")["totp_secret_enc"]
    assert new_cipher != old_row["totp_secret_enc"]
    assert totp.decrypt_secret(new_key, old_row["id"], new_cipher) is not None

    finished = app_for(STARLING_SECRET_KEY=ROTATED_KEY)
    with TestClient(finished, base_url=PUBLIC_URL, cookies=cookies) as browser:
        assert elevate(browser, key, fake_clock, next_path="/account").status_code == 303
