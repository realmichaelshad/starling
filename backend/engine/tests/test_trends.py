"""X's trend cards, parsed so a topic can be chosen instead of listed.

Verified against the live database: of fourteen stored trend rows, ZERO had a
usable volume number. Two card shapes arrive and neither was parsed —

    name="xAI Unveils Grok 4.5 as Top Efficiency Leader"
    category=None            volume_text="1 day ago · News · 19K posts"

    name="Ghost Rider"
    category="Entertainment · Trending"     volume_text=None

— and the consumer took `name` alone, so a 19K-post launch on his beat and a
Marvel character were the same kind of thing to it, with 48 hours of trends all
counting as current.
"""
from __future__ import annotations

import pytest

from app.trends import parse, rank_for_account, relevance

BEAT = frozenset({"grok", "opus", "claude", "anthropic", "openai", "gpt",
                  "model", "launch", "benchmarks", "pricing"})

NEWS_CARD = {"name": "xAI Unveils Grok 4.5 as Top Efficiency Leader",
             "category": None, "volume_text": "1 day ago · News · 19K posts",
             "rank": 1}
SIMPLE_CARD = {"name": "Ghost Rider", "category": "Entertainment · Trending",
               "volume_text": None, "rank": 6}


def test_a_news_card_yields_its_numbers():
    trend = parse(NEWS_CARD)
    assert trend.post_count == 19_000
    assert trend.age_hours == 24.0
    assert trend.category == "News"


def test_a_simple_card_yields_its_section():
    trend = parse(SIMPLE_CARD)
    assert trend.category == "Entertainment"
    assert trend.post_count is None


@pytest.mark.parametrize("volume,expected", [
    ("Trending now · Entertainment · 20K posts", 20_000),
    ("2 hours ago · Other · 18K posts", 18_000),
    ("1,234 posts", 1_234),
    ("2.3M posts", 2_300_000),
    ("no numbers here", None),
])
def test_volumes_of_every_shape(volume: str, expected: int | None) -> None:
    assert parse({"name": "x", "volume_text": volume}).post_count == expected


def test_a_day_old_trend_is_not_fresh():
    assert not parse(NEWS_CARD).is_fresh
    assert parse({"name": "x", "volume_text": "2 hours ago · News"}).is_fresh
    assert parse({"name": "x", "volume_text": "Trending now · News"}).is_fresh


def test_his_beat_outranks_a_bigger_off_beat_trend():
    """The whole point: size does not make a topic his."""
    ranked = rank_for_account([SIMPLE_CARD, NEWS_CARD], BEAT)
    assert ranked, "an on-beat trend must survive"
    assert ranked[0].name.startswith("xAI Unveils Grok")


def test_off_beat_trends_are_dropped_entirely():
    for card in ({"name": "Black Panther", "category": "Entertainment · Trending"},
                 {"name": "Mead", "category": "Sports · Trending"}):
        assert relevance(parse(card), BEAT) == 0.0
        assert rank_for_account([card], BEAT) == []


def test_an_empty_beat_does_not_crash_a_fresh_install():
    assert isinstance(rank_for_account([NEWS_CARD], frozenset()), list)


# ---------------------------------------------------------------------------
# Who Starling watches for "what is new". The measured watchlist is built from
# accounts he has replied to, and on this machine that is a single entry —
# @grok, from one reply — on a profile whose entire subject is model launches.
# The labs themselves are derivable from his own archive through a curated
# handle map, so nothing has to be guessed.
# ---------------------------------------------------------------------------
def test_beat_handles_come_from_his_archive(monkeypatch):
    from app import release_radar
    monkeypatch.setattr("app.radar._entity_lexicon",
                        lambda: frozenset({"anthropic", "claude", "opus", "openai"}))
    handles = release_radar.beat_handles(limit=4)
    assert handles, "an account that writes about labs must watch those labs"
    assert any(h.lower() in ("anthropicai", "claudeai") for h in handles)
    assert all(not h.startswith("@") for h in handles), "bare handles, no @"


def test_an_account_that_never_mentions_a_lab_never_watches_it(monkeypatch):
    """Grounded in his archive: no vendor is invented."""
    from app import release_radar
    monkeypatch.setattr("app.radar._entity_lexicon",
                        lambda: frozenset({"knitting", "sourdough"}))
    assert release_radar.beat_handles(limit=4) == []


