"""The block scrubbers must run AFTER the formatter, because it makes the blocks.

`drop_reader_address` and `drop_category_verdict` only look at a TRAILING
BLOCK, and `_match_account_style` is what turns one paragraph into several. Run
only on the way in, they therefore did nothing at all for any draft the model
wrote as a single paragraph — which is most of them.

Measured live 2026-07-27, taken straight out of `generation_candidates` after
both scrubbers were already deployed and passing their own unit tests:

    "Two coding agents can chat now. It's a new way to build apps.
     \\n\\n
     You're not using it yet."

`drop_reader_address` changes that text. It shipped anyway, because when the
scrubber ran there was only one block to look at.

This is the third instance of the same trap in this codebase — the comment
above the second `strip_formulas` call already records the first two. Any
scrubber that reasons about blocks or sentence boundaries belongs on BOTH
sides of the shaper.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.intelligence import (_assemble_candidates, get_personal_model)   # noqa: E402
from app.safety import assess_context                                     # noqa: E402
from app.schemas import DraftRequest, PostContext                         # noqa: E402


def _request() -> DraftRequest:
    return DraftRequest(action="post",
                        context=PostContext(author="a", text="coding agents",
                                            visibility="public"))


def _brief() -> dict:
    """A brief with a real account style, so the shaper actually reshapes."""
    return {"objective": "reach",
            "account_model": {"content_pillars": ["coding agents"],
                              "voice_profile": {}}}


# RETIRED (API-native pass, 2026-09-22): test_a_single_paragraph_reader_claim_is_still_removed: the scrubbers no longer rewrite drafts (the model writes in the writer's voice).


# RETIRED (API-native pass, 2026-09-22): test_a_single_paragraph_category_verdict_is_still_removed: same.


def test_the_grounded_sentence_is_kept():
    raw = [{"text": "Otlet runs Qwen35_4b inside Postgres so the data never "
                    "leaves the database. It's a new way to build apps."}]
    request = _request()
    result = _assemble_candidates(raw, [], "unavailable", request, [],
                                  get_personal_model(), _brief(),
                                  assess_context(request.context))
    assert result, "scrubbing must not empty the run"
    assert "Otlet" in result[0].text
