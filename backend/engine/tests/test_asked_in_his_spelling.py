"""Three answers that existed and could not be reached, and two that did not exist.

All four defects were found the same way: standing a companion up against a copy
of the real database and asking it ordinary questions the way he actually types,
rather than reading the code. None was visible in a diff.

1. THE PUBLISH BOUNDARY was unreachable. `find_capability` matched raw text only,
   so "cna u psot for me" missed and the answer — "No, I can't post, and I'm not
   built to" — was handed to the model. He typed that phrasing NINE times in his
   1,102 stored messages and never once got it. Whether Starling can publish to
   his account is the one question a model must never be left to guess at.

2. "automaticly" was not corrected, so the auto-publish phrasing of the same
   boundary missed even after (1): the matcher wants `automatically`, and the
   fuzzy stage only corrects toward `_CANONICAL`, which that word is not in.

3. THE MEAN AND THE TOTAL were absent from the facts payload while the median was
   present. Measured on his archive: median 310, mean 872, total 48,841. Asked
   for an average, the only labelled number available was the median — 64% low.
   The standing lesson in `account_facts.py` is that a missing aggregate becomes
   a fabricated one; the file already carries a note about an invented "median
   reach score of 576".

4. FUTURE-TENSE QUESTIONS were answered with present numbers. "How many
   followers will I have next month" returned "You have 23 followers in the
   latest local snapshot" — a real number silently answering a question nobody
   asked, the same shape `_count_window` exists to prevent for timeframes.

The bar for 1, 2 and 4 is his own corpus: zero of his 1,102 real messages may be
hijacked, and every intent that resolved before must still resolve.
"""
from __future__ import annotations

import re

import pytest

from app import strategy
from app.account_facts import account_facts
from app.capabilities import CAPABILITIES, capability_answer, find_capability
from app.text_normalizer import normalize_typos


# --------------------------------------------------------------------------- #
# 1 + 2. the publish boundary, in his spelling
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("question", [
    "can u psot for me",
    "cna u psot for me",          # the exact phrasing he used nine times
    "can u psot it",
    "psot it for me",
    "will u psot this",
    "can u publsih this",
    "do u psot automaticly",
    "will u psot automaticly",
    "do u publsih automaticly",
])
def test_the_publish_boundary_answers_in_his_spelling(question):
    answer = capability_answer(question, None)
    assert answer, f"{question!r} did not reach the publish answer"
    lowered = answer.lower()
    assert "can't post" in lowered or "cannot post" in lowered, answer
    assert "press post" in lowered, "the answer must say who presses Post"


def test_the_normaliser_turns_his_phrasing_into_the_one_that_routes():
    """The correction already existed; nothing was using it."""
    assert normalize_typos("can u psot for me") == "can you post for me"
    assert "automatically" in normalize_typos("do u psot automaticly")


def test_capability_precedence_is_unchanged_by_normalising():
    """Raw text is still tried first within each capability's turn, so a message
    that already resolved resolves to the SAME capability."""
    for question in ("can you post for me", "can you export my data",
                     "how is my account doing", "what should i do right now"):
        raw_hit = next((c for c in CAPABILITIES if c.matcher.search(question)), None)
        if raw_hit is None:
            continue
        assert find_capability(question) is raw_hit, question


# --------------------------------------------------------------------------- #
# 3. the aggregates that were missing
# --------------------------------------------------------------------------- #

def _seed_skewed_views():
    """Four posts, one of them an outlier — the shape his real archive has.

    views 10, 20, 30, 1940 -> median 25, mean 500, total 2000. Seeded rather than
    read from whatever database the test runs against, so these assertions hold a
    real number instead of skipping when the corpus is empty.

    The last one is deliberately dated OUTSIDE the 30-day window. With everything
    inside it, `total_views_measured` and `views_last_30_days` are legitimately
    equal and the test cannot tell the all-time total apart from the window — the
    exact substitution that made "how many views in total" answer 45,236 against
    a true 48,841 on his archive.
    """
    from datetime import datetime, timedelta, timezone
    from app import storage
    ages = (1, 2, 3, 200)
    for index, (views, age) in enumerate(zip((10, 20, 30, 1940), ages)):
        stamp = (datetime.now(timezone.utc) - timedelta(days=age)).isoformat()
        storage.upsert_post({
            "post_id": f"views{index}", "text": f"A seeded post number {index}.",
            "post_type": "post", "created_at": stamp, "source_file": "test",
            "metrics": {"views": views},
        })
    return {"median": 25, "mean": 500, "total": 2000, "within_30_days": 60}


