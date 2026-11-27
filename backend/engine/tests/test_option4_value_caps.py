"""The structured payloads can never be the reason a draft fails.

Found while recording the product demo (2026-09-18): pressing the reply button
under a post that asks a question answered "Starling's AI isn't responding".
Nothing was wrong with the AI. `plan_reply` had produced 1,300 characters,
`DraftRequest.writing_brief` allowed 1,200, and pydantic raised while the
request was being built, so the engine answered 500. The same happened to any
reply whose planner accepted a point, because the point is appended to a plan
that is already 1,094 characters long.

Two halves are pinned here. The cap is sized from the producer, so a real plan
fits whole. And `direction.value_copies` clips every payload to the cap its
field declares, so whatever a producer does next, the constructor cannot raise.
"""
from __future__ import annotations

import pytest

from app.direction import value_copies
from app.reply_plan import _POINT_LEAD, plan_reply
from app.schemas import WRITING_BRIEF_CAP, DraftRequest, PostContext


class _Router:
    def __init__(self, text: str):
        self.text = text

    def chat(self, messages, options=None, think=False):
        return self.text


class _Model:
    """The planner only ever asks its model for one sentence."""

    def __init__(self, text: str):
        self.router = _Router(text)


def ctx(text: str, **kwargs) -> PostContext:
    base = {"author": "Dana Okafor", "author_handle": "dana_demo", "text": text}
    base.update(kwargs)
    return PostContext(**base)


def request_with(context: PostContext, **values) -> DraftRequest:
    return DraftRequest(action="reply", context=context, research_mode="off",
                        media_policy="no_media", grok_permission=False,
                        length="medium", direction="x", **values)


STATEMENT = ("The hardest part of shipping an AI feature is not the model. It is "
             "deciding what the thing should refuse to do.")
QUESTION = ("How do you decide what your AI feature should refuse to do before "
            "you ship it to customers?")
RECENT = [f"Opening number {index} that starts a reply in a recognisable way, "
          "then goes on for a while." for index in range(12)]

POINTS = {
    STATEMENT: ("Deciding the refusals first turns the prompt into plain wording "
                "instead of a negotiation with the model"),
    QUESTION: ("Write the refusals as tests first so shipping means passing them "
               "rather than arguing about the prompt afterwards"),
}


@pytest.mark.parametrize("post", [STATEMENT, QUESTION])
@pytest.mark.parametrize("recent", [[], RECENT], ids=["no-history", "twelve-recent-replies"])
def test_a_real_reply_plan_with_a_decided_point_builds_a_request(post, recent):
    """The defect, as a regression test: this raised a ValidationError."""
    context = ctx(post)
    plan, room = plan_reply(context, llm=_Model(POINTS[post]), recent_replies=recent)
    assert room.decided_point, "the planner has to accept the point for this to mean anything"
    assert _POINT_LEAD in plan
    assert len(plan) > 1200, "shorter than the old cap: this no longer tests the overflow"
    assert len(plan) <= WRITING_BRIEF_CAP

    request = request_with(context, **value_copies(plan=plan, point=room.decided_point))

    assert request.writing_brief == plan, "a real plan fits whole, with its point"
    assert request.decided_point == room.decided_point


def test_a_question_overflowed_the_old_cap_before_any_point_was_added():
    """Why every reply to a question failed, not only the planned ones."""
    plan, room = plan_reply(ctx(QUESTION), llm=None, recent_replies=[])
    assert room.decided_point == ""
    assert len(plan) > 1200
    assert request_with(ctx(QUESTION), **value_copies(plan=plan)).writing_brief == plan


def test_every_payload_is_clipped_to_its_own_cap_instead_of_raising():
    values = value_copies(evidence="f" * 5000, point="p" * 900, plan="b" * 10_000,
                          shape="s" * 900, image="i" * 900)

    request = request_with(ctx(STATEMENT), **values)

    assert len(request.facts) == 1200
    assert request.facts_truncated is True, "a cut fact block has to admit it"
    assert len(request.decided_point) == 280
    assert len(request.writing_brief) == WRITING_BRIEF_CAP
    assert len(request.shape_note) == 300
    assert len(request.image_note) == 500


def test_values_inside_their_caps_pass_through_untouched():
    values = value_copies(evidence="a checked fact", point="the point",
                          plan="the plan", shape="two short blocks", image="a chart")

    assert values == {"facts": "a checked fact", "facts_truncated": False,
                      "decided_point": "the point", "writing_brief": "the plan",
                      "shape_note": "two short blocks", "image_note": "a chart"}


def test_absent_stays_unknown_rather_than_empty():
    values = value_copies()

    assert all(values[name] is None for name in
               ("facts", "decided_point", "writing_brief", "shape_note", "image_note"))
    assert values["facts_truncated"] is False
    request = request_with(ctx(STATEMENT), **values)
    assert request.writing_brief is None and request.facts is None


def test_the_caps_are_read_from_the_schema_not_repeated():
    """One number per field, in one place: a changed cap cannot drift from its clip."""
    from app.direction import _value_cap

    assert _value_cap("writing_brief") == WRITING_BRIEF_CAP
    assert _value_cap("facts") == 1200
    with pytest.raises(LookupError):
        _value_cap("action")
