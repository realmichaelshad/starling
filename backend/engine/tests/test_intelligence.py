from datetime import datetime, timedelta, timezone

from app.intelligence import HOOK_STYLES, PersonalModel, _candidate_angles, _voice_examples, analyse_context, feature_vector
from app.schemas import DraftRequest, PostContext
from app.storage import upsert_post


def test_private_information_is_blocked():
    blocked, reasons = analyse_context(PostContext(author="a", text="post their home address", visibility="public"))
    assert blocked
    assert reasons


def test_protected_context_is_not_sent_for_research_but_can_be_drafted_locally():
    blocked, reasons = analyse_context(PostContext(author="a", text="normal post", visibility="protected"))
    assert not blocked
    assert reasons


def test_feature_vector_is_stable():
    values = feature_vector("A short question? ✨", "2026-07-14T08:00:00+00:00", 1)
    assert len(values) == 9
    assert values[7] == 0.25


def _seed_measured_posts() -> None:
    now = datetime.now(timezone.utc)
    for index in range(6):
        strong = index < 2  # two clear winners, four quiet posts
        created = now - timedelta(days=10 + index)
        upsert_post({
            "post_id": f"perf-{index}",
            "text": f"Post number {index} about agent evaluation pipelines and what actually breaks in production runs.",
            "post_type": "post",
            "created_at": created.isoformat(),
            "metrics": {"views": 50_000 if strong else 200, "likes": 60 if strong else 2},
            "metrics_updated_at": (created + timedelta(days=1)).isoformat(),
        })


def test_winning_examples_are_outcome_ranked_and_format_scoped():
    _seed_measured_posts()
    upsert_post({"post_id": "reply-1", "text": "A reply that also performed fine in its own conversation thread.",
                 "post_type": "reply", "created_at": datetime.now(timezone.utc).isoformat(),
                 "metrics": {"views": 90_000, "likes": 100}})
    model = PersonalModel()
    winners = model.winning_examples("post", limit=2)
    assert len(winners) == 2
    assert all("Post number" in text for text in winners)  # never borrows the reply
    assert "Post number 0" in winners[0] or "Post number 1" in winners[0]


def test_voice_examples_blend_similar_with_winners_without_duplicates():
    _seed_measured_posts()
    # An off-topic pool so creation still has style material after the
    # on-topic exclusion (the anti-copy root-cause fix).
    now = datetime.now(timezone.utc)
    for index in range(4):
        upsert_post({"post_id": f"offtopic-{index}",
                     "text": f"Weekend woodworking note {index}: sharp chisels and patient sanding beat expensive routers.",
                     "post_type": "post", "created_at": (now - timedelta(days=40 + index)).isoformat(),
                     "metrics": {"views": 900, "likes": 4}, "metrics_updated_at": (now - timedelta(days=39 + index)).isoformat()})
    model = PersonalModel()
    request = DraftRequest(action="post", context=PostContext(author="a", text="agent evaluation pipelines", visibility="public"))
    examples = _voice_examples(model, request)
    assert len(examples) == len(set(examples))
    assert examples
    enhance = DraftRequest(action="post", enhancement_mode=True, original_draft="x",
                           context=PostContext(author="a", text="agent evaluation pipelines", visibility="public"))
    assert len(_voice_examples(model, enhance)) <= 3  # enhance keeps the prompt lean


def test_creation_sees_its_most_similar_own_posts_and_the_copy_guard_holds():
    """V2 (2026-09-21) reverses the old on-topic exclusion: the writer's most similar
    own posts show their voice on this subject best. What made the exclusion feel
    necessary ('Create copied my old post word for word') is caught where it happens:
    a draft that repeats one of them is `copied_voice_example`, an integrity issue."""
    from app.intelligence import _INTEGRITY_ISSUES, _draft_quality_issues
    _seed_measured_posts()  # all about agent evaluation pipelines
    now = datetime.now(timezone.utc)
    upsert_post({"post_id": "woodwork", "text": "Weekend woodworking: sharp chisels and patient sanding beat expensive routers.",
                 "post_type": "post", "created_at": (now - timedelta(days=50)).isoformat(),
                 "metrics": {"views": 800, "likes": 3}, "metrics_updated_at": (now - timedelta(days=49)).isoformat()})
    model = PersonalModel()
    on_topic = DraftRequest(action="post", context=PostContext(
        author="a", text="agent evaluation pipelines and what actually breaks in production runs", visibility="public"))
    examples = _voice_examples(model, on_topic)
    assert "agent evaluation" in examples[0], "the most similar own post leads"
    copied = _draft_quality_issues(str(examples[0]), on_topic, examples)
    assert "copied_voice_example" in copied and "copied_voice_example" in _INTEGRITY_ISSUES
    different = DraftRequest(action="post", context=PostContext(
        author="a", text="thoughts on woodworking chisels", visibility="public"))
    assert "woodworking" in _voice_examples(model, different)[0]


