/**
 * The account session (CONTRACTS section 6.2) and the state the background
 * hands to every content script, plus the gates content uses to decide what it
 * may show, scan, send or capture.
 *
 * Pure. The background builds a BridgeState; content only reads it.
 */
import { CONSENT_VERSION } from "./consent";
import type { ErrorCode } from "./error-codes";

export interface SessionAccount {
  id: string;
  email_masked: string;
  email_verified: boolean;
  /** "active" | "suspended" | "deleting" */
  status: string;
}

export interface ScheduledChange {
  plan_code: string;
  effective_at: number;
}

export interface SessionPlan {
  code: string;
  name: string;
  /** "free" | "active" | "past_due" | "suspended" */
  status: string;
  interval: string | null;
  period_start: number | null;
  period_end: number | null;
  cancel_at: number | null;
  scheduled_change: ScheduledChange | null;
  payment_problem: boolean;
  /** Which payment needs the customer (CONTRACTS 6.2); null when none does or the server is older. */
  payment_issue: PaymentIssue | null;
  /**
   * False on the top plan, where there is nothing to upgrade to: the extension offers Contact support
   * instead of "Upgrade". Null when an older server does not say, which keeps the upgrade wording.
   */
  upgradable: boolean | null;
}

export type PaymentIssueKind = "first_payment" | "renewal" | "lapsed";

export interface PaymentIssue {
  kind: PaymentIssueKind;
  plan_code: string | null;
  plan_name: string | null;
}

const PAYMENT_ISSUE_KINDS: readonly string[] = ["first_payment", "renewal", "lapsed"];

function normalizePaymentIssue(value: unknown): PaymentIssue | null {
  if (!isRecord(value) || typeof value.kind !== "string" || !PAYMENT_ISSUE_KINDS.includes(value.kind)) return null;
  return { kind: value.kind as PaymentIssueKind, plan_code: str(value.plan_code), plan_name: str(value.plan_name) };
}

export interface UsageBlock {
  unit: string;
  unit_label: string;
  allowance: number;
  used: number;
  reserved: number;
  remaining: number;
  resets_at: number | null;
  near_limit: boolean;
  fair_use_blocked: boolean;
  /** Credits one press uses (`AI_CREDIT_MULTIPLIER` can raise it). Absent from an older server: 1. */
  per_request?: number;
}

export interface SessionConsent {
  required_version: string;
  accepted_version: string | null;
  passive_learning: boolean;
}

export type ServiceScope = "all" | "free";

export interface SessionService {
  /** "ok" | "paused" */
  status: string;
  /**
   * What a pause covers: "all", or "free" when only the Free plan is paused (its daily budget) and
   * paid plans keep drafting. Null when drafting is not paused, or the server is older.
   */
  scope: ServiceScope | null;
  message: string | null;
  /** Epoch seconds when a pause is expected to lift; null when unknown. */
  retry_at: number | null;
}

