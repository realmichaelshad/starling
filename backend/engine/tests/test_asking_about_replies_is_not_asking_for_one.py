""""explain why my replies do better than my posts" returned a drafting tutorial.

`_REPLY_INTENT` matches `repl(y|ies|ying)` — a NOUN as often as a verb — and the
comment above it says "verbs and idioms only". Across every distinct message he
has ever typed, the pattern claims 17; **exactly one is a draft request**
("reply to this") and the other sixteen ask about his replies.

CONSEQUENCE BEFORE TRUTH. Eleven of the sixteen are already rescued by
`_facts_question_answer`, which runs before `_wants_draft` — "whats my best
reply", "how many replies did i send in july". So the live damage is TWO
messages, not sixteen, and both are the same shape:

    "Why did my last three replies underperform?"
    "explain why my replies do better than my posts"        (id 2081)

The second is the best analytical question in his corpus. The product has the
answer — `performance_insights().by_format` splits reply-vs-post medians — and
nothing asked it, because the question never got past the word "replies".

THE SECOND HALF, WHICH IS THE ONE THAT MATTERS. De-claiming alone does not
produce an answer; it produces a fall-through to the model. `wants_diagnosis`
does not claim these either (its pattern needs a "well/bad/poorly" word;
"better" and "underperform" are not in it), and it lives in a file this lane
does not edit. So the model answers — and `account_facts` carried a single
global `median_views` and NO per-format split, meaning the comparison he asked
about did not exist in the facts and the only way to answer was to invent it.
*A missing aggregate becomes a fabricated one.* `median_views_by_format` closes
that, with the sample size beside every median.
"""
from __future__ import annotations

import pytest

import app.main as main
from app.account_facts import account_facts
from app.main import _ASKS_ABOUT_OWN_PERFORMANCE, _wants_draft


HIS_QUESTION = "explain why my replies do better than my posts"


# --------------------------------------------------------------------------- #
# de-claimed
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("message", [
    HIS_QUESTION,
    "Explain in your own words why my replies do better than my posts.",
    "Why did my last three replies underperform?",
    "why do my quotes beat my posts",
    "explain why my posts perform worse than my replies",
])
def test_a_question_about_his_replies_is_not_a_draft_request(message):
    assert not _wants_draft(message, has_context=False), message
    assert not _wants_draft(message, has_context=True), message


# --------------------------------------------------------------------------- #
# and nothing else moved — the must-not-break set
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("message", [
    "reply to this",                       # id 2211, the acceptance control
    "respond to this",
    "clap back at him",
    "hit back",
    "quote this",
    "make a reply",
    "cna u reply",
    # An ask VERB in front still drafts, even carrying every other trigger word.
    "write a post explaining why my replies do better than my posts",
    "draft a reply about why my posts underperform",
])
def test_a_real_draft_request_still_drafts(message):
    assert _wants_draft(message, has_context=True), message


def test_the_guard_needs_all_three_conditions():
    """Narrow on purpose: an explanatory opener, a possessive, and a
    comparative performance frame. Any two of the three is not enough."""
    assert not _ASKS_ABOUT_OWN_PERFORMANCE.search("explain my replies")          # no frame
    assert not _ASKS_ABOUT_OWN_PERFORMANCE.search("explain why replies do better")  # no possessive
    assert not _ASKS_ABOUT_OWN_PERFORMANCE.search("my replies do better than my posts")  # no opener
    assert _ASKS_ABOUT_OWN_PERFORMANCE.search(HIS_QUESTION)


def test_the_eleven_already_rescued_are_untouched():
    """These never reached the drafting path — `_facts_question_answer` answers
    them first. Pinned so a later change to that layer cannot quietly hand them
    to the guard, or to the drafter."""
    from app.strategy import _facts_question_answer

    for message in ["whats my best reply", "whats my worst reply",
                    "how many replies did i send in july", "rate my last reply",
                    "what was my latest reply", "hows my latest reply"]:
        assert _facts_question_answer(message), message


# --------------------------------------------------------------------------- #
# the fact he needs is now IN the facts — de-claiming without this just moves
# the wrong answer from the drafter to the model
# --------------------------------------------------------------------------- #

