"""Scheduling: freshness classification, four-signal combination, and the
hold-and-remind store. The hard invariant under test — nothing here ever posts.
"""
from __future__ import annotations

import pathlib
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from app import schedule as sched, security


# ---- freshness classification --------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("Breaking: OpenAI just shipped a new model today", "fast"),
    ("Anthropic announced Opus 5 moments ago", "fast"),
    ("Why most people misunderstand how agents work — a thread", "slow"),
    ("Here's what I learned building a local-first tool", "slow"),
    ("My prediction for AI over the next year", "slow"),
    ("Been using this editor for a while now", "medium"),
])
def test_classify_freshness(text, expected):
    assert sched.classify_freshness(text) == expected


def test_reactive_hook_beats_evergreen_point():
    # A reactive hook on an otherwise evergreen point dies with the reaction.
    assert sched.classify_freshness("Breaking: here's why this changes everything") == "fast"


@pytest.mark.parametrize("text", [
    # Analysis phrasing ("Why does this matter?") pegged to a launch. It reads
    # evergreen and is not — it is only as fresh as the release it is about.
    "Opus 5 is cheaper than Fable 5. It launched just two months after Opus 4.8. Why does this matter?",
    "Here's why the new pricing on their latest model changes the calculus",
    "A framework for thinking about their new model",
    "Why this benchmark result is underrated",
    "The truth about how to read a release two weeks later",
])
def test_news_pegged_analysis_is_not_evergreen(text):
    # Not "slow": an evergreen classification buys a 30h hold, and a post about
    # a three-day-old release is worthless by then.
    assert sched.classify_freshness(text) == "medium"


def test_genuinely_evergreen_analysis_stays_slow():
    # The news-peg rule must not swallow real evergreen writing.
    assert sched.classify_freshness("Why most people misunderstand how agents work — a thread") == "slow"
    assert sched.classify_freshness("Here's what I learned building a local-first tool") == "slow"


# ---- four-signal combination (timing_verdict monkeypatched) ---------------

def _now():
    return datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)


def test_rising_topic_says_post_now_never_schedule(monkeypatch):
    monkeypatch.setattr(sched, "timing_verdict", lambda **k: {
        "verdict": "post_now", "confidence": "directional", "reasons": ["rising"],
        "topic_lifecycle": {"trend": "AI agents", "lifecycle": "rising", "velocity": 0.4},
        "windows": [{"day": "Monday", "start_hour": 14, "end_hour": 16}],
    })
    rec = sched.recommend_schedule("AI agents are eating software", topic="AI agents", now=_now())
    assert rec["default"] == "post_now"
    assert rec["offer"] is False
    # No schedule target at all — holding a live conversation is actively wrong,
    # so the dock must not even offer a "Schedule instead" button.
    assert rec["target_iso"] is None
    assert "momentum" in rec["headline"].lower() or "live" in rec["headline"].lower()


def test_fading_topic_does_not_schedule(monkeypatch):
    monkeypatch.setattr(sched, "timing_verdict", lambda **k: {
        "verdict": "topic_stale", "confidence": "directional", "reasons": ["fading"],
        "topic_lifecycle": {"trend": "old news", "lifecycle": "fading", "velocity": -0.5},
        "windows": [],
    })
    rec = sched.recommend_schedule("A slow analysis of old news", topic="old news", now=_now())
    assert rec["default"] == "post_now"
    assert rec["offer"] is False
    assert rec["target_iso"] is None
    assert "fad" in rec["topic_prediction"]["read"].lower() or "past" in rec["topic_prediction"]["read"].lower()


def test_close_better_window_schedules_a_slow_draft(monkeypatch):
    target = (_now() + timedelta(hours=5)).isoformat()
    monkeypatch.setattr(sched, "timing_verdict", lambda **k: {
        "verdict": "wait_until", "wait_until": "Monday 17:00", "wait_until_iso": target,
        "next_window_iso": target, "next_window_label": "Monday 5 PM ET",
        "confidence": "directional", "reasons": ["window soon"], "windows": [],
    })
    rec = sched.recommend_schedule("Why agents matter — a thread", topic="agents", now=_now())
    assert rec["default"] == "schedule"
    assert rec["offer"] is True
    assert rec["target_iso"] == target
    assert rec["hold_hours"] == 5.0


