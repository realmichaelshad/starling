"""The 17:40 reader-address epidemic (2026-07-28): question hooks reached the
post prompt and five of nine drafts opened by buttonholing the reader. The
hook is defused (HOOK_STYLES has no question instruction any more, and the
bandit's question-shaped title stops instructing posts, same as quotes), and
a second-person QUESTION now flags talks_to_the_reader even when it names
things — his archive asks the reader nothing, 0/71."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.intelligence import HOOK_STYLES, _questions_the_reader   # noqa: E402


def test_no_hook_instructs_a_question():
    import re
    for hook in HOOK_STYLES:
        assert not re.search(r"\bquestion\b", hook, re.IGNORECASE), hook


def test_reader_questions_are_caught_even_when_they_name_things():
    assert _questions_the_reader(
        "You think AI coding agents are helping? They're killing collaboration.")
    assert _questions_the_reader(
        "Do you want open-source memory without keys? It's already in my agent.")


def test_statement_closers_with_you_still_pass():
    """"You're paying 4x what Goose charges" is a closer doing real work —
    a statement, not a question at the reader."""
    assert not _questions_the_reader(
        "Goose is free. You're paying 4x what Goose charges.")


def test_an_at_addressed_question_is_his_register():
    """An @-addressed question has a named referent: conversation rather than
    buttonholing the reader."""
    assert not _questions_the_reader(
        "@example_friend you saw the same pricing leak, didn't you?")
