"""V4: a new account is told its drafts sound generic, on the card, with the step that fixes it.

The note is its own field (`decision.voice_note`), not one of the `reasons`: the extension keeps
reasons behind "Why?", where a new customer would never see it, and shows `voice_note` under the
draft with a button to the writing samples.
"""
from __future__ import annotations

from app import intelligence, main, voice_examples


def test_the_note_is_its_own_field_and_only_on_a_draft(monkeypatch):
    monkeypatch.setattr(main, "_voice_note", lambda: "Drafts sound generic until I know how you write.")
    decision = {"draft": "A reply worth posting.", "reasons": ["kept as it was"]}
    main._add_voice_note(decision)
    assert decision["voice_note"].startswith("Drafts sound generic")
    assert decision["reasons"] == ["kept as it was"]
    nothing_drafted = {"draft": "  ", "reasons": []}
    main._add_voice_note(nothing_drafted)
    assert "voice_note" not in nothing_drafted


def test_the_note_counts_the_writing_it_has_and_stops_once_there_is_enough(monkeypatch):
    monkeypatch.setattr(intelligence, "get_personal_model", lambda: object())
    monkeypatch.setattr(voice_examples, "own_writing_count", lambda model: 0)
    assert "I have none of your writing yet." in main._voice_note()
    monkeypatch.setattr(voice_examples, "own_writing_count", lambda model: 1)
    assert "I have 1 piece of your writing so far." in main._voice_note()
    monkeypatch.setattr(voice_examples, "own_writing_count", lambda model: 3)
    assert "I have 3 pieces of your writing so far." in main._voice_note()
    monkeypatch.setattr(voice_examples, "own_writing_count", lambda model: main._COLD_START_OWN_TEXTS)
    assert main._voice_note() is None
    decision = {"draft": "A reply worth posting."}
    main._add_voice_note(decision)
    assert "voice_note" not in decision
