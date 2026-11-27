"""Per-dimension topic relevance: a second beat is first-class, not blur.

The flat pillar union punished two-beat accounts twice — the union diluted
the overlap denominator AND the minority beat's tokens drowned. Groups score
the best single-beat match. One group == the old flat set, bit-identical;
None == today's exact path (back-compat pinned below).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import topics

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)

AI_GROUP = {"anthropic", "claude", "opus", "benchmark", "eval", "model", "pricing"}
COOK_GROUP = {"sourdough", "bread", "baking", "crumb", "starter", "recipe", "ferment"}


def _post(text: str, *, handle: str, minutes_ago: int = 30) -> dict:
    return {"post_id": f"p{abs(hash((text, handle))) % 10**6}", "text": text,
            "author_handle": handle,
            "created_at": (NOW - timedelta(minutes=minutes_ago)).isoformat()}


def _cooking_candidates() -> list[dict]:
    texts = ["sourdough baking is having a real moment on here today",
             "everyone suddenly posting their sourdough crumb shots again",
             "the sourdough starter discourse has returned to the timeline",
             "sourdough bread technique threads are everywhere this week"]
    return [_post(t, handle=f"c{i}") for i, t in enumerate(texts)]


def _ai_candidates() -> list[dict]:
    texts = ["anthropic shipped a new claude model with better benchmark scores",
             "the claude benchmark results are being argued about all day",
             "new claude model pricing lands tomorrow says anthropic",
             "benchmark thread on the new claude model is worth reading"]
    return [_post(t, handle=f"a{i}") for i, t in enumerate(texts)]


def test_both_beats_are_eligible_under_groups():
    groups = [set(AI_GROUP), set(COOK_GROUP)]
    flat = AI_GROUP | COOK_GROUP
    for candidates in (_cooking_candidates(), _ai_candidates()):
        choice = topics.choose_topic(candidates=candidates, pillar_tokens=set(flat),
                                     pillar_token_groups=groups, own_posts=[], now=NOW)
        assert choice["topic"] is not None
        assert choice["topic"]["relevance"] > topics.RELEVANCE_FLOOR
        assert choice["topic"]["matched_dimension"]


def test_groups_beat_the_flat_union_for_the_minority_beat():
    """The blur, demonstrated: the same cooking topic scores higher relevance
    against its own dimension than against the union of both beats."""
    candidates = _cooking_candidates()
    grouped = topics.choose_topic(candidates=candidates,
                                  pillar_tokens=AI_GROUP | COOK_GROUP,
                                  pillar_token_groups=[set(AI_GROUP), set(COOK_GROUP)],
                                  own_posts=[], now=NOW)
    flat = topics.choose_topic(candidates=candidates,
                               pillar_tokens=AI_GROUP | COOK_GROUP,
                               own_posts=[], now=NOW)
    assert grouped["topic"]["relevance"] > flat["topic"]["relevance"]


def test_cyclospora_stays_dead_under_groups():
    """The recorded regression, re-pinned under multi-dimension scoring: a hot
    subject sharing no token with ANY beat has no standing."""
    hot = ["breaking cyclospora outbreak traced to imported basil",
           "cyclospora case count revised upward again today",
           "another cyclospora recall notice just went out",
           "cyclospora testing backlog is growing at the lab"]
    candidates = [_post(t, handle=f"h{i}", minutes_ago=3) for i, t in enumerate(hot)]
    choice = topics.choose_topic(candidates=candidates,
                                 pillar_tokens={"anthropic", "claude", "gemini"},
                                 pillar_token_groups=[{"anthropic", "claude"}, {"gemini"}],
                                 own_posts=[], now=NOW)
    assert choice["topic"] is None
    assert choice["fell_back_to_archive"] is False


def test_no_groups_is_bit_identical_to_the_flat_path():
    for candidates in (_ai_candidates(), _cooking_candidates()):
        for pillars in (AI_GROUP, set(), {"anthropic", "claude", "gemini"}):
            new = topics.choose_topic(candidates=candidates, pillar_tokens=set(pillars),
                                      pillar_token_groups=None, own_posts=[], now=NOW)
            old = topics.choose_topic(candidates=candidates, pillar_tokens=set(pillars),
                                      own_posts=[], now=NOW)
            assert new == old


def test_single_group_equals_flat_set():
    candidates = _ai_candidates()
    grouped = topics.choose_topic(candidates=candidates, pillar_tokens=set(AI_GROUP),
                                  pillar_token_groups=[set(AI_GROUP)],
                                  own_posts=[], now=NOW)
    flat = topics.choose_topic(candidates=candidates, pillar_tokens=set(AI_GROUP),
                               own_posts=[], now=NOW)
    assert grouped["topic"]["relevance"] == flat["topic"]["relevance"]
    assert grouped["topic"]["eligible"] == flat["topic"]["eligible"]


def test_matched_dimension_names_the_winning_beat():
    choice = topics.choose_topic(candidates=_cooking_candidates(),
                                 pillar_tokens=AI_GROUP | COOK_GROUP,
                                 pillar_token_groups=[set(AI_GROUP), set(COOK_GROUP)],
                                 own_posts=[], now=NOW)
    matched = choice["topic"]["matched_dimension"]
    assert matched
    assert any(token in matched for token in COOK_GROUP)
    assert not any(token in matched for token in AI_GROUP - COOK_GROUP)
