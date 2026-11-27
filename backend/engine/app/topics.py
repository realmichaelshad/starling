"""Choosing what a from-scratch post should be ABOUT (spec §4.4a, §6.2b).

The bug this exists to fix: `_creation_context` fed the account's content
pillars in as the SUBJECT. Pillars are derived from the archive — the things
this account already posts about — so every generated post came back about the
same two or three topics. Observed on the real account: four consecutive
"create a post" runs produced four Anthropic/Fable posts.

Pillars answer "is this account credible here?". They do not answer "what is
worth saying today". Those are different questions, and conflating them makes
the product a mirror of its own history.

    post_topic_value =
        momentum                    how much is actually happening
      x account_relevance           can this account credibly speak to it
      x remaining_angle_space       is there anything left to add
      x factual_confidence          do we know enough to say it
      - self_repetition             HOW OFTEN THIS ACCOUNT ALREADY SAYS THIS

The last term is the one that was missing entirely, and it is negative on
purpose: a topic the account covers constantly should get harder to justify,
not easier. That is what widens the range.

A topic may also be forward-looking — §1.5's falsifiable-prediction archetype
covers "what may happen and what would prove it wrong" — so a topic that is
rising rather than peaked is a legitimate, and often better, choice.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from . import evidence as ev
from . import radar_pipeline as rp

# How far back the account's own posts count as "recently covered".
SELF_WINDOW_DAYS = 14
# A topic the account has covered this many times in the window is saturated
# for THIS account, whatever the rest of X is doing.
SELF_SATURATION_COUNT = 3
# Below this, the topic is not worth a standalone post and we say so rather
# than reaching for the archive.
MIN_TOPIC_VALUE = 0.18
# An off-pillar topic is worth less, not worthless. Zero here would make
# relevance a gate and only ever surface subjects the account already covers.
RELEVANCE_FLOOR = 0.35
# ...but "adjacent" is not "unrelated". The floor keeps a neighbouring subject
# in play; it must not let a subject the account has no standing in win on
# freshness alone. Observed doing exactly that: an AI-commentary account was
# handed a cyclospora prediction market as its topic. The user asked not to
# repeat what they OFTEN post about — that is `self_repetition`'s job — not to
# post about anything at all.
MIN_PILLAR_OVERLAP = 1
# A bare trend name ("Smooth", "#Dbacks") is a pointer, not a subject. A topic
# needs enough substance to actually have a point made about it.
MIN_TOPIC_TOKENS = 4
# Marks grounding text that already carries a chosen subject, so downstream
# scoring can tell "write about THIS" apart from an ordinary post context.
ASSIGNED_PREFIX = "Write about:"

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9'\-\.]{2,}")
_STOP = {"the", "and", "for", "that", "this", "with", "you", "your", "are",
         "was", "have", "has", "but", "not", "they", "them", "from", "what",
         "when", "who", "why", "how", "all", "can", "will", "just", "about",
         "into", "than", "then", "there", "their", "been", "more", "most",
         "some", "such", "only", "its", "would", "could", "should", "new",
         "one", "two", "get", "got", "out", "now", "see", "way", "make"}


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall((text or "").lower()) if w not in _STOP}


@dataclass
class TopicChoice:
    """One candidate subject, with the evidence behind choosing it."""

    topic: str
    tokens: set[str] = field(default_factory=set)
    value: float = 0.0
    momentum: float = 0.0
    relevance: float = 0.0
    angle_space: float = 0.0
    self_repetition: float = 0.0
    eligible: bool = True
    freshness: str = ev.UNKNOWN_FRESHNESS
    observed_age_hours: float | None = None
    lifecycle: str = "unclear"
    forward_looking: bool = False
    sources: list[str] = field(default_factory=list)
    why: str = ""
    # The shared vocabulary of the account dimension this topic matched, when
    # relevance was judged per-dimension — visibility for "which beat won".
    matched_dimension: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"topic": self.topic, "value": round(self.value, 4),
                "momentum": round(self.momentum, 4),
                "relevance": round(self.relevance, 4),
                "angle_space": round(self.angle_space, 4),
                "self_repetition": round(self.self_repetition, 4),
                "eligible": self.eligible,
                "freshness": self.freshness,
                "observed_age_hours": self.observed_age_hours,
                "lifecycle": self.lifecycle,
                "forward_looking": self.forward_looking,
                "matched_dimension": self.matched_dimension,
                "sources": self.sources[:4], "why": self.why}


def self_repetition(topic_tokens: set[str], own_posts: list[dict[str, Any]],
                    *, now: datetime | None = None) -> tuple[float, int]:
    """How much this account already talks about this. 0 = never, 1 = constantly.

    The term that makes the range widen. Counted over the recent window only,
    because a topic covered a year ago is not the same as one covered twice
    this week.
    """
    if not topic_tokens:
        return 0.0, 0
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=SELF_WINDOW_DAYS)
    hits = 0
    considered = 0
    for post in own_posts:
        when = ev.parse_time(post.get("created_at") or post.get("first_observed_at"))
        if when is not None and when < cutoff:
            continue
        considered += 1
        overlap = topic_tokens & _tokens(post.get("text", ""))
        # Two shared meaningful words is a real topical match, not a coincidence.
        if len(overlap) >= 2:
            hits += 1
    if not considered:
        return 0.0, 0
    return min(1.0, hits / SELF_SATURATION_COUNT), hits


def score_topic(cluster: rp.Cluster, *, pillar_tokens: set[str],
                pillar_token_groups: list[set[str]] | None = None,
                own_posts: list[dict[str, Any]],
                previous: dict[str, Any] | None = None,
                now: datetime | None = None) -> TopicChoice:
    """Score one candidate subject. Pillars gate relevance; they are not the topic.

    `pillar_token_groups` carries the account's topic DIMENSIONS (one token
    set per beat): relevance and standing are then the best match against ANY
    single beat. A two-beat account's flat union both dilutes the overlap
    denominator and blurs the beats — per-dimension scoring restores full
    relevance for a topic square in either one. Absent groups, the flat
    `pillar_tokens` set behaves exactly as it always has.
    """
    read = rp.momentum(cluster, previous=previous, now=now)
    space = rp.remaining_angle_space(
        cluster, [str(p.get("text") or "") for p in own_posts[:20]])

    # Relevance is a soft factor with a FLOOR, never a gate. Without the floor
    # a topic sharing no token with the pillars scores exactly zero, so only
    # pillar-matching subjects could ever win — which is the original bug moved
    # from the prompt into the scorer. §1.5's adjacent-audience case exists
    # precisely to reach past the core, so an off-pillar topic stays viable and
    # simply has to be better on momentum, angle space and novelty to win.
    # §1: state how old the observation is. Trend snapshots arrive only when
    # the user opens /explore, so the newest one can be many hours old — and an
    # 18-hour-old X trend presented as "trending right now" is a false claim,
    # not a rounding error.
    newest = cluster.newest_at
    freshness = ev.freshness_of(newest, now=now)
    age_hours = (((now or datetime.now(timezone.utc)) - newest).total_seconds() / 3600.0
                 if newest else None)

    groups = [set(group) for group in (pillar_token_groups or []) if group]
    if not groups and pillar_tokens:
        groups = [set(pillar_tokens)]
    shared_best: set[str] = set()
    shared_max = 0
    if groups:
        overlap = 0.0
        for group in groups:
            group_shared = cluster.tokens & group
            shared_max = max(shared_max, len(group_shared))
            group_overlap = len(group_shared) / max(3, len(group))
            if group_overlap > overlap or (group_overlap == overlap
                                           and len(group_shared) > len(shared_best)):
                overlap, shared_best = group_overlap, group_shared
        relevance = RELEVANCE_FLOOR + (1.0 - RELEVANCE_FLOOR) * min(1.0, overlap * 4.0)
    else:
        relevance = 0.5    # no pillars known: neutral, not penalised

    repetition, hits = self_repetition(cluster.tokens, own_posts, now=now)

    # Factual confidence: a topic carried by several independent accounts is
    # safer to speak about than one person's assertion.
    diversity = read["terms"].get("source_diversity", 0.0)
    factual = min(1.0, 0.4 + diversity)

    # Geometric mean, not a raw product. Multiplication is the right SHAPE —
    # any term at zero must kill the topic, because a subject with no momentum
    # or no angle space left is not worth a post whatever else is true. But a
    # raw product of four sub-1.0 terms compounds so hard that nothing clears
    # any sane threshold, which would trade "always the same topic" for "never
    # any topic". The geometric mean keeps the zero-gate and keeps the scale
    # readable.
    gate = read["score"] * relevance * space["space"] * factual
    value = (gate ** 0.25) - (repetition * 0.5)

    lifecycle = read["lifecycle"]
    # "Early" is a claim about the clock, so it needs a fresh observation to
    # stand on. A single 22-hour-old snapshot reads as ORIGIN purely because one
    # observation cannot show spread — calling that "ahead of the conversation"
    # would be false confidence built on missing data, not on evidence.
    forward = ((lifecycle in (rp.ORIGIN, rp.EARLY_SPREAD) or read["label"] == "emerging")
               and freshness in (ev.FRESH, ev.AGING))

    if repetition >= 0.99:
        why = (f"You have already posted about this {hits} time(s) in "
               f"{SELF_WINDOW_DAYS} days - a fifth take adds nothing.")
    elif forward:
        why = ("Early enough that a prediction with a falsifiable condition "
               "would be genuinely ahead of the conversation.")
    elif freshness == ev.STALE:
        why = ("The freshest thing observed that this account has standing in, "
               "but it is old enough that its current state is unknown.")
    elif space["closed"]:
        why = space["reason"]
    elif relevance < 0.2:
        why = "Little connection to what this account is credible on."
    else:
        why = f"Moving, relevant, and there is angle space left ({lifecycle})."

    # Eligibility is separate from score. A subject the account has no standing
    # in is not a cheap post, it is the wrong post — and no amount of momentum
    # changes that. Kept out of the multiplication deliberately, so this reads
    # as the judgement it is rather than a term that could be outweighed.
    eligible = True
    if groups and shared_max < MIN_PILLAR_OVERLAP:
        eligible = False
        why = ("Moving, but this account has no established standing here - "
               "widening range means a different subject you can speak to, "
               "not an unrelated one.")
    elif len(cluster.tokens) < MIN_TOPIC_TOKENS:
        eligible = False
        why = ("Only a bare trend name was observed, with no substance to make "
               "a point about.")

    return TopicChoice(
        topic=_label(cluster), tokens=set(cluster.tokens),
        value=max(0.0, value) if eligible else 0.0, eligible=eligible,
        momentum=read["score"], relevance=relevance, angle_space=space["space"],
        self_repetition=repetition, lifecycle=lifecycle, forward_looking=forward,
        freshness=freshness,
        observed_age_hours=round(age_hours, 1) if age_hours is not None else None,
        matched_dimension=" ".join(sorted(shared_best)[:3]) or None,
        sources=sorted(cluster.authors)[:4], why=why)


def _label(cluster: rp.Cluster) -> str:
    """A readable subject line, not the cluster's sorted token bag.

    `cluster.key` is an alphabetised bag of tokens — "again breaking cyclospora
    false" — which is unusable both as a prompt and as something to show a
    person. The longest observed post is the best available description of what
    the event actually is.
    """
    texts = [str(p.get("text") or "").strip() for p in cluster.posts]
    texts = [t for t in texts if t]
    if not texts:
        return cluster.key
    best = max(texts, key=len)
    best = " ".join(best.split())
    return best if len(best) <= 160 else best[:157].rsplit(" ", 1)[0] + "..."


def choose_topic(*, candidates: list[dict[str, Any]], pillar_tokens: set[str],
                 pillar_token_groups: list[set[str]] | None = None,
                 own_posts: list[dict[str, Any]],
                 recently_offered: set[str] | None = None,
                 now: datetime | None = None) -> dict[str, Any]:
    """Pick what to write about, or admit there is nothing worth writing about.

    Returns a dict with `topic` (None when nothing clears the bar), the ranked
    alternatives, and the reason — so the caller can be honest instead of
    silently falling back to the archive.
    """
    if not candidates:
        return {
            "topic": None,
            # T3: no "open x.com/explore" and no "add an X API key": neither is
            # something the hosted product can act on.
            "reason": ("No current signal has been observed in the news feeds I "
                       "watch. Tell me a subject and I can write about it instead "
                       "of recycling your own history."),
            "alternatives": [],
            "repeat_offer": False,
            "fell_back_to_archive": False,
        }

    clusters = rp.group(candidates)
    scored = [score_topic(cluster, pillar_tokens=pillar_tokens,
                          pillar_token_groups=pillar_token_groups,
                          own_posts=own_posts, now=now) for cluster in clusters]
    scored.sort(key=lambda item: item.value, reverse=True)

    usable = [item for item in scored if item.eligible]
    # `self_repetition` only sees POSTED history, so without this, asking for
    # four drafts in a row returns the same subject four times — the monotony
    # complaint again, just at session scale. A topic already offered drops
    # behind any other eligible one, but stays available rather than being
    # removed: if it is the only thing worth writing about, saying so beats
    # inventing a worse subject.
    offered = recently_offered or set()
    if offered:
        usable.sort(key=lambda item: (item.topic in offered, -item.value))
    best = usable[0] if usable else None
    if best is None or best.value < MIN_TOPIC_VALUE:
        return {
            "topic": None,
            "reason": ("Nothing observed is both fresh and unlike what you have "
                       "already covered recently. Waiting beats repeating "
                       "yourself." if scored else "No usable candidates."),
            "alternatives": [item.as_dict() for item in scored[:5]],
            "repeat_offer": False,
            "fell_back_to_archive": False,
        }

    # The only eligible subject may be one already offered this session. Say so
    # and ask for a genuinely different angle, rather than re-issuing the same
    # brief and hoping the model varies on its own.
    repeat = best.topic in offered
    return {
        "topic": best.as_dict(),
        "repeat_offer": repeat,
        "reason": ("This is still the only current opportunity this account has "
                   "standing in, so the subject repeats - the angle must not."
                   if repeat else best.why),
        # Deliberately surfaced: the point is range, so the caller can offer
        # "another angle" from a genuinely different subject.
        "alternatives": [item.as_dict() for item in usable[1:5]],
        "fell_back_to_archive": False,
    }


def grounding_line(choice: dict[str, Any]) -> str:
    """The single line the Writer is grounded on.

    Never lists the account's pillars as subjects — that is the bug. Pillars
    stay out of the prompt entirely here; they already did their job by scoring
    relevance.
    """
    topic = choice.get("topic")
    if not topic:
        return ("Original standalone post. No current external signal is "
                "available, so write from a specific concrete observation the "
                "writer can stand behind - not a general take on their usual "
                "subjects.")
    parts = [f"{ASSIGNED_PREFIX} {topic['topic']}."]
    if choice.get("repeat_offer"):
        parts.append("You have already drafted on this subject in this session, "
                     "so take a DIFFERENT angle on it - a different point, a "
                     "different reader, or a different consequence. Do not "
                     "restate the earlier draft.")
    if topic.get("forward_looking"):
        parts.append("This is still early, so a prediction WITH a falsifiable "
                     "condition is the strongest shape.")
    age = topic.get("observed_age_hours")
    if topic.get("freshness") == ev.STALE and age is not None:
        parts.append(f"NOTE: this was last observed about {age:.0f} hours ago and "
                     "may have moved on - write it as a standing point, and do "
                     "not claim it is happening right now.")
    if topic.get("sources"):
        parts.append("Being discussed by: " + ", ".join(topic["sources"][:3]) + ".")
    parts.append(topic.get("why", ""))
    parts.append("Do not write a general take on the account's usual subjects.")
    return " ".join(part for part in parts if part)


def explicit_topic_grounding(topic: str) -> str:
    """Ground a draft on a topic the user directly selected in chat.

    This is deliberately not presented as a live trend or an account-derived
    pillar.  It gives the writer the user’s chosen subject while preserving the
    no-fabrication rule when there is no external evidence behind it.
    """
    subject = " ".join(str(topic or "").split()).strip(" .!?")[:160]
    return (f"{ASSIGNED_PREFIX} {subject}. The writer explicitly chose this subject in chat. "
            "Use only a concrete opinion or fact the writer supplied; do not invent a release, statistic, or trend.")


def is_assigned_subject(context_text: str) -> bool:
    """True when this grounding text names a subject Radar already chose."""
    return (context_text or "").lstrip().startswith(ASSIGNED_PREFIX)
