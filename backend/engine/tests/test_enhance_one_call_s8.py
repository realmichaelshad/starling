"""S8: an enhance is one model call. The draft call names the writer's point first.

The point used to be read by its own call (`enhance_read.read_the_point`) before the rewrite
could start, about a second before the first token of every enhance. The draft call now names
his point before it rewrites (a POINT line on a stream, a "point" field in the JSON object),
and the same two checks judge it: it must be HIS point, and the rewrite must carry it, or the
composer keeps his words.
"""
from __future__ import annotations

import inspect

from app import llm as L
from app import main
from app.intelligence import _assemble_candidates, assess_context, get_personal_model
from app.schemas import DraftRequest, PostContext

HIS = ("Groq cut inference prices again this morning. Everyone on my timeline cheered the number "
       "on the slide. Nobody is pricing in the margin risk that follows.")
HIS_POINT = "Nobody is pricing in the margin risk from Groq's inference price cuts."


def _request(original=HIS, direction="Enhance this exact user-written post.") -> DraftRequest:
    return DraftRequest(action="post", research_mode="off", enhancement_mode=True, original_draft=original,
                        direction=direction,
                        context=PostContext(author="You", text=original, visibility="public", is_own_post=True))


def test_the_draft_call_is_asked_for_his_point_first():
    request = _request()
    assert L.draft_fields(request) == ("point", "text", "rationale")
    fmt = L.draft_response_format(L.draft_fields(request), L.draft_field_schemas(request))
    described = fmt["json_schema"]["schema"]["properties"]["point"]["description"]
    assert "writer's own draft" in described
    stream_prompt = L.draft_messages(request, [], {}, stream=True)[-1]["content"]
    assert "line starting POINT:" in stream_prompt and "the writer's own draft makes" in stream_prompt
    json_prompt = L.draft_messages(request, [], {}, stream=False, fields=L.draft_fields(request))[-1]["content"]
    assert "the writer's own draft makes" in json_prompt


def test_a_short_draft_or_a_decided_point_asks_for_nothing():
    assert L.draft_fields(_request(original="too short to bury")) == ("text", "rationale")
    decided = _request(direction='THE POINT OF THIS POST, decided already: "x y z w"')
    assert L.draft_fields(decided) == ("text", "rationale")


def test_the_separate_read_is_gone_from_the_enhance_path():
    source = inspect.getsource(main)
    assert "read_the_point(llm" not in source, "an enhance must not spend its own call reading the point"


def _assemble(text: str, point: str | None):
    request = _request()
    raw = {"text": text, "rationale": "r"}
    if point is not None:
        raw["point"] = point
    got = _assemble_candidates([raw], [], "not_needed", request, [], get_personal_model(),
                               {"objective": "conversation", "timing": {}, "recent_angles_to_avoid_repeating": []},
                               assess_context(request.context))
    return got[0].text if got else None


def test_a_rewrite_that_drops_his_point_is_rejected_so_his_words_stay():
    drops = "Groq shipped something this morning and the timeline noticed."
    assert _assemble(drops, HIS_POINT) is None


def test_a_rewrite_that_carries_his_point_is_kept():
    keeps = ("Nobody is pricing in the margin risk from Groq's inference price cuts. "
             "Everyone just cheered the number.")
    assert _assemble(keeps, HIS_POINT) == keeps


def test_a_point_that_is_not_his_is_ignored_not_trusted():
    """A point sharing no vocabulary with his draft is the model writing its own post: it
    decides nothing (the rewrite's other gates still judge it)."""
    keeps = ("Nobody is pricing in the margin risk from Groq's inference price cuts. "
             "Everyone just cheered the number.")
    assert _assemble(keeps, "Quantum computing will change everything about cryptography soon.") == keeps
