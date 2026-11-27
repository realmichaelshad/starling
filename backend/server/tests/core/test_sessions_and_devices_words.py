"""The words after signing out other browsers and disconnecting one: counted properly, and named."""
from __future__ import annotations

import html

from .helpers import create_user, login, post_form


def said(client) -> str:
    return html.unescape(client.get("/privacy").text)


def test_signing_out_other_browsers_counts_in_words(client, second_client):
    create_user(client, "ana@example.com")
    post_form(client, "/account/security/sessions", {})
    assert "No other browsers were signed in." in said(client)
    login(second_client, "ana@example.com")
    post_form(client, "/account/security/sessions", {})
    assert "Signed out of 1 other browser." in said(client)
