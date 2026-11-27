"""Someone else's post cannot decide how his question is routed.

`buildChatPayload` (extension/entrypoints/content.ts:6617) sends
``${ask}\\n\\n${sections}`` — his instruction first, then the full text of every X
post and file he attached. Two gates scanned all of it.

Observed in real use: a typo-heavy "what should I do with this post" question
with two benchmark posts attached. `Frontier-Bench vs 20.5` matched the
web-lookup gate's ``\\w+ vs \\w+``; `ARC-AGI-3 vs 7.1` matched it again. He
asked what to do with a post and the reply would have opened *"I looked it
up:"*. The off-topic deflection fired on the same attached text, so with
research disabled he would have been told his question was out of scope
instead. Neither gate matched a single word he typed.

Small n and not a probabilistic defect: "X vs Y", "who is", "when is", "tell
me about" are ordinary on X, and benchmark posts are made of them.

Re-measured after the change: all 6 attachment messages clear both gates, and
of the 188 messages with no attachment, 0 have their text changed and 0 change
which gate they hit.
"""
from __future__ import annotations

import pytest

from app.main import _out_of_scope_answer, _typed_part, _WEB_LOOKUP_Q


HIS_MESSAGE = (
    "wat do u think i shuold do with this psot shuold i mkae a reply or "
    "wahtever makes sense\n"
    "\n"
    "Attached X posts to discuss:\n"
    '• Example Analyst @example_analyst: "The new flagship is here. The benchmark '
    "spread is impressive: 41.0% on Frontier-Bench vs 20.5% for the last model, "
    '29.8% on ARC-AGI-3 vs 7.1% for its closest rival" [has image]\n'
    "https://x.com/example_analyst/status/1234567890123456789"
)


# --------------------------------------------------------------------------- #
# the case
# --------------------------------------------------------------------------- #

def test_the_attached_benchmarks_do_not_trigger_a_web_lookup():
    assert _WEB_LOOKUP_Q.search(HIS_MESSAGE), (
        "control: the raw payload still contains the trigger, so this test is "
        "measuring the split and not a change to the pattern")
    assert not _WEB_LOOKUP_Q.search(_typed_part(HIS_MESSAGE))


def test_the_attached_benchmarks_do_not_deflect_as_off_topic():
    assert _out_of_scope_answer(HIS_MESSAGE) is None


def test_what_he_typed_is_what_survives():
    assert _typed_part(HIS_MESSAGE) == (
        "wat do u think i shuold do with this psot shuold i mkae a reply or "
        "wahtever makes sense")


# --------------------------------------------------------------------------- #
# both section markers content.ts can emit
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("marker", [
    "Attached X post to discuss:",
    "Attached X posts to discuss:",
    "Attached file to read as source material:",
    "Attached files to read as source material:",
])
def test_every_section_marker_is_a_boundary(marker):
    assert _typed_part(f"draft something\n\n{marker}\nwho is Sam Altman") == "draft something"


def test_a_file_attachment_cannot_route_either():
    payload = ("write a post from this\n\nAttached file to read as source "
               "material:\n\n--- Attached file: notes.md (2 KB) ---\n"
               "tell me about the latest Gemini release, and who is behind it")
    assert _WEB_LOOKUP_Q.search(payload), "control"
    assert not _WEB_LOOKUP_Q.search(_typed_part(payload))


# --------------------------------------------------------------------------- #
# what must not change
# --------------------------------------------------------------------------- #

def test_a_real_lookup_still_reaches_the_gate():
    """The gate is narrowed to his words, not disabled."""
    for message in ["look up the opus 5 pricing", "who is sam altman",
                    "what's the latest on gemini", "grok vs claude"]:
        assert _WEB_LOOKUP_Q.search(_typed_part(message)), message


def test_a_real_lookup_survives_an_attachment():
    """He can attach a post AND ask a lookup question about it — the split
    keeps his question, so this still works."""
    payload = ("who is this guy\n\nAttached X post to discuss:\n"
               '• Someone @someone: "hello"')
    assert _WEB_LOOKUP_Q.search(_typed_part(payload))


@pytest.mark.parametrize("message", [
    "what should i do now",
    "reply to this",
    "post about vllm",
    "",
])
def test_a_message_with_no_attachment_is_untouched(message):
    assert _typed_part(message) == message.strip()


def test_none_does_not_raise():
    assert _typed_part(None) == ""


# --------------------------------------------------------------------------- #
# THE SCAFFOLDING IS NOT A SIGNAL. The section header is the literal string
# "Attached X post to discuss:", so every message with a post attached carried
# the draft noun "post" — supplied by Starling itself, not by him and not even
# by the other account.
# --------------------------------------------------------------------------- #

HEADER = ('\n\nAttached X post to discuss:\n'
          '• Someone @someone: "hello world"')


@pytest.mark.parametrize("message", [
    "what does wait mean",
    "should i care about this",
    "whats my goal",
    "why is starling slow",
    "how is my account doing",
])
def test_attaching_a_post_does_not_make_a_question_a_draft_request(message):
    """The first two flipped from a chat answer to the drafting pipeline on the
    strength of the header alone."""
    from app.main import _wants_draft

    assert not _wants_draft(message, has_context=True), f"{message} (alone)"
    assert not _wants_draft(message + HEADER, has_context=True), f"{message} (attached)"


@pytest.mark.parametrize("message", [
    "reply to this",
    "write a post about this",
    "quote this",
    "wat shuold i do with this psot",      # typo-heavy, and still a real ask
])
def test_a_real_draft_request_with_a_post_attached_still_drafts(message):
    from app.main import _wants_draft

    assert _wants_draft(message + HEADER, has_context=True), message


def test_the_marker_must_start_a_line():
    """A post that merely mentions the phrase mid-sentence is not a section
    header, and cutting there would throw away his question."""
    message = "i said Attached X posts to discuss: in a draft once, what now"
    assert _typed_part(message) == message
