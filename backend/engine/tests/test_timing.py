"""Timing verdicts: personal windows + live trend lifecycle + today's cadence
fuse into one concrete, honest call."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.storage import save_trend_snapshots, set_account_profile, upsert_post
from app.timing import timing_verdict


def _seed_windowed_history(peak_weekday: int, peak_hour: int, count: int = 40) -> None:
    """A history where one hour genuinely outperforms the rest.

    The contrast is the point: posts AT the peak hour do well, posts at other
    hours do badly. A history stacked entirely into a single hour looks
    concentrated but proves nothing — with no other hour to compare against,
    that bucket's mean is the account mean, and calling it a "best window" is
    a statement the data cannot support.
    """
    set_account_profile({"audience": "AI builders", "content_pillars": ["AI"], "timezone": "UTC"})
    now = datetime.now(timezone.utc)
    for index in range(count):
        at_peak = index % 2 == 0
        # Walk back week by week to hit the same weekday repeatedly.
        created = (now - timedelta(weeks=index % 10, days=(now.weekday() - peak_weekday) % 7))
        if at_peak:
            created = created.replace(hour=peak_hour, minute=5, second=0, microsecond=0)
        else:
            # Scatter the rest across other hours of the same weekday.
            created = created.replace(hour=(peak_hour + 3 + (index % 5)) % 24, minute=5,
                                      second=0, microsecond=0)
        upsert_post({
            "post_id": f"win-{index}",
            "text": f"Post {index} about AI systems and agents in production settings today.",
            "post_type": "post",
            "created_at": created.isoformat(),
            "metrics": {"views": 30_000 if at_peak else 500, "likes": 40, "replies": 6},
            "metrics_updated_at": (created + timedelta(days=1)).isoformat(),
        })


def test_verdict_is_honest_with_no_history():
    set_account_profile({"audience": "AI builders", "content_pillars": ["AI"], "timezone": "UTC"})
    verdict = timing_verdict("post")
    assert verdict["verdict"] == "post_now"
    assert verdict["confidence"] == "limited"
    assert verdict["reasons"]
    assert "guarantee" in verdict["note"]


def test_rising_topic_overrides_the_calendar():
    now = datetime.now(timezone.utc)
    set_account_profile({"audience": "AI builders", "content_pillars": ["AI"], "timezone": "UTC"})
    for minutes in (5, 15, 30):
        save_trend_snapshots("https://x.com/explore", [
            {"name": "Agent Frameworks", "category": "Technology", "volume_text": "30K posts", "rank": 1},
        ], captured_at=(now - timedelta(minutes=minutes)).isoformat())
    verdict = timing_verdict("post", topic="Agent Frameworks")
    assert verdict["verdict"] == "post_now"
    assert verdict["topic_lifecycle"]["lifecycle"] == "rising"
    assert any("rising" in reason for reason in verdict["reasons"])


def test_fading_topic_is_called_stale():
    now = datetime.now(timezone.utc)
    set_account_profile({"audience": "AI builders", "content_pillars": ["AI"], "timezone": "UTC"})
    for minutes in (300, 330, 360, 390):
        save_trend_snapshots("https://x.com/explore", [
            {"name": "Yesterday Drama", "category": "Technology", "volume_text": "90K posts", "rank": 1},
        ], captured_at=(now - timedelta(minutes=minutes)).isoformat())
    verdict = timing_verdict("post", topic="Yesterday Drama")
    assert verdict["verdict"] == "topic_stale"
    assert any("moment" in reason for reason in verdict["reasons"])


def _midday() -> datetime:
    """A fixed midday clock keeps N-hours-ago fixtures inside one UTC date
    regardless of when the suite runs."""
    return datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)


def test_heavy_same_day_cadence_waits():
    set_account_profile({"audience": "AI builders", "content_pillars": ["AI"], "timezone": "UTC"})
    now = _midday()
    for index in range(3):
        upsert_post({
            "post_id": f"today-{index}",
            "text": f"Original post number {index} from earlier today about AI tooling choices.",
            "post_type": "post",
            "created_at": (now - timedelta(hours=index + 1)).isoformat(),
        })
    verdict = timing_verdict("post", now=now)
    assert verdict["verdict"] == "wait_until"
    assert any("already dated today" in reason for reason in verdict["reasons"])


def _seed_cadence_history(weak_from_position: int) -> None:
    """Many days of history where post #weak_from_position of the day tanks."""
    set_account_profile({"audience": "AI builders", "content_pillars": ["AI"], "timezone": "UTC"})
    now = datetime.now(timezone.utc)
    for day in range(1, 15):
        for position in range(1, 5):
            created = (now - timedelta(days=day)).replace(hour=8 + position * 2, minute=0, second=0, microsecond=0)
            strong = position < weak_from_position
            upsert_post({
                "post_id": f"cad-{day}-{position}",
                "text": f"Cadence history day {day} position {position} about AI tooling and evaluation work.",
                "post_type": "post",
                "created_at": created.isoformat(),
                "metrics": {"views": 20_000 if strong else 300, "likes": 25, "replies": 4},
                "metrics_updated_at": (created + timedelta(days=1)).isoformat(),
            })