def test_beat_handles_do_not_repeat_a_vendor(monkeypatch):
    from app import release_radar
    monkeypatch.setattr("app.radar._entity_lexicon",
                        lambda: frozenset({"anthropic", "claude", "opus", "sonnet"}))
    handles = release_radar.beat_handles(limit=6)
    assert len(handles) == len(set(h.lower() for h in handles))


# ---------------------------------------------------------------------------------------
# TREND-BEAT-1 — "rising AND overlaps the pillars" is now actually an AND.
#
# THE LIVE DEFECT, on his card, on a post about OpenRouter token usage:
#     "Based on rising: Enzo Maresca, Villa, Coinbase · fresh web signal"
# Two Premier League footballers and a crypto exchange. All eight top trends scored
# identically with pillar_fit=0, because `fit_boost = 1.0 + 0.6 * overlap` made overlap a
# BOOST: zero overlap multiplied by 1.0 and passed at full strength.
#
# `strategy.py:73` records a repair for exactly this ("chat listing Bollywood/sports tags at
# an AI account") whose fix was to route through `_rising_for_account` — which did not
# filter. The repair never held.
# ---------------------------------------------------------------------------------------

def test_the_off_beat_trends_from_his_actual_card_are_dropped():
    from app import radar as R

    for name, category in [("Enzo Maresca", "Sports · Trending"),
                           ("Villa", "Sports · Trending"),
                           ("Coinbase", "Business and finance · Trending"),
                           ("Joao Gomes", "Sports · Trending")]:
        assert R._trend_is_on_beat({"name": name, "category": category}, 0) is False, name


def test_a_missing_category_does_not_pass_by_default():
    """28.6% OF CAPTURED ROWS HAVE NO CATEGORY.

    Anything that treats absence as acceptable reinstates the defect for a third of the data
    while looking fixed. Measured: category-kill ALONE still admits these two.
    """
    from app import radar as R

    assert R._trend_is_on_beat(
        {"name": "MrBeast Challenges Friends to Evade 100 Cops", "category": None}, 0) is False
    assert R._trend_is_on_beat(
        {"name": "Klay Thompson Set to Join Miami Heat", "category": None}, 0) is False


def test_a_generic_category_carries_no_topic():
    """"Trending" and "Trending in United States" say nothing about subject - 20% of rows."""
    from app import radar as R

    assert R._trend_is_on_beat({"name": "GUCCI MORTY", "category": "Trending"}, 0) is False
    assert R._trend_is_on_beat(
        {"name": "Cooper Thissen", "category": "Trending in United States"}, 0) is False


def test_real_ai_trends_survive():
    """The filter must not be a mute button. These are real captured rows."""
    from app import radar as R

    for name, category in [
            ("Grok 4.6 Tops CursorBench with Top Score and Lowest Cost",
             "Trending now · News · 978 posts"),
            ("Anthropic Upgrades Claude Voice with Powerful Models", "Trending now · News · 646 posts"),
            ("Developers Debate Vibe Coding's Promise and Pitfalls", "Trending now · Other · 75 posts"),
            ("Grok 4.6", "Technology · Trending")]:
        assert R._trend_is_on_beat({"name": name, "category": category}, 0) is True, name


def test_category_kills_even_when_the_name_would_pass():
    """A category can say what something ISN'T. Belt and braces for a name that matches by
    accident - a sponsorship deal naming a lab is still a sports story."""
    from app import radar as R

    assert R._trend_is_on_beat(
        {"name": "Anthropic Signs Sports Sponsorship Deal", "category": "Sports · Trending"},
        0) is False


def test_pillar_overlap_still_admits():
    """The original intent, preserved: real overlap with his declared pillars passes."""
    from app import radar as R

    assert R._trend_is_on_beat({"name": "anything at all", "category": None}, 2) is True


def test_domain_fit_was_measured_as_an_admit_test_and_rejected():
    """PINNED SO IT IS NOT ADOPTED LATER AS THE OBVIOUS CHOICE.

    `domain_fit` is a lexical strength score, not a beat test: measured, it passes
    "MrBeast Challenges Friends to Evade 100 Cops" and "Erling Haaland Unveils Buzz Cut".
    """
    from app.subject import domain_fit

    assert domain_fit("MrBeast Challenges Friends to Evade 100 Cops for $500,000 Prize") > 0
    from app import radar as R
    assert R._trend_is_on_beat(
        {"name": "MrBeast Challenges Friends to Evade 100 Cops for $500,000 Prize",
         "category": None}, 0) is False
