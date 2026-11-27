"""The voice card (API-native pass, 2026-09-22): made in the background, versioned, refreshed on growth."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app import voice_card
from app.intelligence import reset_personal_model_cache
from app.storage import upsert_post

NOW = datetime.now(timezone.utc)


def _seed(count: int, *, start: int = 0, protected: set[int] | None = None) -> None:
    for index in range(start, start + count):
        created = NOW - timedelta(days=index + 1)
        upsert_post({"post_id": f"v{index}", "post_type": "reply" if index % 3 == 0 else "post",
                     "text": f"note {index}: the retry loop was the bottleneck again, not the model",
                     "parent_text": "why is my agent slow" if index % 3 == 0 else None,
                     "created_at": created.isoformat(),
                     "visibility": "protected" if index in (protected or set()) else "public"})
    reset_personal_model_cache()


class _Model:
    def __init__(self, answer=None):
        self.calls = []
        answer = answer if answer is not None else {
            "beats": ["LLM evals and latency", "@someone's private project", "developer tooling"],
            "stances": ["retry budgets beat bigger models"], "argues_and_jokes": "Deadpan.",
            "uses": ["turns out"], "avoids": ["hype"], "shape": "One line.", "audience": "Builders."}
        outer = self

        class router:
            text_model = "starling-quality"

            @staticmethod
            def chat(messages, **kwargs):
                outer.calls.append((messages, kwargs))
                return answer if isinstance(answer, str) else json.dumps(answer)
        self.router = router


def test_cold_start_makes_no_card():
    _seed(voice_card.MIN_CARD_TEXTS - 1)
    model = _Model()
    assert voice_card.due() == (False, "cold_start")
    assert voice_card.refresh_if_due(model) is None and model.calls == []


def test_the_first_card_is_made_on_the_quality_lane_with_a_strict_schema():
    _seed(voice_card.MIN_CARD_TEXTS)
    model = _Model()
    card = voice_card.refresh_if_due(model)
    assert card and card["version"] == 1 and card["corpus_count"] == voice_card.MIN_CARD_TEXTS
    [(messages, kwargs)] = model.calls
    assert kwargs["lane"] == "quality" and kwargs["fmt"]["json_schema"]["strict"] is True
    assert kwargs["options"]["num_predict"] <= 600, "fits the proxy's output clamp"
    assert "retry loop" in messages[1]["content"]
    assert card["card"]["beats"] == ["LLM evals and latency", "developer tooling"], \
        "a handle is never a beat (beats become search queries)"
    assert card["stable"] and card["habits"]


def test_an_unchanged_corpus_is_not_remade_and_growth_of_twenty_is():
    _seed(voice_card.MIN_CARD_TEXTS)
    voice_card.refresh_if_due(_Model())
    assert voice_card.due()[1] == "unchanged"
    _seed(5, start=100)
    assert voice_card.due() == (False, "fresh"), "a few new texts wait for the weekly refresh"
    _seed(voice_card.REFRESH_GROWTH, start=200)
    assert voice_card.due() == (True, "grew")
    assert voice_card.refresh_if_due(_Model())["version"] == 2


def test_a_changed_corpus_is_remade_after_a_week(monkeypatch):
    _seed(voice_card.MIN_CARD_TEXTS)
    voice_card.refresh_if_due(_Model())
    _seed(2, start=100)
    import time as _time
    later = _time.time() + voice_card.REFRESH_AGE_DAYS * 86400 + 60
    assert voice_card.due(now=later) == (True, "weekly")


def test_an_unusable_answer_is_recorded_and_backs_off():
    _seed(voice_card.MIN_CARD_TEXTS)
    assert voice_card.refresh_if_due(_Model(answer="not json at all")) is None
    assert voice_card.current_card() is None
    assert voice_card.due() == (False, "backoff")


def test_protected_posts_and_background_notes_never_reach_the_card_call():
    from app import own_material
    _seed(voice_card.MIN_CARD_TEXTS + 2, protected={1})
    own_material.add("background", "I am quietly planning Project Falcon for next spring")
    model = _Model()
    voice_card.refresh_if_due(model)
    [(messages, _)] = model.calls
    prompt = messages[1]["content"]
    assert "note 1:" not in prompt, "a protected post never leaves the engine"
    assert "Project Falcon" not in prompt, "background notes steer selection only"


def test_the_stable_sample_is_representative_and_fixed_per_version():
    _seed(60)
    card = voice_card.refresh_if_due(_Model())
    stable = card["stable"]
    assert voice_card.STABLE_MIN <= len(stable) <= voice_card.STABLE_MAX
    assert {row["kind"] for row in stable} == {"reply", "post"}, "both surfaces the writer uses"
    assert sum(len(row["text"]) for row in stable) <= voice_card.STABLE_CHAR_BUDGET + 40 * len(stable)
    again = voice_card.current_card()["stable"]
    assert again == stable


def test_measured_habits_describe_and_never_rule():
    rows = [{"text": t, "kind": "post"} for t in ("short lowercase take", "another quick one here", "third one")]
    habits = " ".join(voice_card.measured_habits(rows))
    assert "posts usually" in habits and "mostly starts lowercase" in habits
    assert "do not" not in habits.lower() and "never use" not in habits.lower()
