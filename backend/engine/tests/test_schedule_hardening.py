"""Scheduling, stressed. Hold-and-remind must never lose, duplicate, or lie.

The invariant under every test: NOTHING here posts to X. A scheduled draft is
held locally and handed back when its time comes; the human posts it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app import main as app_main
from app import storage
from app.schemas import BrowserAssistRequest, ScheduleCommand


def _in(hours: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _cmd(**kw) -> BrowserAssistRequest:
    return BrowserAssistRequest(mode="schedule", schedule=ScheduleCommand(**kw))


@pytest.fixture(autouse=True)
def _clean_store():
    for row in storage.list_scheduled_drafts(None):
        storage.delete_scheduled_draft(int(row["id"]))
    yield
    for row in storage.list_scheduled_drafts(None):
        storage.delete_scheduled_draft(int(row["id"]))


# --- the surface is kept ---------------------------------------------------- #

def test_a_held_reply_comes_back_as_a_reply():
    """action was hardcoded to "post" on save, so a scheduled REPLY resurfaced
    as a standalone post — a draft addressed to nobody, about nothing."""
    result = app_main._handle_schedule_command(_cmd(
        op="save", text="the 256k context number is the real story here",
        target_at=_in(2), target_label="in 2 hours", action="reply",
        context_post_id="1234567890",
        context_url="https://x.com/claudeai/status/1234567890"))
    assert result["ok"] and result["scheduled"]["action"] == "reply"
    held = storage.list_scheduled_drafts("scheduled")
    assert len(held) == 1
    assert held[0]["action"] == "reply"
    # ...and it remembers WHAT it replies to, so the reminder can take him back.
    assert held[0]["meta"]["context_post_id"] == "1234567890"
    assert held[0]["meta"]["context_url"].endswith("/1234567890")


def test_a_held_thread_keeps_every_part():
    """Holding a thread used to store part 1 only: the reminder resurfaced an
    opening line with no thread behind it."""
    parts = ["Local models are cheaper than the cloud bill suggests.",
             "The catch is memory bandwidth, not FLOPs.",
             "Which is why a 5-year-old workstation still beats a laptop here."]
    result = app_main._handle_schedule_command(_cmd(
        op="save", text=parts[0], thread_parts=parts,
        target_at=_in(5), target_label="tonight", action="post"))
    assert result["scheduled"]["thread_parts"] == parts
    assert "thread" in result["message"]
    held = storage.list_scheduled_drafts("scheduled")[0]
    assert held["meta"]["thread_parts"] == parts


# --- one draft, one reminder ------------------------------------------------ #

def test_scheduling_the_same_draft_twice_moves_it_instead_of_duplicating():
    text = "Opus 5 is out and the pricing is the headline, not the benchmark."
    first = app_main._handle_schedule_command(_cmd(
        op="save", text=text, target_at=_in(2), target_label="in 2 hours"))
    second = app_main._handle_schedule_command(_cmd(
        op="save", text=text, target_at=_in(6), target_label="tonight"))
    assert second["ok"]
    held = storage.list_scheduled_drafts("scheduled")
    assert len(held) == 1, "two reminders would fire for one post"
    assert held[0]["id"] == first["scheduled"]["id"], "it moved the original"
    assert held[0]["target_label"] == "tonight"
    assert "already held" in second["message"]


def test_whitespace_differences_still_count_as_the_same_draft():
    app_main._handle_schedule_command(_cmd(
        op="save", text="Opus 5 changes the maths.", target_at=_in(2)))
    app_main._handle_schedule_command(_cmd(
        op="save", text="  Opus 5   changes the maths.  ", target_at=_in(4)))
    assert len(storage.list_scheduled_drafts("scheduled")) == 1


# --- moving a hold ---------------------------------------------------------- #

def test_reschedule_moves_a_hold_and_never_creates_one():
    saved = app_main._handle_schedule_command(_cmd(
        op="save", text="hold this one", target_at=_in(2), target_label="in 2 hours"))
    draft_id = saved["scheduled"]["id"]
    moved = app_main._handle_schedule_command(_cmd(
        op="reschedule", draft_id=draft_id, target_at=_in(20), target_label="tomorrow"))
    assert moved["ok"] and moved["id"] == draft_id
    held = storage.list_scheduled_drafts("scheduled")
    assert len(held) == 1 and held[0]["target_label"] == "tomorrow"


def test_rescheduling_something_already_posted_says_so():
    saved = app_main._handle_schedule_command(_cmd(
        op="save", text="already gone out", target_at=_in(2)))
    draft_id = saved["scheduled"]["id"]
    app_main._handle_schedule_command(_cmd(op="mark_posted", draft_id=draft_id))
    result = app_main._handle_schedule_command(_cmd(
        op="reschedule", draft_id=draft_id, target_at=_in(9)))
    assert result["ok"] is False and "no longer held" in result["error"]


def test_reschedule_needs_both_an_id_and_a_time():
    with pytest.raises(HTTPException):
        app_main._handle_schedule_command(_cmd(op="reschedule", target_at=_in(3)))
    with pytest.raises(HTTPException):
        app_main._handle_schedule_command(_cmd(op="reschedule", draft_id=1))


# --- what comes back, and when ---------------------------------------------- #

def test_only_due_drafts_come_back():
    # Just-past is accepted (a 1-minute grace covers clock skew) and IS due;
    # a draft six hours out must stay held.
    app_main._handle_schedule_command(_cmd(op="save", text="later today", target_at=_in(6)))
    app_main._handle_schedule_command(_cmd(op="save", text="due now", target_at=_in(-0.005)))
    due = app_main._handle_schedule_command(_cmd(op="list_due"))["due"]
    assert [item["text"] for item in due] == ["due now"]


def test_a_stale_hold_comes_back_with_an_honest_warning():
    """A take written for a launch reads as late the next morning. The draft is
    never withheld — it returns with a note so he decides."""
    fresh = {"created_at": datetime.now(timezone.utc).isoformat(), "freshness": "fast"}
    assert app_main._staleness_note(fresh) is None
    old = {"created_at": (datetime.now(timezone.utc) - timedelta(hours=9)).isoformat(),
           "freshness": "fast"}
    note = app_main._staleness_note(old)
    assert note and "time-sensitive" in note and "9h ago" in note
    # Evergreen drafts are not nagged about after nine hours.
    assert app_main._staleness_note({**old, "freshness": "slow"}) is None
    # Garbage in never raises.
    assert app_main._staleness_note({"created_at": "not a date"}) is None


def test_marking_posted_removes_it_from_the_held_list():
    saved = app_main._handle_schedule_command(_cmd(op="save", text="going out", target_at=_in(1)))
    app_main._handle_schedule_command(_cmd(op="mark_posted", draft_id=saved["scheduled"]["id"]))
    assert storage.list_scheduled_drafts("scheduled") == []
    assert len(storage.list_scheduled_drafts("posted")) == 1
    # Marking it twice is honest about it rather than pretending.
    again = app_main._handle_schedule_command(
        _cmd(op="mark_posted", draft_id=saved["scheduled"]["id"]))
    assert again["ok"] is False


# --- refusals --------------------------------------------------------------- #

def test_the_past_and_the_empty_are_refused():
    past = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    with pytest.raises(HTTPException):
        app_main._handle_schedule_command(_cmd(op="save", text="too late", target_at=past))
    with pytest.raises(HTTPException):
        app_main._handle_schedule_command(_cmd(op="save", text="   ", target_at=_in(2)))
    with pytest.raises(HTTPException):
        app_main._handle_schedule_command(_cmd(op="save", text="no time given"))
    with pytest.raises(HTTPException):   # naive timestamps are ambiguous
        app_main._handle_schedule_command(
            _cmd(op="save", text="no timezone", target_at="2030-01-01T10:00:00"))


def test_scheduling_can_never_publish():
    """The invariant. If a publish path ever appears, this test is the alarm."""
    import ast
    import inspect
    # Check the CODE, not the prose: the docstring's promise ("there is no
    # publish branch") contains the very words a naive substring scan bans.
    tree = ast.parse(inspect.getsource(app_main._handle_schedule_command).strip())
    calls = {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    assert not any("requests" in call or "publish" in call.lower() or "post_to" in call
                   for call in calls), calls
    # Every storage call it makes is local CRUD on the held-drafts table.
    assert {call for call in calls if call.startswith("storage.")} <= {
        "storage.save_scheduled_draft", "storage.list_scheduled_drafts",
        "storage.due_scheduled_drafts", "storage.mark_scheduled_draft",
        "storage.reschedule_scheduled_draft", "storage.find_scheduled_duplicate"}
    valid_ops = set(ScheduleCommand.model_fields["op"].annotation.__args__)
    assert valid_ops == {"save", "list", "list_due", "mark_posted", "dismiss", "reschedule"}
