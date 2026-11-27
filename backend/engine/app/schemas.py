from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, field_validator

# Limits for text that somebody other than the customer wrote (a post, a thread, a profile) and for
# the free-form dictionaries that travel with it. A cap on a LIST bounds how many items it holds and
# says nothing about how long each one is: `thread_text` allowed 60 items of any length, which is how
# 100 KB of hostile text reached the safety scan. Every limit is generous against the real thing
# (X's longest post is 25,000 characters) and finite, which is the point.
IdText = Annotated[str, StringConstraints(max_length=200)]
UrlText = Annotated[str, StringConstraints(max_length=2_000)]
PostText = Annotated[str, StringConstraints(max_length=30_000)]
ShortText = Annotated[str, StringConstraints(max_length=300)]
MAX_FREE_FORM_BYTES = 256_000


def _bounded_free_form(value: Any) -> Any:
    """A free-form dictionary may hold anything, up to a size. Measured as the JSON it arrived as."""
    if value is not None and len(json.dumps(value, default=str, separators=(",", ":"))) > MAX_FREE_FORM_BYTES:
        raise ValueError(f"must be at most {MAX_FREE_FORM_BYTES} bytes as JSON")
    return value


FreeForm = Annotated[dict[str, Any], AfterValidator(_bounded_free_form)]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class MediaItem(BaseModel):
    url: UrlText
    kind: Literal["image", "video", "gif", "link_card"]
    alt: str | None = Field(default=None, max_length=2_000)
    source_url: UrlText | None = None
    rights_status: Literal["owned", "licensed", "unknown", "needs_approval"] = "unknown"

    @field_validator("alt", mode="before")
    @classmethod
    def _drop_invisible_characters(cls, value):
        """`alt` is attacker-authored and reaches a model PostContext cannot see.

        `PostContext`'s validator is defined on PostContext, so it cannot reach
        a field on a nested model — and `analyzer.visible_text` (:567-570) feeds
        `alt` to a DIFFERENT model than the drafting path. The scrub has to be
        declared here or this field is simply outside it.
        """
        if isinstance(value, str):
            from .text_normalizer import strip_invisible
            return strip_invisible(value)
        return value