def test_candidate_angles_vary_hook_style_across_attempts():
    request = DraftRequest(action="post", context=PostContext(author="a", text="agents in production", visibility="public"))
    angles = _candidate_angles(request, 3)
    instructions = [angle.get("instruction", "") for angle in angles]
    assert len(instructions) == 3
    used_styles = [style for style in HOOK_STYLES if any(style in instruction for instruction in instructions)]
    assert len(used_styles) == 3  # each attempt opens differently


def test_protected_archive_examples_are_excluded_from_remote_retrieval():
    upsert_post({"post_id": "private-1", "text": "private voice example", "visibility": "protected"})
    model = PersonalModel()
    # The strategy manual's PUBLIC exemplars may appear; the protected archive
    # text must never leave through the remote-safe path.
    assert "private voice example" not in model.similar_examples("private voice example", remote_safe=True)
    assert "private voice example" in model.similar_examples("private voice example", remote_safe=False)


# --------------------------------------------------------------------------- #
# A quote must not restate the post it quotes
# --------------------------------------------------------------------------- #

_QUOTED = ("Hot take: the open-weight releases this month are the real reason the "
           "next Opus is delayed. Anthropic won't release a flagship that only "
           "matches a model anyone can download for free.")

_VERBATIM = ("The open-weight releases this month are the real reason the next Opus "
             "is delayed. Anthropic won't release a flagship that only matches a "
             "model anyone can download for free.")

_PARAPHRASE = ("The open-weight releases might be holding the next Opus back. "
               "Anthropic won't release a flagship that only matches a free "
               "download. OpenAI's response to this could change everything.")

_DISTINCT = ("The delay theory assumes Anthropic ships on capability. They ship on "
             "eval safety margins, which is why the gap between announcement and "
             "release keeps widening regardless of who else launches.")


def _quote_request(quoted: str = _QUOTED) -> DraftRequest:
    return DraftRequest(action="quote", research_mode="off", media_policy="no_media",
                        grok_permission=False,
                        context=PostContext(author="Alex Rivera", author_handle="example_builder",
                                            text=quoted, post_id="q1", visibility="public"))


def test_source_mirroring_separates_a_copy_from_a_contribution():
    from app.growth_scorer import _source_mirroring

    assert _source_mirroring(_VERBATIM, _QUOTED) >= 0.9
    assert _source_mirroring(_DISTINCT, _QUOTED) < 0.2
    # The shipped paraphrase sits between the two, and above the distinct point.
    assert _source_mirroring(_DISTINCT, _QUOTED) < _source_mirroring(_PARAPHRASE, _QUOTED)


def test_quality_gate_has_a_ceiling_not_only_a_floor():
    """Regression: the only similarity check was `missing_context_connection`,
    a FLOOR. Restating the source is the easiest way to clear a floor, so a
    verbatim copy of the quoted post passed the gate cleanly."""
    from app.intelligence import _draft_quality_issues

    request = _quote_request()
    assert "restates_the_source" in _draft_quality_issues(_VERBATIM, request, [])
    # ...without rejecting a real contribution on the same topic.
    assert _draft_quality_issues(_DISTINCT, request, []) == []


def test_audience_payoff_measures_share_not_raw_count():
    """Regression: payoff was len(new_tokens)/12.0 — an ABSOLUTE count — so a
    paraphrase scored full marks just by being long enough to pick up twelve
    incidental new words. The shipped restatement measured 0.92 that way."""
    from app.growth_scorer import _spec_terms

    payoff = lambda text: _spec_terms(text, "quote", _QUOTED)["audience_payoff"]
    assert payoff(_VERBATIM) == 0.0
    assert payoff(_PARAPHRASE) < 0.55, "the shipped draft must no longer look good"
    assert payoff(_DISTINCT) > 0.7
    assert payoff(_PARAPHRASE) < payoff(_DISTINCT)


