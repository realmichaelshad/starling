/**
 * Everything the dock says about the account: which state card wins, what each
 * error means to the user, the usage meter, dates and countdowns.
 *
 * Pure and fixed-locale ("en-US"): the copy is English, so dates are too, and a
 * test can pin the exact words.
 *
 * Every refusal says whether a draft was used. The server refuses before it starts (signed out, out
 * of drafts, rate limited, busy...) and charges only from the first word of a draft that reached the
 * customer, so "No draft was used." is true for all of those. Where the connection itself failed, the
 * browser cannot know what the server did, so the card says the one thing it can vouch for:
 * "Nothing in X changed."
 */
import type { SitePage } from "./config";
import type { BridgeState, PaymentIssue, ServiceScope, SessionJson, SignedOutReason, UsageBlock } from "./session-state";
import type { ErrorCode } from "./error-codes";
import type { StoppedOutcome } from "./stream";

// ---------------------------------------------------------------------------
// The dock's state card
// ---------------------------------------------------------------------------

/**
 * `fair_use` is not `limit_reached`: the account still has drafts, but this period's fair-use limit
 * (the AI cost of its requests) is reached, so drafting pauses until the allowance resets. Saying
 * "you've used your drafts" there contradicted the footer's "24 drafts left".
 */
export type DockState =
  | "consent_needed" | "offline" | "signed_out" | "outdated" | "email_unverified"
  | "service_paused" | "payment_problem" | "no_plan" | "limit_reached" | "fair_use" | "ready";

/** Highest priority first. */
export const DOCK_STATE_PRIORITY: readonly DockState[] = [
  "consent_needed", "offline", "signed_out", "outdated", "email_unverified",
  "service_paused", "payment_problem", "no_plan", "limit_reached", "fair_use", "ready",
];

export interface DockInputs {
  consentAccepted: boolean;
  online: boolean;
  signedIn: boolean;
  outdated: boolean;
  session: SessionJson | null;
}

export function dockInputsFrom(state: BridgeState): DockInputs {
  return {
    consentAccepted: state.consent.accepted,
    online: state.online,
    signedIn: state.signed_in,
    outdated: state.outdated,
    session: state.session,
  };
}

export function dockStateFor(inputs: DockInputs): DockState {
  if (!inputs.consentAccepted) return "consent_needed";
  if (!inputs.online) return "offline";
  if (!inputs.signedIn) return "signed_out";
  if (inputs.outdated) return "outdated";
  const session = inputs.session;
  // Not known yet: nothing to warn about, and the server stays the authority.
  if (!session) return "ready";
  if (!session.account.email_verified) return "email_unverified";
  if (session.service.status === "paused" && pauseApplies(session)) return "service_paused";
  if (session.plan.payment_problem || session.plan.status === "past_due") return "payment_problem";
  if (session.account.status !== "active" || session.plan.status === "suspended" || session.usage.allowance <= 0) return "no_plan";
  if (session.usage.remaining <= 0) return "limit_reached";
  if (session.usage.fair_use_blocked) return "fair_use";
  return "ready";
}

/** On the Free plan: the plan itself, or a paid plan that lapsed back to it. */
export function isOnFreePlan(session: SessionJson): boolean {
  return session.plan.code === "free" || session.plan.status === "free";
}

/** A pause of the Free plan alone (its daily budget) leaves paid plans drafting. */
export function pauseApplies(session: SessionJson): boolean {
  return session.service.scope !== "free" || isOnFreePlan(session);
}

/**
 * States that stop a request before it is sent. The others (paused, payment,
 * plan, limit) are left to the server, which knows about a period that just
 * reset or a payment that just cleared.
 */
export function blocksRequests(state: DockState): boolean {
  return state === "consent_needed" || state === "offline" || state === "signed_out"
    || state === "outdated" || state === "email_unverified";
}

/**
 * States a customer fixes somewhere else (the sign-in window, the email, the Billing page) and then
 * comes back from. When one of them turns back into "ready", a draft that was refused on the way in
 * is offered again instead of being left under words that are no longer true.
 */
export function isRecoverableState(state: DockState): boolean {
  return state === "limit_reached" || state === "signed_out" || state === "email_unverified"
    || state === "payment_problem" || state === "no_plan" || state === "fair_use";
}

export type DotHealth = "ok" | "warn" | "down";

