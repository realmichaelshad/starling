"""The three endpoints settings talks to, including what they refuse."""
import pytest
from fastapi.testclient import TestClient

from app import own_material as om, security
from app.main import app


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def auth():
    return {"Authorization": f"Bearer {security.ensure_token()}"}


@pytest.fixture(autouse=True)
def clean_table():
    from app.storage import connect
    with connect() as conn:
        om._ensure_table(conn)
        conn.execute("DELETE FROM own_material")
    yield
    with connect() as conn:
        conn.execute("DELETE FROM own_material")


VOICE = "at this rate Grok 4.5 might launch before Opus 5"


def test_add_list_remove(client, auth):
    added = client.post("/own-material/add",
                        json={"kind": "voice", "text": VOICE}, headers=auth)
    assert added.status_code == 200, added.text
    item_id = added.json()["item"]["id"]
    assert added.json()["counts"]["voice"] == 1

    listed = client.post("/own-material/list", json={}, headers=auth)
    assert listed.status_code == 200
    assert [item["text"] for item in listed.json()["items"]] == [VOICE]
    assert listed.json()["limits"]["min_voice_words"] == om.MIN_VOICE_WORDS

    removed = client.post("/own-material/remove", json={"id": item_id}, headers=auth)
    assert removed.status_code == 200
    assert removed.json()["counts"]["voice"] == 0


def test_the_refusal_reason_survives_to_the_card(client, auth):
    """A 400 whose detail is "Bad Request" is a shrug on screen."""
    response = client.post("/own-material/add",
                           json={"kind": "voice", "text": "too short"},
                           headers=auth)
    assert response.status_code == 400
    assert "at least" in response.json()["detail"]


def test_an_unknown_kind_is_rejected_by_the_schema(client, auth):
    """There is deliberately no kind that may be asserted as fact."""
    response = client.post("/own-material/add",
                           json={"kind": "fact", "text": "I run a 4090"},
                           headers=auth)
    assert response.status_code == 422


def test_removing_something_twice_says_so(client, auth):
    added = client.post("/own-material/add",
                        json={"kind": "background", "text": "no crypto posts"},
                        headers=auth).json()
    client.post("/own-material/remove", json={"id": added['item']['id']}, headers=auth)
    again = client.post("/own-material/remove", json={"id": added['item']['id']}, headers=auth)
    assert again.status_code == 404


def test_it_needs_the_token(client):
    """404, not 401 — and that is the companion's rule, not an accident.

    Every route answers an unauthenticated caller with 404, `/livez`
    included, so a caller that finds the port cannot learn which endpoints
    exist. Asserted as "not success" plus the shape the app actually uses, so
    this fails if these three routes ever become the exception.
    """
    assert client.post("/own-material/list", json={}).status_code == 404
    assert client.post("/own-material/add",
                       json={"kind": "voice", "text": VOICE}).status_code == 404
    assert client.post("/own-material/remove", json={"id": "whatever"}).status_code == 404
    assert om.voice_texts() == [], "and nothing was stored on the way"


def test_listing_can_be_filtered_by_kind(client, auth):
    client.post("/own-material/add", json={"kind": "voice", "text": VOICE}, headers=auth)
    client.post("/own-material/add",
                json={"kind": "background", "text": "bearish on agents"}, headers=auth)
    voice = client.post("/own-material/list", json={"kind": "voice"}, headers=auth).json()["items"]
    assert [item["text"] for item in voice] == [VOICE]
