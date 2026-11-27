"""The two mechanical defect classes from the 23:46 batch (2026-07-27).

1. "NoBg makes training it easy." — FeyNoBg torn mid-word; the entity gate
   vouches for substrings so the tear passed. Repaired, not flagged.
2. "MAI-Cyber-1-Flash outperforms competitors on benchmarks" — the support
   check flagged it and shipped the assertion anyway. The writer's own good
   form from the same evening ("claims to outperform") is a deterministic
   edit, so the checker now makes it.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.intelligence import repair_name_fragments          # noqa: E402
from app.support_check import _hedge_assertion              # noqa: E402


EVIDENCE = ("FeyNoBg – Automatic background removal model and training "
            "library. Across eight benchmarks it posts the best S-measure. "
            "This grew the model from 222M parameters to 263M.")


def test_a_midword_fragment_becomes_the_full_name():
    import re
    out = repair_name_fragments(
        "FeyNoBg lets you remove backgrounds in one click. NoBg makes "
        "training it easy.", EVIDENCE)
    assert re.search(r"\bNoBg\b", out) is None
    assert "FeyNoBg makes training it easy." in out


def test_a_bare_family_name_beside_a_versioned_one_is_untouched():
    """An archive post with "GPT-5.6" and "ChatGPT" in it made bare "GPT" look
    like a fragment of ChatGPT, and the first version of this repair rewrote
    the account's own post to "ChatGPT-5.6". A dot/hyphen segment of a corpus
    word is a normal reference, not a tear."""
    text = "Switching to GPT-5.6 today. GPT is winning. ChatGPT too."
    assert repair_name_fragments(text, text) == text


def test_a_prefix_shortening_is_deliberate_and_kept():
    out = repair_name_fragments(
        "Opus handles the whole repo in one pass.",
        "Opus5 benchmark results published today.")
    assert out == "Opus handles the whole repo in one pass."


def test_two_possible_hosts_is_ambiguous_and_untouched():
    out = repair_name_fragments(
        "Coder is the one to watch.",
        "NousCoder and DeepCoder shipped the same week.")
    assert out == "Coder is the one to watch."


def test_whole_word_names_are_never_rewritten():
    text = "FeyNoBg grew from 222M to 263M parameters."
    assert repair_name_fragments(text, EVIDENCE) == text


def test_the_assertion_is_hedged_into_an_attribution():
    claim = "Microsoft's MAI-Cyber-1-Flash outperforms competitors on benchmarks"
    assert _hedge_assertion(claim) == \
        "Microsoft's MAI-Cyber-1-Flash claims to outperform competitors on benchmarks"


def test_an_already_hedged_claim_is_left_alone():
    assert _hedge_assertion("KBlip claims to outperform GPT-4o") is None
    assert _hedge_assertion("The paper says it beats the baseline") is None


def test_plural_subjects_are_not_touched():
    """"Open models outperform closed ones" would need verb agreement the
    transform does not attempt — note-only path."""
    assert _hedge_assertion("Open models outperform closed ones") is None


def test_an_unbackable_tail_is_cut_clean(monkeypatch):
    """With one claim checked (the last checkable sentence), an unbackable
    TAIL is cut and the named lead ships with no note."""
    from app import support_check as S
    monkeypatch.setattr(S, "_find_support", lambda claim, ev, r: False)
    text = ("SOTA BEAM with 10M tokens means small models finally matter to "
            "builders. Exabase reports half of long-context performance "
            "degrades without explicit memory design.")
    out, note = S.apply_support_check(text, "x" * 80, type(
        "O", (), {"router": type("R", (), {"chat": lambda *a, **k: "NONE"})()})())
    assert out == ("SOTA BEAM with 10M tokens means small models finally "
                   "matter to builders.")
    assert note is None


def test_a_cut_that_would_behead_the_draft_keeps_the_note(monkeypatch):
    """First arena run: cutting "Mistral OCR4 trails DharmaOCR…" shipped the
    naked tail "The gap is unchanged across three test sets." A remainder
    that names nothing keeps the flagged lead and the note instead."""
    from app import support_check as S
    monkeypatch.setattr(S, "_find_support", lambda claim, ev, r: False)
    text = ("Mistral OCR4 trails DharmaOCR by thirteen points on extraction. "
            "The gap is unchanged across three test sets.")
    out, note = S.apply_support_check(text, "x" * 80, type(
        "O", (), {"router": type("R", (), {"chat": lambda *a, **k: "NONE"})()})())
    assert out == text
    assert note and "can't back" in note


def test_a_single_sentence_draft_keeps_the_note(monkeypatch):
    """Nothing to cut — the note names the weakness and the draft ships."""
    from app import support_check as S
    monkeypatch.setattr(S, "_find_support", lambda claim, ev, r: False)
    text = "CogniKernel keeps your code in a fine-tuned SetFit classifier."
    out, note = S.apply_support_check(text, "x" * 80, type(
        "O", (), {"router": type("R", (), {"chat": lambda *a, **k: "NONE"})()})())
    assert out == text
    assert note and "can't back" in note
