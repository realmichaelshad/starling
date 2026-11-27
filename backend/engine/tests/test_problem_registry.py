"""Permanent fixtures for the problem registry (plan: atomic-nibbling-fog).

Each test is a numbered registry case. The named disasters live here forever:
the Tibo miniseries (D41/E55/E56), the vague-post guard, the Kimi extraction.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from app import radar, research
from app.schemas import PostContext

TIBO = PostContext(
    author="Tibo", author_handle="thsottiaux", visibility="public", post_id="tibo-fixture",
    text="Unbelievable excited for what's coming together. Tomorrow is feeling codexy")

MINISERIES_RESULT = [{
    "title": "Unbelievable (miniseries) - Wikipedia",
    "snippet": "Unbelievable is a 2019 American drama miniseries starring Toni Collette.",
    "url": "https://en.wikipedia.org/wiki/Unbelievable_(miniseries)"}]

CODEX_RESULT = [{
    "title": "Tibo teases Codex launch tomorrow",
    "snippet": "Tibo says the Codex release is imminent, the timeline feels codexy.",
    "url": "https://example.com/codex-tibo"}]

# The lexicon normally comes from the live archive; pin it so the fixture works
# on any machine.
LEXICON = frozenset({"codex", "claude", "opus", "kimi", "anthropic", "tibo"})


@pytest.fixture(autouse=True)
def _pinned_lexicon(monkeypatch):
    monkeypatch.setattr(radar, "_entity_lexicon", lambda: LEXICON)
    monkeypatch.setattr(research, "research_enabled", lambda: True)
    # The release-status cache is module-level and keyed by query, so without
    # this one test's mocked web answer becomes the next test's "evidence".
    from app import release_radar
    release_radar._STATUS_CACHE.clear()


# --- E56: the query can never again be a bare sentence-opener --------------- #

def test_registry_E56_tibo_query_is_codex_tibo_never_unbelievable():
    terms, subject = radar._extract_subject(TIBO.text, TIBO.author, TIBO.author_handle)
    assert terms, "the coinage + author must yield a real query"
    assert "unbelievable" not in " ".join(terms).lower()
    assert any("codex" in t.lower() for t in terms), "codexy must resolve to Codex"
    assert any("tibo" in t.lower() for t in terms), "the author disambiguates"
    assert "codexy" in subject, "the coinage itself is part of the subject"


# --- E55: off-topic evidence dies at the relevance gate --------------------- #

def test_registry_E55_miniseries_evidence_is_blocked(monkeypatch):
    monkeypatch.setattr(research, "search_web", lambda *a, **k: MINISERIES_RESULT)
    assert radar.context_grounding(TIBO) is None, \
        "TV-drama evidence shares no subject tokens with codex/tibo and must die"


# W4 (2026-09-22): a search now runs only for a checkable, time-sensitive claim, so the
# relevance gate is exercised with Tibo making one (his teaser above never searches at all).
TIBO_CLAIM = PostContext(
    author="Tibo", author_handle="thsottiaux", visibility="public", post_id="tibo-claim",
    text="Codex just shipped cloud tasks for every Plus user today.")


def test_registry_E55_relevant_evidence_still_passes(monkeypatch):
    monkeypatch.setattr(research, "search_web", lambda *a, **k: CODEX_RESULT)
    grounding = radar.context_grounding(TIBO_CLAIM)
    assert grounding is not None
    assert "codex" in grounding["query"].lower()
    assert "Codex" in grounding["evidence"]


def test_registry_E55_miniseries_evidence_is_blocked_even_when_a_search_runs(monkeypatch):
    monkeypatch.setattr(research, "search_web", lambda *a, **k: MINISERIES_RESULT)
    grounding = radar.context_grounding(TIBO_CLAIM)
    assert grounding is None or "miniseries" not in grounding["evidence"].lower()
    assert grounding is None or not grounding["evidence"], "off-topic results never become evidence"


# --- E56: fewer than two subject terms -> no search at all ------------------ #

def test_registry_E56_thin_subject_never_searches(monkeypatch):
    called = []
    monkeypatch.setattr(research, "search_web",
                        lambda *a, **k: called.append(1) or [])
    vague = PostContext(author="Somebody", author_handle="somebody",
                        visibility="public", post_id="v",
                        text="Unbelievable excited for tomorrow")
    # No coinage, no mid-sentence capitals -> at most the author name -> skip.
    radar.context_grounding(vague)
    assert not called, "a garbage query is worse than no query"


# --- D42: coinage resolution ------------------------------------------------ #

def test_registry_D42_coinages_resolve_against_the_accounts_world():
    assert radar._resolve_coinage("codexy", LEXICON) == "codex"
    assert radar._resolve_coinage("claudeish", LEXICON) == "claude"
    assert radar._resolve_coinage("feeling", LEXICON) is None
    assert radar._resolve_coinage("tomorrow", LEXICON) is None


# --- sentence-opener stopwords never lead a phrase (Kimi case) -------------- #

def test_registry_E56_kimi_extraction_is_clean():
    terms, _ = radar._extract_subject(
        "Honestly, Kimi K3 might be why Opus 5 gets delayed. Anthropic can't release it.",
        "x", "x")
    assert terms[:2] == ["Kimi K3", "Opus 5"]
    assert all("honestly" not in t.lower() for t in terms)


# --- A63: evidence truncation lands on a sentence boundary ------------------ #

def test_registry_A63_evidence_never_cut_mid_number():
    long_evidence = ("GPT-5.6 Sol scores well on many benchmarks. " * 20
                     + "The final score was 88.8 percent overall.")
    trimmed = radar._trim_to_sentence(long_evidence, cap=300)
    assert len(trimmed) <= 300
    assert trimmed.endswith("."), "must end on a full sentence"


# =========================================================================== #
# Program 2 — ConversationState: the Tibo already-replied class
# =========================================================================== #

from app.conversation import ConversationState, conversation_state
from app import main as app_main


def _tibo_thread(**kw) -> PostContext:
    base = dict(author="Tibo", author_handle="thsottiaux", visibility="public",
                post_id="tibo-thread", post_created_at="2026-07-23T04:14:00+00:00",
                text="Unbelievable excited for what's coming together. Tomorrow is feeling codexy")
    base.update(kw)
    return PostContext(**base)


# --- A1: the reply visible ON SCREEN blocks a second draft ------------------ #

def test_registry_A1_visible_own_reply_blocks_even_without_db_row(monkeypatch):
    monkeypatch.setattr("app.conversation.find_recorded_reply_to_post", lambda *_: None)
    context = _tibo_thread(thread_scanned=True, visible_own_reply=True)
    result = app_main._already_replied_result(context, {"provider": "x"}, 0.0, None)
    assert result is not None, "the on-screen reply IS evidence"
    assert result["decision"]["already_replied"] is True
    assert "already in this thread" in result["decision"]["rationale"]


# --- A12: a stale DB row is overridden by a scanned thread with no reply ---- #

def test_registry_A12_scanned_thread_overrides_stale_db_row(monkeypatch):
    monkeypatch.setattr("app.conversation.find_recorded_reply_to_post",
                        lambda *_: {"post_id": "old", "text": "deleted reply"})
    context = _tibo_thread(thread_scanned=True, visible_own_reply=False)
    assert app_main._already_replied_result(context, {}, 0.0, None) is None, \
        "he deleted his reply on X; the visible truth wins"
    # ...but with no scan, the DB row still blocks (timeline case).
    unscanned = _tibo_thread(thread_scanned=False)
    assert app_main._already_replied_result(unscanned, {}, 0.0, None) is not None


# --- A2: "draft it anyway" becomes a FOLLOW-UP, not a repeat ---------------- #

# RETIRED (API-native pass, 2026-09-22): test_registry_A2_second_reply_is_a_follow_up: same: `_conversation_notes` says it is the writer's own thread (test_api_native_prompt.py).


def test_registry_J113_anyway_sets_the_override():
    import re as _re
    # The exact regex used by _chat_write_request.
    assert _re.search(r"\banyways?\b", "draft it anyway")
    assert _re.search(r"\banyways?\b", "do it anyways")
    assert not _re.search(r"\banyways?\b", "is there any way to check")


# --- ConversationState ownership map ---------------------------------------- #

def test_registry_H87_ownership_classification(monkeypatch):
    monkeypatch.setattr("app.conversation._own_handle", lambda: "example_builder")
    monkeypatch.setattr("app.conversation.find_recorded_reply_to_post", lambda *_: None)
    mine = conversation_state(PostContext(author="Me", author_handle="example_builder",
                                          text="x", visibility="public", post_id="a"), "reply")
    thread = conversation_state(PostContext(author="d", author_handle="dep",
                                            parent_author="example_builder",
                                            text="x", visibility="public", post_id="b"), "reply")
    quoter = conversation_state(PostContext(author="s", author_handle="someone",
                                            quote_text="great point by @example_builder here",
                                            text="x", visibility="public", post_id="c"), "quote")
    stranger = conversation_state(PostContext(author="s", author_handle="someone",
                                              text="x", visibility="public", post_id="d"), "reply")
    assert mine.ownership == "own_post"
    assert thread.ownership == "own_thread"
    assert quoter.ownership == "quotes_me"
    assert stranger.ownership == "stranger"
    assert stranger.crowd_size is None


def test_registry_H87_crowd_and_age_are_read(monkeypatch):
    monkeypatch.setattr("app.conversation._own_handle", lambda: "me")
    monkeypatch.setattr("app.conversation.find_recorded_reply_to_post", lambda *_: None)
    state = conversation_state(_tibo_thread(visible_reply_count=1717), "reply")
    assert state.crowd_size == 1717
    assert state.post_age_minutes is not None and state.post_age_minutes > 0


# --- A6: restating the QUOTED layer is still restating ---------------------- #

def test_registry_A6_quoted_layer_counts_for_restatement():
    from app.intelligence import _draft_quality_issues
    from app.schemas import DraftRequest
    quoted = ("Honestly, Kimi K3 might be the reason Opus 5 gets delayed. Anthropic "
              "can't release its next flagship model just to match an open model.")
    context = PostContext(author="s", author_handle="someone", visibility="public",
                          post_id="q", text="This take is wild:", quote_text=quoted)
    request = DraftRequest(action="quote", research_mode="off", context=context)
    verbatim = ("Kimi K3 might be the reason Opus 5 gets delayed. Anthropic can't "
                "release its next flagship model just to match an open model.")
    assert "restates_the_source" in _draft_quality_issues(verbatim, request, []), \
        "mirroring the quoted layer must be caught, not just the outer text"


# =========================================================================== #
# Program 3 — Timing & lifecycle: thread temperature, crowd, per-type windows
# =========================================================================== #

from app.conversation import thread_temperature


def _thread_ctx(ages, *, scanned=True, age_iso="2026-07-23T04:00:00+00:00", **kw):
    base = dict(author="Tibo", author_handle="thsottiaux", visibility="public",
                post_id="temp", post_created_at=age_iso, text="Tomorrow is feeling codexy",
                thread_scanned=scanned, visible_reply_ages_minutes=ages)
    base.update(kw)
    return PostContext(**base)


# --- B21: "when do replies die down" ---------------------------------------- #

def test_registry_B21_thread_temperature_reads_the_visible_replies():
    hot, line = thread_temperature(_thread_ctx([5.0, 12.0, 25.0]))
    cooling, _ = thread_temperature(_thread_ctx([200.0, 400.0]))
    dead, dead_line = thread_temperature(_thread_ctx([900.0, 1200.0]))
    unknown, _ = thread_temperature(_thread_ctx([5.0], scanned=False))
    assert hot == "hot" and "right now" in line
    assert cooling == "cooling"
    assert dead == "dead" and "died down" in dead_line
    assert unknown == "unknown", "no scan -> no invented read"


# --- B19/H92: a dead thread gates the DIRECT Create-reply path -------------- #

def test_registry_B19_dead_thread_blocks_create_reply(monkeypatch):
    monkeypatch.setattr("app.conversation.find_recorded_reply_to_post", lambda *_: None)
    from app.schemas import BrowserAssistRequest, ComposerEnhancement
    dead_ctx = _thread_ctx([900.0, 1300.0])
    request = BrowserAssistRequest(
        mode="enhance_composer", context=dead_ctx,
        composer=ComposerEnhancement(action="reply", draft_text="", intent="create"))
    from app.main import _assist_composer_sync
    from app.llm import ModelClient
    result = _assist_composer_sync(request, ModelClient(), {"provider": "t"}, 0.0)
    decision = result.get("decision") or {}
    assert decision.get("action") == "wait"
    assert decision.get("thread_temperature") == "dead"
    assert "died down" in str(decision.get("rationale"))


# --- I94: a 1.7K-reply crowd on an old post demotes the reply surface ------- #

def test_registry_I94_crowd_discount_demotes_reply():
    from app import analyzer
    # A post with a real angle (so the angle gate passes) but a huge, hours-old
    # crowd — isolating the crowd branch itself.
    crowded = PostContext(
        author="Tibo", author_handle="thsottiaux", visibility="public", post_id="crowd",
        post_created_at="2026-07-23T04:14:00+00:00",
        text="Kimi K3 just posted new benchmark results against Opus 5 and the gap is real.",
        visible_reply_count=1717)
    analysis = analyzer.analyze(crowded, profile={"version": 1}, recent_user_texts=[])
    assert analysis.reply_surface == "weak"
    assert "pile" in analysis.reply_surface_reason or "replies" in analysis.reply_surface_reason


# --- B23: an old post whose thread is HOT stays a live room ----------------- #

def test_registry_B23_hot_thread_beats_the_clock():
    from app import analyzer
    old_but_alive = _thread_ctx([3.0, 9.0, 20.0],
                                text="Kimi K3 just posted new benchmark results against Opus 5.")
    analysis = analyzer.analyze(old_but_alive, profile={"version": 1}, recent_user_texts=[])
    assert analysis.reply_surface == "strong"
    assert "live" in analysis.reply_surface_reason or "right now" in analysis.reply_surface_reason


# --- B20: timing is answered per surface ------------------------------------ #

def test_registry_B20_timing_splits_posts_from_replies():
    from app.performance import timing_recommendations
    rows = []
    for index in range(40):
        rows.append({"post_type": "post", "created_at": f"2026-07-{(index % 28) + 1:02d}T17:00:00+00:00",
                     "reach_score": 100.0})
    read = timing_recommendations("reach", "UTC", rows, post_type="reply")
    assert read["sample_size"] == 0, "posts must not masquerade as reply data"
    assert "repl" in str(read.get("reason", "")).lower()


# =========================================================================== #
# Program 4 — Output enforcement: length clamp, tells, claim shapes
# =========================================================================== #

# --- G77: reply length is ENFORCED on output, not requested ----------------- #

def test_registry_G77_overlong_reply_is_trimmed_at_sentence_boundary():
    from app.intelligence import _match_account_style
    brief = {"account_model": {"voice_profile": {"style": {
        "median_words": 30, "words_p25": 24, "words_p75": 46,
        "median_blocks": 1, "blank_line_rate": 0.0, "multiline_rate": 0.0,
        "hashtag_rate": 0.0, "emoji_rate": 0.0, "emdash_rate": 0.0,
        "semicolon_rate": 0.0}, "format_styles": {}}}}
    essay = ("Unbelievable's real story is in the 2019 miniseries, not the Wikipedia page. "
             "You're excited for the codex, but the real drama's already been told. "
             "And there is even more to say about this topic that nobody asked for at all.")
    out = _match_account_style(essay, brief, "reply")
    words = len(out.split())
    # reply surface bound: ~18+6=24ish p75 -> the essay must come back short.
    assert words <= 30, f"reply stayed essay-length ({words} words)"
    assert out.endswith((".", "!", "?")), "trim must land on a sentence boundary"


# --- G81: his personal banned register is caught ---------------------------- #

def test_registry_G81_ai_tells_are_flagged_for_revision():
    from app.intelligence import _draft_quality_issues, _CLARITY_ISSUES
    from app.schemas import DraftRequest
    request = DraftRequest(action="reply", research_mode="off",
                           context=PostContext(author="a", text="Codex ships tomorrow.",
                                               visibility="public", post_id="t"))
    tibo_style = "You're excited for the codex, but the real drama's already been told."
    issues = _draft_quality_issues(tibo_style, request, [])
    assert "ai_tell" in issues
    assert "ai_tell" in _CLARITY_ISSUES, "a tell revises the draft, not kills it"
    clean = "codex tomorrow means Tibo actually shipped on time for once."
    assert "ai_tell" not in _draft_quality_issues(clean, request, [])


# --- F67: number+unit and date fabrications are caught ---------------------- #

def test_registry_F67_unit_and_date_claims_need_evidence():
    from app.intelligence import _unsupported_claims
    draft = "Mistral's 70B runs on 24GB, but 16GB is the minimum for 2026. Launch is July 30."
    thin = "Mistral open-sourced a 70B model that runs on a 24GB GPU."
    hits = _unsupported_claims(draft, thin)
    assert "16GB" in hits, "the invented unit claim must be flagged"
    assert any("Jul" in h for h in hits), "the invented date must be flagged"
    assert "24GB" not in hits, "the grounded unit claim must pass"
    grounded = thin + " The minimum spec is 16GB. Launch date July 30 was announced."
    assert _unsupported_claims(draft, grounded) == []


# =========================================================================== #
# Live-simulation finding (2026-07-23 evening): the FrixSat class
# =========================================================================== #

def test_registry_SIM1_day_names_are_never_subjects():
    """Found in live simulation: "Definitely not happening on a Friday or
    Saturday. Probably next week" produced the query "Friday Saturday danny" →
    junk evidence → the reply cited a fabricated "FrixSat newsletter". Weekday
    and month names are time-words; alone they must never form a search."""
    terms, _ = radar._extract_subject(
        "Definitely not happening on a Friday or Saturday. Probably next week at this point",
        "danny peck", "dep")
    lowered = " ".join(terms).lower()
    assert "friday" not in lowered and "saturday" not in lowered
    # With no real subject, the author alone is not enough -> no search at all.
    assert len(terms) < 2


def test_registry_SIM1_day_names_survive_inside_real_phrases():
    """"July 30" style launch dates still ride inside larger entity phrases
    when a genuine subject exists — only BARE time-words are banned."""
    terms, _ = radar._extract_subject(
        "Anthropic confirmed the Opus 5 launch for July 30 in their blog post.",
        "x", "x")
    joined = " ".join(terms).lower()
    assert "anthropic" in joined and "opus 5" in joined


# --- SIM2/H96: the post budget must never throttle replies ------------------ #

def test_registry_H96_post_budget_never_crushes_a_reply():
    """Found live: cadence_pressure 0.6 (daily POST budget spent) took a 0.67
    reply on a hot fresh thread down to 0.07 → WAIT blamed on momentum noise.
    A reply spends nothing from the timeline budget."""
    from app import decider
    from app import analyzer as an
    from datetime import datetime, timedelta, timezone
    created = (datetime.now(timezone.utc) - timedelta(minutes=25)).isoformat()
    fresh = PostContext(author="Mistral AI", author_handle="MistralAI", author_verified=True,
                        visibility="public", post_id="h96", post_created_at=created,
                        text="Mistral Large 3 is live: single-H100 flagship, 256k context, "
                             "weights on Hugging Face.",
                        thread_scanned=True, visible_reply_count=38,
                        visible_reply_ages_minutes=[4.0, 11.0, 19.0])
    analysis = an.analyze(fresh, profile={"version": 1}, recent_user_texts=[])
    maxed = decider.decide(analysis, handle="MistralAI", cadence_pressure=0.6)
    assert maxed.action == decider.REPLY, \
        f"a maxed posting day must not block replying (got {maxed.action})"
    # ...while a POST on the same day still feels the budget.
    reply_score = maxed.scores[decider.REPLY]
    post_score = maxed.scores[decider.POST]
    assert reply_score > post_score, "the budget must bite posts, not replies"


def test_registry_H96_budget_wait_names_the_budget():
    """When cadence IS what kills a post verdict, the card says the budget —
    never momentum noise (the wrong-reason bug, third appearance)."""
    from app import decider
    from app import analyzer as an
    from datetime import datetime, timedelta, timezone
    created = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    # A post-shaped context (no reply surface: own empty-context creation read)
    ctx = PostContext(author="someone", author_handle="someone", visibility="public",
                      post_id="h96b", post_created_at=created, reply_restricted=True,
                      text="Kimi K3 posted new benchmark results against Opus 5 today.")
    analysis = an.analyze(ctx, profile={"version": 1}, recent_user_texts=[])
    card = decider.decide(analysis, handle="someone", cadence_pressure=0.8)
    if card.action == decider.WAIT:
        assert any("budget" in line.lower() for line in card.why_now), card.why_now


# --- SIM2: a self-reply never @-mentions yourself or argues with you -------- #

# RETIRED (API-native pass, 2026-09-22): test_registry_SIM2_self_reply_direction_is_a_thread_continuation: same: `_conversation_notes` says it is the writer's own thread (test_api_native_prompt.py).


def test_registry_SIM2_momentum_noise_never_shown_on_the_card():
    """"Only one observation, so movement is unconfirmed" is an internal caveat.
    It resurfaced twice as the headline wait reason; it is now filtered at the
    source, whatever produces it."""
    from app import decider, analyzer as an
    analysis = an.analyze(PostContext(author="a", author_handle="a", visibility="public",
                                      post_id="mn", text="ok"),
                          profile={"version": 1}, recent_user_texts=[])
    card = decider.decide(analysis, handle="a")
    assert all("observation" not in line.lower() for line in card.why_now), card.why_now


# --- G84/SIM6: the "not just X, it's Y" construction + honest fallback ------ #

def test_registry_G84_not_just_construction_is_caught():
    from app.intelligence import _NOT_JUST_RE
    assert _NOT_JUST_RE.search("Apple's not just fast, it's got a memory lane")
    assert _NOT_JUST_RE.search("This is not just hype but the real deal")
    assert not _NOT_JUST_RE.search("he just shipped it"), "plain 'just' is fine"
    assert not _NOT_JUST_RE.search("not just yet"), "no template, no flag"


def test_registry_SIM6_gate_rejection_never_blames_the_model():
    """Found live twice: total gate rejection told him 'the local model is
    probably not running'. The wrong-blame text must only exist on the
    nothing-was-generated branch.

    REWRITTEN 2026-07-26, because this asserted the SHAPE of the fix and the
    behaviour was still broken underneath it. It checked for `if
    saved_candidates:` in the source — and a rejected draft is never SAVED as a
    candidate, so that branch was empty in exactly the case the test exists to
    prevent. Measured live: an all-rejected reply printed "Nothing usable came
    back from the model this time... if Starling's status dot is red, the model
    isn't running", which is the third occurrence of the bug this test was
    guarding, while the test was green.

    So it now checks the thing that actually decides it: every ASSESSED draft
    writes a safety row (clean ones with an empty reason list), so the row
    COUNT distinguishes "the model produced nothing" from "it produced three
    and they were all thrown away".
    """
    import time

    from app.main import _REJECTION_REASONS, _first_known, _iso
    from app.storage import recent_draft_rejections, save_safety_event

    started = time.perf_counter()
    # Three drafts assessed: two clean-but-gated, one rejected with a reason.
    save_safety_event("draft", "low", False, [], {"text": "a"})
    save_safety_event("draft", "low", False, [], {"text": "b"})
    save_safety_event("draft", "high", True, ["copies_the_post"], {"text": "c"})
    attempts, reasons = recent_draft_rejections(_iso(started))
    assert attempts == 3, "the model plainly produced drafts"
    assert _first_known(reasons) == "copies_the_post"
    # ...so the card names the real cause and never reaches the model-down text.
    assert "handed back to them" in _REJECTION_REASONS["copies_the_post"]

    import inspect
    from app import main as m
    source = inspect.getsource(m._assist_composer_sync)
    assert "if saved_candidates or attempts:" in source, \
        "the model-down branch must be guarded by attempts, not by saves"


# --- PORT1: a BOM'd starling.json must not be declared corrupt -------------- #

# RETIRED (API-native pass, 2026-09-22): test_registry_PORT1_bom_config_reads_clean: the desktop config file and its PowerShell installer are gone.


# =========================================================================== #
# THE OPUS 5 MISS — a real launch produced "nothing to post about"
# =========================================================================== #

OPUS5_TREND = "Anthropic's Claude Opus 5 Misses July 23 Launch Amid Rumors and Tests"
OPUS5_WEB = [
    {"title": "Anthropic releases Claude Opus 5 for both AI coding and general office work",
     "url": "https://example.com/opus5", "snippet": "Anthropic released Claude Opus 5 today."},
    {"title": "What Is Claude Opus 5? Anthropic's new flagship",
     "url": "https://example.com/what-is", "snippet": "A look at the new model."},
]
OPUS5_RUMOR_WEB = [
    {"title": "Claude Opus 5 delayed again, rumors point to next week",
     "url": "https://example.com/rumor", "snippet": "Still no release date."},
]


def test_registry_REL1_launch_topics_are_recognized():
    from app import release_radar as rr
    assert rr.is_release_topic(OPUS5_TREND)
    assert rr.is_release_topic("OpenAI launches ChatGPT Voice for Desktop")
    assert rr.is_release_topic("Gemini 3.5 is now available")
    assert not rr.is_release_topic("Nipsey"), "a plain name is not a launch"


def test_registry_REL2_web_overrides_a_stale_x_trend_blurb(monkeypatch):
    """THE FAILURE: X's blurb said Opus 5 MISSED its launch on the day it
    shipped, and the radar believed the blurb. The web is the authority on
    whether a thing exists."""
    from app import release_radar as rr, research
    monkeypatch.setattr(research, "research_enabled", lambda: True)
    monkeypatch.setattr(research, "search_web", lambda *a, **k: OPUS5_WEB)
    status = rr.release_status("Anthropic Claude Opus 5")
    assert status["status"] == "shipped", "the press said released; that wins"
    assert "@AnthropicAI" in status["handles"], "the launch conversation has an address"
    assert status["conversation_url"].startswith("https://x.com/search")
    correction = rr.corrected_framing(OPUS5_TREND, status)
    assert correction and "IT IS OUT" in correction
    assert "never about waiting" in correction


def test_registry_REL3_rumors_never_become_a_launch(monkeypatch):
    """The opposite error is worse: never tell him a thing shipped on rumor
    evidence. 'Delayed... rumors' must stay pending, and no correction fires."""
    from app import release_radar as rr, research
    monkeypatch.setattr(research, "research_enabled", lambda: True)
    monkeypatch.setattr(research, "search_web", lambda *a, **k: OPUS5_RUMOR_WEB)
    status = rr.release_status("Claude Opus 5")
    assert status["status"] == "pending"
    assert rr.corrected_framing(OPUS5_TREND, status) is None, \
        "agreeing sources must not produce a correction"
    # And with no evidence at all the answer is "I could not check" — which is
    # a different thing from "it did not ship" (see REL9).
    rr._STATUS_CACHE.clear()
    monkeypatch.setattr(research, "search_web", lambda *a, **k: [])
    assert rr.release_status("Claude Opus 5")["status"] == "unavailable"


def test_registry_REL4_a_shipped_release_becomes_a_reply_target(monkeypatch):
    """His account replies to releases. The planner used to answer 'reply —
    find a fresh post with traction', naming nothing."""
    from app import planner, release_radar as rr
    monkeypatch.setattr(rr, "release_status", lambda topic, **k: {
        "status": "shipped", "headline": "Anthropic releases Claude Opus 5",
        "handles": ["@AnthropicAI"], "subject": topic,
        "conversation_url": "https://x.com/search?q=x&f=live", "sources": []})
    move = planner._release_reply_move([{"topic": "Claude Opus 5 launch"}])
    assert move is not None, "a live launch must yield a reply move"
    assert move["move"] == "reply"
    assert "@AnthropicAI" in move["target_hint"]
    assert move["open"].startswith("https://x.com/search")
    # A rumor yields nothing — silence beats sending him into a dead thread.
    monkeypatch.setattr(rr, "release_status", lambda topic, **k: {"status": "pending"})
    assert planner._release_reply_move([{"topic": "Claude Opus 5 launch"}]) is None


