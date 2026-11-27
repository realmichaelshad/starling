"""Model-intelligence suite: GrowthScore, bandit, learning loop, create mode."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app import main, security
from app.bandit import bandit_summary, choose_angles, classify_angle, record_observation
from app.growth_scorer import band_for, score_candidate
from app.learning_loop import recompute_learning
from app.schemas import DraftCandidate
from app.storage import (
    create_generation,
    get_job_state,
    record_outcome,
    save_generation_candidates,
    save_metric_snapshot,
    set_job_state,
    upsert_post,
)
from app.strategy_v2 import angle_ids


# ---------------------------------------------------------------- GrowthScore

def test_growth_scorer_prefers_specific_hooked_drafts():
    weak = "I think this is interesting and raises important questions about the competitive landscape."
    strong = "Qwen3 14B on a $300 GPU beats last year's cloud models at coding. The gap that matters is access, not benchmarks."
    context = "New local model benchmarks show 14B models closing on cloud offerings"
    weak_score, weak_terms = score_candidate(weak, action="reply", objective="reach", context_text=context)
    strong_score, strong_terms = score_candidate(strong, action="reply", objective="reach", context_text=context)
    assert strong_score > weak_score
    assert weak_terms.get("banned_phrases", 0) < 0  # "competitive landscape" is banned
    assert strong_terms["hook_strength"] > weak_terms["hook_strength"]


def test_growth_scorer_redistributes_forecast_weight_honestly():
    score, terms = score_candidate("A concrete point about Claude limits.", action="reply",
                                   objective="reach", context_text="Claude limits", predicted_percentile=None)
    assert "predicted_engagement" not in terms or terms.get("predicted_engagement") == 0.0
    assert "predicted_engagement_note" in terms
    assert 0 <= score <= 100


def test_growth_scorer_penalizes_invented_numbers_and_deleted_lookalikes():
    context = "Anthropic announced a new model today"
    invented, terms = score_candidate("Anthropic spent $4,700,000,000 on this model.", action="post",
                                      objective="followers", context_text=context)
    assert terms.get("unverifiable_precision", 0) < 0
    deleted = ["the exact same wording I deleted before about anthropic model"]
    _, dup_terms = score_candidate("The exact same wording I deleted before about anthropic model", action="post",
                                   objective="followers", context_text=context, deleted_texts=deleted)
    assert dup_terms.get("near_deleted_duplicate", 0) < 0
    del invented


def test_growth_scorer_penalizes_soft_bait():
    context = "New model release discussion"
    clean = "The new model tops benchmarks but costs 4x more per token — which gap actually matters for real work?"
    bait = "The new model is wild. Like if you agree and tag someone who needs this."
    clean_score, clean_terms = score_candidate(clean, action="post", objective="followers", context_text=context)
    bait_score, bait_terms = score_candidate(bait, action="post", objective="followers", context_text=context)
    assert bait_terms.get("soft_engagement_bait", 0) < 0
    assert "soft_engagement_bait" not in clean_terms
    assert clean_score > bait_score


def test_growth_scorer_algorithm_alignment_orders_hooks():
    context = "New model release discussion"
    question = "Everyone cites the new model's benchmark wins — but has anyone actually shipped it in production yet?"
    link_first = "https://example.com/review\nThe new model benchmark review is worth reading."
    question_score, question_terms = score_candidate(question, action="post", objective="followers", context_text=context)
    link_score, link_terms = score_candidate(link_first, action="post", objective="followers", context_text=context)
    assert question_terms["algorithm_alignment"] > link_terms["algorithm_alignment"]
    assert question_score > link_score


def test_term_maxima_still_sum_to_one_hundred():
    from app.growth_scorer import TERM_MAXIMA
    assert sum(TERM_MAXIMA.values()) == 100.0


def test_bands_match_strategy_thresholds():
    assert band_for(92.0) == "post-ready"
    assert band_for(80.0) == "strong"   # §8.4 vocabulary
    assert band_for(70.0) == "workable"
    assert band_for(50.0) == "below-bar"
    assert band_for(None) is None


def test_isotonic_calibration_is_monotone_when_present():
    set_job_state("scorer_isotonic", {"x": [40.0, 60.0, 80.0], "y": [0.2, 0.5, 0.9], "n": 30})
    low, _ = score_candidate("Solid point about Claude usage limits being confusing.", action="reply",
                             objective="reach", context_text="Claude usage limits confusing")
    set_job_state("scorer_isotonic", {})
    high, _ = score_candidate("Solid point about Claude usage limits being confusing.", action="reply",
                              objective="reach", context_text="Claude usage limits confusing")
    # Calibration re-scales but stays within [0, 100].
    assert 0 <= low <= 100 and 0 <= high <= 100


# --------------------------------------------------------------------- Bandit

def test_bandit_chooses_k_distinct_angles_with_shrunk_priors():
    chosen = choose_angles("post", 3, seed="test")
    ids = [item["angle_id"] for item in chosen]
    assert len(ids) == len(set(ids)) == 3
    assert all(angle in angle_ids() for angle in ids)
    # No data -> posterior means sit at the prior.
    assert all(abs(item["posterior_mean"] - 0.5) < 0.01 for item in chosen)


def test_bandit_learns_from_observations_with_recency():
    now = datetime.now(timezone.utc)
    for _ in range(6):
        record_observation("reply", "direct_comparison", 0.95, observed_at=now.isoformat())
    for _ in range(6):
        record_observation("reply", "question_with_premise", 0.05, observed_at=now.isoformat())
    chosen = choose_angles("reply", 2, seed="x")
    assert chosen[0]["angle_id"] == "direct_comparison"
    assert all(item["angle_id"] != "question_with_premise" or item is chosen[-1] for item in chosen)
    summary = bandit_summary("reply", limit=3)
    assert summary and summary[0]["angle"] == "Direct comparison"
    # Old observations decay: a years-old success cannot dominate.
    old = (now - timedelta(days=200)).isoformat()
    for _ in range(6):
        record_observation("reply", "release_cadence", 1.0, observed_at=old)
    posterior = {item["angle_id"]: item for item in choose_angles("reply", 9, seed="y")}
    assert posterior["direct_comparison"]["posterior_mean"] > posterior["release_cadence"]["posterior_mean"]


def test_angle_classifier_is_conservative():
    assert classify_angle("If you're saying Grok is the same level as Fable you gotta be high") == "direct_comparison"
    assert classify_angle("open weights can't be revoked, that's the whole point") == "open_vs_closed"
    assert classify_angle("nice weather today") is None


# -------------------------------------------------------------- Learning loop

def test_learning_loop_finalizes_matured_outcomes_into_bandit_and_calibration():
    posted_at = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    upsert_post({"post_id": "p-1", "text": "Grok is not on the same level as Fable, not even close",
                 "post_type": "reply", "created_at": posted_at, "metrics": {"views": 1500, "likes": 20, "replies": 3},
                 "followers_count": 100, "metrics_updated_at": posted_at})
    # Baseline sibling posts so the percentile has company.
    for index in range(4):
        upsert_post({"post_id": f"base-{index}", "text": f"baseline reply {index}", "post_type": "reply",
                     "created_at": posted_at, "metrics": {"views": 100 + index, "likes": 1},
                     "followers_count": 100, "metrics_updated_at": posted_at})
    generation_id = create_generation({"action": "reply", "context": {"author": "a", "text": "grok vs fable"}})
    save_generation_candidates(generation_id, [{"id": "cand-1", "text": "draft", "angle_id": "direct_comparison"}])
    record_outcome({"generation_id": generation_id, "status": "posted", "published_post_id": "p-1",
                    "posted_at": posted_at, "candidate_id": "cand-1"})
    from app.growth_scorer import log_scored_candidate
    log_scored_candidate("cand-1", generation_id, 82.0, {"hook_strength": 8.0}, "reach")

    result = recompute_learning()
    assert result["finalized"] == 1
    summary = bandit_summary("reply", limit=5)
    assert any(item["angle"] == "Direct comparison" for item in summary)
    # Idempotent: a second pass does not double-count.
    assert recompute_learning()["finalized"] == 0
    state = get_job_state("learning_loop")
    assert generation_id in state["processed"]


# ------------------------------------------------------------- Create mode API

class CreateFakeModel:
    text_model = "qwen3:14b"

    def status(self):
        return {"available": True, "provider": "starling-services", "text_model": "qwen3:14b"}


def _client(monkeypatch) -> tuple[TestClient, dict]:
    client = TestClient(main.app)
    token = security.ensure_token()
    monkeypatch.setattr(main, "ModelClient", CreateFakeModel)
    return client, {"Authorization": f"Bearer {token}"}


def test_create_mode_replies_from_scratch_and_never_waits(monkeypatch):
    client, headers = _client(monkeypatch)
    candidate = DraftCandidate(id="c-create", text="The real story is access, not another benchmark.",
                               performance={}, rationale=["Grounded in the visible post."],
                               retrieved_examples=[], sources=[], growth_score=84.0, score_band="strong")
    monkeypatch.setattr(main, "generate_drafts", lambda request, llm, *, brief=None: [candidate])
    response = client.post("/browser/assist", headers=headers, json={
        "mode": "enhance_composer",
        "composer": {"action": "reply", "draft_text": "", "intent": "create"},
        "context": {"author": "A", "text": "New model beats benchmarks", "visibility": "public"},
    })
    body = response.json()
    assert response.status_code == 200
    assert body["decision"]["action"] == "reply"
    assert body["decision"]["action"] != "wait"
    assert body["decision"]["draft"] == "The real story is access, not another benchmark."
    assert body["decision"]["score_band"] == "strong"


def test_create_mode_post_grounds_in_a_subject_not_in_the_archive(monkeypatch):
    """A from-scratch post takes its SUBJECT from observed signal, not pillars.

    Regression: this previously injected `content_pillars` straight into the
    grounding text, so every generated post came back about the same two or
    three subjects the account already writes about.
    """
    client, headers = _client(monkeypatch)
    seen = {}

    def capture(request, llm, *, brief=None):
        seen["request"] = request
        return []

    monkeypatch.setattr(main, "generate_drafts", capture)
    monkeypatch.setattr(main, "infer_account_model",
                        lambda: {"content_pillars": ["Anthropic", "Fable"]})
    # A live topic must exist for drafting to proceed at all: with no signal the
    # create path now refuses rather than inventing an evergreen nothing (see
    # test_registry_TOPIC1). This test is about the DIRECTION, so give it one.
    monkeypatch.setattr("app.radar.creation_grounding", lambda *a, **k: {
        "topic": "Kimi K3 benchmark results", "angle": "say what it means for builders",
        "why": "it landed this morning", "evidence": "Kimi K3 posted new results today."})
    response = client.post("/browser/assist", headers=headers, json={
        "mode": "enhance_composer",
        "composer": {"action": "post", "draft_text": "", "intent": "create"},
    })
    body = response.json()
    assert response.status_code == 200
    assert seen["request"].enhancement_mode is False

    grounding = seen["request"].context.text
    assert "standalone post" in grounding.lower()
    # The pillars must never be handed over as the topic.
    assert "anthropic" not in grounding.lower()
    assert "fable" not in grounding.lower()
    assert "content pillars" not in grounding.lower()
    # Total gate rejection keeps the action but fabricates nothing: with no
    # usable candidate the draft is empty and the rationale says why, rather
    # than a template sentence being passed off as the writer's own.
    assert body["decision"]["action"] == "post"
    assert body["decision"]["draft"] == ""
    # The PROPERTY, not the prose: it reports that nothing arrived and does
    # not pass a template off as his writing. Pinned to an exact sentence,
    # this broke on a wording change that improved the message, and again
    # when "the model" became "the AI service".
    assert "nothing came back" in body["decision"]["rationale"].lower()


def test_create_mode_reply_requires_visible_context(monkeypatch):
    """No post, no reply: nothing is drafted and the reason is said. Not a 400 any more: the
    gateway reported that refusal as "Starling's AI isn't responding" (30 Oct 2026)."""
    client, headers = _client(monkeypatch)
    response = client.post("/browser/assist", headers=headers, json={
        "mode": "enhance_composer",
        "composer": {"action": "reply", "draft_text": "", "intent": "create"},
    })
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True and not body["decision"].get("draft")
    assert body["decision"]["action"] == "skip"
    assert "couldn't read the post you're replying to" in body["decision"]["rationale"]


def test_enhance_with_empty_draft_still_requires_text(monkeypatch):
    client, headers = _client(monkeypatch)
    response = client.post("/browser/assist", headers=headers, json={
        "mode": "enhance_composer",
        "composer": {"action": "reply", "draft_text": "", "intent": "enhance"},
    })
    assert response.status_code == 400
