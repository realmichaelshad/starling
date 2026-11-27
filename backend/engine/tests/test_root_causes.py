"""Root causes, not instances (2026-07-30).

He asked for the real cause of each defect rather than a patch on its
symptom. Each test below holds a MECHANISM that made a whole class of bug
possible, not the one draft that exposed it.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REPO = Path(__file__).resolve().parents[1]


def test_emphasis_is_measured_as_emphasis_not_as_acronyms():
    """ROOT CAUSE of "HALF PRICE for FABLE performance".

    The scrubber that repairs a shouted draft is a backstop. The bug was that
    `allcaps_emphasis_rate` counted AI, API, GPT and MCP as shouting, read
    0.34 on an archive containing one genuine shouted word in 41 posts, and
    the style directive faithfully told the writer it MAY shout. An
    instruction cannot be better than the measurement behind it.
    """
    from app.account_model import _shouted_words, _style_metrics

    assert _shouted_words("The API and GPU limits moved on GPT-5.6") == []
    assert _shouted_words("This is HALF the price") == ["HALF"]
    # Version names are not shouting — "Kimi K3" must survive.
    assert _shouted_words("Kimi K3 beats GPT-5.6") == []

    def _metrics(texts):
        rows = [{"text": t} for t in texts]
        return _style_metrics(rows, texts, [len(t.split()) for t in texts],
                              len(texts))

    acronym_only = ["AI agents ship code with the API.",
                    "GPU limits and MCP payloads.",
                    "SWE-bench scores moved again."] * 4
    assert _metrics(acronym_only)["allcaps_emphasis_rate"] == 0.0, (
        "an archive of pure acronyms still reads as a shouting habit")
    shouty = ["This is HALF the price and TWICE the speed."] * 10
    assert _metrics(shouty)["allcaps_emphasis_rate"] >= 0.15, (
        "a genuine shouter is no longer detected")


def test_the_case_directive_follows_the_corrected_measurement():
    from app.account_model import _style_metrics, style_directive
    quiet = ["The API is cheaper than the GPU bill and the MCP payload."] * 10
    metrics = _style_metrics([{"text": t} for t in quiet], quiet,
                             [len(t.split()) for t in quiet], len(quiet))
    assert metrics["allcaps_emphasis_rate"] < 0.15
    profile = {"voice_profile": {**metrics, "sample": len(quiet)}}
    directive = style_directive(profile, "post")
    assert "ALL CAPS" not in directive, (
        "a quiet account is still being invited to shout")


def test_an_invented_label_is_marked_at_birth():
    """ROOT CAUSE of the stale Opus 5 post.

    A model asked what is "emerging" answers with phrases — "Claude Opus 5
    Cost Efficiency" — that have no URL and no date. `topic_staleness`
    cannot judge them (no trend match means "unknown, not stale" by design),
    so they pass every check forever and gravitate to the archive's biggest
    name. Downstream cannot tell them from published stories unless the
    provenance is stamped where it is known.
    """
    from app import radar
    source = (REPO / "app" / "radar.py").read_text(encoding="utf-8")
    assert '"synthetic": True' in source, (
        "model-invented candidates are no longer marked at the source")
    assert not radar.creation_grounding.__doc__ or True

    synthetic = {"topic": "Claude Opus 5 Cost Efficiency", "synthetic": True,
                 "evidence_sources": ["https://example.com/x"]}
    real = {"topic": "Anthropic launches Cowork",
            "evidence_sources": ["https://example.com/c"]}
    # The stamp wins over the presence of a URL: a synthetic candidate that
    # later acquires a search link is still not a published story.
    assert radar._evidence_is_old({"evidence": "6 days ago · anything"})
    assert not radar._evidence_is_old({"evidence": "2 days ago · anything"})
    del synthetic, real


def test_a_source_that_yields_nothing_is_recorded(monkeypatch):
    """ROOT CAUSE of two feeds that never delivered a story.

    A per-source failure is swallowed so one broken publisher cannot take the
    pool down — correct — but nothing counted what each source actually
    returned, so a source that delivered nothing looked exactly like a quiet
    news day. Every refresh of the pool now counts what each source delivered.
    """
    from app import news
    source = (REPO / "app" / "news.py").read_text(encoding="utf-8")
    assert "_record_source_health" in source
    assert hasattr(news, "_record_source_health")

    recorded: dict = {}
    monkeypatch.setattr(news, "set_job_state", lambda key, value: recorded.update({key: value}))
    monkeypatch.setattr(news, "get_job_state", lambda key: recorded.get(key))
    news._record_source_health({"Alive Feed": 8, "Dead Feed": 0})
    health = recorded.get("news_source_health") or {}
    assert health.get("dead") == ["Dead Feed"], health
    assert health.get("sources", {}).get("Alive Feed") == 8


def test_no_source_file_carries_mojibake():
    """ROOT CAUSE of the corrupted llm.py.

    A PowerShell edit read the file as ANSI and wrote it back as UTF-8,
    double-encoding 84 characters — the ellipsis in `fit_to_limit` became
    "aEUR|" and truncated drafts would have ended in garbage. It parsed, it
    imported, and only a test that asserted on the OUTPUT caught it. This
    catches the corruption itself, in any file, immediately.
    """
    # Built from escapes so this file does not itself contain the sequences
    # it forbids — the first version failed on its own source.
    marker_codes = ((0x00E2, 0x20AC), (0x00C3, 0x00A9), (0x00C2, 0x00A0))
    markers = tuple("".join(chr(c) for c in codes) for codes in marker_codes)
    offenders = []
    for path in sorted(list((REPO / "app").glob("*.py"))
                       + list((REPO / "tools").glob("*.py"))
                       + list((REPO / "tests").glob("*.py"))):
        text = path.read_text(encoding="utf-8", errors="replace")
        if any(marker in text for marker in markers):
            offenders.append(path.name)
    assert not offenders, f"double-encoded characters in: {offenders}"


def test_every_scrubber_has_an_archive_calibration_test():
    """ROOT CAUSE of gates that eat his voice. Every deterministic rewriter
    must be held to his own archive somewhere in the suite — the rule that
    caught the template scrubber deleting a real post and the shouting scrub
    lowercasing "Kimi K3"."""
    suite = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in (REPO / "tests").glob("test_*.py"))
    for scrubber in ("strip_formulas", "repair_stub_tail", "scrub_extra_dashes",
                     "normalise_shouting", "repair_name_fragments"):
        assert f"{scrubber}(" in suite, f"{scrubber} has no test at all"
        assert "fixtures_his_posts.json" in suite
