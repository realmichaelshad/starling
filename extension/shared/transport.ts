/**
 * HTTPS transport between the background worker and Starling's gateway
 * (CONTRACTS section 6).
 *
 * Pure: fetch, the clock, randomness and the consent gate are injected, so every
 * rule here (headers, timeouts, retries, status mapping, the NDJSON stream) is
 * unit-tested without a browser. Nothing in this file touches chrome.*.
 */
import { isAssistResponse } from "./assist-contract";
import { isSafeSitePath } from "./config";
import { isErrorCode, type ErrorCode, type ServerErrorCode } from "./error-codes";
import { NdjsonError, createLineSplitter, parseNdjsonLine } from "./ndjson";
import { normalizeUsage, type UsageBlock } from "./session-state";
import type { AssistResponse } from "./types";

// ---------------------------------------------------------------------------
// Error codes (CONTRACTS section 6.3) and the browser's own failures
// ---------------------------------------------------------------------------

export { CLIENT_ERROR_CODES, ERROR_CODES, SERVER_ERROR_CODES, isErrorCode } from "./error-codes";
export type { ClientErrorCode, ErrorCode, ServerErrorCode } from "./error-codes";

export const TIMEOUT_MS = {
  /** Session, snapshots, outcomes, consent, own material, token exchange. */
  standard: 15_000,
  /** One-shot `POST /assist`. */
  assist: 60_000,
  /** A stream that says nothing (not even a heartbeat) for this long is dead. */
  streamInactivity: 45_000,
  /** Hard cap on a whole stream. */
  streamTotal: 240_000,
  /** Best-effort device revoke during sign-out. */
  revoke: 5_000,
} as const;

export interface ApiError {
  code: ErrorCode;
  message: string;
  /** HTTP status, when a response arrived. */
  status?: number;
  retryAfterS?: number;
  usage?: UsageBlock;
  /** A same-origin path the server suggests (402 and near-limit responses). */
  upgradePath?: string;
  /** The gateway's X-Request-Id, shown to the user as a support reference. */
  requestId?: string;
  /** Whether doing the same thing again can help. */
  retryable: boolean;
  /** The device token the failed request carried, so a stale 401 cannot sign out a newer token. */
  tokenUsed: string | null;
  /** False when nothing came back at all. */
  responseReceived: boolean;
}

const DEFAULT_MESSAGES: Record<ErrorCode, string> = {
  bad_request: "Starling couldn't read that request.",
  signed_out: "Sign in to Starling to keep drafting.",
  limit_reached: "You've used all the drafts in your plan.",
  payment_required: "Your payment didn't go through.",
  fair_use_exceeded: "You've reached this period's fair-use limit.",
  email_unverified: "Verify your email to start drafting.",
  consent_required: "Starling needs your OK before it sends anything.",
  account_suspended: "Drafting is on hold for this account.",
  not_found: "That item no longer exists.",
  busy: "Starling is finishing another draft.",
  duplicate_request: "Starling is still working on that request.",
  idempotency_mismatch: "That request changed while it was being sent.",
  payload_too_large: "That's too much to send at once.",
  extension_outdated: "This version of Starling is out of date.",
  rate_limited: "You're sending requests too quickly.",
  server_error: "Something went wrong on Starling's side.",
  ai_unavailable: "Starling's AI isn't responding.",
  service_paused: "Drafting is paused.",
  offline: "You're offline.",
  network: "Starling couldn't connect.",
  timeout: "Starling took too long, so it stopped.",
  cancelled: "Stopped.",
  interrupted: "The connection to Starling ended before the draft arrived. Nothing in X changed.",
  protocol_error: "Starling couldn't read its own answer.",
  not_configured: "This build of Starling has no service address.",
};

const RETRYABLE = new Set<ErrorCode>([
  "busy", "duplicate_request", "rate_limited", "server_error", "ai_unavailable", "service_paused",
  "offline", "network", "timeout", "cancelled", "interrupted", "protocol_error",
]);

export function defaultErrorMessage(code: ErrorCode): string {
  return DEFAULT_MESSAGES[code];
}

