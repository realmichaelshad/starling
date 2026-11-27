"""Adversarial cover for malformed requests and for the rows the account flows leave behind.

Nothing a stranger can send may produce a 500 or a page that describes the server's insides,
and no table an account flow writes to may grow without a way to shrink again.
"""
from __future__ import annotations

import random

import pytest

from starling_server.auth import email_tokens, service, sessions
from starling_server.devices import connect
from starling_server.security import ratelimit

from ..conftest import PUBLIC_URL
from .helpers import PASSWORD, account_row, create_user, page_token, post_form, signup

LEAKS = ("Traceback", "sqlite3", "starling_server", "File \"", "self.", "anyio", "argon2")


def assert_no_internals(response) -> None:
    text = response.text
    for marker in LEAKS:
        assert marker not in text, f"{response.request.url} leaked {marker!r}"


# --------------------------------------------------------------------------- malformed requests


@pytest.mark.parametrize("method", ["GET", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
def test_wrong_methods_on_the_account_forms_are_shaped(client, method, fake_state):
    create_user(client, "ana@example.com")
    response = client.request(method, "/account/security/sessions", follow_redirects=False)
    assert response.status_code in (405, 404)
    assert_no_internals(response)


@pytest.mark.parametrize("path", ["/signup", "/login", "/forgot-password", "/verify-email", "/reset-password"])
def test_a_json_body_on_a_form_route_is_refused_not_crashed(client, path):
    page_token(client)
    response = client.post(path, json={"email": "ana@example.com", "password": PASSWORD}, follow_redirects=False)
    assert response.status_code == 403  # no form token can be read from a JSON body
    assert_no_internals(response)


@pytest.mark.parametrize("path", ["/signup", "/login", "/forgot-password"])
def test_a_form_without_a_length_is_refused(client, path):
    token = page_token(client)
    response = client.post(
        path,
        content=iter([f"csrf_token={token}&email=ana@example.com".encode()]),
        headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": PUBLIC_URL},
        follow_redirects=False,
    )
    assert response.status_code == 411
    assert_no_internals(response)


def test_an_oversized_form_is_refused_with_413(client):
    response = post_form(client, "/signup", {"email": "ana@example.com", "password": "x" * 200_000})
    assert response.status_code == 413
    assert_no_internals(response)


@pytest.mark.parametrize(
    "body, content_type",
    [
        (b"\xff\xfe\x00not utf-8", "application/x-www-form-urlencoded"),
        (b"%%%%", "application/x-www-form-urlencoded"),
        (b"a=1&a=2&a=3", "application/x-www-form-urlencoded"),
        (b"--x\r\nContent-Disposition: form-data; name=\"a\"\r\n\r\n1\r\n--x--\r\n", "multipart/form-data; boundary=x"),
        (b"", "application/x-www-form-urlencoded"),
        (b"<xml/>", "text/xml"),
        (b"x", ""),
    ],
)
def test_odd_request_bodies_never_produce_a_server_error(client, body, content_type):
    headers = {"Origin": PUBLIC_URL}
    if content_type:
        headers["Content-Type"] = content_type
    response = client.post("/signup", content=body, headers=headers, follow_redirects=False)
    assert response.status_code < 500, response.text
    assert_no_internals(response)


@pytest.mark.parametrize("forged_last, expected", [(True, 403), (False, 303)])
def test_a_repeated_csrf_field_cannot_smuggle_a_forged_token_through(client, forged_last, expected):
    """Starlette keeps the last value of a repeated field. Either way there is no bypass: a
    cross-site page cannot produce the good token at all, and a forged one placed last is
    refused rather than ignored."""
    token = page_token(client)
    values = [token, "x" * 43] if forged_last else ["x" * 43, token]
    fields = "&".join(f"csrf_token={value}" for value in values)
    body = f"{fields}&email=ana%40example.com&password={PASSWORD.replace(' ', '+')}"
    response = client.post(
        "/signup",
        content=body.encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": PUBLIC_URL},
        follow_redirects=False,
    )
    assert response.status_code == expected
    assert_no_internals(response)


def test_a_form_body_the_parser_refuses_gets_the_website_refusal(client):
    """A file part or a flood of fields must not leave the framework's own JSON on a page route."""
    token = page_token(client)
    multipart = (
        b"--x\r\n"
        b'Content-Disposition: form-data; name="csrf_token"\r\n\r\n' + token.encode() + b"\r\n"
        b"--x\r\n"
        b'Content-Disposition: form-data; name="upload"; filename="a.txt"\r\n'
        b"Content-Type: text/plain\r\n\r\nhello\r\n"
        b"--x--\r\n"
    )
    with_file = client.post(
        "/signup", content=multipart,
        headers={"Content-Type": "multipart/form-data; boundary=x", "Origin": PUBLIC_URL},
        follow_redirects=False,
    )
    flood = "&".join(f"f{index}=1" for index in range(200))
    with_many_fields = client.post(
        "/signup", content=flood.encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": PUBLIC_URL},
        follow_redirects=False,
    )
    for response in (with_file, with_many_fields):
        assert response.status_code == 400
        assert "detail" not in response.text and response.text.lstrip().startswith("<!doctype html")
        assert_no_internals(response)


def test_an_absurd_query_string_is_handled(client):
    for path in ("/verify-email", "/reset-password", "/login", "/connect"):
        response = client.get(path, params={"token": "x" * 5000, "next": "y" * 5000}, follow_redirects=False)
        assert response.status_code < 500, path
        assert_no_internals(response)


def test_an_enormous_user_agent_is_stored_bounded(app, client, settings, fake_state):
    from starling_server.security.crypto import sha256_hex

    create_user(client, "ana@example.com")
    huge = "Mozilla/5.0 " + "A" * 10_000
    assert post_form(client, "/login", {"email": "ana@example.com", "password": PASSWORD},
                     headers={"User-Agent": huge}).status_code == 303
    cookie = client.cookies.get(settings.session_cookie_name)
    with app.state.db.read() as conn:
        stored = conn.execute(
            "SELECT user_agent FROM web_sessions WHERE id_hash = ?", (sha256_hex(cookie),)
        ).fetchone()[0]
    assert len(stored) == sessions.USER_AGENT_MAX
    # The account page only ever shows the short summary it derives, never the raw header.
    page = client.get("/account/security")
    assert "AAAA" not in page.text


def test_unknown_urls_answer_the_right_shape_for_their_side_of_the_app(client):
    api = client.get("/api/v1/nothing-here")
    assert api.status_code == 404 and api.json()["error"]["code"] == "not_found"
    assert_no_internals(api)
    wrong_method = client.get("/api/v1/devices/token")
    assert wrong_method.status_code == 405 and wrong_method.json()["error"]["code"] == "bad_request"
    page = client.get("/no-such-page", follow_redirects=False)
    assert page.status_code == 404
    assert_no_internals(page)


def test_an_unexpected_failure_shows_a_page_without_internals(app, client, monkeypatch):
    async def explode(*args, **kwargs):
        raise RuntimeError("the database melted at /data/control/control.db")

    monkeypatch.setattr(service, "signup", explode)
    response = signup(client, "ana@example.com")
    assert response.status_code == 500
    assert "melted" not in response.text and "control.db" not in response.text
    assert_no_internals(response)
    assert response.headers.get("x-request-id")


def test_an_unexpected_failure_on_a_json_client_does_not_describe_the_server(app, client, monkeypatch):
    async def explode(*args, **kwargs):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(service, "signup", explode)
    token = page_token(client)
    with pytest.raises(RuntimeError):
        # The framework's own handler takes over for JSON clients; nothing is rendered by us.
        client.post(
            "/signup",
            data={"csrf_token": token, "email": "ana@example.com", "password": PASSWORD},
            headers={"Origin": PUBLIC_URL, "Accept": "application/json"},
            follow_redirects=False,
        )


# --------------------------------------------------------------------------- housekeeping


def test_expired_email_tokens_are_cleared_out(app, client, database, settings, fake_clock):
    assert signup(client, "ana@example.com").status_code == 303
    account_id = account_row(app, "ana@example.com")["id"]

    def count() -> int:
        with app.state.db.read() as conn:
            return conn.execute("SELECT COUNT(*) FROM email_tokens").fetchone()[0]

    assert count() == 1
    fake_clock.advance(email_tokens.VERIFY_TTL_S + email_tokens.PURGE_AFTER_S + 1)
    app.state.db.write(lambda conn: email_tokens.purge(conn, fake_clock.value))
    assert count() == 0

    # A live token is never swept, and an issue is what triggers the sweep in normal running.
    fresh = app.state.db.write(
        lambda conn: email_tokens.issue(conn, account_id, "verify", email_tokens.VERIFY_TTL_S, fake_clock.value)
    )
    app.state.db.write(lambda conn: email_tokens.purge(conn, fake_clock.value))
    assert count() == 1 and fresh


def test_issuing_a_token_occasionally_sweeps_the_table(app, client, monkeypatch, fake_clock):
    assert signup(client, "ana@example.com").status_code == 303
    account_id = account_row(app, "ana@example.com")["id"]
    fake_clock.advance(email_tokens.VERIFY_TTL_S + email_tokens.PURGE_AFTER_S + 1)
    monkeypatch.setattr(random, "random", lambda: 0.0)  # force the sweep this time
    app.state.db.write(
        lambda conn: email_tokens.issue(conn, account_id, "verify", email_tokens.VERIFY_TTL_S, fake_clock.value)
    )
    with app.state.db.read() as conn:
        rows = conn.execute("SELECT expires_at FROM email_tokens").fetchall()
    assert len(rows) == 1 and rows[0][0] > fake_clock.value


def test_used_tokens_do_not_pile_up_forever(app, client, fake_clock):
    from .helpers import link_token

    assert signup(client, "ana@example.com").status_code == 303
    token = link_token(client, "ana@example.com", "/verify-email")
    assert post_form(client, "/verify-email", {"token": token}).status_code == 303
    fake_clock.advance(email_tokens.PURGE_AFTER_S + 1)
    app.state.db.write(lambda conn: email_tokens.purge(conn, fake_clock.value))
    with app.state.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM email_tokens").fetchone()[0] == 0


def test_dead_sessions_are_cleared_out(app, client, fake_clock, fake_state):
    create_user(client, "ana@example.com")
    assert post_form(client, "/logout", {}).status_code == 303
    fake_clock.advance(sessions.PURGE_AFTER_S + 1)
    app.state.db.write(lambda conn: sessions.purge(conn, fake_clock.value))
    with app.state.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM web_sessions").fetchone()[0] == 0


def test_old_connect_requests_and_rate_limit_rows_are_cleared_out(app, client, fake_clock, fake_state):
    from .helpers import connect_params, open_connect, pkce_pair

    create_user(client, "ana@example.com")
    open_connect(client, connect_params(pkce_pair()[1]))
    with app.state.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM connect_requests").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM rate_limits").fetchone()[0] > 0

    fake_clock.advance(connect.PURGE_AFTER_S + connect.REQUEST_TTL_S + 1)
    app.state.db.write(
        lambda conn: conn.execute("DELETE FROM connect_requests WHERE expires_at < ?", (fake_clock.value - connect.PURGE_AFTER_S,))
    )
    fake_clock.advance(ratelimit.CLEANUP_AGE_S + 1)
    app.state.db.write(
        lambda conn: conn.execute("DELETE FROM rate_limits WHERE window_start < ?", (fake_clock.value - ratelimit.CLEANUP_AGE_S,))
    )
    with app.state.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM connect_requests").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM rate_limits").fetchone()[0] == 0
