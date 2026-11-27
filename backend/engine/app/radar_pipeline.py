"""Radar as an opportunity filter, not a firehose (spec §4).

The eight stages of §4.1a, the momentum formula of §4.4, the four queues of
§4.4b, the candidate-compatibility pass of §4.1c and the reply-context packet
of §4.5a.

What existed before was a trend list grouped by lowercased NAME with a
two-term rising score. This adds the parts that decide whether a topic is an
opportunity for THIS account: whether anything actually changed, whether a
distinct angle remains, whether the user has already covered it, and whether
one contribution per conversation is still available.

Calibration honesty (§4.4): a raw momentum number is not a probability. Until
enough local outcomes exist to calibrate, this reports qualitative labels —
emerging / rising / unclear / fading — and explains the observable signals
rather than inventing a percentage.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from . import evidence as ev
from . import relationships
from .storage import replies_to_author

# §4.4 momentum weights. Starling priors, as everywhere else.
MOMENTUM_WEIGHTS = {
    "velocity": 0.35,
    "acceleration": 0.20,
    "source_diversity": 0.15,
    "new_author_entry": 0.10,
    "cross_community_spread": 0.10,
    "freshness": 0.10,
}

# §4.4b queues.
ACT_NOW = "act_now"
WATCH = "watch"
EVERGREEN = "evergreen"
IGNORE = "ignore"

# Lifecycle stages (§4.1b). Five, not the three the old trend table had.
ORIGIN = "origin"
EARLY_SPREAD = "early_spread"
ACCELERATION = "acceleration"
SATURATION = "saturation"
AFTERMATH = "aftermath"

# Above this share of observed posts making the same point, the angle space is
# effectively closed and a "me too" post is all that is left.
SATURATION_CLOSED = 0.7

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9'\-]{2,}")
_STOP = {"the", "and", "for", "that", "this", "with", "you", "your", "are",
         "was", "have", "has", "but", "not", "they", "them", "from", "what",
         "when", "who", "why", "how", "all", "can", "will", "just", "about",
         "into", "than", "then", "there", "their", "been", "more", "most",
         "some", "such", "only", "its", "would", "could", "should", "new"}


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall((text or "").lower()) if w not in _STOP}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# §4.1a stage 2 — group by EVENT, not by trend name
# --------------------------------------------------------------------------- #

@dataclass
class Cluster:
    """One developing event, assembled from several observed posts."""

    key: str
    posts: list[dict[str, Any]] = field(default_factory=list)
    tokens: set[str] = field(default_factory=set)

    @property
    def authors(self) -> set[str]:
        return {str(p.get("author_handle") or "").lower()
                for p in self.posts if p.get("author_handle")}

    @property
    def newest_at(self) -> datetime | None:
        stamps = [ev.parse_time(p.get("created_at")) for p in self.posts]
        stamps = [s for s in stamps if s]
        return max(stamps) if stamps else None

    @property
    def oldest_at(self) -> datetime | None:
        stamps = [ev.parse_time(p.get("created_at")) for p in self.posts]
        stamps = [s for s in stamps if s]
        return min(stamps) if stamps else None


def group(posts: list[dict[str, Any]], *, threshold: float = 0.22) -> list[Cluster]:
    """Merge repeated wording into one event (§4.1a).

    Grouping by trend NAME — what the old table did — cannot tell a genuine new
    development from recycled commentary, because both carry the same name.
    """
    clusters: list[Cluster] = []
    for post in posts:
        tokens = _tokens(post.get("text", ""))
        if not tokens:
            continue
        for cluster in clusters:
            if _jaccard(tokens, cluster.tokens) >= threshold:
                cluster.posts.append(post)
                cluster.tokens |= tokens
                break
        else:
            key = " ".join(sorted(tokens)[:4])
            clusters.append(Cluster(key=key, posts=[post], tokens=set(tokens)))
    return clusters


# --------------------------------------------------------------------------- #
# §4.4 momentum
# --------------------------------------------------------------------------- #

def momentum(cluster: Cluster, *, previous: dict[str, Any] | None = None,
             now: datetime | None = None) -> dict[str, Any]:
    """All six positive terms plus saturation decay, with an honest label.

    `previous` is the same cluster's reading from an earlier cycle; without it
    acceleration is UNKNOWN rather than zero, because one observation cannot
    show a change in rate.
    """
    now = now or _now()
    newest, oldest = cluster.newest_at, cluster.oldest_at
    span_minutes = ((newest - oldest).total_seconds() / 60.0
                    if newest and oldest and newest > oldest else None)

    # velocity: posts per minute across the observed window.
    velocity = (len(cluster.posts) / span_minutes) if span_minutes else None

    prior_velocity = (previous or {}).get("velocity")
    acceleration = (velocity - prior_velocity
                    if velocity is not None and prior_velocity is not None else None)

    authors = cluster.authors
    diversity = len(authors) / len(cluster.posts) if cluster.posts else 0.0

    prior_authors = set((previous or {}).get("authors") or [])
    new_authors = (len(authors - prior_authors) / len(authors)) if authors and prior_authors else None

    # Cross-community spread: how varied the vocabulary is across posts. One
    # tight group repeats itself; a spreading topic collects different framings.
    spread = 0.0
    if len(cluster.posts) > 1:
        sets = [_tokens(p.get("text", "")) for p in cluster.posts]
        sets = [s for s in sets if s]
        if len(sets) > 1:
            pairs = [(a, b) for i, a in enumerate(sets) for b in sets[i + 1:]]
            spread = 1.0 - (sum(_jaccard(a, b) for a, b in pairs) / len(pairs))

    freshness = 0.0
    if newest:
        age_h = max(0.0, (now - newest).total_seconds() / 3600.0)
        freshness = float(0.5 ** (age_h / 6.0))  # halves every six hours

    # Saturation: how much the posts repeat one another. High repetition means
    # the remaining angle space is small.
    saturation = 0.0
    if len(cluster.posts) > 1:
        sets = [_tokens(p.get("text", "")) for p in cluster.posts]
        sets = [s for s in sets if s]
        if len(sets) > 1:
            pairs = [(a, b) for i, a in enumerate(sets) for b in sets[i + 1:]]
            saturation = sum(_jaccard(a, b) for a, b in pairs) / len(pairs)

    terms = {
        "velocity": _norm(velocity, 0.5),
        "acceleration": _norm(acceleration, 0.25) if acceleration is not None else 0.0,
        "source_diversity": diversity,
        "new_author_entry": new_authors if new_authors is not None else 0.0,
        "cross_community_spread": spread,
        "freshness": freshness,
    }
    unknown = [name for name, value in
               (("velocity", velocity), ("acceleration", acceleration),
                ("new_author_entry", new_authors)) if value is None]

    # §1: unknown is not zero. Acceleration and new-author entry are both
    # UNKNOWN on a first observation (they need a prior reading to exist), and
    # summing them in as 0.0 silently spent 30% of the weight on absence. A
    # topic carried by four distinct authors minutes ago scored 0.05 that way.
    # Renormalise over the weight actually available, so a first observation is
    # scored on what IS known and stays comparable to a second one.
    available = sum(w for k, w in MOMENTUM_WEIGHTS.items() if k not in unknown)
    known_total = sum(MOMENTUM_WEIGHTS[k] * v for k, v in terms.items()
                      if k not in unknown)
    score = (known_total / available if available else 0.0) - saturation * 0.25

    return {
        "score": round(max(0.0, min(1.0, score)), 4),
        "terms": {k: round(v, 4) for k, v in terms.items()},
        "saturation": round(saturation, 4),
        "velocity": velocity,
        "authors": sorted(authors),
        "unknown_terms": unknown,
        "label": _label(score, acceleration, freshness, saturation),
        "lifecycle": lifecycle(cluster, saturation, acceleration, freshness),
        # §4.4: a raw score is not a probability until it is calibrated against
        # local outcomes, so no percentage is offered.
        "calibrated": False,
        "note": ("Qualitative until enough of your own outcomes exist to calibrate; "
                 "a raw score is not a probability."),
    }


def _norm(value: float | None, reference: float) -> float:
    """Log-scale a rate into 0..1 without a hard cliff."""
    if value is None or value <= 0:
        return 0.0
    return min(1.0, math.log1p(value) / math.log1p(reference * 4))


def _label(score: float, acceleration: float | None, freshness: float,
           saturation: float) -> str:
    if saturation >= SATURATION_CLOSED:
        return "fading"
    if acceleration is None:
        return "unclear"        # one observation cannot show direction
    if acceleration > 0 and score >= 0.45:
        return "rising"
    if acceleration > 0:
        return "emerging"
    if freshness < 0.25:
        return "fading"
    return "unclear"


def lifecycle(cluster: Cluster, saturation: float, acceleration: float | None,
              freshness: float) -> str:
    """The five stages of §4.1b, not the three the old trend table had."""
    authors = len(cluster.authors)
    if saturation >= SATURATION_CLOSED:
        return SATURATION if freshness >= 0.25 else AFTERMATH
    if authors <= 1:
        return ORIGIN
    if acceleration is not None and acceleration > 0 and authors >= 3:
        return ACCELERATION
    if authors >= 2:
        return EARLY_SPREAD
    return ORIGIN


# --------------------------------------------------------------------------- #
# §4.1a stage 6 — what is left to say
# --------------------------------------------------------------------------- #

def remaining_angle_space(cluster: Cluster, recent_user_texts: list[str]) -> dict[str, Any]:
    """How much room is left, given the crowd AND the user's own recent output."""
    sets = [_tokens(p.get("text", "")) for p in cluster.posts]
    sets = [s for s in sets if s]
    crowd_repetition = 0.0
    if len(sets) > 1:
        pairs = [(a, b) for i, a in enumerate(sets) for b in sets[i + 1:]]
        crowd_repetition = sum(_jaccard(a, b) for a, b in pairs) / len(pairs)

    user_overlap = 0.0
    for text in recent_user_texts:
        user_overlap = max(user_overlap, _jaccard(_tokens(text), cluster.tokens))

    space = max(0.0, 1.0 - max(crowd_repetition, user_overlap))
    return {
        "space": round(space, 4),
        "crowd_repetition": round(crowd_repetition, 4),
        "user_overlap": round(user_overlap, 4),
        "closed": space < (1.0 - SATURATION_CLOSED),
        "reason": ("You have already covered this angle." if user_overlap > crowd_repetition
                   else "The same take is already widespread." if crowd_repetition > 0.5
                   else "There is still room for a distinct point."),
    }


