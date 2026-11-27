"""What the PUBLIC X release actually supports, and nothing more (spec §1.5).

This file previously encoded numeric weights from the 2023 `twitter/the-algorithm`
heavy ranker — "a reply is worth ~13x a like", "reports cost ~74x" — and asserted
them as fact inside prompt briefs and user-visible copy. Verified against the
current primary source, that is wrong: `home-mixer/scorers/ranking_scorer.rs`
reads every weight from runtime parameters:

    let favorite = params.get(FavoriteWeight);
    let reply    = params.get(ReplyWeight);

The values are not published. Neither is the final VM ranker's behaviour. So the
widely repeated "one reply outweighs N likes" claim has no public basis, and this
module must never state one.

What the release DOES establish, and what Starling may therefore use:

  * a per-viewer pipeline, not one global "viral score" per post;
  * a multi-action model — many positive actions AND explicit negative ones;
  * author-diversity attenuation and an out-of-network adjustment;
  * conversation de-duplication that keeps one candidate per conversation;
  * reply status as a distinct candidate feature with its own service path.

Everything numeric below is a STARLING PRIOR — an explainable creator-side
triage weight, taken from the spec, that the learning loop may retune from local
outcomes. None of it is an X weight, and nothing here may be displayed as an
"X score".
"""
from __future__ import annotations

import re

# --------------------------------------------------------------------------- #
# The public pipeline (§1.5). Stages, in order, with what each implies for us.
# --------------------------------------------------------------------------- #
PIPELINE_STAGES: tuple[tuple[str, str], ...] = (
    ("query_hydration",
     "X assembles the viewer's history, follows, social graph and served/seen "
     "state. A post needs a credible audience whose prior interests make it "
     "relevant — not merely a large one."),
    ("candidate_retrieval",
     "In-network candidates come from followed accounts; out-of-network ones "
     "are retrieved from a broader corpus. Consider adjacent communities, not "
     "only large accounts."),
    ("candidate_hydration",
     "Posts are enriched with quote context, language, media, topics and "
     "safety metadata. Collect enough context before recommending anything."),
    ("pre_ranking_filters",
     "Duplicates, old posts, already-seen/served posts, blocked/muted authors "
     "and muted keywords are removed. Reject stale, duplicate or already-used "
     "ideas before the Writer ever sees them."),
    ("phoenix_ranking",
     "A transformer scores viewer history against the candidate post, author, "
     "surface and post age, predicting many possible actions. Write for a real "
     "audience and action hypothesis, not a generic 'high engagement' target."),
    ("weighted_scoring",
     "Predicted action probabilities are combined with runtime-configured "
     "weights, normalised, then adjusted for author diversity and "
     "out-of-network status. The 2026 release publishes DIRECTIONAL baselines "
     "(production values still float): the author engaging back with a reply "
     "is by far the heaviest positive outcome — an order of magnitude above a "
     "plain reply, which itself far outweighs a like; reports and mute/block/"
     "'show less' are catastrophic negatives that outweigh mountains of mild "
     "positives."),
    ("selection_and_filtering",
     "Top candidates are selected, then visibility, brand-safety and "
     "conversation de-duplication run. A strong draft still has to pass "
     "safety, originality and conversation-quality gates."),
)

# The action set the public scorer combines. Order carries no weight claim —
# these are simply the actions the release shows being predicted separately.
POSITIVE_ACTIONS: tuple[str, ...] = (
    "favorite", "reply", "retweet", "photo_expand", "click", "profile_click",
    "video_quality_view", "share", "share_via_dm", "share_via_copy_link",
    "dwell", "quote", "quoted_click", "quoted_video_quality_view",
    "follow_author",
)

NEGATIVE_ACTIONS: tuple[str, ...] = (
    "not_interested", "block_author", "mute_author", "report", "not_dwelled",
)

# Mechanisms the release shows, with no numbers attached.
MECHANISMS: dict[str, str] = {
    "author_diversity": (
        "Repeated candidates from the same author are attenuated within one "
        "feed response. Mechanically targeting one account is self-defeating; "
        "a genuine recurring relationship is not."),
    "out_of_network_adjustment": (
        "Out-of-network candidates receive a separate adjustment. Reaching a "
        "new audience is possible but is not owed to anyone."),
    "conversation_dedup": (
        "The released filter keeps the highest-scoring candidate per "
        "conversation. Prefer ONE materially different contribution per root; "
        "follow-up clutter competes with itself."),
    "reply_surface": (
        "Reply status is an explicit candidate feature and the home path calls "
        "a reply service. Replies are a distinct surface needing thread context "
        "and a unique contribution — the service's formula is not public."),
    "normalisation": (
        "Raw weighted scores are normalised before adjustment, so a bigger raw "
        "number is not directly a better position."),
    # Mechanisms confirmed by the 2026 xai-org/x-algorithm release and its
    # public documentation — qualitative, no invented numbers.
    "recency_decay": (
        "Visibility decays fast — roughly halving within hours. The first ~30 "
        "minutes of a post's life decide most of its distribution, and early "
        "reply velocity triggers the cascade. Being early beats being clever."),
    "two_way_conversation": (
        "A reply chain where the author engages back is the single most "
        "valuable outcome the ranker rewards. Write replies the AUTHOR wants "
        "to answer."),
    "external_links": (
        "External links are heavily deprioritised (escalated again in 2026); "
        "the native text must carry the whole payoff. Links belong in a "
        "follow-up reply, never in the post that has to travel."),
    "content_classification": (
        "The 2026 pipeline classifies content with an LLM: constructive "
        "framing gets wider reach; pure negative dunking gets narrowed. Make "
        "the point additive — keep the edge, drop the sneer."),
}

