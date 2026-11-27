"""Regression tests for the 2026-07-23 output-quality fixes (real-usage feedback):
topic!=voice, over-spacing, WAIT card, hallucination claim-check, readability,
per-action reply length, and own-thread engagement."""
from __future__ import annotations

from app.schemas import PostContext


# --- #1 Topic != Voice: no entity lists reach the writer -------------------- #

def test_no_entity_lists_reach_the_writer():
    import json
    from app.llm import _compact_brief
    brief = {"account_model": {
        "positioning": "Practical perspectives on fable, opus, gpt-5",
        "content_pillars": ["fable", "opus", "gpt-5"],
        "voice_profile": {"median_words": 30, "emoji_rate": 0.0,
                          "frequent_vocabulary": ["fable", "opus", "kimi"]},
    }, "live_trends": [{"name": "Rumors Swirl Around Claude Opus 5"}],
       "recent_chat_instructions": ["Ask whether the new Kimi release pushes Opus pricing down"]}
    for action in ("post", "reply", "quote"):
        blob = json.dumps(_compact_brief(brief, action=action), default=str).lower()
        for entity in ("fable", "opus", "gpt-5", "kimi", "content_pillars",
                       "frequent_vocabulary", "live_trends"):
            assert entity not in blob, f"{action} leaks {entity}"
        # GENUINE STYLE SURVIVES, in the English style block (V5, 2026-09-21):
        # the voice-profile JSON only repeated the rates that block states.
        assert "median_words" not in blob, (
            f"{action} ships the median shape beside the sampled one")
    # API-native pass: genuine style survives as the writer block's measured habits.
    from app.voice_card import measured_habits
    assert "Emoji:" in " ".join(measured_habits([{"text": "a post in their voice", "kind": "post"}] * 3)), \
        "the writer block lost a genuine style signal"


def test_topic_variety_skips_recent_subjects():
    from app.radar import _repeats_recent
    recent = ["GPT-5.6 vs Fable 5 pricing"]
    assert _repeats_recent("GPT-5.6 Fable 5 comparison", recent)
    assert not _repeats_recent("Mistral 70B runs on a laptop", recent)


# --- #3 Formatting: group to measured blocks, not one per sentence ---------- #

def test_no_over_spacing():
    from app.intelligence import _reshape_to_style
    roomy = {"multiline_rate": 0.9, "blank_line_rate": 0.9, "median_blocks": 3}
    draft = "one thought here. two thought here. three here. four here. five here."
    out = _reshape_to_style(draft, roomy)
    assert out.count("\n\n") <= 2, "must not blank-separate every sentence"
    assert out.split() == draft.split()


# --- #5 Hallucination: claim-check catches invented benchmarks -------------- #

def test_claim_check_catches_invented_benchmark_and_stats():
    from app.intelligence import _unsupported_claims
    draft = "GPT-5.6 Sol leads on TerminalBench 2.1 with 88.8% vs Opus 4.8 at 78.9%."
    thin = "GPT-5.6 Sol is more cost-efficient than Fable 5."
    hits = _unsupported_claims(draft, thin)
    assert "TerminalBench 2.1" in hits and "88.8%" in hits
    # Grounded evidence clears them; a bare version number ("Opus 4.8") is a name.
    assert _unsupported_claims(draft, "GPT-5.6 Sol hits 88.8% on TerminalBench 2.1, Opus 4.8 78.9%.") == []
    assert _unsupported_claims("Sonnet 5 matches Opus 4.8 now.", "Sonnet 5 shipped.") == []


# --- #4 Readability: cut-off drafts are flagged ----------------------------- #

def test_cut_off_drafts_flagged():
    from app.intelligence import _readability_issues, _CLARITY_ISSUES
    assert "cut_off" in _readability_issues("the wait is getting long and")
    assert "cut_off" in _readability_issues("opus 5 delayed because")
    assert "cut_off" not in _readability_issues("Opus 5 is coming. Just my prediction.")
    assert "cut_off" in _CLARITY_ISSUES  # triggers revise, not a hard reject


# --- #2 Per-action: replies are bounded shorter than posts ------------------ #

def test_reply_length_is_shorter_than_post():
    from app.account_model import style_for_action
    model = {"voice_profile": {"style": {"median_words": 34, "words_p25": 22, "words_p75": 46},
                               "format_styles": {}}}
    post = style_for_action(model, "post")
    reply = style_for_action(model, "reply")
    assert reply["median_words"] < post.get("median_words", 34)
    assert reply["blank_line_rate"] == 0.0  # replies stay compact


# --- #6 Own-thread engagement ---------------------------------------------- #

# RETIRED (API-native pass, 2026-09-22): test_reply_on_own_thread_gets_engagement_intent: the fallback reply direction left with the reply plan; the model reads the conversation notes.


def test_post_reply_quote_each_have_distinct_length_shape_and_tone():
    """The user's rule: posts, replies and quotes are different kinds of writing
    — each its own length, formatting AND tone. With a post-heavy archive (few
    replies, no quotes) the surface defaults must still differentiate them."""
    from app.account_model import style_for_action, style_directive
    model = {"voice_profile": {"style": {
        "median_words": 30, "words_p25": 24, "words_p75": 46,
        "median_blocks": 3, "blank_line_rate": 0.9, "multiline_rate": 0.9,
        "sample": 27}, "format_styles": {}}}

    post = style_for_action(model, "post")
    reply = style_for_action(model, "reply")
    quote = style_for_action(model, "quote")

    # Length ladder: reply < quote < post.
    assert reply["median_words"] < quote["median_words"] < post["median_words"]
    # Formatting: reply compact (1 block, no blank lines); post roomy.
    assert reply["median_blocks"] == 1 and reply["blank_line_rate"] == 0.0
    assert post.get("blank_line_rate", 0) >= 0.5
    # Tone: a distinct hint per surface, surfaced in the directive.
    tones = {a: style_for_action(model, a).get("tone_hint") for a in ("post", "reply", "quote")}
    assert len(set(tones.values())) == 3, "each surface needs its own tone"
    assert "Tone:" in style_directive(model, "reply")
    assert "address their point" in style_directive(model, "reply").lower()
    assert "stand on its own" in style_directive(model, "quote").lower()


def test_thin_action_samples_fall_back_to_surface_default():
    """Two noisy reply samples must not override the sensible default; a real
    handful (>=3) should be trusted."""
    from app.account_model import style_for_action, _MIN_ACTION_SAMPLES
    base = {"style": {"median_words": 30, "words_p25": 24, "median_blocks": 3,
                      "blank_line_rate": 0.9, "sample": 27}}
    thin = {"voice_profile": {**base, "format_styles": {"reply": {"sample": 2, "median_words": 55}}}}
    assert style_for_action(thin, "reply")["median_words"] < 25  # default, not the noisy 55
    enough = {"voice_profile": {**base, "format_styles": {
        "reply": {"sample": _MIN_ACTION_SAMPLES, "median_words": 12, "median_blocks": 1}}}}
    assert style_for_action(enough, "reply")["median_words"] == 12  # measured wins
