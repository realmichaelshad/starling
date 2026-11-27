"""The scrubber deletes what the register gates detect — including across a
sentence boundary.

"X isn't just about A. It's about B." survived `strip_formulas` untouched and
shipped (flagged, still shipped) three times on 2026-07-27, because every
killer pattern in the scrubber is sentence-anchored and the live construction
arrives split across two sentences: "…but it's not just about speed." / "It's
about who gets to control the flow." Neither half alone matches.

The fix locates the construction on the WHOLE text — where `_NOT_JUST_RE` and
`_ABOUT_TEMPLATE_RE` already sit at 0/35 against his archive — and drops every
sentence the match overlaps. The founding rule of the module: delete the stock
constructions instead of asking the model not to write them.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.intelligence import strip_formulas               # noqa: E402


def test_the_cross_sentence_template_is_deleted():
    text = ("Claude shipped Agent Teams. Opus 4.6 just rolled out.\n\n"
            "The new feature lets agents work together, but it's not just "
            "about speed. It's about who gets to control the flow.")
    out = strip_formulas(text)
    assert "not just about" not in out
    assert "who gets to control" not in out
    assert "Claude shipped Agent Teams" in out
    assert "Opus 4.6 just rolled out" in out


def test_the_template_is_deleted_even_across_a_block_boundary():
    """The shaper's fourth strike: it split the construction across a BLANK
    LINE, and a character class that excludes newlines cannot follow it there.
    Shipped once: "not just writing code anymore." / "They're debugging it,
    live." in separate blocks."""
    text = ("Snapshield's undo button means agents can fix mistakes without "
            "breaking the repo. They're not just writing code anymore.\n\n"
            "They're debugging it, live.")
    out = strip_formulas(text)
    assert "not just writing code" not in out
    assert "debugging it, live" not in out
    assert "Snapshield's undo button" in out


def test_the_comma_form_is_still_handled():
    text = ("Mistral OCR4 posts a 13-point gap. The race isn't about speed, "
            "it's about fixing the flaws that make models fail. DharmaOCR "
            "still leads the table.")
    out = strip_formulas(text)
    assert "isn't about speed" not in out
    assert "Mistral OCR4" in out and "DharmaOCR" in out


def test_not_just_anyone_still_passes():
    """The guard case from the original calibration: ordinary usage of the
    words is not the construction."""
    text = ("Opus 5 handles a 400k repo without choking. Not just anyone can "
            "ship that. The price cut makes it the default choice.")
    assert strip_formulas(text) == text


def test_his_bare_not_just_closer_is_never_deleted():
    """A real archive post. "isn't just safety. It's insurance." is his voice
    — short noun payoff, no "about", no gerund, no "anymore" — and the
    cross-sentence scan deleted it on 2026-07-27 until the reply lane's
    byte-identical archive test caught it. Both floors, byte-identical."""
    text = ("Honestly Dario is right, open models need testing.\n\n"
            "The problem is who runs it.\n\n"
            "That filter isn't just safety. It's insurance.")
    assert strip_formulas(text) == text
    assert strip_formulas(text, min_survivors=1) == text


def test_a_self_answered_question_is_deleted():
    """"ARC-AGI 3 result nobody saw coming? / Yeah, that's the one." shipped
    2026-07-27 — a question answered by its own author is a conversation with
    nobody. Calibrated: 0/35 fixture and 0/36 archive posts self-answer. Both
    halves die; the real sentences around them survive."""
    text = ("Opus 5 beat Mythos 5 on cybersecurity. ARC-AGI 3 result nobody "
            "saw coming?\n\nYeah, that's the one. GPT-5.6 Sol's not even "
            "close to matching it.")
    out = strip_formulas(text)
    assert "nobody saw coming" not in out
    assert "Yeah" not in out
    assert "Opus 5 beat Mythos 5" in out
    assert "not even close to matching it" in out
    # A closing question with no self-answer is not the shape.
    closer = ("The benchmark gap is 13 points. If the model is ready, why "
              "isn't the code?")
    assert strip_formulas(closer) == closer


def test_a_stub_broken_tail_is_cut_not_shipped():
    """"That tree prints straight to stdout by a lo" shipped twice — flagged
    as cut_off, shipped anyway, because a note does not un-break a sentence."""
    from app.intelligence import repair_stub_tail
    text = ("Graph Skill lets agents run tasks as dependency graphs.\n\n"
            "That tree prints straight to stdout by a lo")
    assert repair_stub_tail(text) == (
        "Graph Skill lets agents run tasks as dependency graphs.")
    # Real short words survive; the only sentence is never cut.
    assert repair_stub_tail("Ship it to the US.") == "Ship it to the US."
    assert repair_stub_tail("LLM-spend is a l") == "LLM-spend is a l"


def test_stub_repair_never_rewrites_his_posts():
    from app.intelligence import repair_stub_tail
    fixture = Path(__file__).with_name("fixtures_his_posts.json")
    if fixture.exists():
        posts = [p for p in json.loads(fixture.read_text(encoding="utf-8")) if p.strip()]
        changed = [p for p in posts if repair_stub_tail(p) != p]
        assert not changed, f"rewrote his post: {changed[0][:90]!r}"


def test_date_furniture_is_stripped_from_summaries():
    """"Jul 13, 2026 · AI agents are becoming more capable." went out INSIDE a
    post — blog-index chrome copied as if it were a fact."""
    from app.news import _DATE_FURNITURE_RE
    raw = ("Jun 29, 2026 · Undo makes AI coding agents effective. "
           "Jun 23, 2026 · Let your agents run with permissions skipped.")
    out = _DATE_FURNITURE_RE.sub("", raw)
    assert "Jun 29" not in out and "Jun 23" not in out
    assert "Undo makes AI coding agents effective." in out
    # A date inside a sentence is content, not furniture.
    kept = "The beta opened on Jun 29, 2026 and sold out."
    assert _DATE_FURNITURE_RE.sub("", kept) == kept


def test_embed_chrome_is_stripped_from_summaries():
    """A video widget row rode a summary into a post: "The 5-minute AI video
    got 358k views." The page's chrome is not evidence."""
    from app.news import _strip_furniture
    raw = ("Supports Python, JavaScript, and 30+ languages. Copy link. "
           "Hoc Claude Code tu con so 0 in 2 gio. 358k views. "
           "Snapshot your repo before it runs.")
    out = _strip_furniture(raw)
    assert "Copy link" not in out and "views" not in out
    assert "Supports Python, JavaScript, and 30+ languages." in out
    assert "Snapshot your repo before it runs." in out


def test_the_scrubber_bar_on_his_archive_still_holds():
    fixture = Path(__file__).with_name("fixtures_his_posts.json")
    if not fixture.exists():
        return
    posts = [p for p in json.loads(fixture.read_text(encoding="utf-8")) if p.strip()]
    changed = [p for p in posts if strip_formulas(p) != p]
    assert len(changed) <= 2, (
        f"strip_formulas rewrote {len(changed)}/{len(posts)} of his posts, "
        f"e.g. {changed[0][:90]!r}")
