"""Radar as an opportunity filter (§4): grouping, momentum, queues, packets.

What existed before was a trend list grouped by lowercased NAME with a two-term
rising score, which cannot distinguish a genuine development from recycled
commentary, and had no notion of whether anything was left to say.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import radar_pipeline as rp
from app import storage

NOW = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)


def _post(text: str, *, handle: str = "a", minutes_ago: float = 10, **kw):
    payload = {
        "post_id": kw.pop("post_id", f"{handle}-{minutes_ago}-{abs(hash(text)) % 9999}"),
        "author_handle": handle, "text": text,
        "created_at": (NOW - timedelta(minutes=minutes_ago)).isoformat(),
    }
    payload.update(kw)
    return payload


# --------------------------------------------------------------------------- #
# §4.1a grouping
# --------------------------------------------------------------------------- #

def test_posts_about_one_event_group_together():
    posts = [
        _post("Qwen3.5 released today with a much larger context window", handle="a"),
        _post("The new Qwen3.5 release has a bigger context window than before", handle="b"),
        _post("Completely unrelated thoughts about coffee brewing methods", handle="c"),
    ]
    clusters = rp.group(posts)
    sizes = sorted(len(c.posts) for c in clusters)
    assert sizes == [1, 2], "the two Qwen posts are one event, coffee is another"


def test_grouping_is_by_content_not_by_name():
    """A trend NAME cannot separate a new development from recycled commentary."""
    posts = [
        _post("Qwen3.5 benchmark results are out and context handling improved", handle="a"),
        _post("Qwen3.5 pricing changes make the API cheaper for batch work", handle="b"),
    ]
    assert len(rp.group(posts)) == 2, "same subject, different developments"


# --------------------------------------------------------------------------- #
# §4.4 momentum
# --------------------------------------------------------------------------- #

def test_acceleration_is_unknown_without_a_previous_reading():
    cluster = rp.group([_post("A developing story about model releases today", handle="a"),
                        _post("More on the developing model release story now", handle="b")])[0]
    read = rp.momentum(cluster, previous=None, now=NOW)
    assert "acceleration" in read["unknown_terms"]
    assert read["label"] in ("unclear", "fading")


def test_momentum_is_never_presented_as_a_probability():
    cluster = rp.group([_post("Something is happening in the model world", handle="a")])[0]
    read = rp.momentum(cluster, now=NOW)
    assert read["calibrated"] is False
    assert "not a probability" in read["note"]
    assert read["label"] in ("emerging", "rising", "unclear", "fading")


def test_all_six_positive_terms_are_present():
    cluster = rp.group([_post("A story", handle="a")])[0]
    read = rp.momentum(cluster, now=NOW)
    assert set(read["terms"]) == set(rp.MOMENTUM_WEIGHTS)
    assert sum(rp.MOMENTUM_WEIGHTS.values()) == pytest.approx(1.0)


def test_repeated_takes_raise_saturation():
    """Three people saying the same thing leaves less room than three saying
    different things about the same event."""
    same = "The new model is a huge leap forward for everyone building products"
    repeated = rp.Cluster(
        key="r", posts=[_post(same, handle=h, post_id=f"r{h}") for h in "abc"],
        tokens=rp._tokens(same))
    varied_texts = [
        "The new model release cuts inference cost for batch pipelines",
        "The new model release finally makes streaming latency viable",
        "The new model release changes how teams budget for evaluation runs",
    ]
    varied = rp.Cluster(
        key="v",
        posts=[_post(text, handle=h, post_id=f"v{h}")
               for text, h in zip(varied_texts, "abc")],
        tokens=set().union(*(rp._tokens(t) for t in varied_texts)))
    assert (rp.momentum(repeated, now=NOW)["saturation"]
            > rp.momentum(varied, now=NOW)["saturation"])


def test_lifecycle_has_five_stages_not_three():
    assert {rp.ORIGIN, rp.EARLY_SPREAD, rp.ACCELERATION, rp.SATURATION,
            rp.AFTERMATH} == {"origin", "early_spread", "acceleration",
                              "saturation", "aftermath"}


def test_a_single_author_is_origin_not_a_trend():
    cluster = rp.group([_post("I noticed something odd in the new release", handle="solo")])[0]
    assert rp.momentum(cluster, now=NOW)["lifecycle"] == rp.ORIGIN


# --------------------------------------------------------------------------- #
# §4.1a stage 6 — remaining angle space
# --------------------------------------------------------------------------- #

def test_angle_space_closes_when_the_user_already_covered_it():
    text = "Local inference finally beats cloud economics for batch workloads"
    cluster = rp.group([_post(text, handle="a")])[0]
    space = rp.remaining_angle_space(cluster, [text])
    assert space["closed"] is True
    assert "already covered" in space["reason"]


def test_angle_space_stays_open_for_a_fresh_subject():
    cluster = rp.group([_post("A brand new development nobody has discussed", handle="a")])[0]
    space = rp.remaining_angle_space(cluster, ["Completely different older post"])
    assert space["closed"] is False
    assert space["space"] > 0.5


# --------------------------------------------------------------------------- #
# §4.1c candidate compatibility
# --------------------------------------------------------------------------- #

def test_a_stale_conversation_is_rejected():
    result = rp.candidate_compatible(
        _post("An old but substantive point about model pricing", minutes_ago=60 * 72),
        recent_user_texts=[], now=NOW)
    assert result["eligible"] is False
    assert any("moved on" in f for f in result["failures"])


def test_a_repost_is_not_a_conversation():
    result = rp.candidate_compatible(
        _post("Something substantive that was reposted here", is_retweet=True),
        recent_user_texts=[], now=NOW)
    assert result["eligible"] is False


def test_a_point_the_user_already_made_is_rejected():
    text = "Local inference finally beats cloud economics for batch workloads"
    result = rp.candidate_compatible(_post(text), recent_user_texts=[text], now=NOW)
    assert result["eligible"] is False
    assert any("already made this point" in f for f in result["failures"])


def test_freshness_state_is_named_not_guessed():
    fresh = rp.candidate_compatible(_post("A substantive new claim about pricing", minutes_ago=5),
                                    recent_user_texts=[], now=NOW)
    assert fresh["freshness_state"] == "new"
    unknown = rp.candidate_compatible({"text": "A substantive claim with no timestamp at all"},
                                      recent_user_texts=[], now=NOW)
    assert unknown["freshness_state"] == "unknown"


# --------------------------------------------------------------------------- #
# §4.5a reply-context packet
# --------------------------------------------------------------------------- #

def test_reply_visibility_is_always_unknown():
    """The released reply service's formula is not public (§4.5a)."""
    packet = rp.reply_context_packet(_post("A claim worth responding to", handle="karpathy"))
    assert packet["reply_visibility"] == "unknown"
    assert "cannot infer" in packet["note"]


