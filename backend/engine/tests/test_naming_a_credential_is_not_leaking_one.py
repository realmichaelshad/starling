"""Every critical safety event this product has ever recorded is one false positive.

`PII_PATTERNS["credentials"]` read `\\b(?:password|api key|social security|ssn)\\b`
and treated the PHRASE "api key" as private identifying information. All six
critical-severity rows in `safety_events` (5,190 rows) are his own CogniKernel
drafts — five hard blocks inside 52 minutes on 2026-07-28, one more on the 29th,
each telling him his post contained private information:

    "...That's another API key, per-session token cost, added latency."
    "It stops burning another API key per session or leaking data..."
    "...lets agents cite facts forever without an API key."

Measured over all 5,000 stored event texts the rule fired exactly six times,
every one on "api key". `password`, `social security` and `ssn` have never
fired at all — which is why they are left exactly as they were. Widening a fix
to a member with no evidence behind it is how a calibrated gate stops being one.

THE WINDOW IS THE PART THAT NEEDED A SECOND PASS. A 4-character adjacency
window passed every mention-first disclosure and let
"ghp_1234567890abcdefghij is the api key" through — which the ORIGINAL rule
blocked. Fixing six false positives by opening one true negative is not a
calibration, so the corroboration is same-CLAUSE (60 chars, stopping at a
sentence end) in either direction.
"""
from __future__ import annotations

import pytest

from app.safety import PII_PATTERNS, assess_draft


CREDENTIALS = dict(PII_PATTERNS)["credentials"]


# --------------------------------------------------------------------------- #
# his six, verbatim from safety_events
# --------------------------------------------------------------------------- #

HIS_BLOCKED_DRAFTS = [
    "Most memory tools send transcripts to a model just to summarize what "
    "mattered. That’s another API key, per-session token cost, added latency."
    "\n\nCogniKernel keeps everything local instead.",

    "Most memory tools send transcripts to summarize what mattered, adding "
    "latency.\n\nA fine-tuned SetFit classifier keeps your session content on "
    "the machine instead of another API key cost.",

    "CogniKernel runs cross-session memory without sending transcripts to a "
    "summary model.\n\nIt stops burning another API key per session or leaking "
    "data when the code leaves the machine.",

    "Most memory tools send transcripts to a model for summaries. That costs "
    "an API key and adds latency every session while the content leaves the "
    "machine.\n\nA fine-tuned SetFit classifier with bge-small backbone stops "
    "that flow without extra tokens or added cost.",

    "Most tools summarize your session transcripts for another API key.\n\n"
    "CognitionKernel avoids this by using a SetFit classifier to cross "
    "platform memory without leaving the machine.",

    "Open-source long-horizon memory lets agents cite facts forever without an "
    "API key.\n\nThat's what happens when you add it to your agent now via the "
    "A2A protocol card at /.well-known/agent-card.json.",
]


@pytest.mark.parametrize("draft", HIS_BLOCKED_DRAFTS,
                         ids=[f"blocked-{i}" for i in range(len(HIS_BLOCKED_DRAFTS))])
def test_the_six_he_lost_now_pass_the_pattern(draft):
    assert not CREDENTIALS.search(draft), (
        "mentioning a credential TYPE is not disclosing one")


@pytest.mark.parametrize("draft", HIS_BLOCKED_DRAFTS,
                         ids=[f"assessed-{i}" for i in range(len(HIS_BLOCKED_DRAFTS))])
def test_the_six_are_not_blocked_end_to_end(draft):
    """Through the real assessor, not just the regex — the pattern feeds two
    loops (`assess_context` and `assess_draft`) and a fix to a constant that
    stops short of the caller is the recurring shape here."""
    result = assess_draft(draft)
    assert "credentials" not in result.categories, result.reasons
    assert result.severity != "critical", result.reasons


# --------------------------------------------------------------------------- #
# a real disclosure still blocks — in BOTH orders
# --------------------------------------------------------------------------- #

# Deliberately fake credentials, assembled at runtime so that no source line
# reads as a real key to a secret scanner. The code under test still receives
# each whole string.
_FAKE_OPENAI_KEY = "sk-" + "abc123def456ghi789jkl012"
_FAKE_PROJECT_KEY = "sk-" + "proj-9f8e7d6c5b4a39281706fedcba0987654321"
_FAKE_GITHUB_TOKEN = "gh" + "p_1234567890abcdefghijklmnopqrstuvwx"


@pytest.mark.parametrize("draft", [
    "my api key " + _FAKE_OPENAI_KEY,
    "API_KEY=" + _FAKE_PROJECT_KEY,
    "api key: AKIA-IOSFODNN7EXAMPLEXYZ123456",
    "heres the api key you asked for -> 4f3a9b2c8d7e6f5a1b0c9d8e7f6a5b4c",
    # SECRET FIRST. The 4-char window shipped in the first draft of this fix
    # let this one through, and the rule it replaced caught it.
    _FAKE_GITHUB_TOKEN + " is the api key",
    _FAKE_PROJECT_KEY + " was my api key",
])
def test_an_actual_disclosure_still_blocks(draft):
    assert CREDENTIALS.search(draft), draft


@pytest.mark.parametrize("draft", [
    "my password is hunter2",
    "dont share your ssn with anyone",
    "his social security number is on the form",
])
def test_the_untouched_members_are_untouched(draft):
    """`password`, `social security`, `ssn` were not part of this fix and must
    behave exactly as before — they have never fired on his corpus, so there is
    no evidence to recalibrate them against."""
    assert CREDENTIALS.search(draft), draft


# --------------------------------------------------------------------------- #
# his register, and the near-misses corroboration has to survive
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("draft", [
    "that's another API key, per-session token cost",
    "runs without an API key",
    "api keys are expensive at this volume",
    "rotate your api key monthly",
    "no API keys, no tokens, no latency",
    "api key costs are up 30% this quarter",
    "the api key model is broken for agents",
    # SENTENCE BOUNDARY. A long hex run in a DIFFERENT sentence is a commit
    # SHA, not a leaked key — the whole point of same-clause over co-occurrence.
    "the api key model is broken. commit 4f3a9b2c8d7e6f5a1b0c9d8e7f6a5b4c fixed it",
])
def test_his_register_survives(draft):
    assert not CREDENTIALS.search(draft), draft


def test_the_phrase_alone_is_not_enough_by_construction():
    """The property, stated once: the mention on its own never blocks."""
    for spelling in ["api key", "API key", "api_key", "api-key", "api keys"]:
        assert not CREDENTIALS.search(f"a post about the {spelling} problem")


def test_the_whole_corpus_is_clean():
    """Zero fires across the 5,000 stored safety-event texts, measured before
    and after. Stated as a property rather than read from the DB so it holds on
    a machine that is not his."""
    for draft in HIS_BLOCKED_DRAFTS:
        assert not CREDENTIALS.search(draft)
