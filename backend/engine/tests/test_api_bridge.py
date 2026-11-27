from __future__ import annotations

from fastapi.testclient import TestClient

from app import main, research, storage, security


def api():
    client = TestClient(main.app)
    client.__enter__()
    token = security.ensure_token()
    return client, {"Authorization": f"Bearer {token}"}, token


def test_auth_is_required_and_the_remote_settings_surface_is_gone():
    """`/settings*` accepted a remote base_url and api_key for a remote model
    client that would have POSTed post text and the user's voice examples to
    that host. Nothing ever instantiated it, so the whole surface was deleted
    rather than deprecated — an exfiltration path that no longer exists cannot
    come back by accident."""
    client, headers, _ = api()
    try:
        assert client.get("/livez").status_code == 404, "closed, and does not admit it exists"
        assert client.get("/livez", headers=headers).status_code == 200
        for path in ("/settings", "/settings/verify"):
            assert client.post(path, headers=headers, json={}).status_code == 404
            assert client.get(path, headers=headers).status_code == 404
    finally:
        client.__exit__(None, None, None)


def test_the_token_is_never_served_over_http():
    """`POST /bootstrap` handed the persistent full-privilege bearer token to
    ANY local caller — no auth, no rate limit, never rotating. The route is
    gone, and the engine has no CORS at all now: only the gateway, which
    already holds the token, calls it.

    Asserted here rather than deleted so the endpoint cannot quietly return.
    """
    client, _headers, token = api()
    origin = "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    try:
        for headers in ({}, {"Origin": origin}):
            response = client.post("/bootstrap", headers=headers)
            assert response.status_code == 404, "the token endpoint must stay gone"
            assert token not in response.text

        preflight = client.options("/livez", headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
        })
        assert preflight.status_code == 404
        assert "access-control-allow-origin" not in preflight.headers
    finally:
        client.__exit__(None, None, None)


def test_the_events_socket_is_gone():
    """Asserted rather than deleted so a token-in-the-URL endpoint cannot
    quietly return."""
    from starlette.routing import WebSocketRoute
    paths = [r.path for r in main.app.routes if isinstance(r, WebSocketRoute)]
    assert "/events" not in paths


def test_generation_feedback_and_outcome_lifecycle():
    """The goal, campaign, experiment, knowledge and strategy routes this test
    used to walk through are gone. What survives is the lifecycle a draft goes
    through once it exists: edited, inserted, then recorded as posted."""
    client, headers, _ = api()
    text = "The useful test is whether the workflow changes a real decision."
    generation_id = storage.create_generation(
        {"action": "post", "context": {"author": "A", "text": "What are AI agents?", "visibility": "public"}},
        {"objective": "conversation"},
    )
    storage.save_generation_candidates(generation_id, [{"id": "lifecycle-candidate", "text": text}])
    try:
        storage.save_feedback("lifecycle-candidate", "edited", text + " Edited.", None, generation_id)
        storage.save_feedback("lifecycle-candidate", "inserted", None, None, generation_id)
        outcome = client.post("/outcomes/record", headers=headers, json={
            "generation_id": generation_id, "candidate_id": "lifecycle-candidate",
            "status": "posted", "final_text": text + " Edited.", "published_post_id": "999"})
        assert outcome.status_code == 200
        assert outcome.json()["lifecycle_status"] == "posted"
        generation = storage.get_generation(generation_id)
        assert [event["event"] for event in generation["events"]][-3:] == ["edited", "inserted", "posted"]
        assert generation["published_post_id"] == "999"
        assert generation["final_text"] == text + " Edited."
    finally:
        client.__exit__(None, None, None)


def test_the_offline_strategy_answer_never_promises_virality():
    """`/strategy/chat` is gone. Its offline answer is still the chat fallback
    when the model returns nothing, and it must refuse to promise reach."""
    from app.strategy import local_strategy_answer
    assert "guarantee" in local_strategy_answer("Can you guarantee this goes viral?").lower()


def test_image_feedback_is_a_separate_explicit_preference_signal():
    client, headers, _ = api()
    try:
        used = client.post("/images/feedback", headers=headers, json={
            "choice": "used", "metadata": {"visual_job": "show a comparison"},
        })
        skipped = client.post("/images/feedback", headers=headers, json={
            "choice": "skipped", "metadata": {"visual_job": "show a comparison"},
        })
        assert used.status_code == 200 and used.json()["choice"] == "used"
        assert skipped.status_code == 200 and skipped.json()["choice"] == "skipped"
        # Draft acceptance and image preference stay distinct: both choices
        # remain available to the visual-habit calculation.
        summary = storage.image_feedback_summary()
        assert summary["used"] == 1 and summary["skipped"] == 1
    finally:
        client.__exit__(None, None, None)


def test_snapshot_routes_and_research_degrade_explicitly():
    client, headers, _ = api()
    try:
        profile = client.post("/snapshots/profile", headers=headers, json={"handle": "starling", "display_name": "Example Builder",
            "bio": "Useful AI notes", "followers_count": 10, "source": "extension"})
        assert profile.status_code == 200 and profile.json()["bio"] == "Useful AI notes"
        metric = client.post("/snapshots/metrics", headers=headers, json={"post_id": "p1", "impressions": 10, "source": "extension"})
        assert metric.status_code == 200
        assert storage.list_profile_snapshots()[0]["display_name"] == "Example Builder"

        # The `/knowledge/research` route is gone; the research call behind it
        # still degrades explicitly instead of inventing an answer.
        answer = research.research("What changed today?")
        assert answer["status"] == "unavailable"
        assert answer["limitation"]
    finally:
        client.__exit__(None, None, None)


