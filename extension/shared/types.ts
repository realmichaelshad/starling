// Threads can be read as context but Starling never creates one.
// Threads are read-only context. A browser action never publishes anything.
export type ComposerAction = "reply" | "quote" | "post";

export interface Source {
  title: string;
  url: string;
  published_at?: string;
  snippet?: string;
}

export interface MediaItem {
  url: string;
  kind: "image" | "video" | "gif" | "link_card";
  alt?: string;
  poster_url?: string;
  source_url?: string;
}

export interface VisibleMetrics {
  replies?: number;
  reposts?: number;
  likes?: number;
  bookmarks?: number;
  views?: number;
}

export interface PollOption {
  label: string;
  votes?: number;
  percent?: number;
}

export interface PostContext {
  context_id?: string;
  post_id?: string;
  post_url?: string;
  conversation_id?: string;
  author: string;
  author_handle?: string;
  author_verified?: boolean;
  post_created_at?: string;
  text: string;
  parent_text?: string;
  parent_author?: string;
  parent_post_id?: string;
  parent_post_url?: string;
  quote_text?: string;
  quoted_post_id?: string;
  thread_text?: string[];
  links: string[];
  media: MediaItem[];
  language?: string;
  visibility: "public" | "protected" | "unknown";
  visible_metrics?: VisibleMetrics;
  post_type?: "original" | "reply" | "quote" | "thread" | "poll" | "unknown";
  poll?: { options: PollOption[] };
  poll_options?: PollOption[];
  sensitive?: boolean;
  is_sensitive?: boolean;
  reply_restricted?: boolean;
  /** ConversationState inputs: what the visible thread actually shows. The
   *  engine trusts what's ON SCREEN over a stale DB row — a visible own
   *  reply blocks a duplicate; a scanned thread with none un-blocks one. */
  thread_scanned?: boolean;
  visible_own_reply?: boolean;
  visible_reply_count?: number;
  visible_reply_ages_minutes?: number[];
  is_long_post?: boolean;
  is_own_post?: boolean;
  page_url?: string;
  captured_at?: string;
}

export interface ProfileSnapshotPayload {
  captured_at: string;
  handle: string;
  display_name?: string;
  bio?: string;
  profile_url: string;
  pinned_post_id?: string;
  followers_count?: number;
  following_count?: number;
  posts_count?: number;
  source: "extension";
  metadata: {
    is_own_profile: true;
    signed_in_handle: string;
    source_page: string;
    capture_mode: "passive_visible_page";
  };
}

export interface MetricSnapshotPayload extends VisibleMetrics {
  post_id: string;
  captured_at: string;
  followers_count?: number;
  source: "extension";
  metadata: {
    is_own_post: true;
    account_handle: string;
    author_handle: string;
    post_url: string;
    post_text?: string;
    parent_post_id?: string;
    parent_text?: string;
    /** The parent's @handle, without the "@". The IDENTITY field — every
     *  consumer compares against it, and `relationships.reply_target` strips a
     *  leading "@" from it, which only makes sense on a handle. It held a
     *  display name until 2026-08-07. */
    parent_author?: string;
    /** The parent's human-readable name, for showing. Fills the
     *  `author_relationships.display_name` column, NULL since the table was
     *  created while its value sat in the primary key. */
    parent_display_name?: string;
    parent_post_url?: string;
    media_count?: number;
    has_image?: boolean;
    source_page: string;
    /** `publication_proof` marks a post published from a Starling draft: the server then asks only for
     *  the main disclosure, while `passive_*` captures also need passive-learning consent. */
    capture_mode: "passive_visible_post" | "publication_proof";
    visibility?: PostContext["visibility"];
    post_type?: PostContext["post_type"];
    created_at?: string;
  };
}

export type AssistAction = "reply" | "quote" | "post" | "wait" | "skip";

export type ComposerIntentKind = "create" | "enhance";