# --------------------------------------------------------------------------- #
# §4.1c X candidate-compatibility pass
# --------------------------------------------------------------------------- #

def candidate_compatible(post: dict[str, Any], *, recent_user_texts: list[str],
                         now: datetime | None = None) -> dict[str, Any]:
    """Creator-side eligibility BEFORE anything is ranked or drafted.

    Mirrors the discipline of the public pre-ranking filters: reject stale,
    duplicate, already-covered or conflicting candidates early rather than
    generating a post that was never a viable candidate.
    """
    now = now or _now()
    failures: list[str] = []

    created = ev.parse_time(post.get("created_at"))
    if created is None:
        freshness_state = "unknown"
    else:
        age_h = (now - created).total_seconds() / 3600.0
        freshness_state = ("new" if age_h <= 1 else "active" if age_h <= 6
                           else "fading" if age_h <= 48 else "evergreen")
        if age_h > 48:
            failures.append("the conversation is old enough that it has moved on")

    if post.get("is_retweet"):
        failures.append("a repost is not a conversation to join")

    text = post.get("text") or ""
    if len(text.strip()) < 25:
        failures.append("too little substance to respond to")

    tokens = _tokens(text)
    for prior in recent_user_texts:
        if _jaccard(_tokens(prior), tokens) >= 0.55:
            failures.append("you have already made this point recently")
            break

    handle = str(post.get("author_handle") or "").lstrip("@").lower()
    conversation_used = False
    if handle:
        root = post.get("in_reply_to_id") or post.get("post_id")
        for prior in replies_to_author(handle):
            if str(prior.get("parent_post_id") or "") == str(root):
                conversation_used = True
                failures.append("you have already contributed to this conversation")
                break

    return {
        "eligible": not failures,
        "failures": failures,
        "freshness_state": freshness_state,
        "conversation_unique": not conversation_used,
        "surface": "reply" if post.get("in_reply_to_id") else "post",
        "note": "Creator-side eligibility only; X's own filters are not visible.",
    }