export function dotHealth(state: DockState): DotHealth {
  if (state === "ready") return "ok";
  if (state === "offline" || state === "outdated" || state === "service_paused") return "down";
  return "warn";
}

// ---------------------------------------------------------------------------
// Numbers, dates, countdowns
// ---------------------------------------------------------------------------

/** CONTRACTS section 6.2: used + reserved >= 80% of the allowance, with something left. */
export function isNearLimit(usage: UsageBlock | null | undefined): boolean {
  if (!usage || usage.allowance <= 0 || usage.remaining <= 0) return false;
  // Integer form of (used + reserved) >= 0.8 * allowance: no float rounding at the boundary.
  return (usage.used + usage.reserved) * 5 >= usage.allowance * 4;
}

/** 20% or less of the allowance left (including none): the footer offers an upgrade. */
export function isLowOnDrafts(usage: UsageBlock | null | undefined): boolean {
  if (!usage || usage.allowance <= 0) return false;
  return Math.max(0, usage.remaining) * 5 <= usage.allowance;
}

/** One key per billing period, so the near-limit notice shows once per period per device. */
export function usagePeriodKey(session: SessionJson): string {
  return String(session.plan.period_start ?? session.usage.resets_at ?? "none");
}

export function shouldShowNearLimit(session: SessionJson | null | undefined, lastShownKey: string | null | undefined): boolean {
  return Boolean(session && isNearLimit(session.usage) && usagePeriodKey(session) !== lastShownKey);
}

export interface DateOptions {
  timeZone?: string;
  /** Epoch milliseconds; the year is added only when it differs from this one. */
  now?: number;
}

function dateFormatter(options: Intl.DateTimeFormatOptions, timeZone?: string): Intl.DateTimeFormat {
  try {
    return new Intl.DateTimeFormat("en-US", { ...options, timeZone });
  } catch {
    return new Intl.DateTimeFormat("en-US", { ...options, timeZone: "UTC" });
  }
}

export function formatDate(epochSeconds: number, options: DateOptions & { short?: boolean } = {}): string {
  const date = new Date(epochSeconds * 1000);
  const yearOf = (value: Date) => dateFormatter({ year: "numeric" }, options.timeZone).format(value);
  const sameYear = yearOf(date) === yearOf(new Date(options.now ?? Date.now()));
  const month = options.short ? "short" : "long";
  return dateFormatter(
    sameYear ? { month, day: "numeric" } : { month, day: "numeric", year: "numeric" },
    options.timeZone,
  ).format(date);
}

/** "3:05 PM", "tomorrow at 9:00 AM", "on October 21 at 9:00 AM": when a daily limit lifts. */
export function formatWhen(epochMs: number, options: DateOptions = {}): string {
  const now = options.now ?? Date.now();
  const time = dateFormatter({ hour: "numeric", minute: "2-digit" }, options.timeZone).format(new Date(epochMs));
  const dayKey = (value: number) => dateFormatter({ year: "numeric", month: "2-digit", day: "2-digit" }, options.timeZone).format(new Date(value));
  if (dayKey(epochMs) === dayKey(now)) return `at ${time}`;
  if (dayKey(epochMs) === dayKey(now + 86_400_000)) return `tomorrow at ${time}`;
  return `on ${formatDate(Math.floor(epochMs / 1000), options)} at ${time}`;
}

/** "45s", "2m 05s", "1h 03m". */
export function formatCountdown(seconds: number): string {
  const whole = Math.max(0, Math.ceil(seconds));
  if (whole < 60) return `${whole}s`;
  if (whole < 3_600) return `${Math.floor(whole / 60)}m ${String(whole % 60).padStart(2, "0")}s`;
  return `${Math.floor(whole / 3_600)}h ${String(Math.floor((whole % 3_600) / 60)).padStart(2, "0")}m`;
}

/** Seconds left of a Retry-After that started at `startedAtMs`. */
export function countdownRemaining(retryAfterS: number, startedAtMs: number, nowMs: number): number {
  return Math.max(0, Math.ceil(retryAfterS - (nowMs - startedAtMs) / 1000));
}

export interface MeterView {
  role: "meter";
  label: string;
  min: number;
  max: number;
  now: number;
  valueText: string;
}

