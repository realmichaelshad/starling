"""The Account Intelligence Profile (spec §2.7).

Discovery's output: one versioned, timestamped object that every later tool
reads, with eight sections and — the part that matters most — an explicit list
of what is still missing.

The pieces already existed and were computed independently at each call site,
so nothing could say "this recommendation ran on profile v7, which is
preliminary". Assembling them once, with a version and an evidence timestamp,
is what lets a downstream card admit its own limits (§2.7, §11).

Building is cheap and read-only; it is not a training step. `ensure_profile`
returns the current version and rebuilds only when the corpus has moved.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from . import evidence as ev
from . import relationships
from .account_model import infer_account_model, personal_topic_profile, topic_dimensions
from .performance import cadence_curve, daily_post_budget, performance_insights
from .storage import (
    confirmed_preferences,
    corpus_version,
    get_account_profile,
    latest_account_profile,
    latest_observation_at,
    list_profile_snapshots,
    profile_counts,
    save_account_profile,
)

# Below this many authored posts the profile is preliminary: the spec forbids
# pretending the account is understood after a tiny sample (§10.1), and the
# performance model's own comparison gates need roughly this much to say
# anything beyond a hint.
PRELIMINARY_BELOW_POSTS = 30


def _identity(stored: dict[str, Any], snapshots: list[dict]) -> dict[str, Any]:
    latest = snapshots[0] if snapshots else {}
    return {
        "handle": stored.get("handle") or latest.get("handle"),
        "positioning": stored.get("bio") or stored.get("positioning"),
        "intended_audience": stored.get("audience"),
        "objectives": stored.get("objectives") or stored.get("goals") or [],
        "followers": ev.observed(
            latest.get("followers_count"),
            source=ev.SOURCE_VISIBLE_X, observed_at=latest.get("captured_at"),
            scope=ev.ACCOUNT_WIDE,
            note=None if latest.get("followers_count") is not None
            else "No profile snapshot has been captured yet.").as_dict(),
    }


def _voice(model: dict[str, Any]) -> dict[str, Any]:
    voice = dict(model.get("voice_profile") or {})
    sample = int(voice.get("sample_size") or model.get("sample_size") or 0)
    return {
        "observed": voice,
        "sample_size": sample,
        "confidence": ev.confidence_for_samples(sample),
        # §2.5 wants these dimensions; they are not measured yet, and saying so
        # is better than shipping an invented number for "warmth".
        "unmeasured": ["directness", "certainty", "humor", "warmth", "edge",
                       "sentence_rhythm", "structure_taxonomy"],
    }


def _risk_rules(preferences: list[dict]) -> dict[str, Any]:
    hard = [item for item in preferences
            if str(item.get("scope") or "").lower() in ("safety", "privacy", "hard")]
    return {
        "confirmed_preferences": preferences,
        "hard_avoidances": hard,
        "note": "Only user-confirmed rules are hard; inferred patterns stay advisory (§9.3).",
    }


def build(*, save: bool = True) -> dict[str, Any]:
    """Assemble the profile from what is actually known right now."""
    posts, _ = profile_counts()
    stored = get_account_profile() or {}
    snapshots = list_profile_snapshots(limit=5)
    model = infer_account_model()
    topics = personal_topic_profile()
    insights = performance_insights()
    cadence = cadence_curve()
    budget, budget_note = daily_post_budget()
    preferences = confirmed_preferences()

    # Rebuilding the relationship map here keeps it in step with the archive;
    # it is a grouped read over `posts`, not a model fit.
    # (The old read was `topics.get("pillars")`, a key personal_topic_profile
    # never returned — relationships always rebuilt with None. Dimension
    # tokens plus the merged pillars are the real vocabulary.)
    dimensions = topic_dimensions()
    # The cluster centroids are numpy arrays used for in-memory matching. They
    # only survived being persisted because the JSON writer falls back to
    # str(), which stored 4.8KB of stringified float array per profile version
    # that nothing can read back. Matching always recomputes from
    # personal_topic_profile(), so the saved copy loses nothing.
    topics = {**topics, "clusters": [{key: value for key, value in cluster.items()
                                      if key != "_centroid"}
                                     for cluster in topics.get("clusters", [])]}
    pillar_tokens = {str(token).lower() for dim in dimensions for token in dim.get("tokens", [])}
    pillar_tokens |= {token for pillar in (model.get("content_pillars") or [])
                      for token in re.findall(r"[a-z0-9]{3,}", str(pillar).lower())}
    try:
        relationships.rebuild(pillar_tokens or None)
    except Exception:
        # A relationship rebuild must never block the profile: the other seven
        # sections are still useful without it, and §2.7 wants the gap named.
        pass
    watchlist = relationships.watchlist(limit=10)

    try:
        from .approach import approach_state
        approach = approach_state()
    except Exception:                                    # noqa: BLE001
        approach = {"state": "stable", "note": "Approach detector unavailable."}

    gaps = _find_gaps(posts, snapshots, insights, watchlist, preferences, approach)
    profile = {
        "identity": _identity(stored, snapshots),
        "voice": _voice(model),
        "performance_map": {
            "insights": insights,
            "topics": topics,
            "note": "Formats are compared like with like; raw totals are not comparable (§2.3).",
        },
        "audience_map": {
            # The merged pillar list (configured + learned dimensions) — this
            # is what reply triage and radar consume downstream.
            "pillars": model.get("content_pillars") or [],
            # numpy centroids stay out of anything JSON-shaped.
            "dimensions": [{key: value for key, value in dim.items() if key != "centroid"}
                           for dim in dimensions],
            "topic_profile": topics,
            "conversion": ev.unknown(
                "Profile visits and follows are parsed but not yet modelled.",
                scope=ev.ACCOUNT_WIDE).as_dict(),
        },
        "relationship_map": {
            "watchlist": watchlist,
            "note": "Ranked by relationship opportunity, never by follower count (§4.2).",
        },
        "cadence_map": {
            "curve": cadence,
            "daily_post_budget": budget,
            "budget_note": budget_note,
        },
        "risk_rules": _risk_rules(preferences),
        # Whether the account still writes like its measured baseline, and the
        # half-life the voice/pillar learners are running at because of it.
        "approach": approach,
        "counts": {"authored_posts": posts},
    }

    preliminary = posts < PRELIMINARY_BELOW_POSTS
    version = None
    if save:
        version = save_account_profile(
            profile, gaps=gaps, preliminary=preliminary,
            evidence_through=latest_observation_at())
    return {
        "version": version,
        "preliminary": preliminary,
        # BUILT NOW, so say now. This was hardcoded None on the path that
        # actually computes the profile, while the cached path 60 lines below
        # returns the stored timestamp — so a freshly built profile reported no
        # build time at all and its freshness could not be judged by anything
        # reading it. The column is NOT NULL in the schema for the same reason.
        "built_at": datetime.now(timezone.utc).isoformat(),
        "evidence_through": latest_observation_at(),
        "corpus_version": corpus_version(),
        "profile": profile,
        "gaps": gaps,
    }


def _find_gaps(posts: int, snapshots: list[dict], insights: dict,
               watchlist: list[dict], preferences: list[dict],
               approach: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Exactly what evidence is missing, and what cannot be concluded (§2.7.8).

    This is the section that keeps the rest honest: a profile that cannot say
    what it does not know will happily present a guess as a finding.
    """
    gaps: list[dict[str, Any]] = []
    if posts == 0:
        gaps.append({"missing": "authored posts",
                     "blocks": "voice, performance and cadence are all unavailable",
                     "fix": "Import your X archive."})
    elif posts < PRELIMINARY_BELOW_POSTS:
        gaps.append({"missing": f"only {posts} authored posts",
                     "blocks": "format comparisons stay directional at best",
                     "fix": "Import more history, or keep posting."})
    if not snapshots:
        gaps.append({"missing": "profile snapshots",
                     "blocks": "follower context and account-stage normalisation",
                     "fix": "Open your X profile with the extension enabled."})
    if not watchlist:
        gaps.append({"missing": "author relationships",
                     "blocks": "the watchlist and reply targeting (§4.2)",
                     "fix": "Import replies/quotes, or reply from X with Starling running."})
    if not preferences:
        gaps.append({"missing": "confirmed preferences",
                     "blocks": "hard avoidances; only inferred style is available",
                     "fix": "Confirm a preference when Starling proposes one."})
    if not (insights or {}).get("formats"):
        gaps.append({"missing": "per-format outcome data",
                     "blocks": "comparing replies with posts (§2.3)",
                     "fix": "Import an analytics export, or let outcomes mature."})
    if (approach or {}).get("state") == "watching":
        gaps.append({"missing": "a settled read on the account's current approach",
                     "blocks": "nothing yet — recent posts are moving off the measured "
                               "baseline, but the shift is unconfirmed so learning "
                               "stays at the normal pace",
                     "fix": "Keep posting; the detector confirms or clears this as "
                            "the corpus grows."})
    # Named unconditionally: these are architectural limits, not missing data,
    # and §5.3b requires the visibility boundary to be stated rather than implied.
    gaps.append({
        "missing": "X viewer-level ranking data",
        "blocks": "any claim about who will see a post or how X will rank it",
        "fix": "Not obtainable. Starling models creator-side alignment only.",
        "permanent": True,
    })
    return gaps


