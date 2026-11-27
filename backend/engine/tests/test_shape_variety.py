"""Shape variety, measured (2026-07-29).

The register work made drafts SOUND like the account; they still all LOOKED
alike. Census over 663 generated drafts vs the 40 archived texts: 71.3% of
drafts were exactly two blocks against his 27.5%, his 3-5 block posts (37.5%
of the archive) appeared in 0.3% of drafts, and lowercase openers (7 of his
40) appeared once in 663. The cause is instructing the MEDIAN shape, which
has zero variance by construction.

These tests hold the sampler to the account's real distribution and to the
per-account rule: an archive of anything yields that archive's shapes.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import shape                                     # noqa: E402
from app import storage                                   # noqa: E402


def test_signature_reads_the_silhouette():
    one_liner = "Opus 5 runs at Opus speed and costs the same."
    assert shape.signature(one_liner) == "1B/1S/NAME/STATEMENT"
    three_block = ("goose is free.\n\n"
                   "Claude Code is two hundred a month.\n\n"
                   "Same drafts.")
    assert shape.signature(three_block) == "3B/3S/LOWER/PUNCH"
    pointing = "It's an undo button for agents. Snapshield ships today."
    assert shape.opener_class(pointing) == "PRONOUN"
    numeric = "$0.60 per million tokens. That's the whole pitch."
    assert shape.opener_class(numeric) == "NUMBER"
    asking = "The gap is thirteen points. So why ship it?"
    assert shape.closer_class(asking) == "QUESTION"


def test_coarse_signature_ignores_sentence_count():
    """Two drafts at 2B/2S/NAME and 2B/3S/NAME are the same post to a reader,
    so the anti-repeat axis must collapse them."""
    a = "Cursor cut latency. It runs on a smaller model now."
    b = "Cursor cut latency. It runs on a smaller model. That's the trade."
    assert shape.signature(a) != shape.signature(b)
    assert shape.coarse_signature(a) == shape.coarse_signature(b)


@pytest.fixture()
def archive(request):
    """Install a synthetic archive so the distribution is known exactly."""
    texts = request.param
    with storage.connect() as conn:
        for index, text in enumerate(texts):
            conn.execute("insert into posts (post_id, text) values (?, ?)",
                         (f"shape-{index}", text))
        conn.commit()
    shape.reset_shape_cache()
    yield texts
    shape.reset_shape_cache()


_SPREAD = (
    ["one block only, lowercase."] * 4
    + ["Name leads here. Second sentence follows."] * 3
    + ["Name leads.\n\nSecond block.\n\nThird block.\n\nFourth."] * 3
)


@pytest.mark.parametrize("archive", [_SPREAD], indirect=True)
def test_distribution_is_measured_not_assumed(archive):
    distribution = shape.account_shape_distribution()
    assert distribution["sample"] == 10
    assert distribution["shapes"]["1B/1S/LOWER/PUNCH"] == 4
    # The four-block shape is present, which is what the median can never say.
    assert any(sig.startswith("4B") for sig in distribution["shapes"])


@pytest.mark.parametrize("archive", [_SPREAD], indirect=True)
def test_sampling_offers_the_whole_spread_not_the_median(archive):
    picker = random.Random(7)
    seen = {shape.sample_shape(rng=picker)["signature"] for _ in range(60)}
    assert len(seen) >= 3, f"sampler collapsed onto {seen}"
    assert any(s.startswith("4B") for s in seen), "never offered his 4-block shape"


@pytest.mark.parametrize("archive", [_SPREAD], indirect=True)
def test_exclusion_avoids_the_last_drafts_shape(archive):
    picker = random.Random(11)
    for _ in range(20):
        target = shape.sample_shape(exclude=["1B/LOWER"], rng=picker)
        assert shape.coarse_signature_of_sig(target["signature"]) != "1B/LOWER"


@pytest.mark.parametrize("archive", [_SPREAD], indirect=True)
def test_excluding_everything_still_returns_a_shape(archive):
    """A repeated shape beats no guidance — falling back to the median is how
    the collapse happened in the first place."""
    everything = [shape.coarse_signature_of_sig(s)
                  for s in shape.account_shape_distribution()["shapes"]]
    assert shape.sample_shape(exclude=everything, rng=random.Random(3))


# ------------------------- size is voice too (an owner instruction, 2026-07-31)
# Holding a 13-word draft, the instruction was that writing in the account's
# own voice includes its size: work out the best length for the material and
# never keep repeating one size. Measured that night: archive median 24 words
# with 30% at 31-50; the last 120 drafts had collapsed to median 17.5 with 40%
# at <=15. The size plan reads the MATERIAL and the recent RUN; the sampler
# then draws inside the planned class, from the account's own silhouettes only.

_SIZED = (
    ["goose is free. that's the story."] * 4
    + ["Claude Code costs two hundred a month on the Max plan. Goose does "
       "the same work against local models for free. Same drafts, same "
       "terminal."] * 3
    + ["Anthropic shipped Opus at half the old price today.\n\n"
       "The benchmark gap to Fable is two points on coding and the context "
       "window doubled to a million tokens.\n\n"
       "Every small team doing agent work just got the build versus buy "
       "math rewritten for them this quarter."] * 3
)


def test_size_classes_cut_at_his_thirds():
    assert shape.size_class(13) == "short"
    assert shape.size_class(15) == "short"
    assert shape.size_class(16) == "medium"
    assert shape.size_class(30) == "medium"
    assert shape.size_class(31) == "long"


def test_the_size_plan_reads_the_material():
    rich = shape.plan_size_classes(600)
    assert rich[0] == "long"
    moderate = shape.plan_size_classes(200)
    assert moderate[0] == "medium"
    thin = shape.plan_size_classes(60)
    assert thin[0] == "short"
    # Thin material is never stretched into a long post — that is how
    # invented filler gets written.
    assert "long" not in thin


def test_the_size_plan_breaks_a_run():
    """Two same-class drafts running send that class to the back — three
    reply-sized posts in a row is the live complaint."""
    plan = shape.plan_size_classes(200, ["medium", "medium"])
    assert plan[0] != "medium" and plan[-1] == "medium"
    unbroken = shape.plan_size_classes(200, ["medium", "short"])
    assert unbroken[0] == "medium"


@pytest.mark.parametrize("archive", [_SIZED], indirect=True)
def test_sampling_draws_inside_the_planned_class(archive):
    """size_classes=["long"] must only ever offer silhouettes of his long
    posts — the size is chosen, the silhouette stays his."""
    long_sigs = {shape.signature(t) for t in _SIZED if len(t.split()) >= 31}
    picker = random.Random(5)
    for _ in range(20):
        target = shape.sample_shape(rng=picker, size_classes=["long"])
        assert target["signature"] in long_sigs, target["signature"]


@pytest.mark.parametrize("archive", [_SPREAD], indirect=True)
def test_a_class_his_archive_lacks_falls_through(archive):
    """_SPREAD has no long posts; asking for long still returns one of his
    real shapes rather than nothing."""
    target = shape.sample_shape(rng=random.Random(9), size_classes=["long"])
    assert target is not None
    assert target["signature"] in shape.account_shape_distribution()["shapes"]


def test_recent_sizes_round_trip():
    shape.record_size("goose is free. that's the story.")
    shape.record_size(" ".join(["word"] * 40))
    sizes = shape.recent_sizes()
    assert sizes[0] == "long"
    assert sizes[1] == "short"


def test_the_word_floor_reads_the_ask_back():
    """His press, 2026-07-31: asked "2 blocks, 2 sentences, about 24 words",
    shipped "vLLM lets you run open models locally." — seven words that
    passed the sentence check at wanted-1. The word floor is the door."""
    direction = ("SHAPE FOR THIS POST: Write it as 2 blocks separated by "
                 "blank lines, 2 sentences total, about 24 words; open by "
                 "pointing at it, then name it; end on a full statement.")
    assert shape.asked_words(direction) == 24
    assert not shape.size_was_delivered(
        "vLLM lets you run open models locally.", direction)
    # A modest miss on a big shape is model wobble, not a collapse.
    assert shape.size_was_delivered(
        "vLLM runs open models on your own hardware. No account, no "
        "server, no vendor deciding what loads.", direction)
    # A planned SHORT post at seven words is his own voice.
    short_plan = ("SHAPE FOR THIS POST: Write it as ONE block, 1 sentence, "
                  "about 14 words; end on a full statement.")
    assert shape.size_was_delivered(
        "vLLM lets you run open models locally.", short_plan)
    # No shape sampled -> no opinion.
    assert shape.size_was_delivered("anything at all", "")


def test_an_undersized_survivor_ships_with_the_weakness_named():
    """When retries die to the repeat corpus and a short survivor ships
    against a bigger plan, the card says so — never a silent shrug, never a
    block (NEVER1's law)."""
    from app.intelligence import (_INTEGRITY_ISSUES, _WEAKNESS_NOTES,
                                  _draft_quality_issues)
    from app.schemas import DraftRequest, PostContext
    assert "under_planned_size" in _WEAKNESS_NOTES
    assert "under_planned_size" not in _INTEGRITY_ISSUES
    request = DraftRequest(action="post", context=PostContext(
        author="a", text="", visibility="public"))
    request.direction = ("Ground it in topic \"Together AI serves Kimi\". "
                         "SHAPE FOR THIS POST: Write it as 2 blocks separated "
                         "by blank lines, 2 sentences total, about 24 words.")
    issues = _draft_quality_issues(
        "Together AI just added native Kimi support.", request, [])
    assert "under_planned_size" in issues
    # A planned short post is his voice — no flag.
    request.direction = ("SHAPE FOR THIS POST: Write it as ONE block, "
                         "1 sentence, about 14 words.")
    issues = _draft_quality_issues(
        "Together AI just added native Kimi support.", request, [])
    assert "under_planned_size" not in issues


# RETIRED (API-native pass, 2026-09-22): test_the_retry_door_fires_on_an_undersized_delivery: style findings never buy another attempt now; only an integrity rejection or a repeated reply does (`_first_draft_that_passes`, test_api_native_paths.py).


def test_a_cold_account_gets_no_invented_shape():
    shape.reset_shape_cache()
    assert shape.sample_shape() is None


@pytest.mark.parametrize("archive", [_SPREAD], indirect=True)
def test_directive_is_concrete_and_names_the_case_habit(archive):
    text = shape.shape_directive(
        {"blocks": 4, "sentences": 5, "opener": "LOWER", "closer": "PUNCH"})
    assert "4 blocks" in text
    assert "lowercase" in text.lower()
    assert "five words or fewer" in text
    # A lowercase OPENER is a habit; a lowercase product name is a mistake.
    # Live 2026-07-29: "vibeue edits animsequences... it drives metasond".
    assert "capitalised normally" in text
    single = shape.shape_directive(
        {"blocks": 1, "sentences": 1, "opener": "NAME", "closer": "STATEMENT"})
    assert "ONE block" in single
    assert shape.shape_directive(None) == ""


@pytest.mark.parametrize("archive", [_SPREAD], indirect=True)
def test_recent_shapes_round_trip(archive):
    shape.record_shape("Name leads.\n\nSecond block.")
    assert shape.recent_shapes()[0] == "2B/NAME"
    shape.record_shape("one block only, lowercase.")
    assert shape.recent_shapes()[:2] == ["1B/LOWER", "2B/NAME"]


def test_the_sampler_never_asks_for_a_draft_the_gates_reject():
    """A PRONOUN opener with one sentence would be "It's an undo button." —
    the exact shape `dangles_its_subject` rejects. The instruction must keep
    the name inside that sentence instead of promising a next one."""
    single = shape.shape_directive(
        {"blocks": 1, "sentences": 1, "opener": "PRONOUN", "closer": "STATEMENT"})
    assert "that same sentence" in single
    assert "then name it" not in single
    pair = shape.shape_directive(
        {"blocks": 2, "sentences": 3, "opener": "PRONOUN", "closer": "PUNCH"})
    assert "then name it" in pair


def test_the_shaper_stops_crushing_shapes_the_account_uses():
    """The mechanism-level cause, reproduced and fixed. A four-block draft
    went into `_reshape_to_style` and came out two blocks — every varied
    draft was flattened AFTER the gates passed it. Shapes the account
    publishes are now left alone; the model's over-segmentation is not."""
    from app.intelligence import _reshape_to_style
    style = {"multiline_rate": 0.9, "blank_line_rate": 0.9,
             "median_blocks": 2, "median_lines": 3}
    his_four = ("goose is free.\n\nClaude Code costs 200 a month.\n\n"
                "Same drafts, same edits.\n\nThat is the whole story.")
    known = {1, 2, 3, 4, 5}
    # Old behaviour, still there for callers that pass nothing (the replies).
    assert shape.block_count(_reshape_to_style(his_four, style)) == 2
    # Guarded: his own silhouette survives byte-identical.
    assert _reshape_to_style(his_four, style, known_block_counts=known) == his_four
    # A layout the account never uses is still normalised — this is the 9B's
    # one-sentence-per-line habit, and the guard must not protect it.
    foreign = "\n\n".join(f"Sentence {n} here." for n in range(7))
    assert shape.block_count(
        _reshape_to_style(foreign, style, known_block_counts=known)) == 2


def test_layout_is_enforced_because_the_model_ignores_it():
    """Measured 2026-07-29 over 32 drafts carrying a sampled shape: opener
    96.9% obeyed, closer 96.9%, block count 43.8% — and all thirteen requests
    for 3+ blocks came back as one or two. Rhetoric stays a request; layout
    becomes a mechanism, using the grouping that used to impose the median."""
    from app.intelligence import _reshape_to_style
    style = {"multiline_rate": 0.9, "blank_line_rate": 0.9,
             "median_blocks": 2, "median_lines": 3}
    four_thoughts = ("Kimi K3 is on Telnyx now. It costs 60 cents per million "
                     "tokens. The window is 256k. That undercuts everyone.")
    for target in (1, 2, 3, 4):
        out = _reshape_to_style(four_thoughts, style, target_blocks=target)
        assert shape.block_count(out) == target, f"target {target} not applied"
    # Never splits a sentence to reach a target: four blocks from two
    # sentences is two blocks, not two half-sentences.
    two = "Goose is free. Claude Code is 200 a month."
    assert shape.block_count(_reshape_to_style(two, style, target_blocks=4)) == 2
    # And an over-large target caps at the sentence supply.
    assert shape.block_count(
        _reshape_to_style(four_thoughts, style, target_blocks=9)) == 4


def test_the_asked_layout_travels_in_the_direction():
    directive = shape.shape_directive(
        {"blocks": 3, "sentences": 3, "opener": "NAME", "closer": "PUNCH"})
    assert shape.asked_blocks(directive) == 3
    single = shape.shape_directive(
        {"blocks": 1, "sentences": 1, "opener": "NAME", "closer": "STATEMENT"})
    assert shape.asked_blocks(single) == 1
    assert shape.asked_blocks("no shape block here") is None


@pytest.mark.parametrize("archive", [
    ["short one here."] * 4
    + ["Longer post with two blocks.\n\nAnd a second block that runs on a bit more."] * 4
], indirect=True)
def test_length_is_conditioned_on_shape(archive):
    """A global median beside a one-sentence shape produced a 38-word run-on
    that failed the register gate. Word budget is measured per block count."""
    by_blocks = shape.account_words_by_blocks()
    assert by_blocks[1] < by_blocks[2], f"length not shape-conditioned: {by_blocks}"
    text = shape.shape_directive(
        {"blocks": 1, "sentences": 1, "opener": "NAME", "closer": "STATEMENT"})
    assert f"about {by_blocks[1]} words" in text
    assert "no sentence past 20 words" in text


# RETIRED (API-native pass, 2026-09-22): test_the_sampled_shape_speaks_alone_in_the_prompt: no sampled shape reaches a draft any more: the length and shape are the writer's voice.


def test_the_shape_block_never_overflows_the_schema_cap():
    """THE BUG THAT WOULD HAVE KILLED POST DRAFTING (caught 2026-07-29 by
    three independent audit lenses, hours before deploy). `DraftRequest.
    direction` is max_length=1200 and `_with_evidence` fills it to 1192 BY
    CONSTRUCTION whenever grounding returns evidence. Appending a ~300-char
    shape block after that raised a ValidationError on every grounded
    from-scratch post — surfaced to the user as "Starling could not finish
    that local request". The arena never saw it because the arena composes a
    short direction of its own.

    The shape's length is now reserved out of the evidence budget, so both
    survive: the direction fits AND still carries the shape instruction.
    """
    from app.main import _with_evidence
    from app.schemas import DraftRequest, PostContext

    shaped = shape.shape_directive(
        {"blocks": 3, "sentences": 4, "opener": "LOWER", "closer": "STATEMENT"})
    assert shaped, "the fixture needs a real shape block"
    reserve = len(shaped) + 2

    base = ("Write one original standalone post for this account today. " * 8)[:700]
    evidence = "Telnyx runs Kimi K3 on owned GPU infrastructure. " * 20  # ~960 chars
    direction = _with_evidence(base, evidence, reserve=reserve)
    direction = direction[:1200 - reserve]
    direction = f"{direction}\n\n{shaped}"

    assert len(direction) <= 1200, f"direction overflows at {len(direction)}"
    assert "SHAPE FOR THIS POST" in direction, "the shape block was truncated away"
    assert "Telnyx" in direction, "the evidence was starved to fit the shape"
    # The thing that actually broke: constructing the request must not raise.
    context = PostContext(post_id="cap", author="X", author_handle="@x",
                          text="(from scratch)", visibility="public")
    DraftRequest(action="post", context=context, research_mode="off",
                 media_policy="no_media", grok_permission=False,
                 length="short", direction=direction)


def test_the_shaper_lays_out_a_one_paragraph_draft():
    """The guard read `current in known_block_counts`, and 1 is the account's
    MOST common block count — so every single-paragraph draft matched and was
    returned unshaped, disabling layout on the enhance path, the decide path
    and any post without a sampled shape. Only a draft that already has
    multiple blocks can be wearing a silhouette worth protecting."""
    from app.intelligence import _reshape_to_style
    style = {"multiline_rate": 0.9, "blank_line_rate": 0.9,
             "median_blocks": 2, "median_lines": 3}
    wall = ("Kimi K3 lands on Telnyx today. It costs 60 cents per million "
            "tokens. That undercuts everyone.")
    out = _reshape_to_style(wall, style, known_block_counts={1, 2, 3, 4, 5})
    assert shape.block_count(out) == 2, "a one-paragraph draft must still be laid out"
    # ...while a genuine multi-block silhouette of his is still protected.
    his = "goose is free.\n\nClaude Code is 200 a month.\n\nSame drafts."
    assert _reshape_to_style(his, style, known_block_counts={1, 2, 3, 4, 5}) == his


def test_an_over_long_draft_keeps_its_blocks():
    """The 280-char fit deleted every blank line, so a four-block draft one
    character too long shipped as one wall — and the shape recorded for the
    anti-repeat memory was a shape the reader never saw."""
    from app.intelligence import _reshape_to_style
    style = {"multiline_rate": 0.9, "blank_line_rate": 0.9,
             "median_blocks": 2, "median_lines": 3}
    long_draft = " ".join(
        f"Sentence number {n} carries a fairly long thought about the model."
        for n in range(8))
    out = _reshape_to_style(long_draft, style, target_blocks=4)
    assert len(out) <= 280
    assert "\n\n" in out, "the blank lines were stripped instead of trimming words"
    assert shape.block_count(out) >= 2


def test_a_one_sentence_question_shape_is_never_requested():
    """`asks_without_saying` rejects a post whose only sentence is a question,
    so the sampler must not ask for one — a gate and a sampler disagreeing is
    the prompt arguing with itself one layer down."""
    import random as _random

    class _FixedPicker(_random.Random):
        def uniform(self, a, b):          # always take the first weighted item
            return 0.0

    shape.reset_shape_cache()
    with storage.connect() as conn:
        conn.execute("insert into posts (post_id, text) values (?, ?)",
                     ("q-only", "why does anyone still pay for this?"))
        for index in range(8):
            conn.execute("insert into posts (post_id, text) values (?, ?)",
                         (f"q-pad-{index}", "why does anyone still pay for this?"))
        conn.commit()
    shape.reset_shape_cache()
    target = shape.sample_shape(rng=_FixedPicker())
    shape.reset_shape_cache()
    assert target is not None
    assert not (target["closer"] == "QUESTION" and target["sentences"] <= 1)
