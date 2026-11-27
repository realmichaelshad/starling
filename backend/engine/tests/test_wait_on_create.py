"""Starling may recommend WAIT on a from-scratch post — and the draft stays.

Requested 2026-07-28: when nothing strong is rising and the draft ran without
web evidence, the card says so plainly and the user still decides. The draft
must never be withheld by the recommendation — a wait call beside an empty
composer would be a refusal wearing advice's clothes."""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_the_wait_block_exists_and_keeps_the_draft():
    """Static contract check on the composer flow: the wait recommendation
    is gated on a PRESENT draft, sets the flag, and never clears the text."""
    source = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(
        encoding="utf-8")
    # API-native pass: the recommendation fires when nothing new was found for the beat.
    block = re.search(
        r"if \(creating and action == \"post\" and no_news and decision\.get\(\"draft\"\)\):\n"
        r"(?P<body>(?:\s{8}.+\n)+)",
        source)
    assert block, "the wait-recommendation block is gone from the composer flow"
    body = block.group("body")
    assert 'decision["wait_recommended"] = True' in body
    # Product review U6: the note says WHY there is no news ("nothing new" only when a search
    # really ran and found nothing; a refused or late search is said as what it was).
    assert "_no_news_note(search_status)" in body
    assert 'decision["draft"]' not in body.replace(
        'decision.get("draft")', ""), \
        "the wait block must never reassign the draft"
