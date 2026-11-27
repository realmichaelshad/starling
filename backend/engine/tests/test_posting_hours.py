"""The "it's 4am, wait" decision — measured per account, in the account's own clock.

The bar every rejecting gate here has to clear is the usual one: it must never
fire on an hour the account actually posts in. That is true by construction (a
dead hour has zero posts) and checked against the real archive anyway, because
"true by construction" is how the last several inert gates got shipped.

EVERY TEST PINS ITS TIMEZONE. Without `tz=` these expectations would be written
in whatever zone the developer's laptop is in, and the suite would pass in New
York and fail in Amsterdam. That is also the bug this module exists to fix, so
letting it back in through the tests would be a poor joke.

Nothing here may be tuned to one account. The synthetic accounts are the real
test: a new user, a round-the-clock user, and one whose quiet stretch crosses
midnight.
"""
from datetime import datetime, timezone

from app.posting_hours import (MIN_DEAD_RUN_HOURS, MIN_EXPECTED_POSTS_IN_RUN,
                               MIN_POSTS_FOR_AN_OPINION, _longest_zero_run,
                               dead_hours, hour_verdict)

# HIS 41 ARCHIVED POSTS, as UTC hours, measured 2026-07-31 straight from
# `performance_records()`. Recorded here so the calibration RUNS: the suite
# uses an isolated database, so a test that reads live storage skips — which is
# how the zero-false-positive check would have protected nothing at all.
_HIS_POST_HOURS_UTC = [1, 2, 2, 3, 4, 4, 12, 13, 13, 14, 14, 15, 15, 15, 16,
                       16, 16, 16, 17, 17, 17, 18, 18, 18, 19, 19, 20, 20, 20,
                       20, 20, 21, 21, 22, 22, 22, 22, 22, 23, 23, 23]
# The zone that turns those hours into an ordinary human day. In UTC they read
# as a strange split pattern; here they are "asleep 1 AM - 8 AM, peak 4 PM".
_HIS_ZONE = "America/New_York"


def _rows(hours, day=15):
    """Synthetic posts at the given UTC hours."""
    return [{"post_type": "post",
             "created_at": datetime(2026, 7, day, hour % 24, 30,
                                    tzinfo=timezone.utc).isoformat()}
            for hour in hours]


def _at(hour_utc, day=31):
    return datetime(2026, 7, day, hour_utc % 24, 30, tzinfo=timezone.utc)


# --- the account's own data ------------------------------------------------ #

def test_it_never_fires_on_an_hour_the_account_posts_in():
    """Zero false positives on the real archive, checked post by post."""
    rows = _rows(_HIS_POST_HOURS_UTC)
    fired = [hour for hour in sorted(set(_HIS_POST_HOURS_UTC))
             if hour_verdict(_at(hour), rows, tz=_HIS_ZONE)["dead"]]
    assert not fired, (
        f"the wait gate fires at UTC hours this account really posts in: {fired}")


def test_the_real_archive_has_a_measurable_quiet_stretch():
    facts = dead_hours(_rows(_HIS_POST_HOURS_UTC), tz=_HIS_ZONE)
    assert facts["confident"], facts
    assert facts["sample"] == 41
    # 01:00-07:59 local — seven hours, not one of which has a post in it.
    assert facts["hours"] == [1, 2, 3, 4, 5, 6, 7], facts
    assert facts["expected"] >= MIN_EXPECTED_POSTS_IN_RUN


def test_four_in_the_morning_is_a_wait_for_this_account():
    """The thing he actually asked for, in his real clock."""
    rows = _rows(_HIS_POST_HOURS_UTC)
    four_am_local = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)   # 04:00 EDT
    verdict = hour_verdict(four_am_local, rows, tz=_HIS_ZONE)
    assert verdict["dead"], verdict
    assert "4 AM" in verdict["reason"]