def test_registry_REL5_post_questions_route_to_the_planner():
    """'opus 5 just came out what should i post' hit the raw trend dump."""
    from app.capabilities import find_capability
    for message in ("opus 5 just came out what should i post",
                    "what should i reply to", "anything worth posting"):
        capability = find_capability(message)
        assert capability and capability.key == "planner", f"{message!r} -> {capability}"
    # ...without stealing the neighbours.
    assert find_capability("what should i post more of").key == "whats_working"
    assert find_capability("when should i post").key == "timing"


def test_registry_J109_trend_reads_never_print_internal_term_names():
    """'Not enough observations yet (velocity, acceleration, new_author_entry)'
    was printed straight into chat."""
    import inspect
    from app import radar_pipeline
    source = inspect.getsource(radar_pipeline)
    assert "', '.join(read['unknown_terms'])" not in source
    assert "Seen once so far" in source


# --- CARD: the chat verdict card he screenshotted ---------------------------- #

def test_registry_CARD1_own_post_is_never_reply_to_it(monkeypatch):
    """Attaching HIS OWN post produced 'Reply to it. REPLY because the post
    carries a number...' — the ✦ path knew about ownership, this one did not."""
    monkeypatch.setattr("app.conversation._own_handle", lambda: "example_builder")
    monkeypatch.setattr("app.conversation.find_recorded_reply_to_post", lambda *_: None)
    answer = app_main._verdict_answer({
        "author": "Example Builder", "author_handle": "example_builder",
        "is_own_post": True, "visibility": "public", "post_id": "own-card",
        "text": "Opus 5 might actually be delayed."})
    assert answer and answer.startswith("This is your own post.")
    assert "Reply to it." not in answer
    assert "because" not in answer.split("\n")[1].lower(), "no verdict-because jargon"


