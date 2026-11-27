"""Analyzer: make one post or topic legible before anything is decided (§5).

Previously Analyzer and Decider were the same function (`opportunities.assess_
opportunity`), which meant the evidence and the judgement were produced in one
pass and could not be inspected separately. The spec requires a staged handoff
(§6.9): Analyzer establishes what is true and what is unknown, Decider chooses.

Analyzer never writes copy and never picks the action. It answers:

    is it moving?      real velocity/acceleration from the snapshot series
    is it relevant?    subject / audience / relationship / voice fit
    who would act?     the audience-action forecast
    does it align?     creator-side X alignment, with the visibility boundary
    is it a reply?     the reply-surface label
    what is missing?   angle space — the one useful point not already made
    is it safe?        blockers, separated from penalties
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from . import algorithm_model as am
from . import evidence as ev
from . import momentum as mom
from . import relationships
from .safety import assess_context
from .schemas import PostContext
from .storage import replies_to_author

# §5.2 freshness brackets for a reply opportunity, in minutes.
REPLY_STRONG_MINUTES = 30
REPLY_VIABLE_MINUTES = 90
REPLY_NEEDS_MOVEMENT_MINUTES = 240

# Overlap above which the user's own recent wording counts as a repeat (§5.4).
REPEAT_SIMILARITY = 0.55

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9'\-]{2,}")
_STOP = {"the", "and", "for", "that", "this", "with", "you", "your", "are",
         "was", "have", "has", "but", "not", "they", "them", "from", "what",
         "when", "who", "why", "how", "all", "can", "will", "just", "about",
         "into", "than", "then", "there", "their", "been", "more", "most",
         "some", "such", "only", "its", "it's", "would", "could", "should"}

# Angle families the spec names (§5.4) and the anti-patterns it rejects.
ANGLE_KINDS: tuple[str, ...] = (
    "practical_implication", "clear_distinction", "overlooked_tradeoff",
    "first_hand_observation", "concise_counterexample", "verification_question",
    "audience_consequence", "explicit_prediction", "stronger_standalone_thesis",
)
ANTI_PATTERNS: tuple[str, ...] = (
    "generic_agreement", "generic_question", "restatement", "recent_repeat",
    "excessive_hostility", "unsupported_extension", "promotion_without_payoff",
)


# Image selection needs a more exact representation than a bag of words.  A
# title such as "Claude the Dragon" shares a word with Anthropic's product,
# but it is not about the same thing.  These rules record the *meaningful
# context* that must travel with an ambiguous entity before Image Radar is even
# allowed to search for it.  The catalog is intentionally small and
# conservative: an unfamiliar proper noun without enough context gets no image
# rather than an invented association.
_IMAGE_ENTITY_RULES: dict[str, dict[str, Any]] = {
    "claude": {
        "canonical": "Claude by Anthropic",
        "query": "Claude Anthropic AI model",
        "aliases": ("claude", "claude ai", "claude code", "claude opus",
                    "claude sonnet", "claude haiku"),
        "anchors": ("anthropic", "ai", "model", "assistant", "llm", "api",
                    "chatbot", "opus", "sonnet", "haiku", "claude code",
                    "benchmark", "prompt", "coding"),
        "conflicts": ("dragon", "sculpture", "statue", "painting", "artist",
                      "painter", "saint", "musician", "singer", "horse",
                      "portrait", "person"),
        "signature": r"\bclaude\s*(?:[0-9]|opus|sonnet|haiku|code|ai)\b",
    },
    "chatgpt": {
        "canonical": "ChatGPT by OpenAI",
        "query": "ChatGPT OpenAI AI assistant",
        "aliases": ("chatgpt", "gpt-4", "gpt-5", "openai"),
        "anchors": ("openai", "ai", "model", "assistant", "llm", "api",
                    "chatbot", "gpt", "prompt", "coding"),
        "conflicts": ("painting", "artist", "portrait", "person", "band"),
        "signature": r"\b(?:chatgpt|gpt[- ]?[0-9])\b",
    },
    "gemini": {
        "canonical": "Gemini by Google",
        "query": "Google Gemini AI model",
        "aliases": ("gemini", "google gemini"),
        "anchors": ("google", "ai", "model", "assistant", "llm", "api",
                    "chatbot", "benchmark", "prompt", "android"),
        "conflicts": ("zodiac", "constellation", "astrology", "twins", "star sign",
                      "painting", "person"),
        "signature": r"\bgemini\s*(?:[0-9]|ai|advanced|flash|pro)\b",
    },
    "llama": {
        "canonical": "Llama by Meta AI",
        "query": "Meta Llama AI model",
        "aliases": ("llama", "meta llama"),
        "anchors": ("meta", "ai", "model", "llm", "api", "inference",
                    "benchmark", "open weights"),
        "conflicts": ("animal", "alpaca", "zoo", "farm", "wildlife", "peru",
                      "mountain", "portrait"),
        "signature": r"\bllama\s*(?:[0-9]|ai)\b",
    },
    "grok": {
        "canonical": "Grok by xAI",
        "query": "xAI Grok AI model",
        "aliases": ("grok", "xai grok"),
        "anchors": ("xai", "ai", "model", "assistant", "llm", "api", "x.com",
                    "chatbot", "benchmark"),
        "conflicts": ("comic", "cartoon", "character", "band", "portrait"),
        "signature": r"\bgrok\s*(?:[0-9]|ai|beta)\b",
    },
}

_VISUAL_FILLER = {
    "about", "after", "again", "also", "because", "been", "being", "best",
    "better", "could", "every", "first", "from", "getting", "going", "good",
    "great", "here", "into", "just", "last", "like", "made", "make", "many",
    "more", "most", "much", "need", "new", "next", "now", "over", "really",
    "right", "still", "take", "than", "that", "their", "there", "these", "they",
    "think", "those", "today", "very", "want", "well", "were", "what", "which",
    "while", "with", "would", "year",
}

_COMPARISON_CUES = {
    "benchmark", "compare", "comparison", "compared", "versus", "faster", "slower",
    "latency", "leaderboard", "difference", "gap", "outperform", "performance",
    "accuracy", "chart", "graph", "table",
}
_EVIDENCE_CUES = {
    "data", "results", "result", "report", "study", "numbers", "metrics", "metric",
    "revenue", "users", "downloads", "screenshot", "demo", "demonstration", "before",
    "after", "measured", "measuring", "tested", "testing",
}
_ANNOUNCEMENT_CUES = {
    "announce", "announced", "announcement", "launch", "launched", "release", "released",
    "ship", "shipped", "update", "updated", "introduce", "introduced", "available",
    "rollout", "rolled", "drops", "dropped",
}


def _contains_term(text: str, term: str) -> bool:
    """Match a semantic anchor as words, never as a substring."""
    return bool(re.search(r"(?<![a-z0-9])" + re.escape(term.lower()) + r"(?![a-z0-9])",
                          text.lower()))


def _entity_matches(text: str) -> list[dict[str, Any]]:
    """Return only disambiguated product entities present in ``text``.

    A bare ambiguous name is intentionally omitted.  This is the principal
    guard against a search result matching a name rather than the thing the
    post means.
    """
    lowered = (text or "").lower()
    matches: list[dict[str, Any]] = []
    for key, rule in _IMAGE_ENTITY_RULES.items():
        has_alias = any(_contains_term(lowered, alias) for alias in rule["aliases"])
        if not has_alias:
            continue
        anchor_count = sum(_contains_term(lowered, anchor) for anchor in rule["anchors"])
        has_signature = bool(re.search(rule["signature"], lowered))
        # A known model signature ("Claude Opus", "Gemini Flash") carries the
        # needed context on its own.  Otherwise demand another domain anchor.
        if not (has_signature or anchor_count):
            continue
        matches.append({
            "key": key,
            "canonical": rule["canonical"],
            "query": rule["query"],
            "aliases": list(rule["aliases"]),
            "anchors": list(rule["anchors"]),
            "conflicts": list(rule["conflicts"]),
            "signature": rule["signature"],
            "anchor_count": anchor_count,
        })
    return matches


def _meaning_similarity(left: str, right: str) -> float | None:
    """A local embedding signal, used only after explicit entity gates.

    It improves ranking among already-valid candidates; it never turns a loose
    name match into a valid image.  The function quietly degrades when the
    bundled runtime is unavailable.
    """
    try:
        from .embeddings import LocalOnnxEmbedder

        embedder = LocalOnnxEmbedder()
        if not embedder.available:
            return None
        vectors = embedder.encode([left[:2000], right[:2000]])
        cosine = float(vectors[0] @ vectors[1])
        return max(0.0, min(1.0, (cosine + 1.0) / 2.0))
    except Exception:
        return None


def analyze_visual_brief(draft: str, *, action: str = "post",
                         visual_habit: dict[str, Any] | None = None) -> dict[str, Any]:
    """Have Analyzer decide the *visual job* before Image Radar searches.

    This is deliberately not an image-keyword generator.  It identifies the
    central subject, any disambiguated entities, and the kind of proof or
    explanation an image would need to supply.  If there is no real visual job,
    it returns ``none`` and no scraper is started.
    """
    text = " ".join((draft or "").split())
    lowered = text.lower()
    tokens = _tokens(text)
    entities = _entity_matches(text)
    entity_words = {word for entity in entities for alias in entity["aliases"]
                    for word in _tokens(alias)}
    concepts = sorted(token for token in tokens
                      if token not in _VISUAL_FILLER and token not in entity_words)
    comparison = bool(tokens & _COMPARISON_CUES) or bool(re.search(r"\bvs\.?\b", lowered))
    evidence = bool(tokens & _EVIDENCE_CUES) or bool(re.search(r"\b\d+(?:\.\d+)?\s*(?:%|x|ms|m|b)\b", lowered))
    announcement = bool(tokens & _ANNOUNCEMENT_CUES)

    habit = dict(visual_habit or {})
    image_forward = bool(habit.get("image_forward"))
    concrete_subject = bool(entities) or len(concepts) >= 3

    if comparison:
        state, visual_job, gain = "recommended", "make a concrete comparison legible", 0.86
    elif evidence:
        state, visual_job, gain = "recommended", "show the evidence behind the claim", 0.80
    elif entities and announcement:
        state, visual_job, gain = "optional", "identify the exact announced product", 0.62
    elif entities and any(entity["anchor_count"] >= 2 for entity in entities):
        state, visual_job, gain = "optional", "give the named product intentional visual context", 0.54
    elif image_forward and concrete_subject:
        # The user has a demonstrated image habit. A visual can therefore be
        # offered more often, but only for a concrete subject that candidate
        # analysis can verify; this is not permission for stock filler.
        state, visual_job, gain = (
            "optional", "make the exact subject recognizable in the user's established visual rhythm",
            max(0.46, min(0.64, 0.38 + float(habit.get("propensity") or 0.0))),
        )
    else:
        state, visual_job, gain = "none", "text carries the point more clearly", 0.0

    # A reply needs a visual to do actual evidence/comparison work.  Product
    # identity alone is decorative in a reply and therefore never starts a
    # search.
    if action == "reply" and state == "optional":
        state, visual_job, gain = "none", "a reply does not need decorative media", 0.0

    focus = concepts[:8]
    queries: list[str] = []
    for entity in entities:
        queries.append(entity["query"])
    if not entities and focus and state != "none":
        # The full subject, not a broad category such as "technology", is the
        # only permissible non-entity query.  A miss means text-only.
        queries.append(" ".join(focus[:5]))
    if comparison and queries:
        queries = [f"{query} benchmark comparison" for query in queries]
    elif evidence and queries:
        queries = [f"{query} data chart" for query in queries]

    return {
        "state": state,
        "visual_job": visual_job,
        "evidence_gain": gain,
        "text_is_sufficient": state == "none",
        "draft": text,
        "entities": entities,
        "focus_concepts": focus,
        "queries": list(dict.fromkeys(queries)),
        "comparison": comparison,
        "evidence": evidence,
        "visual_habit": {
            "sample": int(habit.get("sample") or 0),
            "propensity": float(habit.get("propensity") or 0.0),
            "image_forward": image_forward,
            "reason": str(habit.get("reason") or "No observed posting pattern yet."),
        },
        "reasons": ([visual_job] if state != "none" else
                    ["No exact proof, comparison, demonstration, or clear product context requires an image."]),
    }


def score_visual_candidate(brief: dict[str, Any], candidate_description: str) -> dict[str, Any]:
    """Analyzer's final semantic gate for an image candidate.

    The formula deliberately gives entity identity and visual-job alignment
    precedence over fuzzy similarity.  Thus a semantically adjacent but wrong
    picture cannot pass, and a beautiful generic image receives no score.
    """
    description = " ".join((candidate_description or "").split())
    candidate = description.lower()
    if brief.get("state") == "none":
        return {"pass": False, "score": 0.0, "reason": "Analyzer found no visual job."}
    if not candidate:
        return {"pass": False, "score": 0.0, "reason": "The source does not describe what the image depicts."}

    entity_scores: list[float] = []
    for entity in brief.get("entities") or []:
        if not any(_contains_term(candidate, alias) for alias in entity.get("aliases") or []):
            return {"pass": False, "score": 0.0,
                    "reason": f"It does not identify {entity.get('canonical')}.",
                    "entity_match": 0.0}
        conflicts = [term for term in entity.get("conflicts") or [] if _contains_term(candidate, term)]
        if conflicts:
            return {"pass": False, "score": 0.0,
                    "reason": "Its description points to a different meaning of the name: " + ", ".join(conflicts) + ".",
                    "entity_match": 0.0, "conflicts": conflicts}
        anchors = sum(_contains_term(candidate, anchor) for anchor in entity.get("anchors") or [])
        signature = bool(re.search(str(entity.get("signature") or ""), candidate))
        # An exact product/model signature is strongest.  A name plus one real
        # domain anchor is still acceptable; the name alone categorically is not.
        entity_scores.append(1.0 if signature else 0.88 if anchors >= 2 else
                             0.80 if anchors == 1 else 0.20)

    candidate_tokens = _tokens(description)
    focus = set(brief.get("focus_concepts") or [])
    concept_coverage = (len(candidate_tokens & focus) / max(1, min(3, len(focus)))
                        if focus else 0.5)
    concept_coverage = min(1.0, concept_coverage)
    embedding_similarity = _meaning_similarity(str(brief.get("draft") or ""), description)
    semantic_similarity = (embedding_similarity if embedding_similarity is not None
                           else concept_coverage)

    job_hints = {
        "comparison": ("benchmark", "comparison", "compare", "chart", "graph", "data",
                       "performance", "latency", "leaderboard"),
        "evidence": ("chart", "graph", "data", "result", "report", "study", "screenshot",
                     "demo", "before", "after"),
    }
    if brief.get("comparison"):
        job_alignment = 1.0 if any(_contains_term(candidate, hint) for hint in job_hints["comparison"]) else 0.35
    elif brief.get("evidence"):
        job_alignment = 1.0 if any(_contains_term(candidate, hint) for hint in job_hints["evidence"]) else 0.45
    else:
        job_alignment = 1.0 if entity_scores else min(1.0, 0.45 + concept_coverage)

    entity_match = min(entity_scores) if entity_scores else min(1.0, 0.35 + concept_coverage)
    # The advanced fit formula is intentionally inspectable.  Entity identity
    # dominates when present; semantic similarity only helps rank legitimate
    # candidates and can never compensate for an ambiguous or missing entity.
    score = (0.50 * entity_match + 0.25 * job_alignment +
             0.15 * concept_coverage + 0.10 * semantic_similarity)
    minimum = 0.80 if entity_scores else 0.76
    if entity_scores and entity_match < 0.80:
        return {"pass": False, "score": round(score, 4), "entity_match": round(entity_match, 4),
                "concept_coverage": round(concept_coverage, 4),
                "job_alignment": round(job_alignment, 4),
                "reason": "The name is present, but the source does not establish the intended product context."}
    if job_alignment < 0.70 and (brief.get("comparison") or brief.get("evidence")):
        return {"pass": False, "score": round(score, 4), "entity_match": round(entity_match, 4),
                "job_alignment": round(job_alignment, 4),
                "reason": "It does not show the comparison or evidence this post needs."}
    if score < minimum:
        return {"pass": False, "score": round(score, 4), "entity_match": round(entity_match, 4),
                "concept_coverage": round(concept_coverage, 4),
                "job_alignment": round(job_alignment, 4),
                "reason": "Its described subject is not specific enough for this post."}
    return {
        "pass": True, "score": round(score, 4),
        "entity_match": round(entity_match, 4),
        "concept_coverage": round(concept_coverage, 4),
        "semantic_similarity": round(semantic_similarity, 4),
        "job_alignment": round(job_alignment, 4),
        "reason": "Analyzer confirmed the exact subject and visual job.",
    }


def _tokens(text: str) -> set[str]:
    return {word for word in _WORD_RE.findall((text or "").lower())
            if word not in _STOP}


def _overlap(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


@dataclass
class Analysis:
    """The decision-ready evidence card (§5.6). Contains no chosen action."""

    summary: str = ""
    topic_tokens: list[str] = field(default_factory=list)
    momentum: dict[str, Any] = field(default_factory=dict)
    age_minutes: float | None = None
    fit: dict[str, Any] = field(default_factory=dict)
    audience_forecast: dict[str, Any] = field(default_factory=dict)
    alignment: dict[str, Any] = field(default_factory=dict)
    reply_surface: str = "n/a"
    reply_surface_reason: str = ""
    angles: list[dict[str, Any]] = field(default_factory=list)
    anti_patterns: list[str] = field(default_factory=list)
    readiness: dict[str, bool] = field(default_factory=dict)
    blockers: list[str] = field(default_factory=list)
    penalties: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    confidence: str = ev.UNKNOWN_CONFIDENCE
    reach: str | None = None
    source: dict[str, Any] = field(default_factory=dict)
    visibility_boundary: str = am.VISIBILITY_BOUNDARY
    # "ok" | "ad" | "bait". Not a safety verdict — a judgement about whether the
    # TARGET can support any move at all. A dead thread is still "ok": the room
    # is empty but the idea may be worth a post of his own. An ad and a bait
    # post are not, and the difference matters because the decider treats POST
    # as an alternative to replying.
    target_quality: str = "ok"

    def as_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary, "topic_tokens": self.topic_tokens,
            "momentum": self.momentum, "age_minutes": self.age_minutes,
            "fit": self.fit, "audience_forecast": self.audience_forecast,
            "alignment": self.alignment, "reply_surface": self.reply_surface,
            "reply_surface_reason": self.reply_surface_reason,
            "angles": self.angles, "anti_patterns": self.anti_patterns,
            "readiness": self.readiness, "blockers": self.blockers,
            "penalties": self.penalties, "unknowns": self.unknowns,
            "confidence": self.confidence, "reach": self.reach,
            "source": self.source,
            "visibility_boundary": self.visibility_boundary,
            "target_quality": self.target_quality,
        }


def _age_minutes(context: PostContext) -> float | None:
    created = ev.parse_time(getattr(context, "post_created_at", None))
    if created is None:
        return None
    return max(0.0, (ev._now() - created).total_seconds() / 60.0)


def _fit(context: PostContext, profile: dict[str, Any]) -> dict[str, Any]:
    """The four fit questions (§5.3), each with the bridge spelled out.

    "AI is relevant to an AI account" is not an answer; the spec wants the
    actual bridge named, so each dimension reports WHY rather than a bare score.
    """
    account = (profile or {}).get("profile") or {}
    pillars = [str(p).lower() for p in
               ((account.get("audience_map") or {}).get("pillars") or [])]
    # DO NOT fall back to `infer_account_model().content_pillars` here. It looks
    # like the obvious fix for the empty `audience_map.pillars` (Radar does
    # exactly that at main.py:690) and it was MEASURED on 2026-07-23 to be far
    # worse: those pillars are frequency-inferred junk ("fable, opus, gpt-5,
    # than"), so a genuine post that shares none of them scores a MEASURED zero
    # instead of honestly unknown. A reply that scored 0.5653 collapsed to
    # 0.0000 and every draft went back to WAIT — the exact bug fixed hours
    # earlier. Unknown must stay unknown; the decider already abstains on it.
    post_tokens = _tokens(getattr(context, "text", "") or "")
    pillar_tokens = {token for pillar in pillars for token in _tokens(pillar)}

    subject = _overlap(post_tokens, pillar_tokens) if pillar_tokens else None
    shared = sorted(post_tokens & pillar_tokens)[:5]

    handle, _basis = relationships.reply_target(
        {"parent_author": getattr(context, "author_handle", None)
         or getattr(context, "author", None), "text": ""})
    rel = relationships.get_author_relationship(handle) if handle else None

    return {
        "subject": {
            "score": round(subject, 3) if subject is not None else None,
            "bridge": (f"Overlaps your pillars on: {', '.join(shared)}." if shared
                       else "No overlap with a demonstrated pillar was found."
                       if pillar_tokens else "No pillars are known yet."),
        },
        "audience": {
            # Honest: the audience read is derived from pillar overlap until
            # conversion data exists. Claiming more would be inventing evidence.
            "score": round(subject, 3) if subject is not None else None,
            "bridge": ("Your existing audience follows you for these topics."
                       if shared else "No named audience bridge yet."),
        },
        "relationship": {
            "score": (rel or {}).get("topic_overlap"),
            "interactions": (rel or {}).get("interactions", 0),
            "bridge": (f"You have engaged @{handle} {rel['interactions']} time(s)."
                       if rel else
                       f"No recorded relationship with @{handle}." if handle
                       else "The author is unknown."),
        },
        "voice": {
            "score": None,
            "bridge": "Voice fit is judged at drafting time, not here.",
        },
    }


def _audience_forecast(context: PostContext, fit: dict[str, Any],
                       angles: list[dict[str, Any]]) -> dict[str, Any]:
    """§5.3a: a small falsifiable forecast, not "this could do well".

    Explicitly a hypothesis about why the intended people might act — never a
    claim about what X will rank.
    """
    lead = angles[0] if angles else None
    primary = lead.get("primary_action") if lead else "favorite"
    return {
        "core": fit["subject"]["bridge"],
        "adjacent": fit["relationship"]["bridge"],
        "primary_action": primary,
        "secondary_action": "reply" if primary != "reply" else "share",
        "conversion_bridge": ("The account's archive shows repeated work on "
                              "these topics." if fit["subject"].get("score")
                              else "No coherent account promise is evidenced yet."),
        "failure_mode": (lead.get("failure_mode") if lead else
                         "There is no distinct point, so it may read as a recap."),
        "evidence_quality": ev.DIRECTIONAL if angles else ev.LIMITED,
        "note": "A testable explanation of why people might act — not an X prediction.",
    }


# Function words that are common in one language and rare in the others. A
# whole language-ID dependency would be overkill for one card line; this only
# has to answer "is this clearly not English?" and stay silent when unsure.
_NON_ENGLISH_MARKERS = frozenset("""
el la los las un una und der die das ist nicht für mit auch aber sehr
de du le les des est pas pour avec dans sur mais très
que por con para como más pero muy este esta são não uma para
il lo gli che di da per non più molto
""".split())


# Ad copy, by its call to action. Two independent hits required so a genuine
# post that happens to say "sign up" is not mistaken for an ad.
#
# "Independent" means independent SIGNALS, and it did not: one entry bundled
# "link in bio", "limited time", "use code" and "N% off" into a single
# alternation, so "My course is live. Link in bio. First 50 people get 40% off."
# — three textbook ad signals — counted as ONE hit, missed the bar of two, and
# was scored as a live conversation worth replying to. Each signal now gets its
# own entry, which is what the rule above always claimed.
_PROMO_PATTERNS = (
    re.compile(r"\bsign up (?:now|today|free|here)\b", re.I),
    re.compile(r"\bfree (?:for )?\d+[- ]day\b|\bfree trial\b", re.I),
    re.compile(r"\bno credit card (?:required|needed)\b", re.I),
    re.compile(r"\blink in bio\b", re.I),
    re.compile(r"\blimited time\b|\bspots? (?:left|remaining)\b|"
               r"\bfirst \d+ (?:people|buyers|customers|signups)\b", re.I),
    re.compile(r"\buse code\b|\b\d{1,2}% off\b", re.I),
    re.compile(r"\btry (?:our|it) (?:free|now)\b|\bget started (?:free|today)\b", re.I),
    re.compile(r"\bbook a demo\b|\bpre-?order (?:now|today)\b", re.I),
    re.compile(r"\b(?:my|our) (?:course|cohort|program|bootcamp) (?:is )?(?:live|open|out)\b|"
               r"\benroll(?:ment)? (?:now|is open)\b|\bjoin my\b|\bdm me (?:for|if)\b", re.I),
)


def visible_text(context: PostContext) -> str:
    """EVERYTHING the reader sees: the post, the quoted layer, and the images.

    "my code at 3am:" and "the odds moved." are not thin posts — the picture is
    the post. Judging the caption alone made image posts read as having nothing
    to respond to, which is how a chart that WAS the evidence produced a WAIT.
    Alt text is used because it is free and reliable; the vision description is
    richer but costs seconds, so it joins the prompt rather than this read.
    """
    parts = [str(getattr(context, "text", "") or ""),
             str(getattr(context, "quote_text", "") or "")]
    for item in (getattr(context, "media", None) or []):
        alt = getattr(item, "alt", None)
        if alt:
            parts.append(str(alt))
    return "\n".join(part for part in parts if part.strip()).strip()


def conversation_text(context: PostContext) -> str:
    """visible_text PLUS the conversation above it.

    A reply inside a thread is not a standalone post: "Same. The tooling is what
    keeps me on the hosted version though." carries almost nothing on its own,
    while the message it answers is a detailed technical claim. The angle
    search read only the post in front of it, so every depth-2 reply came back
    with no angle and the catch-all "I could not find a point worth adding".

    Kept separate from visible_text on purpose. visible_text answers "how much
    does THIS post say", which the thin-post check still needs; this answers
    "what is the conversation about", which is what having something to say
    depends on.
    """
    parts = [visible_text(context),
             str(getattr(context, "parent_text", "") or "")]
    # thread_text is a LIST of messages, not a string. Stringifying it fed the
    # angle search "['first message', 'second message']" — brackets, quotes and
    # commas read as words, and the actual sentences buried inside quoting.
    thread = getattr(context, "thread_text", None) or []
    if isinstance(thread, str):
        parts.append(thread)
    else:
        parts.extend(str(message) for message in thread if message)
    return "\n".join(part for part in parts if str(part).strip()).strip()


def _has_image(context: PostContext) -> bool:
    return any(getattr(item, "kind", "") == "image"
               for item in (getattr(context, "media", None) or []))


def _looks_promotional(text: str) -> bool:
    return sum(1 for pattern in _PROMO_PATTERNS if pattern.search(text or "")) >= 2


def _looks_non_english(text: str) -> bool:
    """True only when the evidence is clear; unknown reads as English."""
    words = re.findall(r"[a-zà-ÿ]+", (text or "").lower())
    if len(words) < 6:
        return False
    hits = sum(1 for word in words if word in _NON_ENGLISH_MARKERS)
    return hits / len(words) >= 0.15


def _angle_space(context: PostContext, recent_user_texts: list[str]) -> tuple[
        list[dict[str, Any]], list[str]]:
    """The missing useful point, and the anti-patterns present (§5.4).

    Most weak replies fail by repeating the parent, so this searches for what is
    NOT already said rather than scoring what is.
    """
    # Both visible layers: a quote-post's own line is often a four-word reaction
    # over the claim that actually carries the substance, and reading only the
    # outer line found no angle at all on a perfectly good quote target.
    parent = visible_text(context)
    parent_tokens = _tokens(parent)
    anti: list[str] = []

    # Does the user already have near-identical wording in flight? Compared
    # against THIS post only — matching against the whole thread would call a
    # repeat on wording that belongs to someone else in the conversation.
    for text in recent_user_texts:
        if _overlap(_tokens(text), parent_tokens) >= REPEAT_SIMILARITY:
            anti.append("recent_repeat")
            break

    # A DEAD THREAD HAS NO ANGLES, because it has no conversation left to join.
    # Widening the angle search made a 15h-old thread with 1717 replies produce
    # an angle from the word "codexy", and the decider promoted that into
    # "post this yourself" — a standalone post about a launch whose "tomorrow"
    # was yesterday. Finding something to say is not the same as there being
    # somewhere to say it.
    try:
        from .conversation import thread_temperature   # local: avoids a cycle
        if thread_temperature(context)[0] == "dead":
            anti.append("dead_thread")
            return [], anti
    except Exception:
        pass

    # Angles are searched across the whole conversation, because that is where
    # the substance of a nested reply lives.
    parent = conversation_text(context) or parent
    lowered = parent.lower()
    angles: list[dict[str, Any]] = []

    # Each angle is only offered when the parent leaves room for it. This is
    # deliberately conservative: a fabricated angle is worse than WAIT.
    if any(word in lowered for word in ("will", "going to", "predict", "soon", "next")):
        angles.append({
            "kind": "explicit_prediction",
            "why": "The post makes a forward claim that can be given a falsifiable condition.",
            "primary_action": "reply",
            "failure_mode": "A prediction without a condition is just an opinion.",
        })
    if any(word in lowered for word in ("vs", "versus", "better than", "compared", "instead of")):
        angles.append({
            "kind": "clear_distinction",
            "why": "A comparison is present that can be sharpened into a distinction.",
            "primary_action": "share",
            "failure_mode": "Restating the comparison adds nothing.",
        })
    if any(word in lowered for word in ("should", "must", "never", "always")) or _UNIVERSAL.search(parent):
        angles.append({
            "kind": "overlooked_tradeoff",
            "why": "A universal claim usually hides a trade-off worth naming.",
            "primary_action": "reply",
            "failure_mode": "Disagreeing without evidence reads as hostility.",
        })
    if any(char.isdigit() for char in parent):
        angles.append({
            "kind": "verification_question",
            "why": "The post carries a number whose source can be asked for precisely.",
            "primary_action": "reply",
            "failure_mode": "A sceptical question without a premise looks like sniping.",
        })
    if len(parent) > 180:
        angles.append({
            "kind": "practical_implication",
            "why": "The post is long enough to have consequences it does not spell out.",
            "primary_action": "share",
            "failure_mode": "Summarising the post is a recap, not a contribution.",
        })

    # The five checks above are all keyword or digit matches, which left the
    # most repliable posts on X with no angle at all: a direct question, someone
    # answering him in his own thread, a chart, a linked article. Measured over
    # the arena, seven reply surfaces declined 100% of the time with "I could
    # not find a point worth adding" — not because there was nothing to say, but
    # because the post happened to contain no digit and none of fifteen words.
    #
    # These four stay inside the same rule as the rest: each fires only when the
    # post itself supplies the thing to respond to, so none of them invents an
    # angle where there is none.
    if _asks_a_question(parent):
        angles.append({
            "kind": "direct_answer",
            "why": "The post asks a question, and answering it is the reply.",
            "primary_action": "reply",
            "failure_mode": "Talking around the question instead of answering it.",
        })
    if _addressed_to_me(context):
        angles.append({
            "kind": "owed_response",
            "why": "Someone replied to you. Answering your own thread is the "
                   "cheapest reach on the platform.",
            "primary_action": "reply",
            "failure_mode": "A generic thanks adds nothing they could not predict.",
        })
    if _has_image(context):
        angles.append({
            "kind": "read_the_image",
            "why": "The picture carries the claim, so the reply can engage what "
                   "it actually shows.",
            "primary_action": "reply",
            "failure_mode": "Replying to the caption while ignoring the image.",
        })
    if _reports_experience(parent):
        angles.append({
            "kind": "compare_experience",
            "why": "They report first-hand experience, which yours can be "
                   "measured against.",
            "primary_action": "reply",
            "failure_mode": "Agreeing without adding your own numbers.",
        })
    # THREE ARGUMENT SHAPES, not three more keyword lists. Measured on a battery
    # of fifteen realistic timeline posts, four substantive ones declined with
    # "I could not find a point worth adding to this one" — a terminal WAIT, so
    # not even overridable:
    #
    #   "The batching numbers only hold if you keep the KV cache resident."
    #   "You do not need a vector DB until your context no longer fits."
    #   "The airdrop meta is dead because the incentive design rewards sybils."
    #   "That kills half the glue code in every voice agent I have seen."
    #
    # Each is an argument with an exposed joint, and each carried no digit and
    # none of the fifteen words above. The gap was never "is there something to
    # say" — it was that the search only knew how to recognise a prediction, a
    # comparison, a number, or length. These three match on the JOINT itself, so
    # they stay inside the same rule as the rest: the post supplies the thing to
    # respond to, and none of them invents an angle where there is none.
    if _MECHANISM.search(parent):
        angles.append({
            "kind": "stated_mechanism",
            "why": "The post says WHY, so the reply can test the mechanism "
                   "rather than the conclusion.",
            "primary_action": "reply",
            "failure_mode": "Arguing with the conclusion while leaving the "
                            "stated cause untouched.",
        })
    if _BOUNDARY.search(parent):
        angles.append({
            "kind": "boundary_condition",
            "why": "The claim is scoped to a condition, and where that boundary "
                   "actually sits is the useful reply.",
            "primary_action": "reply",
            "failure_mode": "Accepting the stated boundary instead of testing it.",
        })
    if _CONTRARIAN.search(parent):
        angles.append({
            "kind": "contested_norm",
            "why": "The post says the accepted thing is wrong — either the "
                   "evidence for that is missing, or the counterexample is.",
            "primary_action": "reply",
            "failure_mode": "Agreeing loudly, which adds a vote and not a fact.",
        })

    coined = _coined_subject(parent)
    if coined:
        angles.append({
            "kind": "named_subject",
            "why": f"'{coined}' is the subject, said in shorthand.",
            "primary_action": "reply",
            "failure_mode": "Answering the wording instead of the thing it names.",
        })

    if not angles:
        anti.append("restatement")
    return angles, anti


# A real question, not a rhetorical flourish: either it ends in a question mark
# or it uses one of the forms people actually use to ask the timeline something.
_QUESTION_FORMS = re.compile(
    r"\b(?:does anyone|has anyone|is anyone|anyone else|any recommendations|"
    r"how do you|how are you|what do you|which one|is it worth|am i wrong|"
    r"genuine question|honest question|thoughts\?)", re.I)


def _asks_a_question(text: str) -> bool:
    stripped = (text or "").strip()
    return bool(stripped.endswith("?") or _QUESTION_FORMS.search(stripped))


def _addressed_to_me(context: PostContext) -> bool:
    """Is this someone replying to HIS post?

    Reuses the ownership read the rest of the pipeline already shares rather
    than re-deriving it, so "this is my thread" means the same thing here as it
    does on the card.
    """
    try:
        from .conversation import conversation_state   # local: avoids a cycle
        return conversation_state(context).ownership == "own_thread"
    except Exception:
        return False


# First-hand use, which he can answer with his own measurements. Deliberately
# narrow: "I think" is an opinion, "I've been running it for a month" is data.
# "seen", "built", "benchmarked" and the rest were missing, so "in every voice
# agent I have seen" — a first-hand sample size, stated as one — read as no
# experience at all.
_EXPERIENCE = re.compile(
    r"\b(?:i(?:'ve| have)? (?:been )?(?:running|using|tried|switched|tested|shipped|"
    r"seen|built|deployed|benchmarked|measured|migrated|rolled out)|"
    r"we (?:run|use|tried|switched|tested|moved|built|deployed|benchmarked|measured)|"
    r"my (?:setup|rig|box|server|bill)|"
    r"in production|for (?:a|two|three|\d+) (?:week|month|year)s?)\b", re.I)

# A universal claim, which was only ever matched as the literal word "everyone".
# "EVERY protocol that tried it got mercenary liquidity" is the same shape and
# matched nothing. The lookahead keeps out the frequency sense — "I use it every
# day" quantifies a habit, not a class, and carries no trade-off to name.
_UNIVERSAL = re.compile(
    r"\bevery(?:one|body|thing)?\b"
    r"(?!\s+(?:day|week|month|year|time|morning|night|hour|minute|"
    r"so often|now and (?:then|again)))"
    r"|\b(?:no ?body|no one|none of (?:them|these|those|us)|"
    r"all of (?:them|these|those|us)|not a single|without exception)\b", re.I)

# A stated MECHANISM. The post does not just assert, it says why — so the reply
# can test the why, which is both harder to dismiss and more useful than
# arguing with the conclusion.
_MECHANISM = re.compile(
    r"\b(?:because|the reason (?:is|why)|which is why|that'?s why|"
    r"the (?:problem|issue|catch|trick) is|comes down to|boils down to|"
    r"due to|driven by|caused by|the result of)\b"
    # THE DIAGNOSIS SHAPE: "the fatal flaw in this benchmark IS that it leaks
    # the eval set". A named defect plus what it actually is — one of the most
    # repliable things on this timeline, and it matched nothing, so the verdict
    # was "I could not find a point worth adding to this one".
    r"|\bthe\s+(?:\w+\s+){0,2}"
    r"(?:flaw|problem|issue|catch|trick|mistake|bug|gap|risk|danger|"
    r"whole point|real (?:problem|issue|reason))\b"
    r"[^.!?\n]{0,40}?\bis\b", re.I)

# A BOUNDARY the claim depends on: where it stops being true. Naming where that
# boundary actually sits is the most useful thing a reply can do to a scoped
# claim, and it is the shape of half the good technical posts on this timeline.
_BOUNDARY = re.compile(
    # "only hold if" needs the general shape, not a verb list: the list had
    # "holds" and the post said "hold", so the one case this angle was written
    # for still declined.
    r"\b(?:only (?:if|when)|only \w+s? (?:if|when)|"
    r"only (?:works|holds|matters|applies|true)|unless|"
    r"until (?:you|your|it|the|they)|as long as|provided (?:that|you)|"
    r"assuming (?:you|your|the|it)|depends (?:on|entirely)|"
    r"breaks (?:down )?(?:when|at|once)|stops working|"
    r"falls apart (?:when|at|once)|edge case)\b", re.I)

# A CONTRARIAN claim: the accepted thing is wrong. Either the evidence for that
# is missing, or the counterexample is — both are replies with a point.
_CONTRARIAN = re.compile(
    r"\b(?:you (?:do ?n[o']?t|don'?t) (?:need|have to|want|actually)|"
    r"(?:no ?body|no one) (?:needs|wants|should|actually)|"
    r"stop (?:using|doing|building|shipping|treating)|"
    r"is ?n[o']?t (?:actually|really)|not actually\b|"
    r"no longer (?:true|works|matters|needed|the case)|"
    r"(?:overrated|underrated|a myth|is dead|are dead))\b", re.I)


def _reports_experience(text: str) -> bool:
    return bool(_EXPERIENCE.search(text or ""))


# Deliberately narrow. Only explicit laughter markers, or the shape that makes
# a joke a joke on this timeline: a technical thing measured against something
# from the poster's private life ("my model router has better uptime than my
# relationship"). Anything less certain keeps the ordinary reason, because
# mislabelling a serious post as a bit is worse than the catch-all it replaces.
_JOKE_MARKERS = re.compile(
    r"(?:\blol\b|\blmao\b|\bhaha\b|\bjk\b|/s\b|\U0001F602|\U0001F923|\U0001F60D)",
    re.IGNORECASE)
_PRIVATE_LIFE = re.compile(
    r"\bmy (?:relationship|marriage|girlfriend|boyfriend|wife|husband|ex|"
    r"therapist|sleep schedule|dating life|landlord|mother|dad|cat|dog)\b",
    re.IGNORECASE)


def _coined_subject(text: str) -> str | None:
    """"Tomorrow is feeling codexy" -> "codex".

    The resolver already existed, but only the grounding query used it: the
    search knew the post was about Codex while the angle search saw an unknown
    word, found nothing to say, and declined. This is the exact shape of the
    post that started the worst output on record — the coinage IS the subject.
    """
    try:
        from .radar import _entity_lexicon, _resolve_coinage   # local: cycle
        lexicon = _entity_lexicon()
        if not lexicon:
            return None
        for token in re.findall(r"\b[A-Za-z][A-Za-z0-9]{4,}\b", text or ""):
            stem = _resolve_coinage(token, lexicon)
            # Only a COINED form counts. The plain entity is already handled by
            # the checks above, and treating it as a coinage would hand every
            # post that says "Claude" an extra angle.
            if stem and stem != token.lower():
                return stem
    except Exception:
        return None
    return None


def _looks_like_a_joke(text: str) -> bool:
    body = text or ""
    if _JOKE_MARKERS.search(body):
        return True
    # The comparison has to be TO the private-life thing, which is what makes
    # it a punchline rather than a passing mention.
    return bool(_PRIVATE_LIFE.search(body)
                and re.search(r"\b(?:than|like|compared to|vs\.?)\b", body, re.I))


def _readiness(context: PostContext, angles: list[dict[str, Any]]) -> dict[str, bool]:
    """§5.4a viral-readiness checks, as named booleans rather than a vibe."""
    text = getattr(context, "text", "") or ""
    return {
        # Can a stranger understand a response without the parent? Proxy: the
        # parent itself is self-contained enough to quote.
        "immediate_comprehension": len(text.strip()) >= 40,
        "information_gain": bool(angles),
        # Several valid reasons to act, not one demanded action.
        "action_elasticity": len({a["primary_action"] for a in angles}) > 1,
        "native_payoff": "http" not in text.lower() or len(text) > 120,
        "semantic_novelty": "recent_repeat" not in text,
        "trust_surplus": not any(word in text.lower() for word in
                                 ("guaranteed", "everyone knows", "obviously")),
    }


def _reply_surface(context: PostContext, age: float | None,
                   momentum_read: dict[str, Any], angles: list[dict[str, Any]],
                   anti: list[str], prior_replies: int) -> tuple[str, str]:
    """§5.3c: strong | directional | weak | blocked, with the reason.

    A recommendation-quality assessment, never a prediction of X reply rank —
    the released reply service's formula is not public.
    """
    if getattr(context, "reply_restricted", False):
        return "blocked", "Replies are restricted on this post."
    if getattr(context, "visibility", "public") == "protected":
        return "blocked", "The account is protected."
    # A promo post is not a conversation: the account is running an ad, the
    # replies are support tickets, and a sharp take there earns nothing
    # (registry I104). Cheap to detect from the copy itself.
    if _looks_promotional(str(getattr(context, "text", "") or "")):
        return "weak", "This is an ad. Replying to it earns nothing."

    # Engagement bait was only ever screened in his own DRAFTS, never in the
    # post he is replying to — so "Like if you agree that AGI is coming in 2027.
    # Retweet if you disagree." read as an ordinary target and drew a real
    # reply, because the year in it counted as a number worth questioning.
    # There is no version of answering bait that earns him anything.
    from .safety import ENGAGEMENT_BAIT_PATTERN
    if ENGAGEMENT_BAIT_PATTERN.search(str(getattr(context, "text", "") or "")):
        return "weak", ("This is engagement bait. Replying to it feeds someone "
                        "else's numbers and earns you nothing.")

    # A DEAD THREAD OUTRANKS EVERY OTHER WEAK REASON. Even a perfectly distinct
    # point lands in an empty room, and "everyone is already making the same
    # point" on a thread that stopped moving 14 hours ago is both unhelpful and
    # probably untrue. Checked before the angle read so the card names the fact
    # he can act on (registry B21).
    from .conversation import thread_temperature      # local: avoids a cycle
    temperature, temperature_line = thread_temperature(context)
    if temperature == "dead":
        return "weak", temperature_line
    if not angles:
        # A NON-ENGLISH POST is not an empty one. The angle finder is
        # English-only, so a substantial Spanish post came back as "I could not
        # find a point worth adding" — blaming the post for a limitation of the
        # reader. Name the real reason (registry C39).
        if _looks_non_english(str(getattr(context, "text", "") or "")):
            return "weak", ("This post is not in English, and I would be guessing at "
                            "the nuance. Reply in their language yourself if you have "
                            "the point.")
        # Plain language: "survived the angle-space check" is internal
        # vocabulary that reached the user verbatim in the decision card. But
        # ONE sentence cannot serve three different situations — "everyone is
        # already making the same point" is simply false about a vague one-line
        # post nobody has replied to, which is what it used to say.
        crowd = getattr(context, "visible_reply_count", None)
        body = visible_text(context)               # text + quote + image alt
        if isinstance(crowd, (int, float)) and crowd >= 200:
            return "weak", "Everyone is already making the same point here."
        # A POST CUT OFF BEHIND "Show more" IS NOT A THIN POST — being cut off
        # is precisely what makes it LOOK thin. The extension detects the
        # expander and says so; the schema dropped the flag, so a long argument
        # whose visible opening happened to be setup read as having nothing in
        # it. Checked before the length test for that reason.
        if getattr(context, "is_long_post", False):
            return "directional", ("This post is cut off — open 'Show more' "
                                   "before replying, or you are answering the "
                                   "setup and not the point.")
        if len(body.split()) <= 6:
            # An image post with a three-word caption is not a thin post — the
            # picture carries it, and saying "nothing specific here" about a
            # chart is just wrong.
            if _has_image(context):
                return "directional", ("The image is the post here — read it before replying.")
            return "weak", "There is nothing specific in this post to respond to."
        # A JOKE IS NOT AN EMPTY POST. Declining is right — analysing a bit is
        # the fastest way to look like a bot — but "I could not find a point
        # worth adding" reads as though the post was vacuous, when the reader
        # got exactly what it was for. Naming it lets him decide whether to
        # play along in his own words.
        if _looks_like_a_joke(visible_text(context)):
            return "weak", ("This is a bit, not a claim. Analysing it would land "
                            "badly — if you want in, match the joke in your own "
                            "words.")
        # A post whose payload is a link is not a thin post — the article is the
        # subject. Since W1/D2 (2026-09-21) the ✦ press READS that link through
        # the world service before the model decides, so this no longer claims
        # it cannot; it is only the timing prior's note, and the model's own
        # verdict, made with the article's text in hand, is what the card shows.
        if getattr(context, "links", None):
            return "weak", "The linked article is the real subject here."
        return "weak", "I could not find a point worth adding to this one."
    if "recent_repeat" in anti:
        return "weak", "You have already made a near-identical point recently."
    if prior_replies:
        return "directional", (f"You have replied to this author {prior_replies} time(s) "
                               "recently; author diversity attenuates repetition.")

    # The rest of what the visible thread says (registry B21/I94/I99): 1.7K
    # existing replies on an hours-old post mean a new one lands far down a
    # pile nobody scrolls. (The dead case is handled above, before the angle
    # read, because it outranks every other weak reason.)
    crowd = getattr(context, "visible_reply_count", None)
    if isinstance(crowd, (int, float)) and crowd >= 500 and (age or 0.0) > 120:
        return "weak", (f"~{int(crowd):,} replies are already here and the post is hours old — "
                        "a new one lands far down the pile.")
    if temperature == "hot":
        # Age interacts with life, it doesn't override it (B23): an older post
        # whose thread is landing replies RIGHT NOW is still a live room.
        return "strong", "Replies are landing right now — the room is live."

    trend = momentum_read.get("trend")
    if age is not None and age <= REPLY_STRONG_MINUTES:
        return "strong", "The conversation is fresh and a distinct point exists."
    if age is not None and age <= REPLY_VIABLE_MINUTES:
        return "strong" if trend == "accelerating" else "directional", (
            "Still inside the useful reply window.")
    if age is not None and age <= REPLY_NEEDS_MOVEMENT_MINUTES:
        if trend == "accelerating":
            return "directional", "Older, but the post is still accelerating."
        return "weak", "Old enough that it needs proven movement, and there is none."
    if age is None:
        return "directional", "Post age is unknown, so the window cannot be judged."
    # THE HONEST SENTENCE WAS ALREADY COMPUTED AND THROWN AWAY. Called against
    # real inputs this line fired on `warming` (last reply 45 minutes ago — the
    # thread is measurably still active), on `cooling`, and on `unknown` (the
    # thread was never scanned, so nothing was observed at all). Meanwhile
    # `thread_temperature` had produced "The last reply was ~3h ago — this
    # thread is winding down" and only the `dead` branch ever returned it.
    #
    # No confidence adverb in any branch: name what was seen, or say it was not
    # seen. `opportunities.py`'s dying-thread note is the model, and :1073 above
    # is the in-file precedent.
    if temperature in ("warming", "cooling") and temperature_line:
        return "weak", temperature_line
    # SAY WHAT AGE ESTABLISHES, THEN WHAT VISIBILITY DOES NOT. An earlier
    # version of this fix reported only the unknown half and dropped the window
    # fact entirely — over-correcting a sentence that claimed more than it knew
    # into one that claimed less. Reaching here means age >
    # REPLY_NEEDS_MOVEMENT_MINUTES (240), so the closed window is the same
    # calibrated bracket `opportunities.py` states as "over 4 hours old — the
    # reply window is normally closed". No threshold is invented and no
    # confidence adverb is used for the half that genuinely is unknown.
    hours = int((age or 0) // 60)
    window = f"{hours} hours old, so the reply window has normally closed."
    if not getattr(context, "thread_scanned", False):
        return "weak", (f"{window} The thread was not visible, so whether it "
                        "is still moving cannot be judged.")
    return "weak", f"{window} No reply activity is visible in the thread."


def _identity_tokens(context: PostContext) -> set[str]:
    handle = (getattr(context, "author_handle", "") or "").lstrip("@").lower()
    name = (getattr(context, "author", "") or "").lower()
    # "ai"/"official"/"hq" are identity SUFFIXES, not the brand, so drop them:
    # @claudeai's brand token is "claude", and matching "ai" would make every
    # post about AI look first-party.
    junk = {"the", "official", "real", "team", "app", "inc", "hq", "ai", "eng", "labs"}
    return {tok for tok in re.findall(r"[a-z]{4,}", handle + " " + name)
            if tok not in junk}


def _source_authority(context: PostContext) -> dict[str, Any]:
    """Who is speaking, and how much that alone lets us trust the claim (§5.3b).

    The gap the user hit: the analysis had no idea whether a post came from the
    OFFICIAL account it is about or from a random person. @claudeai announcing
    Claude Fable pricing is a PRIMARY SOURCE - authoritative about itself, and
    calling that "thin evidence" is nonsense. A verified account speaking about
    its own brand is first-party; a verified account speaking about something
    else is merely credible; an unverified account's bare claim stands alone.
    """
    verified = bool(getattr(context, "author_verified", False))
    handle = (getattr(context, "author_handle", "") or "").lstrip("@") or "this account"
    text = (getattr(context, "text", "") or "").lower()
    id_tokens = _identity_tokens(context)
    speaks_about_self = any(tok in text for tok in id_tokens)

    # Your own post is not a source to be vetted. Assessing it as one produced
    # "@example_builder is a verified account, so identity is confirmed" on the
    # user's OWN timeline — which is why it read as though Starling did not know
    # whose account it was looking at.
    if getattr(context, "is_own_post", False):
        return {"level": "own_post", "verified": verified,
                "confidence": ev.STRONG,
                "note": "This is your own post, so you already know the source."}

    if verified and speaks_about_self:
        return {"level": "first_party", "verified": True,
                "confidence": ev.STRONG,
                "note": (f"Primary source: @{handle} is the verified account this "
                         f"is about, so it is authoritative on its own claim.")}
    if verified:
        return {"level": "verified", "verified": True,
                "confidence": ev.DIRECTIONAL,
                "note": f"@{handle} is a verified account, so identity is confirmed."}
    return {"level": "standard", "verified": False,
            "confidence": ev.UNKNOWN_CONFIDENCE,
            "note": (f"@{handle} is not a verified account, so a factual claim here "
                     f"is not corroborated on its own.")}


def _reach_confidence(context: PostContext) -> tuple[str, str | None]:
    """Evidence strength from a post's OWN visible engagement (§1, §5.3a).

    The extension sends the counts already shown on the post — views, likes,
    reposts, replies, bookmarks — and until now nothing read them. They are
    direct, first-hand evidence of reach and liveness: a post with 26M views and
    53K likes is not "thin evidence", it is a large, live conversation observed
    at first hand. This is separate from momentum, which needs two snapshots to
    say whether that reach is still GROWING; here we only judge how much signal
    the post already carries.
    """
    metrics = getattr(context, "visible_metrics", None) or {}

    def num(key: str) -> float:
        value = metrics.get(key)
        try:
            return float(value) if value is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    interactions = int(num("likes") + num("reposts") + num("replies") + num("bookmarks"))
    views = int(num("views"))
    if interactions <= 0 and views <= 0:
        return ev.UNKNOWN_CONFIDENCE, None

    # Interactions map straight onto the shared sample thresholds (30 -> strong).
    from_interactions = ev.confidence_for_samples(interactions)
    # Views run on a much larger scale, so they get their own ladder: being seen
    # a thousand times is already strong evidence a post is live and reaching.
    from_views = (ev.STRONG if views >= 1000 else
                  ev.DIRECTIONAL if views >= 100 else
                  ev.LIMITED if views >= 1 else ev.UNKNOWN_CONFIDENCE)
    conf = ev.best_of(from_interactions, from_views)
    # Only call a post "live" when the numbers actually support it; a 4-view
    # post is real evidence of being SEEN barely at all, not of being live.
    if conf in (ev.STRONG, ev.DIRECTIONAL):
        note = (f"The post already shows {views:,} views and {interactions:,} "
                f"interactions - direct evidence it is live.")
    else:
        note = (f"The post shows only {views:,} views and {interactions:,} "
                f"interactions, so there is little reach to build on.")
    return conf, note


def verify_claim(context: PostContext, authority: dict[str, Any] | None = None,
                 *, budget_seconds: float = 6.0) -> dict[str, Any]:
    """Check whether the post's claim holds up, and report the SCOPE (§1, §4.1a).

    Honest by construction: a first-party primary source needs no external
    check, and when it is a third-party claim the check is a bounded keyless web
    search that reports how many independent sources corroborate it - never a
    fabricated "confirmed". If the search cannot run (offline, disabled), it
    says so rather than guessing.
    """
    authority = authority or _source_authority(context)
    if authority["level"] == "first_party":
        return {"status": "authoritative", "scope": "first-party primary source",
                "sources": [], "note": authority["note"]}

    text = (getattr(context, "text", "") or "").strip()
    try:
        from . import research
        if not research.research_enabled() or not text:
            return {"status": "unchecked", "scope": "no external check available",
                    "sources": [],
                    "note": "I have not corroborated this against outside sources."}
        query = " ".join(text.split()[:16])
        results = research.search_web(query, max_results=5) or []
    except Exception:
        return {"status": "unchecked", "scope": "check failed", "sources": [],
                "note": "I could not reach outside sources to corroborate this."}

    sources = [{"title": r.get("title", ""), "url": r.get("url", "")}
               for r in results if r.get("url")][:4]
    if len(sources) >= 2:
        status, scope = "corroborated", f"{len(sources)} independent sources"
        note = f"Corroborated by {len(sources)} outside sources."
    elif sources:
        status, scope = "weak", "1 source"
        note = "Only one outside source mentions this, so treat it as unconfirmed."
    else:
        status, scope = "unconfirmed", "no sources found"
        note = "No outside source corroborates this yet."
    return {"status": status, "scope": scope, "sources": sources, "note": note}


def analyze(context: PostContext, *, profile: dict[str, Any] | None = None,
            recent_user_texts: list[str] | None = None) -> Analysis:
    """Turn one post into decision-ready evidence. Chooses nothing."""
    result = Analysis()
    own_text = getattr(context, "text", "") or ""
    # THE QUOTED LAYER IS PART OF THE POST. A quote-post's own line is often
    # four words ("This aged like milk.") over a substantial claim — reading
    # only the outer line judged the whole thing as "nothing specific to
    # respond to" and waited on a perfectly good quote target. What the reader
    # sees is both layers, so the analysis reads both.
    quoted = str(getattr(context, "quote_text", "") or "").strip()
    text = f"{own_text}\n{quoted}".strip() if quoted else own_text
    result.summary = (own_text[:140] + "…") if len(own_text) > 140 else own_text
    result.topic_tokens = sorted(_tokens(text))[:12]
    result.age_minutes = _age_minutes(context)

    # Safety first: a blocked context never reaches the rest of the analysis.
    assessment = assess_context(context)
    if getattr(assessment, "blocked", False):
        result.blockers = list(getattr(assessment, "reasons", []) or
                               ["This context is not safe to draft from."])
        result.confidence = ev.STRONG
        result.reply_surface = "blocked"
        result.reply_surface_reason = result.blockers[0]
        return result
    result.penalties = list(getattr(assessment, "reasons", []) or [])

    post_id = getattr(context, "post_id", None)
    if post_id:
        read = mom.best_available(post_id, posted_at=getattr(context, "post_created_at", None))
        result.momentum = read.as_dict()
        # THE AUDIENCE IS PRINTED ON THE SCREEN. Momentum works from a snapshot
        # SERIES, which only exists for posts Starling has watched over time —
        # never for a stranger's. So a post showing 1.6M views came back
        # "movement cannot be confirmed", as though nothing were known about
        # it, and the reply decision could not tell a viral thread from a dead
        # one. Velocity still needs a series; SIZE does not, and size is most
        # of what matters when deciding whether to walk into a room.
        visible = getattr(context, "visible_metrics", None) or {}
        try:
            views = float(visible.get("views") or 0)
        except (TypeError, ValueError):
            views = 0.0
        if views > 0:
            result.momentum["visible_views"] = views
            result.momentum.setdefault("basis", "none")
            if read.basis in ("none", "average"):
                result.momentum["trend"] = "large_audience" if views >= 100_000 else "measurable"
        if read.basis in ("none", "average"):
            if views >= 100_000:
                result.unknowns.append(
                    f"No metric history for this post, but {views:,.0f} views are "
                    f"visible — the size is known even though the movement is not.")
            else:
                result.unknowns.append(
                    "Only one metric observation, so movement cannot be confirmed.")
    else:
        result.momentum = {"basis": "none", "trend": "unknown"}
        result.unknowns.append("No post id, so no metric history is available.")

    result.fit = _fit(context, profile or {})
    # Replying to YOUR OWN post, the post is itself one of your recent posts —
    # so it matched itself, every single time, and came back "you have already
    # made a near-identical point" with zero angles and a WAIT. A self-reply is
    # not a repeat of the thing it is attached to; that is what a follow-up IS.
    # Drop the parent from the comparison set (and only the parent).
    history = list(recent_user_texts or [])
    if getattr(context, "is_own_post", False):
        parent = _tokens(getattr(context, "text", "") or "")
        if parent:
            history = [text for text in history
                       if _overlap(_tokens(text), parent) < 0.75]
    result.angles, result.anti_patterns = _angle_space(context, history)
    result.readiness = _readiness(context, result.angles)

    handle = (getattr(context, "author_handle", None) or
              getattr(context, "author", "") or "").lstrip("@").lower()
    prior = len(replies_to_author(handle)) if handle else 0
    result.reply_surface, result.reply_surface_reason = _reply_surface(
        context, result.age_minutes, result.momentum, result.angles,
        result.anti_patterns, prior)

    # Is the TARGET capable of supporting any move? An ad and a bait post are
    # not: the copy is a call to action, not an idea, so there is nothing in it
    # to reply to AND nothing in it to post about either. Recorded separately
    # from reply_surface because the decider offers POST as the alternative to
    # replying, and "don't reply to this ad" must not come back as "post your
    # own thing" off a score the ad never influenced.
    own_text = str(getattr(context, "text", "") or "")
    if _looks_promotional(own_text):
        result.target_quality = "ad"
    else:
        from .safety import ENGAGEMENT_BAIT_PATTERN
        if ENGAGEMENT_BAIT_PATTERN.search(own_text):
            result.target_quality = "bait"

    result.audience_forecast = _audience_forecast(context, result.fit, result.angles)

    lead = result.angles[0] if result.angles else None
    result.alignment = {
        "audience_bridge": result.fit["subject"]["bridge"],
        "surface_fit": result.reply_surface,
        "freshness": result.momentum.get("trend", "unknown"),
        "distinctness": lead["kind"] if lead else None,
        "primary_action": result.audience_forecast.get("primary_action"),
        "negative_action_risks": result.penalties or ["none detected"],
        "account_conversion": result.audience_forecast.get("conversion_bridge"),
        "visibility_boundary": am.VISIBILITY_BOUNDARY,
        # §5.3b: "strong" is only allowed when the first fields are evidenced.
        "label": ("strong" if (lead and result.fit["subject"].get("score"))
                  else "directional" if lead else "weak"),
    }

    if not (profile or {}).get("version"):
        result.unknowns.append("No account profile has been built yet.")
    if result.fit["subject"]["score"] is None:
        result.unknowns.append("No content pillars are known, so topic fit is unmeasured.")
    result.unknowns.append(am.VISIBILITY_BOUNDARY)

    # Confidence in the OPPORTUNITY is the STRONGEST independent line of support,
    # not the weakest. Reach (the post's own visible engagement) and angle space
    # are separate reasons to be confident; either alone means we are not in the
    # dark. weakest() was correct for a chain (freshness x baseline) but wrong
    # here — it let a missing SECOND metric snapshot report "thin evidence" for a
    # 26M-view post. Movement stays honestly unconfirmed and is surfaced on its
    # own, but that no longer collapses the whole judgement to unknown.
    reach_conf, reach_note = _reach_confidence(context)
    if reach_note:
        result.reach = reach_note

    # Who is speaking is a third independent line of confidence: a verified
    # first-party announcement is authoritative even before reach or angles are
    # weighed. This is what stops "@claudeai posting about Claude" reading as
    # thin evidence.
    result.source = _source_authority(context)
    result.confidence = ev.best_of(
        reach_conf,
        ev.confidence_for_samples(len(result.angles) * 10),
        result.source.get("confidence"),
    )
    return result