export function clientError(code: ErrorCode, tokenUsed: string | null, extra: Partial<ApiError> = {}): ApiError {
  return {
    retryable: RETRYABLE.has(code),
    responseReceived: false,
    ...extra,
    code,
    tokenUsed,
    message: extra.message ?? DEFAULT_MESSAGES[code],
  };
}

const CODES_BY_STATUS: Record<number, { codes: readonly ServerErrorCode[]; fallback: ServerErrorCode }> = {
  400: { codes: ["bad_request"], fallback: "bad_request" },
  401: { codes: ["signed_out"], fallback: "signed_out" },
  402: { codes: ["limit_reached", "payment_required", "fair_use_exceeded"], fallback: "limit_reached" },
  403: { codes: ["email_unverified", "consent_required", "account_suspended"], fallback: "account_suspended" },
  404: { codes: ["not_found"], fallback: "not_found" },
  409: { codes: ["busy", "duplicate_request", "idempotency_mismatch"], fallback: "busy" },
  413: { codes: ["payload_too_large"], fallback: "payload_too_large" },
  422: { codes: ["bad_request"], fallback: "bad_request" },
  426: { codes: ["extension_outdated"], fallback: "extension_outdated" },
  429: { codes: ["rate_limited"], fallback: "rate_limited" },
  500: { codes: ["server_error"], fallback: "server_error" },
  503: { codes: ["ai_unavailable", "service_paused"], fallback: "server_error" },
};

/** CONTRACTS section 6.3: a documented code for its status wins; otherwise the status decides. */
export function codeForStatus(status: number, bodyCode: unknown): ErrorCode {
  const entry = CODES_BY_STATUS[status];
  if (entry) {
    return typeof bodyCode === "string" && (entry.codes as readonly string[]).includes(bodyCode)
      ? bodyCode as ServerErrorCode
      : entry.fallback;
  }
  if (status >= 500) return "server_error";
  if (status >= 400) return "bad_request";
  return "protocol_error";
}

