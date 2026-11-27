from __future__ import annotations

from app.account_model import growth_diagnoses, infer_account_model
from app.storage import set_account_profile, upsert_post


def test_account_model_reports_voice_positioning_and_evidence_limits():
    set_account_profile({"audience": "AI builders", "content_pillars": ["agents", "evaluation"], "bio": "Practical AI notes"})
    for index in range(24):
        upsert_post({"post_id": str(index), "text": f"My agent evaluation note {index}?", "post_type": "post"})
    model = infer_account_model()
    assert model["intended_audience"] == "AI builders"
    assert model["content_pillars"] == ["agents", "evaluation"]
    assert model["voice_profile"]["question_rate"] == 1.0
    assert model["confidence"]["post_count"] == 24
    assert any("one format" in item["observed_pattern"] for item in growth_diagnoses())