/** The usage meter, with `aria-valuetext` that reads naturally. */
export function usageMeter(usage: UsageBlock): MeterView {
  const spent = Math.max(0, usage.used + usage.reserved);
  if (usage.allowance <= 0) {
    return { role: "meter", label: "Plan usage", min: 0, max: 1, now: 0, valueText: `No ${usage.unit_label} included in your plan` };
  }
  return {
    role: "meter",
    label: "Plan usage",
    min: 0,
    max: usage.allowance,
    now: Math.min(spent, usage.allowance),
    valueText: `${spent} of ${usage.allowance} ${usage.unit_label} used`,
  };
}

/**
 * What one press uses: the server's `usage.per_request` (1 today; `AI_CREDIT_MULTIPLIER` can raise
 * it), or 1 when an older server does not say.
 */
export function draftsPerRequest(usage: UsageBlock | null | undefined): number {
  const value = usage?.per_request;
  return typeof value === "number" && Number.isInteger(value) && value > 0 ? value : 1;
}

/** "1 draft", "2 drafts", in the account's own unit. */
export function draftCount(count: number, usage?: UsageBlock | null): string {
  const unit = usage?.unit?.trim() || "draft";
  const label = usage?.unit_label?.trim() || `${unit}s`;
  return `${count} ${count === 1 ? unit : label}`;
}

/** What one press costs, as words: "1 draft". */
export function pressCost(usage: UsageBlock | null | undefined): string {
  return draftCount(draftsPerRequest(usage), usage);
}

/**
 * A stream the customer stopped after part of the draft was shown is the one stopped request that
 * uses a draft; the server says so with `used_after_stop: true` on its final answer.
 */
export function usedAfterStopLine(usage: UsageBlock | null | undefined): string {
  return `This used ${pressCost(usage)} because part of it was shown.`;
}

/**
 * The line a stopped stream's card ends on, once the server has said what the stop cost. Null when
 * there is nothing true to add: a gateway older than `used_after_stop` (its usage has no
 * `per_request`, from the same contract revision) charges a stop after text without saying so, so
 * its silence cannot be read as "No draft was used".
 */
export function stoppedCostLine(outcome: StoppedOutcome, usage: UsageBlock | null | undefined): string | null {
  switch (outcome) {
    case "used_after_stop":
      return usedAfterStopLine(usage);
    case "finished":
      return `Starling had already finished it, so it used ${pressCost(usage)}.`;
    default:
      return usage?.per_request !== undefined ? NO_DRAFT_USED : null;
  }
}

/** The always-visible footer line: "24 of 25 drafts left · resets Oct 20". */
export function footerUsageLine(state: BridgeState, options: DateOptions = {}): string {
  if (!state.configured) return "Not set up";
  if (!state.consent.accepted) return "Needs your OK to start";
  if (!state.online) return "Offline";
  if (!state.signed_in) return "Signed out";
  if (state.outdated) return "Update needed";
  const session = state.session;
  if (!session) return "Connecting";
  const { remaining, allowance, unit_label: unit, resets_at: resetsAt } = session.usage;
  const left = allowance > 0
    ? `${Math.max(0, remaining)} of ${allowance} ${unit} left`
    : `${Math.max(0, remaining)} ${unit} left`;
  return resetsAt ? `${left} · resets ${formatDate(resetsAt, { ...options, short: true })}` : left;
}

// ---------------------------------------------------------------------------
// Copy
// ---------------------------------------------------------------------------

/** `primary` draws the action as the card's one filled button. */
export type UiAction =
  | { kind: "sign_in"; label: string; primary?: boolean }
  | { kind: "open_site"; label: string; page: SitePage; primary?: boolean }
  | { kind: "consent"; label: string; primary?: boolean }
  | { kind: "retry"; label: string; primary?: boolean }
  /** Read the account again ("I've confirmed it"): nothing to send, only a fresh answer to show. */
  | { kind: "refresh"; label: string; primary?: boolean }
  /** Open the contact page, copying the support reference first when there is one. */
  | { kind: "support"; label: string; ref?: string; primary?: boolean }
  | { kind: "dismiss"; label: string; primary?: boolean };

export interface UiCopy {
  title: string;
  body: string;
  actions: UiAction[];
}