def test_restating_the_source_is_penalised_in_scoring():
    from app.growth_scorer import score_candidate

    _, terms = score_candidate(_VERBATIM, action="quote", objective="reach",
                               context_text=_QUOTED)
    assert terms.get("restates_the_source", 0) <= -12.0
    _, clean = score_candidate(_DISTINCT, action="quote", objective="reach",
                               context_text=_QUOTED)
    assert "restates_the_source" not in clean


def test_reply_and_quote_prompts_carry_the_do_not_restate_rule():
    """Every existing 'do not restate' rule was scoped to the writer's PAST
    posts; nothing mentioned the post being replied to or quoted."""
    from app.llm import _draft_rules

    for action in ("reply", "quote"):
        request = _quote_request()
        request.action = action
        rules = _draft_rules(request)
        assert "Never summarise or paraphrase it." in rules
        # ("if the original were deleted" left with the rest of the responding rules: API-native pass.)

    # A standalone post has no source, so the rule must not appear.
    standalone = DraftRequest(action="post", research_mode="off",
                              context=PostContext(author="a", text="", visibility="public"))
    assert "Never summarise or paraphrase it." not in _draft_rules(standalone)


# --------------------------------------------------------------------------- #
# Spacing follows the WRITER's measured habit, not the model's whim
# --------------------------------------------------------------------------- #

_ROOMY = {"multiline_rate": 0.93, "blank_line_rate": 0.93, "median_blocks": 3}
_COMPACT = {"multiline_rate": 0.0, "blank_line_rate": 0.0, "median_blocks": 1}

_THREE_THOUGHTS = ("Local models are getting cheaper every month. The gap with "
                   "hosted inference is closing fast. Most teams have not noticed yet.")
_MODEL_SINGLE_NL = ("Local models are getting cheaper every month.\nThe gap with "
                    "hosted inference is closing fast.\nMost teams have not noticed yet.")
_MODEL_BLANK_NL = ("Local models are getting cheaper every month.\n\nThe gap with "
                   "hosted inference is closing fast.\n\nMost teams have not noticed yet.")


def test_roomy_writer_gets_blank_lines_whatever_the_model_emitted():
    """Regression: `_reshape_to_style` returned early on any draft that already
    had two newlines ("already has structure"). The 9B emits some newline on
    most drafts, so layout was handed to the model and a writer measured at a
    0.93 blank-line rate shipped single-newline drafts."""
    from app.intelligence import _reshape_to_style

    for source in (_THREE_THOUGHTS, _MODEL_SINGLE_NL, _MODEL_BLANK_NL):
        out = _reshape_to_style(source, _ROOMY)
        assert "\n\n" in out, "the writer's measured blank line must be applied"
        assert out.count("\n\n") == 2, "three blocks, two gaps"
        assert "\n\n\n" not in out


def test_compact_writer_never_keeps_invented_line_breaks():
    """The mirror image: the `multiline_rate < 0.4` guard returned early, so it
    only skipped ADDING breaks — it never removed the ones the model invented,
    on exactly the accounts measured never to use them. Replies take this path
    by design via style_for_action's reply fallback."""
    from app.intelligence import _reshape_to_style

    for source in (_THREE_THOUGHTS, _MODEL_SINGLE_NL, _MODEL_BLANK_NL):
        out = _reshape_to_style(source, _COMPACT)
        assert "\n" not in out, "a compact writer's draft must be one block"


def test_spacing_change_never_moves_the_words_or_their_order():
    """Only the gaps may change. Same words, same order, nothing dropped."""
    from app.intelligence import _reshape_to_style

    for style in (_ROOMY, _COMPACT):
        for source in (_THREE_THOUGHTS, _MODEL_SINGLE_NL, _MODEL_BLANK_NL):
            out = _reshape_to_style(source, style)
            assert out.split() == source.split()


def test_a_single_sentence_is_never_fragmented():
    from app.intelligence import _reshape_to_style

    one = "Local models are getting cheaper every month."
    assert _reshape_to_style(one, _ROOMY) == one