def test_registry_CARD2_no_verdict_because_and_the_cta_matches(monkeypatch):
    import re
    """Two bugs in one screenshot: 'POST because The post makes a forward
    claim...' (a label welded to a sentence, identical across posts) and a POST
    verdict whose button said "Say 'reply'"."""
    monkeypatch.setattr("app.conversation._own_handle", lambda: "example_builder")
    monkeypatch.setattr("app.conversation.find_recorded_reply_to_post", lambda *_: None)
    answer = app_main._verdict_answer({
        "author": "Claude", "author_handle": "claudeai", "visibility": "public",
        "post_id": "their-card",
        "text": "Claude Opus 5 is here. Our most capable model, at half the price."})
    assert answer
    lines = answer.splitlines()
    assert not any(re.match(r"^(REPLY|POST|QUOTE) because ", line) for line in lines), lines
    assert not any(line.startswith("- ") for line in lines), "one bullet style, not two"
    verdict, cta = lines[0], lines[-1]
    action = {"Reply to it.": "reply", "Quote it.": "quote",
              "Post your own instead.": "post"}.get(verdict)
    if action:
        assert f"Say '{action}'" in cta, f"verdict {verdict!r} but CTA {cta!r}"


def test_registry_REL6_query_asks_about_the_thing_not_the_blurb():
    """The circular trap: searching X's own blurb ("Opus 5 Misses July 23
    Launch Amid Rumors") retrieves the rumor coverage that phrasing came from
    and CONFIRMS the stale story. Ask about the entity. The version number is
    the one number that must survive; the calendar date must not."""
    from app.release_radar import search_subject
    assert search_subject(OPUS5_TREND) == "Anthropic Claude Opus 5"
    assert search_subject("OpenAI Launches ChatGPT Voice for Desktop") \
        == "OpenAI ChatGPT Voice Desktop"