# --------------------------------------------------------------------------- #
# §4.5a reply-context packet
# --------------------------------------------------------------------------- #

def reply_context_packet(post: dict[str, Any], *, thread: list[dict[str, Any]] | None = None,
                         now: datetime | None = None) -> dict[str, Any]:
    """Enough thread evidence to judge whether one contribution belongs.

    Only permitted, currently visible information. `reply_visibility` is
    explicitly unknown: X ranks per viewer and the released reply service's
    formula is not public (§4.5a).
    """
    thread = thread or []
    handle = str(post.get("author_handle") or "").lstrip("@").lower()
    prior = replies_to_author(handle) if handle else []

    claims = [str(item.get("text") or "")[:140] for item in thread[:6]]
    repeated = 0
    if len(thread) > 1:
        sets = [_tokens(item.get("text", "")) for item in thread]
        sets = [s for s in sets if s]
        pairs = [(a, b) for i, a in enumerate(sets) for b in sets[i + 1:]]
        repeated = sum(1 for a, b in pairs if _jaccard(a, b) > 0.5)

    return {
        "root_id": post.get("in_reply_to_id") or post.get("post_id"),
        "parent_id": post.get("in_reply_to_id"),
        "selected_id": post.get("post_id"),
        "observed_at": post.get("observed_at") or (now or _now()).isoformat(),
        "visible_thread": claims,
        "repeated_takes": repeated,
        "unresolved_questions": [c for c in claims if c.rstrip().endswith("?")],
        "replies_restricted": bool(post.get("reply_restricted")),
        "author_handle": handle,
        "your_prior_replies_to_author": len(prior),
        "your_prior_replies_to_root": sum(
            1 for item in prior
            if str(item.get("parent_post_id") or "") == str(post.get("in_reply_to_id") or "")),
        # The spec requires this field by name, and requires it to stay unknown.
        "reply_visibility": "unknown",
        "note": ("Starling cannot infer private reply-ranking signals, follower "
                 "graphs, or which specific people will see a reply."),
    }