class PostContext(BaseModel):
    # EVERY field below is filled from the x.com DOM, which means every field is
    # authored by whoever wrote the post — not by the user. They were unbounded,
    # so a crafted post could push arbitrarily large strings through the native
    # host into the model's prompt and into the database. The caps are generous
    # against real posts (X's own limit is 25k characters for long posts) and
    # finite, which is the point. `safety._combined` truncates at 100k for
    # SCANNING, but the untruncated values still reached the prompt.
    context_id: IdText | None = None
    post_id: IdText | None = None
    post_url: UrlText | None = None
    conversation_id: IdText | None = None
    author: str = Field(max_length=200)
    author_handle: str | None = Field(default=None, max_length=120)
    # The extension already reads X's verified badge; the server used to drop it
    # (no field existed), so every author looked anonymous and a first-party
    # announcement from a verified official account read as "thin evidence".
    author_verified: bool = False
    text: str = Field(max_length=30_000)
    # ISO timestamp of the post itself — age and velocity go into every
    # decision (freshness brackets per the strategy manual §3.1).
    post_created_at: ShortText | None = None
    parent_text: str | None = Field(default=None, max_length=30_000)
    parent_author: str | None = Field(default=None, max_length=200)
    parent_post_id: IdText | None = None
    parent_post_url: UrlText | None = None
    quote_text: str | None = Field(default=None, max_length=30_000)
    quoted_post_id: IdText | None = None
    thread_text: list[PostText] | None = Field(default=None, max_length=60)
    links: list[UrlText] = Field(default_factory=list, max_length=40)
    media: list[MediaItem] = Field(default_factory=list, max_length=12)
    poll: FreeForm | None = None
    language: ShortText | None = None
    visibility: Literal["public", "protected", "unknown"] = "unknown"
    sensitive: bool = False
    is_own_post: bool = False
    reply_restricted: bool = False
    # THE EXTENSION ALREADY WORKED THESE OUT AND THE SCHEMA DROPPED THEM.
    # Pydantic ignores unknown keys, so every one of these arrived on the wire
    # and vanished without a word:
    #
    #   post_type      the extension INFERS post/reply/quote from the DOM. Not
    #                  declaring it is why the archive reads 31 posts, 3 replies
    #                  and 0 quotes, and why per-surface voice learning starves.
    #   is_long_post   the post is cut off behind "Show more". Without it the
    #                  engine replies to half a post and never knows.
    #   poll_options   a poll's actual choices; `poll` alone carries no answers.
    #   page_url       whether this was seen on a profile, a thread or the feed.
    #   captured_at    WHEN it was seen, which is what makes "is this still
    #                  live" answerable rather than guessed.
    post_type: ShortText | None = None
    is_long_post: bool = False
    poll_options: list[ShortText] = Field(default_factory=list, max_length=8)
    page_url: UrlText | None = None
    captured_at: ShortText | None = None
    visible_metrics: Annotated[dict[str, float | ShortText | None], AfterValidator(_bounded_free_form)] | None = None
    # Thread visibility from the extension (ConversationState inputs). The Tibo
    # incident: the user's own 15h-old reply was ON SCREEN and Starling drafted
    # a second one because dedup checked only a DB row. What the extension can
    # SEE is evidence, and it beats a stale database both ways.
    thread_scanned: bool = False              # the visible thread was actually read
    visible_own_reply: bool = False           # my reply is visible in this thread
    visible_reply_count: int | None = None    # crowd size (from the reply stat)
    # Ages (minutes) of the visible replies, newest first — the raw material for
    # the reply-decay read ("this thread went quiet 6 hours ago").
    visible_reply_ages_minutes: list[float] = Field(default_factory=list, max_length=30)

    @field_validator("text", "parent_text", "quote_text", "author", "author_handle",
                     "parent_author", "thread_text", "poll_options", mode="before")
    @classmethod
    def _drop_invisible_characters(cls, value):
        """Strip characters the user cannot see before they reach the model.

        One definition, applied at the boundary, so every route that accepts a
        PostContext is covered rather than each one remembering. Zero-width and
        bidi-override characters let a crafted post carry instructions that are
        invisible in the draft the user is about to approve — which defeats the
        review step the whole design leans on.

        NOW ACTUALLY APPLIED EVERYWHERE. The docstring above claimed "every
        route" while the field list named six SCALARS, and the `isinstance(str)`
        guard returned every list through untouched. Two attacker-authored list
        fields were outside it and both are prompt material:

            thread_text   reply_plan.py:308 and :1150, and the input to
                          safety.py:279's own scan — so a crafted thread could
                          carry invisible instructions PAST the safety read
            poll_options  reply_plan.py:315

        A third, `MediaItem.alt`, cannot be reached from here at all and carries
        its own copy of this validator — see the class above.

        LEFT OUT DELIBERATELY: `links` (list[str], also attacker-authored). It
        was not in the enumeration that licensed this fix and I have no evidence
        of where it reaches; adding it here would be an unratified widening of a
        safety patch. It is the obvious next candidate.
        """
        from .text_normalizer import strip_invisible
        if isinstance(value, str):
            return strip_invisible(value)
        if isinstance(value, list):
            return [strip_invisible(item) if isinstance(item, str) else item
                    for item in value]
        return value


class SupportEvidence(BaseModel):
    """What may VOUCH for a claim. Nothing else may ever be added here.

    `intelligence.py`'s scaffold-strip exists for this reason, verbatim from its
    own comment:

        "the dangerous one — the VISION MODEL'S description of an attached
        image. That last is model output, so a hallucinated reading of a
        screenshot became promotable to verified fact: the draft repeats the
        invented number, `_find_support` finds that sentence verbatim in the
        'evidence', and it ships unflagged. Evidence must be the captured facts
        and nothing this process wrote itself."

    OPTION-4 DELETES THAT STRING-STRIPPING GUARD, so the property has to be
    carried by the type instead of by a test: the support path accepts this
    object, which has no `image_note` field and forbids extras, so a future
    "merge them for convenience" is a TypeError at the call site rather than a
    silent promotion of model output to evidence.
    """

    model_config = ConfigDict(extra="forbid")

    facts: str | None = None

    @classmethod
    def of(cls, request: "DraftRequest") -> "SupportEvidence":
        """The ONLY sanctioned way to build one — copies captured facts, nothing else."""
        return cls(facts=getattr(request, "facts", None))