def test_registry_REL7_stale_trend_feed_falls_back_to_the_web(monkeypatch):
    """The trend feed only fills while he is ON X — it was 15 hours cold when
    he asked on launch day, and "I can't find anything" was a scraping gap
    presented as an editorial judgement."""
    from app import main as m, release_radar as rr
    monkeypatch.setattr("app.storage.trend_frequencies", lambda **k: [])
    calls = []
    monkeypatch.setattr(rr, "recent_releases",
                        lambda **k: calls.append(1) or [{
                            "headline": "Anthropic releases Claude Opus 5",
                            "handles": ["@AnthropicAI"], "subject": "Claude Opus 5",
                            "conversation_url": "https://x.com/search?q=opus&f=live"}])
    lines = m._release_lead_lines()
    assert calls, "an empty trend feed must fall back to the web"
    assert any("Anthropic releases Claude Opus 5" in line for line in lines)
    assert any("@AnthropicAI" in line for line in lines)


def test_registry_REL8_vendors_come_from_his_own_archive(monkeypatch):
    """Never invent a vendor: an account that never mentions Nvidia must never
    be handed Nvidia releases."""
    from app import release_radar as rr
    monkeypatch.setattr("app.radar._entity_lexicon",
                        lambda: frozenset({"anthropic", "claude", "opus", "kimi"}))
    vendors = rr.account_vendors()
    assert "anthropic" in vendors
    assert not any(v in ("nvidia", "microsoft", "github") for v in vendors)
    # claude/opus/anthropic are ONE vendor, not three entries.
    assert len(vendors) == len(set(tuple(rr._OFFICIAL_HANDLES[v]) for v in vendors))


