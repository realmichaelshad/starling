"""Strategy manual ingestion: parser coverage, briefs, fallbacks, hash check."""
from __future__ import annotations

import pytest

from app import pack_ownership, strategy_v2
from app.strategy_source import enhancement_guidance
from app.strategy_v2 import BRIEF_CHAR_BUDGET, get_strategy, reload_strategy


@pytest.fixture(autouse=True)
def fresh_strategy():
    reload_strategy()
    yield
    reload_strategy()


def test_bundled_manual_parses_all_major_sections():
    strategy = get_strategy()
    assert strategy.loaded
    assert len(strategy.sections) >= 40
    assert strategy.sha_matches_distillation, "EXPECTED_SHA256 must follow the shipped manual"


def test_extractions_are_populated():
    strategy = get_strategy()
    assert "competitive landscape" in strategy.banned_phrases
    assert len(strategy.banned_phrases) >= 8
    assert len(strategy.voice_traits) >= 6
    assert any("anthropic" in topic.lower() for topic in strategy.tier1_topics)
    assert len(strategy.hook_families) >= 5
    assert len(strategy.scoring_components) >= 6
    assert "40-120" in strategy.length_targets["reply"]


def test_the_shipped_manual_is_craft_not_a_person():
    """The manual ships to every customer, so it declares no owner, carries no
    verbatim replies and names no one."""
    strategy = get_strategy()
    assert strategy.owner_handle == ""
    assert strategy.exemplars == []
    assert strategy_v2.exemplars_for_this_install() == []
    raw = strategy_v2._bundled_path().read_text(encoding="utf-8")
    report = pack_ownership.personal_text_report(raw)
    assert report["handles"] == []
    assert report["quoted_blocks"] == 0
    assert not report["declares_owner"]
    assert pack_ownership.release_blocker(raw, public=True) == ""


def test_every_brief_exists_and_fits_budget():
    strategy = get_strategy()
    purposes = ["enhance_reply", "enhance_quote", "enhance_post",
                "create_reply", "create_quote", "create_post", "chat_persona", "decision"]
    for purpose in purposes:
        brief = strategy.brief_for(purpose)
        assert brief, f"empty brief: {purpose}"
        assert len(brief) <= BRIEF_CHAR_BUDGET
    assert "never ask for follows" in strategy.brief_for("create_post").lower()
    assert "wait" in strategy.brief_for("decision").lower()


def test_enhancement_guidance_leads_with_manual_and_keeps_fallback_rules():
    guidance = enhancement_guidance("quote")
    joined = " ".join(str(rule) for rule in guidance["rules"]).lower()
    assert "embed" in joined  # manual quote rule
    assert "quoted source" in joined  # distilled fallback rule kept
    assert "never tell the user to wait" in str(guidance["output_contract"]).lower() or \
           "one polished draft only" in str(guidance["output_contract"]).lower()


def test_missing_file_degrades_to_fallbacks(monkeypatch, tmp_path):
    monkeypatch.setattr(strategy_v2, "_bundled_path", lambda: tmp_path / "missing.txt")
    strategy = reload_strategy()
    assert not strategy.loaded
    assert strategy.brief_for("enhance_reply") == ""
    # Guidance still works from distilled constants.
    guidance = enhancement_guidance("reply")
    assert any("borrowed distribution" in str(rule).lower() for rule in guidance["rules"])
    status = strategy_v2.source_status()
    assert "missing" in status["message"].lower()


def test_a_changed_manual_is_parsed_and_reported_as_changed(monkeypatch, tmp_path):
    changed = tmp_path / "strategy_v2.txt"
    changed.write_text(
        "=" * 70 + "\n1. VOICE MODEL\n" + "=" * 70 + "\n\n"
        "7.2 WHAT TO AVOID\n\n- \"synergy\"\n\n",
        encoding="utf-8")
    monkeypatch.setattr(strategy_v2, "_bundled_path", lambda: changed)
    strategy = reload_strategy()
    assert strategy.loaded
    assert strategy.path == str(changed)
    assert strategy.banned_phrases == ["synergy"]
    assert not strategy.sha_matches_distillation
    assert "updated since" in strategy_v2.source_status()["message"].lower()


def test_there_is_no_per_customer_manual_override(monkeypatch, tmp_path):
    """One manual for every customer: a file dropped into a customer's data folder
    must not replace it."""
    monkeypatch.setenv("STARLING_APP_DIR", str(tmp_path))
    planted = tmp_path / "strategy" / "strategy_v2.txt"
    planted.parent.mkdir(parents=True)
    planted.write_text("=" * 70 + "\n1. VOICE MODEL\n" + "=" * 70 + "\n", encoding="utf-8")
    strategy = reload_strategy()
    assert strategy.path == str(strategy_v2._bundled_path())
    assert not hasattr(strategy_v2, "_app_strategy_override")
