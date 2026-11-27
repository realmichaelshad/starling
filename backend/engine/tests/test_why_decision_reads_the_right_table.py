""""Why did you say wait" reads the table that has the decisions.

HE ASKED ELEVEN TIMES. `strategy_messages` holds "why did u say wait on that
last one" eleven times across eleven conversations, NINE of them between 02:31
and 06:35 on 26 July. The first answer was invented — "Because the last post had
low engagement and high risk of being ignored" — and every one after it said "I
don't have that decision recorded, so I won't invent a reason for it."

Replacing the fabrication with an honest refusal was the right repair. Ten
refusals is a different failure, and it could never have been anything else:

    _why_decision_answer read list_generations
    generations carrying a decision payload : 0 of 2223   <- not "usually none". none, ever.
    generations by action : post, reply, quote            <- a WAIT makes no row at all
    opportunity_assessments : 36 rows, 15 of them waits, with headline + reasons

The answer existed the whole time, one table away, and
`capabilities._decision_record_answer` already read it. Two answerers, one
inert, and his phrasing routed to the inert one.

WHY GENERATIONS IS KEPT AS A SECOND SOURCE rather than replaced: it can carry
decisions for drafted actions that the opportunity table may not have, and
consulting both costs one query. The bug was reading ONLY the empty one.
"""
from __future__ import annotations

import json

import pytest

from app import main


@pytest.fixture(autouse=True)
def _no_generations(monkeypatch):
    """Generations are empty in reality — 0 of 2223 — so the default fixture
    matches the world. A test that populated them would prove nothing about the
    defect."""
    monkeypatch.setattr("app.storage.list_generations", lambda **kw: [])


def _assessment(action: str, headline: str, reasons: list[str]) -> dict:
    """A row shaped as `recent_opportunity_assessments` actually returns one:
    `reasons` is a genuine list, not a string."""
    return {"id": "x", "action": action, "headline": headline,
            "reasons": reasons, "created_at": "2026-08-07T00:00:00+00:00"}


# --------------------------------------------------------------------------- #
# the question he asked eleven times
# --------------------------------------------------------------------------- #

def test_a_wait_on_record_is_explained(monkeypatch):
    monkeypatch.setattr("app.storage.recent_opportunity_assessments", lambda **kw: [
        _assessment("wait", "I could not find a point worth adding to this one.",
                    ["The evidence behind this is thin."])])
    answer = main._why_decision_answer()
    assert "don't have that decision recorded" not in answer, answer
    assert "wait" in answer
    assert "I could not find a point worth adding to this one." in answer
    assert "The evidence behind this is thin." in answer


def test_the_stored_duplicate_is_not_read_back_twice(monkeypatch):
    """8 of 15 real wait rows have `reasons[0] == headline`. The guard for that
    was written while this function could not reach any data at all — it is
    live and load-bearing the moment the reader is repointed."""
    line = "Nothing here is worth acting on right now."
    monkeypatch.setattr("app.storage.recent_opportunity_assessments", lambda **kw: [
        _assessment("wait", line, [line])])
    answer = main._why_decision_answer()
    assert answer.count(line) == 1, f"read back twice:\n{answer}"


def test_nothing_on_record_still_refuses_honestly(monkeypatch):
    """The refusal was the right repair for the fabrication and must survive.
    It is only wrong as the answer to a question that IS on record."""
    monkeypatch.setattr("app.storage.recent_opportunity_assessments", lambda **kw: [])
    answer = main._why_decision_answer()
    assert "won't invent a reason" in answer


# --------------------------------------------------------------------------- #
# the second source, and the failure modes of both
# --------------------------------------------------------------------------- #

def test_a_generations_decision_is_still_read(monkeypatch):
    monkeypatch.setattr("app.storage.recent_opportunity_assessments", lambda **kw: [])
    monkeypatch.setattr("app.storage.list_generations", lambda **kw: [
        {"request_json": json.dumps({"decision": {
            "action": "reply", "rationale": "There is a distinct point here.",
            "reasons": ["The thread is live and worth entering now."]}})}])
    answer = main._why_decision_answer()
    assert "There is a distinct point here." in answer
    assert "The thread is live and worth entering now." in answer


def test_a_broken_store_does_not_take_the_answer_down(monkeypatch):
    def boom(**kw):
        raise RuntimeError("database is locked")

    monkeypatch.setattr("app.storage.recent_opportunity_assessments", boom)
    monkeypatch.setattr("app.storage.list_generations", boom)
    assert "won't invent a reason" in main._why_decision_answer()


def test_one_broken_source_does_not_hide_the_other(monkeypatch):
    """Both are consulted, so a failure in the first must not swallow the
    second — that is how this became a single-source read in the first place."""
    def boom(**kw):
        raise RuntimeError("database is locked")

    monkeypatch.setattr("app.storage.recent_opportunity_assessments", boom)
    monkeypatch.setattr("app.storage.list_generations", lambda **kw: [
        {"request_json": json.dumps({"decision": {
            "action": "post", "rationale": "It carries a number worth stating.",
            "reasons": []}})}])
    assert "It carries a number worth stating." in main._why_decision_answer()


def test_a_row_with_no_words_is_skipped(monkeypatch):
    """An action with neither a headline nor reasons explains nothing; falling
    through to the next row beats printing "I said wait because:" and stopping."""
    monkeypatch.setattr("app.storage.recent_opportunity_assessments", lambda **kw: [
        _assessment("wait", "", []),
        _assessment("wait", "The thread is already answered.", [])])
    assert "The thread is already answered." in main._why_decision_answer()


def test_the_reader_consults_the_opportunity_table():
    """Pinned positively — an absence assertion would trip on this file's own
    prose, which has caught four source scans here already."""
    import inspect

    source = inspect.getsource(main._decisions_on_record)
    assert "recent_opportunity_assessments" in source, (
        "the answer must read the table that actually holds decisions; "
        "generations has never carried one")