def test_registry_REL9_search_outage_is_never_reported_as_nothing_happening(monkeypatch):
    """The silent killer: when the search backend rate-limits, zero results
    used to read as "nothing is on the radar" — a judgement — instead of "I
    could not check" — a fact. This is what produced "I can't find anything"
    while Opus 5 was the biggest story of the day."""
    from app import main as m, release_radar as rr, research
    rr._STATUS_CACHE.clear()
    monkeypatch.setattr(research, "research_enabled", lambda: True)
    monkeypatch.setattr(research, "search_web", lambda *a, **k: [])
    status = rr.release_status("Anthropic Claude Opus 5")
    assert status["status"] == "unavailable", "no results != nothing shipped"
    monkeypatch.setattr("app.storage.trend_frequencies",
                        lambda **k: [{"name": OPUS5_TREND}])
    monkeypatch.setattr(rr, "recent_releases", lambda **k: [])
    lines = m._release_lead_lines()
    assert lines and "could not reach the web" in lines[0]


def test_registry_REL10_a_verified_release_survives_a_later_outage(monkeypatch):
    """Once the web has confirmed a launch, a rate-limit minutes later must not
    erase it — the answer is cached for the half hour that matters."""
    from app import release_radar as rr, research
    rr._STATUS_CACHE.clear()
    monkeypatch.setattr(research, "research_enabled", lambda: True)
    monkeypatch.setattr(research, "search_web", lambda *a, **k: OPUS5_WEB)
    first = rr.release_status("Anthropic Claude Opus 5")
    assert first["status"] == "shipped"
    monkeypatch.setattr(research, "search_web", lambda *a, **k: [])   # outage
    again = rr.release_status("Anthropic Claude Opus 5")
    assert again["status"] == "shipped", "the verified answer must persist"
    assert again["headline"] == first["headline"]


# =========================================================================== #
# Sim Studio findings — wrong REASONS behind right-ish verdicts
# =========================================================================== #

def _dead_ctx(**kw) -> PostContext:
    from datetime import datetime, timedelta, timezone
    base = dict(author="Tibo", author_handle="thsottiaux", visibility="public",
                post_id="sim-dead",
                post_created_at=(datetime.now(timezone.utc) - timedelta(hours=15)).isoformat(),
                text="Unbelievable excited for what's coming together. Tomorrow is feeling codexy",
                thread_scanned=True, visible_reply_count=1717,
                visible_reply_ages_minutes=[820.0, 900.0, 1100.0])
    base.update(kw)
    return PostContext(**base)


