"""The instant niche read (API-native pass, 2026-09-22): the beat is known from the first press.

The owner's rule: "the first press after someone adds writing must already know their beat",
checked on the evaluation personas' own samples and background notes (e2e/eval/personas.py):
the coach reads as strength training, nutrition and beginners, the founder as SaaS pricing,
churn and bootstrapping, the terse engineer as LLM evals, latency and inference cost.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from app import niche, own_material

PERSONAS_FILE = Path(__file__).resolve().parents[3] / "e2e" / "eval" / "personas.py"


def _personas():
    if not PERSONAS_FILE.exists():
        pytest.skip("the evaluation personas are not in this checkout")
    spec = importlib.util.spec_from_file_location("eval_personas", PERSONAS_FILE)
    module = importlib.util.module_from_spec(spec)
    import sys
    sys.modules["eval_personas"] = module
    spec.loader.exec_module(module)
    return {persona.key: persona for persona in module.PERSONAS}


def _has(terms: list[str], *needles: str) -> bool:
    joined = " | ".join(terms).lower()
    return all(needle in joined for needle in needles)


@pytest.mark.parametrize("key, expected", [
    ("coach", ("strength", "nutrition", "beginner")),
    ("essayist", ("saas", "pricing", "churn", "bootstrap")),
    ("terse", ("eval", "latency", "inference cost")),
])
def test_the_eval_personas_read_as_their_beats(key, expected):
    persona = _personas()[key]
    read = niche.read(list(persona.samples), notes=[persona.background])
    assert _has(read["terms"][:8], *expected), read["terms"]
    assert len(read["beats"]) >= 3 and all(beat in read["terms"] for beat in read["beats"])


def test_relevance_is_high_on_the_beat_and_zero_off_it():
    personas = _personas()
    coach = niche.read(list(personas["coach"].samples), notes=[personas["coach"].background])
    terse = niche.read(list(personas["terse"].samples), notes=[personas["terse"].background])
    split = "Tried a 5-day split for 3 months and gained almost nothing. Switched to full body 3x a week."
    marathon = "Just finished my first marathon! 4:12, legs are destroyed but so happy"
    rag = "We moved our RAG pipeline from 1k-token chunks to 300-token chunks and recall on our eval set rose"
    assert niche.fit(split, coach)["score"] >= 0.35 and niche.fit(rag, coach)["score"] < 0.2
    assert niche.fit(rag, terse)["score"] >= 0.35 and niche.fit(marathon, terse)["score"] == 0.0


def test_a_capitalised_name_from_a_private_note_never_becomes_a_query_word():
    read = niche.read(["retry budgets beat bigger models", "retry budgets are the whole trick"],
                      notes=["I am building Project Falcon, a retry budgets tool, in secret"])
    assert "retry budgets" in read["query_terms"]
    assert not any("falcon" in term.lower() for term in read["query_terms"])


def test_the_read_is_ready_the_moment_writing_is_added():
    """No model call and no background job: adding material refreshes it (main._refresh_niche)."""
    from fastapi.testclient import TestClient

    from app import main, security
    client = TestClient(main.app)
    headers = {"Authorization": f"Bearer {security.ensure_token()}"}
    for text in ("Protein at breakfast is the easiest win nobody takes.",
                 "Beginners: three full-body days a week beats six days of chaos.",
                 "Creatine is boring, cheap, and works."):
        response = client.post("/own-material/add", json={"kind": "voice", "text": text}, headers=headers)
        assert response.status_code == 200, response.text
    own_material.add("background", "I coach strength training and nutrition for beginners.")
    client.post("/own-material/add", json={"kind": "voice", "text": "Sleep is the supplement."}, headers=headers)
    read = niche.current()
    assert _has(read["terms"], "strength", "nutrition", "beginner"), read["terms"]
    assert niche.beats(), "the beats the research job looks up"