def test_the_facts_payload_carries_the_mean_and_the_total_beside_the_median():
    expected = _seed_skewed_views()
    facts = account_facts()
    assert facts.get("posts_with_measured_views") == 4

    for key in ("median_views", "mean_views", "total_views_measured"):
        assert facts.get(key) is not None, f"{key} is absent — it will be invented"

    assert facts["median_views"] == expected["median"]
    assert facts["mean_views"] == expected["mean"]
    assert facts["total_views_measured"] == expected["total"]

    note = facts.get("views_stat_note") or ""
    # The note is what stops the mean being used for the "did it do well"
    # judgement, which stays on the median on purpose.
    assert "median_views" in note and "mean_views" in note, note


def test_the_mean_is_computed_from_the_same_list_as_the_median():
    """They differed by 2.8x on his real archive — one 17,787-view post. Equal
    values here would mean the mean is being read off the wrong list, and a
    30-day window must never be handed over as an all-time total."""
    expected = _seed_skewed_views()
    facts = account_facts()
    assert facts["mean_views"] != facts["median_views"]
    assert facts["mean_views"] > facts["median_views"], "the outlier must pull the mean up"
    # The all-time total counts the 200-day-old post; the 30-day window does not.
    assert facts["total_views_measured"] == expected["total"]
    assert facts.get("views_last_30_days") == expected["within_30_days"]
    assert facts["total_views_measured"] > facts["views_last_30_days"], (
        "the all-time total is indistinguishable from the 30-day window, which is "
        "how a window gets handed over as a total")


# --------------------------------------------------------------------------- #
# 4. the future is not the present
# --------------------------------------------------------------------------- #

_REFUSAL = "can't tell you that"


@pytest.mark.parametrize("question", [
    "how many followers will i have next month",
    "how many followers will i have next week",
    "hwo mnay folwers will i have next moth",
    "what will my next post get",
    "how many views will my next post get",
    "how many followers am i going to have",
    "will i hit 100 followers",
    "predict my growth",
    "will my next post do well",
])
def test_a_question_about_the_future_is_refused_not_answered_with_todays_number(question):
    answer = strategy._facts_question_answer(question)
    assert answer, f"{question!r} returned nothing at all"
    assert _REFUSAL in answer.lower(), answer
    # It must not smuggle a number in beside the refusal.
    assert not re.search(r"\b\d{2,}\b", answer), f"a figure leaked into the refusal: {answer}"


@pytest.mark.parametrize("question", [
    "how many followers do i have",
    "am i growign",
    "whats my raech",
    "whats my best psot",
    "when shodu i psot",
    "hwo mnay psots did i do this wek",
    "what will help me grow",              # "will", and NOT a prediction
    "what was my last rpely",
    "what tiem do i usaully psot",
    "am i psoting to much",
    "how many followers did i gain from my last post",
])
def test_the_guard_leaves_every_real_question_alone(question):
    answer = strategy._facts_question_answer(question)
    if answer is None:
        return                              # handled elsewhere; not hijacked
    assert _REFUSAL not in answer.lower(), f"{question!r} was hijacked as a prediction"


def test_both_halves_are_required_so_will_alone_never_fires():
    """`will` is ordinary advice vocabulary. Without a metric noun beside it the
    guard must stay quiet, which is what keeps it off 1,102 real messages."""
    assert strategy._PREDICTIVE_RE.search("will i have")
    assert not strategy._PREDICTIVE_METRIC_RE.search("will i have")
    assert strategy._PREDICTIVE_METRIC_RE.search("how many followers do i have")
    assert not strategy._PREDICTIVE_RE.search("how many followers do i have")
