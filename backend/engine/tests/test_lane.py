"""LANE-1 - the two-lane classifier, proved on hand-written cases before anything reads it.

Read `lane.py`'s docstring first. The one rule that governs this file:

    A LANE IS A THEME TEST AND MUST NEVER BE A SUBJECT TEST.

`tests/test_subject_not_profile.py` records the account's own framing - the profile is AI
topics and vibe coding, not a list of labs. So nothing here may assert that a lane is decided
by which company a topic names.

## What each test in this file is worth, stated honestly

These measure CORRECTNESS against cases whose expected lane a person decided. A drift pin over
a fixture labelled by the classifier under test can only ever agree with it, so none is used
here as evidence of accuracy.
"""
from __future__ import annotations

import re

import pytest

from app import lane as L


def _alternatives(pattern: re.Pattern[str]) -> set[str]:
    """The literal alternatives of a `(?:a|b|c)` group, for the disjointness proof."""
    source = pattern.pattern
    start = source.index("(?:") + 3
    depth, end = 0, None
    for i in range(start, len(source)):
        if source[i] == "(":
            depth += 1
        elif source[i] == ")":
            if depth == 0:
                end = i
                break
            depth -= 1
    body = source[start:end]
    parts, depth, current = [], 0, ""
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "|" and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += ch
    parts.append(current)
    return {p.strip() for p in parts if p.strip()}


# ---------------------------------------------------------------------------------------
# 1. THE INVARIANT THAT COST A REWRITE.  Correctness.
# ---------------------------------------------------------------------------------------

def test_the_two_halves_of_the_practice_conjunction_are_disjoint():
    """NO TOKEN MAY APPEAR IN BOTH HALVES OF A CONJUNCTION.

    When `orchestrat\\w*` and `vibe cod\\w*` sat in both, one token satisfied both sides and
    `BUILD_SURFACE and BUILD_FRAME` was decoration - it fired PRACTICE on a Gemini Robotics
    headline and on a VentureBeat analyst hire.
    """
    overlap = _alternatives(L.BUILD_SURFACE) & _alternatives(L.BUILD_FRAME)
    assert not overlap, f"a token satisfies both halves, so the conjunction is decoration: {overlap}"


def test_the_disjointness_check_can_fail():
    """NEGATIVE CONTROL. The parser must be able to SEE an overlap, or the test above is
    satisfied by the extractor being broken rather than by the patterns being clean."""
    a = re.compile(r"\b(?:alpha|beta|orchestrat\w*)\b")
    b = re.compile(r"\b(?:gamma|orchestrat\w*)\b")
    assert _alternatives(a) & _alternatives(b) == {r"orchestrat\w*"}
    assert _alternatives(a) & _alternatives(re.compile(r"\b(?:delta)\b")) == set()


# ---------------------------------------------------------------------------------------
# 2. THE CASES THE DESIGN NAMES, each with the reason it is in the record.  Correctness.
# ---------------------------------------------------------------------------------------

DOCUMENTED = [
    ("Gemini Robotics ER 2: video understanding, task orchestration, and multi-robot collaboration",
     None, "control 1: the conjunction bug fired PRACTICE here"),
    ("VentureBeat hires a senior AI analyst to lead coverage",
     None, "control 1: a staff announcement is not a lane"),
    ("Claude Code costs up to $200 a month. Goose does the same thing for free",
     L.PRACTICE, "tie-break: the build surface leads the head"),
    ("DeepSeek V4 Pro vs GPT-5.6 Sol on DeepSWE: Cost, Coding, and Routing",
     L.FRONTIER, "tie-break: the model leads the head"),
    ("Codex app rollout", L.FRONTIER, "the same noun, as a release"),
    ("Codex feels behind right now - I would rather run agents in parallel "
     "than babysit one terminal", L.PRACTICE, "the same noun, as a workflow remark"),
    ("so i built a tiny one, first app ive ever actually finished",
     L.PRACTICE, "BUILD_ACT alone is sufficient"),
    ("im open sourcing habitloop this friday",
     L.PRACTICE, "people write 'im', NOT \"i'm\" - the archive miss that fixed the regex"),
    ("vLLM: all aboard open models ($88M funding round)",
     None, "off-lane pick from a real pick list - names_notable cannot reject it"),
    ("AMD acquires Taalas, a startup that bakes AI models into silicon",
     None, "off-lane pick from his last 12"),
    ("Google just redesigned the search box for the first time in 25 years",
     None, "off-lane pick from his last 12"),
    ("Qwen3.5 tops SWE-bench", L.FRONTIER, "version-glued name; the _BUILDER_THINGS fix inherited"),
    ("NousCoder-14B lands within two points of closed models",
     L.FRONTIER, "PARAM_COUNT structural floor - an unknown lab still classifies"),
]


