"""/docs has no page of its own; it opens the first guide."""
import pytest


@pytest.mark.parametrize("path", ["/docs", "/docs/"])
def test_docs_home_opens_getting_started(client, path):
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/docs/getting-started"