def test_far_window_fast_draft_posts_now_and_offers_no_button(monkeypatch):
    target = (_now() + timedelta(hours=20)).isoformat()  # 20h > fast max_hold (4h)
    monkeypatch.setattr(sched, "timing_verdict", lambda **k: {
        "verdict": "wait_until", "wait_until": "Tuesday 08:00", "wait_until_iso": target,
        "next_window_iso": target, "next_window_label": "Tuesday 8 AM ET",
        "confidence": "directional", "reasons": ["window far"], "windows": [],
    })
    rec = sched.recommend_schedule("Breaking news just happened today", topic="news", now=_now())
    assert rec["default"] == "post_now"      # freshness beats a far window
    # A reactive draft would be stale by then — do NOT invite scheduling it.
    assert rec["offer"] is False
    assert rec["target_iso"] is None
    assert "stale" in rec["headline"].lower()


def test_far_window_evergreen_draft_still_offers_the_wait(monkeypatch):
    target = (_now() + timedelta(hours=20)).isoformat()  # 20h < slow max_hold (30h)
    monkeypatch.setattr(sched, "timing_verdict", lambda **k: {
        "verdict": "wait_until", "wait_until": "Tuesday 08:00", "wait_until_iso": target,
        "next_window_iso": target, "next_window_label": "Tuesday 8 AM ET",
        "confidence": "directional", "reasons": ["window far"], "windows": [],
    })
    # Evergreen "how to" draft (slow, max_hold 30h): 20h is within budget so the
    # default is actually schedule, and the button is offered.
    rec = sched.recommend_schedule("How to think about local-first design", topic="design", now=_now())
    assert rec["offer"] is True
    assert rec["target_iso"] == target


def test_over_budget_holds_even_a_fast_draft_and_names_the_tradeoff(monkeypatch):
    target = (_now() + timedelta(hours=20)).isoformat()
    monkeypatch.setattr(sched, "timing_verdict", lambda **k: {
        "verdict": "wait_until", "wait_until": "Tuesday 8 AM", "wait_until_iso": target,
        "next_window_iso": target, "next_window_label": "Tuesday 8 AM ET",
        "daily_budget": 2, "confidence": "directional", "reasons": ["over budget"], "windows": [],
    })
    rec = sched.recommend_schedule("Breaking: shipped today", topic=None, now=_now())
    # Cadence is a hard signal — over budget outranks freshness, so we do NOT
    # tell the user to post now, but we surface the staleness tradeoff honestly.
    assert rec["default"] == "schedule"
    assert "budget" in rec["headline"].lower() or "stale" in rec["headline"].lower()


def _far_window_verdict(hours_out: float):
    """A post_now verdict whose only measured window is days away — the shape
    that produced 'Post now' next to a Schedule-for-Wednesday button."""
    target = (_now() + timedelta(hours=hours_out)).isoformat()
    return lambda **k: {
        "verdict": "post_now", "confidence": "directional",
        "reasons": [f"Your next measured window is ~{hours_out:.0f}h away"],
        "windows": [{"day": "Wednesday", "start_hour": 20, "end_hour": 22}],
        "next_window_iso": target, "next_window_label": "Wednesday 8 PM UTC",
    }


def test_post_now_never_ships_with_a_schedule_target_past_the_budget(monkeypatch):
    # The regression: an evergreen-sounding draft was offered a window 89h out
    # under a headline claiming the user was already in their best window.
    monkeypatch.setattr(sched, "timing_verdict", _far_window_verdict(89))
    rec = sched.recommend_schedule("Why most people misunderstand how agents work — a thread",
                                   topic="agents", now=_now())
    assert rec["default"] == "post_now"
    assert rec["offer"] is False           # no button for a 4-day hold
    assert rec["target_iso"] is None       # and no time baked into one
    # The headline must not claim we are in the window we just said is 4 days out.
    assert "in (or near)" not in rec["headline"]
    assert "4 days away" in rec["headline"]
    # The window is still reported as the fact it is — with its distance.
    assert "Wednesday 8 PM UTC" in rec["signals"]["best_window"]
    assert "out" in rec["signals"]["best_window"]