/** `Retry-After` as delta-seconds or an HTTP date, in whole seconds from now. */
export function parseRetryAfter(header: string | null | undefined, nowMs: number): number | undefined {
  const value = header?.trim();
  if (!value) return undefined;
  if (/^\d+$/.test(value)) return Number(value);
  const when = Date.parse(value);
  return Number.isFinite(when) ? Math.max(0, Math.ceil((when - nowMs) / 1000)) : undefined;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function serverMessage(value: unknown, code: ErrorCode): string {
  return typeof value === "string" && value.trim() ? value.trim().slice(0, 500) : DEFAULT_MESSAGES[code];
}

function nonNegativeSeconds(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? Math.ceil(value) : undefined;
}

export function errorFromResponse(status: number, body: unknown, headers: Headers, nowMs: number, tokenUsed: string | null): ApiError {
  const error = isRecord(body) && isRecord(body.error) ? body.error : {};
  const code = codeForStatus(status, error.code);
  return {
    code,
    status,
    message: serverMessage(error.message, code),
    retryAfterS: nonNegativeSeconds(error.retry_after_s) ?? parseRetryAfter(headers.get("Retry-After"), nowMs),
    usage: normalizeUsage(error.usage) ?? undefined,
    upgradePath: isSafeSitePath(error.upgrade_path) ? error.upgrade_path : undefined,
    requestId: headers.get("X-Request-Id") ?? undefined,
    retryable: RETRYABLE.has(code),
    tokenUsed,
    responseReceived: true,
  };
}

/** A terminal `{"event": "error"}` line inside a stream. */
export function errorFromStreamEvent(value: unknown, tokenUsed: string | null, requestId?: string): ApiError {
  const body = isRecord(value) ? value : {};
  const code: ErrorCode = isErrorCode(body.code) ? body.code : "server_error";
  return {
    code,
    message: serverMessage(body.message, code),
    retryAfterS: nonNegativeSeconds(body.retry_after_s),
    requestId,
    retryable: RETRYABLE.has(code),
    tokenUsed,
    responseReceived: true,
  };
}

// ---------------------------------------------------------------------------
// Dependencies, headers, retries
// ---------------------------------------------------------------------------

export interface TransportDeps {
  fetch: (url: string, init: RequestInit) => Promise<Response>;
  /** `${origin}/api/v1` */
  apiBase: string;
  extensionVersion: string;
  timeZone: () => string;
  isOnline: () => boolean;
  /** The consent gate. False means nothing leaves the browser. */
  canUseNetwork: () => boolean | Promise<boolean>;
  randomUUID: () => string;
  sleep: (ms: number) => Promise<void>;
  random: () => number;
  now: () => number;
}

function isConfigured(deps: TransportDeps): boolean {
  return /^https?:\/\/[^/]/.test(deps.apiBase);
}

function safeTimeZone(read: () => string): string {
  try {
    const zone = read();
    return typeof zone === "string" && /^[A-Za-z0-9_+\-/]{1,64}$/.test(zone) ? zone : "UTC";
  } catch {
    return "UTC";
  }
}

export function requestHeaders(
  deps: TransportDeps,
  options: { token?: string | null; idempotencyKey?: string; json?: boolean; accept?: string },
): Record<string, string> {
  const headers: Record<string, string> = {
    Accept: options.accept ?? "application/json",
    "X-Starling-Extension": deps.extensionVersion,
    "X-Starling-Time-Zone": safeTimeZone(deps.timeZone),
  };
  if (options.json) headers["Content-Type"] = "application/json";
  if (options.token) headers.Authorization = `Bearer ${options.token}`;
  if (options.idempotencyKey) headers["Idempotency-Key"] = options.idempotencyKey;
  return headers;
}

/**
 * session: `GET /session`; capture: snapshot, outcome and image-feedback POSTs;
 * assist: one-shot `POST /assist`; none: everything else.
 */
export type RetryClass = "session" | "capture" | "assist" | "none";

export const RETRY_BUDGET: Record<RetryClass, number> = { session: 2, capture: 2, assist: 1, none: 0 };

/** Backoff before the first and second retry; each is jittered by +/-20%. */
export const BACKOFF_MS = [500, 2_000] as const;

export function backoffDelay(attempt: number, random: () => number): number {
  const base = BACKOFF_MS[Math.min(Math.max(attempt, 0), BACKOFF_MS.length - 1)];
  return Math.round(base * (0.8 + 0.4 * random()));
}

/**
 * Never retried: 401, 402, 403, 409, 422, 429, 500, a 503 carrying a code, and
 * timeouts (the server may still be working on the first attempt).
 */
export function shouldRetry(retry: RetryClass, error: ApiError): boolean {
  switch (retry) {
    case "session":
    case "capture":
      return (!error.responseReceived && error.code === "network") || error.status === 502 || error.status === 504;
    case "assist":
      // Only when nothing at all came back: the same key makes a duplicate harmless.
      return !error.responseReceived && error.code === "network";
    default:
      return false;
  }
}

type AbortReason = "timeout" | "total" | "cancelled";

function failureFromThrow(deps: TransportDeps, error: unknown, reason: AbortReason | null, token: string | null, responseReceived: boolean): ApiError {
  if (reason === "timeout") return clientError("timeout", token, { responseReceived });
  if (reason === "total") {
    return clientError("timeout", token, { responseReceived, message: "Starling took more than four minutes, so the request was stopped." });
  }
  if (reason === "cancelled" || (error instanceof Error && error.name === "AbortError")) {
    return clientError("cancelled", token, { responseReceived });
  }
  if (!deps.isOnline()) return clientError("offline", token, { responseReceived });
  return clientError("network", token, { responseReceived });
}

function parseJson(text: string): unknown {
  if (!text.trim()) return undefined;
  try {
    return JSON.parse(text);
  } catch {
    return undefined;
  }
}

// ---------------------------------------------------------------------------
// One-shot JSON requests
// ---------------------------------------------------------------------------

export type ApiResult<T = unknown> =
  | { ok: true; status: number; data: T; requestId?: string }
  | { ok: false; error: ApiError };

export interface RequestOptions {
  method: "GET" | "POST" | "DELETE";
  /** Relative to `apiBase`, starting with "/". */
  path: string;
  body?: unknown;
  token?: string | null;
  /** Reused for every retry of this request; generated for POST and DELETE when absent. */
  idempotencyKey?: string;
  timeoutMs: number;
  retry?: RetryClass;
  signal?: AbortSignal;
  keepalive?: boolean;
  /** Only for requests that stop processing (device revoke); never for anything that sends content. */
  bypassConsent?: boolean;
}

export async function apiRequest<T = unknown>(deps: TransportDeps, options: RequestOptions): Promise<ApiResult<T>> {
  const retry = options.retry ?? "none";
  const idempotencyKey = options.method === "GET" ? undefined : options.idempotencyKey ?? deps.randomUUID();
  for (let attempt = 0; ; attempt += 1) {
    const result = await attemptRequest<T>(deps, options, idempotencyKey);
    if (result.ok || attempt >= RETRY_BUDGET[retry] || options.signal?.aborted || !shouldRetry(retry, result.error)) {
      return result;
    }
    await deps.sleep(backoffDelay(attempt, deps.random));
  }
}

async function attemptRequest<T>(deps: TransportDeps, options: RequestOptions, idempotencyKey: string | undefined): Promise<ApiResult<T>> {
  const token = options.token ?? null;
  if (!isConfigured(deps)) return { ok: false, error: clientError("not_configured", token) };
  if (!options.bypassConsent && !(await deps.canUseNetwork())) {
    return { ok: false, error: clientError("consent_required", token) };
  }
  if (!deps.isOnline()) return { ok: false, error: clientError("offline", token) };
  if (options.signal?.aborted) return { ok: false, error: clientError("cancelled", token) };

  const controller = new AbortController();
  const abort: { reason: AbortReason | null } = { reason: null };
  const timer = setTimeout(() => {
    abort.reason ??= "timeout";
    controller.abort();
  }, options.timeoutMs);
  const onUserAbort = () => {
    abort.reason ??= "cancelled";
    controller.abort();
  };
  options.signal?.addEventListener("abort", onUserAbort, { once: true });
  try {
    const init: RequestInit = {
      method: options.method,
      headers: requestHeaders(deps, { token, idempotencyKey, json: options.body !== undefined }),
      signal: controller.signal,
      credentials: "omit",
      cache: "no-store",
      redirect: "error",
    };
    if (options.body !== undefined) init.body = JSON.stringify(options.body);
    if (options.keepalive) init.keepalive = true;
    let response: Response;
    try {
      response = await deps.fetch(`${deps.apiBase}${options.path}`, init);
    } catch (error) {
      return { ok: false, error: failureFromThrow(deps, error, abort.reason, token, false) };
    }
    let text: string;
    try {
      text = await response.text();
    } catch (error) {
      return { ok: false, error: { ...failureFromThrow(deps, error, abort.reason, token, true), status: response.status } };
    }
    const data = parseJson(text);
    if (!response.ok) {
      return { ok: false, error: errorFromResponse(response.status, data, response.headers, deps.now(), token) };
    }
    const requestId = response.headers.get("X-Request-Id") ?? undefined;
    if (text.trim() && data === undefined) {
      return { ok: false, error: clientError("protocol_error", token, { responseReceived: true, status: response.status, requestId }) };
    }
    return { ok: true, status: response.status, data: (data ?? null) as T, requestId };
  } finally {
    clearTimeout(timer);
    options.signal?.removeEventListener("abort", onUserAbort);
  }
}

// ---------------------------------------------------------------------------
// The assist stream (CONTRACTS section 6.4)
// ---------------------------------------------------------------------------

export interface StreamHandlers {
  /** `{"event":"status","stage":"accepted"}`, delivered once. */
  onAccepted?: (requestRef: string | undefined) => void;
  onStatus?: (stage: string) => void;
  onQueued?: (position: number | undefined) => void;
  onToken?: (text: string) => void;
  onHeartbeat?: () => void;
  onUsage?: (usage: UsageBlock) => void;
  /** Delivered the moment it arrives; the stream is still drained for a trailing usage event. */
  onFinal?: (response: AssistResponse) => void;
}

export interface StreamOptions {
  body: unknown;
  token: string | null;
  /** One key for the whole press: the single pre-header retry, the cancel and any replay reuse it. */
  idempotencyKey: string;
  signal?: AbortSignal;
  inactivityMs?: number;
  totalMs?: number;
}

export type StreamOutcome =
  | { ok: true; response: AssistResponse; requestId?: string; exposed: boolean }
  | { ok: false; error: ApiError; exposed: boolean; headersReceived: boolean };

export async function streamAssist(deps: TransportDeps, options: StreamOptions, handlers: StreamHandlers = {}): Promise<StreamOutcome> {
  const token = options.token;
  const progress = { exposed: false, headersReceived: false, requestSent: false };
  const fail = (error: ApiError): StreamOutcome => ({
    ok: false, error, exposed: progress.exposed, headersReceived: progress.headersReceived,
  });
  if (!isConfigured(deps)) return fail(clientError("not_configured", token));
  if (!(await deps.canUseNetwork())) return fail(clientError("consent_required", token));
  if (!deps.isOnline()) return fail(clientError("offline", token));

  const controller = new AbortController();
  const abort: { reason: AbortReason | null } = { reason: null };
  const box: { reader: ReadableStreamDefaultReader<string> | null; final: AssistResponse | null; terminal: ApiError | null } = {
    reader: null, final: null, terminal: null,
  };
  const stop = (reason: AbortReason) => {
    abort.reason ??= reason;
    controller.abort();
    // A body the fetch did not create (or one already detached from the
    // signal) must still unblock a pending read.
    void box.reader?.cancel().catch(() => undefined);
  };
  let inactivity: ReturnType<typeof setTimeout> | undefined;
  const armInactivity = () => {
    if (inactivity !== undefined) clearTimeout(inactivity);
    inactivity = setTimeout(() => stop("timeout"), options.inactivityMs ?? TIMEOUT_MS.streamInactivity);
  };
  const total = setTimeout(() => stop("total"), options.totalMs ?? TIMEOUT_MS.streamTotal);
  const onUserAbort = () => stop("cancelled");
  options.signal?.addEventListener("abort", onUserAbort, { once: true });
  if (options.signal?.aborted) stop("cancelled");

  try {
    armInactivity();
    let response: Response | null = null;
    for (let attempt = 0; attempt < 2 && response === null && abort.reason === null; attempt += 1) {
      try {
        progress.requestSent = true;
        response = await deps.fetch(`${deps.apiBase}/assist/stream`, {
          method: "POST",
          headers: requestHeaders(deps, {
            token, idempotencyKey: options.idempotencyKey, json: true, accept: "application/x-ndjson",
          }),
          body: JSON.stringify(options.body),
          signal: controller.signal,
          credentials: "omit",
          cache: "no-store",
          redirect: "error",
        });
      } catch (error) {
        const failure = failureFromThrow(deps, error, abort.reason, token, false);
        // One retry, and only before any header arrived.
        if (attempt === 0 && abort.reason === null && failure.code === "network") {
          await deps.sleep(backoffDelay(0, deps.random));
          if (!deps.isOnline()) return fail(clientError("offline", token));
          armInactivity();
          continue;
        }
        return fail(failure);
      }
    }
    if (response === null) return fail(failureFromThrow(deps, undefined, abort.reason ?? "cancelled", token, false));

    progress.headersReceived = true;
    const requestId = response.headers.get("X-Request-Id") ?? undefined;
    if (!response.ok) {
      const text = await response.text().catch(() => "");
      return fail({ ...errorFromResponse(response.status, parseJson(text), response.headers, deps.now(), token), requestId });
    }
    if (!response.body) {
      return fail(clientError("protocol_error", token, { responseReceived: true, status: response.status, requestId }));
    }
    armInactivity();
    box.reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
    if (abort.reason !== null) void box.reader.cancel().catch(() => undefined);

    const splitter = createLineSplitter();
    let accepted = false;
    const handle = (value: unknown) => {
      if (!isRecord(value)) return;
      switch (value.event) {
        case "status": {
          const stage = typeof value.stage === "string" ? value.stage : "";
          if (stage !== "accepted") {
            handlers.onStatus?.(stage);
          } else if (!accepted) {
            accepted = true;
            handlers.onAccepted?.(typeof value.request_id === "string" ? value.request_id : undefined);
          }
          return;
        }
        case "queued":
          handlers.onQueued?.(typeof value.position === "number" && Number.isFinite(value.position) ? value.position : undefined);
          return;
        case "token":
          if (typeof value.text === "string" && value.text) {
            progress.exposed = true;
            handlers.onToken?.(value.text);
          }
          return;
        case "heartbeat":
          handlers.onHeartbeat?.();
          return;
        case "usage": {
          const usage = normalizeUsage(value.usage);
          if (usage) handlers.onUsage?.(usage);
          return;
        }
        case "final":
          if (box.final) return;
          if (!isAssistResponse(value.response)) {
            box.terminal = clientError("protocol_error", token, {
              responseReceived: true, requestId, message: "Starling's final answer was not in a readable shape.",
            });
            return;
          }
          box.final = value.response;
          handlers.onFinal?.(value.response);
          return;
        case "error":
          if (!box.final) box.terminal = errorFromStreamEvent(value.error, token, requestId);
          return;
        default:
          // Unknown events are ignored so the server can add new ones safely.
          return;
      }
    };

    try {
      for (;;) {
        const { done, value } = await box.reader.read();
        if (done) break;
        armInactivity();
        for (const line of splitter.push(value)) {
          handle(parseNdjsonLine(line));
          if (box.terminal || (abort.reason !== null && !box.final)) break;
        }
        if (box.terminal || (abort.reason !== null && !box.final)) break;
      }
      if (!box.terminal && abort.reason === null) {
        for (const line of splitter.flush()) handle(parseNdjsonLine(line));
      }
    } catch (error) {
      if (!box.final && !box.terminal) {
        box.terminal = error instanceof NdjsonError
          ? clientError("protocol_error", token, {
            responseReceived: true, requestId,
            message: error.code === "line_too_large" ? "Starling sent a reply that was too large to read." : "Starling sent a reply it couldn't read.",
          })
          : droppedStream(failureFromThrow(deps, error, abort.reason, token, true), token, requestId);
      }
    }

    if (box.final) return { ok: true, response: box.final, requestId, exposed: progress.exposed };
    if (box.terminal) return fail(box.terminal);
    if (abort.reason !== null) return fail({ ...failureFromThrow(deps, undefined, abort.reason, token, true), requestId });
    return fail(clientError("interrupted", token, { responseReceived: true, requestId }));
  } finally {
    if (inactivity !== undefined) clearTimeout(inactivity);
    clearTimeout(total);
    options.signal?.removeEventListener("abort", onUserAbort);
    if (!box.final && abort.reason !== null && progress.requestSent) {
      sendCancel(deps, token, options.idempotencyKey);
    }
    controller.abort();
    void box.reader?.cancel().catch(() => undefined);
  }
}

/**
 * Fire-and-forget `POST /requests/{key}/cancel`, with keepalive so it survives
 * the page or worker going away. The path names the stream's key; the request
 * itself carries its own fresh Idempotency-Key.
 */
export function sendCancel(deps: TransportDeps, token: string | null, key: string): void {
  if (!isConfigured(deps) || !key) return;
  try {
    void deps.fetch(`${deps.apiBase}/requests/${encodeURIComponent(key)}/cancel`, {
      method: "POST",
      headers: requestHeaders(deps, { token, idempotencyKey: deps.randomUUID(), json: true }),
      body: "{}",
      keepalive: true,
      credentials: "omit",
      cache: "no-store",
      redirect: "error",
    }).catch(() => undefined);
  } catch {
    /* best effort */
  }
}

/** A read that fails after the headers arrived is the connection ending before the draft did. */
function droppedStream(failure: ApiError, token: string | null, requestId: string | undefined): ApiError {
  return failure.code === "network"
    ? clientError("interrupted", token, { responseReceived: true, requestId })
    : { ...failure, requestId };
}

// ---------------------------------------------------------------------------
// Replay after a dropped stream (CONTRACTS section 6.5)
// ---------------------------------------------------------------------------

/** How often a dropped stream's request is polled while the server reports it pending. */
export const REPLAY_POLL_MS = 2_000;

export type ReplayBody =
  | { state: "pending" }
  | { state: "done"; response: AssistResponse; usage?: UsageBlock };

/**
 * `GET /api/v1/requests/{key}`: 202 `{"request_id","state":"pending"}` while
 * the request runs; 200 `{"request_id","state":"done","response","usage"}` once
 * a stored result exists; 404 `not_found` otherwise.
 */
export function replayBody(data: unknown): ReplayBody | null {
  if (!isRecord(data)) return null;
  if (data.state === "pending") return { state: "pending" };
  if (data.state === "done" && isAssistResponse(data.response)) {
    return { state: "done", response: data.response, usage: normalizeUsage(data.usage) ?? undefined };
  }
  return null;
}

export type ReplayOutcome =
  | { kind: "done"; response: AssistResponse; usage?: UsageBlock; requestId?: string }
  /** 404: the server holds no request under that key, so nothing is coming. */
  | { kind: "not_found" }
  /** Still pending when the request's own 240 s budget ran out. */
  | { kind: "timeout" }
  | { kind: "cancelled" }
  /** The user signed out (or the token changed) while waiting. */
  | { kind: "stopped" }
  /** A refusal that waiting cannot fix: 401, consent withdrawn, a malformed answer. */
  | { kind: "error"; error: ApiError };

export interface ReplayOptions {
  token: string | null;
  /** The dropped stream's Idempotency-Key. */
  key: string;
  /** Epoch ms when the original request started; polling ends 240 s after it. */
  startedAt: number;
  signal?: AbortSignal;
  /** Checked before every poll; false ends the wait (sign-out). */
  shouldContinue?: () => boolean | Promise<boolean>;
  /** Called after each pending answer, before the next wait. */
  onPending?: () => void;
  intervalMs?: number;
  totalMs?: number;
}

const TRANSIENT_CLIENT_CODES = new Set<ErrorCode>(["offline", "network", "timeout"]);

function sleepUnlessAborted(deps: TransportDeps, ms: number, signal?: AbortSignal): Promise<boolean> {
  if (!signal) return deps.sleep(ms).then(() => true);
  if (signal.aborted) return Promise.resolve(false);
  return new Promise(resolve => {
    const onAbort = () => resolve(false);
    signal.addEventListener("abort", onAbort, { once: true });
    void deps.sleep(ms).then(() => {
      signal.removeEventListener("abort", onAbort);
      resolve(!signal.aborted);
    });
  });
}

/**
 * After a stream dropped mid-way, wait for the server to finish that exact
 * request (same key) instead of losing a draft it will charge for. Polls every
 * 2 s while the answer is 202, until 240 s after the request started. A blip
 * while polling (offline, network, timeout, 429, 5xx) counts as still pending.
 */
export async function pollReplay(deps: TransportDeps, options: ReplayOptions): Promise<ReplayOutcome> {
  const interval = options.intervalMs ?? REPLAY_POLL_MS;
  const deadline = options.startedAt + (options.totalMs ?? TIMEOUT_MS.streamTotal);
  const path = `/requests/${encodeURIComponent(options.key)}`;
  for (;;) {
    if (options.signal?.aborted) return { kind: "cancelled" };
    if (options.shouldContinue && !(await options.shouldContinue())) return { kind: "stopped" };
    const remaining = deadline - deps.now();
    if (remaining <= 0) return { kind: "timeout" };
    const result = await apiRequest(deps, {
      method: "GET", path, token: options.token, retry: "none", signal: options.signal,
      timeoutMs: Math.min(TIMEOUT_MS.standard, remaining),
    });
    if (options.signal?.aborted) return { kind: "cancelled" };
    if (result.ok) {
      const body = replayBody(result.data);
      if (body?.state === "done") {
        return { kind: "done", response: body.response, usage: body.usage, requestId: result.requestId };
      }
      if (result.status !== 202 && body?.state !== "pending") {
        return {
          kind: "error",
          error: clientError("protocol_error", options.token, { responseReceived: true, status: result.status, requestId: result.requestId }),
        };
      }
    } else {
      const { error } = result;
      if (error.code === "cancelled") return { kind: "cancelled" };
      if (error.status === 404) return { kind: "not_found" };
      const transient = TRANSIENT_CLIENT_CODES.has(error.code)
        || error.status === 429
        || (error.status !== undefined && error.status >= 500);
      if (!transient) return { kind: "error", error };
    }
    options.onPending?.();
    const wait = Math.min(interval, deadline - deps.now());
    if (wait <= 0) return { kind: "timeout" };
    if (!(await sleepUnlessAborted(deps, wait, options.signal))) return { kind: "cancelled" };
  }
}