def test_the_packet_separates_root_from_parent():
    packet = rp.reply_context_packet(
        _post("A reply in a thread", post_id="child", in_reply_to_id="root1"))
    assert packet["root_id"] == "root1" and packet["selected_id"] == "child"


def test_the_packet_counts_your_prior_replies_to_the_author():
    storage.upsert_post({"post_id": "mine1", "text": "@karpathy an earlier reply",
                         "post_type": "reply", "parent_author": "karpathy",
                         "created_at": NOW.isoformat(), "source_file": "test"})
    packet = rp.reply_context_packet(_post("Another claim", handle="karpathy"))
    assert packet["your_prior_replies_to_author"] == 1


# --------------------------------------------------------------------------- #
# §4.4b the four queues
# --------------------------------------------------------------------------- #

def test_an_irrelevant_topic_is_ignored():
    clusters = rp.group([_post("Sourdough starter hydration ratios explained", handle="a")])
    result = rp.triage(clusters, recent_user_texts=[],
                       pillar_tokens={"model", "inference", "gpu"}, now=NOW)
    assert result[0].queue == rp.IGNORE


def test_a_big_topic_with_no_angle_left_is_not_act_now():
    """The whole difference between opportunity and trend-chasing (§4.4a)."""
    text = "The new model release changes everything for inference workloads"
    clusters = rp.group([_post(text, handle=h, post_id=f"p{h}") for h in "abcde"])
    result = rp.triage(clusters, recent_user_texts=[text],
                       pillar_tokens={"model", "inference"}, now=NOW)
    assert result[0].queue != rp.ACT_NOW
    assert result[0].recheck


def test_every_queue_entry_carries_a_reason():
    clusters = rp.group([_post("Some development in model inference costs", handle="a")])
    for item in rp.triage(clusters, recent_user_texts=[], now=NOW):
        assert item.reason


def test_watch_entries_name_a_recheck_trigger():
    clusters = rp.group([_post("An early signal about inference pricing shifts", handle="a")])
    watch = [i for i in rp.triage(clusters, recent_user_texts=[], now=NOW)
             if i.queue == rp.WATCH]
    for item in watch:
        assert item.recheck, "§4.4b requires the exact re-check trigger"


def test_queues_returns_all_four_keys():
    grouped = rp.queues(rp.triage(rp.group([_post("Anything at all here", handle="a")]),
                                  recent_user_texts=[], now=NOW))
    assert set(grouped) == {rp.ACT_NOW, rp.WATCH, rp.EVERGREEN, rp.IGNORE}
