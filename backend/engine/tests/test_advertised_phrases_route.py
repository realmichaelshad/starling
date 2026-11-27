"""Every phrase the help menu tells him to type must reach something.

`_help_answer` carries a comment naming the exact hazard: "A feature nobody can
find is as good as unbuilt, and this menu drifting out of date is exactly how
that happens." It then builds the list from the capability registry so a new
capability appears automatically — and puts TWO advertisements above that call,
hardcoded, outside the registry and outside anything that checked them:

    "Draft a post, reply or quote in your voice — try \"reply to this\""
    "Fix the spacing on a draft — try \"format it\""

So the drift the comment feared was guarded for the generated lines and open for
the hand-written ones. This asserts on the RENDERED text, which is the only form
that contains both.

MEASURED 2026-08-07 BEFORE WRITING THIS: all 14 advertised phrases route — 12
through the registry, "reply to this" through `_wants_draft`, "format it"
through `_wants_formatting`. **This test guards a property that currently
holds.** It is a regression guard, not a bug fix, and it says nothing about
phantom controls elsewhere in the product — an absence proved in the help menu
is not an absence everywhere.

WHY THREE RECOGNIZERS AND NOT ONE. Formatting is not a registry capability at
all; it is `_wants_formatting`, wired above the registry lookup. A check that
asked only `find_capability` would report "format it" as advertised-but-dead —
which is precisely the false conclusion drawn from that single lookup earlier
tonight. The recognizers are enumerated here so a phrase claimed by a NEW route
fails loudly and gets added, rather than being silently absorbed.
"""
from __future__ import annotations

import re

import pytest

from app.capabilities import capability_menu, find_capability
from app.main import _help_answer, _wants_draft, _wants_formatting


def _advertised() -> list[str]:
    """Every `try "X"` in the rendered menu — hardcoded lines included."""
    return re.findall(r'try "([^"]+)"', _help_answer("what can you do") or "")


def _claims(phrase: str) -> list[str]:
    found = []
    if find_capability(phrase):
        found.append("registry")
    if _wants_formatting(phrase):
        found.append("formatting")
    if _wants_draft(phrase, has_context=True):
        found.append("draft")
    return found


def test_every_advertised_phrase_reaches_a_recognizer():
    phrases = _advertised()
    assert phrases, "the help menu advertised nothing — it used to advertise 14"
    dead = [p for p in phrases if not _claims(p)]
    assert not dead, (
        "the menu tells him to type these and nothing routes them: "
        + ", ".join(repr(p) for p in dead))


def test_the_hardcoded_advertisements_are_covered():
    """THE POINT OF THIS FILE. These two are not in `capability_menu()`, so a
    test that walked the registry would pass while they rotted."""
    phrases = _advertised()
    generated = " ".join(capability_menu())
    for hand_written in ("reply to this", "format it"):
        assert hand_written in phrases, (
            f"{hand_written!r} left the menu; if it was renamed, rename it here "
            "too — this test is the only thing checking the hand-written lines")
        assert f'try "{hand_written}"' not in generated, (
            f"{hand_written!r} is now generated; drop it from this list so the "
            "test keeps measuring the hand-written lines specifically")


@pytest.mark.parametrize("phrase, recognizer", [
    ("reply to this", "draft"),
    ("format it", "formatting"),
])
def test_the_hand_written_pair_routes_where_it_claims_to(phrase, recognizer):
    """Named individually so a failure says WHICH promise broke.

    "format it" is the one that matters: it is not a registry capability, and
    reading its absence from `find_capability` produced a false "there is no
    formatting capability at all" earlier tonight. `_wants_formatting` also
    absorbs his real misspellings, which
    `test_browser_assist.test_format_intent_survives_the_users_real_typos`
    pins across nine spellings.
    """
    assert recognizer in _claims(phrase)


def test_the_menu_is_not_silently_shrinking():
    """A registry that fails to load would leave the two hand-written lines and
    an otherwise empty menu — which still passes every check above."""
    assert len(_advertised()) >= 10, (
        f"only {len(_advertised())} advertisements rendered; the registry "
        "portion of the menu has probably failed to load")


# --------------------------------------------------------------------------- #
# THE REST OF THE SAMPLES — the menu shows `samples[0]` and nothing else.
#
# `capability_menu()` renders one line per capability using `c.samples[0]`, so
# the 14 phrases above are all the fence above could see. The registry declares
# 47 samples across 12 capabilities; the other 35 are the phrasings the product
# claims to understand, checked by nothing. A sample is a promise in the same
# way a menu line is — it is what a reader of the registry, or of the docs built
# from it, will type.
#
# MEASURED 2026-08-08 BEFORE WRITING THIS: 47 of 47 route, every one to its own
# capability, and every one returns a real answer with no post open. This pins a
# property that already holds. It is not a bug fix and it does not say anything
# about phrases that are NOT samples.
# --------------------------------------------------------------------------- #

def _samples() -> list[tuple[str, str]]:
    from app.capabilities import CAPABILITIES
    return [(c.key, s) for c in CAPABILITIES for s in (c.samples or ())]


def test_every_capability_advertises_at_least_one_phrase():
    """`capability_menu()` skips a capability with no samples, so an empty
    tuple removes a working feature from the only place it is listed."""
    from app.capabilities import CAPABILITIES

    silent = [c.key for c in CAPABILITIES if not c.samples]
    assert not silent, f"advertised nowhere and therefore unfindable: {silent}"


@pytest.mark.parametrize("key, sample", _samples(), ids=lambda v: v)
def test_every_sample_routes_to_its_own_capability(key, sample):
    """Not just "routes" — routes HERE. `find_capability` returns the first
    matcher that hits, so a broad new pattern can quietly swallow another
    capability's advertised phrase and the product still answers something."""
    got = find_capability(sample)
    assert got is not None, f"{key}: {sample!r} advertised and routes nowhere"
    assert got.key == key, (
        f"{sample!r} is advertised for {key} but reaches {got.key} — "
        "a matcher earlier in the registry has claimed it")


@pytest.mark.parametrize("key, sample", _samples(), ids=lambda v: v)
def test_every_sample_answers_without_a_post_open(key, sample):
    """Routing is not answering. `capability_answer` swallows every exception
    from a handler and returns None, which drops the message through to the
    model — the exact path these deterministic answers exist to avoid, and
    invisible from the outside because a plausible answer still comes back.

    Passed `None` context on purpose: `needs_context` capabilities are expected
    to say what they need rather than return nothing.
    """
    from app.capabilities import capability_answer

    answer = capability_answer(sample, None)
    assert isinstance(answer, str) and answer.strip(), (
        f"{key}: {sample!r} routes but produces no answer, so it falls through "
        "to the model")