@pytest.mark.parametrize("text,expected,why", DOCUMENTED)
def test_documented_cases(text, expected, why):
    verdict = L.classify(text)
    assert verdict.lane == expected, (
        f"{why}\n  got {verdict.lane!r} want {expected!r}\n"
        f"  reason={verdict.reason!r}\n  matched={verdict.matched!r}")


# ---------------------------------------------------------------------------------------
# 3. THE TWO KNOWN RESIDUALS, pinned WITH their costs.  Correctness.
# ---------------------------------------------------------------------------------------

def test_the_formerly_accepted_practice_miss_is_now_RECOVERED():
    """A bare "it's all on github now" update.

    THIS TEST USED TO ASSERT THE OPPOSITE, and the change was deliberate. LANE-DESIGN §6.2
    named this shape the classifier's ONE known practice miss and explicitly accepted losing it
    rather than add `update:` to a vocabulary, because that is how word lists rot. The
    surface-alone weak verdict recovers it WITH NO VOCABULARY AT ALL - `github` was already a
    build surface, and the new rule is about span structure rather than words.

    WHAT IT COST, because the old docstring demanded the cost be stated: on the archive it was
    measured against, this was the ONLY post that moved, and cross-lane errors stayed at zero.
    A looser earlier version of the rule also pulled in a joke ABOUT a tool, not the practice,
    and requiring the frontier match to sit strictly inside a LONGER surface phrase removed
    that gain along with a real regression it had introduced on "Codex app rollout".
    """
    text = ("quick update: the code is public now, all of it is on github "
            "for anyone to clone")
    verdict = L.classify(text)
    assert verdict.lane == L.PRACTICE
    assert verdict.score == 0.5, "recovered by the WEAK branch - not a strong verdict"


def test_the_shitpost_about_a_tool_is_not_the_practice():
    """A JOKE MENTIONING A TOOL IS NOT BUILDING WITH IT.

    The practice lane is "building with AI, what breaks, what you built". A sustained bit
    that name-drops Codex is a register, not the practice lane, and a looser version of the
    surface-alone rule counted it. Pinned so that version cannot return unnoticed: `Codex`
    matches BUILD_SURFACE and MODEL_NAME on the SAME span, so it is not "buried", the
    frontier hit survives, and it blocks the weak verdict.
    """
    assert L.classify("I tell my friends I only speak fluent Codex now. It's a personality, "
                      "honestly. Nobody asks follow-up questions.").lane is None


def test_no_fiction_guard_exists():
    """A practice post can be about a fictional artifact.

    A film/franchise kill would delete real practice posts along with the noise. Measured,
    not assumed. This test exists so nobody adds one as an "obvious" noise fix.
    """
    assert L.classify("should someone vibe code the gadget from the Batman movies?").lane \
        == L.PRACTICE


# ---------------------------------------------------------------------------------------
# 5. THE PRIVACY SURFACE.  Correctness.
# ---------------------------------------------------------------------------------------

def test_every_authored_query_is_a_literal_we_wrote():
    """No query may be assembled from anything the account produced."""
    assert L.ALL_QUERIES, "the authored list must not be empty - unknown lane needs the union"
    for query in L.ALL_QUERIES:
        assert isinstance(query, str) and query.strip()
    for lane in L.LANES:
        assert set(L.queries_for(lane)) <= set(L.ALL_QUERIES)


def test_an_unknown_lane_gets_the_widest_net_not_nothing():
    """UNKNOWN MUST NOT MEAN NOTHING - an empty query set is a silent supply outage."""
    assert set(L.queries_for(None)) == set(L.ALL_QUERIES)
    assert set(L.queries_for("astrology")) == set(L.ALL_QUERIES)


def test_the_five_shipped_hn_queries_are_preserved():
    """This is a WIDENING of an already-shipped list, not a replacement vocabulary."""
    from app.research import AUTHORED_QUERY_TEXTS as HN_QUERIES

    missing = set(HN_QUERIES) - set(L.ALL_QUERIES)
    assert not missing, f"widening dropped a shipped query: {missing}"


# ---------------------------------------------------------------------------------------
# 6. EVIDENCE MAY CONFIRM, NEVER CREATE.  Correctness.
# ---------------------------------------------------------------------------------------

