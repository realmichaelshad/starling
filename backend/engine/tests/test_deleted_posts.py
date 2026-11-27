"""A post he deleted must stop being called his current one — registry K119.

He asked what his most recent post was, got one he had already deleted, and
said "but i deleted it". Two separate faults, both fixed here:

  1. `performance_records()` had no idea a post could be gone. A deletion made
     in X's own UI never reaches this database until a re-sync, so the row sat
     there looking live and fed every path that reads "most recent".
  2. The correction did nothing. Starling dropped the thread and answered with
     the canned "Tell me the point you want to add…" prompt. A user correcting
     a factual claim is the strongest signal available and it was discarded.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.storage import (mark_post_deleted, performance_records,   # noqa: E402
                         records, upsert_post)
from app.strategy import _deletion_correction                      # noqa: E402

OLDER = "Model routing is simple until it isn't."
NEWEST = "The pricing page is the only benchmark that has ever mattered."


def _seed() -> None:
    upsert_post({"post_id": "1", "created_at": "2026-07-20T10:00:00Z",
                 "post_type": "post", "text": OLDER})
    upsert_post({"post_id": "2", "created_at": "2026-07-25T10:00:00Z",
                 "post_type": "post", "text": NEWEST})


def test_a_deleted_post_leaves_performance_records():
    _seed()
    assert {r["post_id"] for r in performance_records()} == {"1", "2"}
    assert mark_post_deleted("2") is True
    remaining = performance_records()
    assert {r["post_id"] for r in remaining} == {"1"}
    # The row is not destroyed — it is marked. Deleting his history to represent
    # a deletion would be the worse bug.
    assert {r["post_id"] for r in records()} == {"1", "2"}


def test_marking_the_same_post_twice_is_a_no_op():
    _seed()
    assert mark_post_deleted("2") is True
    assert mark_post_deleted("2") is False
    assert mark_post_deleted("") is False
    assert mark_post_deleted("nope") is False


def test_the_correction_marks_it_and_names_the_next_one():
    """Updated for the confirmation step: an UNNAMED correction now asks which
    post before writing, because the write is irreversible and there is no
    restore path. The end state is unchanged — it still retires the right post.
    """
    from app import strategy
    _seed()
    strategy._PENDING_DELETE = None
    asked = _deletion_correction("but i deleted it")
    assert asked and NEWEST[:30] in asked, "it must name the post it means"
    assert {r["post_id"] for r in performance_records()} == {"1", "2"}, \
        "nothing may be written before the yes"
    answer = _deletion_correction("yes")
    assert answer and NEWEST[:30] in answer
    assert {r["post_id"] for r in performance_records()} == {"1"}


def test_the_correction_is_recognised_in_the_ways_he_writes_it():
    for phrasing in ("but i deleted it", "i deleted that post",
                     "I've already deleted it", "that post is deleted",
                     "i just deleted that"):
        _seed()
        assert _deletion_correction(phrasing), f"missed: {phrasing!r}"


def test_asking_about_deleting_is_not_a_correction():
    """The advice path must keep its questions. Marking a live post dead
    because he wondered aloud about deleting it is the expensive direction."""
    _seed()
    for question in ("should i delete it", "what should i delete",
                     "do you think i should delete that post",
                     "would deleting it help", "why did my post flop"):
        assert _deletion_correction(question) is None, f"fired on: {question!r}"
    assert {r["post_id"] for r in performance_records()} == {"1", "2"}


def test_his_actual_typo_reaches_the_handler():
    """The defect this path was built for and never caught: he typed
    "but i dleted it" and deletion is user-declared only, so the post stayed
    counted forever. `deleted_at` was NULL on all 99 of his rows."""
    from app import strategy
    _seed()
    strategy._PENDING_DELETE = None
    answer = _deletion_correction("but i dleted it")
    assert answer, "his exact words must reach the handler"
    assert NEWEST[:30] in answer
    # Reaches the handler, then CONFIRMS — the repair widened the path to an
    # irreversible write, so the destination was made safe in the same batch.
    assert {r["post_id"] for r in performance_records()} == {"1", "2"}
    _deletion_correction("yes")
    assert {r["post_id"] for r in performance_records()} == {"1"}, \
        "the repair has to reach mark_post_deleted, not merely match"


def test_the_other_one_character_slips():
    for slip in ("i delted it", "i deletd it", "i deleted it"):
        _seed()
        assert _deletion_correction(slip), f"missed: {slip!r}"


def test_a_synonym_is_a_different_intent_and_stays_outside():
    """"removed" is edit distance 4 and he may have removed a LINK, not the
    post. The boundary is derived from the word, so widening it to synonyms
    means editing this test first — which is the point."""
    _seed()
    for other in ("i removed it", "i took it down", "i hid that post"):
        assert _deletion_correction(other) is None, f"fired on: {other!r}"
    assert {r["post_id"] for r in performance_records()} == {"1", "2"}


def test_the_repair_never_turns_a_question_into_a_declaration():
    """THE TRAP IN THIS FIX. "delete" is ALSO one edit from "deleted", so a
    naive repair rewrites "should i delete this" into "should i deleted this"
    — which the matcher accepts, marking a live post dead on an advice
    question. Real inflections are excluded for exactly this reason."""
    _seed()
    for question in ("should i delete this", "delete my last post",
                     "what should i delete", "i am deleting it",
                     "can you delete it", "i might delete that post"):
        assert _deletion_correction(question) is None, f"fired on: {question!r}"
    assert {r["post_id"] for r in performance_records()} == {"1", "2"}, \
        "no question may ever write"


def test_the_accepted_spellings_are_derived_not_typed():
    """A hand-typed list drifts; this asserts the boundary itself."""
    from app.strategy import _one_edit_from_deleted, _repair_deleted_typo
    for near in ("dleted", "delted", "deletd", "deletee"):
        assert _one_edit_from_deleted(near), f"{near!r} is one edit away"
    for far in ("removed", "deleting", "deletion", "deleted"):
        assert not _one_edit_from_deleted(far), f"{far!r} must not be repaired"
    # And the repair only ever touches that one word.
    assert _repair_deleted_typo("but i dleted it") == "but i deleted it"
    assert _repair_deleted_typo("should i delete this") == "should i delete this"


def test_an_unnamed_deletion_asks_before_it_writes():
    """The walker's replay: three unqualified sentences marked THREE different
    posts dead, cascading backwards through the newest each time. There is no
    restore path in the companion and `mark_corpus_dirty()` propagates each
    loss into the voice caches — so the unnamed case asks."""
    from app import strategy
    for sentence in ("but i dleted it", "i deleted it", "i already deleted that one"):
        _seed()
        strategy._PENDING_DELETE = None
        answer = strategy._deletion_correction(sentence)
        assert answer and "to be sure" in answer, sentence
        assert {r["post_id"] for r in performance_records()} == {"1", "2"}, \
            f"{sentence!r} wrote without a confirmation"


def test_a_bare_yes_completes_the_deletion():
    from app import strategy
    _seed()
    strategy._PENDING_DELETE = None
    strategy._deletion_correction("but i deleted it")
    assert {r["post_id"] for r in performance_records()} == {"1", "2"}
    answer = strategy._deletion_correction("yes")
    assert answer and "Marked" in answer
    assert {r["post_id"] for r in performance_records()} == {"1"}


def test_only_a_bare_affirmative_claims_the_pending_delete():
    """This function cannot see the conversation, so the slot is time-boxed and
    a four-word ceiling keeps an unrelated 'yes' away from a write."""
    from app import strategy
    for other in ("what was my best post", "yes please do that for the other one too",
                  "no", "should i delete this"):
        _seed()
        strategy._PENDING_DELETE = None
        strategy._deletion_correction("i deleted it")
        strategy._deletion_correction(other)
        assert {r["post_id"] for r in performance_records()} == {"1", "2"}, other


def test_a_stale_pending_delete_expires_rather_than_firing():
    import time as _time
    from app import strategy
    _seed()
    strategy._PENDING_DELETE = None
    strategy._deletion_correction("i deleted it")
    stamped, post_id, snippet = strategy._PENDING_DELETE
    strategy._PENDING_DELETE = (stamped - strategy._PENDING_DELETE_TTL_S - 1, post_id, snippet)
    assert strategy._deletion_correction("yes") is None
    assert {r["post_id"] for r in performance_records()} == {"1", "2"}


# --------------------------------------------------------------------------- #
# His real deletion questions reach the delete path (PREP-TYPO-WRITE)
# --------------------------------------------------------------------------- #


def test_his_real_deletion_question_reaches_the_delete_path():
    """id 2049, VERBATIM. This missed `_DELETE_Q`, so the deterministic delete
    answer never ran and the model improvised from a payload carrying
    engagement_rate_pct=33.33 - producing "Don't delete it yet. It has a high
    engagement rate for you right now." on a post with 3 views."""
    from app.main import _DELETE_Q
    from app.text_normalizer import normalize_typos
    said = ("that spidy psot what hsodu i do with it hsodu i dleate it "
            "its nto doign well")
    assert _DELETE_Q.search(normalize_typos(said)), normalize_typos(said)


