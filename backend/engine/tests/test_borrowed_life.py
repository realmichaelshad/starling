"""The draft may not claim his life from someone else's article.

When the topic is a blog post or a Show HN writeup, the writer adopts the
author's first person. Measured live on 2026-07-27:

    "Why My Open-Source Project Hasn't Done Better"
        -> "My open-source project has 400 stars, but it's not the LLMs that
            are holding it back."

    "C++ Vs Rust: Which is better for writing AI/ML code with LLMs"
        -> "Rust's 50% lower token use is impressive, but I still can't get
            past the type system tripping me up."

These are the worst failures the pipeline can produce. A weak post wastes a
slot; this puts a false statement about his own work into his voice, and it is
grammatical, on-topic and fluent, so no readability or relevance gate can see
it.

The bar, unchanged from the rest of this gate: ZERO of his own published posts
may match. Opinion and prediction in the first person ARE his voice — "I'd take
Fable 5 over every GPT model", "My prediction: July 30" — and must survive. The
line is between having an OPINION about the subject and having LIVED WITH it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.intelligence import _invented_experience          # noqa: E402

UNRELATED = "An unrelated article about something else entirely, with no overlap."

# Every one of these reached a composer.
INVENTED = (
    "My open-source project has 400 stars, but it's not the LLMs holding it back.",
    "Rust's 50% lower token use is impressive, but I still can't get past the type system.",
    "I keep running into the same context limit every afternoon.",
    "I've been fighting this agent all week.",
    "My codebase is 400k lines and it chokes.",
    "I switched my whole team over last month.",
)

# His own voice. These must never be blocked.
HIS = (
    "Opus 5 is the first model that handles a 400k repo without choking.",
    "The pricing page is the only benchmark that has ever mattered.",
    "I think Opus 5 ships next week.",
    "I'd take Fable 5 over every GPT model available right now.",
    "My prediction: July 30.",
    "Anthropic can't release Opus 5 while Kimi K3 is trending.",
    "I want to see what it does on a real repo.",
)


def test_a_borrowed_first_person_claim_is_refused():
    for text in INVENTED:
        assert _invented_experience(text, UNRELATED), f"slipped through: {text!r}"


def test_his_own_first_person_still_ships():
    for text in HIS:
        assert _invented_experience(text, UNRELATED) is None, f"blocked his voice: {text!r}"


def test_it_never_fires_on_his_published_posts():
    """The bar the rest of this gate is held to, measured not argued."""
    fixture = Path(__file__).with_name("fixtures_his_posts.json")
    if not fixture.exists():
        return
    posts = [p for p in json.loads(fixture.read_text(encoding="utf-8")) if p.strip()]
    hits = [p for p in posts if _invented_experience(p, UNRELATED)]
    assert not hits, (
        f"the gate fires on {len(hits)}/{len(posts)} of his own posts, "
        f"e.g. {hits[0][:100]!r}")