def test_in_window_post_now_still_says_so(monkeypatch):
    # The honest version of the same headline: no target, so nothing to contradict.
    monkeypatch.setattr(sched, "timing_verdict", lambda **k: {
        "verdict": "post_now", "confidence": "directional", "reasons": ["in window"],
        "windows": [{"day": "Monday", "start_hour": 12, "end_hour": 14}],
    })
    rec = sched.recommend_schedule("Some thoughts on tooling", now=_now())
    assert rec["headline"] == "Post now — you're in (or near) your best window."
    assert rec["offer"] is False and rec["target_iso"] is None


def test_near_window_is_offered_as_a_real_second_option(monkeypatch):
    # Inside the budget, a measured window IS worth naming — but as an option,
    # not as the recommendation.
    monkeypatch.setattr(sched, "timing_verdict", _far_window_verdict(6))
    rec = sched.recommend_schedule("Why most people misunderstand how agents work — a thread",
                                   topic="agents", now=_now())
    assert rec["default"] == "post_now" and rec["offer"] is True
    assert rec["target_iso"] is not None and rec["hold_hours"] == 6.0
    assert "hold it for" in rec["headline"]


def test_peaking_topic_caps_the_hold_below_the_freshness_budget(monkeypatch):
    # The draft keeps for 30h; the conversation does not. The shorter clock wins
    # — this is the "best window is after the topic dies" case.
    target = (_now() + timedelta(hours=20)).isoformat()
    monkeypatch.setattr(sched, "timing_verdict", lambda **k: {
        "verdict": "wait_until", "wait_until": "Tuesday 08:00", "wait_until_iso": target,
        "next_window_iso": target, "next_window_label": "Tuesday 8 AM ET",
        "topic_lifecycle": {"trend": "AI agents", "lifecycle": "peaking", "velocity": 0.0},
        "confidence": "directional", "reasons": ["window far"], "windows": [],
    })
    rec = sched.recommend_schedule("Why agents matter — a thread", topic="AI agents", now=_now())
    assert rec["hold_budget"] == 10.0      # topic cap, not the 30h freshness cap
    assert rec["offer"] is False           # 20h > 10h — the wave is gone by then
    assert rec["target_iso"] is None
    assert "moved on" in rec["headline"]


def test_over_budget_hold_is_capped_at_tomorrow_not_next_week(monkeypatch):
    # Cadence outranks freshness, but "you posted too much today" justifies
    # tomorrow — never the back half of the week.
    target = (_now() + timedelta(hours=89)).isoformat()
    monkeypatch.setattr(sched, "timing_verdict", lambda **k: {
        "verdict": "wait_until", "wait_until": "Wednesday 8 PM", "wait_until_iso": target,
        "next_window_iso": target, "next_window_label": "Wednesday 8 PM UTC",
        "daily_budget": 2, "confidence": "directional", "reasons": ["over budget"], "windows": [],
    })
    rec = sched.recommend_schedule("A general post about tooling", now=_now())
    assert rec["default"] == "schedule" and rec["offer"] is True
    assert rec["hold_hours"] == 24.0 and rec["target_label"] == "tomorrow"


def test_firm_post_now_calls_forbid_manual_scheduling(monkeypatch):
    # A live conversation is the one case where picking any time is wrong.
    monkeypatch.setattr(sched, "timing_verdict", lambda **k: {
        "verdict": "post_now", "confidence": "directional", "reasons": ["rising"],
        "topic_lifecycle": {"trend": "AI agents", "lifecycle": "rising", "velocity": 0.4},
        "windows": [],
    })
    assert sched.recommend_schedule("AI agents are eating software", topic="AI agents",
                                    now=_now())["allow_manual"] is False
    # An ordinary post-now call still lets the user choose their own time.
    monkeypatch.setattr(sched, "timing_verdict", _far_window_verdict(89))
    assert sched.recommend_schedule("A general post", now=_now())["allow_manual"] is True


