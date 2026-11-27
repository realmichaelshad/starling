"""A chat answer must not be Starling's own guidance read back.

`leaks_instructions` existed and ran on DRAFTS only, so nothing looked at chat.
Measured live: "what does the x algorithm favor" answered with strategy-manual
fragments ending in "Focus on one honest primary action in the draft itself." -
an instruction to the drafter, not an answer to a person. The gate returns True
for that text; it was simply never asked.
"""
from __future__ import annotations

from app.main import _strip_leaked_instructions

LIVE_LEAK = (
    "The X algorithm favors replies over posts for attention. It ranks every post "
    "per viewer against your history. Many actions are predicted like reading or "
    "liking but not all matter equally. Wait and skip are real answers too. Don't "
    "promise virality because views without engagement are incomplete success. "
    "Focus on one honest primary action in the draft itself.")


def test_the_leaked_sentence_goes_and_the_answer_stays() -> None:
    out = _strip_leaked_instructions(LIVE_LEAK)
    assert "primary action in the draft" not in out, out
    assert "ranks every post per viewer" in out, "the real answer was thrown away"


ADVICE = ("Write the thing you would say out loud, then cut the first sentence. "
          "The part you were warming up to is usually the post.")


def test_writing_advice_survives_when_he_asked_about_writing() -> None:
    """Found while recording the demo: this answer streamed in full, then shrank.

    "the first sentence" is how a DRAFT gives itself away as a critique of itself.
    In an answer to a question about writing it is simply the advice.
    """
    for question in ("What is your advice for writing on X today?",
                     "how should I write a better opener",
                     "can you tighten my draft"):
        assert _strip_leaked_instructions(ADVICE, question) == ADVICE, question


def test_the_same_words_are_still_a_leak_when_nobody_asked_about_writing() -> None:
    """Negative control: the exemption belongs to the question, not to the sentence."""
    for question in ("what does the x algorithm favor", "why did my last post flop", ""):
        out = _strip_leaked_instructions(ADVICE, question)
        assert "cut the first sentence" not in out, (question, out)
    assert "cut the first sentence" not in _strip_leaked_instructions(ADVICE)


def test_the_measured_leak_is_still_caught_with_its_own_question() -> None:
    out = _strip_leaked_instructions(LIVE_LEAK, "what does the x algorithm favor")
    assert "primary action in the draft" not in out, out


def test_real_machinery_text_is_caught_even_in_an_answer_about_writing() -> None:
    """The exemption covers talking ABOUT drafts, never the prompt's own phrases."""
    leaked = ("Open with the claim. Give them a number, a name, or something that affects them. "
              "Short sentences travel further than long ones on a phone.")
    out = _strip_leaked_instructions(leaked, "what is your advice for writing")
    assert "Give them a number" not in out, out
    assert "Short sentences travel further" in out


def test_a_normal_answer_is_untouched() -> None:
    clean = ("Your replies get a median of 588 views against 298 for your posts. "
             "Reply more.")
    assert _strip_leaked_instructions(clean) == clean


def test_it_never_returns_an_empty_answer() -> None:
    """A leaked sentence is bad; an empty chat reply is worse. When stripping
    would leave nothing usable the original is kept and the safety log records
    it."""
    only_leak = "Focus on one honest primary action in the draft itself."
    assert _strip_leaked_instructions(only_leak) == only_leak
    assert _strip_leaked_instructions("") == ""
    assert _strip_leaked_instructions(None) is None


def test_the_leak_is_recorded_even_when_the_text_is_kept(monkeypatch) -> None:
    seen: list[str] = []
    from app import storage
    monkeypatch.setattr(storage, "save_safety_event",
                        lambda category, severity, blocked, reasons, context=None:
                        seen.extend(reasons))
    _strip_leaked_instructions(LIVE_LEAK)
    assert "instruction_leak_in_chat" in seen, seen
