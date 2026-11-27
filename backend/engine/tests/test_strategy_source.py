from __future__ import annotations

import pytest

from app.schemas import DraftRequest, PostContext
from app.strategy import strategy_brief
from app.strategy_source import SOURCE, enhancement_guidance


@pytest.mark.parametrize(
    ("action", "required_phrase", "excluded_phrase"),
    [
        ("reply", "borrowed distribution", "quoted source"),
        ("quote", "quoted source", "borrowed distribution"),
        ("post", "standalone post", "borrowed distribution"),
    ],
)
def test_private_strategy_retrieves_only_rules_for_the_selected_composer_action(action, required_phrase, excluded_phrase):
    guidance = enhancement_guidance(action)

    assert guidance["source"] == SOURCE
    assert guidance["source"]["mode"] == "private_local_strategy"
    rules = " ".join(guidance["rules"]).lower()
    assert required_phrase in rules
    assert excluded_phrase not in rules
    assert "never tell the user to wait" in guidance["output_contract"].lower()


def test_strategy_brief_carries_private_composer_guidance_not_a_remote_source():
    request = DraftRequest(
        action="reply",
        context=PostContext(author="A", text="A visible public post", visibility="public"),
        research_mode="off",
        media_policy="no_media",
        grok_permission=False,
    )
    brief = strategy_brief(request)

    guidance = brief["composer_enhancement_guidance"]
    assert guidance["source"]["sha256"] == SOURCE["sha256"]
    assert guidance["source"]["mode"] == "private_local_strategy"
    assert guidance["source"].get("url") is None