export interface ComposerEnhancement {
  action: ComposerAction;
  draft_text: string;
  /** "create" writes from scratch for an EMPTY composer; "enhance" (default) rewrites the user's draft. */
  intent?: ComposerIntentKind;
  /** Downscaled JPEG data-URLs of images attached to the composer, so the
   *  engine's vision model can keep the text consistent with the picture. */
  images?: string[];
}

/** A hold-and-remind action. Never posts to X — the user always posts. */
export interface ScheduleCommand {
  op: "save" | "list" | "list_due" | "mark_posted" | "dismiss";
  draft_id?: number;
  text?: string;
  target_at?: string;      // ISO-8601 UTC
  target_label?: string;
  topic?: string;
  freshness?: string;
  reason?: string;
  image?: Record<string, unknown> | null;
}

export interface BrowserAssistRequest {
  mode: "context" | "chat" | "enhance_composer" | "schedule";
  context?: PostContext;
  message?: string;
  composer?: ComposerEnhancement;
  conversation_id?: string;
  schedule?: ScheduleCommand;
  /**
   * The customer pressed "Write another reply" on a post they already replied to. The engine's own
   * override: without it the duplicate check refuses; with it the draft is written as a follow-up.
   */
  second_reply_ok?: boolean;
}

/**
 * The scheduling recommendation attached to a created post (§ timing). One
 * clear call — "post_now" or "schedule" — with a concrete target, the reasoning
 * behind it, and a per-signal breakdown for the card. Hold-and-remind only.
 */
export interface ScheduleRecommendation {
  default: "post_now" | "schedule";
  /** We actively believe this specific target beats posting now. */
  offer: boolean;
  /**
   * May the user pick their own time? False only on a firm post-now call (a
   * rising or faded conversation), where any hold is the wrong move. True with
   * offer:false is the normal case — no recommended time, but the choice is
   * still the user's.
   */
  allow_manual?: boolean;
  target_iso: string | null;
  target_label: string | null;
  freshness: "fast" | "medium" | "slow";
  /** Longest hold, in hours, the engine would stand behind for this draft. */
  hold_budget?: number;
  /** Mirrors AssistDecision's scale — "moderate" reaches here from the window read. */
  confidence: "limited" | "directional" | "moderate" | "strong";
  headline: string;
  reason: string;
  signals: { best_window: string; topic_momentum: string; freshness: string; cadence: string };
  topic_prediction: { read: string; lifecycle?: string | null; velocity?: number; trend?: string };
}

export interface AssistDecision {
  action: AssistAction;
  rationale: string;
  reasons: string[];
  likely_reaction: string;
  downside?: string;
  confidence: "limited" | "directional" | "moderate" | "strong";
  draft?: string;
  native_intent?: ComposerAction;
  /** The target already has a verified reply from this account. */
  already_replied?: boolean;
  /** A quality or integrity bar this draft missed and shipped anyway.
   *  The engine has set these on every gated draft since the gates were
   *  written; nothing in the extension read either of them, so the warning
   *  "I can't back this claim from the evidence I have" was computed, stored,
   *  transmitted — and shown to nobody. Declared here so the card can. */
  needs_review?: boolean;
  review_note?: string;
  /** Whether the WRITER received fetched evidence — not whether a search ran.
   *  `false` means the draft was written with nothing external behind it;
   *  omitted means not applicable (enhancing his own words). The engine has
   *  sent this since the grounding gate was written and the card never showed
   *  it, so a skip the code calls "visible" was silent. */
  web_checked?: boolean;
  /** The pages whose facts reached the writer (web search results, or the post's own links), at
   *  most three. Shown as links under the draft (`shared/sources.ts`): OpenAI requires clickable
   *  citations for anything drawn from web search results. */
  sources?: Array<{ url: string; title?: string }>;
  /** Set while the account has too little of its own writing for drafts to sound like it: shown
   *  under the draft with a button to the writing samples in Settings. */
  voice_note?: string;
  /** Why this draft's claim is unchecked although a web search was due: it came back late, the
   *  search allowance is used up, or it found nothing that confirms the claim. Said in the card's
   *  note line instead of the generic "No web check" caution. */
  search_note?: string;
  /** The story a created post was written about (the topic Starling picked). Shown as "About: …". */
  topic?: string;
  schedule?: ScheduleRecommendation;
  timing_line?: string;
}