# What Starling can never know. Stated, not implied (§5.3b, §6.8).
VISIBILITY_BOUNDARY = (
    "Starling has no access to individual viewer histories, X's live "
    "candidate pool, runtime ranking weights, experiments, or final "
    "distribution decisions."
)

# --------------------------------------------------------------------------- #
# The four gates (§1.5) — each must pass; a clever draft cannot rescue an
# upstream failure.
# --------------------------------------------------------------------------- #
GATES: tuple[tuple[str, str], ...] = (
    ("eligibility", "Safe, original, current enough and policy-compliant."),
    ("retrieval_fit", "Which real people are most likely to care."),
    ("action_value", "Why they would act once they see it."),
    ("conversion_value", "Whether the profile makes new attention worth keeping."),
)

# --------------------------------------------------------------------------- #
# Starling priors (§1.5). Explainable creator-side triage weights from the
# spec — NOT reverse-engineered X weights. The learning loop may retune these
# from enough comparable local outcomes, keeping a baseline prior.
# --------------------------------------------------------------------------- #
CREATOR_OPPORTUNITY_WEIGHTS: dict[str, float] = {
    "audience_retrieval_fit": 0.24,
    "distinct_useful_angle": 0.20,
    "multi_action_value": 0.16,
    "timeliness": 0.14,
    "clarity_and_dwell": 0.10,
    "relationship_fit": 0.08,
    "profile_conversion_fit": 0.08,
}

CREATOR_OPPORTUNITY_PENALTIES: tuple[str, ...] = (
    "repetition", "saturation", "factual_uncertainty",
    "negative_action_risk", "cadence_cost",
)

# The four audiences every opportunity must be able to name (§1.5). "Everyone
# interested in AI" is not an audience and fails retrieval fit.
AUDIENCE_TYPES: tuple[tuple[str, str], ...] = (
    ("core", "People who already engage with the account's demonstrated pillars."),
    ("adjacent", "People around a related account, topic or problem who would "
                 "still understand the post without prior familiarity."),
    ("action", "The subgroup most likely to share, reply or quote because the "
               "point serves their identity or work."),
    ("conversion", "Those most likely to find the profile coherent and follow."),
)

# Nine reasons a reader might pass a post on (§1.5). Controlled experiments, not
# templates: the selected archetype is recorded and tested against history.
ARCHETYPES: tuple[tuple[str, str, str], ...] = (
    ("new_evidence", "An original test, observation, benchmark or result.", "share"),
    ("useful_distinction", "Separates two ideas people wrongly treat as one.", "share"),
    ("practical_implication", "What a new event changes for a specific audience.", "share"),
    ("defensible_thesis", "A concise position with a reason, not contrarianism.", "quote"),
    ("falsifiable_prediction", "What may happen and what would prove it wrong.", "reply"),
    ("before_after", "Makes a change legible through a clear contrast.", "share"),
    ("decision_framework", "A compact way to choose or evaluate something.", "share"),
    ("recognisable_observation", "Names a real friction the audience recognises.", "favorite"),
    ("constructive_disagreement", "Disagrees with an idea using evidence.", "reply"),
)

# Tactics X's own policies prohibit (§1.5). Hard blocks, not growth experiments.
PROHIBITED_TACTICS: tuple[str, ...] = (
    "duplicate or near-duplicate replies",
    "unrelated trend or hashtag insertion",
    "engagement bait, paid engagement, or engagement swaps",
    "mass mentions or automated engagement",
    "deceptive edits, fabricated media, or invented outcomes",
    "repeatedly reposting and deleting the same idea",
    "stuffing a post with links, hashtags, or all-caps",
)


# --------------------------------------------------------------------------- #
# Prompt briefs. Same API as before so callers do not change.
# --------------------------------------------------------------------------- #
_BRIEFS: dict[str, str] = {
    "create_post": (
        "How X actually distributes this (from the public release): posts are "
        "ranked PER VIEWER against that person's history, and many separate "
        "actions are predicted — reading, liking, replying, reposting, quoting, "
        "sharing, dwelling, opening the profile, following — alongside explicit "
        "negative ones (not interested, mute, block, report). The live weights "
        "are not published, so there is no magic phrasing. Write for one named "
        "audience with one honest primary action, give a real payoff in the "
        "post itself, and remove anything that invites a negative reaction."),
    "reply": (
        "Replies are a distinct surface with their own ranking path, and X keeps "
        "only the best candidate per conversation. So: one contribution per "
        "conversation, and it must add a fact, distinction, implication, "
        "counterexample or precise question the thread does not already have. "
        "Agreement and recap add nothing. Repeatedly targeting one account is "
        "attenuated by author diversity."),
    "timing": (
        "Post age is an input to ranking and conversations move, so a "
        "contribution can arrive too late to be useful. That is a reason to act "
        "while a conversation is live — not evidence of a magic posting hour."),
    "evaluate": (
        "Judge a post against the account's own comparable history, never raw "
        "totals from larger accounts. Many actions matter, not likes alone, and "
        "a negative reaction is modelled separately from the absence of a "
        "positive one."),
}


