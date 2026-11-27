"""The three fabrication shapes this account actually produces.

All three were generated live on 2026-07-26 and shipped past every gate:

    "Claude just dropped a 50K context window update"    invented capacity
    "launching just 6 weeks after GPT-4.5"               invented interval
    "Opus 5 scores 88.8 on the coding benchmark"         invented score

The claim gate covered %, x, $, GB/ms/GHz, dates and benchmark NAMES. It did
not cover token capacities, time intervals, or a bare decimal — and a bare
decimal is the single most common shape, because the code's own comment calls
a precise one-decimal statistic the signature of a fabrication.

The boundary that matters more than any of them: A MODEL VERSION IS NOT A
STATISTIC. "Opus 4.5", "GPT-5.6" and "Grok 4.5" appear in nearly every post he
writes and are almost never in the evidence corpus. Flagging those would block
the whole account.
"""
from __future__ import annotations

import pytest

from app.intelligence import _unsupported_claims

EVIDENCE = "Local models are cheaper than cloud pricing for small teams."


@pytest.mark.parametrize("text,shape", [
    ("Claude just dropped a 50K context window update.", "capacity"),
    ("It handles a 128k context window now.", "capacity"),
    ("Launching just 6 weeks after GPT-4.5.", "interval"),
    ("It shipped 3 days early.", "interval"),
    ("Opus 5 scores 88.8 on the coding benchmark.", "bare score"),
])
def test_invented_figures_are_caught(text: str, shape: str) -> None:
    assert _unsupported_claims(text, EVIDENCE), f"{shape} slipped through: {text}"


@pytest.mark.parametrize("text", [
    "GPT-5.6 beats Fable 5.",
    "Grok 4.5 is now live for Heavy users.",
    "Sonnet 5 matches Opus 4.8 on benchmarks.",
])
def test_a_real_model_version_passes_on_unrelated_evidence(text: str) -> None:
    """A version is a NAME, not a statistic — so it is checked as a name.

    It never has to appear in the evidence: it passes if it is attested
    anywhere real (his published archive, the radar, the release feed).
    Comparing models across labs is this account's entire beat and must not
    require a citation.
    """
    assert not _unsupported_claims(text, EVIDENCE), text


@pytest.mark.parametrize("text", [
    "Grok 2.0 is out and no one's talking about it.",
    "Grok-2.5 is live with no benchmarks.",
    "Grok-1 still beats it on cost.",
])
def test_an_invented_model_version_is_caught(text: str) -> None:
    """The regression that removing entity names from the style examples caused.

    With no names left to copy, the model started inventing version numbers —
    "Grok 2.0", "Grok-1", "Grok-2.5" in one run, when the real one is 4.5. A
    wrong version is the same class of error as naming the wrong lab: instantly
    visible to exactly the readers he wants.

    The evidence here is the real one from that run — it names Grok 4.5, so the
    draft naming 2.0 contradicts what it was handed.
    """
    grounded = ("xAI unveils Grok 4.5 as top efficiency leader. "
                "Grok 4.5 is now live for Heavy users.")
    assert _unsupported_claims(text, grounded), f"invented version shipped: {text}"


def test_a_family_with_no_reference_frame_is_left_alone(monkeypatch) -> None:
    """Comparing across labs from knowledge is the account's whole beat.

    "Sonnet 5 matches Opus 4.8" must not need a citation for Opus merely
    because the evidence happened to be about Sonnet. The gate fires on a
    family it has a reference for — the evidence names it, or something real
    attests to other versions of it — and stays quiet otherwise.
    """
    import app.intelligence as intel
    monkeypatch.setattr(intel, "known_model_versions", lambda: {"sonnet 5"})
    assert not _unsupported_claims("Sonnet 5 matches Opus 4.8 now.", "Sonnet 5 shipped.")
    # Same family, different number, present in the evidence: that IS checkable.
    assert _unsupported_claims("Sonnet 3 matches it now.", "Sonnet 5 shipped.")


def test_versions_degrade_silently_with_no_ground_truth(monkeypatch) -> None:
    """A fresh install has no archive and no radar: flag nothing rather than all."""
    import app.intelligence as intel
    monkeypatch.setattr(intel, "known_model_versions", lambda: set())
    assert not _unsupported_claims("Grok 2.0 is out.", "")


def test_a_figure_present_in_the_evidence_is_supported():
    """The gate blocks INVENTION, not specificity."""
    assert not _unsupported_claims("It handles a 128k context window.",
                                   "The model supports a 128k context window.")


def test_his_own_posts_carry_no_unsupported_claims_against_themselves():
    from app.storage import performance_records
    rows = [str(r.get("text") or "") for r in performance_records() if r.get("text")]
    if not rows:
        pytest.skip("no archive in this environment")
    flagged = [t for t in rows if _unsupported_claims(t, t)]
    assert not flagged, f"{len(flagged)} of his own posts flagged against themselves"