export interface SessionJson {
  account: SessionAccount;
  plan: SessionPlan;
  usage: UsageBlock;
  consent: SessionConsent;
  service: SessionService;
  extension: { min_version: string };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function str(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

export function normalizeUsage(value: unknown): UsageBlock | null {
  if (!isRecord(value)) return null;
  const allowance = num(value.allowance);
  const used = num(value.used);
  const remaining = num(value.remaining);
  if (allowance === null || used === null || remaining === null) return null;
  const perRequest = positiveInteger(value.per_request);
  return {
    unit: str(value.unit) ?? "draft",
    unit_label: str(value.unit_label) ?? "drafts",
    allowance,
    used,
    reserved: num(value.reserved) ?? 0,
    remaining,
    resets_at: num(value.resets_at),
    near_limit: value.near_limit === true,
    fair_use_blocked: value.fair_use_blocked === true,
    // Left out when the server does not send it (`draftsPerRequest` then says 1). Its absence also
    // says the server predates `used_after_stop`, the same contract revision (`stoppedCostLine`).
    ...(perRequest !== null ? { per_request: perRequest } : {}),
  };
}

function positiveInteger(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value > 0 ? value : null;
}

function normalizeService(service: Record<string, unknown>): SessionService {
  const status = str(service.status) ?? "ok";
  if (status !== "paused") return { status, scope: null, message: null, retry_at: null };
  const scope = service.scope === "all" || service.scope === "free" ? service.scope : null;
  const message = str(service.message)?.trim() || null;
  return { status, scope, message, retry_at: num(service.retry_at) };
}

export function normalizeSession(value: unknown): SessionJson | null {
  if (!isRecord(value)) return null;
  const { account, plan, consent, service, extension } = value;
  if (!isRecord(account) || !isRecord(plan) || !isRecord(consent) || !isRecord(service)) return null;
  const usage = normalizeUsage(value.usage);
  if (!usage) return null;
  const change = plan.scheduled_change;
  const effectiveAt = isRecord(change) ? num(change.effective_at) : null;
  return {
    account: {
      id: str(account.id) ?? "",
      email_masked: str(account.email_masked) ?? "",
      email_verified: account.email_verified === true,
      status: str(account.status) ?? "active",
    },
    plan: {
      code: str(plan.code) ?? "free",
      name: str(plan.name) ?? "Free",
      status: str(plan.status) ?? "free",
      interval: str(plan.interval),
      period_start: num(plan.period_start),
      period_end: num(plan.period_end),
      cancel_at: num(plan.cancel_at),
      scheduled_change: isRecord(change) && typeof change.plan_code === "string" && effectiveAt !== null
        ? { plan_code: change.plan_code, effective_at: effectiveAt }
        : null,
      payment_problem: plan.payment_problem === true,
      payment_issue: normalizePaymentIssue(plan.payment_issue),
      upgradable: typeof plan.upgradable === "boolean" ? plan.upgradable : null,
    },
    usage,
    consent: {
      required_version: str(consent.required_version) ?? "",
      accepted_version: str(consent.accepted_version),
      passive_learning: consent.passive_learning === true,
    },
    service: normalizeService(service),
    extension: { min_version: (isRecord(extension) ? str(extension.min_version) : null) ?? "0.0.0" },
  };
}

export function withUsage(session: SessionJson, usage: UsageBlock): SessionJson {
  return { ...session, usage };
}

/** Numeric comparison of the first three dotted parts ("1.2.10" > "1.2.9"). */
export function compareVersions(left: string, right: string): number {
  const parts = (value: string) => value.split(/[.+-]/).slice(0, 3).map(part => {
    const parsed = Number.parseInt(part, 10);
    return Number.isFinite(parsed) ? parsed : 0;
  });
  const a = parts(left);
  const b = parts(right);
  for (let index = 0; index < 3; index += 1) {
    const delta = (a[index] ?? 0) - (b[index] ?? 0);
    if (delta) return Math.sign(delta);
  }
  return 0;
}

/** The server says this extension build is older than it supports. */
export function isBelowMinimumVersion(session: SessionJson | null | undefined, version: string): boolean {
  return Boolean(session && compareVersions(version, session.extension.min_version) < 0);
}

/** The server requires a disclosure version this build cannot show. */
export function requiresNewerDisclosure(session: SessionJson | null | undefined): boolean {
  return Boolean(session && session.consent.required_version && session.consent.required_version !== CONSENT_VERSION);
}

// ---------------------------------------------------------------------------
// Bridge state: what the background tells content scripts
// ---------------------------------------------------------------------------

export type SignedOutReason = "never" | "user" | "revoked" | "expired";

export interface BridgeError {
  code: ErrorCode;
  message: string;
  requestRef?: string;
  retryAfterS?: number;
}

export interface BridgeState {
  checked_at: string;
  /** A service origin was compiled into this build. */
  configured: boolean;
  online: boolean;
  consent: { accepted: boolean; passive_learning: boolean };
  signed_in: boolean;
  signed_out_reason: SignedOutReason;
  /** 426, a minimum version above this build, or a disclosure this build cannot show. */
  outdated: boolean;
  session: SessionJson | null;
  /** The last failure refreshing the session, if the last attempt failed. */
  error: BridgeError | null;
  extension_version: string;
  /**
   * The key Chrome has bound to "Open or close Starling" (`chrome.commands`), as Chrome writes it:
   * "Alt+Shift+S", or "⌃⇧S" on a Mac. Empty when the customer removed it or another extension
   * holds it. Read by the worker, because a content script cannot ask.
   */
  shortcut?: string;
}

export function initialBridgeState(extensionVersion = "", now = Date.now()): BridgeState {
  return {
    checked_at: new Date(now).toISOString(),
    configured: false,
    online: true,
    consent: { accepted: false, passive_learning: false },
    signed_in: false,
    signed_out_reason: "never",
    outdated: false,
    session: null,
    error: null,
    extension_version: extensionVersion,
  };
}

export function isBridgeState(value: unknown): value is BridgeState {
  return isRecord(value)
    && typeof value.checked_at === "string"
    && typeof value.configured === "boolean"
    && typeof value.online === "boolean"
    && typeof value.signed_in === "boolean"
    && typeof value.outdated === "boolean"
    && isRecord(value.consent)
    && typeof value.consent.accepted === "boolean"
    && typeof value.consent.passive_learning === "boolean";
}

// ---------------------------------------------------------------------------
// Page gates
// ---------------------------------------------------------------------------

/** X surfaces Starling never touches: direct messages, chat, settings and login flows. */
export const EXCLUDED_ROUTES = ["/messages", "/i/chat", "/settings", "/i/flow"] as const;

export function isExcludedRoute(pathname: string): boolean {
  const path = pathname.toLowerCase();
  return EXCLUDED_ROUTES.some(route => path === route || path.startsWith(`${route}/`));
}

export interface PageGate {
  pageDisabled: boolean;
  pathname: string;
}

/** The dock may exist on this page (it can still show only the consent card). */
export function canMountDock(_state: BridgeState, page: PageGate): boolean {
  return !page.pageDisabled && !isExcludedRoute(page.pathname);
}

/** Starling may read the page and add its buttons: requires consent. */
export function canScanPosts(state: BridgeState, page: PageGate): boolean {
  return canMountDock(state, page) && state.consent.accepted;
}

/** Starling may send something to its servers at all. */
export function canRequest(state: BridgeState, page: PageGate): boolean {
  return canScanPosts(state, page) && state.configured && state.online && state.signed_in && !state.outdated;
}

/** Capture nobody asked for in the moment: also needs the passive-learning opt-in. */
export function canCapturePassive(state: BridgeState, page: PageGate): boolean {
  return canRequest(state, page) && state.consent.passive_learning;
}
