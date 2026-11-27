"""`plan_post` — the moment decides the register, and it defaults to shitpost.

THE REGRESSION THIS FILE EXISTS FOR is `test_release_topic_without_age_is_a_shitpost`:
it reproduces today's product, which treats every undated headline as newsworthy
because 87% of the pool carries no date. If that test ever goes green while
returning "facts", the detector has been rebuilt with the defect.

Every case is a literal grounding dict and an injected clock — no DB, no
network, no wall clock — so a failure here is a failure of the rule, never of
the environment.

WHICH OF THESE COVER A LIVE PATH: `plan_post` has ONE caller, guarded by
`if grounding and grounding.get("topic")`, and the alternative branch returns a
wait card rather than drafting. So a grounding reaching this module ALWAYS has
a truthy topic, and the no-topic cases below are defensive only — marked as
such at each one. The distinction is not pedantry: a rate was once measured for
an ungrounded-press defect that cannot occur, and the tests looked identical.
"""
import pytest

from app.post_plan import (BREAKING_AGE_SECONDS, QUIET_AGE_SECONDS, PostRoom,
                           SHITPOST_SIZE_CLASSES, plan_post, read_the_moment)

NOW = 1_700_000_000.0


def _room(grounding, now=NOW):
    return read_the_moment(grounding, now=now)


# --------------------------------------------------------------- breaking

def test_a_verified_shipped_release_is_the_fact_register():
    room = _room({"release": {"status": "shipped"}, "topic": "Opus 5"})
    assert room.register == "facts"
    assert room.moment == "breaking"
    assert room.move == "state_the_fact"
    assert room.tone == "direct"


def test_a_two_hour_old_candidate_is_breaking():
    room = _room({"published_at": NOW - 2 * 3600, "topic": "anything"})
    assert room.moment == "breaking"
    assert room.register == "facts"
    assert room.signals["age_source"] == "published_at"


def test_created_at_is_the_fallback_when_published_at_is_absent():
    room = _room({"created_at": NOW - 3600, "topic": "anything"})
    assert room.register == "facts"
    assert room.signals["age_source"] == "created_at"


def test_the_boundary_is_closed_at_six_hours():
    """Exactly 6h is NOT breaking — the window is `age < 6h`."""
    assert _room({"published_at": NOW - BREAKING_AGE_SECONDS}).register == "shitpost"
    assert _room({"published_at": NOW - BREAKING_AGE_SECONDS + 1}).register == "facts"


# -------------------------------------------------------- recent and old

def test_a_thirty_hour_old_candidate_is_old():
    room = _room({"published_at": NOW - 30 * 3600, "topic": "anything"})
    assert room.moment == "old"
    assert room.register == "shitpost"
    assert room.tone == "funny"
    assert room.size_classes == SHITPOST_SIZE_CLASSES


def test_no_date_at_all_is_unknown():
    room = _room({"topic": "a quiet afternoon"})
    assert room.moment == "unknown"
    assert room.register == "shitpost"


# ------------------------------------------------- the band is four, not two

def test_a_twelve_hour_story_is_not_the_same_moment_as_a_four_hundred_hour_one():
    """THE ASSERTION THAT STOPS THE BAND COLLAPSING BACK TO TWO.

    An earlier version of this module labelled both `quiet`, because the two
    branches producing that label differed only in their reason wording and
    collapsing them looked safe. The routing was right and the provenance was
    not — and the column exists precisely to answer "which register performs",
    which a collapsed band silently biases.
    """
    twelve = _room({"published_at": NOW - 12 * 3600}).moment
    four_hundred = _room({"published_at": NOW - 400 * 3600}).moment
    assert twelve != four_hundred


def test_the_quiet_boundary_is_actually_read():
    """`QUIET_AGE_SECONDS` must be load-bearing, not a phantom knob."""
    assert _room({"published_at": NOW - QUIET_AGE_SECONDS + 1}).moment == "recent"
    assert _room({"published_at": NOW - QUIET_AGE_SECONDS}).moment == "old"


def test_every_band_is_reachable():
    """All four labels must be producible, or one of them is dead."""
    seen = {
        _room({"release": {"status": "shipped"}}).moment,
        _room({"published_at": NOW - 10 * 3600}).moment,
        _room({"published_at": NOW - 100 * 3600}).moment,
        _room({"topic": "x"}).moment,
    }
    assert seen == {"breaking", "recent", "old", "unknown"}


def test_the_register_stays_a_two_way_decision():
    """Only `breaking` admits facts; the other three bands are all shitpost."""
    assert _room({"release": {"status": "shipped"}}).register == "facts"
    for grounding in ({"published_at": NOW - 10 * 3600},
                      {"published_at": NOW - 100 * 3600},
                      {"topic": "x"}):
        assert _room(grounding).register == "shitpost", grounding


# ---------------------------------------------------------------- unknown

def test_release_topic_without_age_is_a_shitpost():
    """THE REGRESSION. This is today's product, and it must not come back.

    `is_release_topic` matches release VOCABULARY and fires on week-old
    retrospectives. It may mark the moment `unknown`; it may never promote to
    breaking on its own.
    """
    room = _room({"topic": "GPT-5.6 launch: everything announced"})
    assert room.register == "shitpost", "an undated release topic became a bulletin"
    assert room.moment == "unknown"
    assert room.signals["release_topic"] is True, "the signal must still be recorded"