def algorithm_brief(purpose: str, budget: int = 700) -> str:
    """A compact, honest brief for a prompt. Never states a weight."""
    brief = _BRIEFS.get(purpose) or _BRIEFS["create_post"]
    if len(brief) <= budget:
        return brief
    return brief[:budget].rsplit(" ", 1)[0] + "…"


def pipeline_brief(budget: int = 700) -> str:
    """The staged pipeline, for explaining a recommendation."""
    lines = [f"{name}: {note}" for name, note in PIPELINE_STAGES]
    text = " | ".join(lines)
    return text if len(text) <= budget else text[:budget].rsplit(" ", 1)[0] + "…"


# --------------------------------------------------------------------------- #
# Creator-side alignment of one draft. Unchanged signature; the checks are now
# about reader value and negative-action risk rather than imagined weights.
# --------------------------------------------------------------------------- #
_SOFT_BAIT = (
    "like if", "retweet if", "rt if", "follow for", "follow me", "share if",
    "who agrees", "am i the only one", "drop a", "comment below", "tag someone",
)
_QUESTION_PREMISE_MIN_CHARS = 30
_GENERIC_QUESTIONS = ("thoughts?", "agree?", "right?", "no?", "yes?", "thoughts ?")
_HASHTAG_RE = re.compile(r"(?:^|\s)#\w+")
_LINK_RE = re.compile(r"https?://\S+")


def algorithm_alignment(text: str, action: str) -> tuple[float, list[str]]:
    """0..1 creator-side alignment for a draft, with the reasons.

    NOT an X score and never displayable as one (§6.1a). It asks only whether
    the draft is the kind of candidate that can earn a natural action without
    inviting a negative one.
    """
    body = (text or "").strip()
    if not body:
        return 0.0, ["empty draft"]

    notes: list[str] = []
    score = 1.0
    lowered = body.lower()

    # Negative-action risk: the release models these separately, so bait is a
    # cost rather than a neutral tactic.
    if any(phrase in lowered for phrase in _SOFT_BAIT):
        score -= 0.45
        notes.append("engagement bait invites negative actions")

    # A question with no premise is the classic empty reply-farm (§8.3e).
    stripped = lowered.rstrip()
    if any(stripped.endswith(question) for question in _GENERIC_QUESTIONS):
        premise = len(body) - len(body.rstrip().split()[-1])
        if premise < _QUESTION_PREMISE_MIN_CHARS:
            score -= 0.30
            notes.append("question without a premise")

    # Native payoff: a post whose value sits behind a link gives the reader
    # nothing in the feed itself.
    links = _LINK_RE.findall(body)
    without_links = _LINK_RE.sub("", body).strip()
    if links and len(without_links) < 40:
        score -= 0.30
        notes.append("the payoff is behind a link")
    elif links and _LINK_RE.match(body.lstrip()):
        # Leading with the URL means the reader meets a link before the idea.
        # §8.3g: the native takeaway comes BEFORE the asset.
        score -= 0.20
        notes.append("leads with a link instead of the idea")

    # X's own organic guidance: hashtags in ordinary copy, and all caps.
    hashtags = _HASHTAG_RE.findall(body)
    if hashtags:
        score -= 0.10 * min(len(hashtags), 3)
        notes.append("hashtags in ordinary post copy")
    letters = [char for char in body if char.isalpha()]
    if len(letters) >= 20 and sum(char.isupper() for char in letters) / len(letters) > 0.6:
        score -= 0.15
        notes.append("all caps")

    # Scannability: one unbroken wall is poor dwell design.
    if len(body) > 220 and "\n" not in body and body.count(". ") <= 1:
        score -= 0.10
        notes.append("one long unbroken block")

    # A reply that only agrees is exactly what conversation de-duplication drops.
    if action == "reply" and len(body) < 60 and not any(
            char.isdigit() for char in body):
        agreement = ("agree", "this", "exactly", "so true", "well said", "100%")
        if any(word in lowered for word in agreement):
            score -= 0.35
            notes.append("agreement filler adds nothing to the conversation")

    return max(0.0, min(1.0, score)), notes


__all__ = [
    "PIPELINE_STAGES", "POSITIVE_ACTIONS", "NEGATIVE_ACTIONS", "MECHANISMS",
    "VISIBILITY_BOUNDARY", "GATES", "CREATOR_OPPORTUNITY_WEIGHTS",
    "CREATOR_OPPORTUNITY_PENALTIES", "AUDIENCE_TYPES", "ARCHETYPES",
    "PROHIBITED_TACTICS", "algorithm_brief", "pipeline_brief",
    "algorithm_alignment",
]