# --------------------------------------------------------------------------- #
# §4.4b the four queues
# --------------------------------------------------------------------------- #

@dataclass
class Opportunity:
    cluster_key: str
    queue: str
    score: float
    momentum: dict[str, Any]
    angle_space: dict[str, Any]
    reason: str
    recheck: str | None = None
    posts: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"cluster_key": self.cluster_key, "queue": self.queue,
                "score": self.score, "momentum": self.momentum,
                "angle_space": self.angle_space, "reason": self.reason,
                "recheck": self.recheck, "post_count": len(self.posts)}


def triage(clusters: list[Cluster], *, recent_user_texts: list[str],
           pillar_tokens: set[str] | None = None,
           previous: dict[str, dict] | None = None,
           now: datetime | None = None) -> list[Opportunity]:
    """Sort clusters into act-now / watch / evergreen / ignore (§4.4b).

    A big trend with no remaining angle space is an Ignore or a Watch, never an
    Act now — which is the whole difference between opportunity and chasing.
    """
    previous = previous or {}
    out: list[Opportunity] = []
    for cluster in clusters:
        read = momentum(cluster, previous=previous.get(cluster.key), now=now)
        space = remaining_angle_space(cluster, recent_user_texts)
        fit = _jaccard(cluster.tokens, pillar_tokens) if pillar_tokens else None

        # radar opportunity = signal x fit x angle space x confidence x time
        score = read["score"] * space["space"]
        if fit is not None:
            score *= max(0.15, fit)

        if fit is not None and fit < 0.05:
            queue, reason = IGNORE, "No credible connection to your pillars."
            recheck = None
        elif space["closed"]:
            queue, reason = (WATCH, space["reason"])
            recheck = "Re-check if a new fact opens a different angle."
        elif read["label"] == "fading" and read["lifecycle"] == AFTERMATH:
            queue, reason = EVERGREEN, "The headline faded but the consequence is durable."
            recheck = None
        elif read["label"] in ("rising", "emerging") and not space["closed"]:
            queue, reason = ACT_NOW, "Moving, relevant, and a distinct angle remains."
            recheck = None
        elif read["unknown_terms"]:
            queue = WATCH
            # The internal term names ("velocity, acceleration, new_author_entry")
            # used to be printed straight into chat — the leak class the user
            # called out: "chat shouldn't tell you what the calculation did".
            # The count is honest; the machinery stays inside.
            reason = ("Seen once so far, so the direction is not confirmed."
                      if len(read["unknown_terms"]) < 3 else
                      "Too new to read — one observation is not a trend.")
            recheck = "Re-check after a second observation confirms direction."
        else:
            queue, reason = WATCH, "Relevant but not clearly moving."
            recheck = "Re-check on the next radar cycle."

        out.append(Opportunity(
            cluster_key=cluster.key, queue=queue, score=round(score, 4),
            momentum=read, angle_space=space, reason=reason, recheck=recheck,
            posts=cluster.posts))

    out.sort(key=lambda item: item.score, reverse=True)
    return out


def queues(opportunities: list[Opportunity]) -> dict[str, list[dict[str, Any]]]:
    """The four queues as the UI/chat would present them."""
    grouped: dict[str, list[dict[str, Any]]] = {
        ACT_NOW: [], WATCH: [], EVERGREEN: [], IGNORE: []}
    for item in opportunities:
        grouped[item.queue].append(item.as_dict())
    return grouped