def test_evidence_cannot_create_a_lane():
    """Letting evidence vote took the 128-item pool from 7 correct practice claims to 13
    with 6 wrong. A headline with no lane stays laneless however build-shaped its summary."""
    headline = "A shot-scraper-style JSON API on Bun 1.4"
    assert L.classify(headline).lane is None
    assert L.classify(headline, evidence="run it in your terminal, sessions in parallel, "
                                         "the workflow just works").lane is None


def test_evidence_confirms_and_raises_the_score():
    """Uses a 0.7 verdict deliberately.

    A headline whose primary conjunction already fired scores 1.0, and confirmation is capped
    there - so testing this on "Codex app rollout" asserts nothing, which is how the first
    draft of this test passed the wrong thing. Pick a STRUCTURAL-FLOOR verdict, where the
    confirmation has somewhere to go.
    """
    headline = "NousCoder-14B lands within two points of closed models"
    bare = L.classify(headline)
    assert bare.score == 0.7, "pick a floor-only verdict or this test cannot observe a rise"

    confirmed = L.classify(headline, evidence="Nous Research announced the release, "
                                              "with benchmarks against closed models")
    assert bare.lane == confirmed.lane == L.FRONTIER
    assert confirmed.score > bare.score


def test_confirmation_is_capped_at_one():
    """A already-strong verdict cannot be inflated past 1.0 by piling on evidence."""
    strong = L.classify("Qwen3.5 tops SWE-bench")
    assert strong.score == 1.0
    assert L.classify("Qwen3.5 tops SWE-bench",
                      evidence="Alibaba announced the release with new pricing").score == 1.0


def test_a_contested_verdict_is_visibly_weaker():
    """Both lanes firing scores 0.5, so a consumer can tell a clean verdict from a coin-flip."""
    contested = L.classify("Claude Code costs up to $200 a month. Goose does the same thing "
                           "for free, and GPT-5.6 launches next week with new pricing")
    assert contested.lane in L.LANES
    assert contested.score == 0.5


# ---------------------------------------------------------------------------------------
# 7. label_for_texts - the food-blogger guard.  Correctness.
# ---------------------------------------------------------------------------------------

def test_a_non_lane_account_keeps_its_own_label():
    """The None fallback is REQUIRED. Forced naming would label a Peloton account
    "frontier models"."""
    assert L.label_for_texts(["my peloton ride today", "best protein shake",
                              "marathon training week 6"]) is None


def test_a_lane_account_gets_the_lane_name():
    assert L.label_for_texts(["Qwen3.5 tops SWE-bench",
                              "GPT-5.6 launches with new pricing",
                              "Opus 5 benchmarks leak ahead of release"]) == L.FRONTIER


def test_two_members_are_not_enough():
    """>= 3 classified members, so a one-off cannot become a dimension name."""
    assert L.label_for_texts(["Qwen3.5 tops SWE-bench",
                              "GPT-5.6 launches with new pricing"]) is None


# ---------------------------------------------------------------------------------------
# 8. THE BREAKAGE FRAME, and the cost family that was MEASURED AND REJECTED.
# ---------------------------------------------------------------------------------------

def test_a_thing_that_breaks_while_building_is_practice():
    """"Codex on AWS bedrock bug causing 10x charges" - 145 points on HN, dead centre of
    "the practice of building with AI", and dropped before `bug` was frame vocabulary."""
    verdict = L.classify("Codex on AWS bedrock bug causing 10x charges")
    assert verdict.lane == L.PRACTICE
    assert "BUILD_SURFACE" in verdict.matched and "BUILD_FRAME" in verdict.matched


def test_the_cost_family_stayed_out_and_here_is_why():
    """THE REJECTED HALF, pinned so it is not re-added by someone who only sees the gap.

    Adding `charges` / `billing` / `cost me` alongside the breakage words cost one cross-lane
    error: a PRICING story naming a build surface was pulled into the practice lane, because
    `CAPABILITY_CLAIM` does not list `charges` so frontier never even contested it. Zero posts
    in his archive moved either way - the archive could not see this, and a probe of
    adversarial cases could. Per LANE-DESIGN §6.4 that does not ship.
    """
    assert L.classify("OpenAI charges more for the Codex API from next month").lane \
        is not L.PRACTICE
    # and the half that DID ship still does its job
    assert L.classify("Cursor keeps crashing when I run two sessions").lane == L.PRACTICE


