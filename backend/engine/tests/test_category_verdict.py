"""The closing flourish, separated from his voice by SUBJECT rather than shape.

My first attempt at this gate failed calibration badly. "A short last block
that names nothing" fires on 11-23% of his own posts depending on the word cap,
and there it is the best writing in the archive:

    "bro is not saving the multiverse, he's vibe coding it"
    "The silence is making me think they're cooking."
    "Just my prediction."

So structure is the wrong axis. What separates his closer from the model's is
what the sentence is ABOUT. His are personal and specific to the moment. The
model's are a verdict on the entire category, and would close a post about
anything at all:

    "It's a new way to build apps."
    "That's a big shift from models that take weeks."
    "It's the way we're building AI."
    "That's a leaky faucet."

Measured: 6 of 6 live examples caught, 0 of 6 of his real closers, 0 of his 35
published posts.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.intelligence import drop_category_verdict         # noqa: E402

GROUNDED = "Otlet runs Qwen35_4b inside Postgres, so the data never leaves the database."

MODEL_CLOSERS = (
    "It's a new way to build apps.",
    "That's a big shift from models that take weeks.",
    "It's a way to keep your model alive when your disk's full.",
    "It's the way we're building AI.",
    "That's a leaky faucet.",
)

HIS_CLOSERS = (
    "Now let's just hope it performs",
    "Just my prediction.",
    "I'm to young for this",
    "The silence is making me think they're cooking.",
    "bro is not saving the multiverse, he's vibe coding it",
)


def test_a_category_verdict_is_cut():
    for closer in MODEL_CLOSERS:
        text = f"{GROUNDED}\n\n{closer}"
        assert drop_category_verdict(text) == GROUNDED, f"kept: {closer!r}"


def test_his_own_closers_survive():
    """The reason the structural version of this gate was thrown away."""
    for closer in HIS_CLOSERS:
        text = f"{GROUNDED}\n\n{closer}"
        assert drop_category_verdict(text) == text, f"cut his voice: {closer!r}"


def test_a_closer_carrying_something_real_survives():
    text = f"{GROUNDED}\n\nIt's a 40% drop in query latency."
    assert drop_category_verdict(text) == text


def test_it_never_empties_a_draft():
    only = "It's a new way to build apps."
    assert drop_category_verdict(only) == only
    assert drop_category_verdict("") == ""


def test_it_does_not_rewrite_his_published_posts():
    fixture = Path(__file__).with_name("fixtures_his_posts.json")
    if not fixture.exists():
        return
    posts = [p for p in json.loads(fixture.read_text(encoding="utf-8")) if p.strip()]
    changed = [p for p in posts if drop_category_verdict(p) != p]
    assert not changed, (
        f"rewrote {len(changed)}/{len(posts)} of his own posts, "
        f"e.g. {changed[0][:100]!r}")