def test_layout_never_pushes_a_draft_past_the_x_limit():
    """Blank lines cost characters; a block that fits beats a pretty one."""
    from app.intelligence import _reshape_to_style

    # Eight 34-character blocks: 279 with single newlines, 286 with blank ones.
    blocks = ["t" * 30 + str(n) + "abc" for n in range(8)]
    long_draft = "\n".join(blocks)
    assert len(long_draft) <= 280 < len("\n\n".join(blocks))

    out = _reshape_to_style(long_draft, _ROOMY)
    assert len(out) <= 280
    assert out == long_draft, "it must decline to re-space rather than overflow"


def test_reply_style_is_compact_until_real_reply_samples_exist():
    """style_for_action deliberately refuses to infer reply spacing from a post
    archive — and the formatter must now actually enforce that."""
    from app.account_model import style_for_action
    from app.intelligence import _reshape_to_style

    model = {"voice_profile": {"style": {"multiline_rate": 0.93, "blank_line_rate": 0.93},
                               "format_styles": {"post": {"sample": 15, "multiline_rate": 0.93,
                                                          "blank_line_rate": 0.93}}}}
    reply_style = style_for_action(model, "reply")
    assert reply_style["blank_line_rate"] == 0.0
    assert reply_style.get("reply_shape_fallback") is True
    assert "\n" not in _reshape_to_style(_MODEL_BLANK_NL, reply_style)


# --------------------------------------------------------------------------- #
# The scorer must be able to tell real writing from slop
# --------------------------------------------------------------------------- #

_SLOP = (
    "Fable 5 launched with high performance.\n\nClaude's AI is coming back strong."
    "\n\nIt's timely, not a trend.\n\nDon't miss the rise.",
    "This isn't just a tool. It's a new way to think about code safety.",
    "AI is changing everything. The future is here. Don't get left behind.",
    "Big things are coming in AI this year. You can feel the momentum building.",
)

_REAL = (
    "My guess is Gemini 4 slips past October because of the new open-weight "
    "coding models.\n\nNo lab wants a flagship launch that only ties something "
    "people can download for free.",
    "Cursor quietly became a code review team that lives in the editor.\n\n"
    "It writes the diff and then tells you which test it is about to break.",
    "Prediction: Gemini 4 launches on October 14, 2026.",
)


def _post_request():
    return DraftRequest(action="post", research_mode="off",
                        context=PostContext(author="a", text="", visibility="public"))


def test_scorer_separates_real_writing_from_generic_hype():
    """Regression: it could not. Measured on this account's own archive, real
    posts averaged 63.70 and slop 63.80 — the slop scored HIGHER. Content-free
    hype took full marks on audience_payoff because, with no context to be new
    relative to, the term had collapsed into 'is it at least twelve words'."""
    from app.growth_scorer import score_candidate

    def avg(texts):
        return sum(score_candidate(t, action="post", objective="reach",
                                   context_text="")[0] for t in texts) / len(texts)

    assert avg(_REAL) - avg(_SLOP) > 5.0


def test_empty_hype_never_takes_full_audience_payoff():
    from app.growth_scorer import _spec_terms

    hype = "AI is changing everything. The future is here. Don't get left behind."
    real = "Prediction: Gemini 4 launches on October 14, 2026."
    assert _spec_terms(hype, "post", "")["audience_payoff"] < 0.3
    assert _spec_terms(real, "post", "")["audience_payoff"] > 0.7


def test_says_nothing_gate_blocks_slop_and_spares_real_posts():
    """Both conditions are required: this account writes genuinely short punchy
    posts carrying a single anchor, and those must still ship."""
    from app.intelligence import _draft_quality_issues

    request = _post_request()
    for text in _SLOP:
        assert "says_nothing" in _draft_quality_issues(text, request, []), text[:40]
    for text in _REAL:
        assert "says_nothing" not in _draft_quality_issues(text, request, []), text[:40]


