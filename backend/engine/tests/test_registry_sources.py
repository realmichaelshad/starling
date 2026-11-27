"""Shaping for the two vocabulary-free story kinds, against a synthetic capture.

Every source in this product identified a story by its WORDS until these two.
That works until a lab ships under a codename picked so that nobody has seen the
word, at which point `builder_fit`, `names_notable` and any lane regex all return
False and the story is dropped at `radar.py`'s ingest gate however good the
query set is.

The engine no longer fetches these sources (all web data comes through the
world service); what remains is the shaping, which these tests pin. The capture
below is synthetic: invented listings and hits in the shape the two endpoints
return, with every age computed against `CAPTURED_AT` rather than the wall clock.
"""
import pytest

from app import registry_sources as rs
from app.subject import builder_fit, event_subject, names_notable

CAPTURED_AT = 1_780_000_000
HOUR = 3600
LONG_DESCRIPTION = (
    "A stealth reasoning model listed for early testing. It supports long context, "
    "tool calls and structured output; pricing and limits are provisional while the "
    "preview window is open.")


@pytest.fixture(scope="module")
def capture() -> dict:
    return {
        "captured_at": CAPTURED_AT,
        "openrouter_models": [
            {"id": "stealth/ox-alpha", "name": "Ox Alpha", "created": CAPTURED_AT - 23 * HOUR,
             "description": LONG_DESCRIPTION},
            {"id": "deepseek/v4-flash", "name": "DeepSeek: V4 Flash", "created": CAPTURED_AT - 10 * HOUR,
             "description": "A faster DeepSeek variant tuned for coding agents, with a larger context "
                            "window and lower per-token pricing than the previous generation."},
            {"id": "old/archive-model", "name": "Archive Model", "created": CAPTURED_AT - 5 * 24 * HOUR,
             "description": LONG_DESCRIPTION},
            {"id": "thin/model", "name": "Thin Model", "created": CAPTURED_AT - HOUR,
             "description": "too short"},
        ],
        "hn_host_hot_hits": [
            {"title": "Ox Alpha: a stealth model appears on OpenRouter",
             "url": "https://openrouter.ai/stealth/ox-alpha", "created_at_i": CAPTURED_AT - 19 * HOUR,
             "points": 223, "num_comments": 174,
             "story_text": '<a href="https://x.test/a">https://x.test/a</a>, '
                           "<p>Early testers say it reasons &amp; plans well.</p>"},
            {"title": "Claude Code adds hooks for tool calls",
             "url": "https://github.com/anthropics/claude-code/releases",
             "created_at_i": CAPTURED_AT - 6 * HOUR, "points": 120, "num_comments": 40,
             "story_text": '<a href="https://x.test/b">https://x.test/b</a>'},
            {"title": "Emacs 31 released", "url": "https://github.com/emacs-mirror/emacs",
             "created_at_i": CAPTURED_AT - 2 * HOUR, "points": 400, "num_comments": 210},
            {"title": "Trade deadline recap", "url": "https://www.espn.com/nba/story",
             "created_at_i": CAPTURED_AT - 3 * HOUR, "points": 90, "num_comments": 30},
            {"title": "Ask HN: what are you building?", "url": "",
             "created_at_i": CAPTURED_AT - 4 * HOUR, "points": 80, "num_comments": 300},
        ],
    }


# ---------------------------------------------------------------- title shaping

def test_the_bare_codename_cannot_pass_the_ingest_gate():
    """The reason this module exists. Not a defect to fix by widening the
    vocabulary - a codename SHOULD be unrecognisable."""
    assert builder_fit("Ox Alpha") is False
    assert names_notable("Ox Alpha") is False


def test_the_shaped_title_passes_and_reports_only_what_was_observed():
    title = rs.model_title("Ox Alpha", "stealth/ox-alpha")
    assert title == "Ox Alpha, a newly listed model"
    assert builder_fit(title) is True
    assert event_subject(title)
    lowered = title.lower()
    for claim in ("released", "launched", "best", "fastest", "shipped", "openrouter"):
        assert claim not in lowered


