"""Two features that answered without doing anything.

GOALS. `POST /goals` and `create_goal` both exist and NOTHING could reach
either — the native host has no goals route and the extension never calls one.
So saying "my goal is 5000 followers" was answered with:

    You have no goal set. Tell me one in plain words — "grow to 5k followers"
    or "more replies from AI builders" — and I will hold every recommendation
    against it.

It asked for exactly what had just been said, in exactly the words it had just
been said in, because no path could accept it. This is the failure
`capabilities.py` was written to prevent, one layer in: the capability had a
door for READING and none for writing.

REMINDERS. "remind me to post at 3pm" got "I'll remind you when the time
comes" — a promise nothing can keep. Scheduling here operates on a DRAFT
(`mode=schedule`, ops save/list_due/mark_posted); a bare reminder with nothing
attached does not exist. He would have found out at 3pm.
"""
from __future__ import annotations

import pytest

from app.capabilities import capability_answer, find_capability
from app.storage import list_goals, save_profile_snapshot


def _followers(count: int) -> None:
    from datetime import datetime, timezone
    save_profile_snapshot({"captured_at": datetime.now(timezone.utc).isoformat(),
                           "followers_count": count})


@pytest.mark.parametrize("message,target", [
    ("my goal is 5000 followers", "5000 followers"),
    ("i want to get to 5k followers", "5000 followers"),
    ("grow to 1000 followers", "1000 followers"),
    ("my goal is 2,500 followers", "2500 followers"),
])
def test_stating_a_goal_sets_it(message: str, target: str) -> None:
    _followers(22)
    reply = capability_answer(message, None)
    assert reply is not None, message
    assert reply.startswith(("Set:", "Updated:")), reply
    assert target in reply
    assert [g for g in list_goals() if g["name"] == target], "nothing stored"


def test_changing_your_mind_replaces_rather_than_accumulates() -> None:
    """The brief picks one active goal; a stale duplicate would sometimes win."""
    _followers(22)
    capability_answer("my goal is 5000 followers", None)
    reply = capability_answer("my goal is 10000 followers", None)
    assert reply is not None and reply.startswith("Updated:"), reply
    followers_goals = [g for g in list_goals()
                       if g["objective"] == "followers" and g.get("status", "active") == "active"]
    assert len(followers_goals) == 1, followers_goals
    assert followers_goals[0]["name"] == "10000 followers"


def test_a_different_objective_is_its_own_goal() -> None:
    _followers(22)
    capability_answer("my goal is 5000 followers", None)
    capability_answer("grow to 100000 views", None)
    objectives = {g["objective"] for g in list_goals()}
    assert {"followers", "reach"} <= objectives


def test_a_goal_below_where_he_already_is_is_refused() -> None:
    _followers(22)
    reply = capability_answer("my goal is 10 followers", None)
    assert reply is not None
    assert "isn't a goal" in reply
    assert not list_goals()


def test_asking_about_goals_still_reads_them() -> None:
    _followers(22)
    capability_answer("my goal is 5000 followers", None)
    reply = capability_answer("what's my goal", None)
    assert reply is not None
    assert "5000 followers" in reply
    assert not reply.startswith(("Set:", "Updated:"))


@pytest.mark.parametrize("message", [
    "why is starling slow", "why are you so slow",
    "why does it take so long", "what model are you using",
])
def test_it_explains_its_own_speed_instead_of_denying_it(message: str) -> None:
    """It answered "I'm not slow. I'm just focused on making sure your posts
    hit the right notes… You're not alone in this." — after taking 88.9
    SECONDS to say it. The honest answer says what a draft costs: the hosted
    AI service and the steps every draft goes through."""
    capability = find_capability(message)
    assert capability is not None and capability.key == "speed", message
    reply = capability_answer(message, None)
    assert reply is not None
    assert "not slow" not in reply.lower()
    assert "AI service" in reply
    assert "no AI call" in reply


def test_the_speed_answer_costs_nothing_to_produce(monkeypatch) -> None:
    """It used to ask the model client for its status, which LISTED MODELS
    OVER THE NETWORK — so the answer to "why are you slow" was itself slow
    (195s across four cases in this file). The answer must stay free to
    produce: no model client, no router call, no socket."""
    import socket
    import time

    from app.llm import ModelClient
    from app.providers import ProviderRouter

    def forbidden(*_args, **_kwargs):
        raise AssertionError("the speed answer reached for a model or the network")

    monkeypatch.setattr(ModelClient, "__init__", forbidden)
    for name in ("status", "chat", "chat_stream"):
        monkeypatch.setattr(ProviderRouter, name, forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)

    start = time.time()
    for message in ("why is starling slow", "what model are you using"):
        reply = capability_answer(message, None)
        assert reply is not None and "AI service" in reply, message
    assert time.time() - start < 8, "the speed answer is doing real work"


def test_the_delete_advisor_resolves_the_post_he_named() -> None:
    """"Should I delete my last post" was answered with "open the post you're
    considering deleting" — about the one post it can identify from storage."""
    from datetime import datetime, timedelta, timezone
    from app.storage import save_metric_snapshot, upsert_post
    from app.strategy import _delete_question_answer

    now = datetime.now(timezone.utc)
    for index in range(4):
        upsert_post({"post_id": f"d{index}", "post_type": "post",
                     "created_at": (now - timedelta(days=index + 1)).isoformat(),
                     "text": f"a post about models number {index}"})
        save_metric_snapshot({"post_id": f"d{index}", "captured_at": now.isoformat(),
                              "metrics": {"views": 100 * (index + 1)}})
    reply = _delete_question_answer("should i delete my last post", None)
    assert reply is not None
    assert "open the post" not in reply.lower(), reply


@pytest.mark.parametrize("message", [
    "remind me to post at 3pm", "set a reminder", "ping me later",
    "can you remember to post this tonight",
])
def test_a_bare_reminder_is_refused_not_promised(message: str) -> None:
    capability = find_capability(message)
    assert capability is not None and capability.key == "reminder", message
    reply = capability_answer(message, None)
    assert reply is not None
    assert "can't set a bare reminder" in reply.lower()
    assert "draft" in reply.lower()