# A spread of posts in a real published register, inlined so this runs
# anywhere. Synthetic, but chosen to cover the same hard cases: a one-anchor
# punchline, a bare list header, a casual aside with a typo, and a long
# evidence-stacked prediction.
_PUBLISHED = (
    "Cursor quietly became a code review team that lives in the editor.\n\n"
    "It writes the diff and then tells you which test it is about to break.",
    "If the leak is accurate, DeepSeek V4 was trained on outputs from a closed "
    "model.\n\nThat makes the open-weight lead a lot less independent than it looks.",
    "My guess is Gemini 4 slips past October because of the new open-weight "
    "coding models.\n\nNo lab wants a flagship launch that only ties something "
    "people can download for free.",
    "Gemini just flagged the official Google Cloud status page as a phishing link "
    "lol\n\nthis is definately the future we were promised",
    "Gemini 4 is closer than people think.\n\nGemini 3.7 Flash, the cheap tier, "
    "already matches Gemini 3 Pro on most coding benchmarks.\n\nWhen the budget "
    "model catches the flagship, the next flagship is usually weeks out.",
    "Calling it: GPT-5.6 Sol stays on the free plan only until October 1.\n\n"
    "Once the next Sol ships, OpenAI moves 5.6 behind the Plus plan.\n\n"
    "Just my read on the pricing page.",
    "Prediction: Gemini 4 launches on October 14, 2026.",
    "Everything that shipped today:\n\nGemini 3.7 launched with Flash, Pro and "
    "Ultra tiers.\n\nCursor added background agents that run tests in parallel.",
)


def test_no_published_post_is_rejected_as_saying_nothing():
    """The strongest guard available: the writer's own published work. A gate
    that would have blocked something they actually posted is miscalibrated,
    however well it separates on average."""
    from app.intelligence import _draft_quality_issues

    request = _post_request()
    rejected = [text for text in _PUBLISHED
                if "says_nothing" in _draft_quality_issues(text, request, [])]
    assert not rejected, f"would have rejected the writer's own posts: {rejected}"


def test_no_slop_can_ship_by_either_route():
    """An average can hide an inversion, so assert the contract the pipeline
    actually offers: a slop draft never reaches the user. Either the gate
    rejects it, or it loses to every post the writer has published.

    Both routes matter. Scoring alone is not enough — 'Fable 5 launched with
    high performance / Don't miss the rise' still scores well on format (four
    tidy short blocks) and is stopped by the gate, not by its score.
    """
    from app.growth_scorer import score_candidate
    from app.intelligence import _draft_quality_issues

    request = _post_request()

    def score(text):
        return score_candidate(text, action="post", objective="reach", context_text="")[0]

    floor = min(score(text) for text in _PUBLISHED)
    for text in _SLOP:
        gated = "says_nothing" in _draft_quality_issues(text, request, [])
        assert gated or score(text) < floor, f"this could ship: {text[:60]!r}"


def test_hype_is_penalised_in_scoring():
    from app.growth_scorer import score_candidate

    _, terms = score_candidate("This isn't just a tool. It's a new way to think about it.",
                               action="post", objective="reach", context_text="")
    assert terms.get("empty_hype", 0) <= -7.0
    _, clean = score_candidate(_REAL[0], action="post", objective="reach", context_text="")
    assert "empty_hype" not in clean


# --------------------------------------------------------------------------- #
# A QUOTE THAT COPIES ITS SOURCE — the gate was switched off on the one surface
# where copying is worst
# --------------------------------------------------------------------------- #

_SAMA_POST = "i see your moore's law and i raise you 20x"


def _sama_quote_request(source: str):
    """A quote whose SOURCE is the @sama post — distinct from `_quote_request`
    at the top of this file, which defaults its quoted text.

    Named apart deliberately. The first version of this helper reused
    `_quote_request`, which is defined 300 lines above; Python takes the last
    definition for the whole module, so the two callers of the original
    (`:155`, `:191`) began raising TypeError while the tests written against
    this one passed. A shadowed helper is silent for the new code and fatal for
    the old, and both shadowed tests were absent — not failing — from a
    3,037-test run.
    """
    from app.schemas import DraftRequest, PostContext
    return DraftRequest(
        action="quote", research_mode="off", media_policy="no_media",
        grok_permission=False,
        context=PostContext(author="sama", author_handle="@sama",
                            text=source, visibility="public"))