def test_recommendation_is_always_hold_and_remind(monkeypatch):
    monkeypatch.setattr(sched, "timing_verdict", lambda **k: {
        "verdict": "post_now", "confidence": "limited", "reasons": [], "windows": [],
    })
    rec = sched.recommend_schedule("some post", now=_now())
    assert "never posts" in rec["note"].lower()
    assert "post it yourself" in rec["reason"].lower()
    # Never promises automation.
    assert "auto-post" not in rec["reason"].lower()
    # The four signals are all present and named for the card.
    assert set(rec["signals"]) == {"best_window", "topic_momentum", "freshness", "cadence"}


# ---- storage round-trip (temp DB) ----------------------------------------

@pytest.fixture()
def temp_db(monkeypatch):
    from app import storage
    tmp = pathlib.Path(tempfile.mkdtemp()) / "sched.db"
    monkeypatch.setattr(storage, "DB_PATH", tmp)
    storage.initialize()
    return storage


def test_scheduled_draft_round_trip(temp_db):
    storage = temp_db
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    future = datetime.now(timezone.utc) + timedelta(hours=5)
    due_id = storage.save_scheduled_draft({"target_at": past, "target_label": "due", "text": "ready", "freshness": "slow"})
    storage.save_scheduled_draft({"target_at": future, "target_label": "later", "text": "hold",
                                  "image": {"url": "http://x/y.jpg", "credit": "me"}})

    scheduled = storage.list_scheduled_drafts("scheduled")
    assert [d["target_label"] for d in scheduled] == ["due", "later"]  # soonest first
    assert scheduled[1]["image"] == {"url": "http://x/y.jpg", "credit": "me"}

    due = storage.due_scheduled_drafts()
    assert [d["text"] for d in due] == ["ready"]

    storage.mark_scheduled_draft(due_id, "posted")
    assert [d["target_label"] for d in storage.list_scheduled_drafts("scheduled")] == ["later"]
    posted = storage.list_scheduled_drafts("posted")
    assert posted[0]["status"] == "posted" and posted[0]["posted_at"]


def test_scheduled_draft_terminal_transition_is_one_way(temp_db):
    storage = temp_db
    draft_id = storage.save_scheduled_draft({
        "target_at": datetime.now(timezone.utc) + timedelta(hours=2),
        "text": "A held draft",
    })

    assert storage.mark_scheduled_draft(draft_id, "posted") is True
    # A retry, another open tab, or a stale Dismiss click cannot rewrite the
    # author's confirmation or pretend it updated a record that is gone.
    assert storage.mark_scheduled_draft(draft_id, "dismissed") is False
    assert storage.mark_scheduled_draft(999_999, "dismissed") is False
    assert storage.list_scheduled_drafts("posted")[0]["id"] == draft_id
    assert storage.list_scheduled_drafts("dismissed") == []


def test_due_is_empty_when_nothing_is_ripe(temp_db):
    storage = temp_db
    future = datetime.now(timezone.utc) + timedelta(hours=3)
    storage.save_scheduled_draft({"target_at": future, "text": "not yet"})
    assert storage.due_scheduled_drafts() == []


# ---- API command dispatch (temp DB) --------------------------------------

@pytest.fixture()
def api(monkeypatch):
    from app import storage, main
    tmp = pathlib.Path(tempfile.mkdtemp()) / "api.db"
    monkeypatch.setattr(storage, "DB_PATH", tmp)
    from fastapi.testclient import TestClient
    client = TestClient(main.app)
    client.__enter__()
    token = security.ensure_token()
    return client, {"Authorization": f"Bearer {token}"}


