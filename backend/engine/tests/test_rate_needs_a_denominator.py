"""An engagement rate on three views is not an estimate.

THE LIVE EXCHANGE, 2026-08-02, from his own chat history:

    him:      "that spidy psot what hsodu i do with it hsodu i dleate it its nto doign well"
    Starling: "Don't delete it yet. It has a high engagement rate for you right
               now. Wait an hour to see if views climb before removing anything."
    him:      "but in 7 minutes and it onyl got 3 vews is bad"
    Starling: "That's normal for your account size. Three views isn't failure yet."

**He was right and the product was confidently wrong.** It is the only place in
his whole corpus where he catches it out and the record proves him correct.

One like on three views is 33.33%, and `account_facts` handed that percentage to
the model with no floor — `if views:` was the entire guard. The model did the
rest: nothing in the codebase produces the string "high engagement rate", so it
read a large number and described it.

CALIBRATED, NOT ROUNDED. One extra interaction moves the rate by 100/N points:
3 views → 33.3, 10 → 10.0, 20 → 5.0, 30 → 3.3, 100 → 1.0. Measured against his
own distribution — 58 posts with view counts, median 310, p10 = 5 — a floor of
30 sends 31% of them to a count instead of a percentage. That share is large and
correct: those are the young, low-delivery posts where "3 views, 1 interaction"
is true and "33%" is not.

THE COUNT IS NEVER SUPPRESSED. `interactions` is still reported below the floor,
because a like is a like whatever the denominator. What goes away is the ratio,
which is the only part that was inventing confidence.
"""
from __future__ import annotations

import pytest

from app.account_facts import _RATE_MIN_VIEWS, _post_line


def _row(views, likes=0, replies=0, reposts=0):
    return {"post_id": "1", "text": "x",
            "metrics": {"views": views, "likes": likes,
                        "replies": replies, "reposts": reposts}}


def test_his_actual_post_gets_no_rate():
    """Seven minutes old, three views, one like."""
    line = _post_line(_row(3, likes=1))
    assert line.get("engagement_rate_pct") is None
    assert line["interactions"] == 1, "the COUNT must survive; only the ratio goes"
    assert line["views"] == 3


@pytest.mark.parametrize("views", [1, 3, 7, 12, 29])
def test_below_the_floor_there_is_no_percentage(views):
    assert _post_line(_row(views, likes=1)).get("engagement_rate_pct") is None


@pytest.mark.parametrize("views, expected", [(30, 10.0), (100, 3.0), (310, 0.97)])
def test_at_and_above_the_floor_the_rate_is_reported(views, expected):
    """The fix must not become an outage — most of his posts still get a rate."""
    line = _post_line(_row(views, likes=3))
    assert line["engagement_rate_pct"] == pytest.approx(expected, abs=0.01)


def test_the_floor_is_where_one_like_stops_swinging_it():
    """The calibration, as an assertion rather than a comment: at the floor a
    single extra interaction may move the rate by no more than ~3.5 points."""
    assert 100.0 / _RATE_MIN_VIEWS <= 3.5
    # ...and one step below it still swings by more, so the floor is not
    # needlessly high.
    assert 100.0 / (_RATE_MIN_VIEWS - 10) > 3.5


def test_zero_views_is_unchanged():
    assert _post_line(_row(0, likes=0)).get("engagement_rate_pct") is None


def test_a_post_with_no_interaction_keys_reports_neither():
    line = _post_line({"post_id": "1", "text": "x", "metrics": {"views": 500}})
    assert "interactions" not in line
    assert "engagement_rate_pct" not in line


# --------------------------------------------------------------------------- #
# the account-wide median
# --------------------------------------------------------------------------- #

def test_a_three_view_post_does_not_drag_the_account_median():
    """It does not only mislead about itself. Before the floor, a 33% row went
    into the median that answers "how is my engagement"."""
    from app.account_facts import _interaction_totals

    rows = [_row(3, likes=1)] + [_row(1000, likes=10) for _ in range(4)]
    out = _interaction_totals(rows)
    assert out["median_engagement_rate_pct"] == pytest.approx(1.0, abs=0.01), (
        "the 33% row from a 3-view post is still in the median")
    assert out["total_likes"] == 41, "totals are unaffected — a like is a like"


# --------------------------------------------------------------------------- #
# The same floor, on the momentum verdict
# --------------------------------------------------------------------------- #

_BASELINE = {"views_per_hour_median": 20.0, "replies_per_hour_median": 1.0, "sample": 30}


def test_a_one_view_post_is_not_taking_off():
    """The walker's worst case, replayed: 1 view, 24 seconds old, told "answer
    the best replies now" — with zero replies to answer. Dividing 1 view by a
    fraction of an hour produces a huge rate, and the only floor was on the
    BASELINE's sample size, never on the post's own views."""
    from app.learning_loop import momentum_verdict
    assert momentum_verdict(1 / 0.25, 0.0, _BASELINE, views=1.0) is None


@pytest.mark.parametrize("views", [1, 5, 17, 29])
def test_below_the_floor_there_is_no_momentum_verdict(views):
    from app.learning_loop import momentum_verdict
    assert momentum_verdict(views / 0.25, 0.0, _BASELINE, views=float(views)) is None


def test_at_and_above_the_floor_a_real_run_still_reads_as_taking_off():
    """The floor must not silence the legitimate firings — 133 of the walker's
    217 survive it."""
    from app.learning_loop import momentum_verdict
    verdict = momentum_verdict(300.0, 2.0, _BASELINE, views=300.0)
    assert verdict and verdict["status"] == "taking_off"
    edge = momentum_verdict(30.0, 1.0, _BASELINE, views=float(_RATE_MIN_VIEWS))
    assert edge is not None, "the floor is inclusive at its own value"


def test_the_floor_is_the_same_constant_not_a_copy():
    """A second 30 living in learning_loop.py is a number that drifts."""
    import ast, inspect, textwrap
    from app import learning_loop
    tree = ast.parse(textwrap.dedent(inspect.getsource(learning_loop.momentum_verdict)))
    fn = tree.body[0]
    ast.get_docstring(fn)                      # prose may say "30 views"; code may not
    fn.body = fn.body[1:] if ast.get_docstring(fn) else fn.body
    literals = {n.value for n in ast.walk(ast.Module(body=fn.body, type_ignores=[]))
                if isinstance(n, ast.Constant) and isinstance(n.value, (int, float))}
    assert "_RATE_MIN_VIEWS" in inspect.getsource(learning_loop.momentum_verdict)
    assert 30 not in literals, f"floor hard-coded instead of imported: {literals}"
