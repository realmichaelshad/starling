"""The account review: blunt, grounded, and never ranked against strangers.

"What do you think of my account honestly, what should I do to make it better"
had no handler. It fell past every account answer to the preference gate and
came back "Noted — 'Make it better.' I'll hold every draft to that from now on."

Before that gate existed it fell to the model, which is the component that
answered "You're not growing" over a payload showing +5 followers and "I'm not
slow" after taking 88.9 seconds. A judgement question invites a judgement.

These tests pin the two properties that make the answer trustworthy: every
claim carries the number and sample size behind it, and no verdict is ever
stated against other accounts — Starling can see exactly one.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

from app.account_review import account_review, account_review_text
from app.capabilities import capability_answer, find_capability
from app.storage import (save_metric_snapshot, save_profile_snapshot,
                         set_account_profile, upsert_post)

HIS_QUESTION = ("what do you think of my account honestly like what should i "
                "do to make it better")


def _seed(*, replies_beat_posts: bool = True) -> None:
    """His real shape: a handful of replies out-earning many more posts."""
    set_account_profile({"handle": "example_builder", "timezone": "UTC"})
    now = datetime.now(timezone.utc)
    save_profile_snapshot({"captured_at": (now - timedelta(days=7)).isoformat(),
                           "followers_count": 17})
    save_profile_snapshot({"captured_at": now.isoformat(), "followers_count": 22})
    reply_views = 488 if replies_beat_posts else 90
    for index in range(12):
        upsert_post({"post_id": f"p{index}", "post_type": "post",
                     "created_at": (now - timedelta(days=index + 1)).isoformat(),
                     "text": f"a standalone post about local models number {index}"})
        save_metric_snapshot({"post_id": f"p{index}", "captured_at": now.isoformat(),
                              "metrics": {"views": 4931 if index == 0 else 302}})
    for index in range(3):
        upsert_post({"post_id": f"r{index}", "post_type": "reply",
                     "created_at": (now - timedelta(days=index + 1, hours=3)).isoformat(),
                     "text": f"a reply about model pricing number {index}"})
        save_metric_snapshot({"post_id": f"r{index}", "captured_at": now.isoformat(),
                              "metrics": {"views": reply_views}})


def test_his_question_reaches_the_review() -> None:
    """The acceptance test. This exact sentence, end to end."""
    _seed()
    capability = find_capability(HIS_QUESTION)
    assert capability is not None, "matched no capability at all"
    assert capability.key == "account_review", capability.key
    answer = capability_answer(HIS_QUESTION, None)
    assert answer is not None
    assert "Noted" not in answer, "the preference gate answered it again"
    assert "honest read" in answer.lower()


@pytest.mark.parametrize("message", [
    "what do you think of my account",
    "whats holding my account back",
    "what am i doing wrong",
    "how do i get more views",
    "whats my biggest problem",
    "review my account",
    "how can i make my account better",
    "what should i be doing",
])
def test_the_whole_family_routes(message: str) -> None:
    capability = find_capability(message)
    assert capability is not None and capability.key == "account_review", message


@pytest.mark.parametrize("message", [
    "what should i post", "how many followers do i have",
    "whats my best post", "write me a post about opus 5",
    "when was my last post",
])
def test_the_review_does_not_swallow_the_narrow_questions(message: str) -> None:
    capability = find_capability(message)
    assert capability is None or capability.key != "account_review", message


def test_the_unexploited_format_is_the_headline_finding() -> None:
    """Replies out-earning posts while barely used IS the answer to "what should
    I do" — and it carries both medians and both sample sizes, because a 3-post
    median read as a law is how a lead becomes a false certainty."""
    _seed(replies_beat_posts=True)
    review = account_review()
    gap = next((line for line in review["diagnosis"] if "out-earn" in line), None)
    assert gap is not None, review["diagnosis"]
    assert "488" in gap and "302" in gap, gap
    assert re.search(r"\b3 replies\b", gap), gap
    assert re.search(r"\b12 standalone posts\b", gap), gap
    assert "evidence" in gap


def test_no_format_gap_is_claimed_when_replies_do_not_win() -> None:
    _seed(replies_beat_posts=False)
    review = account_review()
    assert not any("out-earn" in line for line in review["diagnosis"])


def test_the_ceiling_finding_names_both_numbers() -> None:
    _seed()
    review = account_review()
    ceiling = next((line for line in review["diagnosis"] if "repeat" in line), None)
    assert ceiling is not None, review["diagnosis"]
    assert "4,931" in ceiling


def test_it_never_ranks_him_against_other_accounts() -> None:
    """The exact failure behind "You're not growing": a verdict with no
    benchmark to support it. Starling sees one account."""
    _seed()
    text = account_review_text()
    assert "one account" in text
    assert "not growing" not in text.lower()
    for phrase in ("average account", "most accounts", "compared to others",
                   "industry", "benchmark"):
        assert phrase not in text.lower(), phrase


def test_every_diagnosis_line_carries_a_number() -> None:
    _seed()
    for line in account_review()["diagnosis"]:
        assert re.search(r"\d", line), f"unquantified claim: {line}"


def test_a_diagnosis_always_comes_with_something_to_do() -> None:
    _seed()
    review = account_review()
    assert review["diagnosis"]
    assert review["moves"], "diagnosis with no recommended action"


def test_an_empty_account_says_so_instead_of_inventing_a_read() -> None:
    review = account_review()
    assert review["diagnosis"] == [] or review["headline"]
    if not review["diagnosis"]:
        assert "sync" in review["headline"].lower() or review["headline"]


def test_percentiles_read_as_english() -> None:
    from app.account_review import _ordinal
    assert [_ordinal(n) for n in (1, 2, 3, 11, 12, 13, 21, 61)] == \
        ["1st", "2nd", "3rd", "11th", "12th", "13th", "21st", "61st"]


def test_stating_a_goal_is_not_answered_with_a_review() -> None:
    """"I want to get to 5k followers" reads as a growth question but is an
    instruction to record something."""
    _seed()
    answer = capability_answer("i want to get to 5000 followers", None)
    assert answer is not None
    assert answer.startswith(("Set:", "Updated:")), answer
