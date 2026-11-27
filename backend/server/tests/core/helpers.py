"""Drive the website like a browser and the API like the extension."""
from __future__ import annotations

import base64
import hashlib
import html
import json
import re
import secrets
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ..conftest import PUBLIC_URL, TEST_EXTENSION_ID

PASSWORD = "correct horse battery"
NEW_PASSWORD = "another long passphrase"
CSRF_PAGE = "/privacy"  # renders for everyone without redirects or account state


def context_value(response, key: str) -> Any:
    match = re.search(rf'<pre id="ctx-{re.escape(key)}">(.*?)</pre>', response.text, re.S)
    assert match, f"context key {key!r} missing from {response.request.url}"
    return json.loads(html.unescape(match.group(1)))


def csrf_token(response) -> str:
    match = re.search(r'name="csrf_token" value="([^"]*)"', response.text)
    assert match, "page has no csrf token"
    return match.group(1)


def page_token(client, path: str = CSRF_PAGE) -> str:
    return csrf_token(client.get(path))


def post_form(client, path: str, data: dict[str, str], *, token: str | None = None, origin: str | None = PUBLIC_URL,
              headers: dict[str, str] | None = None, follow: bool = False):
    payload = dict(data)
    payload["csrf_token"] = page_token(client) if token is None else token
    merged = {} if origin is None else {"Origin": origin}
    merged.update(headers or {})
    return client.post(path, data=payload, headers=merged, follow_redirects=follow)


def outbox(client, to: str) -> list[dict[str, Any]]:
    response = client.get("/_dev/outbox", params={"to": to})
    assert response.status_code == 200, response.text
    return response.json()["messages"]


def link_token(client, to: str, path: str) -> str:
    pattern = re.compile(re.escape(PUBLIC_URL + path) + r"\?token=([A-Za-z0-9_-]+)")
    for message in reversed(outbox(client, to)):
        match = pattern.search(message["body"] or "")
        if match:
            return match.group(1)
    raise AssertionError(f"no {path} link sent to {to}")


def signup(client, email: str, password: str = PASSWORD):
    return post_form(client, "/signup", {"email": email, "password": password})


def verify(client, email: str):
    return post_form(client, "/verify-email", {"token": link_token(client, email, "/verify-email")})


def login(client, email: str, password: str = PASSWORD, next_path: str | None = None):
    data = {"email": email, "password": password}
    if next_path:
        data["next"] = next_path
    return post_form(client, "/login", data)


def create_user(client, email: str, password: str = PASSWORD, verified: bool = True):
    assert signup(client, email, password).status_code == 303
    if verified:
        assert verify(client, email).status_code == 303
    response = login(client, email, password)
    assert response.status_code == 303, response.text
    return response


def session_cookie(client, settings) -> str | None:
    return client.cookies.get(settings.session_cookie_name)


def account_row(database_or_app, email: str):
    db = getattr(getattr(database_or_app, "state", None), "db", database_or_app)
    with db.read() as conn:
        return conn.execute("SELECT * FROM accounts WHERE email_norm = ?", (email.lower(),)).fetchone()


# --------------------------------------------------------------------------- extension connect


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def redirect_uri(client_id: str = TEST_EXTENSION_ID) -> str:
    return f"https://{client_id}.chromiumapp.org/connect"


def connect_params(challenge: str, **overrides: str) -> dict[str, str]:
    params = {
        "client_id": TEST_EXTENSION_ID,
        "redirect_uri": redirect_uri(),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "state-from-extension-0123",
        "device_label": "Work laptop",
    }
    params.update(overrides)
    return params


def open_connect(client, params: dict[str, str]):
    page = client.get("/connect", params=params, follow_redirects=False)
    assert page.status_code == 200, page.text
    return page, context_value(page, "connect_request_id"), context_value(page, "connect_state")


def approve_connect(client, params: dict[str, str]) -> tuple[str, str]:
    page, request_id, state = open_connect(client, params)
    response = post_form(client, "/connect/approve", {"connect_request_id": request_id, "connect_state": state}, token=csrf_token(page))
    assert response.status_code == 303, response.text
    query = parse_qs(urlsplit(response.headers["location"]).query)
    return query["code"][0], query["state"][0]


def redeem(client, code: str, verifier: str, *, client_id: str = TEST_EXTENSION_ID, uri: str | None = None, version: str = "1.0.0"):
    return client.post(
        "/api/v1/devices/token",
        json={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": uri or redirect_uri(client_id),
            "client_id": client_id,
            "device_label": "Work laptop",
            "extension_version": version,
        },
    )


def connect_device(client) -> dict[str, Any]:
    verifier, challenge = pkce_pair()
    code, _ = approve_connect(client, connect_params(challenge))
    response = redeem(client, code, verifier)
    assert response.status_code == 200, response.text
    return response.json()


def api_headers(token: str, version: str = "1.0.0") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Starling-Extension": version}