export interface CopyContext {
  usage?: UsageBlock | null;
  timeZone?: string;
  now?: number;
  retryAfterS?: number;
  /** The gateway's request id, offered as a support reference. */
  requestRef?: string;
  /** Draft text already reached the user for this request (the server charges from that point). */
  exposed?: boolean;
  serviceMessage?: string | null;
  /** What a pause covers ("free": only the Free plan is paused) and when it lifts, in epoch seconds. */
  serviceScope?: ServiceScope | null;
  serviceRetryAt?: number | null;
  /**
   * False on the top plan: there is nothing to upgrade to, so the card offers Contact support.
   * Null or absent (an older server) keeps the upgrade wording.
   */
  upgradable?: boolean | null;
  signedOutReason?: SignedOutReason;
  /** Which payment needs the customer, and the plan the account is on now (for "to keep Starter"). */
  paymentIssue?: PaymentIssue | null;
  planName?: string | null;
  /** The account's masked address, for "We sent a link to s***@example.com". */
  email?: string | null;
}

export const NO_DRAFT_USED = "No draft was used.";
export const NOTHING_CHANGED = "Nothing in X changed.";
/** A rate limit that lifts later than this is a daily cap, not a burst: it is said as a time of day. */
export const DAILY_LIMIT_THRESHOLD_S = 5 * 60;

const RETRY: UiAction = { kind: "retry", label: "Try again" };
const CHECK_AGAIN: UiAction = { kind: "retry", label: "Check again" };
const SEE_PLANS: UiAction = { kind: "open_site", page: "limit", label: "See plans", primary: true };
const NOT_NOW: UiAction = { kind: "dismiss", label: "Not now" };
const VIEW_USAGE: UiAction = { kind: "open_site", page: "account", label: "View usage" };
const CONTACT_SUPPORT: UiAction = { kind: "support", label: "Contact support" };

/** What a refused or failed request cost: nothing, when no draft text reached the customer. */
function usedSentence(ctx: CopyContext): string {
  return ctx.exposed ? NOTHING_CHANGED : NO_DRAFT_USED;
}

/** The website's words: "Your allowance resets on October 14." */
function resetSentence(ctx: CopyContext): string | null {
  const resetsAt = ctx.usage?.resets_at;
  return resetsAt ? `Your allowance resets on ${formatDate(resetsAt, ctx)}.` : null;
}

/** False only when the server says so: the top plan has nothing to upgrade to. */
function canUpgrade(ctx: CopyContext): boolean {
  return ctx.upgradable !== false;
}

function needMoreSentence(ctx: CopyContext): string {
  return `Contact support if you need more ${ctx.usage?.unit_label || "drafts"}.`;
}

function limitCopy(ctx: CopyContext, forRequest: boolean): UiCopy {
  const usage = ctx.usage;
  const title = usage && usage.allowance > 0
    ? `You've used your ${usage.allowance} ${usage.unit_label} for this month`
    : "You've used the drafts in your plan";
  const next = canUpgrade(ctx) ? "Upgrade to keep drafting today." : needMoreSentence(ctx);
  const body = [resetSentence(ctx), next, forRequest ? NO_DRAFT_USED : null].filter(Boolean).join(" ");
  return { title, body, actions: [canUpgrade(ctx) ? SEE_PLANS : { ...CONTACT_SUPPORT, primary: true }, NOT_NOW] };
}

/** The website's fair-use notice: drafts are left, but drafting pauses until the allowance resets. */
function fairUseCopy(ctx: CopyContext, forRequest: boolean): UiCopy {
  const resetsAt = ctx.usage?.resets_at;
  return {
    title: resetsAt ? `Drafting is paused until ${formatDate(resetsAt, ctx)}` : "Drafting is paused until your allowance resets",
    body: `Your account reached this period's fair-use limit. Nothing extra is charged.${forRequest ? ` ${NO_DRAFT_USED}` : ""}`,
    actions: [VIEW_USAGE],
  };
}

/**
 * Drafting is paused. The server's own message is said as it is, with nothing appended ("Try again
 * later." after a message that already said it read twice; CONTRACTS 6.2). A pause of the Free plan
 * alone (its daily budget) adds only when Free drafting comes back, in the reader's own time: the
 * server's message already says that paid plans keep drafting.
 */