def test_a_quote_that_copies_its_source_is_an_integrity_failure():
    """Measured live 2026-08-07 on 22 real captured posts.

    Quoting `@sama`'s "i see your moore's law and i raise you 20x", the draft
    came back as that sentence CHARACTER FOR CHARACTER. The restatement gate
    named it — "this leans on saying the post back" — and it shipped anyway,
    because nothing on the quote surface treated copying as an integrity
    failure. `_reply_failures` raises `copies_the_post`, and its call site was
    gated to `action == "reply"`.

    On a quote this matters MORE than on a reply: the quoted post is rendered
    directly underneath, so a verbatim copy puts the duplicate above the
    original.
    """
    from app.intelligence import _draft_quality_issues
    issues = _draft_quality_issues(_SAMA_POST + ".", _sama_quote_request(_SAMA_POST), [])
    assert "copies_the_post" in issues


def test_only_the_copy_check_crossed_over_not_the_container():
    """THE REPAIR IS ONE CHECK, NOT THE FUNCTION, AND THAT IS THE POINT.

    `_reply_failures` raises eight issues and the gate sits on the container,
    so "add quote to the action list" would have enabled all eight. Measured on
    the same 22: `intruders` (which raises `leaked_into_reply`) fires on 4 quote
    drafts and THREE ARE FALSE — "Iron Man" answering a post that said "Tony
    Stark", an author's own surname, and "LotR" for a source that wrote "Lord
    of the Rings". It would reject the best draft in that run.

    So this pins the narrowness: a quote draft naming something absent from its
    source must NOT raise the reply-lane leak.
    """
    from app.intelligence import _draft_quality_issues
    draft = "OpenAI isn't giving you Iron Man. They're just automating a junior dev's job."
    source = "OpenAI just turned everyone into Tony Stark with ChatGPT voice.\n\nNow this runs inside Codex."
    issues = _draft_quality_issues(draft, _sama_quote_request(source), [])
    assert "leaked_into_reply" not in issues
    assert "bounced_question" not in issues


def test_a_quote_that_does_not_copy_is_untouched():
    from app.intelligence import _draft_quality_issues
    draft = "Half the price is the whole story here."
    issues = _draft_quality_issues(draft, _sama_quote_request(_SAMA_POST), [])
    assert "copies_the_post" not in issues


# --------------------------------------------------------------------------- #
# THE TWO REGISTRIES MUST AGREE — a fence, not a finding
# --------------------------------------------------------------------------- #

def test_every_quality_key_has_a_note_and_no_key_is_in_both_sets():
    """Both assertions pass on arrival; this guards an invariant rather than
    reporting a defect.

    The registries drifted apart once already — `intelligence.py:1506` records
    `cut_off` being pulled back out of `_INTEGRITY_ISSUES` because "an issue in
    both sets means a QUALITY miss vanishes into silence instead of shipping
    named". That audit was run by hand on 2026-07-30 and nothing automated it,
    so it gets re-run by whoever next happens to look.

    THE ASSERTION IS THE COMPLEMENT, NOT THE OBVIOUS ONE. "Every emitted key has
    a note" fails on all five integrity keys and would drive exactly the fix the
    comment forbids — an integrity issue must NOT have a note, because it can
    never reach the near-miss lookup (`integrity_failed` gates that branch, and
    `quality_only_issues` is only assigned when no integrity key is present).

    THE SCAN COVERS `app/*.py`, NOT `intelligence.py`. The keys arrive by
    FAN-IN — `:5459` extends from `_readability_issues`, `:5759` from
    `_reply_failures` — and both producers merely happen to live in this module
    today. Move one to another file, or write a new one there, and a
    single-file grep reports 0 missing while a whole family goes unaudited: it
    would not fail, it would PASS silently, which is the failure mode this test
    exists to prevent, one level up, in the test itself.

    AND THE LITERAL-ONLY PROPERTY IS ASSERTED, NOT ASSUMED. The scan can only
    see `issues.append("literal")`; a key appended through a variable would be
    invisible. So the second assertion below fixes that by construction —
    every append site must carry a string literal — which makes the first scan
    complete rather than merely thorough. Positive structural check, not an
    absence one.
    """
    import re as _re
    from pathlib import Path as _Path
    from app.intelligence import _INTEGRITY_ISSUES, _WEAKNESS_NOTES

    assert not (set(_WEAKNESS_NOTES) & set(_INTEGRITY_ISSUES)), (
        "a key in both sets makes a quality miss ship silently — see the "
        "`cut_off` note at intelligence.py:1506")

    app_dir = _Path(__file__).resolve().parents[1] / "app"
    sources = {path.name: path.read_text(encoding="utf-8", errors="replace")
               for path in sorted(app_dir.glob("*.py"))}
    assert "intelligence.py" in sources, "app/ did not resolve"

    # 1. Every append site carries a literal, so the scan below can see it all.
    dynamic: list[str] = []
    for name, source in sources.items():
        for match in _re.finditer(r"issues\.append\(\s*([^\s)])", source):
            if match.group(1) not in "\"'":
                line = source[:match.start()].count("\n") + 1
                dynamic.append(f"{name}:{line}")
    assert not dynamic, (
        "an issue key appended through a variable is invisible to the registry "
        f"audit below, so this guard would pass while that key went unchecked: {dynamic}")

    # 2. Now the literal scan is complete by construction.
    emitted: set[str] = set()
    for source in sources.values():
        emitted |= set(_re.findall(r'issues\.append\(\s*"([a-z_]+)"', source))
        emitted |= set(_re.findall(r'issues\.extend\(\s*\[\s*"([a-z_]+)"', source))
    missing = {key for key in emitted
               if key not in _INTEGRITY_ISSUES and key not in _WEAKNESS_NOTES}
    assert not missing, (
        f"emitted quality keys with no _WEAKNESS_NOTES entry, so they fall to "
        f"the generic shrug the registry exists to replace: {sorted(missing)}")


