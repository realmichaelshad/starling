"""One assembly for the post direction.

A shape block appended past the schema cap, or a shape instruction alone in a
short direction, are different requests from the same instruction at the end of
1200 characters of evidence. So the assembly lives in `app/direction.py` and the
composer goes through it.
"""
import pathlib
import re

from app.direction import (DIRECTION_CAP, EVIDENCE_LEAD, build_post_direction,
                           with_evidence)

_MAIN = pathlib.Path(__file__).resolve().parents[1] / "app" / "main.py"


def test_the_composer_has_no_second_copy_of_the_assembly():
    source = _MAIN.read_text(encoding="utf-8")
    # The one remaining literal cap is the belt-and-braces clamp; the budgeting
    # arithmetic must not be duplicated here.
    assert "_EVIDENCE_LEAD not in" not in source, (
        "the shape-yields-to-facts rule has been re-inlined in main.py")
    assert source.count("1200 - ") == 0, (
        "cap arithmetic is back in main.py — it belongs in direction.py")
    assert "append_before_shape" in source, (
        "the image note is truncating the direction on its own again, which "
        "is how an attached image used to silently cut the shape block off")


def test_the_result_is_always_inside_the_schema_cap():
    """Any shape, any evidence length, still under the cap."""
    base = "Write one original standalone post. " * 8
    for evidence_len in (0, 40, 79, 80, 400, 4000):
        for shape_len in (0, 120, 300, 600):
            built, _ = build_post_direction(
                base, "x" * evidence_len if evidence_len else None,
                shape_text="S" * shape_len)
            assert len(built) <= DIRECTION_CAP, (
                f"evidence={evidence_len} shape={shape_len} -> {len(built)}")


def test_the_shape_yields_rather_than_starving_the_facts():
    """A shaped post about nothing is worse than an unshaped post about something."""
    base = "Write one post. " * 44          # room for facts, not for both
    facts = "Claude Code ships skills as markdown files. " * 3
    built, kept = build_post_direction(base, facts, shape_text="S" * 300)
    assert EVIDENCE_LEAD in built, "the facts were starved by the reservation"
    assert kept == "", "the shape claimed to survive while the facts were cut"


def test_the_shape_survives_when_yielding_would_buy_nothing():
    """Dropping the shape is only worth it if it actually recovers the facts.

    The first version yielded whenever the reserved build came back with no
    evidence — including when the direction was so long that the evidence did
    not fit either way. That paid the shape and got nothing for it.
    """
    base = "Write one post. " * 62          # no room for facts at any reserve
    facts = "Claude Code ships skills as markdown files. " * 3
    built, kept = build_post_direction(base, facts, shape_text="S" * 300)
    assert EVIDENCE_LEAD not in built, "the fixture no longer starves the facts"
    assert kept, "the shape was sacrificed and the facts still did not fit"
    assert built.endswith("S" * 300)


def test_the_shape_rides_along_when_there_is_room():
    built, kept = build_post_direction(
        "Write one post.", "Claude Code ships skills as markdown files.",
        shape_text="SHAPE: two blocks, lowercase opener.")
    assert EVIDENCE_LEAD in built and kept
    assert built.rstrip().endswith("lowercase opener."), built


def test_a_reply_gets_the_reply_instruction_not_the_post_mandate():
    facts = "Qwen Code can file issues on its own now, per the release notes."
    reply, _ = build_post_direction("Answer them.", facts, action="reply")
    post, _ = build_post_direction("Write one post.", facts)
    assert "only where they genuinely fit" in reply
    assert "Mention at least one specific thing" in post
    assert "Mention at least one specific thing" not in reply


def test_short_evidence_is_kept_and_cut_evidence_is_dropped():
    """A headline is thin evidence, but it is evidence."""
    headline = "Anthropic ships Cowork."                    # 24 chars, whole
    assert EVIDENCE_LEAD in with_evidence("Write one post.", headline)
    # Truncated to a fragment under the floor: dropped rather than shipped.
    base = "W" * (DIRECTION_CAP - 380)
    built = with_evidence(base, "y" * 900)
    fragment = re.search(re.escape(EVIDENCE_LEAD) + r"(y*)", built)
    assert fragment is None or len(fragment.group(1)) >= 80, built[-120:]
