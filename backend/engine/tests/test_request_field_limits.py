"""Every field a stranger can fill has a finite limit, including list ITEMS and free-form dicts.

Found by the security review. `thread_text` was capped at 60 items of ANY length, and
`poll_options`, `links`, the ids, the urls and every `metadata` dictionary had no limit at all, so the
only bound on hostile text reaching the safety scan, the prompt and the customer's database was the
1 MiB body limit. The limits are generous against the real thing (X's longest post is 25,000
characters) and finite.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas import Feedback, ImageFeedback, MediaItem, MetricSnapshot, OutcomeRecord, PostContext, ProfileSnapshot

HUGE = "A" * 200_000


@pytest.mark.parametrize("payload", [
    {"thread_text": [HUGE]},
    {"poll_options": [HUGE]},
    {"poll_options": ["option"] * 9},
    {"links": ["https://example.com/" + HUGE]},
    {"post_id": "9" * 5_000},
    {"post_url": "https://x.com/" + HUGE},
    {"conversation_id": HUGE},
    {"parent_post_id": HUGE},
    {"quoted_post_id": HUGE},
    {"page_url": "https://x.com/" + HUGE},
    {"post_type": HUGE},
    {"language": HUGE},
    {"captured_at": HUGE},
    {"post_created_at": HUGE},
    {"poll": {"question": "A" * 300_000}},
    {"visible_metrics": {f"metric{i}": "v" * 250 for i in range(2_000)}},
    {"media": [{"url": "https://pbs.twimg.com/" + HUGE, "kind": "image"}]},
    {"media": [{"url": "https://pbs.twimg.com/a.jpg", "kind": "image", "alt": HUGE}]},
])
def test_no_field_of_a_post_is_unbounded(payload):
    with pytest.raises(ValidationError):
        PostContext(author="stranger", text="hello", **payload)


def test_the_longest_real_post_and_thread_still_fit():
    post = PostContext(
        author="stranger", text="x" * 25_000, parent_text="y" * 25_000, quote_text="z" * 25_000,
        thread_text=["t" * 25_000] * 3, links=["https://example.com/" + "a" * 1_500] * 40,
        poll_options=["Yes", "No", "Maybe", "Results"], poll={"ends_at": "2026-10-01T00:00:00Z", "votes": 1200},
        visible_metrics={"likes": 3.0, "views": "1.2K", "replies": None},
        media=[MediaItem(url="https://pbs.twimg.com/media/abc.jpg", kind="image", alt="a chart " * 100)],
        post_id="1900000000000000001", post_url="https://x.com/someone/status/1900000000000000001",
        page_url="https://x.com/someone/status/1900000000000000001", captured_at="2026-09-20T01:02:03.456Z",
    )
    assert len(post.thread_text) == 3 and len(post.links) == 40


@pytest.mark.parametrize("build", [
    lambda: Feedback(candidate_id="c1", action="edited", final_text=HUGE),
    lambda: Feedback(candidate_id="c1", action="edited", metadata={"note": "A" * 300_000}),
    lambda: Feedback(candidate_id=HUGE, action="edited"),
    lambda: ProfileSnapshot(bio=HUGE),
    lambda: ProfileSnapshot(metadata={"note": "A" * 300_000}),
    lambda: ProfileSnapshot(handle=HUGE),
    lambda: MetricSnapshot(post_id="1", post_text=HUGE),
    lambda: MetricSnapshot(post_id="1", metadata={"post_text": "A" * 300_000}),
    lambda: MetricSnapshot(post_id=HUGE),
    lambda: OutcomeRecord(generation_id="g1", status="posted", final_text=HUGE),
    lambda: OutcomeRecord(generation_id="g1", status="posted", metadata={"note": "A" * 300_000}),
    lambda: ImageFeedback(choice="used", metadata={"note": "A" * 300_000}),
])
def test_no_field_of_what_is_learned_from_is_unbounded(build):
    with pytest.raises(ValidationError):
        build()


def test_real_captures_still_fit():
    snapshot = MetricSnapshot(post_id="1900000000000000001", post_text="x" * 25_000, metadata={
        "capture_mode": "publication", "post_text": "x" * 25_000, "parent_text": "y" * 25_000, "quote_text": "z" * 25_000,
        "author_handle": "someone", "is_own_post": True,
    })
    assert snapshot.metadata["capture_mode"] == "publication"
    assert ProfileSnapshot(handle="someone", display_name="Some One", bio="b" * 160, metadata={"source": "profile page"}).bio
