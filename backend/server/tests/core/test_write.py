"""The writing page for phones (web/write.py): sign-in, consent, CSRF, drafting through the
extension's metered path against the fake engine, refusals, links to posts on X, writing samples,
and the installable app's manifest and service worker.

Pages use the core suite's stand-in templates (they print the context keys the gateway sends);
`test_real_templates.py` renders the real ones. Drafts run through the real usage service, the
real account slot and a real engine pool whose engines are `tests/engines/fake_engine.py`.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

import pytest
from fastapi.testclient import TestClient

from starling_server.app import create_app
from starling_server.billing import entitlements
from starling_server.config import DEFAULT_WEB_DIR
from starling_server.security import clock
from starling_server.services.world.x_post import snowflake_time
from starling_server.usage import service as usage_service
from starling_server.web import write

from ..conftest import PUBLIC_URL
from ..engines.support import make_pool, served, wait_until
from . import helpers as h

STATUS_ID = "1851234567890123456"
POST_LINK = f"https://x.com/dana_example/status/{STATUS_ID}?s=20&t=abc"
POST_WORDS = "Shipping beats polishing. Ten small releases taught us more than one big one."


@pytest.fixture
def app(settings):
    application = create_app(settings)
    # A pool placed before start-up is started instead of replaced (engines/lifecycle.py): its
    # engines are the fake engine, so nothing here calls an AI provider.
    application.state.engine_pool = make_pool(settings, application.state.db)
    application.state.admin_background_jobs = False
    return application


@pytest.fixture
def client(app):
    with TestClient(app, base_url=PUBLIC_URL) as test_client:
        yield test_client


@pytest.fixture
def engine_bodies(app, client):
    """Every request the gateway sends to an engine: (path, decoded JSON body)."""
    pool = app.state.engine_pool
    original = pool.request
    seen: list[tuple[str, dict]] = []

    async def recording(account_id, tenant_seq, method, path, body, headers, timeout_s):
        seen.append((path, json.loads(body or b"{}")))
        return await original(account_id, tenant_seq, method, path, body, headers, timeout_s)

    pool.request = recording
    return seen


@pytest.fixture
def ai_call_stand_in(app, client):
    """The services proxy is not in these tests: record one successful AI call for every request the
    gateway starts, so a usable draft is charged as it is in production (CONTRACTS 7)."""
    app.state.db.write(lambda conn: conn.execute(
        "CREATE TRIGGER stand_in_ai_call AFTER UPDATE OF state ON usage_reservations "
        "WHEN NEW.state = 'active' AND OLD.state = 'reserved' BEGIN "
        "INSERT INTO ai_calls (account_id, reservation_id, bucket, lane, model, status, started_at, finished_at) "
        "VALUES (NEW.account_id, NEW.id, 'press', 'quality', 'test-model', 'ok', 0, 0); END"))


def sign_in(client, email: str = "writer@example.com", *, verified: bool = True):
    h.create_user(client, email, verified=verified)
    return h.account_row(client.app, email)


def token(client) -> str:
    return h.page_token(client)


def agree(client, next_path: str = "/write"):
    return h.post_form(client, "/write/consent", {"agree": "yes", "next": next_path})


def draft(client, body: dict, *, key: str | None = None, csrf: str | None = None, origin: str | None = PUBLIC_URL,
          extra: dict | None = None):
    headers = {"Accept": "application/json", "Idempotency-Key": key or str(uuid.uuid4()),
               "X-CSRF-Token": token(client) if csrf is None else csrf, "X-Starling-Time-Zone": "Europe/London"}
    if origin:
        headers["Origin"] = origin
    headers.update(extra or {})
    return client.post("/write/draft", json=body, headers=headers)


def reservations(app, account_id: str) -> list[dict]:
    with app.state.db.read() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM usage_reservations WHERE account_id = ? ORDER BY created_at, rowid", (account_id,))]


def commits(app, account_id: str) -> int:
    with app.state.db.read() as conn:
        return conn.execute("SELECT COUNT(*) FROM credit_ledger WHERE account_id = ? AND kind = 'commit'",
                            (account_id,)).fetchone()[0]


def fake_engine_script(monkeypatch, **fake):
    """Steer the fake engine (tests/engines/fake_engine.py reads a `fake` object in the body)."""
    original = write.build_request

    def scripted(payload):
        engine, target = original(payload)
        return {**engine, "fake": fake}, target

    monkeypatch.setattr(write, "build_request", scripted)


# --------------------------------------------------------------------------- signed out, consent, CSRF


def test_signed_out_visitors_are_sent_to_sign_in_and_back(client):
    page = client.get("/write", params={"text": POST_LINK}, follow_redirects=False)
    assert page.status_code == 303
    target = unquote(parse_qs(urlsplit(page.headers["location"]).query)["next"][0])
    assert target.startswith("/write?text=") and STATUS_ID in target
    voice = client.get("/write/voice", follow_redirects=False)
    assert voice.status_code == 303 and voice.headers["location"] == "/login?next=" + quote("/write/voice", safe="")
    refused = client.post("/write/draft", json={"kind": "reply", "text": "x"},
                          headers={"Accept": "application/json", "Origin": PUBLIC_URL, "Idempotency-Key": str(uuid.uuid4())})
    assert refused.status_code in (401, 403) and refused.json()["error"]["code"] in ("signed_out", "csrf_failed")


def test_signing_in_comes_back_to_the_shared_post(client):
    h.create_user(client, "share@example.com")
    client.post("/logout", data={"csrf_token": token(client)}, headers={"Origin": PUBLIC_URL})
    response = h.login(client, "share@example.com", next_path="/write?text=" + quote(POST_LINK, safe=""))
    assert response.status_code == 303 and response.headers["location"].startswith("/write?text=")


def test_the_page_shows_the_account_numbers_and_asks_for_consent_once(app, client):
    account = sign_in(client)
    page = client.get("/write")
    assert page.status_code == 200
    assert h.context_value(page, "consent") == {"given": False, "granted_at": None,
                                                "version": write.WEB_DISCLOSURE_VERSION}
    assert h.context_value(page, "disclosure") == write.DISCLOSURE
    assert h.context_value(page, "state") == h.context_value(client.get("/account"), "state")

    response = agree(client)
    assert response.status_code == 303 and response.headers["location"] == "/write"
    with app.state.db.read() as conn:
        rows = [dict(row) for row in conn.execute("SELECT kind, version, passive_learning, revoked_at FROM consents "
                                                  "WHERE account_id = ?", (account["id"],))]
    assert rows == [{"kind": "web_disclosure", "version": write.WEB_DISCLOSURE_VERSION, "passive_learning": 0,
                     "revoked_at": None}], "recorded beside the extension's consent, never as it"
    consent = h.context_value(client.get("/write"), "consent")
    assert consent["given"] is True and consent["granted_at"] > 0
    # The extension's own disclosure is untouched: the account page still says it is not given.
    assert h.context_value(client.get("/account"), "state")["consent"]["accepted_version"] is None

    withdrawn = h.post_form(client, "/write/consent/withdraw", {})
    assert withdrawn.status_code == 303 and withdrawn.headers["location"] == "/write"
    assert h.context_value(client.get("/write"), "consent")["given"] is False


def test_agreeing_returns_only_to_the_writing_pages(client):
    sign_in(client)
    shared = "/write?text=" + quote(POST_LINK, safe="")
    assert agree(client, shared).headers["location"] == shared
    assert agree(client, "/write/voice").headers["location"] == "/write/voice"
    for elsewhere in ("https://evil.example/", "//evil.example/write", "/account/delete", "/writer"):
        assert agree(client, elsewhere).headers["location"] == "/write"
    assert h.post_form(client, "/write/consent", {"next": "/write"}).status_code == 400, "only the Agree button agrees"
    assert h.post_form(client, "/write/consent", {"agree": "yes"}, token="forged").status_code == 403


def test_a_share_from_another_app_fills_the_box(client):
    sign_in(client)
    page = client.get("/write", params={"title": "", "text": f"Look at this {POST_LINK}", "url": POST_LINK})
    prefill = h.context_value(page, "prefill")
    assert prefill["text"] == f"Look at this {POST_LINK}", "the url was already in the text: not repeated"
    assert prefill["link"] == f"https://x.com/dana_example/status/{STATUS_ID}" and prefill["shared"] is True
    assert h.context_value(page, "next_path").startswith("/write?")
    separate = write.prefill_from_share({"title": "A post", "text": "Words of the post", "url": POST_LINK})
    assert separate["text"] == f"A post\nWords of the post\n{POST_LINK}"
    assert write.prefill_from_share({})["text"] == "" and write.prefill_from_share({})["shared"] is False


def test_drafting_needs_consent_csrf_and_a_same_origin_request(app, client, engine_bodies):
    account = sign_in(client)
    body = {"kind": "reply", "text": POST_WORDS}
    no_consent = draft(client, body)
    assert no_consent.status_code == 403 and no_consent.json()["error"]["code"] == "consent_required"
    agree(client)
    no_token = draft(client, body, csrf="")
    assert no_token.status_code == 403 and no_token.json()["error"]["code"] == "csrf_failed"
    other_site = draft(client, body, origin="https://evil.example")
    assert other_site.status_code == 403 and other_site.json()["error"]["code"] == "csrf_failed"
    assert reservations(app, account["id"]) == [] and engine_bodies == []


def test_unconfirmed_accounts_cannot_draft(app, client):
    account = sign_in(client, "unconfirmed@example.com", verified=False)
    assert h.context_value(client.get("/write"), "state")["account"]["email_verified"] is False
    app.state.db.write(lambda conn: conn.execute(
        "INSERT INTO consents (account_id, kind, version, passive_learning, granted_at) VALUES (?, ?, ?, 0, ?)",
        (account["id"], write.WEB_CONSENT_KIND, write.WEB_DISCLOSURE_VERSION, clock.now())))
    response = draft(client, {"kind": "reply", "text": POST_WORDS})
    assert response.status_code == 403 and response.json()["error"]["code"] == "email_unverified"


# --------------------------------------------------------------------------- drafting


def test_a_reply_goes_through_the_extensions_metered_path(app, client, engine_bodies, ai_call_stand_in, settings):
    account = sign_in(client)
    agree(client)
    key = str(uuid.uuid4())
    body = {"kind": "reply", "text": f"{POST_LINK}\n{POST_WORDS} More at https://example.com/notes.",
            "author": "", "author_name": "Dana Example", "lang": "en"}
    response = draft(client, body, key=key)
    assert response.status_code == 200, response.text
    page = response.json()
    assert page["ok"] is True and page["draft"] == "A usable draft." and page["kind"] == "reply"
    assert page["usage"]["used"] == 1 and page["usage"]["remaining"] == 24
    handoff = urlsplit(page["handoff"]["url"])
    assert page["handoff"]["label"] == "Reply on X"
    assert (handoff.scheme, handoff.netloc, handoff.path) == ("https", "x.com", "/intent/post")
    assert parse_qs(handoff.query) == {"in_reply_to": [STATUS_ID], "text": ["A usable draft."]}

    path, engine = engine_bodies[-1]
    assert path == "/browser/assist", "the one-shot route the extension's ✦ uses, into the composer pipeline"
    assert engine["mode"] == "enhance_composer"
    assert engine["composer"] == {"action": "reply", "draft_text": "", "intent": "create"}
    context = engine["context"]
    assert context["text"] == f"{POST_WORDS} More at https://example.com/notes."
    assert context["author"] == "Dana Example" and context["author_handle"] == "dana_example"
    assert context["post_id"] == STATUS_ID and context["post_url"] == f"https://x.com/dana_example/status/{STATUS_ID}"
    assert context["links"] == ["https://example.com/notes"] and context["language"] == "en"
    assert context["visibility"] == "unknown" and context["media"] == []
    assert context["post_created_at"].startswith("20")
    assert "second_reply_ok" not in engine and "page_url" not in context

    row = reservations(app, account["id"])[-1]
    assert (row["route"], row["mode"], row["state"]) == ("/write", "enhance_composer", "committed")
    assert served(settings, account["tenant_seq"])[-1]["request_id"] == row["id"]
    assert served(settings, account["tenant_seq"])[-1]["time_zone"] == "Europe/London"
    state = h.context_value(client.get("/account"), "state")
    assert (state["usage"]["used"], state["usage"]["remaining"]) == (1, 24), "the account page shows the same numbers"

    pending = client.get(f"/write/requests/{key}", headers={"Accept": "application/json"})
    assert pending.status_code == 200 and pending.json() == {"request_id": key, "state": "done"}
    replay = draft(client, body, key=key)
    assert replay.status_code == 200 and replay.json()["draft"] == "A usable draft."
    assert commits(app, account["id"]) == 1 and len(engine_bodies) == 1, "collected again, not charged again"
    missing = client.get(f"/write/requests/{uuid.uuid4()}", headers={"Accept": "application/json"})
    assert missing.status_code == 404


def test_a_quote_and_a_new_post(app, client, engine_bodies, ai_call_stand_in):
    sign_in(client)
    agree(client)
    quote_page = draft(client, {"kind": "quote", "text": POST_WORDS, "link": POST_LINK, "author": "@dana_example"}).json()
    assert quote_page["handoff"]["label"] == "Quote on X"
    assert parse_qs(urlsplit(quote_page["handoff"]["url"]).query) == {
        "url": [f"https://x.com/dana_example/status/{STATUS_ID}"], "text": ["A usable draft."]}
    assert engine_bodies[-1][1]["composer"]["action"] == "quote"

    post_page = draft(client, {"kind": "post", "text": "  why beginners\n should track sleep "}).json()
    assert post_page["handoff"]["label"] == "Post on X"
    assert parse_qs(urlsplit(post_page["handoff"]["url"]).query) == {"text": ["A usable draft."]}
    engine = engine_bodies[-1][1]
    assert engine == {"mode": "enhance_composer", "composer": {"action": "post", "draft_text": "", "intent": "create",
                                                               "topic": "why beginners should track sleep"}}
    draft(client, {"kind": "post", "text": ""})
    assert engine_bodies[-1][1]["composer"] == {"action": "post", "draft_text": "", "intent": "create"}, \
        "no subject: Starling picks one from the writer's beat, as the extension's Create post does"


def test_a_reply_without_the_posts_id_has_no_hand_off_but_can_be_copied(client, ai_call_stand_in):
    sign_in(client)
    agree(client)
    page = draft(client, {"kind": "reply", "text": POST_WORDS, "author": "Dana Example"}).json()
    assert page["draft"] == "A usable draft." and page["handoff"] is None


def test_engine_refusals_are_explained_and_never_charged(app, client, monkeypatch, ai_call_stand_in):
    account = sign_in(client)
    agree(client)
    body = {"kind": "reply", "text": POST_WORDS}
    fake_engine_script(monkeypatch, script="refused", code="ai_quota")
    unavailable = draft(client, body)
    assert unavailable.status_code == 503 and unavailable.json()["error"]["code"] == "ai_unavailable"
    fake_engine_script(monkeypatch, script="refused", code="budget_exhausted", retry_after_s=600)
    paused = draft(client, body)
    assert paused.status_code == 503 and paused.json()["error"]["code"] == "service_paused"
    assert commits(app, account["id"]) == 0
    assert [row["state"] for row in reservations(app, account["id"])] == ["released", "released"]
    def recorded() -> set[str]:
        with app.state.db.read() as conn:
            return {row["code"] for row in conn.execute(
                "SELECT code FROM request_errors WHERE route = '/write/draft' AND account_id = ?", (account["id"],))}

    # Failures are counted off the request path (db/request_errors.py), so the rows land a moment later.
    assert wait_until(lambda: recorded() == {"ai_unavailable", "service_paused"}, timeout=5), recorded()


def test_a_draft_starling_holds_back_is_a_message_not_an_error(app, client, monkeypatch, ai_call_stand_in):
    account = sign_in(client)
    agree(client)
    fake_engine_script(monkeypatch, script="empty")
    response = draft(client, {"kind": "reply", "text": POST_WORDS})
    assert response.status_code == 200
    page = response.json()
    assert page["draft"] is None and page["message"] and page["can_write_anyway"] is False
    assert commits(app, account["id"]) == 0 and reservations(app, account["id"])[-1]["state"] == "released"


def test_an_engine_that_breaks_is_ai_unavailable(app, client, monkeypatch):
    account = sign_in(client)
    agree(client)
    fake_engine_script(monkeypatch, script="error_status")
    response = draft(client, {"kind": "reply", "text": POST_WORDS})
    assert response.status_code == 503 and response.json()["error"]["code"] == "ai_unavailable"
    assert reservations(app, account["id"])[-1]["release_reason"] == "engine_error"


def test_the_plan_allowance_and_rate_limits_apply(app, client, settings, ai_call_stand_in, fake_clock):
    account = sign_in(client)
    agree(client)
    body = {"kind": "reply", "text": POST_WORDS}
    for _ in range(3):  # the Free plan's three a minute
        assert draft(client, body).status_code == 200
    limited = draft(client, body)
    assert limited.status_code == 429 and limited.json()["error"]["code"] == "rate_limited"
    assert int(limited.headers["retry-after"]) > 0

    fake_clock.advance(120)
    period = entitlements.current_entitlement(app.state.db, settings, account["id"], clock.now()).period_id
    app.state.db.write(lambda conn: conn.execute(
        "UPDATE usage_counters SET committed = 25 WHERE account_id = ? AND period_id = ?", (account["id"], period)))
    spent = draft(client, body)
    assert spent.status_code == 402
    error = spent.json()["error"]
    assert error["code"] == "limit_reached" and error["usage"]["remaining"] == 0


def test_bad_requests_say_what_to_fix(app, client):
    account = sign_in(client)
    agree(client)
    cases = [
        ({"kind": "thread", "text": POST_WORDS}, "Choose Reply, Quote or New post."),
        ({"kind": "reply", "text": "   "}, "Paste the post you're answering."),
        ({"kind": "reply", "text": POST_LINK}, "Paste the post's words too. X's link alone is not enough."),
        ({"kind": "post", "text": "x" * 501}, "Keep what it is about under 500 characters."),
        ({"kind": "reply", "text": POST_WORDS, "author": "a" * 101}, "That author name is too long."),
    ]
    for body, message in cases:
        response = draft(client, body)
        assert response.status_code == 400 and response.json()["error"] == {"code": "bad_request", "message": message}
    no_key = client.post("/write/draft", json={"kind": "reply", "text": POST_WORDS},
                         headers={"Accept": "application/json", "Origin": PUBLIC_URL, "X-CSRF-Token": token(client)})
    assert no_key.status_code == 400
    assert reservations(app, account["id"]) == []
    plain = h.post_form(client, "/write/draft", {"kind": "reply", "text": POST_WORDS})
    assert plain.status_code == 303 and plain.headers["location"] == "/write", "without JavaScript: back to the page"


def test_write_one_anyway_is_the_extensions_second_reply_override():
    engine, _ = write.build_request({"kind": "reply", "text": POST_WORDS, "again": True})
    assert engine["second_reply_ok"] is True
    engine, _ = write.build_request({"kind": "quote", "text": POST_WORDS, "again": True})
    assert "second_reply_ok" not in engine


def test_what_a_pasted_post_honestly_provides():
    engine, target = write.build_request({"kind": "reply", "text": f"see https://twitter.com/i/web/status/{STATUS_ID} {POST_WORDS}",
                                          "author": "@Some_One"})
    context = engine["context"]
    assert context["text"] == f"see {POST_WORDS}" and context["author_handle"] == "Some_One" and context["author"] == ""
    assert target == {"kind": "reply", "status_id": STATUS_ID, "url": f"https://x.com/i/status/{STATUS_ID}"}
    engine, target = write.build_request({"kind": "reply", "text": "Just words https://pic.x.com/abc https://x.com/home"})
    assert engine["context"]["links"] == [] and "post_id" not in engine["context"] and target["status_id"] is None
    created = snowflake_time(STATUS_ID)
    assert created is not None
    engine, _ = write.build_request({"kind": "reply", "text": POST_WORDS, "link": POST_LINK, "lang": "not a language!"})
    assert "language" not in engine["context"]


def test_the_hand_off_link_is_encoded_and_bounded():
    text = "Line one & two #tag 100%\nSecond line ✦ é"
    link = write.handoff("post", text, None, None)
    assert link["url"].startswith("https://x.com/intent/post?text=")
    assert "%0A" in link["url"] and "%26" in link["url"] and "%23" in link["url"] and " " not in link["url"]
    assert parse_qs(urlsplit(link["url"]).query)["text"] == [text]
    assert write.handoff("reply", text, None, None) is None and write.handoff("quote", text, STATUS_ID, None) is None
    assert write.handoff("post", "x" * 9000, None, None) is None, "too long for an address: Copy remains"


# --------------------------------------------------------------------------- links to posts on X


def test_a_link_is_read_through_the_services_process(app, client, monkeypatch):
    sign_in(client)
    asked: list[str] = []

    async def read_x_post(settings, url, *, transport=None):
        asked.append(url)
        return {"url": f"https://x.com/dana_example/status/{STATUS_ID}", "status_id": STATUS_ID,
                "author_name": "Dana Example", "author_handle": "dana_example", "text": POST_WORDS, "lang": "en",
                "links": [], "has_media": False, "cached": False}

    monkeypatch.setattr(write.services_client, "read_x_post", read_x_post)
    headers = {"Accept": "application/json", "Origin": PUBLIC_URL, "X-CSRF-Token": token(client)}
    before_consent = client.post("/write/link", json={"url": POST_LINK}, headers=headers)
    assert before_consent.status_code == 403 and asked == [], "nothing is sent anywhere before consent"
    agree(client)
    headers["X-CSRF-Token"] = token(client)
    response = client.post("/write/link", json={"url": f"look {POST_LINK}"}, headers=headers)
    assert response.status_code == 200
    assert response.json() == {"ok": True, "post": {
        "url": f"https://x.com/dana_example/status/{STATUS_ID}", "status_id": STATUS_ID, "author_name": "Dana Example",
        "author_handle": "dana_example", "text": POST_WORDS, "lang": "en", "has_media": False}}
    assert asked == [f"https://x.com/dana_example/status/{STATUS_ID}"], "only the post's own address is passed on"
    not_a_post = client.post("/write/link", json={"url": "https://example.com/a"}, headers=headers)
    assert not_a_post.status_code == 400
    assert client.post("/write/link", json={"url": POST_LINK}, headers={**headers, "X-CSRF-Token": ""}).status_code == 403


@pytest.mark.parametrize("answer, message", [
    ({"ok": False, "error": {"code": "post_unavailable", "message": "x"}}, "X does not show that post outside X. Paste its text instead."),
    ({"ok": False, "error": {"code": "services_unreachable", "message": "x"}}, "Couldn't read that post. Paste its text instead."),
    ({"ok": False, "error": {"code": "fetch_timeout", "message": "x"}}, "Couldn't read that post. Paste its text instead."),
])
def test_a_link_that_cannot_be_read_keeps_the_link(client, monkeypatch, answer, message):
    sign_in(client)
    agree(client)

    async def read_x_post(settings, url, *, transport=None):
        return answer

    monkeypatch.setattr(write.services_client, "read_x_post", read_x_post)
    response = client.post("/write/link", json={"url": POST_LINK},
                           headers={"Accept": "application/json", "Origin": PUBLIC_URL, "X-CSRF-Token": token(client)})
    assert response.status_code == 200
    assert response.json() == {"ok": False, "link": f"https://x.com/dana_example/status/{STATUS_ID}",
                               "status_id": STATUS_ID, "message": message}


def test_link_lookups_are_limited_per_account(client, monkeypatch):
    sign_in(client)
    agree(client)

    async def read_x_post(settings, url, *, transport=None):
        return {"ok": False, "error": {"code": "post_unavailable", "message": "x"}}

    monkeypatch.setattr(write.services_client, "read_x_post", read_x_post)
    headers = {"Accept": "application/json", "Origin": PUBLIC_URL, "X-CSRF-Token": token(client)}
    statuses = [client.post("/write/link", json={"url": POST_LINK}, headers=headers).status_code for _ in range(31)]
    assert statuses[:30] == [200] * 30 and statuses[30] == 429


# --------------------------------------------------------------------------- writing samples


def test_writing_samples_use_the_engines_own_material(app, client, engine_bodies):
    sign_in(client)
    before = client.get("/write/voice")
    assert h.context_value(before, "samples") is None and engine_bodies == [], "no engine before consent"
    agree(client, "/write/voice")
    page = client.get("/write/voice")
    assert page.status_code == 200 and h.context_value(page, "samples") == []
    assert engine_bodies[-1] == ("/own-material/list", {"kind": "voice"})

    added = h.post_form(client, "/write/voice/add", {"text": "Shipping beats polishing, every single time."})
    assert added.status_code == 303 and added.headers["location"] == "/write/voice"
    assert engine_bodies[-1] == ("/own-material/add", {"kind": "voice", "text": "Shipping beats polishing, every single time."})
    removed = h.post_form(client, "/write/voice/a1b2c3d4/remove", {})
    assert removed.status_code == 303 and engine_bodies[-1] == ("/own-material/remove", {"id": "a1b2c3d4"})

    empty = h.post_form(client, "/write/voice/add", {"text": "   "})
    assert empty.status_code == 400 and h.context_value(empty, "errors") == {"text": "Paste one of your own posts first."}
    assert h.post_form(client, "/write/voice/add", {"text": "a forged one"}, token="forged").status_code == 403
    assert h.post_form(client, "/write/voice/bad id!/remove", {}).status_code == 404


def test_the_engines_own_sentence_comes_back_on_the_form(app, client, engine_bodies):
    sign_in(client)
    agree(client, "/write/voice")
    pool = app.state.engine_pool
    recording = pool.request

    async def refusing(account_id, tenant_seq, method, path, body, headers, timeout_s):
        if path == "/own-material/add":
            return 400, json.dumps({"detail": "A voice example needs at least 3 words."}).encode()
        if path == "/own-material/list":
            items = [{"id": "a1b2c3d4", "kind": "voice", "text": "Mine.", "created_at": "2026-10-29T10:00:00+00:00"}]
            return 200, json.dumps({"items": items, "limits": {"max_chars": 2000, "max_items": 200,
                                                               "min_voice_words": 3}}).encode()
        return await recording(account_id, tenant_seq, method, path, body, headers, timeout_s)

    pool.request = refusing
    response = h.post_form(client, "/write/voice/add", {"text": "so true"})
    assert response.status_code == 400
    assert h.context_value(response, "errors") == {"text": "A voice example needs at least 3 words."}
    assert h.context_value(response, "form") == {"text": "so true"}
    assert h.context_value(response, "samples") == [{"id": "a1b2c3d4", "text": "Mine.", "added_at": 1_793_268_000}]


# --------------------------------------------------------------------------- the installable app


def test_the_manifest_makes_an_installable_app_with_a_share_target(client):
    response = client.get("/manifest.webmanifest")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/manifest+json")
    manifest = response.json()
    assert (manifest["name"], manifest["short_name"], manifest["start_url"], manifest["display"]) == \
        ("Starling", "Starling", "/write", "standalone")
    assert manifest["background_color"] == manifest["theme_color"] == "#000000"
    sizes = {(icon["sizes"], icon["purpose"]) for icon in manifest["icons"]}
    assert {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")} <= sizes
    for icon in manifest["icons"]:
        assert (DEFAULT_WEB_DIR / urlsplit(icon["src"]).path.lstrip("/")).is_file(), icon["src"]
    assert manifest["share_target"] == {"action": "/write", "method": "GET",
                                        "params": {"title": "title", "text": "text", "url": "url"}}


def test_the_service_worker_is_served_from_the_root(client, settings):
    target = Path(settings.web_dir) / "static" / "js" / "sw.js"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(DEFAULT_WEB_DIR / "static" / "js" / "sw.js", target)
    response = client.get("/sw.js")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
    assert response.headers["service-worker-allowed"] == "/" and response.headers["cache-control"] == "no-cache"
    assert "/offline" in response.text
    offline = client.get("/offline")
    assert offline.status_code == 200 and "offline.html" in offline.text


def test_robots_keep_the_writing_pages_out_of_search(client):
    robots = client.get("/robots.txt").text
    assert "Disallow: /" in robots  # development blocks everything; production lists /write (system/routes.py)
    from starling_server.system.routes import ROBOTS_DISALLOW

    assert "/write" in ROBOTS_DISALLOW and "/offline" in ROBOTS_DISALLOW


def test_the_disclosure_text_is_pinned_to_its_version():
    digest = hashlib.sha256(write.disclosure_text().encode("utf-8")).hexdigest()
    assert digest == write.WEB_DISCLOSURE_SHA256, (
        "The writing page's disclosure changed: raise WEB_DISCLOSURE_VERSION (everyone is asked again) "
        f"and set WEB_DISCLOSURE_SHA256 to {digest}.")
    assert write.WEB_DISCLOSURE_VERSION != usage_service.CONSENT_KIND


def test_migration_6_keeps_every_consent_and_allows_the_web_kind(tmp_path, monkeypatch):
    import sqlite3

    from starling_server.db import migrations

    conn = sqlite3.connect(tmp_path / "control.db", isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON")
    monkeypatch.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:5])
    assert migrations.apply(conn) == 5
    conn.execute("INSERT INTO accounts (id, tenant_seq, email, email_norm, allowance_key, password_hash, "
                 "password_changed_at, created_at) VALUES ('a', 1, 'a@example.com', 'a@example.com', 'k', 'x', 0, 0)")
    conn.execute("INSERT INTO consents (account_id, kind, version, passive_learning, granted_at, revoked_at) "
                 "VALUES ('a', 'extension_disclosure', '2026-09-22.1', 1, 10, NULL)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO consents (account_id, kind, version, granted_at) VALUES ('a', 'web_disclosure', 'v', 1)")
    monkeypatch.undo()
    assert migrations.apply(conn) == migrations.latest_version() == 6
    conn.execute("INSERT INTO consents (account_id, kind, version, granted_at) VALUES ('a', 'web_disclosure', 'v', 11)")
    rows = conn.execute("SELECT account_id, kind, version, passive_learning, granted_at, revoked_at FROM consents "
                        "ORDER BY kind").fetchall()
    assert rows == [("a", "extension_disclosure", "2026-09-22.1", 1, 10, None), ("a", "web_disclosure", "v", 0, 11, None)]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO consents (account_id, kind, version, granted_at) VALUES ('a', 'anything', 'v', 1)")
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


def test_an_unconfirmed_account_cannot_make_the_server_ask_x(app, client, monkeypatch):
    account = sign_in(client, "unconfirmed-link@example.com", verified=False)
    app.state.db.write(lambda conn: conn.execute(
        "INSERT INTO consents (account_id, kind, version, passive_learning, granted_at) VALUES (?, ?, ?, 0, ?)",
        (account["id"], write.WEB_CONSENT_KIND, write.WEB_DISCLOSURE_VERSION, clock.now())))
    asked: list[str] = []

    async def read_x_post(settings, url, *, transport=None):
        asked.append(url)
        return {}

    monkeypatch.setattr(write.services_client, "read_x_post", read_x_post)
    response = client.post("/write/link", json={"url": POST_LINK},
                           headers={"Accept": "application/json", "Origin": PUBLIC_URL, "X-CSRF-Token": token(client)})
    assert response.status_code == 403 and response.json()["error"]["code"] == "email_unverified" and asked == []
