"""A whole draft that is only a claim about the reader.

`drop_reader_address` deletes a TRAILING block, which cannot help when the
reader claim is the entire draft. Measured 2026-07-27, quoting @levelsio's
spend-audit post:

    "You're spending like a king but still can't tell what half the money did."

One sentence, about a stranger, carrying no name and no figure — and the reader
is already looking at the post it is commenting on, so it adds nothing.

He addresses the reader in 0 of 35 published posts. Requiring `names_nothing`
as well keeps a second-person line that is doing real work shippable, because
the fault is emptiness rather than the pronoun.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.intelligence import _draft_quality_issues          # noqa: E402
from app.schemas import DraftRequest, PostContext           # noqa: E402


def _request(action: str) -> DraftRequest:
    return DraftRequest(action=action,
                        context=PostContext(author="a",
                                            text="I audited my OpenAI and Anthropic "
                                                 "spend for the first time.",
                                            visibility="public"))


def test_the_live_quote_is_flagged():
    issues = _draft_quality_issues(
        "You're spending like a king but still can't tell what half the money did.",
        _request("quote"), [])
    assert "talks_to_the_reader" in issues


def test_a_second_person_line_carrying_a_figure_survives():
    """The pronoun is not the fault; saying nothing is."""
    issues = _draft_quality_issues(
        "You're paying 4x what Goose charges for the same agent loop.",
        _request("quote"), [])
    assert "talks_to_the_reader" not in issues


def test_a_reply_may_address_the_person_it_answers():
    issues = _draft_quality_issues(
        "You're right that the routing is the bottleneck here.",
        _request("reply"), [])
    assert "talks_to_the_reader" not in issues


def test_it_is_revisable_and_never_a_bare_shrug():
    from app.intelligence import _CLARITY_ISSUES, _INTEGRITY_ISSUES, _WEAKNESS_NOTES
    assert "talks_to_the_reader" in _CLARITY_ISSUES
    assert "talks_to_the_reader" in _WEAKNESS_NOTES
    assert "talks_to_the_reader" not in _INTEGRITY_ISSUES


def test_it_never_fires_on_his_own_posts():
    fixture = Path(__file__).with_name("fixtures_his_posts.json")
    if not fixture.exists():
        return
    posts = [p for p in json.loads(fixture.read_text(encoding="utf-8")) if p.strip()]
    request = _request("post")
    hits = [p for p in posts
            if "talks_to_the_reader" in _draft_quality_issues(p, request, [])]
    assert not hits, f"fired on his own post: {hits[0][:90]!r}"