def test_visible_metric_metadata_backfills_post_and_matches_inserted_draft():
    client, headers, _ = api()
    generation_id = storage.create_generation(
        {"action": "post", "context": {"author": "You", "text": "A source", "visibility": "public"}},
        {"objective": "reach"},
    )
    storage.save_generation_candidates(generation_id, [{"id": "visible-candidate", "text": "Make the useful choice obvious."}])
    storage.save_feedback("visible-candidate", "inserted", None, None, generation_id)
    try:
        response = client.post("/snapshots/metrics", headers=headers, json={
            "post_id": "visible-post",
            "captured_at": "2026-07-15T13:00:00+00:00",
            "followers_count": 250,
            "impressions": 1200,
            "source": "extension",
            "metadata": {
                "post_text": "  make the useful choice obvious.  ",
                "post_url": "https://x.com/starling/status/visible-post",
                "post_type": "original",
                "created_at": "2026-07-15T12:30:00+00:00",
                "visibility": "public",
                "media_count": 1,
                "has_image": True,
            },
        })
        assert response.status_code == 200
        assert response.json()["matched_generation_id"] == generation_id
        assert response.json()["metrics"] == {"impressions": 1200.0}

        post = storage.get_post("visible-post")
        assert post["text"] == "  make the useful choice obvious.  "
        assert post["post_type"] == "post"
        assert post["post_url"].endswith("/visible-post")
        assert post["visibility"] == "public"
        assert post["media_count"] == 1 and post["has_image"] == 1
        assert storage.get_generation(generation_id)["lifecycle_status"] == "posted"
    finally:
        client.__exit__(None, None, None)


def test_visible_own_reply_snapshot_preserves_its_exact_parent_identity():
    client, headers, _ = api()
    try:
        response = client.post("/snapshots/metrics", headers=headers, json={
            "post_id": "my-visible-reply", "captured_at": "2026-07-22T13:00:00+00:00",
            # A just-posted reply can have no engagement labels at all. Its
            # visible own-post identity must still persist and feed the exact
            # duplicate-reply guard.
            "source": "extension",
            "metadata": {
                "is_own_post": True, "author_handle": "me",
                "post_text": "An existing reply.", "post_url": "https://x.com/me/status/my-visible-reply",
                "post_type": "reply", "parent_post_id": "source-post",
                "parent_text": "The source text", "parent_author": "Source",
                "parent_post_url": "https://x.com/source/status/source-post",
                "visibility": "public",
            },
        })
        assert response.status_code == 200
        assert response.json()["metrics"] == {}
        stored = storage.get_post("my-visible-reply")
        assert stored["parent_post_id"] == "source-post"
        assert storage.find_recorded_reply_to_post("source-post")["post_id"] == "my-visible-reply"
    finally:
        client.__exit__(None, None, None)


def _own_post_snapshot(post_id: str, handle: str | None, text: str) -> dict:
    metadata = {"is_own_post": True, "post_text": text,
                "post_url": f"https://x.com/whoever/status/{post_id}",
                "post_type": "post", "visibility": "public"}
    if handle is not None:
        metadata["author_handle"] = handle
    return {"post_id": post_id, "captured_at": "2026-08-04T16:23:37+00:00",
            "source": "extension", "metadata": metadata}


def test_a_post_by_someone_else_never_enters_his_archive():
    """`is_own_post` is decided in the content script and was believed outright.

    It was wrong at least twice on real data: two posts by a different account
    ended up in `posts`, which is the corpus the voice model, the pillars and
    the size planner all learn from. Their handle was in the same payload the
    whole time.
    """
    client, headers, _ = api()
    try:
        storage.set_account_profile({"handle": "example_builder"})

        # Someone else's post, claiming to be his. The handle only differs by
        # a trailing letter, so a loose comparison would let it through.
        assert client.post("/snapshots/metrics", headers=headers,
                           json=_own_post_snapshot("foreign-1", "example_builders",
                                                   "That's crazy who knew that?"),
                           ).status_code == 200
        assert storage.get_post("foreign-1") is None, \
            "another account's writing is being learned as his voice"

        # His own post still lands, and the handle compare is case/@-insensitive.
        assert client.post("/snapshots/metrics", headers=headers,
                           json=_own_post_snapshot("mine-1", "@Example_Builder",
                                                   "Mine, capitalised differently."),
                           ).status_code == 200
        assert storage.get_post("mine-1")["text"] == "Mine, capitalised differently."

        # An older extension sends no handle at all. Trusting `is_own_post` there
        # is the only option, and refusing it would stop learning entirely.
        assert client.post("/snapshots/metrics", headers=headers,
                           json=_own_post_snapshot("legacy-1", None, "No handle sent."),
                           ).status_code == 200
        assert storage.get_post("legacy-1")["text"] == "No handle sent."
    finally:
        client.__exit__(None, None, None)


def test_visible_media_only_reply_still_enters_the_duplicate_ledger():
    client, headers, _ = api()
    try:
        response = client.post("/snapshots/metrics", headers=headers, json={
            "post_id": "my-image-reply", "captured_at": "2026-07-22T13:05:00+00:00",
            "source": "extension",
            "metadata": {
                "is_own_post": True, "author_handle": "me",
                "post_url": "https://x.com/me/status/my-image-reply",
                "post_type": "reply", "parent_post_id": "image-source",
                "parent_post_url": "https://x.com/source/status/image-source",
                "media_count": 1, "has_image": True, "visibility": "public",
            },
        })
        assert response.status_code == 200
        stored = storage.get_post("my-image-reply")
        assert stored["text"] == "" and stored["parent_post_id"] == "image-source"
        assert storage.find_recorded_reply_to_post("image-source")["post_id"] == "my-image-reply"
    finally:
        client.__exit__(None, None, None)