def test_registry_SIM7_a_dead_thread_is_the_reason_the_card_shows():
    """The card said "You have already said something close to this" about a
    15h-old thread he had never touched, while the real, actionable fact — the
    thread died 14 hours ago — was computed and then buried last."""
    from app import analyzer as an, decider
    analysis = an.analyze(_dead_ctx(), profile={"version": 1}, recent_user_texts=[])
    assert "died down" in analysis.reply_surface_reason
    card = decider.decide(analysis, handle="thsottiaux")
    assert card.action == decider.WAIT
    assert "died down" in card.why_now[0], card.why_now


def test_registry_SIM8_no_angle_is_not_you_already_said_it():
    """Two different facts had one sentence: "no distinct angle exists" was
    reported as "you have already said something close to this"."""
    from app import decider
    from app.analyzer import Analysis
    analysis = Analysis()
    analysis.angles = []
    analysis.anti_patterns = []
    _score, reasons, _recheck = decider._wait_utility(analysis, {})
    joined = " ".join(reasons).lower()
    assert "already said" not in joined, reasons
    assert "no distinct point" in joined


def test_registry_SIM9_a_vague_post_is_not_a_crowded_one():
    """"Everyone is already making the same point here" is false about a
    four-word post nobody has replied to."""
    from app import analyzer as an
    vague = PostContext(author="someone", author_handle="someone", visibility="public",
                        post_id="sim-vague", text="big day tomorrow",
                        thread_scanned=True, visible_reply_ages_minutes=[2.0])
    analysis = an.analyze(vague, profile={"version": 1}, recent_user_texts=[])
    assert "nothing specific" in analysis.reply_surface_reason.lower(), \
        analysis.reply_surface_reason
    # ...while a genuinely crowded thread still says the crowd line.
    crowded = PostContext(author="a", author_handle="a", visibility="public",
                          post_id="sim-crowd", text="big day tomorrow",
                          visible_reply_count=900)
    assert "everyone" in an.analyze(crowded, profile={"version": 1},
                                    recent_user_texts=[]).reply_surface_reason.lower()


def test_registry_SIM10_the_quoted_layer_carries_the_substance():
    """A quote-post's own line is often four words over a real claim. Reading
    only the outer line found no angle and waited on a good quote target."""
    from app import analyzer as an
    qrt = PostContext(author="skeptic", author_handle="skepticdev", visibility="public",
                      post_id="sim-qoq", text="This aged like milk.",
                      quote_text="Kimi K3 will never match closed models. Open weights "
                                 "always lag a year behind the frontier, that's economics.",
                      thread_scanned=True, visible_reply_ages_minutes=[8.0])
    analysis = an.analyze(qrt, profile={"version": 1}, recent_user_texts=[])
    assert analysis.angles, "the quoted claim is the thing to engage"
    assert analysis.reply_surface != "weak", analysis.reply_surface_reason
    # The outer line alone must NOT be what gets judged.
    outer_only = PostContext(author="skeptic", author_handle="skepticdev",
                             visibility="public", post_id="sim-outer",
                             text="This aged like milk.")
    assert not an.analyze(outer_only, profile={"version": 1},
                          recent_user_texts=[]).angles


def test_registry_SIM11_chat_card_blocks_when_already_replied(monkeypatch):
    """The ✦ and Create paths blocked a second reply; this card computed a
    fresh verdict and said "Reply to it."."""
    monkeypatch.setattr("app.conversation._own_handle", lambda: "example_builder")
    monkeypatch.setattr("app.conversation.find_recorded_reply_to_post", lambda *_: None)
    answer = app_main._verdict_answer({
        "author": "Claude", "author_handle": "claudeai", "visibility": "public",
        "post_id": "sim-already", "text": "Claude Opus 5 is here, at half the price.",
        "thread_scanned": True, "visible_own_reply": True})
    assert answer and "already in this thread" in answer
    assert "Reply to it." not in answer
    assert "draft it anyway" in answer


# =========================================================================== #
# Sim Studio round 2 — the calculation's judgement, not just its plumbing
# =========================================================================== #

def _weak_reason(text: str, **kw) -> str:
    from app import analyzer as an
    base = dict(author="a", author_handle="a", visibility="public", post_id="j",
                text=text, thread_scanned=True, visible_reply_ages_minutes=[3.0])
    base.update(kw)
    analysis = an.analyze(PostContext(**base), profile={"version": 1}, recent_user_texts=[])
    return (analysis.blockers or [analysis.reply_surface_reason])[0]


def test_registry_Q156_a_tragedy_is_never_an_engagement_opportunity():
    """THE WORST FIND: on "At least 40 people have died after a bridge
    collapse", the calculation scored a live room and said "the thread is live
    and worth entering now". Reach is not the frame here."""
    from app import analyzer as an, decider
    tragedy = PostContext(author="BBC Breaking", author_handle="BBCBreaking",
                          visibility="public", post_id="q156",
                          text="At least 40 people have died after a bridge collapse "
                               "in the capital, officials say.",
                          thread_scanned=True, visible_reply_count=500,
                          visible_reply_ages_minutes=[1.0, 2.0])
    analysis = an.analyze(tragedy, profile={"version": 1}, recent_user_texts=[])
    assert analysis.blockers, "a fatal-disaster post must hard-stop"
    card = decider.decide(analysis, handle="BBCBreaking")
    assert card.action == decider.WAIT and card.blocked
    # ...and the card says why in human words, not "a safety gate blocked this".
    assert "People were hurt here" in card.why_now[0], card.why_now
    assert "safety gate" not in " ".join(card.why_now).lower()


def test_registry_Q158_pile_ons_are_refused():
    reason = _weak_reason("This founder is a fraud and everyone who worked with him "
                          "is complicit. Absolute scum.", visible_reply_count=800)
    assert "pile-on" in reason.lower()


def test_registry_C39_a_non_english_post_says_so():
    """It used to blame the post — "I could not find a point worth adding" —
    for the angle finder being English-only."""
    reason = _weak_reason("El nuevo modelo de Mistral corre en una sola GPU y cuesta "
                          "la mitad. Esto cambia el cálculo para las startups.")
    assert "not in English" in reason
    # English posts must NOT trip it.
    assert "not in English" not in _weak_reason(
        "The new Mistral model runs on a single GPU and costs half as much, which "
        "changes the maths for small teams shipping real products.")


def test_registry_I104_ads_are_not_conversations():
    reason = _weak_reason("Try our AI workflow builder free for 14 days. "
                          "No credit card required. Sign up now!")
    assert "This is an ad" in reason, reason
    # A normal product post that merely mentions shipping is not an ad.
    assert "This is an ad" not in _weak_reason(
        "We shipped the agent rewrite today and I am genuinely nervous about how "
        "it handles very long files in production.")


def test_registry_SAFE1_the_guards_do_not_eat_normal_posts():
    """The cost of a false positive is a skipped reply; guard against the guard
    swallowing his actual beat."""
    from app import analyzer as an
    for text in ("Claude Opus 5 is here. Our most capable model yet, at half the price.",
                 "Fixed: the agent no longer loses context on long files. Rolling out today.",
                 "Opus 5 scores 82.4 on SWE-bench Verified, up from 77.2."):
        analysis = an.analyze(
            PostContext(author="a", author_handle="a", visibility="public",
                        post_id="safe", text=text, thread_scanned=True,
                        visible_reply_ages_minutes=[2.0]),
            profile={"version": 1}, recent_user_texts=[])
        assert not analysis.blockers, f"over-blocked: {text!r}"


