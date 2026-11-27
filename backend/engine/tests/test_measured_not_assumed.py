"""Three numbers that described something other than what they claimed to.

Each was found by asking what the value is actually derived from, rather than by
reading the line that prints it.

1. `account_review` quoted a ROW COUNT as the sample behind a median. `posts`
   is every row of that format; `median_views` is the median of only the rows
   carrying a view count. On his archive that overstated the evidence by 78%
   (95 against 56) in the one sentence whose job is to say how much to trust the
   number beside it. `performance_insights` already returns `measured_views` and
   `measured` for this; nothing read them.

2. GrowthScore's invented-figure penalty never saw the EVIDENCE. It compared the
   draft's numbers against `context_text`, which for a created post is the topic
   grounding line — the captured facts live in `request.direction`. So a
   correctly grounded figure took a flat -10 and ranked BELOW a draft with no
   numbers at all: selection actively steered away from the specificity the
   pipeline exists to produce.

3. Press-timing stages were keyed by `threading.get_ident()`, recorded inside a
   `asyncio.to_thread` worker and collected on the event-loop thread. Different
   id, so a blocking press logged NO stages and left them under the worker's key
   for the next press that reused it to log as its own.
"""
from __future__ import annotations

import threading
import time

from app import main
from app.growth_scorer import score_candidate
from app.intelligence import _direction_without_scaffolding


# --------------------------------------------------------------------------- #
# 1. the sample behind a median is the measured sample
# --------------------------------------------------------------------------- #

def test_the_format_gap_quotes_the_measured_sample_not_the_row_count():
    from app.account_review import _format_gap
    facts = {
        "by_format": {
            "reply": {"median_views": 900, "posts": 40, "measured_views": 9, "evidence": "limited"},
            "post": {"median_views": 300, "posts": 95, "measured_views": 56, "evidence": "moderate"},
        },
    }
    gap = _format_gap(facts)
    assert gap is not None, "the fixture no longer produces a format gap; re-anchor it"
    text = gap["text"]
    assert "9 replies" in text, text
    assert "56 standalone" in text, text
    assert "40 replies" not in text, "the row count is being quoted as the sample"
    assert "95 standalone" not in text, "the row count is being quoted as the sample"


# --------------------------------------------------------------------------- #
# 2. the evidence grounds a figure
# --------------------------------------------------------------------------- #

_EVIDENCE = ("TOPIC: Opus 5. USE THESE FACTS, and only these: Anthropic reports 750,000 "
             "lines refactored in the launch demo. "
             "SHAPE FOR THIS POST: 3 blocks, 4 sentences; no sentence past 20 words.")


class _Req:
    action = "post"
    direction = _EVIDENCE


def _penalty(draft: str, grounded: str) -> float:
    _score, terms = score_candidate(draft, action="post", objective="followers",
                                    context_text="", grounded_text=grounded)
    return float(terms.get("unverifiable_precision", 0.0))


def test_a_grounded_figure_is_not_penalised_as_invented():
    evidence = _direction_without_scaffolding(_Req())
    draft = "Opus 5 refactored 750,000 lines in the launch demo."
    assert _penalty(draft, "") == -10.0, (
        "this test no longer reproduces the original bug and should be re-anchored")
    assert _penalty(draft, evidence) == 0.0, "a figure straight out of the evidence still lost 10 points"


def test_an_invented_figure_is_still_penalised():
    evidence = _direction_without_scaffolding(_Req())
    assert _penalty("Opus 5 refactored 920,000 lines in the launch demo.", evidence) == -10.0


def test_the_shape_blocks_numbers_do_not_ground_anything():
    """The raw direction ends with "no sentence past 20 words"; the helper cuts it,
    so 20 cannot vouch for a figure."""
    evidence = _direction_without_scaffolding(_Req())
    assert "20 words" not in evidence
    assert "750,000" in evidence


def test_punctuation_does_not_decide_whether_a_figure_is_grounded():
    """Raw-token comparison meant "750,000" in the evidence did not vouch for
    "750000" in the draft."""
    evidence = _direction_without_scaffolding(_Req())
    assert _penalty("Opus 5 refactored 750000 lines.", evidence) == 0.0


# --------------------------------------------------------------------------- #
# 3. press stages belong to the press, not the thread
# --------------------------------------------------------------------------- #

def test_stages_recorded_on_a_worker_are_collected_from_another_thread():
    started = time.perf_counter()
    done = threading.Event()

    def worker():
        main._stage(started, "grounding")
        main._stage(started, "writer")
        done.set()

    threading.Thread(target=worker, daemon=True).start()
    assert done.wait(5)
    stages = main._take_stages(started)
    assert [name for name, _ in stages] == ["grounding", "writer"], stages
    assert main._take_stages(started) == [], "a press's stages must be taken exactly once"


def test_two_presses_never_take_each_others_stages():
    first, second = time.perf_counter(), time.perf_counter()
    main._stage(first, "first-only")
    main._stage(second, "second-only")
    assert [n for n, _ in main._take_stages(first)] == ["first-only"]
    assert [n for n, _ in main._take_stages(second)] == ["second-only"]


def test_a_press_that_never_logs_cannot_grow_the_store_without_bound():
    """The thread key was self-cleaning by accident. A per-press key is unique
    forever, so an abandoned press would otherwise stay resident."""
    for _ in range(main._PRESS_STAGES_MAX * 3):
        main._stage(time.perf_counter(), "abandoned")
    assert len(main._PRESS_STAGES) <= main._PRESS_STAGES_MAX
