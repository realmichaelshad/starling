"""The personal-learning loop stays explicit, local, and verifiable."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app import main, storage, user_learning
from app.schemas import BrowserAssistRequest, MetricSnapshot
from app.strategy import strategy_brief
from app.schemas import DraftRequest, PostContext


def test_explicit_chat_correction_becomes_the_current_topic_and_draft_subject(monkeypatch):
    memory = user_learning.learn_from_chat(
        "No, I want to write about open-source AI agents.", "chat-1")

    assert memory is not None
    assert memory["topic"] == "open-source AI agents"
    assert memory["explicit_signals"] == 1
    assert main._wants_draft("No, I want to write about open-source AI agents.") is True
    assert main._chat_topic("No, I want to write about open-source AI agents.") == "open-source AI agents"

    # An active user focus takes precedence over the automatic trend chooser.
    monkeypatch.setattr(main, "_topic_candidates", lambda **_kwargs: [])
    context = main._creation_context(BrowserAssistRequest(mode="chat"), "post")
    assert context.text.startswith("Write about: open-source AI agents.")

    brief = strategy_brief(DraftRequest(action="post", context=PostContext(author="You", text="A real idea")))
    assert brief["topic_memory"][0]["topic"] == "open-source AI agents"


def test_explicit_topic_correction_overrides_an_open_unrelated_post(monkeypatch):
    captured = {}
    candidate = SimpleNamespace(text="A draft about local models.", thread_parts=[], rationale=[],
                                score_band=None, growth_score=None)
    monkeypatch.setattr(main, "generate_drafts", lambda request, _llm, *, brief=None: captured.setdefault("request", request) and [candidate])
    monkeypatch.setattr(main, "finish_strategy_chat", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "_model_payload", lambda *_args, **_kwargs: {})

    # An unrelated page can remain open while the user pivots their standalone
    # idea. The new topic must win over that stale visible context.
    open_context = PostContext(author="Someone else", text="A post about cloud pricing")
    main._chat_write_request(
        BrowserAssistRequest(mode="chat", context=open_context),
        "No, I want to write about local AI models.", "chat-override", object(), {}, 0.0)

    assert captured["request"].context.text.startswith("Write about: local AI models.")


def test_verified_post_reinforces_matching_topic_once_only():
    user_learning.learn_from_chat("I want to focus on local AI models.", "chat-2")

    first = user_learning.record_verified_post(
        "post-1", "Local AI models are finally useful for serious offline work.",
        verification_method="visible_own_post")
    second = user_learning.record_verified_post(
        "post-1", "Local AI models are finally useful for serious offline work.",
        verification_method="visible_own_post")

    assert len(first["reinforced_topics"]) == 1
    assert second["reinforced_topics"] == []
    memory = user_learning.active_topics()[0]
    assert memory["verified_posts"] == 1
    assert memory["confidence"] == "reinforced"


def test_visible_post_verifies_an_inserted_draft_before_learning_from_it():
    topic = user_learning.learn_from_chat("I want to write about open source agents.", "chat-3")
    assert topic is not None
    text = "Open source agents need better evaluation, not another leaderboard."
    generation_id = storage.create_generation({"action": "post", "context": {}})
    storage.save_generation_candidates(generation_id, [{"id": "candidate-1", "text": text}])
    storage.record_outcome({"generation_id": generation_id, "candidate_id": "candidate-1",
                            "status": "inserted", "final_text": text})

    result = asyncio.run(main.metric_snapshot_create(MetricSnapshot(
        post_id="visible-post-1", post_text=text, post_type="original", source="extension",
        metadata={"is_own_post": True, "author_handle": "writer"}), None))

    assert result["matched_generation_id"] == generation_id
    assert result["post_verification"]["method"] == "exact_generation_match"
    post = storage.get_post("visible-post-1")
    assert post is not None and post["origin"] == "starling_verified"
    memory = storage.get_topic_memory(topic["topic_key"])
    assert memory is not None and memory["verified_posts"] == 1


def test_topic_memory_can_be_removed_without_touching_post_history():
    saved = user_learning.learn_from_chat("I want to focus on synthetic biology.", "chat-4")
    assert saved is not None

    assert user_learning.forget_topic(saved["topic_key"]) is True
    assert user_learning.active_topics() == []
    # The audit ledger remains local; removal makes it inactive rather than
    # rewriting prior account history or pretending that history never existed.
    assert storage.get_topic_memory(saved["topic_key"])["active"] is False