function pausedCopy(ctx: CopyContext, forRequest: boolean): UiCopy {
  const tail = forRequest ? ` ${NO_DRAFT_USED}` : "";
  const again = forRequest ? RETRY : CHECK_AGAIN;
  const message = ctx.serviceMessage?.trim();
  if (ctx.serviceScope === "free") {
    const retryAt = ctx.serviceRetryAt
      ?? (ctx.retryAfterS !== undefined ? Math.floor((ctx.now ?? Date.now()) / 1000) + ctx.retryAfterS : null);
    const when = retryAt ? formatWhen(retryAt * 1000, ctx) : null;
    const body = message
      ? [message, when ? `Free drafting resumes ${when}.` : null].filter(Boolean).join(" ")
      : `Drafting on the Free plan resumes ${when ?? "later"}. Paid plans keep drafting.`;
    return {
      title: "Drafting is paused",
      body: `${body}${tail}`,
      actions: canUpgrade(ctx) ? [{ kind: "open_site", page: "pricing", label: "See plans", primary: true }, again] : [again],
    };
  }
  return {
    title: "Drafting is paused",
    body: `${message || "Starling has paused drafting for a short while. Try again later."}${tail}`,
    actions: [again],
  };
}

function signedOutCopy(reason: SignedOutReason | undefined, forRequest: boolean): UiCopy {
  const tail = forRequest ? ` ${NO_DRAFT_USED}` : "";
  if (reason === "revoked") {
    return { title: "You were signed out on this browser", body: `Sign in again to keep drafting.${tail}`, actions: [{ kind: "sign_in", label: "Sign in again", primary: true }] };
  }
  if (reason === "expired") {
    return { title: "Your sign-in expired", body: `Sign in again to keep drafting.${tail}`, actions: [{ kind: "sign_in", label: "Sign in again", primary: true }] };
  }
  return {
    title: "Sign in to start drafting",
    body: `Sign in or create a free account. It takes a few seconds.${tail}`,
    actions: [{ kind: "sign_in", label: "Sign in", primary: true }],
  };
}

function unverifiedCopy(ctx: CopyContext, forRequest: boolean): UiCopy {
  const where = ctx.email?.trim() ? `We sent a link to ${ctx.email.trim()}.` : "We sent you a link.";
  return {
    title: "Confirm your email to start drafting",
    body: `${where} Open it, then come back.${forRequest ? ` ${NO_DRAFT_USED}` : ""}`,
    actions: [
      { kind: "open_site", page: "verify", label: "Resend email", primary: true },
      { kind: "refresh", label: "I've confirmed it" },
    ],
  };
}

function outdatedCopy(forRequest: boolean): UiCopy {
  return {
    title: "Update Starling",
    body: `This version of Starling is no longer supported. Chrome updates extensions on its own; you can also update now from chrome://extensions, then reload X.${forRequest ? ` ${NO_DRAFT_USED}` : ""}`,
    actions: [{ kind: "dismiss", label: "OK" }],
  };
}

/**
 * Too many requests. Two different situations share the one status code: a burst (a few seconds to
 * wait) and the plan's daily cap (hours). "Slow down a little" was the wrong words for the second, and
 * a countdown of "13h 20m" is not how anyone thinks about "tomorrow morning".
 */
function rateLimitCopy(ctx: CopyContext): UiCopy {
  const seconds = ctx.retryAfterS;
  if (seconds !== undefined && seconds > DAILY_LIMIT_THRESHOLD_S) {
    const plan = ctx.planName?.trim();
    const when = formatWhen((ctx.now ?? Date.now()) + seconds * 1000, ctx);
    const title = plan ? `You've reached today's limit on ${plan}` : "You've reached today's limit";
    if (!canUpgrade(ctx)) {
      // The top plan: there is no plan with more requests a day to offer.
      return { title, body: `You can draft again ${when}. ${NO_DRAFT_USED}`, actions: [CONTACT_SUPPORT] };
    }
    return {
      title,
      body: `You can draft again ${when}, or upgrade for more requests a day. ${NO_DRAFT_USED}`,
      actions: [{ kind: "open_site", page: "pricing", label: "See plans", primary: true }],
    };
  }
  return {
    title: "One moment",
    body: `Starling can take your next request ${seconds === 0 ? "now" : seconds ? `in ${formatCountdown(seconds)}` : "in a moment"}. ${NO_DRAFT_USED}`,
    actions: [RETRY],
  };
}