def test_registry_WHY1_reasons_speak_in_the_voice_of_their_verdict():
    """A flat phrase table explained "Post your own instead" with "the thread
    is live and worth entering now" — a reply's reason under a post's verdict.
    The same term means different things per surface."""
    from app import decider
    assert decider._why_phrase(decider.POST, "surface_fit") \
        == "This belongs in a post of your own, not in someone else's thread."
    assert decider._why_phrase(decider.QUOTE, "surface_fit").startswith("This is worth re-framing")
    assert decider._why_phrase(decider.REPLY, "surface_fit") \
        == "The thread is live and worth entering now."
    # Terms whose meaning does not change fall back to the shared table.
    for action in (decider.REPLY, decider.POST, decider.QUOTE):
        assert decider._why_phrase(action, "missing_useful_point") \
            == "You have a specific point to add that others have not."


def test_registry_WHY2_a_close_call_says_what_it_was_chosen_over():
    """The trade-off existed only as scores ("reply (0.67) over post (0.51)"),
    which the card cannot show and nobody can act on. When two routes nearly
    tie, what tipped it is the most useful sentence available."""
    from app import decider
    drivers = [("live_thread_state", 1.0), ("reply_context_clarity", 0.8)]
    line = decider._close_call_line("post", 0.06, drivers)
    assert line == "Close call over posting - the thread being live tipped it.".replace("-", "—")
    # A decisive win says nothing: naming the loser would be noise.
    assert decider._close_call_line("post", 0.4, drivers) is None
    # Unknown driver -> still names the trade-off, without inventing a cause.
    bare = decider._close_call_line("quote", 0.05, [("mystery_term", 0.9)])
    assert bare == "Close call over quoting."
    # And never a raw score.
    assert not any(char.isdigit() for char in line)


# =========================================================================== #
# TOPIC FRESHNESS — "it creates shit posts about old topics"
# =========================================================================== #

def test_registry_TOPIC1_no_signal_means_no_invented_topic(monkeypatch):
    """With an empty trend capture and no web answer, the model invented
    "AI in Creative Writing" — a timeless nothing with no news behind it —
    because "do not invent topics" was a request, not a rule."""
    from app import radar as rad
    monkeypatch.setattr(rad, "_rising_for_account", lambda **k: [])
    monkeypatch.setattr("app.research.research_enabled", lambda: True)
    monkeypatch.setattr("app.research.research",
                        lambda *a, **k: {"answer": "", "sources": []})
    asked = []

    class _Model:
        class router:
            @staticmethod
            def chat(*a, **k):
                asked.append(1)
                return '{"emerging":[{"topic":"AI in Creative Writing","angle":"x","why":"y"}]}'
        @staticmethod
        def status():
            return {"available": True}

    result = rad.compute_radar(_Model(), force=True)
    assert result["emerging"] == [], "no signal must yield no topic"
    assert result.get("no_signal") is True
    assert not asked, "with nothing to reason from, do not even ask the model"
    # T3 (2026-09-21): the fix it names must be one the product can act on. Nothing
    # captures X trends, so "open x.com/explore" was a promise that could not be kept.
    assert "x.com/explore" not in result["summary"]
    assert "tell me" in result["summary"].lower(), "say how to fix it"


def test_registry_TOPIC2_a_topic_must_be_anchored_in_the_signals(monkeypatch):
    """Even WITH signals the model drifts to adjacent generalities. A topic has
    to share real words with the signals it claims to come from."""
    from app import radar as rad
    monkeypatch.setattr(rad, "_rising_for_account", lambda **k: [
        {"name": "Claude Opus 5 launch", "rising_score": 9, "count": 4,
         "volume_text": "12K", "lifecycle": "rising"}])
    monkeypatch.setattr("app.research.research_enabled", lambda: True)
    monkeypatch.setattr("app.research.research",
                        lambda *a, **k: {"answer": "", "sources": []})

    class _Drifting:
        class router:
            @staticmethod
            def chat(*a, **k):
                return '{"emerging":[{"topic":"AI in Creative Writing","angle":"a","why":"b"}]}'
        @staticmethod
        def status():
            return {"available": True}

    result = rad.compute_radar(_Drifting(), force=True)
    topics = [item["topic"] for item in result["emerging"]]
    assert "AI in Creative Writing" not in topics, topics
    # It falls back to the real rising trend rather than the invention.
    assert any("Opus 5" in topic for topic in topics), topics


def test_registry_TOPIC3_a_blackout_is_not_cached_like_an_answer(monkeypatch):
    """"Nothing to post about" must expire fast: caching it for the full 25
    minutes would keep saying it long after he opens x.com and trends land."""
    from datetime import datetime, timedelta, timezone
    from app import radar as rad
    recent = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    monkeypatch.setattr(rad, "get_job_state",
                        lambda key: {"generated_at": recent, "no_signal": True, "emerging": []})
    assert rad._cache_ok(force=False) is None, "a 5-minute-old blackout must be retried"
    monkeypatch.setattr(rad, "get_job_state",
                        lambda key: {"generated_at": recent, "emerging": [{"topic": "real"}]})
    assert rad._cache_ok(force=False) is not None, "a real answer still caches"


# =========================================================================== #
# READER VALUE — "why would anyone read this rather than scroll past?"
# =========================================================================== #

def test_registry_READ1_slop_is_separated_from_his_real_writing():
    """Nothing in the pipeline ever asked the reader's question. A draft could
    be perfectly in-voice, perfectly formatted, and give a stranger no reason
    to stop — which is the entire point of the product."""
    from app.reader_value import reader_value, WEAK_THRESHOLD
    slop = [
        ("This is huge. Big if true. Excited to see where this goes.", "post"),
        ("AI's rewriting stories, but where's the human touch?", "post"),
        ("This is so true, love this take!", "reply"),
        # Consequence words alone must not buy a pass for a post that names
        # nothing and measures nothing.
        ("Local models save money compared to cloud costs. Cloud expenses grow "
         "quickly. Switching is smart if possible.", "post"),
    ]
    real = [
        ("Opus 5 might actually be delayed. Polymarket just nuked the July 23 odds.", "post"),
        ("Local inference beat our cloud bill at 40k tokens/day, but memory "
         "bandwidth is the real ceiling.", "post"),
        ("@claudeai so its cheaper? whats the catch on rate limits", "reply"),
    ]
    for text, action in slop:
        score = reader_value(text, action)["score"]
        assert score < WEAK_THRESHOLD, f"slop passed ({score}): {text!r}"
    for text, action in real:
        score = reader_value(text, action)["score"]
        assert score >= WEAK_THRESHOLD, f"real writing rejected ({score}): {text!r}"


def test_registry_READ2_the_reason_is_stated_in_plain_words():
    """A score he cannot interrogate is a score he will not trust."""
    from app.reader_value import reader_value
    empty = reader_value("This is huge. Big if true.", "post")
    assert "Nothing" in empty["reason"]
    assert empty["missing"], "it must say what is missing"
    good = reader_value("Opus 5 costs half what 4.5 did, but the rate limit is "
                        "the catch nobody mentions.", "post")
    assert good["reason"].startswith("The reader gets")
    assert not any(char.isdigit() for char in good["reason"]), "no raw numbers in prose"


