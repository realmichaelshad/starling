"""A post retries on a detected weakness, never on its score (S1, 2026-09-21).

The reply lane's arithmetic, applied to the post surface: a writer that lands
40% of the time is captured 87% of the time by four INDEPENDENT shots and 40%
of the time by one, so a post with a weakness the gates can see (a missed
shape, a question with no claim, naming nothing) buys another attempt.

"Scored below ACCEPT_SCORE" used to be one of those signals. On an API model it
made three attempts the norm for posts that had passed every gate: GrowthScore
ranks candidates, it does not gate them, so a score alone no longer keeps the
queue. The first draft that passes the gates ships.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.intelligence import ACCEPT_SCORE                                  # noqa: E402,F401
from app.llm import _sampling_for, variation_note                        # noqa: E402
from app.schemas import DraftRequest, PostContext                           # noqa: E402

WEAK = "Otlet lets you run Qwen35_4b inside Postgres."


# RETIRED (API-native pass, 2026-09-22): test_a_below_band_score_alone_no_longer_keeps_the_queue: style findings never buy another attempt now; only an integrity rejection or a repeated reply does (`_first_draft_that_passes`, test_api_native_paths.py).


# RETIRED (API-native pass, 2026-09-22): test_an_accepted_post_ends_the_run: style findings never buy another attempt now; only an integrity rejection or a repeated reply does (`_first_draft_that_passes`, test_api_native_paths.py).


# RETIRED (API-native pass, 2026-09-22): test_no_score_means_no_extra_attempt: style findings never buy another attempt now; only an integrity rejection or a repeated reply does (`_first_draft_that_passes`, test_api_native_paths.py).


# RETIRED (API-native pass, 2026-09-22): test_a_detected_weakness_still_buys_another_attempt: style findings never buy another attempt now; only an integrity rejection or a repeated reply does (`_first_draft_that_passes`, test_api_native_paths.py).


# RETIRED (API-native pass, 2026-09-22): test_the_ceiling_binds_posts_too: style findings never buy another attempt now; only an integrity rejection or a repeated reply does (`_first_draft_that_passes`, test_api_native_paths.py).


def test_post_retries_vary_the_prompt_not_the_temperature():
    """A same-prompt re-roll is not an independent attempt, and the AI service
    may ignore temperature entirely. So sampling is the same hint on every
    attempt and a retry is steered by its prompt: the variation note names how
    the earlier candidates opened and asks for a different opening and angle."""
    request = DraftRequest(action="post",
                           context=PostContext(author="a", text="agents",
                                               visibility="public"))
    assert _sampling_for(request, attempt=0)["temperature"] == 0.65
    assert _sampling_for(request, attempt=1) == _sampling_for(request, attempt=0)
    assert _sampling_for(request, attempt=2) == _sampling_for(request, attempt=0)

    assert variation_note(0, [WEAK]) == ""
    second, third = variation_note(1, [WEAK]), variation_note(2, [WEAK])
    assert "Otlet lets you run" in second, "the retry must be told how attempt 1 opened"
    assert "different" in second.lower()
    assert second != third, "each retry is steered somewhere new"
