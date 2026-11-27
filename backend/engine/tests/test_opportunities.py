

# ------------------------------------------------ the decay and room reads
def _hot_ctx(now, minutes_old, views, n_replies, newest_reply_min, text=None):
    from datetime import timedelta
    from app.schemas import PostContext
    return PostContext(
        author="OpenAI", author_handle="openai",
        text=text or ("Announcing our new model: twice the context at half the "
                      "price. How are teams planning to use the longer context?"),
        post_created_at=(now - timedelta(minutes=minutes_old)).isoformat(),
        visible_metrics={"views": views, "replies": n_replies},
        visible_reply_count=n_replies,
        visible_reply_ages_minutes=[newest_reply_min + i * 9.0
                                    for i in range(min(n_replies, 8))])


def test_a_quiet_thread_revokes_still_hot():
    """Views-per-hour is cumulative: a post that did 500K in hour one and died
    still averages 100K/h at hour five. The newest visible reply being 3h old
    is the direct evidence, and it outranks the average."""
    from datetime import datetime, timezone
    from app.opportunities import assess_freshness
    now = datetime.now(timezone.utc)
    f = assess_freshness(_hot_ctx(now, 300, 500_000, 8, 180), now)
    assert f["dying"] and not f["still_hot"]
    assert "went quiet" in f["stats_line"]


def test_a_dead_giant_is_a_wait_not_a_reply():
    from datetime import datetime, timezone
    from app.opportunities import assess_opportunity
    now = datetime.now(timezone.utc)
    verdict = assess_opportunity(_hot_ctx(now, 300, 500_000, 8, 180), now)
    assert verdict["action"] == "wait"


def test_a_fresh_big_room_replies_and_says_why():
    from datetime import datetime, timezone
    from app.opportunities import assess_freshness, assess_opportunity
    now = datetime.now(timezone.utc)
    f = assess_freshness(_hot_ctx(now, 25, 40_000, 6, 2), now)
    assert f["bracket"] == "prime" and f["alive"] and f["room"] == "huge"
    assert assess_opportunity(_hot_ctx(now, 25, 40_000, 6, 2), now)["action"] == "reply"


def test_a_tiny_room_is_named_and_skipped():
    """200 views an hour is an empty hallway — even a good reply reaches
    nobody. The room size is said out loud so the user knows why."""
    from datetime import datetime, timezone
    from app.opportunities import assess_freshness, assess_opportunity
    now = datetime.now(timezone.utc)
    f = assess_freshness(_hot_ctx(now, 180, 220, 0, 0), now)
    assert f["room"] == "tiny"
    assert "room" in f["stats_line"]
    assert assess_opportunity(_hot_ctx(now, 180, 220, 0, 0), now)["action"] == "wait"


def test_no_visible_replies_means_decay_unknown_not_dying():
    from datetime import datetime, timezone
    from app.opportunities import assess_freshness
    now = datetime.now(timezone.utc)
    f = assess_freshness(_hot_ctx(now, 60, 30_000, 0, 0), now)
    assert not f["dying"] and f["newest_reply_minutes"] is None


def test_a_thinning_thread_is_read_as_past_its_peak():
    """The derivative, not just the silence: newest replies spaced wider than
    older ones = dying down even while technically active."""
    from datetime import datetime, timezone
    from app.opportunities import assess_freshness
    now = datetime.now(timezone.utc)
    ctx = _hot_ctx(now, 90, 30_000, 6, 20)
    ctx = ctx.model_copy(update={"visible_reply_ages_minutes": [20, 40, 60, 70, 75, 80]})
    f = assess_freshness(ctx, now)
    assert f["trend"] == "decelerating"
    assert "thinning out" in f["stats_line"]


def test_a_broadcast_room_leans_quote_a_conversation_room_leans_reply():
    from datetime import datetime, timezone
    from app.opportunities import assess_freshness
    now = datetime.now(timezone.utc)
    broadcast = _hot_ctx(now, 60, 200_000, 12, 5)
    conversation = _hot_ctx(now, 60, 8_000, 60, 3)
    assert assess_freshness(broadcast, now)["shape"] == "broadcast"
    assert assess_freshness(conversation, now)["shape"] == "conversation"


def test_a_crowded_late_thread_is_penalized_a_first_mover_window_is_not():
    from datetime import datetime, timezone
    from app.opportunities import assess_freshness
    now = datetime.now(timezone.utc)
    crowded = _hot_ctx(now, 150, 50_000, 40, 10)
    early = _hot_ctx(now, 20, 9_000, 2, 4)
    f1, f2 = assess_freshness(crowded, now), assess_freshness(early, now)
    assert f1["crowded"] and not f1["first_mover"]
    assert f2["first_mover"] and not f2["crowded"]


def test_a_co_signable_post_is_favored_and_says_why():
    """Reply engagement is mostly people agreeing — a reply that lands WITH
    the room gets co-signed. The planner's deterministic stance is reused,
    never re-derived."""
    from datetime import datetime, timezone
    from app.opportunities import assess_opportunity
    now = datetime.now(timezone.utc)
    firsthand = _hot_ctx(now, 30, 20_000, 6, 4,
                         text="Ran Qwen locally for a month. Memory bandwidth was the "
                              "whole story — the 30B beat the 70B on my single card.")
    verdict = assess_opportunity(firsthand, now)
    assert verdict["action"] == "reply"
    joined = " ".join(verdict.get("reasons", []) or []) + str(verdict)
    assert "co-signed" in joined or "lands WITH the room" in joined


def test_a_correction_post_is_flagged_as_engagement_risk():
    from datetime import datetime, timezone
    from app.opportunities import assess_opportunity
    now = datetime.now(timezone.utc)
    wrong_lab = _hot_ctx(now, 30, 20_000, 6, 4,
                         text="Google's new Claude Opus 5 model is half the price now.")
    verdict = assess_opportunity(wrong_lab, now)
    joined = " ".join(verdict.get("reasons", []) or []) + str(verdict)
    assert "argue with the post" in joined or "set a fact straight" in joined