# The suite runs on a seeded test database with no measured view counts, so
# `by_format` is legitimately empty there and the field is correctly ABSENT.
# These stub the one upstream call rather than assert against his live numbers —
# which are recorded in the docstring and re-measurable, not testable on another
# machine.

HIS_LIVE_SHAPE = {
    "strongest_format": "reply",
    "by_format": {
        "reply": {"posts": 7, "measured_views": 7, "median_views": 488.0,
                  "evidence": "very limited"},
        "post": {"posts": 95, "measured_views": 55, "median_views": 226.0,
                 "evidence": "solid"},
        "quote": {"posts": 0, "measured_views": 0, "evidence": "very limited"},
    },
}


@pytest.fixture
def his_numbers(monkeypatch):
    """His live shape as of 2026-08-09, measured from `performance_insights()`."""
    import app.performance
    monkeypatch.setattr(app.performance, "performance_insights",
                        lambda *a, **k: HIS_LIVE_SHAPE)


def test_the_reply_vs_post_comparison_exists_in_the_facts(his_numbers):
    from app.account_facts import _format_medians

    by_format = _format_medians().get("median_views_by_format")
    assert by_format, "the model has no basis for the comparison he asked about"
    assert by_format["reply"]["median_views"] == 488
    assert by_format["post"]["median_views"] == 226


def test_every_median_carries_its_sample(his_numbers):
    """"Your replies do better — a median of 588 views" was once stated off SIX
    replies as a strategy recommendation. A median without its sample is how
    that happens."""
    from app.account_facts import _format_medians

    for name, row in _format_medians()["median_views_by_format"].items():
        assert isinstance(row["median_views"], int), name
        assert isinstance(row["measured"], int), name
        assert row["evidence"], name
    assert _format_medians()["median_views_by_format"]["reply"]["measured"] == 7


def test_the_note_tells_the_model_to_say_when_the_sample_is_thin(his_numbers):
    from app.account_facts import _format_medians

    note = _format_medians()["format_stat_note"]
    assert "measured" in note and "thin" in note


def test_a_format_with_no_measured_views_is_absent_not_zero(his_numbers):
    """Reporting quote as 0 would be a claim about performance. Absent is the
    honest state, and the note says so."""
    from app.account_facts import _format_medians

    by_format = _format_medians()["median_views_by_format"]
    assert "quote" not in by_format, "a format with no measured views must not be reported"


def test_no_performance_data_means_no_field(monkeypatch):
    """The empty case is the default on a fresh install — it must add nothing
    rather than a row of nulls the model can misread as zeros."""
    import app.performance
    from app.account_facts import _format_medians

    monkeypatch.setattr(app.performance, "performance_insights",
                        lambda *a, **k: {"by_format": {}})
    assert _format_medians() == {}


def test_a_broken_performance_call_does_not_break_the_facts(monkeypatch):
    import app.performance
    from app.account_facts import _format_medians

    def _boom(*a, **k):
        raise RuntimeError("performance is down")

    monkeypatch.setattr(app.performance, "performance_insights", _boom)
    assert _format_medians() == {}
    assert account_facts("")           # the whole fact block still builds


def test_the_facts_survive_the_trip_to_the_model(his_numbers):
    """THE PIPE, not the producer. `_chat_messages` serialises the brief through
    `_compact_next_strategy`, which rebuilds a fixed dict — a key it does not
    name is dropped in flight, and the sending end looks correct forever."""
    from app.llm import _compact_next_strategy

    facts = account_facts(HIS_QUESTION)
    assert facts.get("median_views_by_format"), "not in the facts to begin with"
    compacted = _compact_next_strategy({"account_facts": facts})
    assert compacted["account_facts"]["median_views_by_format"] == \
        facts["median_views_by_format"]


def test_the_chat_path_reaches_the_brief_for_this_message(his_numbers):
    """The whole chain: the message no longer diverts to the drafter — the only
    branch that returns before the brief is built — and the brief carries the
    comparison it needs."""
    assert not _wants_draft(HIS_QUESTION, has_context=False)
    assert account_facts(HIS_QUESTION).get("median_views_by_format")
    assert callable(main._wants_draft)