def test_breakage_does_not_outrank_a_frontier_event():
    """A model that crashes on launch day is still a release story."""
    assert L.classify("GPT-5.6 crashed on launch day under load").lane == L.FRONTIER
    assert L.classify("Gemini 3 has a bug in its function calling").lane == L.FRONTIER


# ---------------------------------------------------------------------------------------
# 9. lane_of PREFERS A SOURCE'S OWN PROVENANCE over classifying the title.
# ---------------------------------------------------------------------------------------

def test_lane_of_prefers_a_source_set_lane():
    """A registry listing IS the frontier lane by WHERE IT WAS FOUND.

    `/api/v1/models` returns models, so a record from it is frontier by provenance - and it
    needs no word to be recognised. That matters because the classifier CANNOT place a bare
    listing: "listed" is not a release event, which is the same structural blindness as
    `builder_fit` one layer up. Provenance beats vocabulary here, deliberately.
    """
    listing = {"topic": "Ox Alpha, a newly listed model",
               "lane": L.FRONTIER, "lane_source": "registry"}
    assert L.classify(listing["topic"]).lane is None, (
        "the classifier now places a bare model listing - good, but the provenance hint "
        "exists BECAUSE it could not, so reconsider the hint deliberately rather than "
        "leaving two mechanisms doing one job")
    assert L.lane_of(listing) == L.FRONTIER


def test_lane_of_falls_back_when_the_hint_is_absent_or_junk():
    """A hint is trusted only if it names a real lane; anything else classifies as normal."""
    assert L.lane_of({"topic": "Ox Alpha, a newly listed model"}) is None
    assert L.lane_of({"topic": "Ox Alpha", "lane": "astrology"}) is None
    assert L.lane_of({"topic": "Qwen3.5 tops SWE-bench", "lane": ""}) == L.FRONTIER


# ---------------------------------------------------------------------------------------
# 10. END TO END: the constructor must CARRY the lane, not re-derive it.
# ---------------------------------------------------------------------------------------

def test_a_registry_lane_survives_the_candidate_constructor(monkeypatch):
    """THE CONSTRUCTOR IS WHERE PROVENANCE DIES.

    `radar._web_candidates` builds a FRESH dict from selected keys. Before this test, `lane`
    was not among them - so a record arriving from `registry_sources` with
    `"lane": "frontier models"` lost it at the constructor, `lane_of` fell through to
    classifying the title, and the title is "Ox Alpha, a newly listed model", which is None
    because `listed` is not a release event.

    Written, dropped, re-derived wrongly, and nothing reported a loss. Asserted end to end
    through the real function rather than by reading the dict literal, because reading it is
    exactly what missed it.
    """
    from app import news as news_mod
    from app import radar as radar_mod

    stories = [
        {"title": "Ox Alpha, a newly listed model", "url": "https://openrouter.ai/x",
         "source": "OpenRouter", "summary": "A stealth model listing with no release verb.",
         "created_at": 0, "lane": L.FRONTIER, "lane_source": "registry"},
    ]
    monkeypatch.setattr(news_mod, "headlines", lambda *a, **k: stories)

    items = radar_mod._web_candidates(["model releases"], limit=24, force=True)
    assert items, "the registry record should reach the candidate list"

    candidate = items[0]
    assert candidate["lane"] == L.FRONTIER, (
        "the provenance hint was dropped by the constructor and re-derived from the title")
    assert candidate["lane_score"] == 1.0, "a declared lane is observed, not inferred"
    assert L.lane_of(candidate) == L.FRONTIER

    # and the reason the hint is needed at all, pinned:
    assert L.classify(stories[0]["title"]).lane is None


def test_a_source_without_a_lane_still_gets_one_classified(monkeypatch):
    """The hint is a preference, not a requirement - an ordinary feed still gets a lane."""
    from app import news as news_mod
    from app import radar as radar_mod

    stories = [{"title": "Qwen3.5 tops SWE-bench", "url": "https://example.com/q",
                "source": "Hacker News", "summary": "Benchmarks land.", "created_at": 0}]
    monkeypatch.setattr(news_mod, "headlines", lambda *a, **k: stories)

    items = radar_mod._web_candidates(["model releases"], limit=24, force=True)
    assert items and items[0]["lane"] == L.FRONTIER
    assert 0.0 < items[0]["lane_score"] <= 1.0


