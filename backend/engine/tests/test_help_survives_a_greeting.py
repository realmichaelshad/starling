"""A greeting in front of "what can you do" is not a different question.

`_HELP_Q` is fully anchored, so "hey, what can you do?" missed the deterministic
capability menu and fell through to the model — the path that once invented a
follower delta ("14 vs 16") rather than saying it did not know.

READ THE CALIBRATION BEFORE WIDENING THIS. Measured over every distinct message
ever typed at Starling on this machine (194): `_HELP_Q` matched exactly one
("help"), there were zero near-misses, and the greeting strip recovers zero of
them. The change is not measurable on his archive and should not be — "what can
you do" is a day-one question and he has used the product for months, so his
archive is the wrong population to look for it in.

That inverts what these tests have to prove. Not "how many does it catch" —
"how many does it steal". Stripping only ever feeds a SHORTER string to an
anchored pattern, so a false positive requires the remainder to be a help
phrase exactly; the negatives below are the ones where that could plausibly
happen. Re-measured after the change: 1 of 194, the same one.
"""
from __future__ import annotations

import pytest

from app.main import _HELP_Q, _strip_greeting


def _routes_to_menu(message: str) -> bool:
    return bool(_HELP_Q.match(_strip_greeting(message)))


# --------------------------------------------------------------------------- #
# what it now catches
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("message", [
    "hey, what can you do?",
    "hi what can you do",
    "hello, help",
    "yo what are you",
    "ok so how does this work",
    "okay, how do you work?",
    "so what do you do",
    "hey there help me",
    "um, who are you",
    "Hey — what can you do?",
])
def test_a_greeting_does_not_hide_the_question(message):
    assert _routes_to_menu(message), message


@pytest.mark.parametrize("message", [
    "hey what can u do",          # typed exactly like this in real use
    "what can u do",
    "hey, what can u do?",
])
def test_his_own_spelling_reaches_the_menu(message):
    """THE CORRECTION EXISTED AND THIS PATH DID NOT USE IT. `_TYPO_MAP` has
    mapped `u`->`you` since it was written; `_help_answer` never called
    `normalize_typos`, so "hey what can u do" missed a fully-anchored `_HELP_Q`
    and went to the model — the path that invented the follower delta.

    Re-calibrated over his 194 distinct messages: 1 reached the menu before this
    change, 2 after, and the second is this exact message. The count moved by
    the target and by nothing else.

    NORMALISATION IS NOT PARAPHRASE. `_HELP_Q` stays a fixed, anchored phrase
    list; typo-correcting the tokens does not teach it new phrasings.
    "cna u tell me what u do" normalises cleanly to "can you tell me what you
    do" and still misses — correctly, because that phrasing is in neither the
    pattern nor his archive, and widening the pattern to reach it would be an
    uncalibrated change dressed up as this fix.
    """
    from app.main import _help_answer

    assert _help_answer(message), message


def test_normalisation_did_not_widen_the_gate():
    """Normalisation corrects a token toward the routing vocabulary; it does not
    relax the anchor. Everything that was out stays out."""
    from app.main import _help_answer

    for message in ["help me write a post about opus 5",
                    "what can you do about the rhetorical questions",
                    "so post about vllm", "ok format it",
                    "hey reply to this", "yo boost this", "u"]:
        assert not _help_answer(message), message


def test_the_bare_question_still_works():
    """The regression that matters most: the change is a strip in front of the
    existing match, not a replacement for it."""
    for message in ["help", "what can you do", "how does this work", "who are you"]:
        assert _routes_to_menu(message), message


# --------------------------------------------------------------------------- #
# what it must NOT steal — the only real risk here
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("message", [
    # real work that opens with a greeting word
    "so post about vllm",
    "okay draft a reply",
    "ok format it",
    "hey reply to this",
    "so what should i do now",
    "ok what's my goal",
    "hi can you post for me",
    "yo boost this",
    # help-adjacent but a different question
    "what can you do about the rhetorical questions",
    "how does this work for quotes",
    "who are you replying to",
    "what are you drafting",
    "help me write a post about opus 5",
    # bare greetings are somebody else's job, not the capability menu's
    "hello",
    "hi",
    "hey",
    "yo",
])
def test_it_steals_nothing(message):
    assert not _routes_to_menu(message), message


def test_the_strip_is_bounded():
    """Two openers is real speech ("ok so ..."); three is somebody probing the
    regex, and an unbounded loop is how a strip turns into a wildcard."""
    assert _routes_to_menu("ok so what can you do")
    assert not _routes_to_menu("hey hey hey what can you do")


# --------------------------------------------------------------------------- #
# the strip itself
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw, expected", [
    ("hey, what can you do?", "what can you do?"),
    ("ok so how does this work", "how does this work"),
    ("post about vllm", "post about vllm"),
    ("hi", "hi"),                       # no separator, so nothing to strip
    ("history of x", "history of x"),   # "hi" is not a prefix of a word
])
def test_strip_greeting_is_a_prefix_removal(raw, expected):
    assert _strip_greeting(raw) == expected


def test_the_result_is_never_longer():
    """The safety argument for this whole change: an anchored pattern fed a
    shorter string cannot match more broadly."""
    for message in ["hey draft a post", "ok", "so", "  hello there, help  ", ""]:
        assert len(_strip_greeting(message)) <= len(message.strip())