# --------------------------------------------------------------------------- #
# A self-reply may name his own beat — the continue branch of `intruders`
# --------------------------------------------------------------------------- #

_OWN_PARENT = ("Honestly the eval crowd is right, open models need independent testing.\n\n"
               "The problem is who pays for it.")


def _own_thread_request():
    return DraftRequest(action="reply", research_mode="off", media_policy="no_media",
                        grok_permission=False, direction="", length="short",
                        context=PostContext(author="Alex Rivera",
                                            author_handle="@example_builder",
                                            text=_OWN_PARENT, is_own_post=True,
                                            visibility="public"))


def test_a_self_reply_may_name_something_he_has_published():
    """The continue brief asks for "the detail you left out" and `intruders`
    rejected any name the parent paragraph lacked — 50% of 120 continuation
    pairs from a real archive, typically a follow-up naming a company the
    account had already posted about."""
    from app.intelligence import _reply_failures
    upsert_post({"post_id": "beat-1", "post_type": "post",
                 "text": "Google shipped Gemini Live and the latency is the story.",
                 "created_at": datetime.now(timezone.utc).isoformat()})
    from app.intelligence import _published_names
    _published_names.__globals__["_PUBLISHED_NAMES_CACHE"] = None
    issues = _reply_failures("Now let's see how Google responds with Gemini.",
                             _own_thread_request())
    assert "leaked_into_reply" not in issues, issues


def test_a_name_from_outside_his_beat_is_still_an_intruder_on_his_own_thread():
    """Widened, never skipped: skipping leaves the surface with no name gate at
    all, because `apply_support_check` excludes replies by design."""
    from app.intelligence import _reply_failures, _published_names
    _published_names.__globals__["_PUBLISHED_NAMES_CACHE"] = None
    for planted in ("It also happened at Tesco last year.",
                    "Ask anyone at Goldman Sachs and they will tell you the same.",
                    "This is why Peloton stopped shipping."):
        assert "leaked_into_reply" in _reply_failures(planted, _own_thread_request()), planted


def test_the_vocabulary_is_his_posts_and_never_starlings_drafts():
    """THE TRAP THIS FIX EXISTS TO AVOID. `_own_writing()` is the obvious
    accessor and it returns everything he wrote OR STARLING DRAFTED — 126 names
    he has never used, mostly arena residue. Feeding those to a name gate is the
    gate learning to accept its own hallucinations."""
    from app.intelligence import _published_names
    _published_names.__globals__["_PUBLISHED_NAMES_CACHE"] = None
    names = _published_names()
    assert "dharmaocr" not in names and "animsequence" not in names


def test_the_other_checks_still_fire_on_a_continue():
    """Only `intruders` is widened. A self-reply that copies his own post is
    still a dead post."""
    from app.intelligence import _reply_failures
    issues = _reply_failures(_OWN_PARENT.split("\n\n")[0], _own_thread_request())
    assert issues, "a verbatim copy of his own post must still be caught"
