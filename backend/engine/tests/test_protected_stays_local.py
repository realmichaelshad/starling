"""A protected post's text never leaves, and the off switch reaches everything.

TWO CLAIMS THE PRODUCT MADE AND DID NOT KEEP.

ONE. `docs/user-guide/privacy-and-security.md` says, unconditionally: "Protected
posts stay local and are never sent for research or media reuse."
`radar.context_grounding` had NO visibility check — the whole file had zero
references to `visibility` or `protected`. The quote path did refuse protected
accounts, at main.py:3001, but its predicate is
`action == "quote" and visibility == "protected"`, so the REPLY path was excluded
by the condition's own shape. Someone gated one action and the page promised
both, so replying to a protected post sent its text out as a search query.

TWO. `source_facts` had zero references to `research_enabled`, and the
`fetch_page_text` it calls checks only `_safe_to_fetch` — an SSRF guard, not a
consent one. So a third-party article the user's browser never requested was
fetched with research turned off. Nine call sites across analyzer, images, main,
radar and release_radar honour the switch; this was the tenth that did not.

THE PREDICATE'S DIRECTION IS THE SUBTLE PART. `visibility` is
Literal["public","protected","unknown"] with default "unknown". `== "protected"`
gates what the page means. `!= "public"` would gate every post whose visibility
the extension failed to capture — the same words, the opposite blast radius —
and would silently disable grounding for most real posts.
"""
from __future__ import annotations

import pytest

from app import radar, source_facts


class Ctx:
    def __init__(self, visibility, text="A claim about a launch today."):
        self.visibility = visibility
        self.text = text
        self.quote_text = ""
        self.author_handle = "someone"


# --------------------------------------------------------------------------- #
# a protected post is not researched, whatever the action
# --------------------------------------------------------------------------- #

def test_a_protected_post_is_never_grounded(monkeypatch):
    """The reply path, which the quote path's predicate excluded by shape.

    The research module is imported INSIDE the function, so the probe is placed
    on the module itself: if the gate fails, the call reaches this and raises.
    """
    def must_not_run():
        raise AssertionError("a protected post reached the research machinery")

    monkeypatch.setattr("app.research.research_enabled", must_not_run)
    assert radar.context_grounding(Ctx("protected")) is None


def test_a_public_post_is_still_grounded(monkeypatch):
    """The gate must not become an outage: the common case still gets there."""
    reached = []
    monkeypatch.setattr("app.research.research_enabled",
                        lambda: reached.append("reached") or False)
    radar.context_grounding(Ctx("public"))
    assert reached, "a public post was short-circuited by the visibility gate"


def test_an_unknown_visibility_is_not_treated_as_protected(monkeypatch):
    """THE DIRECTION OF THE PREDICATE. "unknown" is the DEFAULT — the extension
    does not always capture visibility. Gating it would disable grounding for
    most real posts while looking like a privacy win."""
    for value in ("unknown", "public", "", None):
        reached = []
        monkeypatch.setattr("app.research.research_enabled",
                            lambda: reached.append("reached") or False)
        radar.context_grounding(Ctx(value))
        assert reached, f"visibility {value!r} was treated as protected"


def test_the_gate_matches_the_quote_paths_predicate():
    """One rule, written the same way in both places, so they cannot drift."""
    import inspect
    source = inspect.getsource(radar.context_grounding)
    body = source.split('"""')[-1]        # the docstring EXPLAINS the trap; skip it
    assert '== "protected"' in body
    assert '!= "public"' not in body, "the negative predicate has the opposite blast radius"


# --------------------------------------------------------------------------- #
# the off switch reaches the article fetch
# --------------------------------------------------------------------------- #

def test_research_off_fetches_no_article(monkeypatch):
    fetched = []
    monkeypatch.setattr(source_facts, "fetch_page_text",
                        lambda url, **k: fetched.append(url) or "body")
    monkeypatch.setenv("STARLING_RESEARCH", "off")
    got = source_facts.facts_for("A headline", "https://example.com/article")
    assert fetched == [], f"the off switch was ignored: {fetched}"
    assert got == {"facts": [], "url": None, "source": "none"}, got


def test_research_on_still_fetches(monkeypatch):
    """The gate must not become an outage. Default is on."""
    fetched = []
    monkeypatch.setattr(source_facts, "fetch_page_text",
                        lambda url, **k: fetched.append(url) or "")
    monkeypatch.delenv("STARLING_RESEARCH", raising=False)
    source_facts.facts_for("A headline", "https://example.com/article")
    assert fetched, "research is on and the article was not fetched"
