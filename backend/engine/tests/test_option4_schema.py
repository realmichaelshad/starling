"""OPTION-4 P1 — the schema, and the property a test cannot enforce alone.

P1 is behaviour-neutral: nothing reads or writes these fields yet. What is
asserted here is that the SHAPE is right before anything depends on it.
"""
import pytest
from pydantic import ValidationError

from app.schemas import DraftRequest, SupportEvidence


def _minimal(**kw):
    from app.schemas import PostContext
    base = dict(action="reply", context=PostContext(author="a", text="x" * 40))
    base.update(kw)
    return DraftRequest(**base)


def test_new_fields_default_to_none_meaning_unknown():
    """A pre-migration row parses, and absence reads as UNKNOWN, never as empty."""
    request = _minimal()
    for name in ("facts", "decided_point", "writing_brief", "shape_note", "image_note"):
        assert getattr(request, name) is None, f"{name} must default to None, not ''"
    assert request.facts_truncated is False


def test_a_stored_pre_migration_payload_still_loads():
    """The 2,225 existing generations rows must not become unreadable."""
    from app.schemas import PostContext
    stored = {"action": "reply",
              "context": PostContext(author="a", text="x" * 40).model_dump(),
              "direction": "USE THESE FACTS, and only these: something"}
    request = DraftRequest(**stored)
    assert request.facts is None          # unknown, NOT empty
    assert request.direction.startswith("USE THESE FACTS")


def test_facts_cap_is_1200_and_clears_every_measured_row():
    """MAX observed is 987 across 1,351 stored directions; 1200 cuts none of them."""
    assert _minimal(facts="f" * 1200).facts == "f" * 1200
    with pytest.raises(ValidationError):
        _minimal(facts="f" * 1201)


def test_the_fields_do_not_compete():
    """The defect, as a regression test: a full facts and a full brief coexist."""
    request = _minimal(facts="f" * 1200, writing_brief="b" * 1200,
                       decided_point="p" * 280, shape_note="s" * 300,
                       image_note="i" * 500)
    assert len(request.facts) == 1200
    assert len(request.writing_brief) == 1200
    assert len(request.decided_point) == 280


def test_support_evidence_cannot_carry_the_image_note():
    """The safety property, as a TYPE — not a convention a future edit can ignore.

    A hallucinated vision reading must never be able to vouch for a claim.
    """
    assert "image_note" not in SupportEvidence.model_fields
    with pytest.raises(ValidationError):
        SupportEvidence(facts="real", image_note="a vision hallucination")


def test_support_evidence_of_copies_only_the_facts():
    request = _minimal(facts="captured", image_note="model output")
    view = SupportEvidence.of(request)
    assert view.facts == "captured"
    assert not hasattr(view, "image_note")


def test_direction_is_unchanged_at_1200():
    """P1 must not move the existing cap; that is P5's job, not this patch's."""
    assert _minimal(direction="d" * 1200).direction == "d" * 1200
    with pytest.raises(ValidationError):
        _minimal(direction="d" * 1201)