/** The web research behind a chat answer: the pages it cited, as links (same shape as `sources`). */
export interface AssistResearch {
  status?: string;
  sources?: Array<{ url: string; title?: string }>;
}

export interface AssistModelInfo {
  text_ready: boolean;
  provider?: string;
  model_id?: string;
  total_ms?: number;
  generation?: Record<string, unknown>;
}

/**
 * The visual receipt (spec §8.3i). Present only when the Decider chose
 * ATTACH APPROVED IMAGE; `preview_data_url` is a LOCAL prepared copy, never a
 * remote URL, and unknown-rights media never reaches this shape at all.
 */
export interface VisualReceipt {
  why_this_image?: string;
  alt_text?: string;
  preview_data_url?: string;
  mime_type?: string;
  file_name?: string;
  source?: {
    url?: string | null;
    creator?: string | null;
    publisher?: string | null;
    rights?: string;
    required_credit?: string | null;
  };
  post_fit?: {
    action?: string;
    why_not_text_only?: string | null;
    score?: number | null;
    visual_job?: string;
    analyzer_fit?: { pass?: boolean; score?: number; reason?: string };
    visual_habit?: { sample?: number; propensity?: number; image_forward?: boolean; reason?: string };
  };
  controls?: string[];
  requires_user_confirmation?: boolean;
}

export interface AssistResponse {
  ok: boolean;
  decision?: AssistDecision;
  answer?: string;
  error?: string;
  cancelled?: boolean;
  generation_id?: string;
  candidate_id?: string;
  model?: AssistModelInfo;
  /** Set when an approved, rights-cleared image is offered for this draft. */
  visual?: VisualReceipt;
  /** A chat answer's web research: the pages it cited. */
  research?: AssistResearch;
  /** No draft passed the checks: `answer` explains why, and nothing is used (CONTRACTS 3.4). */
  withheld?: boolean;
  /** An Enhance that found nothing to improve and handed the customer's own words back: nothing is used. */
  unchanged?: boolean;
  /**
   * On a stream's final answer: the customer stopped it after part of the draft had been shown, so
   * the draft was used. Every other stopped, failed or withheld request uses nothing.
   */
  used_after_stop?: boolean;
}

export interface OutcomeRecordPayload {
  generation_id: string;
  candidate_id?: string;
  status: "inserted" | "edited" | "abandoned" | "posted";
  final_text?: string;
  metadata?: Record<string, unknown>;
}

/**
 * What the background hands back for a one-shot request: the server's
 * AssistResponse, or a failure that also names the error code, so the card can
 * offer the right next step (sign in, see plans, retry after a countdown).
 * The extra fields are client-side only and never sent to the server.
 */
export interface ClientAssistResponse extends AssistResponse {
  error_code?: string;
  retry_after_s?: number;
  request_ref?: string;
  upgrade_path?: string;
}

/** Messages on the content ⇄ background "starling-assist" port. */
export type AssistPortRequest =
  | { type: "start"; requestId: string; payload: BrowserAssistRequest }
  | { type: "cancel"; requestId: string }
  /** Sent every 20 s while a stream is active so the service worker stays awake. */
  | { type: "ping"; requestId?: string };

export type AssistPortEvent =
  | { type: "accepted"; requestId: string }
  | { type: "status"; requestId: string; stage: string }
  | { type: "token"; requestId: string; text: string }
  | { type: "final"; requestId: string; response: AssistResponse }
  | {
    type: "error"; requestId: string; error: string; retryable: boolean;
    /** An ErrorCode from shared/transport.ts. */
    code?: string;
    retryAfterS?: number;
    requestRef?: string;
    /** Draft text reached the user before the failure (the server charges from there). */
    exposed?: boolean;
  };