/** What a failed request tells the user. Every code has a title, a body and at least one action. */
export function errorCopy(code: ErrorCode, ctx: CopyContext = {}): UiCopy {
  switch (code) {
    case "bad_request":
    case "idempotency_mismatch":
      return { title: "That didn't go through", body: `Try again. ${NO_DRAFT_USED}`, actions: [RETRY] };
    case "signed_out":
      return signedOutCopy(ctx.signedOutReason, true);
    case "limit_reached":
      return limitCopy(ctx, true);
    case "payment_required":
      return { title: "Your payment didn't go through", body: `Update your payment method to keep drafting. ${NO_DRAFT_USED}`, actions: [{ kind: "open_site", page: "billing", label: "Update payment", primary: true }] };
    case "fair_use_exceeded":
      return fairUseCopy(ctx, true);
    case "email_unverified":
      return unverifiedCopy(ctx, true);
    case "consent_required":
      return { title: "Starling needs your OK first", body: `Review what Starling sends before it drafts anything. ${NO_DRAFT_USED}`, actions: [{ kind: "consent", label: "Review", primary: true }] };
    case "account_suspended":
      return { title: "Drafting is on hold for this account", body: `Open your account for details. ${NO_DRAFT_USED}`, actions: [{ kind: "open_site", page: "account", label: "Open account" }] };
    case "not_found":
      return { title: "That item is gone", body: "It may already have been removed.", actions: [{ kind: "dismiss", label: "OK" }] };
    case "busy":
    case "duplicate_request":
      return { title: "Starling is finishing another draft", body: `It may be in another tab. Try again when it's done. ${NO_DRAFT_USED}`, actions: [RETRY] };
    case "payload_too_large":
      return { title: "That's too much to send at once", body: `Remove an attachment or shorten your message, then try again. ${NO_DRAFT_USED}`, actions: [{ kind: "dismiss", label: "OK" }] };
    case "extension_outdated":
      return outdatedCopy(true);
    case "rate_limited":
      return rateLimitCopy(ctx);
    case "server_error":
      return {
        title: "Starling hit a problem",
        body: `Try again. ${usedSentence(ctx)}`,
        actions: [RETRY, { kind: "support", label: "Contact support", ref: ctx.requestRef }],
      };
    case "ai_unavailable":
      // "No draft was used" only when no draft text reached the user: from the
      // first token on, the server charges for the request.
      return { title: "Starling's AI isn't responding", body: `Try again in a moment. ${usedSentence(ctx)}`, actions: [RETRY] };
    case "service_paused":
      return pausedCopy(ctx, true);
    case "offline":
      return { title: "You're offline", body: `Starling will reconnect when your connection is back. ${NO_DRAFT_USED}`, actions: [RETRY] };
    case "network":
      return { title: "Starling couldn't connect", body: `Check your connection and try again. ${NOTHING_CHANGED}`, actions: [RETRY] };
    case "timeout":
      return {
        title: "Starling took too long, so it stopped",
        body: ctx.exposed ? `Try again. ${NOTHING_CHANGED}` : `${NO_DRAFT_USED} Try again.`,
        actions: [RETRY],
      };
    case "cancelled":
      return { title: "Stopped", body: usedSentence(ctx), actions: [RETRY] };
    case "interrupted":
      return { title: "The connection dropped", body: `The connection to Starling ended before the draft arrived. ${NOTHING_CHANGED}`, actions: [RETRY] };
    case "protocol_error":
      return { title: "Starling couldn't read its own answer", body: `Try again. ${NOTHING_CHANGED}`, actions: [RETRY] };
    case "not_configured":
      return { title: "Starling isn't set up", body: "This build has no service address. Install Starling from the Chrome Web Store.", actions: [{ kind: "dismiss", label: "OK" }] };
    default: {
      const unreachable: never = code;
      return { title: "Something went wrong", body: `${String(unreachable)} ${NOTHING_CHANGED}`, actions: [RETRY] };
    }
  }
}

/**
 * Three situations that must never share words (the website's Billing page says the same):
 * a first payment that never finished (never charged, still on Free, drafting carries on), a
 * renewal that failed inside the grace period (the plan is kept for now), and one that outlived
 * it (on Free until it is paid). Every one of them is fixed on the Billing page.
 */
