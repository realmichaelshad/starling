"""Delete Advisor (§7.5) and the learning-loop additions (§9.2).

The Delete Advisor's ordering was INVERTED before this: it was driven by reach
percentile and post age, when §7.5.6 says user safety and ownership outweigh
reach, engagement, historical data and growth opportunity — and §7.5.1 forbids
using a view count to pressure someone into leaving a post up.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import delete_advisor as da
from app import evidence as ev
from app import learning_spec as ls
from app import storage

NOW = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)


def _post(**kw):
    base = {"post_id": "p1", "post_type": "post",
            "created_at": (NOW - timedelta(hours=5)).isoformat(),
            "metrics": {"views": 50_000, "likes": 900, "replies": 40, "reposts": 120}}
    base.update(kw)
    return base


# --------------------------------------------------------------------------- #
# §7.5.6 — severity first, reach never as a veto
# --------------------------------------------------------------------------- #

def test_a_privacy_reason_deletes_immediately_whatever_the_reach():
    """The inversion this module exists to fix: a viral post with a privacy
    problem must not trigger a retention argument."""
    advice = da.advise(_post(), reason_key="privacy_safety", now=NOW)
    assert advice.recommendation == da.DELETE
    assert advice.urgency == "immediate"
    assert advice.show_reach is False, "never argue reach against a safety removal"
    assert advice.reach_note is None


def test_high_reach_does_not_change_a_safety_recommendation():
    quiet = da.advise(_post(metrics={"views": 3}), reason_key="privacy_safety", now=NOW)
    viral = da.advise(_post(metrics={"views": 5_000_000}), reason_key="privacy_safety", now=NOW)
    assert quiet.recommendation == viral.recommendation == da.DELETE


def test_delete_priority_ranks_by_reason_not_by_metrics():
    privacy = da.advise(_post(), reason_key="privacy_safety", now=NOW).delete_priority
    regret = da.advise(_post(), reason_key="low_performance", now=NOW).delete_priority
    assert privacy > regret


def test_delete_now_is_always_available():
    """§7.5.4: the card must never obscure it."""
    for key, _label, _severity in da.REASONS:
        assert da.advise(_post(), reason_key=key, now=NOW).as_dict()["delete_now_available"]


def test_no_reason_is_required_at_all():
    advice = da.advise(_post(), reason_key=None, now=NOW)
    assert advice.recommendation in (da.PAUSE, da.KEEP, da.DELETE)


# --------------------------------------------------------------------------- #
# §7.5.5 — the five distinct actions
# --------------------------------------------------------------------------- #

def test_a_factual_error_inside_the_edit_window_offers_a_correction():
    fresh = _post(created_at=(NOW - timedelta(minutes=20)).isoformat())
    advice = da.advise(fresh, reason_key="factual_error", now=NOW)
    assert advice.edit_eligible is True
    assert advice.recommendation == da.CORRECT
    assert any(alt["action"] == da.DELETE for alt in advice.alternatives)


def test_a_reply_is_never_edit_eligible():
    """X does not allow editing replies."""
    reply = _post(post_type="reply", created_at=(NOW - timedelta(minutes=5)).isoformat())
    assert da.advise(reply, reason_key="factual_error", now=NOW).edit_eligible is False


def test_an_old_post_is_outside_the_edit_window():
    old = _post(created_at=(NOW - timedelta(hours=3)).isoformat())
    assert da.advise(old, reason_key="factual_error", now=NOW).edit_eligible is False


def test_a_duplicate_is_simply_deleted():
    advice = da.advise(_post(), reason_key="duplicate", now=NOW)
    assert advice.recommendation == da.DELETE
    assert any(alt["action"] == da.REPLACE for alt in advice.alternatives)


def test_a_replacement_is_drafted_before_deleting():
    advice = da.advise(_post(), reason_key="replace", now=NOW)
    assert advice.recommendation == da.REPLACE
    assert any("near-duplicate" in c for c in advice.considerations)


# --------------------------------------------------------------------------- #
# §7.5.4 / §7.5.7 — honesty about what deletion does
# --------------------------------------------------------------------------- #

def test_the_deletion_scope_is_always_disclosed():
    advice = da.advise(_post(), reason_key="tone_brand", now=NOW)
    assert "third-party caches may remain" in advice.deletion_scope


def test_a_well_performing_post_shows_the_cost_without_persuading():
    advice = da.advise(_post(), reason_key="doing_well_but", now=NOW)
    assert advice.recommendation == da.PAUSE
    assert advice.reach_note and "views" in advice.reach_note
    assert any("not more important than your reason" in c for c in advice.considerations)


def test_low_performance_defaults_to_keeping_without_pressure():
    advice = da.advise(_post(), reason_key="low_performance", now=NOW)
    assert advice.recommendation == da.KEEP
    assert any("still delete it at any time" in c for c in advice.considerations)


def test_a_deletion_is_a_rejection_signal_not_a_failure_label():
    """§7.5.7: never proof the post performed badly, never an X penalty."""
    record = da.after_decision("p1", da.DELETE, reason_key="tone_brand")["record"]
    assert record["excluded_from_voice_examples"] is True
    assert record["labelled_failure"] is False
    assert record["treated_as_x_penalty"] is False


# --------------------------------------------------------------------------- #
# §9.2 checkpoint series
# --------------------------------------------------------------------------- #

def test_all_five_checkpoints_exist_including_seven_days():
    assert [label for label, _ in ls.CHECKPOINTS] == ["15m", "1h", "6h", "24h", "7d"]


def test_a_missing_checkpoint_is_unknown_not_interpolated():
    posted = (NOW - timedelta(days=8)).isoformat()
    storage.save_metric_snapshot({
        "post_id": "cp1", "captured_at": (NOW - timedelta(days=8) + timedelta(hours=1)).isoformat(),
        "metrics": {"views": 100}, "source": "test"})
    series = ls.checkpoint_series("cp1", posted_at=posted, now=NOW)
    assert series["checkpoints"]["1h"]["known"] is True
    assert series["checkpoints"]["7d"]["value"] == 100, "carries the last reading before it"
    assert series["checkpoints"]["15m"]["known"] is False
    assert "never" in series["note"] and "interpolated" in series["note"]


def test_maturity_needs_a_twenty_four_hour_reading():
    posted = (NOW - timedelta(hours=2)).isoformat()
    storage.save_metric_snapshot({
        "post_id": "cp2", "captured_at": (NOW - timedelta(hours=1)).isoformat(),
        "metrics": {"views": 10}, "source": "test"})
    # now=NOW freezes the clock; without it this asserted maturity against real
    # wall-time and started failing the day the fixed NOW fell >24h in the past.
    assert ls.checkpoint_series("cp2", posted_at=posted, now=NOW)["mature"] is False


# --------------------------------------------------------------------------- #
# §9.2a action hypothesis and experiments
# --------------------------------------------------------------------------- #

def test_the_hypothesis_is_recorded_before_the_post():
    class Analysis:
        audience_forecast = {"primary_action": "share", "secondary_action": "reply",
                             "core": "local inference people",
                             "failure_mode": "may read as a recap"}
        angles = [{"kind": "new_evidence"}]

    class Decision:
        primary_objective = "distribution"

    hypothesis = ls.hypothesis_from(Decision(), Analysis())
    assert hypothesis.primary_action == "share"
    assert hypothesis.archetype == "new_evidence"
    assert hypothesis.failure_mode and hypothesis.as_dict()["recorded_at"]


def test_actual_versus_predicted_is_comparable_at_all():
    """Impossible before: nothing recorded what was expected."""
    hypothesis = ls.ActionHypothesis(primary_action="share")
    result = ls.compare_to_hypothesis(
        hypothesis, {"metrics": {"reposts": 40, "likes": 100}})
    assert result["verdict"] == "as_predicted"


def test_the_wrong_action_is_named_rather_than_averaged_away():
    hypothesis = ls.ActionHypothesis(primary_action="share")
    result = ls.compare_to_hypothesis(
        hypothesis, {"metrics": {"reposts": 0, "replies": 55}})
    assert result["verdict"] == "wrong_action"
    assert "reply" in result["note"]


def test_distribution_and_conversion_are_reported_separately():
    """A high-view post with weak conversion is a win AND a miss (§9.2a)."""
    hypothesis = ls.ActionHypothesis(primary_action="share")
    result = ls.compare_to_hypothesis(
        hypothesis, {"metrics": {"reposts": 500, "follows": 0}})
    assert result["distribution_signal"] == 500
    assert result["conversion_signal"] == 0


def test_an_experiment_changing_everything_teaches_nothing():
    result = ls.experiment_is_clean(
        {"archetype": "thesis", "format": "post", "primary_objective": "authority"},
        {"archetype": "evidence", "format": "reply", "primary_objective": "distribution"})
    assert result["clean"] is False
    assert "3 variables changed" in result["reason"]


def test_a_single_variable_change_is_attributable():
    result = ls.experiment_is_clean({"archetype": "thesis", "format": "post"},
                                    {"archetype": "evidence", "format": "post"})
    assert result["clean"] is True


# --------------------------------------------------------------------------- #
# §9.3 auto vs confirm
# --------------------------------------------------------------------------- #

def test_measurements_update_silently_but_voice_rules_do_not():
    assert ls.update_policy("metric_snapshots")["auto"] is True
    assert ls.update_policy("permanent_voice_rules")["auto"] is False
    assert ls.update_policy("hard_avoidances")["auto"] is False


def test_an_unknown_field_defaults_to_asking():
    policy = ls.update_policy("something_new")
    assert policy["auto"] is False and "default to asking" in policy["reason"]
