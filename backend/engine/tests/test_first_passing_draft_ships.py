"""S1: the attempt loop stops on the first draft that passes the gates.

It used to stop on elapsed time (8 s for a reply, 20 s for a post) or on a score no draft
reached, so a faster model made MORE calls: on an API model every ✦ reply ran all four
attempts even when the first one passed every gate. These drive the real loop and the real
gates with a stand-in writer and count the calls.
"""
from __future__ import annotations

from app import intelligence
from app.schemas import DraftRequest, PostContext

POST = ("Been running Qwen locally for a month. The bottleneck was never the model, "
        "it was memory bandwidth.")
GOOD = "so the upgrade path for local qwen is faster memory, not a bigger model"


class Writer:
    """Hands back one scripted draft per attempt, and counts the attempts."""

    def __init__(self, *texts: str) -> None:
        self.texts = list(texts)
        self.calls = 0

    def generate(self, request, examples, hint, brief, *, angle_instruction="", attempt=0, retry_note=""):
        text = self.texts[min(self.calls, len(self.texts) - 1)]
        self.calls += 1
        return [{"text": text, "rationale": "adds the consequence"}], [], "unavailable"

    def revise_for_clarity(self, text, issues, request):
        return None

    @staticmethod
    def _fallback_candidates(request):
        return []


def _reply() -> DraftRequest:
    return DraftRequest(action="reply", research_mode="off", media_policy="no_media", grok_permission=False,
                        direction="Use the visible post faithfully and write one useful original point.",
                        context=PostContext(author="Some Dev", author_handle="somedev", text=POST,
                                            visibility="public", post_id="17900000000000000042"))


def test_a_first_draft_that_passes_every_gate_is_the_only_call():
    writer = Writer(GOOD, "a second draft that should never be asked for")
    result = intelligence.generate_drafts(_reply(), writer, brief={})
    assert writer.calls == 1, "a draft that passed the gates must end the run"
    assert result and result[0].text == GOOD and not result[0].needs_review


def test_a_first_draft_that_copies_the_post_buys_exactly_one_more():
    writer = Writer("The bottleneck was never the model, it was memory bandwidth.", GOOD)
    result = intelligence.generate_drafts(_reply(), writer, brief={})
    assert writer.calls == 2
    assert result and result[0].text == GOOD


def test_the_streamed_path_stops_by_the_same_rule():
    class StreamWriter(Writer):
        def generate_stream(self, request, examples, hint, brief, emit, stop_event=None,
                            angle_instruction="", attempt=0, retry_note=""):
            text = self.texts[min(self.calls, len(self.texts) - 1)]
            self.calls += 1
            emit(text)
            return {"text": text, "rationale": "adds the consequence"}

    writer = StreamWriter(GOOD, "never asked for")
    result = intelligence.generate_drafts_streaming(_reply(), writer, lambda _t: None, brief={})
    assert writer.calls == 1
    assert result and result[0].text == GOOD
