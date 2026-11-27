"""The second root-cause sweep (2026-07-30), five more classes.

Each of these was a MECHANISM, found by auditing the classes behind the
first five rather than by reading drafts:

  * a call that never happened read as a negative verdict
  * a gate handed boilerplate instead of the thing it judges
  * numbers stated twice, once tailored and once raw, contradicting
  * a branch chosen by one measurement and filled by another
  * a derived shape inheriting the authority of the measurement it came from
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_an_unreachable_verifier_is_not_a_negative_verdict():
    """ROOT CAUSE, critical. `ProviderRouter.chat` returns None WITHOUT
    raising when the deadline expired, no model is configured for the lane,
    or the transport failed. That None flowed into `_quote_supports(str(raw
    or ""))` — False — which this module reads as "the evidence does not
    support this claim". So an unreachable model cut the last sentence off
    every post and quote, or shipped them carrying "I can't back …", and the
    user reads that as Starling judging the draft."""
    from app import support_check

    class _DeadRouter:
        def chat(self, *args, **kwargs):
            return None                     # exactly what the router returns

    class _Client:
        router = _DeadRouter()

    draft = ("Kimi K3 is on Telnyx now. It costs 60 cents per million tokens.")
    text, note = support_check.apply_support_check(draft, "x" * 120, _Client())
    assert text == draft, "a draft was cut on the strength of a call that never happened"
    assert note is None, f"an outage was reported as a judgement: {note!r}"


def test_the_dangling_gate_sees_the_topic_not_the_template():
    """ROOT CAUSE. The post direction opens with a fixed 207-character
    template, and the gate was handed `direction[:200]` — always boilerplate,
    never the subject. It flagged roughly one post in five with a reason that
    was false, and spent a revise call that could not succeed."""
    from app.intelligence import _topic_from_direction, dangles_its_subject

    direction = (
        "Write one original standalone post for this account today, in the "
        "writer's voice: one recognizable thesis with a concrete fact, "
        "contradiction, prediction, or consequence and a short conclusion "
        "with attitude. Ground it in what is rising RIGHT NOW: topic "
        "\"Anthropic launches Cowork\" — angle: take a sharp position.")
    assert _topic_from_direction(direction) == "Anthropic launches Cowork"
    # The real post template runs 207 characters before any subject appears,
    # so the old `direction[:200]` slice could not contain a topic at all.
    real_template_head = (
        "Write one original standalone post for this account today, in the "
        "writer's voice: one recognizable thesis with a concrete fact, "
        "contradiction, prediction, or consequence and a short conclusion "
        "with attitude.")
    assert len(real_template_head) > 200
    assert _topic_from_direction(real_template_head[:200]) == "", (
        "the old slice handed the gate pure instruction text")
    # A draft that names the subject is not dangling...
    assert not dangles_its_subject(
        "It's a desktop agent. Cowork edits your files directly.",
        _topic_from_direction(direction))
    # ...and one that never names it still is.
    assert dangles_its_subject(
        "It's the only reason agents can touch your files safely.",
        _topic_from_direction(direction))


def test_shape_numbers_never_ship_twice():
    """ROOT CAUSE of 43.8% shape compliance. `_style_block` renders ONE
    surface's numbers as English and `_compact_brief` then serialised every
    surface's medians as JSON into the same prompt — including under a
    sampled silhouette whose whole point was that the median must not speak."""
    from app.llm import _clean_voice_profile

    voice = {"style": {"median_words": 25, "median_blocks": 2,
                       "emoji_rate": 0.0, "hashtag_rate": 0.0},
             "format_styles": {"post": {"median_words": 25},
                               "reply": {"median_words": 14}},
             "median_words": 25,                 # some briefs carry it flat
             "frequent_vocabulary": ["opus", "fable"],
             "tone": "direct"}
    cleaned = _clean_voice_profile(voice, drop_shape_numbers=True)
    import json
    blob = json.dumps(cleaned)
    assert "format_styles" not in cleaned
    assert "median_words" not in blob, "a shape number survived at some level"
    assert "median_blocks" not in blob
    assert "frequent_vocabulary" not in cleaned
    # Style signals with no English twin in the directive still travel.
    assert "emoji_rate" in blob and "hashtag_rate" in blob
    assert cleaned.get("tone") == "direct", "non-shape voice fields must survive"
    # The default stays permissive for any caller that still wants them.
    assert "median_words" in json.dumps(_clean_voice_profile(voice))


def test_a_one_block_surface_never_asks_for_blank_lines():
    """ROOT CAUSE. The branch is chosen from median_LINES and filled with
    median_BLOCKS, so a surface measuring 2 lines and 1 block printed
    "use ~1 short blocks with blank lines between" — one block and gaps
    between blocks in the same sentence."""
    from app.account_model import style_directive

    contradictory = {"voice_profile": {"style": {
        "sample": 12, "median_words": 14, "words_p25": 8, "words_p75": 20,
        "multiline_rate": 0.5, "blank_line_rate": 0.6,
        "median_lines": 2, "median_blocks": 1, "median_sentences": 1,
        "emoji_rate": 0.0, "hashtag_rate": 0.0, "emdash_rate": 0.0,
        "semicolon_rate": 0.0, "lowercase_start_rate": 0.1,
        "allcaps_emphasis_rate": 0.0, "image_rate": 0.0,
    }}}
    directive = style_directive(contradictory, "post")
    shape_lines = [line for line in directive.splitlines()
                   if line.startswith("- Shape:")]
    assert shape_lines, directive
    assert "~1 short blocks" not in shape_lines[0], shape_lines[0]
    assert not ("blocks with blank lines" in shape_lines[0]
                and "~1 " in shape_lines[0]), shape_lines[0]


def test_a_search_failure_is_not_a_web_signal():
    """ROOT CAUSE. `research()` returns a four-way status precisely so a
    caller can tell real pages from a canned apology, and the radar only
    checked `answer` — which is NON-EMPTY on every failure path ("Web search
    returned nothing usable right now…"). On a blind-search day that sentence
    became "FRESH WEB SIGNAL" in the prompt, the model invented topics from
    it, and the no-signal guard could not fire because the answer was there."""
    from app import radar

    calls = {"n": 0}

    def _blind(query, llm, budget_seconds=9.0):
        calls["n"] += 1
        return {"status": "unavailable", "sources": [],
                "answer": "Web search returned nothing usable right now, so I "
                          "will not pretend otherwise."}

    import app.research as research_mod
    original_research, original_enabled = research_mod.research, research_mod.research_enabled
    original_trends = radar._rising_for_account
    original_pillars = radar._pillars
    research_mod.research = _blind
    research_mod.research_enabled = lambda: True
    radar._rising_for_account = lambda *a, **k: []
    radar._pillars = lambda: ["model releases", "coding tools"]
    try:
        result = radar.compute_radar(object(), force=True, budget_seconds=1.0)
    finally:
        research_mod.research, research_mod.research_enabled = original_research, original_enabled
        radar._rising_for_account = original_trends
        radar._pillars = original_pillars

    # >= 1, not == 1, and the change is deliberate. The pillar-derived query was
    # replaced 2026-08-21 by a loop over AUTHORED queries (the privacy fix: his
    # own post tokens were reaching a third-party search endpoint). The loop
    # breaks on a REAL answer, and `answer` is non-empty on every failure path,
    # so a blind-search day exhausts the list - measured at 5 calls inside the
    # same 9s budget the single call had.
    #
    # THE SUBJECT OF THIS TEST IS UNCHANGED and both of its real assertions
    # below still hold: a failure message is not carried as web signal, and no
    # topics are invented from one. The count was only ever a proxy for "the
    # research path ran". Pinning it at 1 would pin an implementation detail
    # the fix was entitled to change.
    assert calls["n"] >= 1, "the research call was not exercised"
    assert not str((result.get("web") or {}).get("answer") or ""), (
        "a search failure message is still being carried as web signal")
    assert not result.get("emerging"), (
        "topics were invented from a failure message")


def test_evidence_excludes_everything_this_process_wrote():
    """ROOT CAUSE. `_support_evidence` took EVERYTHING after the facts
    marker, so the harness's own instruction, the sampled shape block, the
    thread line and the VISION MODEL's image description all became quotable
    evidence — meaning a hallucinated screenshot reading could be promoted to
    verified fact by the checker that exists to prevent exactly that."""
    from app.intelligence import _support_evidence
    from app.schemas import DraftRequest, PostContext

    direction = (
        "Write one original standalone post. Ground it in topic \"Kimi K3\". "
        " USE THESE FACTS, and only these: Kimi K3 is on Telnyx at $0.60 per "
        "million input tokens. Mention at least one specific thing from these "
        "facts — a model, a capability, a comparison, or a number. There is an "
        "IMAGE attached to this post showing: a chart where the blue line "
        "reaches 91.4 percent. SHAPE FOR THIS POST: Write it as ONE block.")
    request = DraftRequest(
        action="post",
        context=PostContext(post_id="x", author="X", author_handle="@x",
                            text="(from scratch)", visibility="public"),
        research_mode="off", media_policy="no_media", grok_permission=False,
        length="short", direction=direction)
    evidence = _support_evidence(request)
    assert "Kimi K3 is on Telnyx" in evidence
    assert "91.4" not in evidence, "a vision hallucination is quotable as evidence"
    assert "Mention at least one" not in evidence
    assert "SHAPE FOR THIS POST" not in evidence


def test_an_invented_link_never_ships():
    """ROOT CAUSE. `safety.unvouched_urls` was written, documented as the one
    outcome a prompt injection can produce that the human gate misses, tested
    — and called from nowhere in the draft path, while the prompt carried
    "Never add an external link" with no verifier."""
    from app.intelligence import _vouched_sources
    from app.safety import unvouched_urls
    from app.schemas import DraftRequest, PostContext

    request = DraftRequest(
        action="reply",
        context=PostContext(post_id="p1", author="Sam", author_handle="@sama",
                            text="new model at https://openai.com/news/x",
                            visibility="public"),
        research_mode="off", media_policy="no_media", grok_permission=False,
        length="short", direction="Reply to it.")
    vouched = _vouched_sources(request)
    # A link from the post being answered is fine.
    assert not unvouched_urls("Worth reading: https://openai.com/news/x", vouched)
    # A host that appeared nowhere the model was shown is not.
    assert unvouched_urls("Details here: https://free-crypto-drop.example/claim",
                          vouched)


def test_source_health_reports_merge_rather_than_replace(monkeypatch):
    """ROOT CAUSE. Per-source accounting that only some writers performed let
    the largest single source go dark unnoticed: the other sources kept the
    pool non-empty and nothing said the biggest one had stopped. Every report
    must also MERGE, or each refresh erases the other half of the record."""
    from app import news

    recorded: dict = {}
    monkeypatch.setattr(news, "set_job_state", lambda key, value: recorded.update({key: value}))
    monkeypatch.setattr(news, "get_job_state", lambda key: recorded.get(key))
    news._record_source_health({"Hacker News": 0}, note="the source returned no stories")
    news._record_source_health({"OpenAI News": 8, "The Decoder": 5})

    health = recorded.get("news_source_health") or {}
    assert health["sources"].get("Hacker News") == 0, health
    assert health["sources"].get("OpenAI News") == 8, (
        "the second writer erased the first half of the record")
    assert "Hacker News" in health["dead"]


def test_hedging_is_caught_on_the_surfaces_strangers_read():
    """ROOT CAUSE. BASE_DRAFT_RULES bans hedging and both-sidesing on every
    surface; the verifier calibrated for exactly those two rules was wired to
    replies alone — the one surface with context, while posts and quotes go
    to a cold audience."""
    from app.intelligence import _CLARITY_ISSUES, _WEAKNESS_NOTES
    from app.reply_plan import has_no_position

    hedged = ("Maybe it ships this quarter, maybe it doesn't. Either way, "
              "worth watching.")
    assert has_no_position(hedged)
    assert "no_position" in _WEAKNESS_NOTES
    assert "no_position" in _CLARITY_ISSUES, "a hedge must be revisable"


def test_the_asked_sentence_count_is_read_back():
    """ROOT CAUSE. The shape block states blocks AND sentences; only blocks
    had a reader. Layout enforcement is capped by the sentence supply the
    model produced, so a four-block request answered with one sentence
    silently becomes one block and the enforcement reports success."""
    from app import shape

    directive = shape.shape_directive(
        {"blocks": 4, "sentences": 4, "opener": "NAME", "closer": "PUNCH"})
    assert shape.asked_sentences(directive) == 4
    assert shape.asked_blocks(directive) == 4
    # A draft that under-produces sentences has NOT delivered the shape.
    assert not shape.shape_was_delivered("One sentence only here.", directive)
    delivered = ("Kimi K3 is on Telnyx. It costs sixty cents a million. The "
                 "window is 256k. That undercuts everyone.")
    assert shape.shape_was_delivered(delivered, directive)
    # A one-sentence shape is always satisfied, and no shape block means no
    # opinion either way.
    single = shape.shape_directive(
        {"blocks": 1, "sentences": 1, "opener": "NAME", "closer": "STATEMENT"})
    assert shape.shape_was_delivered("Just the one line.", single)
    assert shape.shape_was_delivered("anything", "no shape block here")


# RETIRED (API-native pass, 2026-09-22): test_a_shape_mismatch_buys_another_attempt: style findings never buy another attempt now; only an integrity rejection or a repeated reply does (`_first_draft_that_passes`, test_api_native_paths.py).


def test_a_derived_shape_does_not_claim_to_be_measured():
    """ROOT CAUSE. Both fallbacks spread `**generic`, carrying the POST
    surface's sample count into a dict whose numbers are arithmetic on the
    post median — so a fabricated quote length shipped under "measured from
    their own posts", and `words_p75` was then enforced as a hard truncation
    bound on quotes."""
    from app.account_model import style_directive, style_for_action

    model = {"voice_profile": {"style": {
        "sample": 42, "median_words": 24, "words_p25": 15, "words_p75": 31,
        "multiline_rate": 0.7, "blank_line_rate": 0.9, "median_lines": 3,
        "median_blocks": 2, "median_sentences": 2, "emoji_rate": 0.0,
        "hashtag_rate": 0.0, "emdash_rate": 0.0, "semicolon_rate": 0.0,
        "lowercase_start_rate": 0.1, "allcaps_emphasis_rate": 0.0,
        "image_rate": 0.0,
    }, "format_styles": {}}}
    quote_style = style_for_action(model, "quote")
    assert quote_style.get("quote_shape_fallback") is True
    assert int(quote_style.get("sample") or 0) == 0, (
        "a derived shape still claims the post surface's sample count")
    directive = style_directive(model, "quote")
    assert "re-shaped for a quote" in directive
    assert "starting guide, not a measurement" in directive