def test_cadence_curve_finds_personal_underperformance_position():
    from app.performance import cadence_curve, daily_post_budget
    _seed_cadence_history(weak_from_position=3)
    curve = cadence_curve()
    assert curve["underperform_after"] == 3
    budget, evidence = daily_post_budget()
    assert budget == 2  # stop BEFORE the position that underperforms
    assert evidence and "your own history" in evidence


def test_personalized_budget_drives_timing_verdict():
    _seed_cadence_history(weak_from_position=3)
    now = _midday()
    for index in range(2):  # only 2 posts today — under the old hardcoded 3
        upsert_post({
            "post_id": f"today-cad-{index}",
            "text": f"Today's post {index} about AI tooling decisions and evaluations in production.",
            "post_type": "post",
            "created_at": (now - timedelta(hours=index + 1)).isoformat(),
        })
    verdict = timing_verdict("post", now=now)
    assert verdict["verdict"] == "wait_until"
    assert verdict["daily_budget"] == 2
    assert any("Measured on this account" in reason for reason in verdict["reasons"])


def test_ten_posts_today_is_a_blunt_hard_stop():
    set_account_profile({"audience": "AI builders", "content_pillars": ["AI"], "timezone": "UTC"})
    now = _midday()
    for index in range(10):
        upsert_post({
            "post_id": f"spam-{index}",
            "text": f"Original post number {index} today about AI tools and their evaluation in production.",
            "post_type": "post",
            "created_at": (now - timedelta(minutes=30 * (index + 1))).isoformat(),
        })
    verdict = timing_verdict("post", now=now)
    assert verdict["verdict"] == "wait_until"
    assert verdict["severity"] == "hard"
    assert verdict["wait_until"] == "tomorrow"
    assert verdict["confidence"] == "strong"
    assert any("Hard stop" in reason for reason in verdict["reasons"])


def test_budget_floor_protects_against_thin_buckets():
    """A position-2 dip with only 8-11 samples must not clamp the budget to 1."""
    from app.performance import daily_post_budget
    set_account_profile({"audience": "AI builders", "content_pillars": ["AI"], "timezone": "UTC"})
    now = datetime.now(timezone.utc)
    for day in range(1, 11):  # 10 days × 2 posts → position-2 bucket has 10 samples
        for position in (1, 2):
            created = (now - timedelta(days=day)).replace(hour=8 + position * 3, minute=0, second=0, microsecond=0)
            upsert_post({
                "post_id": f"floor-{day}-{position}",
                "text": f"Floor test day {day} position {position} about AI systems in production environments.",
                "post_type": "post",
                "created_at": created.isoformat(),
                "metrics": {"views": 20_000 if position == 1 else 200, "likes": 10, "replies": 1},
                "metrics_updated_at": (created + timedelta(days=1)).isoformat(),
            })
    budget, _ = daily_post_budget()
    assert budget == 2  # floored — 10 samples is not enough evidence for a budget of 1


def test_thin_history_falls_back_to_default_budget():
    from app.performance import daily_post_budget
    set_account_profile({"audience": "AI builders", "content_pillars": ["AI"], "timezone": "UTC"})
    budget, evidence = daily_post_budget()
    assert budget == 3
    assert evidence is None


def test_windowed_history_produces_actionable_verdict():
    now = datetime.now(timezone.utc)
    _seed_windowed_history(peak_weekday=now.weekday(), peak_hour=now.hour)
    verdict = timing_verdict("post")
    # Inside the strongest measured window right now → post_now with the window named.
    assert verdict["verdict"] in ("post_now", "wait_until")
    assert verdict["windows"]
    # The window's own evidence, not the size of the whole history.
    if any("measured best window" in reason for reason in verdict["reasons"]):
        assert not any("from 40 posts" in reason for reason in verdict["reasons"])


