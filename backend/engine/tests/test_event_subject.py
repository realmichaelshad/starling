"""A headline is only a topic if something HAPPENED.

Measured on 2026-07-27: ten live posts, eight shipped, six defective. The
writing was rarely the fault — the topic handed to the writer was not an event,
so the draft had to invent whatever the title did not contain. Every case below
is one of those ten, quoted from the run.

Calibration at the time this landed:
    on-beat pool  23 -> 17  (a 26% supply cost)
    dropped        6        every one a discussion prompt, question or essay
    prefix fixed   8        "Show HN: Termic - ..." -> "Termic - ..."
    his archive    6% of his own opening lines rejected (limit 10%)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.subject import event_subject                     # noqa: E402


def test_a_show_hn_launch_keeps_its_subject_without_the_prefix():
    """The prefix went out in a post: "Show HN: LLM-spend lets you track ..."."""
    assert event_subject("Show HN: Termic – desktop app for running CLI coding agents") \
        == "Termic – desktop app for running CLI coding agents"
    assert event_subject("Show HN: LLM-spend – Audit your OpenAI/Anthropic API spend locally") \
        == "LLM-spend – Audit your OpenAI/Anthropic API spend locally"
    # And a plain headline is returned untouched.
    assert event_subject("Anthropic launches Cowork, a Claude Desktop agent") \
        == "Anthropic launches Cowork, a Claude Desktop agent"


def test_a_forum_question_is_not_a_topic():
    """"Ask HN: HotPin - lossless 120B MoE inference on 24GB RAM" produced
    "HotPin lets you run 120B MoE on 24GB RAM. If Disk > RAM, pinning gives
    +45%." — a figure that is in no source, and a second sentence that
    contradicts the first."""
    assert event_subject("Ask HN: HotPin – lossless 120B MoE inference on 24GB RAM") is None
    assert event_subject("Ask HN: Which is the least sloppy model you have used?") is None
    assert event_subject("Tell HN: the API is down again") is None


def test_an_open_question_is_not_a_finding():
    """"Will it be possible to do LLM Pool Training?" was answered as though it
    were settled: "LLM Pool Training's overhead is a killer."."""
    assert event_subject("Will it be possible to do LLM Pool Training?") is None
    assert event_subject("Is RAG dead?") is None


def test_someone_elses_first_person_never_becomes_his():
    """The worst one. "Why My Open-Source Project Hasn't Done Better" came back
    as "My open-source project has 400 stars, but it's not the LLMs that are
    holding it back" — a false statement about his own work, in his voice, that
    no downstream gate can repair because it is grammatical and on-topic."""
    assert event_subject("Why My Open-Source Project Hasn't Done Better") is None
    assert event_subject("Show HN: How I built a debugging tool, and the agent found bugs") is None
    assert event_subject("What building Shippy taught us about building agents") is None
    assert event_subject("How I cut my inference bill in half") is None


def test_it_does_not_reject_the_writing_it_protects():
    """The same bar every other gate is held to: his own published posts.

    Not a perfect comparison — his posts are not headlines — but a topic rule
    that rejected his own subjects would be steering the account away from
    itself, and that is worth measuring rather than assuming.
    """
    fixture = Path(__file__).with_name("fixtures_his_posts.json")
    if not fixture.exists():
        return
    posts = [p for p in json.loads(fixture.read_text(encoding="utf-8")) if p.strip()]
    openings = [p.splitlines()[0] for p in posts if p.splitlines()]
    rejected = [line for line in openings if event_subject(line) is None]
    share = len(rejected) / max(1, len(openings))
    assert share <= 0.10, (
        f"event_subject rejects {len(rejected)}/{len(openings)} ({share:.0%}) of "
        f"his own opening lines, e.g. {rejected[0][:80]!r}")


def test_empty_and_junk_inputs_are_not_topics():
    for junk in ("", "   ", "Show HN:", "Ask HN: ", None):
        assert event_subject(junk) is None