# THE BRIEF'S CAP, SIZED FROM ITS PRODUCER. It was 1200 because `direction` is,
# and that number was never measured against the thing it has to hold. The
# reply plan (`reply_plan.plan_reply`) is 1,094 characters for an ordinary post
# and 1,300 for one that asks a question, before anything is appended; a decided
# point adds about 215 and the recent-openings note about 80, so real plans run
# 1,094 to 1,606. Every value over 1,200 raised a ValidationError while the
# request was being built, the engine answered 500 and the customer was told
# the AI was not responding: every reply to a question, and every reply whose
# planner accepted a point. 4000 clears the measured maximum two and a half
# times over, and `direction.value_copies` clips to it regardless, because a cap
# is acceptable and an exception is not.
WRITING_BRIEF_CAP = 4000


class DraftRequest(BaseModel):
    # Threads are context only. Starling never creates or inserts a thread.
    action: Literal["reply", "quote", "post", "save"]
    context: PostContext
    tone: Literal["natural", "funny", "direct", "thoughtful"] = "natural"
    length: Literal["short", "medium", "long"] = "medium"
    research_mode: Literal["automatic", "off"] = "automatic"
    # VESTIGIAL — nothing reads this. Whether an image is offered is decided by
    # `_visual_offer` from the Analyzer's visual brief and the account's measured
    # image habit, never by this flag. It is set to "no_media" at three call
    # sites, which makes images look deliberately switched off when they are
    # not: images were actually blocked by an Openverse 401 for the whole life
    # of the product. Kept only so previously stored DraftRequest payloads still
    # parse. Do not add logic that reads it; delete it once nothing persists it.
    media_policy: Literal["recommend", "no_media"] = "recommend"
    grok_permission: bool = True
    goal_id: str | None = None
    campaign_id: str | None = None
    audience: str | None = None
    strategy_mode: Literal["balanced", "reach", "conversation", "resonance", "followers"] = "balanced"
    timing_intent: Literal["now", "best_window", "unspecified"] = "unspecified"
    recent_context: list[str] = Field(default_factory=list, max_length=20)
    # HIS WORDS, and after OPTION-4 nothing else. Every machine-generated
    # payload has its own field below, so the 1200 cap stops being a contest
    # five producers append into.
    direction: str | None = Field(default=None, max_length=1200)

    # ---- STRUCTURED PAYLOADS (OPTION-4) ---------------------------------
    #
    # WHY THESE EXIST. One 1200-char string carried five independent payloads
    # and the tail lost: measured on his real captures, 8/8 fresh reply
    # directions were cut MID-WORD at exactly 1200, the fact block was starved
    # on 44% of reply directions, and of 12 media replies only ONE carried both
    # the image caption and the specificity instruction. The caption survived
    # at char 127 by POSITION, not by design — move one block ahead of it and
    # it is the next victim. Fields end the lottery.
    #
    # CAPS ARE MEASURED, NOT CHOSEN. Evidence block: median 335, p90 637,
    # p99 901, MAX 987 across 1,351 stored directions. 1200 truncates none of
    # them and clears the producer's own bound (radar._trim_to_sentence, 700).
    # Note the stored values are already POST-truncation, so this cap is set
    # from the producer side deliberately: sizing a new field from a starved
    # distribution re-creates the starvation.
    #
    # AND A CUT MUST ADMIT ITSELF. `facts_truncated` exists so a silent tail
    # clip can never happen again — a cap is acceptable, an unrecorded cut is
    # the whole disease. It should stay False forever; if it fires, that is a
    # signal about the producer, not noise.
    facts: str | None = Field(default=None, max_length=1200)
    facts_truncated: bool = False
    # NAMED `post_register`, NOT `register`: pydantic warns that `register`
    # shadows an attribute on `BaseModel`. It works today and would break on a
    # version bump, and a field that warns on every import is one somebody
    # "cleans up" later without knowing why it was there.
    #
    # THE POST REGISTER, as a FIELD rather than inferred from `tone`. `tone` is
    # a Literal whose values happen to correlate ("funny" today, on the shitpost
    # path) — and reading a label as if it denoted the thing it labels is how
    # `display_label` ended up being used as a search query. A register is what
    # the writer was asked for; a tone is one consequence of it. None means no
    # register was planned, which is every reply, quote and enhancement.
    post_register: str | None = Field(default=None, max_length=16)
    decided_point: str | None = Field(default=None, max_length=280)
    writing_brief: str | None = Field(default=None, max_length=WRITING_BRIEF_CAP)
    shape_note: str | None = Field(default=None, max_length=300)
    image_note: str | None = Field(default=None, max_length=500)
    #
    # TWO GATES WANT OPPOSITE THINGS FROM `image_note`, and unifying them
    # silently breaks one:
    #
    #   the SUPPORT checker MUST NOT see it — it asks "what may VOUCH for a
    #       claim", and image_note is model output, so letting it vouch lets a
    #       hallucinated screenshot reading promote itself to verified fact.
    #       Enforced by TYPE below (`SupportEvidence`), not by convention.
    #   the ECHO checker MUST see it — it asks "what has the reader ALREADY
    #       READ", and the reader can see the image. A draft that restates the
    #       headline inside a screenshot is a restatement.
    #
    # Same field, opposite correct answers, because the questions differ.
    #
    # UNKNOWN IS NOT EMPTY. A pre-migration row parses with every field None,
    # and None here means "this request predates the fields", never "this
    # request had no facts". Consumers must branch on `is None`, not falsiness.

    # Native composer enhancement is deliberately different from a blank-page
    # draft: if the model cannot produce a safe improvement, keep the user's
    # exact original rather than substituting a generic fallback.
    enhancement_mode: bool = False
    original_draft: str | None = Field(default=None, max_length=25_000)
    # Threads are a deliberate, USER-REQUESTED capability: only when the user
    # explicitly asks for one does generation produce multi-part output. The
    # default remains one idea per post, and nothing ever posts automatically.
    thread_requested: bool = False
    # THE MODEL DECIDES (D1, 2026-09-21). On a ✦ press the first draft call also
    # answers "is there something worth adding here?" (`verdict`, `why`,
    # `confidence` in the same strict object), because keyword lists could not:
    # 21 of 30 ordinary, fresh, repliable posts were refused without asking the
    # model. `decision_brief` is what the call is told about the moment (age,
    # visible metrics, reply activity, the timing prior); deterministic hard
    # stops never reach this call at all.
    decide: bool = False
    decision_brief: str | None = Field(default=None, max_length=2500)
    # API-NATIVE PASS (2026-09-22). What the old reply plan knew and the model cannot see,
    # given as context rather than rules: the writer's own thread, being the host, their
    # earlier reply in this thread, earlier drafts for this post, a fact-check Starling ran.
    # Rendered under the conversation (`llm.conversation_block`).
    conversation_notes: str | None = Field(default=None, max_length=1500)
    # A created post offered several stories (numbered in `facts`) names the one it chose
    # (`llm.topic_choices`); 0 means the post was handed one subject or none.
    topic_choice: int = Field(default=0, ge=0, le=5)
    # A created post for which nothing new was found: the writer writes from their beat and
    # must not claim news (`llm.POST_NO_NEWS_TASK`).
    no_news: bool = False


