"""Search-fallback evidence must be about the topic's subject.

The live defect (2026-07-27): the Snapshield post's evidence contained
"Open any style for colors, typography… DESIGN.md" and "Discover Zencoder's
AI Coding Agent Platform" — another product's pitch, delivered by the search
fallback in `facts_for`, which took snippets from all five hits regardless of
what each hit was about. Reproduced the same day: the same query returned
Undo (the time-travel-debugging company) and its text became Snapshield
"facts". The draft then blended two products into one post.

The gate: a hit contributes snippets only when it shares a `subject.subjects`
key with the title. No subject in the title → single-provenance fallback
(first contributing hit only).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import source_facts                                    # noqa: E402

# The hit set from the live reproduction, in its real shapes (the repository owner is renamed).
_SNAPSHIELD_HITS = [
    {"title": "GitHub - sample-dev-code/Snapshield: A safety net for AI coding ...",
     "url": "https://github.com/sample-dev-code/Snapshield",
     "snippet": ("About A safety net for AI coding agents — Snapshield snapshots "
                 "your repo before it runs, undo instantly if it breaks something.")},
    {"title": "AI Agents Need More Than Permission. They Need an Undo Button",
     "url": "https://www.linkedin.com/pulse/ai-agents-need-more",
     "snippet": ("AI agents are becoming more capable. They can inspect files, "
                 "write code, run commands and complete tasks that once required "
                 "constant human input.")},
    {"title": "Undo Brings Root Cause Analysis to AI Coding Agents",
     "url": "https://www.i40today.com/undo-brings-root-cause/",
     "snippet": ("Undo makes AI coding agents effective on the software that "
                 "matters most. Undo lets agents automatically pinpoint failures.")},
]


def test_hits_about_other_products_are_dropped(monkeypatch):
    # `conftest.py:29` sets STARLING_RESEARCH=off for every test so nothing
    # can reach the network. This test stubs the network itself and exercises
    # the FETCH path, so it opts in exactly as production does: `facts_for`
    # now honours the switch and returns {"facts": [], "source": "none"}
    # without it. The opt-in is not a workaround -- it documents that the
    # behaviour under test requires the user's consent.
    monkeypatch.setenv("STARLING_RESEARCH", "on")
    monkeypatch.setattr(source_facts, "fetch_page_text", lambda url: None)
    monkeypatch.setattr(source_facts, "search_web", lambda q, n: _SNAPSHIELD_HITS)
    found = source_facts.facts_for(
        "Snapshield – an undo button for AI coding agents",
        "https://github.com/sample-dev/snapshield")
    joined = " ".join(found["facts"])
    assert "Undo makes AI coding agents effective" not in joined
    assert "becoming more capable" not in joined
    assert "Snapshield" in joined            # its own hit still contributes
    assert found["url"] == "https://github.com/sample-dev-code/Snapshield"


def test_alias_names_still_match(monkeypatch):
    """"Claude Code" in the title accepts a hit that says "Anthropic" — both
    collapse to the same subject key, so the gate must not starve them."""
    # `conftest.py:29` sets STARLING_RESEARCH=off for every test so nothing
    # can reach the network. This test stubs the network itself and exercises
    # the FETCH path, so it opts in exactly as production does: `facts_for`
    # now honours the switch and returns {"facts": [], "source": "none"}
    # without it. The opt-in is not a workaround -- it documents that the
    # behaviour under test requires the user's consent.
    monkeypatch.setenv("STARLING_RESEARCH", "on")
    hits = [{"title": "Anthropic ships a faster tab model",
             "url": "https://example.com/a",
             "snippet": ("Anthropic launched the update with a 40ms budget and "
                         "cut latency by half across the CLI surfaces.")}]
    monkeypatch.setattr(source_facts, "fetch_page_text", lambda url: None)
    monkeypatch.setattr(source_facts, "search_web", lambda q, n: hits)
    found = source_facts.facts_for("Claude Code adds warmup", None)
    assert found["facts"], "alias-equal hit was wrongly dropped"


def test_no_subject_title_takes_one_provenance(monkeypatch):
    """A title that names nothing checkable can't be relevance-tested, so it
    takes the first contributing hit ONLY — one provenance, not five mixed."""
    # `conftest.py:29` sets STARLING_RESEARCH=off for every test so nothing
    # can reach the network. This test stubs the network itself and exercises
    # the FETCH path, so it opts in exactly as production does: `facts_for`
    # now honours the switch and returns {"facts": [], "source": "none"}
    # without it. The opt-in is not a workaround -- it documents that the
    # behaviour under test requires the user's consent.
    monkeypatch.setenv("STARLING_RESEARCH", "on")
    hits = [{"title": "The land grab, explained",
             "url": "https://example.com/one",
             "snippet": ("The shift moved 40 percent of new tooling budgets "
                         "into agent platforms and dropped seat prices.")},
            {"title": "An unrelated vendor pitch",
             "url": "https://example.com/two",
             "snippet": ("VendorX supports 30+ languages and ships 2,000+ "
                         "design systems for launched enterprise brands.")}]
    monkeypatch.setattr(source_facts, "fetch_page_text", lambda url: None)
    monkeypatch.setattr(source_facts, "search_web", lambda q, n: hits)
    found = source_facts.facts_for("the new land grab in tooling", None)
    joined = " ".join(found["facts"])
    assert "VendorX" not in joined
    assert "40 percent" in joined


def test_page_furniture_scores_zero():
    """Three furniture classes rode page fetches into evidence on 2026-07-27,
    live from the pool probe. None of them is a fact about anything."""
    from app.source_facts import _score
    dead = [
        # GitHub chrome — shipped inside the Ami topic's evidence.
        "Ships with copilot chat and graph memory. · GitHub Skip to content "
        "You signed in with another tab or window.",
        # The RelativeDB SQL fragment the original classes missed: its periods
        # sat inside the [^.!?] spans.
        'NUMBER ) . column ( "signup_date" , ValueType . DATE ) . build ( )',
        # A nav row is pipes with short segments.
        "Rerun the pipeline as new traces arrive. 🌐 Platform | 📚 Docs | Discord",
    ]
    for sentence in dead:
        assert _score(sentence) == 0.0, f"kept furniture: {sentence[:60]!r}"


def test_real_facts_still_score():
    """The keep-guard: the good sentences from the same live probe must
    survive the furniture tightening."""
    from app.source_facts import _score
    alive = [
        "Across eight benchmarks, it posts the best published S-measure on "
        "four and comes within 2% of the leader on the rest.",
        "This grew the model modestly from 222M parameters to 263M.",
        "RelativeDB works best with many tables, 10-100 tables, without "
        "needing any feature engineering.",
        # NOT here: "The model (14B) beats the old one (70B)" — two bracket
        # pairs read as code under the ORIGINAL classes, a trade the module
        # already made. The tightening must not widen that, nothing more.
    ]
    for sentence in alive:
        assert _score(sentence) > 0.0, f"killed a real fact: {sentence[:60]!r}"


def test_page_path_is_untouched(monkeypatch):
    """When the source page itself answers, the gate never runs — the page was
    chosen by URL and is presumptively about its own subject."""
    # `conftest.py:29` sets STARLING_RESEARCH=off for every test so nothing
    # can reach the network. This test stubs the network itself and exercises
    # the FETCH path, so it opts in exactly as production does: `facts_for`
    # now honours the switch and returns {"facts": [], "source": "none"}
    # without it. The opt-in is not a workaround -- it documents that the
    # behaviour under test requires the user's consent.
    monkeypatch.setenv("STARLING_RESEARCH", "on")
    body = ("Snapshield snapshots the repo before every agent run. "
            "It restores the tree in under 2 seconds and supports 30 languages. "
            "The tool shipped its first beta this week.")
    monkeypatch.setattr(source_facts, "fetch_page_text", lambda url: body)
    monkeypatch.setattr(source_facts, "search_web",
                        lambda q, n: (_ for _ in ()).throw(AssertionError("searched")))
    found = source_facts.facts_for(
        "Snapshield – an undo button for AI coding agents",
        "https://example.com/snapshield")
    assert found["source"] == "page"
    assert found["facts"]
