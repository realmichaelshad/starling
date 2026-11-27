"""The register phase (2026-07-28 evening): his measured vocabulary and
punctuation, enforced. Facts from all 39 archived texts: em-dash in 3/39
(one each, never two); sentence words median 10, max 30; 3% of words run
10+ characters and the long ones are everyday."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.intelligence import _off_register, scrub_extra_dashes    # noqa: E402


def test_two_dashes_are_never_his_and_scrub():
    text = ("Opus 5 runs fast — faster than 4.8 — and the price held.")
    out = scrub_extra_dashes(text)
    assert out.count("—") <= 1
    assert "price held" in out


def test_ascii_double_hyphens_count_as_dashes():
    """The first audit found " -- " untouched — models write ASCII dashes."""
    text = "Opus is fast -- faster than 4.8 -- and cheap."
    out = scrub_extra_dashes(text)
    assert out.count("--") <= 1
    from app.intelligence import _off_register
    assert _off_register("Opus is fast -- and cheap.")


def test_number_ranges_are_data_not_mannerism():
    """"40–60" is a range; it neither counts toward the dash limit nor gets
    rewritten, and hyphenated words are never touched."""
    text = "It scores 40–60 on the bench — depending on context — always."
    out = scrub_extra_dashes(text)
    assert "40–60" in out
    assert out.count("—") <= 1
    untouched = "The open-source fine-tuned model is state-of-the-art."
    assert scrub_extra_dashes(untouched) == untouched


def test_one_dash_survives_the_scrub_but_flags_for_revision():
    text = "Opus 5 runs fast — and the price held."
    assert scrub_extra_dashes(text) == text
    assert _off_register(text)


def test_a_run_on_sentence_flags():
    long_sentence = ("The new agent framework promises to change how every "
                     "single developer on the planet writes and reviews and "
                     "ships and maintains their production code every day "
                     "forever without any human in the loop at all.")
    assert _off_register(long_sentence)


def test_formal_vocab_outside_his_words_and_evidence_flags():
    assert _off_register(
        "The infrastructure modernization demonstrates organizational "
        "transformation.", "")
    # Evidence words are quotable, whatever their length.
    assert not _off_register(
        "Telnyx runs it on owned infrastructure.",
        "Telnyx runs the model on owned GPU infrastructure in Texas.")


def test_his_plain_register_passes():
    assert not _off_register(
        "Goose is free. You're paying 4x for the same drafts. That's the "
        "whole story.")


# --------------------------- the account's rule, 2026-07-30: no em dashes
# The account never uses em dashes, and a generated post carried one. The
# GENERATION path now strips every clause dash (keep_first=False); the
# default keeps tolerating one so the account's own archived dash posts stay
# untouched wherever the default runs, and enhancement mode never reaches the
# scrub at all.

def test_generation_mode_strips_every_clause_dash():
    from app.intelligence import scrub_extra_dashes
    text = "GPT-5.6 Fast mode is cheaper — but slower than Sol or Terra."
    out = scrub_extra_dashes(text, keep_first=False)
    assert "—" not in out
    assert "cheaper. But slower" in out
    # Ranges and hyphenated words survive the strict mode too.
    kept = "It scores 40–60 on the open-source bench."
    assert scrub_extra_dashes(kept, keep_first=False) == kept


def test_tidy_whitespace_flattens_the_live_batch_artifact():
    """Straight from the 2026-07-30 batch: doubled blank lines between
    blocks and a trailing space at a line end reached the composer."""
    from app.intelligence import tidy_whitespace
    ragged = ("Together AI now serves Kimi models. \n\n"
              "This means you get the weights natively.\n\n\n\n"
              "Just run the open model directly.")
    tidy = tidy_whitespace(ragged)
    assert "\n\n\n" not in tidy
    assert " \n" not in tidy
    assert tidy.splitlines()[0].endswith("models.")
    # Same words, same order — only spacing changes, so the gates and the
    # character limit judge identical content.
    assert tidy.split() == ragged.split()


def test_a_support_check_cut_never_ships_doubled_blank_lines(monkeypatch):
    """THE ROOT CAUSE OF THE 2026-07-31 SPACING BUG, pinned end to end. The
    support check runs AFTER every scrub and edits the shipping text;
    cutting a sentence out of a multi-block draft left "\\n\\n\\n\\n" at the
    splice — three of six posts in the live batch. Whatever the check does
    to the text, the reader never sees scaffolding."""
    from app import intelligence
    from app.schemas import DraftCandidate, DraftRequest, PostContext

    spliced = ("Together AI now serves Kimi models natively. \n\n"
               "Moonshot joins the partner list today.\n\n\n\n"
               "The old gap between them closes fast.")
    monkeypatch.setattr(intelligence, "_support_evidence",
                        lambda request: "Together AI serves Kimi models.")
    monkeypatch.setattr(intelligence, "repair_name_fragments",
                        lambda text, evidence: text)
    import app.support_check as support_check
    monkeypatch.setattr(support_check, "apply_support_check",
                        lambda text, evidence, llm: (spliced, None))

    candidate = DraftCandidate(id="c-1", text="placeholder original",
                               performance={}, rationale=[],
                               retrieved_examples=[], sources=[],
                               growth_score=70.0, score_band="workable")
    request = DraftRequest(action="post", context=PostContext(
        author="a", text="", visibility="public"))
    out = intelligence._support_checked([candidate], request, llm=None)
    assert out and "\n\n\n" not in out[0].text
    assert " \n" not in out[0].text
    assert out[0].text.split() == spliced.split()


def test_a_support_cut_below_the_planned_size_carries_the_note(monkeypatch):
    """The morning bug, pinned end to end: a full-length draft passed every
    gate, the support check amputated it to 7 words against a 47-word plan,
    and it shipped needs_review=False with a rationale describing the
    pre-cut draft. Whatever the check cuts, an under-plan result says so."""
    from app import intelligence
    from app.schemas import DraftCandidate, DraftRequest, PostContext

    monkeypatch.setattr(intelligence, "_support_evidence",
                        lambda request: "Together AI serves Kimi models.")
    monkeypatch.setattr(intelligence, "repair_name_fragments",
                        lambda text, evidence: text)
    import app.support_check as support_check
    monkeypatch.setattr(
        support_check, "apply_support_check",
        lambda text, evidence, llm: ("Together AI now hosts Kimi models natively.", None))

    candidate = DraftCandidate(
        id="c-cut", text=("Together AI now hosts Kimi models natively. The "
                          "partnership gives builders first-party access and "
                          "the pricing undercuts the usual per-token rates. "
                          "That is the part worth watching."),
        performance={}, rationale=["original rationale"],
        retrieved_examples=[], sources=[], growth_score=75.0,
        score_band="workable")
    request = DraftRequest(action="post", context=PostContext(
        author="a", text="", visibility="public"))
    request.direction = ("SHAPE FOR THIS POST: Write it as 4 blocks separated "
                         "by blank lines, 4 sentences total, about 47 words.")
    out = intelligence._support_checked([candidate], request, llm=None)
    assert out and out[0].text == "Together AI now hosts Kimi models natively."
    assert out[0].needs_review is True
    assert "under the size" in str(out[0].review_note or "")
    # An existing support note is never overwritten by the size note.
    monkeypatch.setattr(
        support_check, "apply_support_check",
        lambda text, evidence, llm: ("Together AI now hosts Kimi models natively.",
                                        "I can't back the pricing claim."))
    out = intelligence._support_checked([candidate], request, llm=None)
    assert "can't back" in str(out[0].review_note or "")