class ComposerEnhancement(BaseModel):
    action: Literal["reply", "quote", "post"]
    # "create" writes from scratch for an EMPTY composer; "enhance" (the
    # default) rewrites the user's existing draft and requires text.
    draft_text: str = Field(min_length=0, max_length=25_000)
    intent: Literal["create", "enhance"] = "enhance"
    # Base64 (or data: URL) images the user attached to the composer. The
    # extension downscales before sending; the engine reads the first one
    # with the vision model so the text can be consistent with what's shown.
    # Empty is the norm and the whole path is a no-op then.
    images: list[str] = Field(default_factory=list, max_length=4)
    # THE SUBJECT OF A CREATED POST, WHEN THE WRITER NAMED ONE (the website's writing page asks
    # "What's it about?"). Only read for action "post" with intent "create": the post is then
    # written about this subject instead of a story Starling picks from the beat. Empty keeps
    # the extension's Create post exactly as it was. It is the writer's own words, so it travels
    # to the model as what they asked for, never as a fact the post may assert.
    topic: str | None = Field(default=None, max_length=500)


class ScheduleCommand(BaseModel):
    """A hold-and-remind action on a scheduled draft.

    Every op touches only this customer's own engine database. There is no publish
    op and no network call: a "scheduled" draft is held and, when its time comes,
    surfaced back to the user as ready to post. The human always posts it, which is
    why scheduling rides the existing `assist` channel.
    """

    model_config = ConfigDict(extra="forbid")
    # "reschedule" moves an existing hold rather than making a second one —
    # without it, changing your mind about a time silently left two reminders.
    op: Literal["save", "list", "list_due", "mark_posted", "dismiss", "reschedule"]
    draft_id: int | None = None
    text: str | None = Field(default=None, max_length=8000)
    target_at: str | None = Field(default=None, max_length=64)   # ISO-8601 UTC
    target_label: str | None = Field(default=None, max_length=120)
    topic: str | None = Field(default=None, max_length=200)
    freshness: str | None = Field(default=None, max_length=16)
    reason: str | None = Field(default=None, max_length=4000)
    image: dict[str, Any] | None = None
    # A held draft keeps its SURFACE. This used to be hardcoded to "post" on
    # save, so a scheduled reply came back as a standalone post — the draft
    # would have made no sense on its own, addressed to nobody.
    action: Literal["post", "reply", "quote"] = "post"
    # A thread is held whole: parts 2..n travel with part 1 or the reminder
    # resurfaces an opening line with no thread behind it.
    thread_parts: list[str] = Field(default_factory=list, max_length=25)
    # What it is a reply/quote TO, so the reminder can take you back there.
    context_post_id: str | None = Field(default=None, max_length=64)
    context_url: str | None = Field(default=None, max_length=500)


