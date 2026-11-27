"""GrowthScore: rank gate-surviving draft candidates by the strategy rubric.

Mirrors the manual's §9 scoring model with locally computable terms. Hard
gates (safety, bait, fabrication) remain REJECTS in the quality gate — this
score only orders candidates that already passed them, and encodes the real
objective: views and followers earned legitimately.

Bands surfaced to the UI: ≥88 "post-ready", 78-87 "strong", 68-77 "workable",
<68 "below-bar" (still returned for user-initiated create — never wait — but
labeled honestly).

Calibration: every scored candidate is logged; once ≥25 posted outcomes have
24h metrics, an isotonic score→P(above median) mapping is fitted offline and
applied here (monotone, so it re-scales without re-ordering).
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

from .algorithm_model import algorithm_alignment
from .storage import get_job_state, performance_records
from .strategy_v2 import get_strategy

# Term maxima (sum = 100). The engagement-design mapping (§8) shifts weight by
# (action, objective): replies/quotes live on hooks + topical borrowed reach;
# standalone posts live on identity (angle, voice, novelty).
# algorithm_alignment scores how the draft plays the published ranking
# mechanics (reply-magnet construction, no soft bait, no link-first hook).
TERM_MAXIMA = {
    "format_fit": 12.0,
    "topic_fit": 12.0,
    "angle_execution": 11.0,
    "voice_match": 11.0,
    "hook_strength": 8.0,
    "novelty": 8.0,
    "predicted_engagement": 8.0,
    "algorithm_alignment": 8.0,
    "objective_bonus": 3.0,
    # §8.4 terms the original rubric did not have. The existing weights were
    # scaled down proportionally rather than re-derived, so the relative order
    # of the terms that were already tuned is preserved and the maxima still
    # total 100.
    "immediate_orientation": 6.0,
    "audience_payoff": 5.0,
    "native_payoff": 4.0,
    "primary_action_execution": 2.0,
    "profile_conversion_coherence": 1.0,
    "factual_integrity": 1.0,
}

OBJECTIVE_EMPHASIS: dict[tuple[str, str], dict[str, float]] = {
    ("reply", "reach"): {"hook_strength": 1.35, "topic_fit": 1.25},
    ("reply", "conversation"): {"hook_strength": 1.2, "topic_fit": 1.2, "novelty": 1.1},
    ("quote", "reach"): {"hook_strength": 1.3, "angle_execution": 1.15},
    ("post", "followers"): {"angle_execution": 1.25, "voice_match": 1.2, "novelty": 1.15},
    ("post", "resonance"): {"voice_match": 1.3, "novelty": 1.1},
}

THROAT_CLEARERS = ("i think", "interesting", "so true", "just my thoughts", "here are my thoughts",
                   "great point", "well said", "hot take:", "unpopular opinion:")
LENGTH_SWEET_SPOTS = {"reply": (40, 200), "quote": (40, 220), "post": (80, 280)}

# How each angle looks when it is actually EXECUTED, as patterns rather than a
# handful of literal phrases.
#
# The literal list this replaces gave 94 of 157 stored drafts the exact floor
# score (3.85 of 11) — not because they failed to make the point, but because
# they made it in words the list did not contain. "GPT-5.6 Sol outperforms
# Fable 5 in coding, but costs less" is a direct comparison by any reading and
# matched none of "same level / better than / worse than / vs / compared to /
# not even close". An 11-point term, the joint-largest in the rubric, was
# effectively measuring vocabulary overlap.
ANGLE_SIGNALS: dict[str, tuple[str, ...]] = {
    "direct_comparison": (
        r"\b(?:better|worse|faster|slower|cheaper|pricier|stronger|weaker)\s+than\b",
        r"\b(?:outperforms?|beats?|edges? out|undercuts?|matches?|trails?|lags?"
        r"|leads?|wins? against|loses? to|prefer)\b",
        r"\bvs\.?\b|\bversus\b|\bcompared (?:to|with)\b|\bagainst\b",
        r"\bsame level\b|\bnot even close\b|\bcloser to\b|\bthe gap\b",
        r"\bcosts? less\b|\bfor half\b|\btwice the\b",
    ),
    "contradiction": (
        r"\beven the\b|\bmeanwhile\b|\bthey said\b|\bturns out\b",
        r"\bbut (?:it|they|the|that)\b|\bexcept\b|\byet\b|\bhowever\b",
        r"\bstill (?:in|not|no)\b|\bactually\b|\bin (?:reality|practice)\b",
        r"\bdon'?t trust\b|\bnobody (?:is|has|mentions)\b",
    ),
    "real_user_pain": (
        r"\brate limits?\b|\blimits?\b|\bpricing\b|\bquota\b|\bthrottl",
        r"\bcan'?t\b|\bcannot\b|\brestricted\b|\bblocked\b|\bbroke\b",
        r"\bannoying\b|\bpainful\b|\bfrustrating\b|\bwhy do i\b|\bhad to\b",
    ),
    "exact_prediction": (
        r"\bprediction\b|\bcalling it\b|\bmy bet\b|\bi (?:bet|expect|reckon)\b",
        r"\bwill (?:ship|release|launch|drop|land)\b|\bgoing to (?:ship|launch|drop)\b",
        r"\bby (?:the end of |early |late )?(?:jan|feb|mar|apr|may|jun|jul|aug|"
        r"sep|oct|nov|dec|q[1-4]|\d{4})",
        r"\bbefore (?:the )?(?:end|launch|october|november|december|\w+day)\b",
        r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b",
    ),
    "open_vs_closed": (
        r"\bopen[- ]?(?:source|weights?)\b|\bclosed\b|\bproprietary\b",
        r"\blocal(?:ly)?\b|\bself[- ]?host|\bon[- ]?prem|\bapi[- ]only\b",
    ),
    "benchmarks_vs_use": (
        r"\bbenchmarks?\b|\bevals?\b|\bleaderboard\b|\bscores?\b",
        r"\bactual use\b|\bin practice\b|\breal work\b|\bday[- ]to[- ]day\b"
        r"|\bfeels? different\b|\bon paper\b|\bin production\b",
    ),
    "release_cadence": (
        r"\bcadence\b|\bevery \d+ (?:weeks?|months?)\b|\bweeks? since\b"
        r"\b|\bpattern\b|\bevery time\b|\bback[- ]to[- ]back\b",
        r"\breleases?\b|\bshipp(?:ed|ing)\b|\blaunch(?:ed|es)?\b|\bdropped\b",
    ),
    "money_compute": (
        r"\$\s?\d|\bbillions?\b|\bmillions?\b|\bper (?:month|year|token)\b",
        r"\bcompute\b|\bgpus?\b|\bvram\b|\bcosts?\b|\bbudget\b|\bbill\b"
        r"|\bcheap(?:er)?\b|\bexpensive\b|\bpricing\b",
    ),
    "question_with_premise": (r"\?",),
}


def _band(score: float) -> str:
    if score >= 88:
        return "post-ready"
    if score >= 78:
        return "strong"
    if score >= 68:
        return "workable"
    return "below-bar"


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _tokens(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9']{3,}", text.lower())}


def _format_fit(text: str, action: str, thread_hook: bool = False) -> float:
    length = len(text)
    low, high = LENGTH_SWEET_SPOTS.get(action, (60, 260))
    if length <= 0:
        return 0.0
    if length < low:
        fit = length / low
    elif length <= high:
        fit = 1.0
    else:
        fit = _clamp(1.0 - (length - high) / (2.0 * high))
    # One idea per post: hard thread markers and wall-of-text both read as the
    # wrong format. A user-REQUESTED thread's hook post is exempt from the
    # marker penalty (its parts are separate posts, not a fake inline thread).
    if not thread_hook and re.search(r"(^|\s)\d+/\d*(\s|$)|🧵", text):
        fit *= 0.3
    if text.count("\n\n") > 3:
        fit *= 0.7
    return fit


# Ordinary English, excluded so the vocabulary describes his BEAT rather than
# the language it is written in. Without this, "He scored twice in the second
# half and the crowd went home happy" matched on and/the/second and scored
# nearly half marks for topical fit.
_GRAMMAR = frozenset("""
about above after again against all also and any are because been before being
below between both but can could did does doing down during each few for from
further had has have having her here hers him his how into its itself just like
more most much must myself nor not now off once only other our ours out over
own same she should some such than that the their theirs them then there these
they this those through too under until very was were what when where which
while who whom why will with you your yours yourself really actually still even
get got going make makes made take takes want wants think thinks know knows
say says see sees look looks feel feels lot lots thing things way ways
""".split())

_VOCAB_CACHE: dict[str, Any] = {"at": 0.0, "tokens": frozenset()}


def _account_vocabulary() -> frozenset[str]:
    """What this account is actually about, learned from its own posts.

    `content_pillars` is empty on this machine and stays empty until enough
    sync has happened, so a standalone post — which has no context_text either
    — scored ZERO on a 12-point term. Every original post was losing 12 points
    out of 100 for a reason that had nothing to do with the post, and posts
    ranked systematically below replies as a result.

    Used for SCORING only. It is never handed to the writer: showing the model
    a list of the account's topics is the entity-injection bug, and that stays
    dead.
    """
    now = time.monotonic()
    if now - _VOCAB_CACHE["at"] < 600 and _VOCAB_CACHE["tokens"]:
        return _VOCAB_CACHE["tokens"]
    tokens: set[str] = set()
    try:
        from .subject import _SUBJECT_ALIASES   # local: avoids a cycle
        counts: dict[str, int] = {}
        for row in performance_records():
            for token in _tokens(str(row.get("text") or "")):
                # ENTITY NAMES ARE EXCLUDED ON PURPOSE. With them in, "is this
                # topic his?" was answered by "does it name a company he has
                # already written about" — and 55% of his entity mentions are a
                # single company, so nothing new could ever score as relevant.
                # That is the circularity: the account could only be told to
                # write more of what it had already written. What stays is the
                # FIELD vocabulary — pricing, benchmarks, latency, context —
                # which a lab he has never mentioned satisfies just as well.
                if token not in _GRAMMAR and token not in _SUBJECT_ALIASES:
                    counts[token] = counts.get(token, 0) + 1
        # Words that recur across his posts describe the beat; one-offs do not.
        tokens = {token for token, count in counts.items() if count >= 2}
    except Exception:
        pass
    _VOCAB_CACHE.update(at=now, tokens=frozenset(tokens))
    return _VOCAB_CACHE["tokens"]


def _topic_fit(text: str, context_text: str, pillars: list[str]) -> float:
    draft_tokens = _tokens(text)
    if not draft_tokens:
        return 0.0
    signals = 0.0
    if context_text:
        context_tokens = _tokens(context_text)
        if context_tokens:
            signals = max(signals, len(draft_tokens & context_tokens) / max(4, min(len(context_tokens), 24)))
    if pillars:
        pillar_tokens = _tokens(" ".join(pillars))
        if pillar_tokens:
            signals = max(signals, len(draft_tokens & pillar_tokens) / max(2.0, len(pillar_tokens) / 3.0))
    if signals == 0.0 and not context_text and not pillars:
        # Nothing else to measure against: is this even about his subject?
        vocabulary = _account_vocabulary()
        if vocabulary:
            overlap = len(draft_tokens & vocabulary)
            signals = min(1.0, overlap / max(3.0, min(len(draft_tokens), 12)))
    return _clamp(signals * 1.6)


def _angle_execution(text: str, angle_id: str | None) -> float:
    if not angle_id:
        return 0.6  # neutral when no angle was assigned (enhance mode)
    signals = ANGLE_SIGNALS.get(angle_id, ())
    lowered = text.lower()
    hits = sum(1 for signal in signals if re.search(signal, lowered))
    return _clamp(0.35 + hits * 0.35)


def _hook_strength(text: str) -> float:
    first_line = text.strip().splitlines()[0] if text.strip() else ""
    lowered = first_line.lower()
    if not first_line:
        return 0.0
    score = 0.5
    if any(lowered.startswith(opener) for opener in THROAT_CLEARERS):
        score -= 0.35
    first_words = lowered.split()[:8]
    has_number = any(any(char.isdigit() for char in word) for word in first_words)
    has_entity = any(word[0].isupper() for word in first_line.split()[:4] if word and word[0].isalpha())
    if has_number:
        score += 0.2  # concrete number early
    if has_entity:
        score += 0.1  # named subject early
    if has_number and has_entity:
        score += 0.05  # entity + number is the strongest specific open
    if any(token in lowered for token in ("but ", "except", "the real", "nobody", "everyone is", "stop ")):
        score += 0.15  # tension/contrast
    # A question that carries its own premise is a reply magnet — but a bare
    # "thoughts?" style tag reads as fishing.
    if "?" in first_line:
        if len(first_line) >= 30 and not any(lowered.endswith(tag) for tag in ("thoughts?", "agree?", "right?")):
            score += 0.1
        elif len(first_line) < 20:
            score -= 0.1
    if re.match(r"^\s*https?://", first_line):
        score -= 0.3  # a link is the weakest possible first impression
    if len(first_line) > 160:
        score -= 0.15  # buried lead
    return _clamp(score)


# Soft engagement bait: passes the hard safety gate, but the public release
# models negative actions (not interested / mute / block / report) SEPARATELY
# from the absence of a positive one, so bait carries a real cost. The size of
# that cost is not published. Penalized, not rejected.
SOFT_BAIT_PATTERNS = ("like if", "rt if", "repost if", "retweet if", "follow for", "follow me",
                      "comment below", "tag someone", "who else", "let that sink in",
                      "read that again", "bookmark this")

# Language that ASSERTS significance instead of showing it. Distinct from soft
# bait: it asks for nothing, it just says nothing. Measured against this
# account's own archive — zero hits across every real post, at least one in
# every generated draft the writer rejected as slop ("It's timely, not a trend.
# Don't miss the rise.").
EMPTY_HYPE_PATTERNS = (
    "not just a", "isn't just a", "is not just a", "a new way to think",
    "changing everything", "changes everything", "the future is here",
    "don't get left behind", "dont get left behind", "don't miss the",
    "big things are coming", "the momentum building", "momentum is building",
    "game changer", "game-changer", "next level", "changing the game",
    "the rise of", "don't miss out", "welcome to the era", "a new era",
    "this is huge", "mark my words", "buckle up", "the possibilities are endless",
)

# A checkable thing: a product or company name, a version, a date, a figure.
# The account's real posts average 5.6 of these; its slop averages 0.4.
# Deliberately STRICT: sentence-initial capitals are skipped and a name must be
# 3+ characters. That undercounts — "Kimi K3 went from..." loses its opening
# entity — but the undercount is what makes the signal work. Loosening it (drop
# the line-start exclusion, allow 2-character names, exempt digit-bearing
# tokens) was measured on 2026-07-22 and made things WORSE across the board:
# real-vs-slop separation fell 6.57 -> 4.3 and audience_payoff maxed at 1.00 for
# filler AND real posts alike, because slop names entities too. Do not "fix"
# this regex without re-running tools/output_quality_gate.py.
_ANCHOR_PROPER = re.compile(r"(?<![.!?]\s)(?<!^)\b[A-Z][A-Za-z0-9.\-]{2,}\b", re.M)
_ANCHOR_NUM = re.compile(r"\b\d[\d.,]*(?:[KMB%x]|\s?(?:million|billion))?\b")
_ANCHOR_MONTH = re.compile(
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\b")


def _anchors(text: str) -> set[str]:
    body = (text or "").strip()
    return (set(_ANCHOR_PROPER.findall(body)) | set(_ANCHOR_NUM.findall(body))
            | set(_ANCHOR_MONTH.findall(body)))


def _anchor_density(text: str) -> float:
    """Concrete anchors per 100 words, normalised to 0..1.

    The archive's real posts land at ~10.7 per 100 words and its slop at ~1.5,
    so 8.0 is a fair full-marks bar: evidence-dense drafts reach it, and the
    account's most-researched post (21 anchors) is no longer punished for it.
    """
    words = len((text or "").split())
    if not words:
        return 0.0
    return _clamp((len(_anchors(text)) / words * 100.0) / 8.0)


def empty_hype_hits(text: str) -> list[str]:
    lowered = (text or "").lower()
    return [phrase for phrase in EMPTY_HYPE_PATTERNS if phrase in lowered]


def _source_mirroring(text: str, context_text: str) -> float:
    """How much of the draft is the SOURCE's own sentences, said again.

    Whole-text overlap misses clause-by-clause mirroring: a quote can recycle
    only 35% of the post's words and still make exactly the same two points in
    the same order. So this scores each draft sentence by how much of it is
    contained in its closest source sentence, and averages. Verbatim scores
    1.0; a genuinely distinct point on the same topic scores under 0.15.
    """
    from .text_plan import _sentences, _tokens as _plan_tokens  # local: cycles

    draft_sentences = [s for s in _sentences(text or "")
                       if len(_plan_tokens(s)) >= 4]
    source_sentences = [s for s in _sentences(context_text or "")
                        if len(_plan_tokens(s)) >= 4]
    if not draft_sentences or not source_sentences:
        return 0.0
    containments = []
    for sentence in draft_sentences:
        tokens = _plan_tokens(sentence)
        containments.append(max(len(tokens & _plan_tokens(source)) / len(tokens)
                                for source in source_sentences))
    return sum(containments) / len(containments)


def _penalties(text: str, context_text: str, banned_phrases: list[str],
               deleted_texts: list[str], grounded_text: str = "") -> tuple[float, dict[str, float]]:
    lowered = text.lower()
    detail: dict[str, float] = {}
    total = 0.0
    bait_hits = [phrase for phrase in SOFT_BAIT_PATTERNS if phrase in lowered]
    if bait_hits:
        detail["soft_engagement_bait"] = -6.0 * min(2, len(bait_hits))
        total += detail["soft_engagement_bait"]
    # Asserting significance is not the same as having any. Sized between soft
    # bait and a banned phrase: one slip is a real cost, two is disqualifying.
    hype_hits = empty_hype_hits(text)
    if hype_hits:
        detail["empty_hype"] = -7.0 * min(2, len(hype_hits))
        total += detail["empty_hype"]
    banned_hits = [phrase for phrase in banned_phrases if phrase.lower() in lowered]
    if banned_hits:
        detail["banned_phrases"] = -8.0 * len(banned_hits)
        total += detail["banned_phrases"]
    # THE EVIDENCE COUNTS AS GROUNDING, and it was not being passed in.
    #
    # This compared the draft's figures against `context_text` alone. For a
    # from-scratch post that is the topic grounding LINE, while the actual
    # captured evidence lives in `request.direction` — so every correctly
    # grounded figure in a created post looked invented and took a flat -10.
    # A draft that quoted the evidence faithfully scored ten points below one
    # that mentioned no numbers at all, which is backwards: selection was
    # actively steering away from the specificity the whole pipeline exists to
    # produce.
    #
    # `grounded_text` is a SEPARATE argument on purpose. Folding the evidence
    # into `context_text` would feed it to `_topic_fit` and `_source_mirroring`
    # too, inflating topic fit and making a well-grounded post look like a
    # restatement of its source.
    #
    # Both sides are normalised the same way, so "$1,250" in the evidence vouches
    # for "1250" in the draft. Comparing raw tokens meant a figure that WAS in the
    # evidence still counted as invented whenever the punctuation differed.
    def _num(token: str) -> str:
        return token.strip("$%.,").replace(",", "")

    numbers = {_num(n) for n in re.findall(r"\$?\d[\d,.]*%?", text)}
    grounded = {_num(n) for n in re.findall(r"\$?\d[\d,.]*%?",
                                           f"{context_text or ''} {grounded_text or ''}")}
    invented = {number for number in numbers if number not in grounded and len(number) >= 3}
    if invented:
        detail["unverifiable_precision"] = -10.0
        total += detail["unverifiable_precision"]
    # A quote or reply whose sentences are the source's sentences again is the
    # one thing a quote must never be — the account spends its own distribution
    # to say what the reader just read. Sized like near_deleted_duplicate: this
    # should lose to any candidate that actually adds something.
    mirroring = _source_mirroring(text, context_text or "")
    if mirroring >= 0.5:
        detail["restates_the_source"] = -12.0
        total += detail["restates_the_source"]
    elif mirroring >= 0.3:
        detail["restates_the_source"] = -6.0
        total += detail["restates_the_source"]

    draft_tokens = _tokens(text)
    for deleted in deleted_texts:
        deleted_tokens = _tokens(deleted)
        if not deleted_tokens or not draft_tokens:
            continue
        jaccard = len(draft_tokens & deleted_tokens) / len(draft_tokens | deleted_tokens)
        if jaccard > 0.8:
            detail["near_deleted_duplicate"] = -12.0
            total += detail["near_deleted_duplicate"]
            break
    return total, detail


def _isotonic_adjust(score: float) -> float:
    """Apply the offline-fitted monotone calibration if one exists."""
    state = get_job_state("scorer_isotonic")
    xs, ys = state.get("x"), state.get("y")
    if not (isinstance(xs, list) and isinstance(ys, list) and len(xs) == len(ys) and len(xs) >= 2):
        return score
    if score <= xs[0]:
        probability = ys[0]
    elif score >= xs[-1]:
        probability = ys[-1]
    else:
        probability = ys[-1]
        for index in range(1, len(xs)):
            if score <= xs[index]:
                span = xs[index] - xs[index - 1] or 1.0
                fraction = (score - xs[index - 1]) / span
                probability = ys[index - 1] + fraction * (ys[index] - ys[index - 1])
                break
    # Blend: calibration re-scales confidence without re-ordering candidates.
    return round(0.7 * score + 0.3 * (probability * 100.0), 2)


def score_candidate(text: str, *, action: str, objective: str, context_text: str = "",
                    pillars: list[str] | None = None, angle_id: str | None = None,
                    voice_similarity: float | None = None, novelty: float | None = None,
                    fatigue: float | None = None, predicted_percentile: float | None = None,
                    deleted_texts: list[str] | None = None,
                    thread_hook: bool = False,
                    grounded_text: str = "") -> tuple[float, dict[str, float]]:
    """Return (growth_score 0-100, per-term breakdown)."""
    strategy = get_strategy()
    emphasis = OBJECTIVE_EMPHASIS.get((action, objective), {})
    topic_fit = _topic_fit(text, context_text, pillars or [])
    # Personal topic memory: a draft landing in a theme with measured history
    # shifts topic_fit toward that theme's real median (±0.15 max, honest and
    # symmetric — proven-weak themes cost as much as proven-strong ones earn).
    try:
        from .account_model import topic_performance_for
        theme = topic_performance_for(text)
        if theme and theme.get("measured", 0) >= 3:
            topic_fit = _clamp(topic_fit + (float(theme["median_percentile"]) - 0.5) * 0.3)
    except Exception:
        pass
    alignment_fraction, _alignment_notes = algorithm_alignment(text, action)
    fractions = {
        "format_fit": _format_fit(text, action, thread_hook),
        "topic_fit": topic_fit,
        "angle_execution": _angle_execution(text, angle_id),
        "voice_match": _clamp(voice_similarity) if voice_similarity is not None else 0.55,
        "hook_strength": _hook_strength(text),
        "novelty": _clamp((novelty if novelty is not None else 0.6) - (fatigue or 0.0) * 0.5),
        "algorithm_alignment": alignment_fraction,
        "objective_bonus": 0.6 + (0.4 if emphasis else 0.0),
        **_spec_terms(text, action, context_text),
    }
    terms: dict[str, float] = {}
    maxima = dict(TERM_MAXIMA)
    if predicted_percentile is None:
        # The honest ≥30-sample gate failed: redistribute the weight instead of
        # inventing a forecast, and say so in the breakdown.
        redistribution = maxima.pop("predicted_engagement")
        maxima["voice_match"] += redistribution * 0.5
        maxima["novelty"] += redistribution * 0.5
        terms["predicted_engagement_note"] = 0.0
    else:
        fractions["predicted_engagement"] = _clamp(predicted_percentile)
    total = 0.0
    for term, maximum in maxima.items():
        fraction = _clamp(fractions.get(term, 0.0) * emphasis.get(term, 1.0))
        value = round(fraction * maximum, 2)
        terms[term] = value
        total += value
    penalty_total, penalty_detail = _penalties(text, context_text, strategy.banned_phrases,
                                               deleted_texts or [], grounded_text)
    terms.update(penalty_detail)
    total = max(0.0, min(100.0, total + penalty_total))
    total = _isotonic_adjust(round(total, 2))
    return total, terms


def _spec_terms(text: str, action: str, context_text: str) -> dict[str, float]:
    """The six §8.4 terms the original rubric did not measure.

    Deliberately cheap and deterministic — they are rubric terms, not another
    model call, and each maps to a check the spec names explicitly.
    """
    from .text_plan import _sentences, _tokens, _LINK_RE, hard_checks  # local: cycles

    body = (text or "").strip()
    if not body:
        return {name: 0.0 for name in (
            "immediate_orientation", "audience_payoff", "native_payoff",
            "primary_action_execution", "profile_conversion_coherence",
            "factual_integrity")}

    sentences = _sentences(body)
    first = sentences[0] if sentences else ""
    lowered = body.lower()

    # §8.3h/§5.4a: can a new reader identify the point without the parent?
    orientation = 1.0
    if len(first) < 25:
        orientation -= 0.5
    if re.match(r"^\s*(this|that|it|they)\b", first, re.IGNORECASE):
        orientation -= 0.4
    if any(lowered.startswith(phrase) for phrase in THROAT_CLEARERS):
        orientation -= 0.3

    # Does the reader get something the source did not already give them?
    #
    # This used to be an ABSOLUTE count — len(new_tokens) / 12.0 — so a draft
    # that merely paraphrased the post it quoted scored full marks purely by
    # being long enough to accumulate twelve incidental new words. A real quote
    # that restated its own source measured 0.92 here. Two things are needed:
    # what SHARE of the draft is new, and whether its sentences mirror the
    # source's sentences one-for-one (which a whole-text ratio cannot see).
    new_tokens = _tokens(body) - _tokens(context_text)
    body_tokens = _tokens(body)
    if context_text and body_tokens:
        new_share = len(new_tokens) / len(body_tokens)
        payoff = _clamp(new_share * (1.0 - _source_mirroring(body, context_text)))
    else:
        # A standalone post has no source to be new RELATIVE TO, so the old
        # len(new_tokens)/12.0 collapsed into "is it at least twelve words" —
        # and handed full marks to content-free hype. The question the term
        # actually asks ("does the reader get something?") is answered for a
        # post by how much checkable substance it carries.
        payoff = _anchor_density(body)

    # Value without clicking, watching or following.
    without_links = _LINK_RE.sub("", body).strip()
    native = _clamp(len(without_links) / 80.0) if _LINK_RE.search(body) else 1.0

    # One honest primary action, not a demand for every metric at once.
    asks = sum(1 for phrase in SOFT_BAIT_PATTERNS if phrase in lowered)
    action_exec = 1.0 if asks == 0 else max(0.0, 1.0 - 0.5 * asks)
    if action == "reply" and body.rstrip().endswith("?") and len(body) < 60:
        action_exec -= 0.3  # an empty question is not an action design

    # Would a visitor find a coherent account behind this? Proxy: the post
    # stands for something specific rather than being a generic reaction.
    conversion = _clamp(len(new_tokens) / 18.0)

    # Unsupported certainty and invented precision.
    integrity = 1.0
    for phrase in ("guaranteed", "will definitely", "always works", "never fails",
                   "100% of", "proven to"):
        if phrase in lowered:
            integrity -= 0.5
    return {
        "immediate_orientation": _clamp(orientation),
        "audience_payoff": payoff,
        "native_payoff": native,
        "primary_action_execution": _clamp(action_exec),
        "profile_conversion_coherence": conversion,
        "factual_integrity": _clamp(integrity),
    }


def band_for(score: float | None) -> str | None:
    return _band(score) if score is not None else None


def log_scored_candidate(candidate_id: str, generation_id: str, score: float,
                         terms: dict[str, float], objective: str) -> None:
    from .storage import connect, _utc  # local import to avoid cycles
    with connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO scorer_calibration(candidate_id,generation_id,score,terms_json,objective,posted,created_at)
               VALUES(?,?,?,?,?,COALESCE((SELECT posted FROM scorer_calibration WHERE candidate_id=?),0),?)""",
            (candidate_id, generation_id, float(score), json.dumps(terms), objective, candidate_id, _utc()))