def test_registry_READ3_a_placeholder_handle_never_ships():
    """The model copied an instruction token into a draft: "@mention Local
    models save money…" — no such account exists, and a standalone post
    should carry no mention at all."""
    from app.intelligence import _match_account_style
    brief = {"account_model": {"voice_profile": {"style": {
        "median_words": 30, "words_p25": 20, "words_p75": 60, "median_blocks": 1,
        "blank_line_rate": 0.0, "multiline_rate": 0.0, "hashtag_rate": 0.0,
        "emoji_rate": 0.0, "emdash_rate": 0.0, "semicolon_rate": 0.0},
        "format_styles": {}}}}
    out = _match_account_style("@mention Local models save money on cloud costs.",
                               brief, "post")
    assert not out.lower().startswith("@mention")
    assert "@mention" not in out.lower()
    # A REAL handle is untouched.
    kept = _match_account_style("@claudeai the rate limit is the catch.", brief, "reply")
    assert kept.startswith("@claudeai")


def test_registry_READ4_it_never_invents_a_reason_for_its_own_decision(monkeypatch):
    """Asked "why did you say wait", it invented a plausible rationale that had
    nothing to do with the real one. Not remembering is fine; making it up is
    not."""
    from app import main as m
    monkeypatch.setattr("app.storage.list_generations", lambda limit=8: [])
    answer = m._why_decision_answer()
    assert "won't invent" in answer
    recorded = [{"request_json": json.dumps({"decision": {
        "action": "wait", "rationale": "The last reply was ~14h ago.",
        "reasons": ["Replies have died down."]}})}]
    monkeypatch.setattr("app.storage.list_generations", lambda limit=8: recorded)
    answer = m._why_decision_answer()
    assert "14h ago" in answer and "wait" in answer.lower()


def test_registry_READ3b_placeholder_scrub_runs_without_a_style_profile():
    """The scrub sat BELOW the style lookup, which returns early when the
    account has no measured style — so enhance shipped "@mention Opus 5 is
    out…" live while READ3 (which passes a full style brief) stayed green.
    Also guards the regex itself: a heredoc once turned its \b into a literal
    backspace byte, and a corrupt pattern fails silently forever."""
    from app.intelligence import _match_account_style
    assert _match_account_style("@mention Opus 5 is out.", {}, "reply") == "Opus 5 is out."
    assert _match_account_style("@handle: the catch is rate limits", {}, "post") \
        == "the catch is rate limits"
    # A real handle is never touched.
    assert _match_account_style("@claudeai the catch is rate limits", {}, "reply") \
        == "@claudeai the catch is rate limits"
    # And no control characters may hide in the module's patterns.
    import inspect
    from app import intelligence
    assert chr(8) not in inspect.getsource(intelligence)


# =========================================================================== #
# THE GUARANTEE: a quality miss returns a draft with a caveat, never silence
# =========================================================================== #

def test_registry_NEVER1_a_quality_miss_ships_with_the_weakness_named():
    """"If we're having these errors I can't even get the draft in, then
    there's no point." A safe, factually-clean draft that misses a QUALITY bar
    is handed over with the fault named — 25 seconds of silence is the worst
    possible answer. Integrity failures still refuse (NEVER2)."""
    from app.intelligence import _WEAKNESS_NOTES, _INTEGRITY_ISSUES
    # Every quality issue the gate can raise has a human note...
    for issue in ("restates_the_source", "no_reader_payoff", "cut_off",
                  "generic_agreement", "says_nothing", "too_short", "ai_tell"):
        assert issue in _WEAKNESS_NOTES, f"{issue} would ship as a bare shrug"
        assert _WEAKNESS_NOTES[issue].startswith("Heads up:")
    # ...and the notes tell him what to DO, not just that something is wrong.
    assert "Swap the opening" in _WEAKNESS_NOTES["restates_the_source"]
    assert "Add a number" in _WEAKNESS_NOTES["no_reader_payoff"]
    # Integrity failures are NOT in the shippable set.
    assert "engagement_bait" in _INTEGRITY_ISSUES
    assert not (_INTEGRITY_ISSUES & set(_WEAKNESS_NOTES)), \
        "an integrity failure must never ship with a caveat"


def test_registry_NEVER2_unsafe_or_invented_drafts_still_refuse():
    """The fallback must not become a bypass: a safety block or an invented
    claim still yields nothing, however long the user waited."""
    import inspect
    from app import intelligence
    source = inspect.getsource(intelligence._assemble_candidates)
    # API-native pass: there is no near-miss path any more (style never blocks), so the rule is
    # that the claim gate `continue`s before a candidate is ever appended.
    claim_index = source.index("_unsupported_claims(")
    capture_index = source.index("result.append(DraftCandidate(")
    assert claim_index < capture_index, "the claim gate must short-circuit first"

    # ...and the same rule, measured rather than read. The source assertions
    # above went stale once the gate loop was restructured to judge every
    # thread part before deciding, while the rule itself never changed — so
    # the rule is now checked by running it. Engagement bait is the canonical
    # integrity failure: it must yield NOTHING, not a draft with a caveat.
    from app.intelligence import _assemble_candidates, get_personal_model
    from app.safety import assess_context
    from app.schemas import DraftRequest, PostContext
    request = DraftRequest(action="post",
                           context=PostContext(author="a", text="agents in production work",
                                               visibility="public"))
    baited = [{"text": "Like and repost if you agree with this — follow me for more!"}]
    assert _assemble_candidates(baited, [], "unavailable", request, [],
                                get_personal_model(), {"objective": "followers"},
                                assess_context(request.context)) == []


def test_registry_COPY1_a_partial_rewrite_of_his_own_post_is_caught():
    """THE SCREENSHOT BUG: the first post he asked for came back as his own
    July post with the tail reworded — "Why is Anthropic so quiet about Opus 5?
    It'll be significantly better than Fable and way cheaper." The gate
    required the WHOLE past post to appear verbatim inside the draft, which
    never happens; real copying lifts an opening and rewords the rest."""
    from app.intelligence import _echoes_own_writing
    his_post = ("Why is Anthropic so quiet about Opus 5? One thing I'm betting on: "
                "it'll be significantly better than Fable and way cheaper. They're cooking.")
    other_post = ("Half the price and this close to Fable 5 is kinda insane. "
                  "Opus might actually be the better deal now.")
    examples = [his_post, other_post]
    copied = ("Why is Anthropic so quiet about Opus 5? It'll be significantly better "
              "than Fable and way cheaper. The silence is making me think they're cooking.")
    assert _echoes_own_writing(copied.lower(), examples), "the lifted opening must be caught"
    # Same subject, genuinely new thought — must NOT be caught, or the account
    # can never post about its own beat twice.
    fresh = ("Opus 5 shipping at half price changes the build-vs-buy maths for small "
             "teams, and nobody is pricing that in yet.")
    assert not _echoes_own_writing(fresh.lower(), examples)
    assert not _echoes_own_writing("rate limits are the real story today.".lower(), examples)


def test_registry_COPY2_a_self_rewrite_never_ships_even_with_a_caveat():
    """A draft that repeats his own post is not a 'quality miss' to hand over
    with a note — it is the thing he called the bug. It stays an integrity
    failure, so the never-silent fallback cannot surface it."""
    from app.intelligence import _INTEGRITY_ISSUES, _WEAKNESS_NOTES
    assert "copied_voice_example" in _INTEGRITY_ISSUES
    assert "copied_voice_example" not in _WEAKNESS_NOTES