# ---------------------------------------------------------------------------------------
# 11. SPAN CONTAINMENT + THE SURFACE-ALONE WEAK VERDICT.
#
# A bare tool announcement carries no workflow FRAME (so practice cannot fire) and no release
# EVENT (so frontier cannot), and falls out of both lanes while holding the evidence. That is
# not a cosmetic miss: `radar._selector_key` term 2 sinks an unlaned candidate below every
# laned one, and the wait card would tell him a CLAUDE CODE story "names nothing this account
# writes about".
#
# THE PRECISE MECHANISM, because an imprecise version was circulated and corrected:
# `MODEL_NAME` does NOT block the practice conjunction - `practice` is computed only from
# `_PRACTICE_TESTS` and never consults frontier hits. Specimen 2 below has no model name at
# all and was still unlaned. The true statement is two separate facts:
#   (a) BUILD_FRAME does not fire on announcement headlines, so the conjunction cannot; and
#   (b) the surface-alone rescue is separately defeated for his most-used tool by a SUBSTRING
#       collision - "Claude Code" contains "Claude".
# ---------------------------------------------------------------------------------------

SPECIMENS = [
    ("Auto mode is now the default in Claude Code", L.PRACTICE,
     "needs BOTH: containment drops MODEL_NAME='Claude', then surface-alone rescues it"),
    ("Remaking Unreal engine in Rust for coding agents", L.PRACTICE,
     "needs only the weak verdict - no frontier hit to contain"),
    ("Anthropic is turning Claude Code's auto mode on", None,
     "CORRECTLY still unlaned: Anthropic is named OUTSIDE the surface span, so it is a "
     "genuine independent frontier signal. Do not chase this one."),
]


@pytest.mark.parametrize("text,expected,why", SPECIMENS)
def test_bare_tool_announcements(text, expected, why):
    assert L.classify(text).lane == expected, why


def test_the_weak_verdict_is_actually_reachable():
    """A BRANCH NOBODY REACHES IS NOT A FEATURE. Assert something really produces it."""
    verdict = L.classify("Auto mode is now the default in Claude Code")
    assert verdict.lane == L.PRACTICE
    assert verdict.score == 0.5, "surface-alone is the weakest honest verdict, like a tie-break"
    assert set(verdict.matched) == {"BUILD_SURFACE"}
    assert verdict.reason == "a build surface with nothing said about it"


def test_negative_control_removing_span_containment(monkeypatch):
    """CONTROL A. Without containment, `MODEL_NAME` survives on the substring of the tool's
    own name, the surface-alone branch never runs, and specimen 1 reverts to unlaned."""
    monkeypatch.setattr(L, "_frontier_hits_outside_the_tool_name",
                        lambda text: L._hits(L._FRONTIER_TESTS, text))

    assert L.classify("Auto mode is now the default in Claude Code").lane is None, \
        "containment is doing nothing - specimen 1 should revert without it"
    # specimen 2 has no frontier hit at all, so containment is irrelevant to it
    assert L.classify("Remaking Unreal engine in Rust for coding agents").lane == L.PRACTICE


def test_negative_control_removing_the_weak_verdict(monkeypatch):
    """CONTROL B. Without the surface-alone branch, containment alone removes a spurious
    frontier hit and BOTH specimens are still unlaned for want of a frame."""
    real = L._verdict

    def no_weak(text):
        verdict = real(text)
        if verdict.reason == "a build surface with nothing said about it":
            return L.LaneVerdict(None, 0.0, {}, "neither lane")
        return verdict

    monkeypatch.setattr(L, "_verdict", no_weak)
    assert L.classify("Auto mode is now the default in Claude Code").lane is None
    assert L.classify("Remaking Unreal engine in Rust for coding agents").lane is None


def test_a_frontier_name_outside_the_tool_name_still_counts():
    """CONTAINMENT MUST NOT SWALLOW A REAL SIGNAL. Only the substring collision is discounted;
    a lab named elsewhere in the headline survives and keeps the story out of practice."""
    hits = L._frontier_hits_outside_the_tool_name(
        "Anthropic is turning Claude Code's auto mode on")
    assert "LAB" in hits, "Anthropic sits outside the surface span and must survive"
    assert "MODEL_NAME" not in hits, "'Claude' inside 'Claude Code' is not a frontier signal"


def test_containment_does_not_disarm_the_frontier_lane():
    """The guards that matter: a real release naming a real model stays frontier."""
    assert L.classify("Introducing Gemini 3.7 Flash").lane == L.FRONTIER
    assert L.classify("Claude Opus 5 launches with new pricing").lane == L.FRONTIER
    assert L.classify("Anthropic announces Claude Code pricing changes and new benchmarks"
                      ).lane == L.FRONTIER