class BrowserAssistRequest(BaseModel):
    """Narrow, text-only contract the extension reaches through the gateway."""

    model_config = ConfigDict(extra="forbid")
    mode: Literal["context", "chat", "enhance_composer", "schedule"]
    context: PostContext | None = None
    message: str | None = Field(default=None, max_length=8000)
    composer: ComposerEnhancement | None = None
    # Dock chat threads one conversation per page session so chat remembers
    # previous turns instead of starting fresh on every message.
    conversation_id: str | None = Field(default=None, max_length=64)
    # Hold-and-remind scheduling command (mode="schedule"). Local-only; never
    # posts. See ScheduleCommand.
    schedule: ScheduleCommand | None = None
    # The user explicitly asked for a SECOND reply in a thread they already
    # replied to ("draft it anyway"). Off by default: dedup blocks; this is the
    # deliberate override, and the draft is then written as a FOLLOW-UP.
    second_reply_ok: bool = False


class Source(BaseModel):
    title: str
    url: str
    published_at: str | None = None
    snippet: str | None = None

    @field_validator("url")
    @classmethod
    def source_url_must_be_http(cls, value: str) -> str:
        if not value.lower().startswith(("https://", "http://")):
            raise ValueError("source URL must use http or https")
        return value


class DraftCandidate(BaseModel):
    id: str
    text: str
    sources: list[Source] = Field(default_factory=list)
    retrieved_examples: list[str] = Field(default_factory=list)
    voice_score: float | None = None
    performance: dict[str, float | str | None]
    rationale: list[str] = Field(default_factory=list)
    grok_suggested: bool = False
    research_status: Literal["used", "unavailable", "not_needed"] = "not_needed"
    strategic_role: str | None = None
    hook: str | None = None
    target_objective: Literal["reach", "conversation", "resonance", "followers"] | None = None
    novelty_score: float | None = None
    fatigue_risk: float | None = None
    recommended_window: dict[str, Any] | None = None
    experiment_id: str | None = None
    safety: dict[str, Any] = Field(default_factory=dict)
    # GrowthScore: strategy-rubric composite used to rank candidates; the band
    # is surfaced to the UI ("post-ready" ≥88, "strong" 78-87, "below-bar" <68).
    growth_score: float | None = None
    score_band: Literal["post-ready", "strong", "workable", "below-bar"] | None = None
    score_terms: dict[str, float] = Field(default_factory=dict)
    # A draft that failed a QUALITY bar but is safe and factually clean. It is
    # handed over with `review_note` naming the weakness, because 25 seconds of
    # silence is worse than a flawed draft the writer can fix in five. Never
    # set for a safety block or an invented claim — those still refuse.
    needs_review: bool = False
    review_note: str | None = None
    angle_id: str | None = None
    # User-requested thread: `text` is part 1 (the hook post); the remaining
    # parts ride here for the card's copy chips. Never auto-posted.
    thread_parts: list[str] = Field(default_factory=list)
    # The one thing a reply says back, decided by the writer in the same call
    # as the text (S3) and checked like the old separate point was.
    point: str | None = None
    # A created post offered several stories names the one it chose (1-based, see
    # `llm.topic_choices`); None when it had one subject or none.
    topic: int | None = None


