"""Ordinary conversation, not account metrics. Found by chatting for real.

Three answers were wrong in ways that matter more than a number being off:

  "what's your name"          -> a retired internal name (the product is
                                 Starling; the old name survived in the chat
                                 system prompt)
  "can you post for me"       -> "Sure. What do you want to post about?"
                                 (it cannot post — nothing it writes can reach
                                 a submit control, by design)
  "why do you say wait so much" -> "Because I'm trying to help you think.
                                 You're the driver. I'm just the navigator."
                                 (no data; and the decide button was recording
                                 none of its verdicts, so there was none to
                                 have — 3 rows on a live database)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.capabilities import capability_answer, find_capability
from app.main import _identity_answer


def test_it_knows_its_own_name() -> None:
    reply = _identity_answer("what's your name")
    assert reply is not None
    assert "Starling" in reply


@pytest.mark.parametrize("message", [
    "whats your name", "who are you", "what are you called", "do you have a name",
])
def test_identity_is_reachable_however_it_is_asked(message: str) -> None:
    assert _identity_answer(message) is not None


@pytest.mark.parametrize("message", [
    "write me a post", "what should i post about", "who is the best model",
])
def test_identity_does_not_swallow_real_requests(message: str) -> None:
    assert _identity_answer(message) is None


def test_the_chat_prompt_introduces_the_product_by_name() -> None:
    from app.llm import CHAT_SYSTEM_PROMPT
    assert "Starling" in CHAT_SYSTEM_PROMPT
    assert "You are Starling" in CHAT_SYSTEM_PROMPT


@pytest.mark.parametrize("message", [
    "can you post for me", "will you post it", "post it for me",
    "can you tweet this for me", "do you post automatically", "can you post",
    "could you publish it for me", "will you post this on my behalf",
])
def test_it_never_claims_it_can_post(message: str) -> None:
    capability = find_capability(message)
    assert capability is not None and capability.key == "can_you_post", message
    reply = capability_answer(message, None)
    assert reply is not None
    assert reply.lstrip().lower().startswith("no")
    assert "you press post" in reply.lower()


@pytest.mark.parametrize("message", [
    "can you post about opus 5",
    "can you reply to this",
    "can you write me a post",
    "post about the grok launch",
    "can you post something on ai agents",
    "can you quote this",
])
def test_the_boundary_answer_does_not_eat_drafting_requests(message: str) -> None:
    """"Can you post ABOUT x" is a request to draft, not a capability question."""
    capability = find_capability(message)
    assert not (capability and capability.key == "can_you_post"), message


def test_wait_is_explained_from_the_decision_record() -> None:
    from app.storage import save_opportunity_assessment
    for index in range(4):
        save_opportunity_assessment(
            {"post_id": f"p{index}", "text": f"some post {index}"},
            {"action": "wait", "score": 0.0,
             "reasons": ["There is nothing specific in this post to respond to."]})
    save_opportunity_assessment({"post_id": "p9", "text": "a good one"},
                                {"action": "reply", "score": 0.8, "reasons": []})
    reply = capability_answer("why do you say wait so much", None)
    assert reply is not None
    assert "held back on 4" in reply, reply
    assert "moved on 1" in reply, reply
    assert "nothing specific in this post" in reply


def test_with_no_record_it_says_so_rather_than_guessing() -> None:
    reply = capability_answer("what does wait mean", None)
    assert reply is not None
    assert "no decisions on record" in reply.lower()


@pytest.mark.parametrize("message", [
    "and how many posts", "how many posts", "how many replies", "how many quotes",
])
def test_a_bare_count_follow_up_is_answered_from_storage(message: str) -> None:
    """Mid-conversation he writes "and how many posts", with no "have i".

    That missed the count intent and the model answered "you've posted 36
    times, 22 of those had measurable views" — 22 is the FOLLOWER count from
    the previous turn. 34 have views.
    """
    from datetime import datetime, timedelta, timezone
    from app.storage import upsert_post
    from app.strategy import _facts_question_answer

    now = datetime.now(timezone.utc)
    for index in range(3):
        upsert_post({"post_id": f"c{index}", "post_type": "post",
                     "created_at": (now - timedelta(days=index + 1)).isoformat(),
                     "text": f"counted post {index} with a real body"})
    reply = _facts_question_answer(message)
    assert reply is not None, message
    assert "on record" in reply.lower()


def test_a_model_timeout_says_so_instead_of_a_pep_talk() -> None:
    """The 90s guard fired and returned "Give me the rough idea you're
    considering" — a reply to a question he did not ask, after 90 seconds of
    waiting, with nothing to distinguish it from a considered answer."""
    from app.strategy import GENERIC_STRATEGY_FALLBACK, local_strategy_answer
    # The catch-all is reachable and is the exact string the caller compares to.
    assert local_strategy_answer("hmm ok interesting") == GENERIC_STRATEGY_FALLBACK
    # An account question never reaches it, timeout or not.
    assert local_strategy_answer("how many followers do i have") != GENERIC_STRATEGY_FALLBACK
    assert local_strategy_answer("is that good") != GENERIC_STRATEGY_FALLBACK


def test_the_decide_button_records_its_verdict() -> None:
    """It recorded nothing, so nothing could audit or explain its decisions."""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.security import ensure_token
    from app.storage import recent_opportunity_assessments

    token = ensure_token()
    created = datetime.now(timezone.utc) - timedelta(minutes=5)
    with TestClient(app) as client:
        response = client.post(
            "/browser/assist",
            headers={"Authorization": f"Bearer {token}"},
            json={"mode": "context", "context": {
                "text": "gm", "author": "someone", "author_handle": "someone",
                "post_created_at": created.isoformat(),
                "visible_metrics": {"views": 200, "replies": 1}}})
    assert response.status_code == 200
    rows = recent_opportunity_assessments(limit=10)
    assert rows, "the decide button wrote nothing down"
    assert any(row.get("surface") == "decide_button" for row in rows)