def test_topic_matching_ignores_filler_words():
    """A shared 'the' is not a shared subject.

    _topic_lifecycle accepted any token of 3+ characters, so 'the' matched and
    an unrelated trend's lifecycle was applied to the draft — deciding post-now
    vs stale vs refuse-to-schedule off a word that names nothing.
    """
    now = datetime.now(timezone.utc)
    set_account_profile({"audience": "AI builders", "content_pillars": ["AI"], "timezone": "UTC"})
    for minutes in (300, 330, 360, 390):
        save_trend_snapshots("https://x.com/explore", [
            {"name": "The Weeknd", "category": "Music", "volume_text": "90K posts", "rank": 1},
        ], captured_at=(now - timedelta(minutes=minutes)).isoformat())
    # "The Weeknd" is fading; if it matched, this would come back topic_stale.
    verdict = timing_verdict("post", topic="the truth about local models")
    assert verdict["verdict"] != "topic_stale"
    assert "topic_lifecycle" not in verdict

    # A real subject overlap still matches.
    matched = timing_verdict("post", topic="what Weeknd fans want")
    assert matched["verdict"] == "topic_stale"


def test_an_unmeasurable_lifecycle_is_not_treated_as_a_reading():
    """A trend we could not classify must not stand in for one we could."""
    now = datetime.now(timezone.utc)
    set_account_profile({"audience": "AI builders", "content_pillars": ["AI"], "timezone": "UTC"})
    save_trend_snapshots("https://x.com/explore", [
        {"name": "Kimi K3", "category": "Technology", "volume_text": "10K posts", "rank": 1},
    ], captured_at=(now - timedelta(minutes=10)).isoformat())
    verdict = timing_verdict("post", topic="Kimi K3")
    # Seen once → lifecycle unknown → no momentum claim either way.
    assert "topic_lifecycle" not in verdict
    assert verdict["verdict"] in ("post_now", "wait_until")


def _seed_conversation_only_window(peak_hour: int) -> None:
    """History where one hour wins on REPLIES but nothing wins on reach.

    This is the real shape of an account whose impressions are driven by what it
    posts rather than when: reach is flat across the clock, but there is still a
    clear hour when the audience answers.
    """
    set_account_profile({"audience": "AI builders", "content_pillars": ["AI"], "timezone": "UTC"})
    now = datetime.now(timezone.utc)
    for index in range(40):
        at_peak = index % 2 == 0
        created = (now - timedelta(weeks=index % 10, days=1))
        created = created.replace(hour=peak_hour if at_peak else (peak_hour + 4 + index % 3) % 24,
                                  minute=5, second=0, microsecond=0)
        upsert_post({
            "post_id": f"conv-{index}",
            "text": f"Post {index} about AI systems and agents in production settings.",
            "post_type": "post",
            "created_at": created.isoformat(),
            # Reach identical everywhere; replies concentrated in the peak hour.
            "metrics": {"views": 8_000, "likes": 20, "replies": 40 if at_peak else 1},
            "metrics_updated_at": (created + timedelta(days=1)).isoformat(),
        })


def test_a_post_borrows_the_reply_window_when_reach_shows_nothing():
    from app.performance import timing_recommendations
    from app.storage import performance_records
    _seed_conversation_only_window(peak_hour=20)
    rows = performance_records()
    # Precondition: reach really does have nothing to say about the clock.
    assert timing_recommendations("reach", "UTC", rows)["windows"] == []
    assert timing_recommendations("conversation", "UTC", rows)["windows"]

    verdict = timing_verdict("post")
    assert verdict["windows"], "a post should fall back to the conversation ranking"
    assert verdict["window_objective"] == "conversation"


def test_a_borrowed_window_is_never_passed_off_as_a_reach_window():
    """The fallback answers a different question and has to say so.

    Presenting a reply-ranked hour as "your measured best window" is the same
    class of fault as naming a below-average hour: a claim the data does not
    support, stated with confidence.
    """
    from app import schedule as sched
    _seed_conversation_only_window(peak_hour=(datetime.now(timezone.utc).hour))
    verdict = timing_verdict("post")
    if any("window" in reason for reason in verdict["reasons"]):
        assert any("replies" in reason or "conversation, not reach" in reason
                   for reason in verdict["reasons"]), verdict["reasons"]
    rec = sched.recommend_schedule("A general post about tooling", action="post")
    assert "replies" in rec["signals"]["best_window"]
    assert "conversation, not reach" in rec["signals"]["best_window"]


def test_a_real_reach_window_is_not_replaced_by_the_fallback():
    now = datetime.now(timezone.utc)
    _seed_windowed_history(peak_weekday=now.weekday(), peak_hour=now.hour)
    verdict = timing_verdict("post")
    assert verdict["window_objective"] == "reach"


def test_a_reply_still_uses_conversation_directly():
    _seed_conversation_only_window(peak_hour=20)
    assert timing_verdict("reply")["window_objective"] == "conversation"
