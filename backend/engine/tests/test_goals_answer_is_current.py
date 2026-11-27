""""What's my goal" answers with goals he still has.

FOUND BY DRIVING, 2026-08-07. Every phrase the help menu advertises was sent
through the chat door; thirteen answered well and one came back:

    Your goals:
    • 5000 followers
    • 5000 followers

Both rows in `growth_goals` were `status='archived'`. `main.py:4611` is the only
writer of that status and it is user-initiated, so nothing archives a goal on
its own — he had deleted both, and the product read them back to him. The
duplicate is what catches the eye; **reporting deleted goals as current is the
defect**, and it would show a single stale goal just as wrongly with nothing to
notice.

WHY IT DIVERGED. `list_goals()` returns every status deliberately — the export
(`storage.py:2110`) and `/strategy/goals` (`main.py:4582`) need archived rows —
so each consumer filters for itself. Three do: `_set_goal` in this same file,
`planner.py:44`, `strategy.py:40`. `_goals_answer` was the fourth and did not.

The author had already seen the failure mode: `_set_goal` carries the comment
"CHANGING YOUR MIND IS NOT A SECOND GOAL ... a stale duplicate would silently
win half the time", and guards the write path only. Same shape as the help
menu, where drift was named in a comment and the two hand-written lines sat
outside the guard.
"""
from __future__ import annotations

import pytest

from app import capabilities


class _Goals:
    """Stands in for `storage.list_goals`, which the answer imports locally."""

    def __init__(self, rows):
        self.rows = rows

    def __call__(self):
        return list(self.rows)


@pytest.fixture(autouse=True)
def _no_goal_setting(monkeypatch):
    """`_goals_answer` calls `_set_goal` first; a question must not set one."""
    monkeypatch.setattr(capabilities, "_set_goal", lambda message: None)


def _answer(rows, monkeypatch):
    monkeypatch.setattr("app.storage.list_goals", _Goals(rows))
    return capabilities._goals_answer("what's my goal", None)


ARCHIVED = {"name": "5000 followers", "objective": "followers", "status": "archived"}
ACTIVE = {"name": "1000 replies", "objective": "replies", "status": "active"}


# --------------------------------------------------------------------------- #
# the live case
# --------------------------------------------------------------------------- #

def test_the_two_archived_rows_he_deleted_are_not_read_back(monkeypatch):
    """The exact database state on 2026-08-07: two archived, nothing active."""
    got = _answer([dict(ARCHIVED), dict(ARCHIVED)], monkeypatch)
    assert "5000 followers" not in got, got
    assert "no goal set" in got, (
        "with every goal archived the honest answer is that none is set, and "
        "it invites him to name one")


def test_a_single_archived_goal_is_also_hidden(monkeypatch):
    """The duplicate was the symptom. One stale goal is the same defect with
    nothing visible to catch it."""
    got = _answer([dict(ARCHIVED)], monkeypatch)
    assert "5000 followers" not in got
    assert "no goal set" in got


# --------------------------------------------------------------------------- #
# and it must not become an outage
# --------------------------------------------------------------------------- #

def test_an_active_goal_is_still_reported(monkeypatch):
    got = _answer([dict(ACTIVE)], monkeypatch)
    assert "1000 replies" in got
    assert "no goal set" not in got


def test_the_active_one_survives_among_archived_ones(monkeypatch):
    got = _answer([dict(ARCHIVED), dict(ACTIVE), dict(ARCHIVED)], monkeypatch)
    assert "1000 replies" in got
    assert "5000 followers" not in got


def test_a_goal_with_no_status_counts_as_active(monkeypatch):
    """`_set_goal`, `planner` and `strategy` all default a missing status to
    "active". Defaulting the other way here would hide a real goal written by
    an older build."""
    got = _answer([{"name": "3000 followers", "objective": "followers"}], monkeypatch)
    assert "3000 followers" in got


def test_a_broken_store_still_answers(monkeypatch):
    def boom():
        raise RuntimeError("database is locked")

    monkeypatch.setattr("app.storage.list_goals", boom)
    got = capabilities._goals_answer("what's my goal", None)
    assert "no goal set" in got


# --------------------------------------------------------------------------- #
# the divergence itself
# --------------------------------------------------------------------------- #

def test_every_consumer_of_list_goals_filters_or_is_a_listing():
    """The fix is one filter; this is what stops a fifth consumer forgetting.

    `list_goals` is deliberately unfiltered, so the check cannot live there.
    Any caller that is not an explicit full listing must narrow by status.
    """
    import inspect
    import re

    from app import capabilities as caps
    from app import planner, strategy

    for module in (caps, planner, strategy):
        source = re.sub(r'"""(?:.|\n)*?"""', "", inspect.getsource(module))
        body = "\n".join(line for line in source.splitlines()
                         if not line.lstrip().startswith("#"))
        for match in re.finditer(r"list_goals\(\)", body):
            window = body[match.start():match.start() + 220]
            assert "status" in window, (
                f"{module.__name__} calls list_goals() without narrowing by "
                f"status near: {window[:120]!r}")
