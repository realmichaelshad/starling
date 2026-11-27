"""The model fetches, the harness judges — the support check on any machine.

Probed live before wiring (`tools/support_check_probe.py`): 10/11 on labelled
claims from real drafts, zero lazy NONEs, ~10s per claim warm. These tests
cover the harness half with a scripted router, because the harness is where
the guarantees live: a fabricated quote CANNOT pass the substring check, an
unsupported trailing sentence is cut rather than shipped, an unsupported lead
ships flagged rather than silently, and his opinions are never sent for
verification at all.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.support_check import apply_support_check, _checkable   # noqa: E402

EVIDENCE = ("A curated index of open-source AI developer tools is live, listing "
            "43 entries including Claude Code, local inference stacks and coding "
            "agents. The index is provider-agnostic and community maintained.")


class _Router:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def chat(self, messages, *, options, fmt=None, think=False, lane=None):
        self.calls += 1
        return self.replies.pop(0) if self.replies else "NONE"


class _Model:
    def __init__(self, replies):
        self.router = _Router(replies)


def test_an_unsupported_trailing_sentence_is_cut():
    """The dominant residual shape: grounded lead, invented tail."""
    text = ("A curated index of open-source AI tools now lists 43 entries. "
            "It replaces Claude Code.")
    llm = _Model(["NONE"])
    out, note = apply_support_check(text, EVIDENCE, llm)
    assert out == "A curated index of open-source AI tools now lists 43 entries."
    assert note is None


def test_an_unsupported_lead_ships_flagged_never_silent():
    text = "The index replaces Claude Code."
    llm = _Model(["NONE"])
    out, note = apply_support_check(text, EVIDENCE, llm)
    assert out == text, "the draft itself is never blanked"
    assert note and "can't back" in note and "replaces Claude Code" in note


def test_a_fabricated_quote_cannot_vouch():
    """The structural guarantee: the model bluffs, the substring check wins."""
    text = ("A curated index of open-source AI tools now lists 43 entries. "
            "It replaces Claude Code.")
    llm = _Model(["The index fully replaces Claude Code for most developers."])
    out, _ = apply_support_check(text, EVIDENCE, llm)
    assert "replaces Claude Code" not in out


def test_a_real_quote_supports_and_nothing_changes():
    text = "The curated index is provider-agnostic and lists 43 entries."
    llm = _Model(["The index is provider-agnostic and community maintained."])
    out, note = apply_support_check(text, EVIDENCE, llm)
    assert out == text and note is None


def test_his_opinions_are_never_sent_for_verification():
    assert not _checkable("I'd take Fable 5 over every GPT model right now.")
    assert not _checkable("My prediction: July 30.")
    assert not _checkable("Maybe it's time to rethink how we value intelligence.")
    assert _checkable("The index replaces Claude Code.")
    assert _checkable("Sonnet cost $79 for 417 tasks.")


def test_thin_evidence_means_no_check_at_all():
    llm = _Model(["NONE"])
    out, note = apply_support_check("It replaces Claude Code.", "too short", llm)
    assert out == "It replaces Claude Code." and note is None
    assert llm.router.calls == 0


def test_a_supported_runner_up_outranks_an_unsupported_winner():
    """Flag-shipping is honest but the reader still sees the unsupported
    sentence. When a second gate-clean candidate exists and checks out, it
    wins; the flagged one rides behind with its note attached."""
    from app.intelligence import _support_checked
    from app.schemas import DraftCandidate, DraftRequest, PostContext

    request = DraftRequest(
        action="post",
        direction=f"Write about the index. USE THESE FACTS, and only these: {EVIDENCE}",
        context=PostContext(author="a", text="", visibility="public"))
    best = [
        DraftCandidate(id="a", text="The index replaces Claude Code.", performance={}),
        DraftCandidate(id="b", text="The curated index is provider-agnostic and "
                                    "lists 43 entries.", performance={}),
    ]
    # Scripted router: claim A unsupported (NONE), claim B supported (real quote).
    llm = _Model(["NONE",
                      "The index is provider-agnostic and community maintained."])
    out = _support_checked(best, request, llm)
    assert out[0].id == "b", "the supported runner-up should ship first"
    assert out[1].id == "a" and out[1].needs_review
    assert "can't back" in (out[1].review_note or "")


def test_a_router_failure_returns_the_draft_untouched():
    class _Boom:
        def chat(self, *a, **k):
            raise RuntimeError("model down")

    class _O:
        router = _Boom()

    text = "The index replaces Claude Code."
    out, note = apply_support_check(text, EVIDENCE, _O())
    assert out == text and note is None


# --- the check has to be visible, not just correct ------------------------- #

def _recorded(monkeypatch):
    """Every safety event this module writes, as reason strings."""
    seen: list[str] = []
    from app import storage
    monkeypatch.setattr(storage, "save_safety_event",
                        lambda category, severity, blocked, reasons, context=None:
                        seen.extend(reasons))
    return seen


def test_a_caught_claim_is_recorded_not_only_repaired(monkeypatch):
    """Skips were recorded and CATCHES were not, so the safety log could answer
    "did this decline to run" and could not answer "is this doing anything" —
    which is the question that decides whether ~10s per claim is worth paying.
    On his machine the only support_check rows were six skips; a fabrication
    caught and cut left no trace at all."""
    seen = _recorded(monkeypatch)
    text = ("A curated index of open-source AI tools now lists 43 entries. "
            "It replaces Claude Code.")
    out, _note = apply_support_check(text, EVIDENCE, _Model(["NONE"]))
    assert out == "A curated index of open-source AI tools now lists 43 entries."
    assert any(r.startswith("support_check_caught:cut_tail") for r in seen), seen


def test_a_flagged_claim_is_recorded_too(monkeypatch):
    seen = _recorded(monkeypatch)
    apply_support_check("The index replaces Claude Code.", EVIDENCE, _Model(["NONE"]))
    assert any(r.startswith("support_check_caught:") for r in seen), seen


def test_the_two_paths_that_used_to_return_in_silence_now_say_so(monkeypatch):
    """`_record_skip`'s own docstring is the rule: "a verification that silently
    does not run is worse than none at all." Only budget and verifier-down were
    recorded, so the two commonest reasons for not running were invisible."""
    seen = _recorded(monkeypatch)
    apply_support_check("Groq cut prices. Nobody prices the margin risk.", "tiny",
                        _Model(["NONE"]))
    assert seen == ["support_check_skipped:thin_evidence"], seen

    seen2 = _recorded(monkeypatch)
    apply_support_check("Groq cut prices. Nobody prices the margin risk.",
                        EVIDENCE, object())          # no .router at all
    assert seen2 == ["support_check_skipped:no_router"], seen2


def test_a_supported_draft_records_nothing(monkeypatch):
    """Silence is correct HERE — there is no event to report when the claim
    checked out, and logging every clean pass would drown the ones that matter."""
    seen = _recorded(monkeypatch)
    text = "A curated index of open-source AI developer tools is live."
    out, note = apply_support_check(text, EVIDENCE, _Model([
        "A curated index of open-source AI developer tools is live"]))
    assert out == text and note is None
    assert seen == [], seen


# S6 (2026-09-22): both claims in ONE call; the harness still judges each quote alone.

TWO_CLAIMS = ("A curated index of open-source AI tools now lists 43 entries. "
              "It replaces Claude Code.")


def test_two_claims_cost_one_call_and_each_quote_is_judged_alone():
    # Claims go last-first: 1 is "It replaces Claude Code.", 2 is the 43-entries lead.
    llm = _Model(["1: The index fully replaces Claude Code for most developers.\n"
                  "2: A curated index of open-source AI developer tools is live, listing 43 entries "
                  "including Claude Code, local inference stacks and coding agents."])
    out, note = apply_support_check(TWO_CLAIMS, EVIDENCE, llm)
    assert llm.router.calls == 1, "one call for both claims"
    assert out == "A curated index of open-source AI tools now lists 43 entries.", "the bluffed tail is cut"
    assert note is None


def test_one_bare_none_answers_every_claim():
    llm = _Model(["NONE"])
    out, _ = apply_support_check(TWO_CLAIMS, EVIDENCE, llm)
    assert llm.router.calls == 1
    assert "replaces Claude Code" not in out


def test_a_claim_the_answer_skipped_is_asked_about_alone_never_let_through():
    llm = _Model(["2: A curated index of open-source AI developer tools is live, listing 43 entries "
                  "including Claude Code, local inference stacks and coding agents.",
                  "NONE"])
    out, _ = apply_support_check(TWO_CLAIMS, EVIDENCE, llm)
    assert llm.router.calls == 2, "the skipped claim got its own call"
    assert "replaces Claude Code" not in out


def test_the_verifier_runs_on_the_fast_lane():
    """S9 (2026-09-22): fetching a quote is extraction, not writing."""
    lanes = []

    class _LaneRouter(_Router):
        def chat(self, messages, *, options, fmt=None, think=False, lane=None):
            lanes.append(lane)
            return super().chat(messages, options=options, fmt=fmt, think=think, lane=lane)

    class _LaneModel:
        router = _LaneRouter(["NONE"])

    apply_support_check(TWO_CLAIMS, EVIDENCE, _LaneModel())
    assert lanes and set(lanes) == {"fast"}
