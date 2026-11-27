"""The two arena defects of 2026-07-30, and why the product already saw them.

Both were `names_nothing`, and the gate fired on both. It was moved out of the
hard-reject path on purpose — a vague draft must ship with its fault named
rather than as silence — and then never added to the revisable set, so it
flagged and shipped with no second attempt. The repair path is covered in
`test_every_flag_has_a_repair.py`; what is locked here is the detection itself,
plus the template escape that produced the second defect.
"""
from app.intelligence import _ABOUT_TEMPLATE_RE, names_nothing, strip_formulas


def test_the_two_arena_defects_are_seen():
    collaboration = ("ai coding agents replace the need to talk rather than "
                     "replacing collaboration.\n\nno team review found in "
                     "most of those pull requests.")
    assert names_nothing(collaboration), (
        "the evidence said 25,264 pull requests across 2,361 repositories "
        "and the draft kept \"most of those\"")

    skills = ("it's not about plugins making agents smarter, it's just a "
              "registry for users to write their own skills in markdown files.")
    assert names_nothing(skills), "Claude Code was in the topic's first two words"
    # It is the banned template too — and the reveal said "it's just", not
    # "it's about", which is the one word that walked it past the old pattern.
    assert _ABOUT_TEMPLATE_RE.search(skills), "the widened template missed its own escape"


def test_a_draft_that_names_its_subject_passes():
    assert not names_nothing("Claude Code ships skills as markdown files.")
    assert not names_nothing("no team review on 25,264 agent pull requests.")


def test_the_widened_template_is_scrubbed_when_there_is_a_draft_left():
    """The scrubber will not empty a short draft — that is the gate's job.

    `strip_formulas` keeps at least two sentences, so the one-sentence arena
    defect was correctly left alone and `ai_tell` is what catches it. Give the
    same construction a third sentence and the scrubber does remove it.
    """
    draft = ("It's not about plugins making agents smarter, it's just a "
             "registry. Claude Code reads the markdown at runtime. Any user "
             "can write one.")
    cleaned = strip_formulas(draft)
    assert "not about" not in cleaned.lower(), cleaned
    assert "Claude Code" in cleaned, "the scrubber took the real sentence too"


def test_an_ordinary_not_about_sentence_survives():
    """Only the formula is banned, not the words."""
    assert not _ABOUT_TEMPLATE_RE.search(
        "This release is not about speed and the benchmarks show why.")
    assert not _ABOUT_TEMPLATE_RE.search(
        "The complaint is not about pricing, it just reads that way here.")
