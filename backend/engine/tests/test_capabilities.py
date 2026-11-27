"""Reachability is a product requirement, so it is a test.

Three of the worst faults found on 2026-07-22 were invisible to every existing
test because unit tests call functions directly and a person cannot call a
function: images never returned once behind a swallowed 401, the self-reply
button was hidden, and `assess_opportunity`'s momentum verdict ("taking off —
answer the best replies now") had no way to be asked for at all.

These tests walk the same dispatch a typed message walks.
"""
from __future__ import annotations

import pytest

from app.capabilities import CAPABILITIES, capability_answer, find_capability
from app.main import _spec_chat_answer

OWN_POST = {
    "post_id": "t-1", "author": "Alex Rivera", "author_handle": "example_builder",
    "is_own_post": True, "visibility": "public",
    "text": "The next open-weight coding model might be the reason the flagship slips.",
}
SOMEONE_ELSES = {**OWN_POST, "is_own_post": False, "author_handle": "claudeai"}


def test_every_capability_declares_provable_samples():
    """A capability with no sample phrases cannot be proven reachable, so it is
    not allowed to exist."""
    for capability in CAPABILITIES:
        assert capability.samples, f"{capability.key} declares no sample phrases"
        assert capability.summary.strip(), f"{capability.key} has no summary"


@pytest.mark.parametrize("capability", CAPABILITIES, ids=lambda c: c.key)
def test_sample_phrases_reach_their_own_capability(capability):
    for phrase in capability.samples:
        hit = find_capability(phrase)
        assert hit is not None, f"{phrase!r} reaches NOTHING — unreachable"
        assert hit.key == capability.key, \
            f"{phrase!r} reaches {hit.key}, not {capability.key}"


@pytest.mark.parametrize("capability", CAPABILITIES, ids=lambda c: c.key)
def test_every_capability_answers_through_real_chat_dispatch(capability):
    """The public path: a message string in, a real answer out."""
    context = OWN_POST if capability.needs_context else None
    answer = _spec_chat_answer(capability.samples[0], context)
    assert answer and answer.strip(), \
        f"{capability.key} produced no answer — it is unreachable in practice"


@pytest.mark.parametrize("capability", CAPABILITIES, ids=lambda c: c.key)
def test_no_answer_tells_him_to_say_what_he_just_said(capability):
    """A capability may not answer by instructing its own trigger phrase.

    The privacy answer told him: "Say 'export my data' in the dock's settings"
    — which is the exact sentence that reached it, so the reply to "export my
    data" was "say 'export my data'". It also named a control that has never
    existed; there is no export or delete affordance in the panel at all.

    Circularity is the detectable half, and it is a reliable smell for the
    other: an answer that redirects to a phrase it already owns is describing
    an affordance rather than doing something. Checked for every capability,
    because the shape is not specific to this one.
    """
    context = OWN_POST if capability.needs_context else None
    for phrase in capability.samples:
        answer = (capability_answer(phrase, context) or "").lower()
        if not answer:
            continue
        for quoted in (f"say '{phrase.lower()}'", f'say "{phrase.lower()}"',
                       f"say {phrase.lower()}"):
            assert quoted not in answer, (
                f"{capability.key} answers {phrase!r} by telling him to say "
                f"{phrase!r} — a loop, and usually a control that is not there")


def test_capabilities_never_take_chat_down():
    """A handler that blows up must degrade to normal chat, never raise.

    Returning None is legitimate (it means "let the model handle this"); raising
    is not, because it would 500 the whole assist request.
    """
    for capability in CAPABILITIES:
        for context in (None, OWN_POST, {}, {"post_id": None}):
            try:
                capability_answer(capability.samples[0], context)
            except Exception as error:  # pragma: no cover - the failure IS the bug
                pytest.fail(f"{capability.key} raised {type(error).__name__}: {error}")


def test_a_raising_handler_is_swallowed_not_propagated(monkeypatch):
    """Prove the guard actually works rather than trusting the try/except."""
    from app import capabilities as caps

    boom = caps.Capability(
        key="boom", summary="explodes", matcher=caps.re.compile(r"\bexplode\b"),
        handler=lambda *_: (_ for _ in ()).throw(RuntimeError("boom")),
        samples=("explode",))
    monkeypatch.setattr(caps, "CAPABILITIES", (boom,))
    assert caps.capability_answer("explode", None) is None


def test_ordinary_chat_is_not_hijacked():
    """The registry must not swallow normal conversation."""
    for phrase in ("hey", "write me a post about kimi", "reply to this",
                   "thanks", "what do you think of my last post"):
        assert find_capability(phrase) is None, f"{phrase!r} was hijacked"


def test_boost_is_reachable_and_knows_whose_post_it_is():
    """The flagship gap: a finished momentum verdict nothing could ask for."""
    answer = _spec_chat_answer("boost this", OWN_POST)
    assert answer and "self-reply" in answer.lower()

    # It must refuse to "boost" someone else's post rather than pretend.
    other = _spec_chat_answer("boost this", SOMEONE_ELSES)
    assert other and "not your post" in other.lower()

    # With no post open it asks for one instead of inventing a verdict.
    bare = _spec_chat_answer("boost this", None)
    assert bare and ("open the post" in bare.lower() or "attach" in bare.lower())


def test_capabilities_are_honest_when_evidence_is_thin():
    """Timing must refuse to invent a window it cannot measure."""
    answer = _spec_chat_answer("when should i post", None)
    assert answer
    lowered = answer.lower()
    assert ("needed before recommending" in lowered
            or "not have enough" in lowered
            or "measured from" in lowered), answer
