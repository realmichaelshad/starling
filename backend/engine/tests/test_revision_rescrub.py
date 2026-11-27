"""Whatever the reader sees goes through every scrubber — including a rewrite.

The revise-once pass is fresh model output. It was re-scrubbed with
`strip_formulas` and nothing else, so any scrubber added later silently did not
apply to revisions. Measured live on 2026-07-27: of ten stored post drafts,
exactly one was still changed by `drop_reader_address` when re-run against it —

    "Mistral OCR4's 13-point gap shows newer models don't always outperform.

     If you're building with it, you're stuck with the same old problems."

— because that draft went through the clarity revision and came back with the
closer the inbound scrub had already removed.

This test is behavioural on purpose. A source-text assertion would go stale the
first time the loop is restructured, which is exactly how the NEVER2 registry
test rotted.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.intelligence import (_assemble_candidates, drop_reader_address,   # noqa: E402
                              get_personal_model)
from app.safety import assess_context                                      # noqa: E402
from app.schemas import DraftRequest, PostContext                          # noqa: E402


class _ReviserThatReintroducesIt:
    """Returns a clean-looking rewrite that carries a reader-address closer."""

    def __init__(self, rewrite: str) -> None:
        self.rewrite = rewrite
        self.calls = 0

    def revise_for_clarity(self, text, issues, request):
        self.calls += 1
        return self.rewrite


def _request() -> DraftRequest:
    return DraftRequest(action="post",
                        context=PostContext(author="a", text="coding agents in production",
                                            visibility="public"))


# RETIRED (API-native pass, 2026-09-22): test_a_revision_cannot_reintroduce_a_reader_address_closer: there is no clarity rewrite any more: style findings are notes on the card.


def test_the_scrubber_itself_still_works_on_that_text():
    """If this fails the test above is vacuous."""
    text = ("Mistral OCR4's 13-point gap shows newer models don't always outperform.\n\n"
            "If you're building with it, you're stuck with the same old problems.")
    assert drop_reader_address(text) == (
        "Mistral OCR4's 13-point gap shows newer models don't always outperform.")
