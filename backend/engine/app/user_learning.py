"""Small, explainable memory for explicit topic corrections and verified posts.

This is deliberately narrower than a claim that the product "learns everything".
It captures the signals that are both useful and attributable: a user directly
choosing a topic, and a post the extension has actually seen on the user's X
account.  The account model still derives broad themes from the complete post
history; this module gives current, explicit intent a safe way to lead it.
"""
from __future__ import annotations

import re
from typing import Any

from .storage import (
    active_topic_memories,
    dismiss_topic_memory,
    remember_topic_focus,
    reinforce_topic_memories_from_verified_post,
)


_TOPIC_PATTERNS = (
    # "No, I want to write about open-source AI agents."
    r"\b(?:i\s+)?(?:want|wanna|prefer|would rather|need)\s+(?:to\s+)?"
    r"(?:write|post|tweet|talk|focus|create|cover)?\s*(?:about|on|around)\s+(.+)$",
    # "Let's focus on local models" / "I want to focus on local models"
    r"\b(?:focus|prioritize|cover|talk)\s+(?:more\s+)?(?:on|about|around)\s+(.+)$",
)
_TRAILING = re.compile(r"\s*(?:instead|now|for now|please|thanks|thank you)\s*[.!?]*$", re.IGNORECASE)
_GENERIC = {"this", "that", "it", "the topic", "a topic", "something", "anything"}


def explicit_topic_from_message(message: str) -> str | None:
    """Return a concrete topic the user plainly selected, never a guess.

    A topic redirect is intentionally narrow: it must contain an explicit
    preference/focus verb and a subject.  "What topic should I cover?" and
    "I like this post" are ordinary chat, not durable learning signals.
    """
    cleaned = " ".join((message or "").strip().split())
    if len(cleaned) < 10:
        return None
    for pattern in _TOPIC_PATTERNS:
        match = re.search(pattern, cleaned, re.IGNORECASE)
        if not match:
            continue
        topic = _TRAILING.sub("", match.group(1)).strip(" .!?\"'")
        if not topic or topic.casefold() in _GENERIC or len(topic) < 3:
            return None
        # A chat message is an instruction, not a document to preserve whole.
        # Keep a readable subject while refusing accidental pasted page text.
        return topic[:160]
    return None


def learn_from_chat(message: str, conversation_id: str | None = None) -> dict[str, Any] | None:
    """Persist an explicit topic focus; duplicate chat delivery is idempotent."""
    topic = explicit_topic_from_message(message)
    if not topic:
        return None
    return remember_topic_focus(topic, source_text=message, conversation_id=conversation_id)


def topic_memory_for_message(message: str) -> dict[str, Any] | None:
    """Look up the active memory for a just-processed correction."""
    topic = explicit_topic_from_message(message)
    if not topic:
        return None
    key = topic_key(topic)
    return next((item for item in active_topic_memories(limit=30) if item["topic_key"] == key), None)


def topic_key(topic: str) -> str:
    """Stable, inspectable key shared with the local storage layer."""
    normalized = topic.casefold().replace("-", " ").replace("/", " ")
    return " ".join(re.findall(r"[a-z0-9][a-z0-9+.#']*", normalized))[:180]


def active_topics(limit: int = 6) -> list[dict[str, Any]]:
    return active_topic_memories(limit=limit)


def record_verified_post(post_id: str, text: str, *, verification_method: str) -> dict[str, Any]:
    """Use a visible authored post to strengthen only matching chosen topics."""
    updated = reinforce_topic_memories_from_verified_post(
        post_id, text, verification_method=verification_method)
    return {
        "verified": True,
        "method": verification_method,
        "post_id": post_id,
        "reinforced_topics": updated,
    }


def forget_topic(topic_key_value: str) -> bool:
    return dismiss_topic_memory(topic_key_value)


def learning_status() -> dict[str, Any]:
    topics = active_topics()
    status: dict[str, Any] = {
        "topic_memory": topics,
        "note": ("Direct topic choices are current, local focuses. Verified posts strengthen matching "
                 "focuses; broad account themes still come from the full verified post history."),
    }
    # What the watcher has actually taught the account model, and at what
    # pace. Every one of these is a measurement, not a training claim.
    try:
        from .storage import observed_post_counts
        status["observed_posts"] = observed_post_counts()
    except Exception:                                    # noqa: BLE001
        pass
    try:
        from .approach import approach_state, transitions
        state = approach_state()
        status["approach"] = {
            "state": state.get("state"),
            "half_life_days": state.get("half_life_days"),
            "since": state.get("since"),
            "recent_posts": state.get("recent_posts"),
            "baseline_posts": state.get("baseline_posts"),
            "note": state.get("note"),
            "transitions": transitions()[-3:],
        }
    except Exception:                                    # noqa: BLE001
        pass
    try:
        from .account_model import topic_dimensions
        from .recency import STARLING_VERIFIED_VOICE_FACTOR, WEIGHT_FLOOR
        dimensions = topic_dimensions()
        status["dimensions"] = {
            "count": len(dimensions),
            "active": sum(1 for dim in dimensions if dim.get("active")),
            "labels": [dim.get("display_label") for dim in dimensions if dim.get("active")],
        }
        status["voice_weighting"] = {
            "half_life_days": (status.get("approach") or {}).get("half_life_days"),
            "floor": WEIGHT_FLOOR,
            "starling_verified_factor": STARLING_VERIFIED_VOICE_FACTOR,
            "note": ("Recent posts weigh more, and posts Starling drafted count half "
                     "toward voice so the writing stays yours."),
        }
    except Exception:                                    # noqa: BLE001
        pass
    return status
