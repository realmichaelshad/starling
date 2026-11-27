"""Bodies and fields are bounded — the risk here is memory, not request volume.

There is no brute-force target behind a 256-bit token and no multi-tenancy, so
rate limiting would be cargo cult. What an authenticated caller CAN do is hand
the process something enormous, and what an x.com post can do is arrive with
fields nobody bounded.
"""
from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import gate, main, security
from app.schemas import PostContext


@pytest.fixture()
def client():
    with TestClient(main.app) as running:
        yield running


@pytest.fixture()
def headers():
    return {"Authorization": f"Bearer {security.ensure_token()}"}


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #

def test_an_oversized_body_is_refused(client, headers):
    oversized = "x" * (gate.MAX_BODY_BYTES + 2048)
    response = client.post("/browser/assist", headers=headers,
                           json={"mode": "context", "context": {"author": "A", "text": oversized}})
    assert response.status_code in (413, 422), "must not be accepted and parsed"


def test_a_normal_body_still_passes(client, headers):
    response = client.post("/images/feedback", headers=headers,
                           json={"choice": "used", "metadata": {"visual_job": "show a comparison"}})
    assert response.status_code == 200


# --------------------------------------------------------------------------- #
# import_pack decoded before it was measured
# --------------------------------------------------------------------------- #

# RETIRED (API-native pass, 2026-09-22): test_an_oversized_pack_is_rejected_before_it_is_decoded: the archive-pack import left the hosted engine (no route took it).


# RETIRED (API-native pass, 2026-09-22): test_the_schema_rejects_an_oversized_pack_first: the archive-pack import left the hosted engine (no route took it).


# RETIRED (API-native pass, 2026-09-22): test_a_real_pack_still_validates: the archive-pack import left the hosted engine (no route took it).


# --------------------------------------------------------------------------- #
# The context fields, every one of which an attacker writes
# --------------------------------------------------------------------------- #

def test_a_normal_post_is_unaffected():
    context = PostContext(author="Someone", text="A perfectly ordinary post.",
                          links=["https://example.com"], thread_text=["a", "b"])
    assert context.text.startswith("A perfectly")


# Parametrised on LENGTH, never on the value: pytest puts the test id into
# PYTEST_CURRENT_TEST, and a 40k-character id exceeds the 32767-character
# ceiling on a Windows environment variable — the test run dies before it can
# assert anything.
@pytest.mark.parametrize("field,length", [
    ("text", 40_000),
    ("parent_text", 40_000),
    ("quote_text", 40_000),
    ("author", 500),
    ("author_handle", 500),
])
def test_an_unbounded_string_is_rejected(field, length):
    with pytest.raises(ValidationError):
        PostContext(**{"author": "A", "text": "hi", field: "x" * length})


@pytest.mark.parametrize("field,item", [
    ("thread_text", "x"),
    ("links", "https://e.com"),
])
def test_an_unbounded_list_is_rejected(field, item):
    with pytest.raises(ValidationError):
        PostContext(**{"author": "A", "text": "hi", field: [item] * 200})


# --------------------------------------------------------------------------- #
# Errors do not leak content
# --------------------------------------------------------------------------- #

def test_a_validation_error_names_fields_without_echoing_them(client, headers):
    secret = "a-private-draft-nobody-should-see-echoed"
    response = client.post("/browser/assist", headers=headers,
                           json={"mode": "context", "context": {"author": "A", "text": secret * 2000}})
    assert response.status_code in (413, 422)
    assert secret not in response.text, "the rejected value must not come back"