function paymentProblemCopy(ctx: CopyContext): UiCopy {
  const issue = ctx.paymentIssue ?? null;
  const wanted = issue?.plan_name?.trim() || null;
  if (issue?.kind === "first_payment") {
    return {
      title: wanted ? `Your ${wanted} payment isn't finished` : "Your payment isn't finished",
      body: "You haven't been charged and you're still on Free. Finish the payment, or pick another plan, on your Billing page.",
      actions: [{ kind: "open_site", page: "billing", label: "Finish payment", primary: true }],
    };
  }
  if (issue?.kind === "lapsed") {
    return {
      title: "Your payment didn't go through",
      body: `Your account is on Free for now. Update your payment method and ${wanted ?? "your plan"} comes back as soon as the payment succeeds.`,
      actions: [{ kind: "open_site", page: "billing", label: "Update payment", primary: true }],
    };
  }
  const current = ctx.planName?.trim();
  const keep = current && current.toLowerCase() !== "free" ? current : "your plan";
  return { title: "Your payment didn't go through", body: `Update your payment method to keep ${keep}.`, actions: [{ kind: "open_site", page: "billing", label: "Update payment", primary: true }] };
}

/** The card at the top of the dock, or null when the account is ready. */
export function dockStateCopy(state: DockState, ctx: CopyContext = {}): UiCopy | null {
  switch (state) {
    case "consent_needed":
      return { title: "Starling needs your OK first", body: "Review what Starling sends before it starts drafting.", actions: [{ kind: "consent", label: "Review", primary: true }] };
    case "offline":
      return { title: "You're offline", body: "Starling will reconnect when your connection is back.", actions: [{ kind: "retry", label: "Check again" }] };
    case "signed_out":
      return signedOutCopy(ctx.signedOutReason, false);
    case "outdated":
      return outdatedCopy(false);
    case "email_unverified":
      return unverifiedCopy(ctx, false);
    case "service_paused":
      return pausedCopy(ctx, false);
    case "payment_problem":
      return paymentProblemCopy(ctx);
    case "no_plan":
      return { title: "No drafts on this account right now", body: "Choose a plan, or open your account to see why.", actions: [{ kind: "open_site", page: "pricing", label: "See plans", primary: true }, { kind: "open_site", page: "account", label: "Open account" }] };
    case "limit_reached":
      return limitCopy(ctx, false);
    case "fair_use":
      return fairUseCopy(ctx, false);
    case "ready":
      return null;
    default: {
      const unreachable: never = state;
      return unreachable;
    }
  }
}

export function nearLimitCopy(usage: UsageBlock, ctx: CopyContext = {}): UiCopy {
  const left = Math.max(0, usage.remaining);
  const upgrade = canUpgrade(ctx);
  const fallback = upgrade ? "Upgrade any time for more." : needMoreSentence({ ...ctx, usage });
  return {
    title: `${left} ${left === 1 ? usage.unit : usage.unit_label} left this month`,
    body: usage.resets_at ? `Your allowance resets on ${formatDate(usage.resets_at, ctx)}.` : fallback,
    actions: [upgrade ? { kind: "open_site", page: "pricing", label: "See plans" } : CONTACT_SUPPORT],
  };
}

/**
 * The link beside the footer's usage line once 20% or less of the drafts are left: "Upgrade", or on
 * the top plan, where there is nothing to upgrade to, "Contact support". Null when it should not show.
 */
export function footerUpgradeAction(state: BridgeState): UiAction | null {
  const session = state.signed_in ? state.session : null;
  if (!session || !isLowOnDrafts(session.usage)) return null;
  return session.plan.upgradable === false
    ? CONTACT_SUPPORT
    : { kind: "open_site", page: "pricing", label: "Upgrade" };
}

/** "Your plan ends on {date}", when a cancellation is scheduled. */
export function planEndingCopy(session: SessionJson, ctx: CopyContext = {}): UiCopy | null {
  const endsAt = session.plan.cancel_at;
  if (!endsAt) return null;
  return {
    title: `Your plan ends on ${formatDate(endsAt, ctx)}`,
    body: "You can keep drafting until then.",
    actions: [{ kind: "open_site", page: "billing", label: "Manage plan" }],
  };
}

/**
 * The line a refused draft's card turns into once the account is ready again, so the card stops
 * saying "you've run out" after the customer paid. After a sign-in it names who is signed in.
 */
export function resumedLine(session: SessionJson, recoveredFrom: DockState): string {
  if (recoveredFrom === "signed_out" && session.account.email_masked) return `Signed in as ${session.account.email_masked}.`;
  const left = Math.max(0, session.usage.remaining);
  const unit = left === 1 ? session.usage.unit : session.usage.unit_label;
  return `You have ${left} ${unit} on ${session.plan.name || "your plan"}.`;
}