def test_his_other_deletion_question_reaches_the_delete_path():
    """Asked twice. Found by hand-reading every archive occurrence, not by a count."""
    from app.main import _DELETE_Q
    from app.text_normalizer import normalize_typos
    assert _DELETE_Q.search(normalize_typos("shodul i deelte this post"))


def test_the_declaration_still_works_through_both_paths():
    """IDEMPOTENCE, not conflict: the upstream map and the matcher-side repair
    now both handle "dleted", and the declaration must still CONFIRM."""
    from app import strategy
    from app.text_normalizer import normalize_typos
    _seed()
    strategy._PENDING_DELETE = None
    assert strategy._DELETED_IT.search(
        strategy._repair_deleted_typo(normalize_typos("but i dleted it")))
    answer = strategy._deletion_correction("but i dleted it")
    assert answer and "to be sure" in answer
    assert {r["post_id"] for r in performance_records()} == {"1", "2"}


def test_deplete_is_no_longer_rewritten_to_delete():
    """A live normalizer false positive found while sweeping for this fix:
    `deplete` scores 0.923 against `delete`, over the 0.88 cutoff."""
    from app.text_normalizer import normalize_typos
    assert normalize_typos("the pool will deplete fast") == "the pool will deplete fast"
    assert "delete" not in normalize_typos("should i deplete it")


def test_delve_survives_the_deletion_typo_fix():
    """THE TOMBSTONE for the distance rule. `delve` is edit-distance 2 from
    `delete` and IS in his vocabulary - a distance-2 net would eat it."""
    from app.text_normalizer import normalize_typos
    assert normalize_typos("dont say delve") == "dont say delve"
