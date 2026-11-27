"""The day the searcher died and nobody noticed.

On 2026-07-26 every general web search this product could make returned zero
results — DuckDuckGo answered a 202 anti-bot page, Mojeek 403'd the custom
user-agent. `search_web` returned `[]`, grounding shrugged, and the model was
handed a subject with no facts attached. It supplied its own; that is where
"Grok 2.0" and "Grok-2.5" came from.

Nothing in the product said a word. These tests are about the silence as much
as the outage: a thin topic pool must not collapse into one subject, and the
selector must not offer a Marvel trend to an account that writes about models.
The sources themselves now belong to the world service's shared news pool;
what stays in the engine is how that pool is read, interleaved and selected from.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import radar                            # noqa: E402
from app.subject import interest_fit, interests, subjects   # noqa: E402

THEMES = ["model releases", "coding tools", "pricing", "performance", "benchmarks"]


def _candidate(topic: str) -> dict:
    return {"topic": topic, "angle": "a", "why": "b"}


def test_off_domain_trends_never_reach_an_ai_account():
    """"Ghost Rider Resurgence" was offered live, and picked, eleven times."""
    for off in ("Ghost Rider Resurgence", "Starship Flight Test",
                "Super Bowl Halftime Show"):
        assert interest_fit(off, THEMES) == 0.0, off
    for on in ("Midjourney Releasing v8.2", "Claude Code costs $200 a month",
               "Show HN: Integrate any CLI agent into any terminal"):
        assert interest_fit(on, THEMES) > 0.0, on


def test_themes_are_never_company_names():
    """The profile is what the account writes ABOUT, not the list of labs it mentions."""
    texts = ["Anthropic shipped Opus 5 today and the pricing is wild.",
             "OpenAI's Codex CLI is faster than Anthropic's for refactors.",
             "Benchmarks on the new Anthropic model look inflated."] * 3
    themes = interests(texts)
    assert themes, "no themes derived at all"
    lowered = " ".join(themes).lower()
    for company in ("anthropic", "openai", "xai", "google"):
        assert company not in lowered, f"{company} leaked into the profile"


def test_the_selector_rotates_instead_of_alternating(monkeypatch):
    """Least-recently-used, not first-in-list.

    Ordering by list position made the fallback flip between the same two
    topics forever: whichever was not posted last is always the first
    survivor. Measured live that produced 6 picks of one subject and 6 of
    another across 12 rounds.
    """
    pool = ["Midjourney v8.2 release", "Cursor pricing change",
            "Goose agent benchmarks", "Codex CLI latency",
            "Gemini context window update", "Grok efficiency claims"]
    memory: list[str] = []

    monkeypatch.setattr(radar, "compute_radar",
                        lambda *a, **k: {"emerging": [_candidate(t) for t in pool],
                                         "trends": [], "web": {}})
    monkeypatch.setattr(radar, "topic_staleness",
                        lambda topic, r: {"stale": False, "reason": ""})
    monkeypatch.setattr(radar, "_account_themes", lambda: THEMES)
    monkeypatch.setattr(radar, "_web_candidates", lambda themes, limit=12: [])
    monkeypatch.setattr(radar, "recent_post_topics", lambda: list(memory))
    monkeypatch.setattr(radar, "record_post_topic",
                        lambda topic, lane=None: memory.insert(0, topic))

    picks = []
    for _ in range(12):
        grounding = radar.creation_grounding(object(), budget_seconds=0.1)
        if grounding:
            picks.append(sorted(subjects(str(grounding["topic"])) or {"?"})[0])

    assert len(picks) >= 10, f"selector went silent: only {len(picks)} picks"
    distinct = len(set(picks))
    assert distinct >= 5, f"only {distinct} distinct subjects in {picks}"
    top_share = max(picks.count(p) for p in set(picks)) / len(picks)
    assert top_share <= 0.40, f"one subject took {top_share:.0%}: {picks}"


def test_the_same_subject_never_lands_twice_in_a_row(monkeypatch):
    """record_post_topic dedupes by exact topic, so a repeatedly-picked subject
    holds ONE memory slot and never looks "crowded". Live, that let the
    fallback return the identical topic eleven times running."""
    pool = ["Cursor pricing change", "Goose agent benchmarks"]
    memory: list[str] = []
    monkeypatch.setattr(radar, "compute_radar",
                        lambda *a, **k: {"emerging": [_candidate(t) for t in pool],
                                         "trends": [], "web": {}})
    monkeypatch.setattr(radar, "topic_staleness", lambda t, r: {"stale": False, "reason": ""})
    monkeypatch.setattr(radar, "_account_themes", lambda: THEMES)
    monkeypatch.setattr(radar, "_web_candidates", lambda themes, limit=12: [])
    monkeypatch.setattr(radar, "recent_post_topics", lambda: list(memory))
    monkeypatch.setattr(radar, "record_post_topic",
                        lambda topic, lane=None: memory.insert(0, topic))

    seen = []
    for _ in range(6):
        grounding = radar.creation_grounding(object(), budget_seconds=0.1)
        seen.append(str(grounding["topic"]) if grounding else None)
    consecutive = [(a, b) for a, b in zip(seen, seen[1:]) if a and b and a == b]
    assert not consecutive, f"repeated back to back: {consecutive}"


def test_a_notable_repeat_beats_an_unknown_stranger(monkeypatch):
    """The live failure, 2026-07-29: press 1 correctly took the Zuckerberg
    story; presses 2 and 3 produced Supapool and UnUI, because every
    remaining notable candidate was an Anthropic subject the variety rule had
    just skipped, and only Show HN survived.

    A recognisable subject used once recently now outranks a stranger's side
    project — while the crowding guard keeps the original defect ("5 of 6
    topics were Opus 5") dead.
    """
    pool = ["UnUI - Help coding agents make better design decisions",
            "Supapool - a Supabase per coding agent in ~400 ms",
            "Anthropic launches Cowork, a Claude Desktop agent"]
    # Anthropic used once in four — recent, but nowhere near crowding, and
    # not the subject of the last post (which would block it outright).
    memory = ["Cursor pricing change", "Gemini context window update",
              "Claude Opus 5 benchmark results", "Goose agent benchmarks"]
    monkeypatch.setattr(radar, "compute_radar",
                        lambda *a, **k: {"emerging": [_candidate(t) for t in pool],
                                         "trends": [], "web": {}})
    monkeypatch.setattr(radar, "topic_staleness", lambda t, r: {"stale": False, "reason": ""})
    monkeypatch.setattr(radar, "_account_themes", lambda: THEMES)
    monkeypatch.setattr(radar, "_web_candidates", lambda themes, limit=12: [])
    monkeypatch.setattr(radar, "recent_post_topics", lambda: list(memory))
    monkeypatch.setattr(radar, "record_post_topic", lambda topic, lane=None: None)

    grounding = radar.creation_grounding(object(), budget_seconds=0.1)
    assert grounding, "the selector went silent"
    assert "Cowork" in str(grounding["topic"]), (
        f"took a stranger's project over recognisable news: {grounding['topic']!r}")
    assert grounding.get("off_lane") is False


def test_a_crowding_subject_is_still_blocked(monkeypatch):
    """The other half. Once a subject dominates recent posts, the account is
    monotonous and an edge topic — flagged with the wait card — is the honest
    answer. This is the defect the variety rule was built for."""
    pool = ["UnUI - Help coding agents make better design decisions",
            "Anthropic launches Cowork, a Claude Desktop agent"]
    memory = ["Claude Opus 5 pricing", "Anthropic ships Claude Code skills",
              "Opus 5 benchmark results"]              # anthropic everywhere
    monkeypatch.setattr(radar, "compute_radar",
                        lambda *a, **k: {"emerging": [_candidate(t) for t in pool],
                                         "trends": [], "web": {}})
    monkeypatch.setattr(radar, "topic_staleness", lambda t, r: {"stale": False, "reason": ""})
    monkeypatch.setattr(radar, "_account_themes", lambda: THEMES)
    monkeypatch.setattr(radar, "_web_candidates", lambda themes, limit=12: [])
    monkeypatch.setattr(radar, "recent_post_topics", lambda: list(memory))
    monkeypatch.setattr(radar, "record_post_topic", lambda topic, lane=None: None)

    grounding = radar.creation_grounding(object(), budget_seconds=0.1)
    assert grounding
    assert "UnUI" in str(grounding["topic"]), (
        "a crowding subject was allowed a fourth run in a row")
    assert grounding.get("off_lane") is True, "the wait card must say it is off-lane"


def test_no_single_source_can_flood_the_pool(monkeypatch):
    """The pool was sorted by created_at descending and only one source carried
    a timestamp, so it meant "all of that source, then everyone else" — one
    source held positions 0-28 of a 60-item cache, and the lab sources added to
    fix the topic problem were cut off the end. Interleaving bounds a source's
    share by how many sources there are, whatever order the shared pool uses."""
    import app.news as news
    from app import cloud

    items = ([{"title": f"HN story {n}", "url": f"https://example.com/h{n}",
               "source": "Hacker News", "points": 5, "published_at": 1000 + n,
               "summary": ""} for n in range(30)]
             + [{"title": f"Lab post {n}", "url": f"https://example.com/l{n}",
                 "source": "OpenAI News", "points": 0, "published_at": 0,
                 "summary": ""} for n in range(8)]
             + [{"title": f"DM post {n}", "url": f"https://example.com/d{n}",
                 "source": "Google DeepMind", "points": 0, "published_at": 0,
                 "summary": ""} for n in range(8)])

    # conftest sets STARLING_RESEARCH=off for every test so nothing can reach
    # the network. This test fakes the world service itself and exercises the
    # FETCH path, so it has to opt in exactly as production does.
    monkeypatch.setenv("STARLING_RESEARCH", "on")
    monkeypatch.setattr(cloud, "is_configured", lambda: True)
    monkeypatch.setattr(cloud, "news_headlines",
                        lambda limit=150: {"generated_at": 1, "items": items})

    pool = news.headlines(["model releases"], limit=120, force=True)

    assert len(pool) == len(items), "a story was lost on the way in"
    first_ten = [i["source"] for i in pool[:10]]
    assert first_ten.count("Hacker News") <= 4, (
        f"one source still floods the top of the pool: {first_ten}")
    assert "OpenAI News" in first_ten and "Google DeepMind" in first_ten, (
        f"lab sources never reach the top of the pool: {first_ten}")


def test_newsletter_digests_never_become_topics():
    """The source list is a judgement; this is the mechanism. A digest lists
    a dozen unrelated stories, so a post grounded on one is a post about a
    newsletter — and both examples scored NOTABLE because they name every lab
    at once."""
    from app.intelligence import is_roundup_topic
    for digest in ("[AINews] Fearing RSI: OpenAI, Anthropic, GDM cosign letter",
                   "Import AI 465: Open vs closed gaps; Kimi K3; policy plan",
                   "The Neuron Issue #412: what shipped this week"):
        assert is_roundup_topic(digest), f"digest passed as a topic: {digest!r}"
    # ...and the news itself still gets through.
    for launch in ("Introducing Gemini 3.6 Flash, 3.5 Flash-Lite and Cyber",
                   "How GPT-5.6 fuses frontier intelligence with efficiency",
                   "Kimi K3 vs GPT-5.6 Sol on DeepSWE: Cost, Coding, Routing",
                   "Microsoft AI Releases MAI-Cyber-1-Flash, a 5B model",
                   "Anthropic launches Cowork, a Claude Desktop agent"):
        assert not is_roundup_topic(launch), f"real news filtered: {launch!r}"


def test_the_wait_card_trigger_keeps_BOTH_weaknesses(monkeypatch):
    """`off_lane` IS A UNION, AND NARROWING IT IS A SILENT REGRESSION.

    `main.py` routes this ONE key to the wait card, so whatever it means is the only thing the
    card can warn about. Two different weaknesses both deserve the card:

      OFF-LANE      a funding story about a recognisable company - it names a recognisable
                    organisation, so `names_notable` cannot reject it. The lane test can.
      UNRECOGNISED  "UnUI - Help coding agents make better design decisions" - an unknown Show
                    HN project. It now earns a WEAK practice verdict from `coding agents`, so
                    the lane test cannot reject it. `names_notable` can.

    Narrowing this key to the lane test alone made the card go quiet on the second case - the
    precise case the wait card was built for - and this test is what caught it. Neither half
    may be dropped while `main.py` reads only this key. `topic_audition` reports them
    separately so the card can eventually say WHICH.
    """
    from app import lane as lane_mod

    def grounding_for(topic: str) -> dict:
        monkeypatch.setattr(radar, "compute_radar",
                            lambda *a, **k: {"emerging": [_candidate(topic)],
                                             "trends": [], "web": {}})
        monkeypatch.setattr(radar, "topic_staleness",
                            lambda t, r: {"stale": False, "reason": ""})
        monkeypatch.setattr(radar, "_account_themes", lambda: THEMES)
        monkeypatch.setattr(radar, "_web_candidates", lambda themes, limit=12: [])
        monkeypatch.setattr(radar, "recent_post_topics", lambda: [])
        monkeypatch.setattr(radar, "record_post_topic", lambda t, lane=None: None)
        return radar.creation_grounding(object(), budget_seconds=0.1)

    unknown = "UnUI - Help coding agents make better design decisions"
    assert lane_mod.classify(unknown).lane is not None, \
        "premise: this one IS laned, so only the unrecognised half can flag it"
    got = grounding_for(unknown)
    assert got["unrecognised"] is True
    assert got["lane_missing"] is False
    assert got["off_lane"] is True, "the card went quiet on an unknown project"
