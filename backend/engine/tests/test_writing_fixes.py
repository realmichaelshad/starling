"""The writing fixes the user demanded: no em-dashes or semicolons (the AI
tells), and never a fabricated statistic in a created post."""
from __future__ import annotations

from app.intelligence import (_match_account_style, _reshape_to_style, _strip_emdashes,
                              _strip_semicolons, _unsupported_figures)
from app.account_model import style_directive


# A roomy writer: blank lines between ~3 blocks. This is the shape drafts must
# match — NOT one blank-separated line per sentence (the over-spacing bug the
# user reported: "the spacing is weird... way too much").
_ROOMY = {"multiline_rate": 0.9, "blank_line_rate": 0.9, "median_blocks": 3}
# A line-break writer: single newlines, no blank spacers.
_LINES = {"multiline_rate": 0.9, "blank_line_rate": 0.0, "median_lines": 2}
_MULTILINE = _ROOMY  # back-compat for other tests in this file


def test_reshape_groups_to_the_writers_block_count_not_one_per_sentence():
    """The over-spacing fix: five short thoughts must NOT become five blank-
    separated lines — they group into the writer's measured ~3 blocks."""
    draft = ("opus 5 seems delayed. look at polymarket. maybe they're waiting. "
             "i'm not sure what's holding them back. but the wait is getting long.")
    out = _reshape_to_style(draft, _ROOMY)
    assert out.count("\n\n") == 2, "3 blocks, 2 blank-line gaps — not one per sentence"
    assert out.split() == draft.split(), "same words, same order"


def test_reshape_uses_single_newlines_for_a_line_break_writer():
    draft = "First quick point. Second quick point."
    out = _reshape_to_style(draft, _LINES)
    assert out == "First quick point.\nSecond quick point."


def test_reshape_leaves_a_single_thought_alone():
    assert _reshape_to_style("grok trying to save its company lol", _ROOMY) == \
        "grok trying to save its company lol"


def test_reshape_skips_accounts_that_post_in_blocks():
    draft = "First thought here. Second thought here."
    assert _reshape_to_style(draft, {"multiline_rate": 0.1}) == \
        "First thought here. Second thought here."


def test_reshape_never_exceeds_the_limit():
    draft = ". ".join(["A fairly wordy sentence that runs on for a while here"] * 6) + "."
    out = _reshape_to_style(draft, _MULTILINE)
    assert len(out) <= 280 or out == draft


# ---- em-dash / semicolon stripping ---------------------------------------

def test_emdash_becomes_two_sentences_or_a_comma():
    assert _strip_emdashes("a tech upgrade—it's about control") == "a tech upgrade. It's about control"
    # a conjunction after the dash reads as a comma, not a new sentence
    assert _strip_emdashes("huge—but let's be real") == "huge, but let's be real"


def test_semicolon_becomes_a_full_stop():
    assert _strip_semicolons("real, though; this isn't new") == "real, though. This isn't new"


def test_match_account_style_strips_both_when_rate_is_low():
    brief = {"account_model": {"voice_profile": {"style": {
        "emdash_rate": 0.05, "semicolon_rate": 0.0, "hashtag_rate": 0.05, "emoji_rate": 0.0}}}}
    out = _match_account_style("It's not just X—it's Y; and more.", brief)
    assert "—" not in out and ";" not in out


def test_match_account_style_keeps_emdash_for_a_writer_who_uses_them():
    brief = {"account_model": {"voice_profile": {"style": {
        "emdash_rate": 0.4, "semicolon_rate": 0.0, "hashtag_rate": 0.05, "emoji_rate": 0.0}}}}
    out = _match_account_style("a real dash—kept", brief)
    assert "—" in out


def test_style_directive_names_the_avoid_rules():
    model = {"voice_profile": {"style": {
        "sample": 50, "median_words": 28, "words_p25": 18, "words_p75": 39,
        "median_lines": 3, "multiline_rate": 0.6,
        "emdash_rate": 0.05, "semicolon_rate": 0.0, "colon_rate": 0.05,
        "emoji_rate": 0.1, "hashtag_rate": 0.05}}}
    directive = style_directive(model).lower()
    assert "do not use em-dash" in directive
    assert "never use semicolons" in directive


# ---- fabricated-figure guard ---------------------------------------------

def test_unsupported_figures_flags_invented_stats():
    assert _unsupported_figures("bolster security by 40%", "anthropic fable") == ["40%"]
    assert _unsupported_figures("it's 2x faster", "no numbers") == ["2x"]
    assert _unsupported_figures("costs $500", "pricing unknown") == ["$500"]


def test_unsupported_figures_allows_evidence_backed_and_version_numbers():
    assert _unsupported_figures("up 40% this year", "the report says 40% growth") == []
    # version numbers are names, not claims
    assert _unsupported_figures("Sonnet 5 matches Opus 4.8, Fable 5 too", "") == []
