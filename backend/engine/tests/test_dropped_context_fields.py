"""Fields the extension sends that the schema silently threw away.

Pydantic ignores unknown keys, so a field can be computed in the DOM, put on
the wire, and vanish without any error anywhere. Six were arriving and being
dropped: post_type, is_long_post, poll_options, page_url, captured_at (and the
duplicate is_sensitive).

Two of them cost real behaviour:

  post_type     the extension infers post/reply/quote from the DOM. The archive
                reads 31 posts, 3 replies, 0 quotes — which is why per-surface
                voice learning has nothing to learn a reply or quote voice from.
  is_long_post  the post is cut off behind "Show more". Without it a long
                argument whose visible opening is setup reads as a thin post,
                and the reply answers the setup instead of the point.
"""
from __future__ import annotations

from app.analyzer import _angle_space, _reply_surface
from app.schemas import PostContext


def test_the_schema_accepts_what_the_extension_sends():
    context = PostContext(author="a", visibility="public", text="t",
                          post_type="quote", is_long_post=True,
                          poll_options=["yes", "no"],
                          page_url="https://x.com/i/status/1",
                          captured_at="2026-07-26T05:00:00Z")
    assert context.post_type == "quote"
    assert context.is_long_post is True
    assert context.poll_options == ["yes", "no"]
    assert context.captured_at == "2026-07-26T05:00:00Z"


def _surface(**kw):
    context = PostContext(author="x", author_handle="x", visibility="public", **kw)
    angles, anti = _angle_space(context, {})
    return _reply_surface(context, 20.0, {}, angles, anti, 0)


def test_a_truncated_post_says_so_instead_of_reading_as_empty():
    band, reason = _surface(text="This is the setup and then it stops right",
                            is_long_post=True)
    assert band == "directional"
    assert "cut off" in reason.lower() and "show more" in reason.lower()


def test_the_truncation_check_runs_before_the_thin_post_check():
    """Being cut off is exactly what makes a post LOOK thin."""
    band, reason = _surface(text="Here is the thing", is_long_post=True)
    assert "nothing specific" not in reason.lower()


def test_a_genuinely_short_post_is_still_called_thin():
    band, reason = _surface(text="big if true")
    assert band == "weak"
    assert "nothing specific" in reason.lower()


# ---------------------------------------------------------------------------
# The audience printed on the screen. Momentum works from a snapshot SERIES,
# which only exists for posts Starling has watched over time — never for a
# stranger's. So a post showing 1.6M views reported "movement cannot be
# confirmed", exactly as if nothing at all were known about it, and the reply
# decision could not tell a viral thread from a dead one.
#
# Velocity genuinely does need a series. SIZE does not, and size is most of
# what matters when deciding whether to walk into a room.
# ---------------------------------------------------------------------------
def test_a_visible_audience_is_known_even_without_history():
    from app.analyzer import analyze
    viral = PostContext(author="Claude", author_handle="claudeai",
                        visibility="public", post_id="viral-1",
                        text="Claude Opus 5 is here at half the price.",
                        visible_metrics={"views": 1_600_000, "replies": 1700},
                        thread_scanned=True, visible_reply_count=1700)
    read = analyze(viral, profile={"version": 1}, recent_user_texts=[])
    assert read.momentum.get("visible_views") == 1_600_000
    assert read.momentum.get("trend") == "large_audience"
    assert any("1,600,000 views are visible" in u for u in read.unknowns)


def test_it_still_says_movement_is_unconfirmed():
    """Honesty matters more than the number: size is not velocity."""
    from app.analyzer import analyze
    viral = PostContext(author="a", author_handle="a", visibility="public",
                        post_id="viral-2", text="A big launch post.",
                        visible_metrics={"views": 900_000})
    read = analyze(viral, profile={"version": 1}, recent_user_texts=[])
    assert read.momentum.get("basis") in ("none", "average")
    assert any("movement is not" in u for u in read.unknowns)


def test_a_small_post_is_not_promoted_to_a_large_audience():
    from app.analyzer import analyze
    small = PostContext(author="d", author_handle="d", visibility="public",
                        post_id="small-1", text="Been running Qwen locally.",
                        visible_metrics={"views": 300})
    read = analyze(small, profile={"version": 1}, recent_user_texts=[])
    assert read.momentum.get("trend") != "large_audience"
