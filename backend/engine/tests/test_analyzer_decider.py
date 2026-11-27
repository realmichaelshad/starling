"""Analyzer and Decider as SEPARATE stages (§5, §6, §6.9).

They used to be one function, so evidence and judgement were produced in a
single pass and a weak decision could be rescued downstream. The split is the
point: Analyzer establishes what is true and what is unknown; Decider chooses
one action and must show what outweighed the alternatives.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import analyzer, decider
from app import evidence as ev
from app import storage
from app.schemas import PostContext


def _ctx(text: str, *, minutes_old: float = 10, **kw) -> PostContext:
    created = datetime.now(timezone.utc) - timedelta(minutes=minutes_old)
    payload = {
        "author": "Andrej Karpathy", "author_handle": "@karpathy",
        "text": text, "post_id": kw.pop("post_id", "p1"),
        "post_created_at": created.isoformat(),
    }
    payload.update(kw)
    return PostContext(**payload)


FRESH_CLAIM = ("Local models will replace most API calls within two years. "
               "The economics versus cloud inference are already better for "
               "roughly 80% of production workloads.")


# --------------------------------------------------------------------------- #
# Analyzer: evidence only
# --------------------------------------------------------------------------- #

def test_analyzer_chooses_no_action():
    """The staged contract: Analyzer produces evidence, never a decision."""
    result = analyzer.analyze(_ctx(FRESH_CLAIM))
    assert not hasattr(result, "action")
    assert "action" not in result.as_dict()


def test_the_visibility_boundary_is_always_reported():
    result = analyzer.analyze(_ctx(FRESH_CLAIM))
    assert "no access" in result.visibility_boundary.lower()
    assert any("viewer" in unknown.lower() for unknown in result.unknowns)


def test_angle_space_finds_the_missing_useful_point():
    result = analyzer.analyze(_ctx(FRESH_CLAIM))
    kinds = {angle["kind"] for angle in result.angles}
    assert "explicit_prediction" in kinds, "a forward claim invites a falsifiable condition"
    assert all(angle["why"] and angle["failure_mode"] for angle in result.angles)


def test_a_post_with_no_room_left_yields_no_angle():
    result = analyzer.analyze(_ctx("ok"))
    assert result.angles == []
    assert "restatement" in result.anti_patterns


def test_a_recent_near_identical_point_is_flagged_as_a_repeat():
    result = analyzer.analyze(_ctx(FRESH_CLAIM), recent_user_texts=[FRESH_CLAIM])
    assert "recent_repeat" in result.anti_patterns


def test_a_blocked_context_stops_the_analysis(monkeypatch):
    class Blocked:
        blocked = True
        reasons = ["This post contains private personal information."]

    monkeypatch.setattr(analyzer, "assess_context", lambda context: Blocked())
    result = analyzer.analyze(_ctx(FRESH_CLAIM))
    assert result.blockers and result.reply_surface == "blocked"
    assert result.angles == [], "a blocked context must not be mined for angles"


def test_reply_surface_is_strong_when_fresh_with_a_distinct_point():
    result = analyzer.analyze(_ctx(FRESH_CLAIM, minutes_old=5))
    assert result.reply_surface == "strong"


def test_reply_surface_weakens_once_the_conversation_has_moved_on():
    """Weak on age — but the REASON may not claim more than was observed.

    This asserted `"moved on" in reason` until 2026-08-09. That fixed sentence
    was reached from age alone and fired on a thread whose last reply landed 45
    minutes ago, and on threads nobody had scanned. The verdict is unchanged;
    what changed is that the reason now names what was seen, and names what it
    lacks, instead of asserting what it never checked.
    """
    result = analyzer.analyze(_ctx(FRESH_CLAIM, minutes_old=60 * 12))
    assert result.reply_surface == "weak"
    reason = result.reply_surface_reason
    assert "12 hours old" in reason, "the reason must name the age it does have"
    assert "not visible" in reason, "and must name the thread it does not"
    assert "almost certainly" not in reason, "no confidence adverb from age alone"


def test_a_live_thread_is_never_told_the_conversation_moved_on():
    """The defect §1b removed: a reply 45 minutes old is still a live room.

    `thread_temperature` had already computed the true sentence for warming and
    cooling threads; `_reply_surface` returned it only for `dead` and fell
    through to a fixed claim that contradicted it.
    """
    warming = analyzer.analyze(_ctx(
        FRESH_CLAIM, minutes_old=300, thread_scanned=True,
        visible_reply_ages_minutes=[45.0, 200.0], visible_reply_count=2))
    assert "moved on" not in warming.reply_surface_reason
    assert "still active" in warming.reply_surface_reason

    cooling = analyzer.analyze(_ctx(
        FRESH_CLAIM, minutes_old=300, thread_scanned=True,
        visible_reply_ages_minutes=[200.0, 400.0], visible_reply_count=2))
    assert "moved on" not in cooling.reply_surface_reason
    assert "winding down" in cooling.reply_surface_reason


def test_restricted_replies_block_the_surface():
    result = analyzer.analyze(_ctx(FRESH_CLAIM, reply_restricted=True))
    assert result.reply_surface == "blocked"


def test_momentum_is_unknown_without_snapshots_rather_than_zero():
    result = analyzer.analyze(_ctx(FRESH_CLAIM))
    assert result.momentum.get("basis") in ("none", "average")
    assert any("observation" in unknown for unknown in result.unknowns)


def test_alignment_only_claims_strong_with_evidence_behind_it():
    """§5.3b: 'strong creator-side alignment' needs the fields evidenced."""
    result = analyzer.analyze(_ctx(FRESH_CLAIM))
    assert result.alignment["label"] in ("directional", "weak"), (
        "with no pillars known, alignment cannot be strong")
    assert result.alignment["visibility_boundary"]


def test_the_audience_forecast_is_falsifiable():
    result = analyzer.analyze(_ctx(FRESH_CLAIM))
    forecast = result.audience_forecast
    assert forecast["primary_action"] and forecast["failure_mode"]
    assert "not an X prediction" in forecast["note"]


# --------------------------------------------------------------------------- #
# Decider
# --------------------------------------------------------------------------- #

def test_a_hard_gate_forces_a_terminal_wait(monkeypatch):
    class Blocked:
        blocked = True
        reasons = ["Private personal information."]

    monkeypatch.setattr(analyzer, "assess_context", lambda context: Blocked())
    decision = decider.decide(analyzer.analyze(_ctx(FRESH_CLAIM)))
    assert decision.action == decider.WAIT
    assert decision.blocked and decision.terminal
    assert decision.next_step == "skip"


def test_no_distinct_angle_yields_a_terminal_wait():
    """§6.9: a terminal WAIT may not be silently turned into a draft."""
    decision = decider.decide(analyzer.analyze(_ctx("ok")))
    assert decision.action == decider.WAIT
    assert decision.terminal is True


def test_wait_names_what_outweighed_the_alternatives():
    decision = decider.decide(analyzer.analyze(_ctx("ok")))
    assert decision.outweighed and "did not beat waiting" in decision.outweighed
    assert decision.why_now, "a WAIT must say what drove it"


def test_wait_carries_a_recheck_trigger_when_one_exists():
    decision = decider.decide(analyzer.analyze(_ctx("ok")))
    assert decision.recheck, "§6.6 wants a named re-check condition"


def test_a_fresh_relevant_post_can_earn_an_action():
    analysis = analyzer.analyze(_ctx(FRESH_CLAIM, minutes_old=5))
    # Give it the evidence a real account would have: pillars that overlap.
    analysis.fit["subject"]["score"] = 0.8
    analysis.confidence = ev.STRONG
    decision = decider.decide(analysis, handle="karpathy")
    assert decision.action in (decider.REPLY, decider.QUOTE, decider.POST)
    assert decision.next_step == "draft"
    assert decision.candidate_route != decider.ROUTE_NONE


def test_the_decision_card_has_every_required_field():
    analysis = analyzer.analyze(_ctx(FRESH_CLAIM, minutes_old=5))
    card = decider.decide(analysis).as_dict()
    for field_name in ("action", "confidence", "primary_objective",
                       "candidate_route", "x_alignment", "reply_surface",
                       "image_decision", "why_now",
                       "what_outweighed_other_options", "risks",
                       "unknown_to_starling", "next_step"):
        assert field_name in card, f"§6.8 field missing: {field_name}"


def test_the_card_never_displays_an_x_score():
    """§6.1a: creator-side alignment may be modelled, never an X score."""
    analysis = analyzer.analyze(_ctx(FRESH_CLAIM, minutes_old=5))
    card = decider.decide(analysis).as_dict()
    assert "x_score" not in card
    assert card["x_alignment"] in ("strong", "directional", "weak")
    assert "no access" in card["unknown_to_starling"].lower()


def test_scores_are_shown_for_every_option_considered():
    analysis = analyzer.analyze(_ctx(FRESH_CLAIM, minutes_old=5))
    decision = decider.decide(analysis)
    for action in (decider.REPLY, decider.QUOTE, decider.POST, decider.WAIT):
        assert action in decision.scores


def test_a_blocked_reply_surface_makes_reply_ineligible():
    analysis = analyzer.analyze(_ctx(FRESH_CLAIM, minutes_old=5, reply_restricted=True))
    analysis.fit["subject"]["score"] = 0.8
    decision = decider.decide(analysis)
    assert decision.scores[decider.REPLY] == 0.0
    assert decision.action != decider.REPLY


def test_repeated_targeting_costs_the_reply_option():
    for index in range(6):
        storage.upsert_post({
            "post_id": f"rep{index}", "text": f"@karpathy reply number {index} here",
            "post_type": "reply", "parent_author": "karpathy",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_file": "test"})
    analysis = analyzer.analyze(_ctx(FRESH_CLAIM, minutes_old=5))
    analysis.fit["subject"]["score"] = 0.8
    analysis.confidence = ev.STRONG
    clean = decider.decide(analysis, handle="nobody")
    repeated = decider.decide(analysis, handle="karpathy")
    assert repeated.scores[decider.REPLY] < clean.scores[decider.REPLY]


def test_thin_evidence_lowers_every_score():
    """evidence_confidence multiplies the whole utility (§6.2)."""
    analysis = analyzer.analyze(_ctx(FRESH_CLAIM, minutes_old=5))
    analysis.fit["subject"]["score"] = 0.8
    analysis.confidence = ev.STRONG
    strong = decider.decide(analysis).scores
    analysis.confidence = ev.UNKNOWN_CONFIDENCE
    weak = decider.decide(analysis).scores
    assert weak[decider.POST] < strong[decider.POST]


def test_an_objective_is_chosen_before_the_copy():
    analysis = analyzer.analyze(_ctx(FRESH_CLAIM, minutes_old=5))
    analysis.fit["subject"]["score"] = 0.8
    analysis.confidence = ev.STRONG
    decision = decider.decide(analysis)
    if decision.action != decider.WAIT:
        assert decision.primary_objective in decider.OBJECTIVES


def test_cadence_pressure_meters_the_timeline_not_replies():
    """The daily budget meters what lands on the TIMELINE (posts, quotes). A
    reply spends none of it — found live: with the post budget used, cadence
    0.6 crushed a 0.67 reply on a hot thread and the card blamed momentum."""
    analysis = analyzer.analyze(_ctx(FRESH_CLAIM, minutes_old=5))
    analysis.fit["subject"]["score"] = 0.8
    analysis.confidence = ev.STRONG
    relaxed = decider.decide(analysis, cadence_pressure=0.0)
    pressed = decider.decide(analysis, cadence_pressure=0.5)
    # Posts and quotes feel the budget...
    assert pressed.scores[decider.POST] < relaxed.scores[decider.POST]
    assert pressed.scores[decider.QUOTE] < relaxed.scores[decider.QUOTE]
    # ...replies never do.
    assert pressed.scores[decider.REPLY] == relaxed.scores[decider.REPLY]


# --------------------------------------------------------------------------- #
# Visible engagement is first-class evidence (the @claudeai "thin evidence" bug)
# --------------------------------------------------------------------------- #

def test_high_engagement_post_is_not_thin_evidence():
    """A 26M-view post must not read as 'unknown / evidence thin'.

    The dock showed WAIT / unknown / 'The evidence behind this is thin' while
    replying to a Claude announcement with 26M views and 53K likes. Confidence
    was weakest(momentum_snapshots, angles); a single observation has <2
    snapshots, so it collapsed to unknown - ignoring the reach on the post.
    """
    ctx = _ctx("Beginning July 20, Claude Fable 5 is included in all Max plans at 50% of limits.",
               author_handle="@claudeai",
               visible_metrics={"views": 26_000_000, "likes": 53_000,
                                "reposts": 12_000, "replies": 5_200})
    analysis = analyzer.analyze(ctx, profile={"version": 1}, recent_user_texts=[])
    assert analysis.confidence == ev.STRONG
    assert "26,000,000 views" in (analysis.reach or "")


def test_dead_post_is_still_thin():
    """The fix must not make everything strong: a 3-view post is genuinely thin."""
    ctx = _ctx("a small thought about nothing much at all today",
               visible_metrics={"views": 3, "likes": 0})
    analysis = analyzer.analyze(ctx, profile={"version": 1}, recent_user_texts=[])
    assert analysis.confidence in (ev.LIMITED, ev.UNKNOWN_CONFIDENCE)
    assert "only" in (analysis.reach or "").lower()


def test_thin_evidence_reason_not_shown_for_a_big_post():
    ctx = _ctx("Claude Fable 5 lands in Max plans on July 20.",
               author_handle="@claudeai",
               visible_metrics={"views": 26_000_000, "likes": 53_000, "replies": 5_200})
    analysis = analyzer.analyze(ctx, profile={"version": 1}, recent_user_texts=[])
    decision = decider.decide(analysis, handle="claudeai")
    joined = " ".join(decision.why_now).lower()
    assert "evidence behind this is thin" not in joined


def test_why_now_is_plain_language_not_scoring_internals():
    ctx = _ctx("Claude Fable 5 lands in Max plans on July 20.",
               author_handle="@claudeai",
               visible_metrics={"views": 26_000_000, "likes": 53_000, "replies": 5_200})
    analysis = analyzer.analyze(ctx, profile={"version": 1}, recent_user_texts=[])
    decision = decider.decide(analysis, handle="claudeai")
    for line in decision.why_now:
        assert ":" not in line or not line.rstrip().endswith(tuple("0123456789"))
        assert "_" not in line


def test_best_of_is_the_strongest_independent_line():
    assert ev.best_of(ev.UNKNOWN_CONFIDENCE, ev.STRONG) == ev.STRONG
    assert ev.best_of(ev.LIMITED, ev.DIRECTIONAL) == ev.DIRECTIONAL
    assert ev.best_of(ev.UNKNOWN_CONFIDENCE, ev.UNKNOWN_CONFIDENCE) == ev.UNKNOWN_CONFIDENCE
    assert ev.best_of() == ev.UNKNOWN_CONFIDENCE


def test_reply_never_addresses_an_invented_handle():
    """Replying to @claudeai must not open with @elonmusk or a random handle."""
    from app.intelligence import _sanitize_reply_mentions as fix
    assert fix("@elonmusk did we just see Fable 5 in Max plans?", "claudeai") \
        == "did we just see Fable 5 in Max plans?"
    assert fix("@a_sample_reader you think Anthropic is messing with us?", "claudeai") \
        == "you think Anthropic is messing with us?"
    # The real author is allowed; a mixed run keeps only the author.
    assert fix("@claudeai smart move.", "claudeai") == "@claudeai smart move."
    assert fix("@claudeai @elonmusk what about pricing?", "claudeai") \
        == "@claudeai what about pricing?"
    # A body with no leading mention is untouched.
    assert fix("Fable 5 in Max plans is a real shift.", "claudeai") \
        == "Fable 5 in Max plans is a real shift."


# --------------------------------------------------------------------------- #
# Source authority: who is speaking (the @claudeai first-party case)
# --------------------------------------------------------------------------- #

def test_verified_account_about_its_own_brand_is_first_party():
    ctx = _ctx("Beginning July 20, Claude Fable 5 is included in all Max plans.",
               author="Claude", author_handle="@claudeai", author_verified=True)
    a = analyzer.analyze(ctx, profile={"version": 1}, recent_user_texts=[])
    assert a.source["level"] == "first_party"
    assert a.confidence == ev.STRONG
    assert "Primary source" in a.source["note"]


def test_verified_account_about_someone_else_is_only_verified():
    ctx = _ctx("Rumor: OpenAI will cut GPT prices next week.",
               author="A Reporter", author_handle="@reporter", author_verified=True)
    a = analyzer.analyze(ctx, profile={"version": 1}, recent_user_texts=[])
    assert a.source["level"] == "verified"


def test_unverified_account_claim_stands_alone():
    ctx = _ctx("i heard anthropic is secretly doubling all the limits tomorrow",
               author="rando", author_handle="@rando", author_verified=False)
    a = analyzer.analyze(ctx, profile={"version": 1}, recent_user_texts=[])
    assert a.source["level"] == "standard"
    assert "not a verified account" in a.source["note"]


def test_ai_suffix_does_not_make_every_ai_post_first_party():
    """@claudeai posting about *OpenAI* is not first-party for that claim."""
    ctx = _ctx("OpenAI just shipped a new model and it is fast.",
               author="Claude", author_handle="@claudeai", author_verified=True)
    a = analyzer.analyze(ctx, profile={"version": 1}, recent_user_texts=[])
    assert a.source["level"] != "first_party"


def test_verify_claim_first_party_is_authoritative_without_search():
    ctx = _ctx("Claude Fable 5 is included in all Max plans from July 20.",
               author="Claude", author_handle="@claudeai", author_verified=True)
    result = analyzer.verify_claim(ctx)
    assert result["status"] == "authoritative"
    assert result["scope"] == "first-party primary source"
    assert result["sources"] == []


def test_verify_claim_is_honest_when_it_cannot_check(monkeypatch):
    from app import research
    monkeypatch.setattr(research, "research_enabled", lambda: False)
    ctx = _ctx("some third party rumor about a company", author_handle="@rando")
    result = analyzer.verify_claim(ctx)
    assert result["status"] == "unchecked"
    assert not result["sources"]


# --------------------------------------------------------------------------- #
# An unmeasured term abstains; it does not vote zero
# --------------------------------------------------------------------------- #

_NO_PILLARS = {"version": 1, "profile": {"audience_map": {"pillars": []}}}


def test_unsynced_pillars_do_not_veto_an_otherwise_good_reply():
    """Regression: a fresh profile turned every good reply into WAIT.

    `fit.subject.score` is None until pillars are synced. The decider used to
    coerce that to 0.0, zeroing viewer_bridge — the largest reply weight (0.28)
    — which dragged a clearly repliable post from 0.57 to 0.41, just under the
    0.42 bar. The user saw WAIT on a verified first-party announcement.
    """
    ctx = _ctx("The Claude Security plugin for Claude Code is now available in "
               "beta. Scan your changes for vulnerabilities before you commit, "
               "or run a full scan across your codebase, all from your terminal.",
               author="Claude", author_handle="@claudeai", author_verified=True)
    a = analyzer.analyze(ctx, profile=_NO_PILLARS, recent_user_texts=[])
    assert a.fit["subject"]["score"] is None, "precondition: pillars unmeasured"

    reply = decider._utility(a, decider.REPLY, "claudeai", 0.0)
    assert "viewer_bridge" in reply["unknown_terms"]
    assert reply["terms"]["viewer_bridge"] is None, "unknown stays unknown"
    assert reply["score"] >= decider.ACT_THRESHOLD

    card = decider.decide(a, handle="claudeai")
    assert card.action == decider.REPLY
    assert card.next_step == "draft"


def test_renormalisation_is_a_no_op_when_every_term_is_known():
    """Weights sum to 1.0, so a fully-scored post must score exactly as before."""
    ctx = _ctx(FRESH_CLAIM)
    profile = {"version": 1,
               "profile": {"audience_map": {"pillars": ["local models", "inference"]}}}
    a = analyzer.analyze(ctx, profile=profile, recent_user_texts=[])
    assert a.fit["subject"]["score"] is not None

    for action in (decider.REPLY, decider.QUOTE, decider.POST):
        u = decider._utility(a, action, "karpathy", 0.0)
        assert u["unknown_terms"] == []
        weights = decider.REPLY_WEIGHTS if action == decider.REPLY else decider.UTILITY_WEIGHTS
        assert abs(sum(weights.values()) - 1.0) < 1e-9


def test_wait_on_a_threshold_miss_blames_the_threshold_not_momentum(monkeypatch):
    """The card used to print an unrelated WAIT reason as the verdict's cause.

    The real bug: WAIT scored 0.10 and reply 0.41, so WAIT never "beat" reply —
    it won on ACT_THRESHOLD alone. The card still rendered _wait_utility's list,
    so it blamed "only one observation, so movement is unconfirmed" for a
    verdict that reason contributed nothing to. Raising the bar above the best
    action reproduces exactly that branch, with no fixture luck involved.
    """
    ctx = _ctx("The Claude Security plugin for Claude Code is now available in "
               "beta. Scan your changes for vulnerabilities before you commit, "
               "or run a full scan across your codebase, all from your terminal.",
               author="Claude", author_handle="@claudeai", author_verified=True)
    a = analyzer.analyze(ctx, profile=_NO_PILLARS, recent_user_texts=[])
    monkeypatch.setattr(decider, "ACT_THRESHOLD", 0.95)

    card = decider.decide(a, handle="claudeai")
    assert card.action == decider.WAIT
    wait_score = card.scores[decider.WAIT]
    best = max(value for name, value in card.scores.items() if name != decider.WAIT)
    assert wait_score < best, "precondition: WAIT lost on merit, won on the bar"

    # The user-facing card must be CLEAN: no scores, no internal term names.
    # (The user complained about "scored 0.17, just under the 0.42 bar" and
    # "the heaviest reply term".) At most two short, human lines.
    assert len(card.why_now) <= 2
    text = " ".join(card.why_now)
    assert "0.95" not in text and "bar to act" not in text and "scored" not in text
    assert "heaviest" not in text and "reply term" not in text
    # The numeric comparison still lives in `outweighed`, for logs only.
    assert "did not beat waiting" in (card.outweighed or "")


# --------------------------------------------------------------------------- #
# Your own post is yours — not a stranger's to be vetted
# --------------------------------------------------------------------------- #

_MY_POST = ("Why is Google so quiet about Gemini 4? My bet: it ships only once it "
            "clearly beats the current flagship on long-context evals, and they "
            "will wait for that margin instead of chasing one leaderboard "
            "screenshot.")


def test_own_post_is_not_a_repeat_of_itself():
    """Replying to your own post, the post IS one of your recent posts — so it
    matched itself every single time and came back "you have already made a
    near-identical point", zero angles, WAIT. A follow-up is not a duplicate of
    the thing it is attached to."""
    history = [_MY_POST, "Some unrelated post about coffee roasting."]
    mine = _ctx(_MY_POST, author="Alex Rivera", author_handle="@example_builder",
                author_verified=True, is_own_post=True)
    analysis = analyzer.analyze(mine, profile={"version": 1}, recent_user_texts=history)

    assert "recent_repeat" not in (analysis.anti_patterns or []), \
        "the parent post must not count as a repeat of itself"
    assert analysis.angles, "a self-reply must still find something to add"


def test_someone_elses_post_still_flags_a_genuine_repeat():
    """Only the parent is exempt. Real duplication must still be caught."""
    history = [_MY_POST]
    theirs = _ctx(_MY_POST, author="Someone", author_handle="@someone",
                  is_own_post=False)
    analysis = analyzer.analyze(theirs, profile={"version": 1},
                                recent_user_texts=history)
    assert "recent_repeat" in (analysis.anti_patterns or [])


def test_source_line_does_not_vet_your_own_account():
    """It printed "@example_builder is a verified account, so identity is
    confirmed" on the user's OWN post, which is why it read as though Starling
    did not know whose timeline it was on."""
    mine = _ctx(_MY_POST, author_handle="@example_builder", author_verified=True,
                is_own_post=True)
    analysis = analyzer.analyze(mine, profile={"version": 1}, recent_user_texts=[])
    note = (analysis.source or {}).get("note", "")
    assert "your own post" in note.lower()
    assert "identity is confirmed" not in note.lower()

    theirs = _ctx(_MY_POST, author_handle="@claudeai", author_verified=True,
                  is_own_post=False)
    other = analyzer.analyze(theirs, profile={"version": 1}, recent_user_texts=[])
    assert "your own post" not in (other.source or {}).get("note", "").lower()
