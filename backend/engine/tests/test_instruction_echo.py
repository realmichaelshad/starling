"""The revision guidance must never come back as the post.

The clarity-revision pass hands the model prose describing a fault, and the
model copies prose. `llm.py` already records that three gates were written
for this shape and each was defeated by the next rewording.

Measured live 2026-07-27 — this reached the composer as a finished post:

    "Nunchaku 4-bit Diffusers require a Blackwell GPU.
     Use INT4 if you don't have one.
     The draft cuts off mid-sentence. Finish it properly."

The last line is the instruction. `leaks_instructions` caught none of the seven
guidance strings tested against it.

So the second half of the check does not try to describe the prose at all — it
compares against the ACTUAL guidance strings, read out of the guidance tables
at runtime. A reworded instruction is still caught, and one added tomorrow is
covered the day it is written, which is the property every previous attempt
lacked.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.intelligence import leaks_instructions           # noqa: E402

LIVE_POST = ("Nunchaku 4-bit Diffusers require a Blackwell GPU.\n\n"
             "Use INT4 if you don't have one.\n\n"
             "The draft cuts off mid-sentence. Finish it properly.")

ECHOES = (
    "The draft cuts off mid-sentence. Finish it properly.",
    "The draft ends mid-thought. Finish the final sentence cleanly.",
    "State the concrete thing this is about, using only facts already present.",
    "Do not describe the draft or what it lacks.",
    "Rewrite it as what is actually true of the thing itself.",
)

REAL_POSTS = (
    "Nunchaku brings 4-bit inference to Diffusers, but you need a Blackwell GPU.",
    "Claude Opus 5 tops the index at 26% lower cost per task.",
    "Model routing is simple. Until it isn't. Sonnet cost $79 for 417 tasks.",
)


def test_the_live_leak_is_caught():
    assert leaks_instructions(LIVE_POST)


def test_every_guidance_echo_is_caught():
    for echo in ECHOES:
        assert leaks_instructions(echo), f"slipped through: {echo!r}"


def test_a_real_post_is_untouched():
    for post in REAL_POSTS:
        assert not leaks_instructions(post), f"blocked a real post: {post!r}"


def test_it_reads_the_live_guidance_tables():
    """The property every hand-written version lacked: a NEW guidance string is
    covered without anyone remembering to add a pattern for it."""
    from app.llm import ModelClient
    tables = [getattr(ModelClient, a, None)
              for a in ("CLARITY_GUIDANCE", "REPLY_CLARITY_GUIDANCE")]
    values = [str(v) for t in tables if isinstance(t, dict) for v in t.values()]
    assert values, "no guidance found — the shingle check would be vacuous"
    caught = [v for v in values if leaks_instructions(v)]
    assert len(caught) >= len(values) * 0.8, (
        f"only {len(caught)}/{len(values)} guidance strings are recognised")


def test_it_never_fires_on_his_published_posts():
    fixture = Path(__file__).with_name("fixtures_his_posts.json")
    if not fixture.exists():
        return
    posts = [p for p in json.loads(fixture.read_text(encoding="utf-8")) if p.strip()]
    hits = [p for p in posts if leaks_instructions(p)]
    assert not hits, f"fired on his own post: {hits[0][:90]!r}"
