"""Extension connect (CONTRACTS section 9): approval, codes, PKCE and device tokens."""
from __future__ import annotations

from urllib.parse import parse_qs, urlencode, urlsplit

from starling_server.devices import connect

from ..conftest import PUBLIC_URL, TEST_EXTENSION_ID
from .helpers import (
    api_headers,
    approve_connect,
    connect_params,
    context_value,
    create_user,
    csrf_token,
    login,
    open_connect,
    pkce_pair,
    post_form,
    redeem,
    redirect_uri,
    signup,
)

OTHER_EXTENSION = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def request_count(app) -> int:
    with app.state.db.read() as conn:
        return conn.execute("SELECT COUNT(*) FROM connect_requests").fetchone()[0]


def decision_fields(request_id: str, state: str) -> dict[str, str]:
    return {"connect_request_id": request_id, "connect_state": state}


def test_connect_issues_a_device_token(app, client, fake_state):
    create_user(client, "ana@example.com")
    verifier, challenge = pkce_pair()
    page, request_id, state = open_connect(client, connect_params(challenge))
    assert context_value(page, "extension_id") == TEST_EXTENSION_ID
    assert context_value(page, "device_label") == "Work laptop"
    assert f"form-action 'self' https://{TEST_EXTENSION_ID}.chromiumapp.org" in page.headers["content-security-policy"]

    approved = post_form(client, "/connect/approve", decision_fields(request_id, state), token=csrf_token(page))
    assert approved.status_code == 303
    location = urlsplit(approved.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == redirect_uri()
    query = parse_qs(location.query)
    assert query["state"] == [state]

    issued = redeem(client, query["code"][0], verifier)
    assert issued.status_code == 200, issued.text
    body = issued.json()
    assert set(body) == {"token", "device_id", "idle_expires_at", "expires_at"}
    assert body["token"].startswith("stl_dev_") and len(body["token"]) == len("stl_dev_") + 43
    assert body["expires_at"] - body["idle_expires_at"] == (365 - 60) * 86400
    assert issued.headers["cache-control"] == "no-store"
    assert client.get("/api/v1/session", headers=api_headers(body["token"])).status_code == 200
    with app.state.db.read() as conn:
        row = conn.execute("SELECT token_hash, label, extension_id FROM devices WHERE id = ?", (body["device_id"],)).fetchone()
    assert len(row["token_hash"]) == 64 and body["token"] not in row["token_hash"]
    assert row["label"] == "Work laptop" and row["extension_id"] == TEST_EXTENSION_ID


def test_signed_out_visitor_signs_in_then_returns_to_the_request(app, client):
    _, challenge = pkce_pair()
    response = client.get("/connect", params=connect_params(challenge), follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=%2Fconnect%3F")
    assert request_count(app) == 0
    next_path = parse_qs(urlsplit(response.headers["location"]).query)["next"][0]
    assert signup(client, "ana@example.com").status_code == 303
    back = login(client, "ana@example.com", next_path=next_path)
    assert back.headers["location"] == next_path
    assert client.get(next_path, follow_redirects=False).status_code == 200
    assert request_count(app) == 1


def test_approval_by_get_is_impossible(app, client):
    create_user(client, "ana@example.com")
    _, challenge = pkce_pair()
    page, request_id, state = open_connect(client, connect_params(challenge))
    for path in ("/connect/approve", "/connect/deny"):
        attempt = client.get(path, params=decision_fields(request_id, state), follow_redirects=False)
        assert attempt.status_code == 405 and "location" not in attempt.headers
    without_csrf = client.post("/connect/approve", data=decision_fields(request_id, state),
                               headers={"Origin": PUBLIC_URL}, follow_redirects=False)
    assert without_csrf.status_code == 403
    with app.state.db.read() as conn:
        row = conn.execute("SELECT code_hash, approved_at FROM connect_requests WHERE id = ?", (request_id,)).fetchone()
    assert row["code_hash"] is None and row["approved_at"] is None
    assert post_form(client, "/connect/approve", decision_fields(request_id, state), token=csrf_token(page)).status_code == 303


def test_code_replay_is_refused_and_revokes_the_device(client, fake_state):
    create_user(client, "ana@example.com")
    verifier, challenge = pkce_pair()
    code, _ = approve_connect(client, connect_params(challenge))
    first = redeem(client, code, verifier)
    assert first.status_code == 200
    token = first.json()["token"]
    assert client.get("/api/v1/session", headers=api_headers(token)).status_code == 200
    replay = redeem(client, code, verifier)
    assert replay.status_code == 400 and replay.json()["error"]["code"] == "bad_request"
    assert client.get("/api/v1/session", headers=api_headers(token)).status_code == 401


def test_expired_code_is_refused(client, fake_clock):
    create_user(client, "ana@example.com")
    verifier, challenge = pkce_pair()
    code, _ = approve_connect(client, connect_params(challenge))
    fake_clock.advance(connect.CODE_TTL_S + 1)
    assert redeem(client, code, verifier).status_code == 400
    verifier, challenge = pkce_pair()  # negative control: within 120 seconds
    code, _ = approve_connect(client, connect_params(challenge))
    fake_clock.advance(connect.CODE_TTL_S - 1)
    assert redeem(client, code, verifier).status_code == 200


def test_wrong_verifier_is_refused_and_spends_the_code(client):
    create_user(client, "ana@example.com")
    verifier, challenge = pkce_pair()
    code, _ = approve_connect(client, connect_params(challenge))
    other_verifier, _ = pkce_pair()
    refused = redeem(client, code, other_verifier)
    assert refused.status_code == 400
    assert redeem(client, code, verifier).status_code == 400


def test_redirect_uri_and_client_id_must_be_allowlisted(app, client):
    create_user(client, "ana@example.com")
    _, challenge = pkce_pair()
    cases = [
        connect_params(challenge, redirect_uri="https://evil.example/connect"),
        connect_params(challenge, redirect_uri=f"https://{TEST_EXTENSION_ID}.chromiumapp.org/other"),
        connect_params(challenge, redirect_uri=f"https://{TEST_EXTENSION_ID}.chromiumapp.org/connect?x=1"),
        connect_params(challenge, client_id=OTHER_EXTENSION, redirect_uri=redirect_uri(OTHER_EXTENSION)),
        connect_params(challenge, code_challenge_method="plain"),
        connect_params("too-short"),
        connect_params(challenge, state="short"),
    ]
    for params in cases:
        response = client.get("/connect", params=params, follow_redirects=False)
        assert response.status_code == 400, params
        assert "location" not in response.headers
    duplicated = list(connect_params(challenge).items()) + [("redirect_uri", "https://evil.example/connect")]
    assert client.get("/connect?" + urlencode(duplicated), follow_redirects=False).status_code == 400
    assert request_count(app) == 0
    open_connect(client, connect_params(challenge))  # negative control
    assert request_count(app) == 1


def test_token_exchange_must_match_client_and_redirect(client):
    create_user(client, "ana@example.com")
    for mismatch in ({"uri": "https://evil.example/connect"}, {"client_id": OTHER_EXTENSION}):
        verifier, challenge = pkce_pair()
        code, _ = approve_connect(client, connect_params(challenge))
        assert redeem(client, code, verifier, **mismatch).status_code == 400


def test_request_of_another_account_cannot_be_approved(app, client, second_client):
    create_user(client, "ana@example.com")
    create_user(second_client, "bob@example.com")
    verifier, challenge = pkce_pair()
    page, request_id, state = open_connect(client, connect_params(challenge))

    hijack = post_form(second_client, "/connect/approve", decision_fields(request_id, state))
    assert hijack.status_code == 400 and "location" not in hijack.headers
    tampered_state = post_form(client, "/connect/approve", decision_fields(request_id, state + "x"), token=csrf_token(page))
    assert tampered_state.status_code == 400
    with app.state.db.read() as conn:
        assert conn.execute("SELECT code_hash FROM connect_requests WHERE id = ?", (request_id,)).fetchone()[0] is None

    approved = post_form(client, "/connect/approve", decision_fields(request_id, state), token=csrf_token(page))
    code = parse_qs(urlsplit(approved.headers["location"]).query)["code"][0]
    device = redeem(second_client, code, verifier).json()
    with app.state.db.read() as conn:
        owner = conn.execute(
            "SELECT a.email_norm FROM devices d JOIN accounts a ON a.id = d.account_id WHERE d.id = ?", (device["device_id"],)
        ).fetchone()[0]
    assert owner == "ana@example.com"  # the code is bound to the approving account


def test_deny_redirects_with_access_denied(client):
    create_user(client, "ana@example.com")
    _, challenge = pkce_pair()
    page, request_id, state = open_connect(client, connect_params(challenge))
    denied = post_form(client, "/connect/deny", decision_fields(request_id, state), token=csrf_token(page))
    assert denied.status_code == 303
    location = urlsplit(denied.headers["location"])
    assert parse_qs(location.query) == {"error": ["access_denied"], "state": [state]}
    assert post_form(client, "/connect/approve", decision_fields(request_id, state), token=csrf_token(page)).status_code == 400


def test_device_label_is_sanitised_and_escaped(client):
    create_user(client, "ana@example.com")
    _, challenge = pkce_pair()
    page, _, _ = open_connect(client, connect_params(challenge, device_label="<script>alert(1)</script>‮" + "x" * 80))
    label = context_value(page, "device_label")
    assert label.startswith("<script>alert(1)</script>") and len(label) == 60 and "‮" not in label
    assert "<script>alert(1)" not in page.text


def test_outdated_extension_cannot_exchange_a_code(client):
    create_user(client, "ana@example.com")
    verifier, challenge = pkce_pair()
    code, _ = approve_connect(client, connect_params(challenge))
    outdated = redeem(client, code, verifier, version="0.1.0")
    assert outdated.status_code == 426 and outdated.json()["error"]["code"] == "extension_outdated"
    assert redeem(client, code, verifier).status_code == 200