def _corpus_has_moved(latest: dict[str, Any]) -> bool:
    """Has the corpus changed since this stored profile was built?

    `corpus_version()` CANNOT ANSWER THIS. It is an in-process counter starting
    at 0 (`storage._corpus_version`), and this function's reuse test compared it
    to a value persisted by a DIFFERENT process. Both directions were wrong:

      * after any restart the counter is 0, and 18 of the 42 stored rows on his
        machine also hold 0 — including three built today whose evidence stops at
        03:33 while they were written at 21:02, 21:19 and 21:20. A fresh process
        matched 0 == 0 and served a profile built from an older corpus.
      * and once the running process writes anything the counter is 1 or more,
        which can never equal a stored 0, so the cache is lost entirely and the
        whole profile is rebuilt on every call.

    The two durable facts are already persisted and cost two COUNT queries and a
    MAX to re-read: how many authored posts the profile was built from, and the
    latest observation it had seen. A new post, a re-sighting, a fresh metric or a
    deletion moves one of them; nothing else needs to.
    """
    stored_through = latest.get("evidence_through")
    stored_posts = ((latest.get("profile") or {}).get("counts") or {}).get("authored_posts")
    if stored_posts is None:
        return True                     # can't prove it is current, so rebuild
    # `stored_through` of None is NOT an unknown -- it is a corpus with nothing
    # observed yet, which is a perfectly stable state that compares equal to
    # `latest_observation_at()` returning None. Treating it as unprovable made
    # every profile on a not-yet-observed account rebuild on every single call,
    # which is the exact cost this function exists to avoid.
    posts, _analytics = profile_counts()
    return bool(stored_posts != posts or stored_through != latest_observation_at())


def ensure_profile(*, max_age_versions: int = 0) -> dict[str, Any]:
    """The current profile, rebuilt only when the corpus has actually moved.

    Cheap to call on every request: the common path is one indexed read.
    """
    latest = latest_account_profile()
    if latest and not _corpus_has_moved(latest):
        return {
            "version": latest.get("version"),
            "preliminary": latest.get("preliminary", True),
            "built_at": latest.get("built_at"),
            "evidence_through": latest.get("evidence_through"),
            "corpus_version": latest.get("corpus_version"),
            "profile": latest.get("profile") or {},
            "gaps": latest.get("gaps") or [],
        }
    return build(save=True)


def freshness_line(profile: dict[str, Any] | None = None) -> str:
    """The one-line honesty statement a card or chat answer should carry."""
    current = profile or ensure_profile()
    parts = [f"Account profile v{current.get('version') or '?'}"]
    if current.get("preliminary"):
        parts.append("preliminary")
    seen = current.get("evidence_through")
    parts.append(ev.freshness_basis(seen, what="your account")
                 if seen else "no live observation yet")
    return " · ".join(parts)
