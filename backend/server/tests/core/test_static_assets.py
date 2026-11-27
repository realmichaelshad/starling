"""Pages are never compressed (BREACH); static files are, with real templates (test_real_templates.py)."""
from __future__ import annotations


def test_pages_are_never_compressed(client):
    """A page carries a CSRF token beside text a visitor can influence: compressing it is BREACH."""
    response = client.get("/login?next=/account", headers={"Accept-Encoding": "gzip"})
    assert response.status_code == 200
    assert "content-encoding" not in response.headers
    assert response.headers["cache-control"] == "no-store"


def test_the_extension_api_is_never_compressed(client):
    """Drafts stream as NDJSON: a compressor in the middle would hold tokens back."""
    response = client.get("/api/v1/session", headers={"Accept-Encoding": "gzip", "Authorization": "Bearer nope"})
    assert "content-encoding" not in response.headers