def test_a_codename_is_not_reported_as_recognisable():
    """`names_notable` must be False for a codename; appending the host would
    borrow the registry's notability for a name no reader has seen."""
    assert names_notable(rs.model_title("Ox Alpha", "stealth/ox-alpha")) is False
    # A title that DOES name a known lab still reports notable, on its own merit.
    assert names_notable(rs.model_title("DeepSeek: V4 Flash Vision", "deepseek/v4")) is True


def test_two_codenamed_listings_do_not_collide_as_the_same_subject():
    """With the host in the title, two stealth launches in the same window both
    yielded the host as a subject and the second read as a repeat of the first."""
    from app.subject import subjects
    a = subjects(rs.model_title("Ox Alpha", "stealth/ox-alpha"))
    b = subjects(rs.model_title("Nimbus 3", "stealth/nimbus-3"))
    assert a and b, "a shaped title must yield at least one subject"
    assert not (a & b), f"two codenames share subjects {a & b} and will collide"


def test_provenance_survives_in_the_evidence_even_though_it_left_the_title(capture):
    recs = rs.openrouter_models(_fixture=capture["openrouter_models"],
                                now=capture["captured_at"], limit=25)
    assert recs
    for r in recs:
        assert "OpenRouter" in r["summary"]


def test_a_nameless_model_still_yields_a_usable_title():
    assert rs.model_title("", "") == "An unnamed model, a newly listed model"


# ---------------------------------------------------------------- host filter

@pytest.mark.parametrize("url", [
    "https://openrouter.ai/stealth/ox-alpha",
    "https://www.anthropic.com/news/whatever",
    "https://github.com/anthropics/claude-code/releases",
    "https://simonwillison.net/2026/Aug/21/thing/",
])
def test_lane_hosts_are_accepted(url):
    assert rs.is_lane_host(url) is True


@pytest.mark.parametrize("url", [
    "https://github.com/emacs-mirror/emacs",
    "https://www.espn.com/nba/story",
    "https://news.ycombinator.com/item?id=1",
    "",
    "not a url",
])
def test_off_lane_hosts_are_rejected(url):
    assert rs.is_lane_host(url) is False


# ---------------------------------------------------------------- the shaping

def test_the_registry_shape_surfaces_a_codename(capture):
    recs = rs.openrouter_models(_fixture=capture["openrouter_models"],
                                now=capture["captured_at"], limit=25)
    ox = [r for r in recs if "ox alpha" in r["title"].lower()]
    assert ox, "the registry shaping no longer surfaces the codename"
    r = ox[0]
    assert r["source"] == rs.SOURCE_OPENROUTER
    assert r["url"] == "https://openrouter.ai/stealth/ox-alpha"
    assert r["created_at"] > 0
    assert len(r["summary"]) >= rs._MIN_SUMMARY
    assert builder_fit(r["title"]), "the record would be dropped at the ingest gate"


def test_the_host_shape_surfaces_a_codename_with_no_query_string(capture):
    hits = rs.hacker_news_by_host(_fixture=capture["hn_host_hot_hits"],
                                  now=capture["captured_at"], limit=25)
    ox = [h for h in hits if "ox alpha" in h["title"].lower()]
    assert ox, "the host filter no longer surfaces the codename"
    assert ox[0]["points"] >= rs.HN_MIN_POINTS
    assert "points" in ox[0]["summary"] and "comments" in ox[0]["summary"]


def test_the_host_filter_actually_rejects_most_of_the_firehose(capture):
    """A filter that keeps everything is not a filter."""
    raw = capture["hn_host_hot_hits"]
    kept = rs.hacker_news_by_host(_fixture=raw, now=capture["captured_at"], limit=100)
    assert 0 < len(kept) < len(raw) * 0.6, f"kept {len(kept)} of {len(raw)}"


def test_records_are_ranked_by_measured_conversation(capture):
    hits = rs.hacker_news_by_host(_fixture=capture["hn_host_hot_hits"],
                                  now=capture["captured_at"], limit=25)
    assert [h["points"] for h in hits] == sorted((h["points"] for h in hits), reverse=True)


