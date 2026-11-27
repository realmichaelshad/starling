"""A cached evidence pool must obey TODAY'S rules, not the rules it was built under.

The expensive shape of this bug, measured live on 2026-07-27:

  - `builder_fit` was taught to reject incident-triage stories, naming the
    exact headline "AI Root Cause Analysis Shifts from Model Reasoning to
    Context Engineering" in its own comment.
  - InfoQ was removed from `RSS_FEEDS` for delivering that kind of piece,
    naming the exact resulting draft in its own comment.
  - The headline reached the composer anyway, an hour later.

Both fixes were correct and both were inert, because the candidate pool is
cached and the cache was returned without re-checking anything. On the live
database `builder_fit` rejected exactly one of the 24 cached candidates — that
one — while the writer was still being handed it.

That failure mode is worse than a wrong rule: you tighten the gate, watch the
bad topic appear regardless, and conclude the gate does not work. Worse still,
`headlines()` falls back to cached items whenever a refresh comes back empty,
so a removed feed can outlive its removal indefinitely behind a network blip.

So the rule is: filter on READ. Both caches, every path.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import news                                       # noqa: E402
from app.subject import builder_fit                        # noqa: E402

OFF_BEAT = "AI Root Cause Analysis Shifts from Model Reasoning to Context Engineering"
ON_BEAT = "Claude Code costs up to $200 a month. Goose does the same thing for free."


def test_the_filter_actually_rejects_the_headline_that_shipped():
    """If this fails, the rest of the file is measuring the wrong thing."""
    assert builder_fit(OFF_BEAT) is False
    assert builder_fit(ON_BEAT) is True


def test_cached_web_candidates_are_re_filtered_on_read(monkeypatch):
    """The radar cache is where the InfoQ item was actually sitting."""
    from app import radar
    cached = [
        {"topic": ON_BEAT, "angle": "a", "why": "w", "source": "news"},
        {"topic": OFF_BEAT, "angle": "a", "why": "w", "source": "news"},
    ]
    monkeypatch.setattr(radar, "get_job_state",
                        lambda key: {"at": time.time(), "items": cached})
    got = radar._web_candidates(["coding tools", "model releases"])
    topics = [row["topic"] for row in got]
    assert ON_BEAT in topics
    assert OFF_BEAT not in topics, (
        "a cached candidate must be re-checked against the current beat filter")