class TrendItem(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    category: str | None = Field(default=None, max_length=120)
    volume_text: str | None = Field(default=None, max_length=120)
    rank: int | None = None


class OwnMaterialRequest(BaseModel):
    """One thing he typed into settings for the account to learn from.

    `kind` is a Literal rather than a validated string so an unknown kind is
    rejected by the schema, before anything can store material whose safety
    rules nobody has decided — the two kinds have opposite ones.
    """
    kind: Literal["voice", "background"]
    # Bounded above the module's own limit so an oversized paste reaches the
    # handler and comes back with the sentence explaining the real limit,
    # rather than a Pydantic error about a field length.
    text: str = Field(max_length=8_000)


class OwnMaterialQuery(BaseModel):
    """Optional filter for the settings list. `None` means both kinds."""
    kind: Literal["voice", "background"] | None = None


class OwnMaterialRemove(BaseModel):
    id: str = Field(max_length=64)


class Feedback(BaseModel):
    candidate_id: IdText
    action: Literal["selected", "accepted", "edited", "rejected", "regenerated", "copied", "inserted", "abandoned", "posted"]
    final_text: PostText | None = None
    context_id: IdText | None = None
    generation_id: IdText | None = None
    metadata: FreeForm = Field(default_factory=dict)


class ProfileSnapshot(BaseModel):
    captured_at: datetime = Field(default_factory=utc_now)
    handle: IdText | None = None
    display_name: ShortText | None = None
    bio: str | None = Field(default=None, max_length=2_000)
    profile_url: UrlText | None = None
    pinned_post_id: IdText | None = None
    followers_count: int | None = Field(default=None, ge=0)
    following_count: int | None = Field(default=None, ge=0)
    posts_count: int | None = Field(default=None, ge=0)
    profile_views: int | None = Field(default=None, ge=0)
    source: Literal["archive", "analytics", "extension", "electron", "manual"] = "manual"
    metadata: FreeForm = Field(default_factory=dict)


class MetricSnapshot(BaseModel):
    post_id: IdText
    captured_at: datetime = Field(default_factory=utc_now)
    post_text: PostText | None = None
    post_url: UrlText | None = None
    post_type: Literal["original", "reply", "quote", "thread", "poll", "post"] | None = None
    media_count: int | None = Field(default=None, ge=0, le=16)
    has_image: bool | None = None
    created_at: datetime | None = None
    visibility: Literal["public", "protected", "unknown"] | None = None
    followers_count: int | None = Field(default=None, ge=0)
    impressions: float | None = Field(default=None, ge=0)
    views: float | None = Field(default=None, ge=0)
    likes: float | None = Field(default=None, ge=0)
    reposts: float | None = Field(default=None, ge=0)
    replies: float | None = Field(default=None, ge=0)
    quotes: float | None = Field(default=None, ge=0)
    bookmarks: float | None = Field(default=None, ge=0)
    profile_clicks: float | None = Field(default=None, ge=0)
    follows: float | None = Field(default=None, ge=0)
    source: Literal["archive", "analytics", "extension", "electron", "manual"] = "manual"
    metadata: FreeForm = Field(default_factory=dict)

    def metrics(self) -> dict[str, float]:
        names = ("impressions", "views", "likes", "reposts", "replies", "quotes", "bookmarks", "profile_clicks", "follows")
        return {name: float(value) for name in names if (value := getattr(self, name)) is not None}


GrowthObjective = Literal["followers", "reach", "conversation", "resonance", "profile_visits"]


class KnowledgeResearchRequest(BaseModel):
    query: str = Field(min_length=2, max_length=4000)
    context: PostContext | None = None
    save_sources: bool = False


class StrategyNextRequest(BaseModel):
    context: PostContext | None = None
    goal_id: str | None = None
    campaign_id: str | None = None
    objective: GrowthObjective | None = None
    now: datetime = Field(default_factory=utc_now)


class PreferenceConfirmRequest(BaseModel):
    proposal_id: str | None = None
    source_text: str = Field(min_length=1, max_length=8000)
    rule: str = Field(min_length=3, max_length=280)
    scope: Literal["voice", "strategy", "format", "timing"] = "strategy"
    confirm: bool = True


class OutcomeRecord(BaseModel):
    generation_id: IdText
    candidate_id: IdText | None = None
    status: Literal["inserted", "edited", "abandoned", "posted"]
    final_text: PostText | None = None
    published_post_id: IdText | None = None
    posted_at: datetime | None = None
    metrics: MetricSnapshot | None = None
    metadata: FreeForm = Field(default_factory=dict)


class ImageFeedback(BaseModel):
    """A direct keep/skip/replace choice for an offered image."""

    generation_id: IdText | None = None
    candidate_id: IdText | None = None
    choice: Literal["used", "skipped", "replaced"]
    metadata: FreeForm = Field(default_factory=dict)