def test_two_sources_without_age_is_unknown_and_still_a_shitpost():
    room = _room({"topic": "something", "evidence_sources": ["a.com", "b.com"]})
    assert room.moment == "unknown"
    assert room.register == "shitpost"
    assert room.signals["evidence_sources"] == 2


def test_duplicate_sources_do_not_count_twice():
    room = _room({"topic": "something", "evidence_sources": ["a.com", "a.com"]})
    assert room.signals["evidence_sources"] == 1
    assert room.moment == "unknown"


# ----------------------------------------------------- never raises, ever

# EVERY CASE BELOW IS UNREACHABLE IN PRODUCTION, AND THAT IS RECORDED RATHER
# THAN QUIETLY TRUE. `plan_post` is called at exactly one site, inside
# `if grounding and grounding.get("topic"):` — and the branch that would run
# without a topic RETURNS A WAIT CARD instead of drafting. So the composer
# never hands this function None, a bare dict, or a non-dict at all.
#
# The tests stay: a never-raises contract is worth holding on a function whose
# whole job is to fail into the safe register. But NONE OF THEM MAY BE QUOTED
# AS EVIDENCE THE BEHAVIOUR SHIPS. A campaign spent an afternoon on a defect
# that "an ungrounded post keeps the fact profile", measured it three ways,
# and got a rate for a case with no instances — because a green test over an
# uncalled path looks exactly like a green test over a live one.
@pytest.mark.parametrize("grounding", [
    None, {}, [], "a string", 7,
    {"release": None}, {"release": "shipped"}, {"published_at": "not a number"},
    {"published_at": None}, {"evidence_sources": "not a list"},
    {"candidate": {"published_at": None}},
])
def test_an_unreachable_grounding_still_yields_a_shitpost_and_no_exception(grounding):
    """DEFENSIVE ONLY — no caller can produce these. See the note above."""
    instruction, room = plan_post(grounding, now=NOW)
    assert isinstance(room, PostRoom)
    assert room.register == "shitpost"
    assert instruction, "the shitpost brief must still carry its move text"


# THE REACHABLE HALF OF THE SAME CONTRACT, which the block above does not cover.
# A live grounding ALWAYS has a truthy topic — that is the guard that let it
# through — but every other field is radar output and can be malformed. These
# are the malformed groundings the composer can actually hand over, so a failure
# here is a real failure and not a hypothetical one.
@pytest.mark.parametrize("grounding", [
    {"topic": "x", "published_at": "not a number"},
    {"topic": "x", "published_at": None},
    {"topic": "x", "created_at": "yesterday"},
    {"topic": "x", "release": "shipped"},
    {"topic": "x", "release": {"status": None}},
    {"topic": "x", "evidence_sources": "not a list"},
    {"topic": "x", "evidence_sources": [None, ""]},
    {"topic": "x", "candidate": {"published_at": None}},
])
def test_a_REACHABLE_malformed_grounding_is_a_shitpost_and_never_raises(grounding):
    """These CAN arrive: topic present, everything else radar-shaped garbage."""
    instruction, room = plan_post(grounding, now=NOW)
    assert isinstance(room, PostRoom)
    assert room.register == "shitpost", grounding
    assert room.moment == "unknown", "an unreadable date is not a fresh one"
    assert instruction


def test_a_future_timestamp_is_broken_not_fresh():
    """A clock skew must not manufacture breaking news."""
    room = _room({"published_at": NOW + 9999})
    assert room.register == "shitpost"
    assert room.signals["age_source"] == "none"


def test_zero_is_absent_not_1970():
    """`created_at = 0` is the hardcoded no-date value, not the epoch."""
    room = _room({"created_at": 0, "topic": "x"})
    assert room.signals["age_source"] == "none"
    assert room.register == "shitpost"


# ------------------------------------------------------- the brief itself

def test_the_two_registers_return_different_briefs():
    _, quiet = plan_post({"topic": "x"}, now=NOW)
    _, loud = plan_post({"release": {"status": "shipped"}}, now=NOW)
    from app.post_plan import post_move
    assert post_move(quiet) != post_move(loud)
    assert "bulletin" in post_move(quiet)
    assert "shipped" in post_move(loud)


def test_the_shitpost_brief_forbids_the_second_sentence():
    """The measured defect is a thesis where a line belongs."""
    _, room = plan_post({"topic": "x"}, now=NOW)
    from app.post_plan import post_move
    brief = post_move(room).lower()
    assert "one line" in brief
    assert "thesis" in brief


def test_the_facts_path_leaves_size_planning_alone():
    """Only the shitpost register forces its size classes."""
    room = _room({"release": {"status": "shipped"}})
    assert room.size_classes == ()


# RETIRED (API-native pass, 2026-09-22): test_the_shitpost_direction_carries_ONE_profile_not_two: the post register no longer reaches a draft (the model writes in the writer's voice).


def test_the_reason_is_always_populated():
    """Every routing decision has to be explainable on the card."""
    for grounding in ({"release": {"status": "shipped"}},
                      {"published_at": NOW - 3600},
                      {"published_at": NOW - 99 * 3600},
                      {"topic": "GPT-5.6 launch"},
                      {"topic": "nothing"}):
        assert _room(grounding).reason, grounding
