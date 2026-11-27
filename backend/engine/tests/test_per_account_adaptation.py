"""The product adapts to WHOEVER's archive is present — his requirement,
2026-07-29: "if it was a different user we will adapt to his archive,
especially by the topic." Nothing user-specific may be hardcoded: the
vocabulary whitelist, the sentence ceiling, and the core lane are all
measured from the posts table, per account.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import storage                                    # noqa: E402
from app.intelligence import (                             # noqa: E402
    _account_register,
    _off_register,
    dangles_its_subject,
    reset_register_cache,
)
from app import subject as subject_mod                     # noqa: E402
from app.subject import core_lane                          # noqa: E402

WEB_DEV_POSTS = [
    "Shipped the authentication flow with Next.js middleware today.",
    "React Server Components finally make deployment predictable.",
    "Tailwind's new engine cut our build times in half.",
    "Vercel's pricing change hits every hobby deployment.",
    "TypeScript strict mode caught the localization bug again.",
    "Next.js 16 makes hydration errors readable at last.",
    "Deployment previews are the best thing Vercel ever shipped.",
    "React's compiler means memoization is finally automatic.",
    "Our accessibility audit found the modal traps focus wrong.",
]


@pytest.fixture()
def web_dev_account():
    with storage.connect() as conn:
        for index, text in enumerate(WEB_DEV_POSTS):
            conn.execute(
                "insert into posts (post_id, text) values (?, ?)",
                (f"webdev-{index}", text))
        conn.commit()
    reset_register_cache()
    subject_mod._LANE_CACHE = None
    yield
    reset_register_cache()
    subject_mod._LANE_CACHE = None


def test_vocabulary_whitelist_is_measured_from_their_archive(web_dev_account):
    register = _account_register()
    assert "authentication" in register["long_words"]
    assert "deployment" in register["long_words"]
    # Their own long words never flag as foreign for THEM.
    assert not _off_register(
        "Authentication and deployment got simpler this week.")


def test_core_lane_is_theirs_not_his(web_dev_account):
    assert core_lane("Next.js 16 ships with a new React compiler")
    assert core_lane("Vercel raises deployment pricing again")
    # A coding-adjacent AI topic is NOT automatically core for a web-dev
    # account... but the seed frame still admits big-lab model news, which
    # is the deliberate cold-start floor. What must NOT happen is the seed
    # blocking THEIR lane — asserted above.


def test_cold_account_falls_back_to_the_seed():
    reset_register_cache()
    subject_mod._LANE_CACHE = None
    assert core_lane("Claude Code costs up to $200 a month. Goose is free")


def test_dangling_subject_is_topic_anchored():
    assert dangles_its_subject(
        "It's the only reason agents can touch Python code safely.",
        "Snapshield snapshots before agent runs")
    assert not dangles_its_subject(
        "Snapshield acts like an undo button for agents.",
        "Snapshield snapshots before agent runs")
    # A pronoun opener with the subject later in the draft is fine.
    assert not dangles_its_subject(
        "It's an undo button. Snapshield snapshots the repo first.",
        "Snapshield snapshots before agent runs")
