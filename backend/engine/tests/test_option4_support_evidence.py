"""OPTION-4 P3a — the support checker reads the field, and CANNOT read the image.

The ZARQUON-7 shape: a unique sentinel in the image note must never be accepted
as backing evidence, while the captured facts must be. This is the behavioural
companion to the type guarantee — it survives a refactor that satisfies the type
alone, which is the failure mode the scaffold-strip was supposed to prevent and
never did on this path.
"""
from app.intelligence import _support_evidence
from app.schemas import DraftRequest, PostContext, SupportEvidence

SENTINEL = "a chart labelled ZARQUON-7"


def _req(**kw):
    base = dict(action="reply", context=PostContext(author="a", text="x" * 40))
    base.update(kw)
    return DraftRequest(**base)


def test_the_captured_facts_are_the_evidence():
    assert _support_evidence(_req(facts="Together AI shipped it on Tuesday.")) == \
        "Together AI shipped it on Tuesday."


def test_the_image_note_is_never_evidence():
    """Model output may not vouch for a claim — the whole point of the type."""
    request = _req(facts="Together AI shipped it on Tuesday.", image_note=SENTINEL)
    evidence = _support_evidence(request)
    assert SENTINEL not in evidence, "vision output reached the support checker"
    assert "Together AI" in evidence


def test_the_narrow_view_cannot_even_hold_it():
    view = SupportEvidence.of(_req(facts="f", image_note=SENTINEL))
    assert not hasattr(view, "image_note")
    assert view.facts == "f"


def test_a_legacy_row_still_parses_the_string():
    """None is UNKNOWN, not empty: pre-P2 rows must behave exactly as before."""
    legacy = _req(direction=" USE THESE FACTS, and only these: the old way.")
    assert legacy.facts is None
    assert "the old way" in _support_evidence(legacy)


def test_an_empty_field_is_not_a_missing_field():
    """A producer that ran and found nothing says so, and is not re-parsed."""
    assert _support_evidence(_req(facts="")) == ""
