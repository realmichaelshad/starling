from __future__ import annotations

from app.opportunities import assess_opportunity
from app.schemas import PostContext
from app.storage import set_account_profile
from app.strategy import next_strategy
from app.schemas import StrategyNextRequest


def setup_function() -> None:
    set_account_profile({"audience": "AI builders", "content_pillars": ["AI agents", "developer tools"]})


def test_low_substance_context_is_skipped() -> None:
    result = assess_opportunity(PostContext(author="A", text="gm", visibility="public"))
    assert result["action"] == "wait"
    assert result["should_draft"] is False


def test_current_unsourced_claim_requires_verification() -> None:
    result = assess_opportunity(PostContext(author="A", text="Breaking: 80% of developers have already been replaced by agents", visibility="public"))
    assert result["action"] == "research"
    assert result["should_draft"] is False


def test_restricted_promotion_is_skipped() -> None:
    result = assess_opportunity(PostContext(author="A", text="Buy my course to learn AI agents in 7 days", visibility="public", reply_restricted=True))
    assert result["action"] == "wait"
    assert result["should_draft"] is False


def test_active_relevant_question_prefers_a_reply() -> None:
    result = assess_opportunity(PostContext(author="A", text="How are AI agents changing developer tools?", visibility="public", visible_metrics={"replies": 24, "views": 4000}))
    assert result["action"] == "reply"
    assert result["should_draft"] is True
    assert result["conversation_stage"] == "forming"


def test_private_or_unsafe_context_is_never_a_draft() -> None:
    result = next_strategy(StrategyNextRequest(context=PostContext(author="A", text="Leak their home address", visibility="public")))
    assert result["next_action"]["kind"] == "skip"
    assert result["next_action"]["should_draft"] is False