# ---------------------------------------------------------------- record hygiene

def test_no_record_makes_a_time_claim_it_cannot_support():
    """0 means UNKNOWN. A record that cannot date itself is dropped, never guessed."""
    recs = rs.openrouter_models(
        _fixture=[{"id": "a/b", "name": "B", "created": 0, "description": "x" * 300}],
        now=CAPTURED_AT)
    assert recs == []


def test_thin_material_is_dropped_before_the_audition_sees_it(capture):
    recs = rs.openrouter_models(_fixture=capture["openrouter_models"],
                                now=capture["captured_at"], limit=25)
    assert all("thin/model" not in r["url"] for r in recs)


def test_stale_models_are_not_news(capture):
    recs = rs.openrouter_models(_fixture=capture["openrouter_models"],
                                now=capture["captured_at"], limit=25)
    assert all("old/archive-model" not in r["url"] for r in recs)


def test_evidence_never_carries_html_or_bare_punctuation(capture):
    hits = rs.hacker_news_by_host(_fixture=capture["hn_host_hot_hits"],
                                  now=capture["captured_at"], limit=25)
    assert hits
    for h in hits:
        s = h["summary"]
        assert "<a " not in s and "&#x" not in s and "href=" not in s and "&amp;" not in s
        assert not s.rstrip().endswith(","), f"punctuation-only tail: {s!r}"


def test_ago_is_singular_correct():
    assert rs._ago(0.5) == "in the last hour"
    assert rs._ago(1.2) == "1 hour ago"
    assert rs._ago(19.0) == "19 hours ago"
    assert rs._ago(25.0) == "1 day ago"
    assert rs._ago(72.0) == "3 days ago"


def test_plain_drops_a_link_only_comment_but_keeps_real_prose():
    linkonly = ('<a href="https://x.test/a">https://x.test/a</a>, '
                '<a href="https://x.test/b">https://x.test/b</a>')
    assert rs._plain(linkonly) == ""
    assert rs._plain("<p>Ox Alpha is a <b>reasoning</b> model.</p>") == "Ox Alpha is a reasoning model."


def test_every_emitted_source_name_is_declared(capture):
    recs = (rs.openrouter_models(_fixture=capture["openrouter_models"],
                                 now=capture["captured_at"], limit=25)
            + rs.hacker_news_by_host(_fixture=capture["hn_host_hot_hits"],
                                     now=capture["captured_at"], limit=25))
    assert recs
    for r in recs:
        assert r["source"] in rs.SOURCE_NAMES


def test_the_engine_never_fetches_these_sources(monkeypatch):
    """All web data comes through the world service: without caller-supplied
    records there is nothing to shape, and no request is attempted."""
    import socket

    def refuse(*args, **kwargs):
        raise AssertionError("registry shaping opened a network connection")

    monkeypatch.setattr(socket, "create_connection", refuse)
    assert rs.openrouter_models() == []
    assert rs.hacker_news_by_host() == []
    assert not hasattr(rs, "_requests")


# ---------------------------------------------------------------- lane provenance

def test_the_lane_constant_cannot_drift_from_the_lane_module():
    from app import lane
    assert rs.LANE_FRONTIER == lane.FRONTIER


def test_every_registry_record_declares_its_lane_from_provenance(capture):
    """A model appearing in a model registry IS the frontier lane, by where it was
    found; a classifier reading the shaped title cannot know that."""
    recs = rs.openrouter_models(_fixture=capture["openrouter_models"],
                                now=capture["captured_at"], limit=25)
    assert recs
    for r in recs:
        assert r["lane"] == rs.LANE_FRONTIER
        assert r["lane_source"] == "registry"


def test_the_classifier_alone_would_still_miss_these(capture):
    """Pins WHY the provenance hint is needed."""
    from app import lane
    recs = rs.openrouter_models(_fixture=capture["openrouter_models"],
                                now=capture["captured_at"], limit=25)
    unplaced = [r for r in recs if lane.classify(r["title"]).lane is None]
    assert unplaced, ("lane.classify now places bare model listings on its own - "
                      "re-read whether the provenance hint is still required")
