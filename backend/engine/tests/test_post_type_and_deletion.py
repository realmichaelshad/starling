"""He asked for his latest POST and got a deleted REPLY.

Two independent faults landing in one answer, both reported from real use:

  1. TYPE. "What was my latest post" took the newest row of ANY type and only
     swapped the noun in the label, so his most recent reply came back as the
     answer. A reply is not a post to him — it lives somewhere else and reads
     differently. He could neither ask for a reply ("what was my latest reply"
     matched no intent at all and fell through to the model) nor avoid getting
     one.

  2. DELETION. The post it named was one he had already deleted from X.
     `performance_records()` does filter `deleted_at` — correctly — but
     nothing had ever set that column: zero rows in his live database were
     marked, so the filter was inert. The only writer is the chat correction
     "but I deleted it", and that had a hole of its own (below).

And the correction itself could destroy data: naming a post it could not
resolve fell back to the NEWEST post, so "I deleted the pancake recipe post"
marked his most recent reply dead — a live post, never mentioned, reported
back to him as handled.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.storage import (connect, mark_post_deleted, save_metric_snapshot,
                         set_account_profile, upsert_post)
from app.strategy import _deletion_correction, _facts_question_answer


def _seed() -> None:
    """Newest row is a REPLY, with standalone posts behind it — his shape."""
    set_account_profile({"handle": "example_builder", "timezone": "UTC"})
    now = datetime.now(timezone.utc)
    rows = [
        ("newest_reply", "reply", 1, "a reply about grok on copilot being faster"),
        ("newest_post", "post", 3, "a standalone post about zuckerberg and superintelligence"),
        ("older_post", "post", 9, "an older standalone post about dario and open models"),
    ]
    for post_id, post_type, hours, text in rows:
        upsert_post({"post_id": post_id, "post_type": post_type,
                     "created_at": (now - timedelta(hours=hours)).isoformat(),
                     "text": text})
        save_metric_snapshot({"post_id": post_id, "captured_at": now.isoformat(),
                              "metrics": {"views": 300}})


@pytest.mark.parametrize("message", [
    "what was my latest post",
    "what was my last post",
    "whats my most recent post",
    "how is my last post doing",
    "what do you think of my last post",
    "when was my last post",
])
def test_asking_for_a_post_never_returns_a_reply(message: str) -> None:
    _seed()
    reply = _facts_question_answer(message)
    assert reply is not None, message
    assert "your last reply" not in reply.lower(), reply
    assert "a reply about grok" not in reply, reply


@pytest.mark.parametrize("message", [
    "what was my latest reply",
    "whats my most recent reply",
    "how is my last reply doing",
])
def test_asking_for_a_reply_returns_the_reply(message: str) -> None:
    _seed()
    reply = _facts_question_answer(message)
    assert reply is not None, f"{message} matched no intent at all"
    assert "reply" in reply.lower()


def test_a_missing_type_says_so_rather_than_substituting() -> None:
    _seed()
    reply = _facts_question_answer("whats my last quote")
    assert reply is not None
    assert "no quote-tweets" in reply.lower()


def test_widening_the_openers_did_not_swallow_the_ranking_questions() -> None:
    """"What's my best post" is a ranking question, not a critique of the
    newest one — accepting "what's" as an opener made it return the reply."""
    _seed()
    best = _facts_question_answer("whats my best post")
    assert best is not None and "best" in best.lower(), best
    worst = _facts_question_answer("whats my worst post")
    assert worst is not None and "lowest" in worst.lower(), worst


def test_a_deleted_post_is_never_the_answer() -> None:
    _seed()
    mark_post_deleted("newest_post")
    reply = _facts_question_answer("what was my latest post")
    assert reply is not None
    assert "zuckerberg" not in reply.lower(), reply
    assert "dario" in reply.lower(), reply


def test_deleting_the_newest_does_not_promote_a_reply_to_post() -> None:
    """His exact report: newest post deleted, so the next row is a reply."""
    _seed()
    mark_post_deleted("newest_post")
    reply = _facts_question_answer("what was my latest post")
    assert reply is not None
    assert "your last reply" not in reply.lower(), reply


def _seed_ranked() -> None:
    """A reply that out-performs every post — the latent case in his data.

    His #1 is a post at 4,931 and his #3 is a REPLY at 3,569. `best_post_ever`
    is a max() over both with no type filter, printed under the word "post", so
    the day that reply overtakes, "your best post" names a reply. Latent is not
    fixed.
    """
    set_account_profile({"handle": "example_builder", "timezone": "UTC"})
    now = datetime.now(timezone.utc)
    rows = [("top_reply", "reply", 9000), ("mid_post", "post", 500),
            ("low_post", "post", 20), ("low_reply", "reply", 60)]
    for index, (post_id, post_type, views) in enumerate(rows):
        upsert_post({"post_id": post_id, "post_type": post_type,
                     "created_at": (now - timedelta(hours=index + 2)).isoformat(),
                     "text": f"{post_type} body number {index} with real words in it"})
        save_metric_snapshot({"post_id": post_id, "captured_at": now.isoformat(),
                              "metrics": {"views": views}})


def test_the_best_post_is_never_a_reply_wearing_the_word_post() -> None:
    _seed_ranked()
    reply = _facts_question_answer("whats my best post")
    assert reply is not None
    assert "9000" not in reply, reply          # that is the reply's view count
    assert "your best post" in reply.lower()


def test_the_best_reply_is_rankable_on_its_own() -> None:
    _seed_ranked()
    reply = _facts_question_answer("whats my best reply")
    assert reply is not None, "matched no intent at all"
    assert "9000" in reply, reply
    assert "your best reply" in reply.lower()


def test_the_worst_post_is_never_a_reply_either() -> None:
    _seed_ranked()
    reply = _facts_question_answer("whats my worst post")
    assert reply is not None
    assert "20" in reply, reply
    assert "lowest post" in reply.lower()


def test_the_worst_reply_is_rankable_on_its_own() -> None:
    _seed_ranked()
    reply = _facts_question_answer("whats my worst reply")
    assert reply is not None
    assert "60" in reply, reply
    assert "lowest reply" in reply.lower()


def test_a_type_with_no_measured_rows_says_so() -> None:
    _seed_ranked()
    reply = _facts_question_answer("whats my best quote")
    assert reply is not None
    assert "no measured quote-tweets" in reply.lower()


def test_bare_correction_asks_then_marks_the_newest() -> None:
    """A BARE "it" is the one case this code admits it cannot resolve — its own
    comment says nothing here can see the previous turn. The write is
    irreversible and there is no restore path, so it asks first. The end state
    is unchanged; only the number of turns is."""
    from app import strategy
    _seed()
    strategy._PENDING_DELETE = None
    asked = _deletion_correction("but i deleted it")
    assert asked is not None and "to be sure" in asked
    with connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM posts WHERE deleted_at IS NOT NULL").fetchone()[0] == 0
    assert _deletion_correction("yes") is not None
    with connect() as conn:
        gone = conn.execute(
            "SELECT post_id FROM posts WHERE deleted_at IS NOT NULL").fetchall()
    assert [row[0] for row in gone] == ["newest_reply"]


def test_a_named_post_is_the_one_marked() -> None:
    _seed()
    answer = _deletion_correction("i deleted the dario post")
    assert answer is not None
    with connect() as conn:
        gone = [row[0] for row in conn.execute(
            "SELECT post_id FROM posts WHERE deleted_at IS NOT NULL")]
    assert gone == ["older_post"], gone


@pytest.mark.parametrize("message", [
    "i deleted the pancake recipe post",
    "i deleted the unicorn post",
])
def test_an_unresolvable_name_marks_nothing_and_asks(message: str) -> None:
    """It fell back to the newest post — marking a live one dead and saying
    it was handled."""
    _seed()
    answer = _deletion_correction(message)
    assert answer is not None
    assert "can't find a post matching" in answer.lower(), answer
    with connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE deleted_at IS NOT NULL").fetchone()[0]
    assert count == 0, "a post was marked dead on an unresolved name"


@pytest.mark.parametrize("message", [
    "whats my name", "what is my handle", "who am i", "whats my username",
    "what do you know about me",
])
def test_it_knows_who_he_is(message: str) -> None:
    """"What's my name" spent 43 seconds in the model to answer "I don't have
    your name. You're the one who sets it on your profile." — over a stored
    profile holding his handle, his display name and his bio."""
    set_account_profile({"handle": "example_builder",
                         "display_name": "Alex Rivera",
                         "bio": "Working on local models.", "timezone": "UTC"})
    reply = _facts_question_answer(message)
    assert reply is not None, message
    assert "example_builder" in reply
    assert "don't have your name" not in reply.lower()


def test_asking_who_STARLING_is_is_a_different_question() -> None:
    set_account_profile({"handle": "example_builder",
                         "display_name": "Alex Rivera", "timezone": "UTC"})
    reply = _facts_question_answer("whats your name")
    assert reply is None or "example_builder" not in reply


@pytest.mark.parametrize("message", [
    "what did i post last", "what did i last post",
])
def test_what_did_i_post_last_is_answered_from_storage(message: str) -> None:
    """It carried no opener the critique matcher knew and spent 52 seconds in
    the model reaching an answer that is free."""
    _seed()
    reply = _facts_question_answer(message)
    assert reply is not None, message
    assert "your last post" in reply.lower()


@pytest.mark.parametrize("message", [
    "i deleted the last post", "i deleted the latest post",
])
def test_generic_referents_still_mean_the_newest(message: str) -> None:
    _seed()
    answer = _deletion_correction(message)
    assert answer is not None
    assert "can't find" not in answer.lower(), answer
    with connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE deleted_at IS NOT NULL").fetchone()[0]
    assert count == 1