def test_the_middle_of_his_day_is_not():
    rows = _rows(_HIS_POST_HOURS_UTC)
    four_pm_local = datetime(2026, 7, 31, 20, 0, tzinfo=timezone.utc)  # 16:00 EDT
    assert not hour_verdict(four_pm_local, rows, tz=_HIS_ZONE)["dead"]


# --- generality: this must work for accounts that are not his -------------- #

def test_a_new_account_is_never_held_back():
    """Below the floor we have no opinion, and no opinion must not read as wait."""
    rows = _rows([9, 10, 11, 14, 15])
    facts = dead_hours(rows, tz="UTC")
    assert not facts["confident"] and facts["hours"] == []
    for hour in range(24):
        assert not hour_verdict(_at(hour), rows, tz="UTC")["dead"], (
            f"a five-post account was told to wait at {hour:02d}:00")


def test_an_account_that_posts_round_the_clock_has_no_dead_hours():
    facts = dead_hours(_rows(list(range(24)) * 3), tz="UTC")
    assert facts["hours"] == [] and not facts["confident"], facts


def test_a_quiet_stretch_that_crosses_midnight_is_seen_whole():
    """A non-wrapping scan reports two short runs and finds neither significant."""
    counts = {hour: (3 if 6 <= hour < 22 else 0) for hour in range(24)}
    run = _longest_zero_run(counts)
    assert set(run) == {22, 23, 0, 1, 2, 3, 4, 5}, run


def test_a_night_shift_account_waits_during_ITS_day():
    """The rule follows the account, not a notion of when mornings are."""
    awake = [22, 23, 0, 1, 2, 3, 4, 5, 6] * 5               # 45 posts overnight
    facts = dead_hours(_rows(awake), tz="UTC")
    assert facts["confident"], facts
    assert 13 in facts["hours"], "1 PM should be dead for a night-shift account"
    assert 2 not in facts["hours"], "2 AM is when this account actually posts"


def test_an_isolated_empty_hour_is_noise_not_a_habit():
    """One quiet hour inside an active day must not stop a post."""
    hours = [h for h in range(8, 23) if h != 15] * 3         # nothing at 15:00
    assert 15 not in dead_hours(_rows(hours), tz="UTC")["hours"], (
        "a single empty hour between two busy ones was read as a sleep window")


def test_the_significance_bar_scales_with_the_sample():
    """The same gap is meaningful at 60 posts and not at the 20-post floor."""
    gap = [h for h in range(24) if h not in (2, 3, 4, 5)]    # 20 posts, 4h gap
    assert not dead_hours(_rows(gap), tz="UTC")["confident"]
    thick = dead_hours(_rows(gap * 3), tz="UTC")             # 60 posts
    assert thick["confident"] and set(thick["hours"]) == {2, 3, 4, 5}, thick


def test_the_floor_is_a_hard_floor():
    """Even a screaming-obvious pattern is ignored under the post minimum."""
    rows = _rows([12, 13] * 9)                               # 18 posts, 22h gap
    assert len(rows) < MIN_POSTS_FOR_AN_OPINION
    assert not dead_hours(rows, tz="UTC")["confident"]


# --- the message he actually sees ------------------------------------------ #

def test_the_wait_reason_names_the_hour_the_evidence_and_the_way_out():
    rows = _rows(list(range(8, 24)) * 3)                     # quiet 00:00-07:59
    verdict = hour_verdict(_at(4), rows, tz="UTC")
    assert verdict["dead"]
    reason = verdict["reason"]
    assert "4 AM" in reason                                  # the hour, his clock
    assert str(verdict["sample"]) in reason                  # the evidence
    assert "8 AM" in reason                                  # when to come back
    assert verdict["hours_left"] == 4
    assert len(verdict["run"]) >= MIN_DEAD_RUN_HOURS


def test_the_verdict_carries_the_timezone_it_reasoned_in():
    """A wait card that cannot say which clock it used is unauditable."""
    assert dead_hours(_rows(_HIS_POST_HOURS_UTC), tz=_HIS_ZONE)["timezone"]
