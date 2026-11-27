"""A confirmed preference has to CHANGE something.

`preferences.py` opens by naming two bugs that made it dead code. This is the
third, and it survived both fixes: the rules it produces and the rules the
gates look for have never been the same strings.

Say "stop asking rhetorical questions" and it stores "Avoid asking rhetorical
questions". The gate in `opportunities.py` searched for the literal substring
"avoid rhetorical question" — which that rule does not contain. Say "never use
hot take" and it stores "Avoid use hot take"; the gate wanted "avoid hot take".

So every confirmed preference was extracted, echoed back to him as recorded,
persisted — and then changed no decision, ever. A silent no-op is worse than a
missing feature, because he has been told it is on.

The rule text was also ungrammatical ("Avoid say delve", "Avoid use hot take"),
which matters for the same reason: it is shown to him for confirmation.
"""
from __future__ import annotations

import pytest

from app import opportunities
from app.opportunities import _preference_penalty
from app.preferences import propose_preference


@pytest.mark.parametrize("said,expected", [
    ("stop using em dashes", "Avoid using em dashes"),
    ("dont say delve", "Avoid saying delve"),
    ("never use hot take", "Avoid using hot take"),
    ("stop asking rhetorical questions", "Avoid asking rhetorical questions"),
    ("dont use the word leverage", "Avoid using the word leverage"),
])
def test_the_rule_reads_like_english(said: str, expected: str) -> None:
    proposal = propose_preference(said)
    assert proposal is not None, said
    assert proposal["rule"] == expected


@pytest.mark.parametrize("said,draft,floor", [
    ("stop asking rhetorical questions", "Is this the end of RAG?", 0.12),
    ("no rhetorical questions", "Is this the end of RAG?", 0.12),
    ("never use hot take", "hot take: RAG is dead", 0.18),
    ("dont use hot takes", "hot take: RAG is dead", 0.18),
])
def test_however_he_phrases_it_the_preference_bites(
        monkeypatch, said: str, draft: str, floor: float) -> None:
    proposal = propose_preference(said)
    assert proposal is not None, said
    monkeypatch.setattr(opportunities, "confirmed_preferences",
                        lambda: [{"rule": proposal["rule"]}])
    penalty, notes = _preference_penalty(draft)
    assert penalty >= floor, f"{proposal['rule']!r} changed nothing"
    assert notes


def test_a_positive_preference_is_not_read_as_a_prohibition(monkeypatch) -> None:
    """"Always ask a rhetorical question" must not penalise questions."""
    monkeypatch.setattr(opportunities, "confirmed_preferences",
                        lambda: [{"rule": "Always ask a rhetorical question"}])
    penalty, notes = _preference_penalty("Is this the end of RAG?")
    assert penalty == 0
    assert notes == []


@pytest.mark.parametrize("said,rule", [
    ("no rhetorical questions", "Avoid rhetorical questions"),
    ("dont say delve", "Avoid saying delve"),
    ("never use hashtags", "Avoid using hashtags"),
    ("always keep replies short", "Always keep replies short"),
])
def test_a_stored_rule_is_acknowledged_not_stored_in_silence(
        said: str, rule: str) -> None:
    """It was auto-confirmed at 0.78+ and the user was told nothing.

    "no rhetorical questions" got 90 seconds of unrelated model filler, so a
    rule that landed and a rule that was ignored looked identical.
    """
    from app.main import _preference_ack
    reply = _preference_ack(said)
    assert reply is not None, said
    assert rule in reply


@pytest.mark.parametrize("message", [
    "what should i post", "no i didnt", "hey",
    "how many followers do i have", "write me a post about opus 5",
])
def test_the_acknowledgement_does_not_eat_ordinary_messages(message: str) -> None:
    from app.main import _preference_ack
    assert _preference_ack(message) is None


# The sentence he actually typed, verbatim. Every pattern is matched with
# `re.search`, so the fragment "make it better" buried mid-question extracted a
# standing style rule, and this gate — which runs only after every account
# handler has declined — answered "Noted — 'Make it better.'" to a question
# about his account strategy.
HIS_QUESTION = ("what do you think of my account honestly like what should i "
                "do to make it better")


@pytest.mark.parametrize("message", [
    HIS_QUESTION,
    "what should i do to make it better",
    "how can i make my account better",
    "what do you think of my account",
    "how do i make it better?",
    "should i make it shorter",
    "is it better to keep posts short",
    "how do i make my posts punchier",
])
def test_a_question_is_never_read_as_a_standing_instruction(message: str) -> None:
    from app.main import _preference_ack
    assert propose_preference(message) is None, f"extractor took {message!r}"
    assert _preference_ack(message) is None, f"gate answered {message!r}"


@pytest.mark.parametrize("message,rule", [
    ("make it shorter", "Make it shorter"),
    ("no hashtags", "Avoid hashtags"),
    ("stop using em dashes", "Avoid using em dashes"),
    ("always keep replies short", "Always keep replies short"),
    ("dont say delve", "Avoid saying delve"),
    ("no rhetorical questions", "Avoid rhetorical questions"),
])
def test_the_guards_did_not_cost_us_the_real_instructions(
        message: str, rule: str) -> None:
    """The fix must not buy precision by losing the feature."""
    from app.main import _preference_ack
    proposal = propose_preference(message)
    assert proposal is not None and proposal["rule"] == rule, message
    assert _preference_ack(message) is not None, message


def test_an_unrelated_preference_changes_nothing(monkeypatch) -> None:
    monkeypatch.setattr(opportunities, "confirmed_preferences",
                        lambda: [{"rule": "Avoid using em dashes"}])
    penalty, _ = _preference_penalty("Is this the end of RAG?")
    assert penalty == 0
