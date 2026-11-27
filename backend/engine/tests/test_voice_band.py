"""voice_match has to be scored against what is reachable, not against 1.0.

Measured on this account: his own posts, compared leave-one-out against the
rest of his own writing, reach a median cosine of 0.457 and a best of 0.670.
That number was being fed straight into an 11-point term as though 1.0 were
attainable, so a draft that sounded exactly like him earned about 5 of 11 and
the remaining 6 points were unavailable to anybody. Generated drafts measured
0.486 — very slightly MORE in-voice than his own posts are to one another —
and the rubric recorded them as barely half marks.

The first attempt at a fix divided by the ceiling alone and scored a cake
recipe at 8.2 of 11. That is the trap worth pinning: on the bundled
96-dimension projection, text with nothing to do with this account still
scores about 0.36, so most of the 0..1 range is noise floor. Both ends have to
be measured, and the score is where a draft falls between them.
"""
from __future__ import annotations

import numpy as np
import pytest

from app.intelligence import PersonalModel


class _StubEmbedder:
    """Deterministic 3-dimension embeddings: axis 0 is 'his voice'."""

    available = True

    @staticmethod
    def encode(texts):
        rows = []
        for text in texts:
            lowered = text.lower()
            voice = 1.0 if any(word in lowered for word in ("opus", "grok", "gpt")) else 0.2
            other = 1.0 if "cake" in lowered or "oven" in lowered else 0.2
            vector = np.array([voice, other, 0.3], dtype=np.float32)
            rows.append(vector / np.linalg.norm(vector))
        return np.vstack(rows)


@pytest.fixture()
def model(monkeypatch) -> PersonalModel:
    instance = PersonalModel.__new__(PersonalModel)
    instance._voice_band = None
    instance.uses_llm_embeddings = False
    instance.embedder = _StubEmbedder()
    corpus = ["Opus 5 lands before October", "Grok 4.5 is live for paid users",
              "GPT-5.6 showed up for select users", "Opus 5 pricing changes things",
              "Grok 4.5 beats GPT on cost", "GPT-5.6 is close to shipping"]
    instance.embedding_matrix = _StubEmbedder.encode(corpus)
    return instance


def test_the_band_has_a_measured_floor_and_ceiling(model: PersonalModel) -> None:
    floor, ceiling = model.voice_band()
    assert 0.0 <= floor < ceiling <= 1.0
    assert ceiling - floor >= 0.05, "a band with no width cannot discriminate"


def test_in_voice_text_scores_near_the_top(model: PersonalModel) -> None:
    assert model.voice_similarity("Opus 5 and Grok 4.5 both ship this month") > 0.8


def test_out_of_domain_text_scores_near_the_bottom(model: PersonalModel) -> None:
    """The cake recipe. The first version of this fix gave it 8.2 of 11."""
    assert model.voice_similarity("Butter a cake tin and heat the oven") < 0.35


def test_a_uniform_corpus_does_not_hand_out_full_marks(monkeypatch) -> None:
    """If everything looks alike, the band is forced open rather than collapsing
    to zero width and scoring every draft as perfect."""
    instance = PersonalModel.__new__(PersonalModel)
    instance._voice_band = None
    instance.uses_llm_embeddings = False
    instance.embedder = _StubEmbedder()
    same = ["Opus 5 lands before October"] * 6
    instance.embedding_matrix = _StubEmbedder.encode(same)
    floor, ceiling = instance.voice_band()
    assert ceiling - floor >= 0.05


def test_no_corpus_returns_no_score() -> None:
    instance = PersonalModel.__new__(PersonalModel)
    instance._voice_band = None
    instance.embedding_matrix = None
    assert instance.voice_similarity("anything") is None