def test_schedule_command_save_requires_text_and_target(api):
    client, headers = api
    tgt = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
    # empty text rejected
    r = client.post("/browser/assist", headers=headers,
                    json={"mode": "schedule", "schedule": {"op": "save", "text": "", "target_at": tgt}})
    assert r.status_code == 400
    # missing target rejected
    r = client.post("/browser/assist", headers=headers,
                    json={"mode": "schedule", "schedule": {"op": "save", "text": "hi"}})
    assert r.status_code == 400


@pytest.mark.parametrize("target", [
    "not-a-time",
    "2026-07-20T12:00:00",  # no timezone means the user's intended hour is ambiguous
    (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
])
def test_schedule_command_rejects_ambiguous_or_past_targets(api, target):
    client, headers = api
    r = client.post("/browser/assist", headers=headers, json={"mode": "schedule", "schedule": {
        "op": "save", "text": "Held draft", "target_at": target,
    }})
    assert r.status_code == 400


def test_schedule_command_full_lifecycle(api):
    client, headers = api
    tgt = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
    r = client.post("/browser/assist", headers=headers, json={"mode": "schedule", "schedule": {
        "op": "save", "text": "Held draft", "target_at": tgt, "target_label": "Tue 2 PM", "freshness": "slow"}})
    assert r.status_code == 200 and r.json()["ok"]
    saved = r.json()["scheduled"]
    sid = saved["id"]
    assert saved["status"] == "scheduled"
    assert saved["action"] == "post"
    assert saved["text"] == "Held draft"
    assert datetime.fromisoformat(saved["target_at"]) == datetime.fromisoformat(tgt)

    r = client.post("/browser/assist", headers=headers, json={"mode": "schedule", "schedule": {"op": "list"}})
    assert len(r.json()["drafts"]) == 1

    r = client.post("/browser/assist", headers=headers,
                    json={"mode": "schedule", "schedule": {"op": "mark_posted", "draft_id": sid}})
    assert r.json()["status"] == "posted"

    # The status update is acknowledged exactly once. A stale second action is
    # an error, not a false success that would make a dock card disappear.
    r = client.post("/browser/assist", headers=headers,
                    json={"mode": "schedule", "schedule": {"op": "dismiss", "draft_id": sid}})
    assert r.status_code == 200
    assert r.json()["ok"] is False

    r = client.post("/browser/assist", headers=headers, json={"mode": "schedule", "schedule": {"op": "list"}})
    assert r.json()["drafts"] == []


def test_flat_clock_and_thin_history_are_told_apart(monkeypatch):
    """'No window' has two very different causes and one wrong shared answer.

    Telling a user with 35 dated posts to "post and measure" is advice for a
    problem they do not have — their history is fine, it just shows no clock
    effect. Timing is not their lever, and the card should say so.
    """
    monkeypatch.setattr(sched, "timing_verdict", lambda **k: {
        "verdict": "post_now", "confidence": "limited", "reasons": [], "windows": [],
        "dated_posts": 35,
        "window_reason": "No hour in your 35 dated posts reliably beats your own average — your results "
                         "vary more by what you post than by when.",
    })
    flat = sched.recommend_schedule("A general post about tooling", now=_now())
    assert "no better moment to wait for" in flat["headline"]
    assert "beats your own average" in flat["signals"]["best_window"]
    assert flat["offer"] is False and flat["target_iso"] is None

    monkeypatch.setattr(sched, "timing_verdict", lambda **k: {
        "verdict": "post_now", "confidence": "limited", "reasons": [], "windows": [],
        "dated_posts": 4,
        "window_reason": "At least 30 dated posts with this metric are needed before recommending a window.",
    })
    thin = sched.recommend_schedule("A general post about tooling", now=_now())
    assert "not enough history" in thin["headline"]


def test_a_moderate_read_is_not_dressed_up_as_directional(monkeypatch):
    # "moderate" reaches here from the window read and was falling through the
    # confidence map onto the directional wording.
    monkeypatch.setattr(sched, "timing_verdict", lambda **k: {
        "verdict": "post_now", "confidence": "moderate", "reasons": [], "windows": [],
    })
    rec = sched.recommend_schedule("A general post", now=_now())
    assert rec["confidence"] == "moderate"
    assert "moderate read" in rec["reason"]


# ---- whole-matrix consistency --------------------------------------------

_SWEEP_DRAFTS = {
    "fast":   "Breaking: they just shipped it today",
    "medium": "Been using this editor for a while now",
    "newsy":  "Opus 5 is cheaper than Fable 5. It launched two months after 4.8. Why does this matter?",
    "slow":   "Why most people misunderstand how agents work — a thread",
}


def _sweep_verdict(lifecycle, hours, kind, budget):
    verdict = {
        "verdict": "topic_stale" if lifecycle == "fading" and kind == "post_now" else kind,
        "confidence": "directional", "reasons": ["x"],
        "windows": [{"day": "Wednesday", "start_hour": 20, "end_hour": 22}] if hours else [],
        "dated_posts": 35, "window_reason": "n/a",
    }
    if lifecycle:
        verdict["topic_lifecycle"] = {"trend": "Some Trend", "lifecycle": lifecycle, "velocity": 0.3}
    if hours is not None:
        target = (_now() + timedelta(hours=hours)).isoformat()
        verdict["next_window_iso"] = target
        verdict["next_window_label"] = "Wednesday 8 PM UTC"
        if kind == "wait_until":
            verdict["wait_until_iso"] = target
            verdict["wait_until"] = "Wednesday 20:00"
    if budget and kind == "wait_until":
        verdict["daily_budget"] = budget
    return verdict


@pytest.mark.parametrize("draft_key", sorted(_SWEEP_DRAFTS))
def test_the_card_can_never_contradict_itself(draft_key, monkeypatch):
    """Every reachable combination of the four signals, checked for coherence.

    The original bug was not a bad number but a self-contradicting card: a
    "post now" headline shipped alongside a Schedule-for-Wednesday button. That
    class of fault only shows up when the branches are crossed against each
    other, so cross them all.
    """
    draft = _SWEEP_DRAFTS[draft_key]
    for lifecycle in (None, "rising", "peaking", "fading"):
        for hours in (None, 0.2, 2, 6, 11, 20, 31, 89, 400):
            for kind in ("post_now", "wait_until"):
                for budget in (None, 2):
                    monkeypatch.setattr(sched, "timing_verdict",
                                        lambda _v=_sweep_verdict(lifecycle, hours, kind, budget), **k: _v)
                    r = sched.recommend_schedule(draft, action="post", topic="Some Trend", now=_now())
                    where = f"{draft_key}/{lifecycle}/{hours}h/{kind}/budget={budget}: {r['headline'][:70]}"

                    # A held draft never outlives the allowance we reported for it.
                    if r["hold_hours"] is not None:
                        assert r["hold_hours"] <= r["hold_budget"] + 1e-9, where
                    # A button and a time exist together or not at all.
                    assert bool(r["target_iso"]) == bool(r["target_label"]), where
                    assert not (r["offer"] and not r["target_iso"]), where
                    assert not (r["target_iso"] and not (r["offer"] or r["default"] == "schedule")), where
                    assert not (r["default"] == "schedule" and not r["target_iso"]), where
                    # A firm post-now call leaves no scheduling path open at all.
                    assert not (r["allow_manual"] is False and r["offer"]), where
                    # The exact regression: claiming to be in a window while
                    # naming one that is hours or days out.
                    if "in (or near)" in r["headline"]:
                        assert r["hold_hours"] is None or r["hold_hours"] <= 2, where
                    # The headline says what the recommendation is.
                    if r["default"] == "schedule":
                        assert not r["headline"].startswith("Post now —"), where
                    else:
                        assert not r["headline"].startswith("Schedule for"), where
                    # Nothing renders a missing value into the user's face.
                    assert "None" not in r["headline"], where
                    # A live conversation is never quietly held.
                    if lifecycle == "rising" and r["default"] == "post_now":
                        assert not r["offer"], where
