/**
 * Extension sign-in (CONTRACTS section 9): PKCE, the connect URL, the redirect
 * check, the device-token exchange, and the rules for losing a token.
 *
 * Every side effect is injected. The launcher today is
 * chrome.identity.launchWebAuthFlow; if that ever proves unusable, a relay-page
 * launcher with the same signature (open the URL, resolve with the redirect URL)
 * can be passed to `runSignIn` without any caller changing.
 */
import { CONNECT_PATH } from "./config";
import type { SignedOutReason } from "./session-state";
import type { ApiError } from "./transport";

/** A pending sign-in (verifier + state) is redeemable for this long. */
export const PENDING_SIGN_IN_TTL_MS = 15 * 60_000;
/** Sign-out waits this long for the server to revoke the token, then clears it anyway. */
export const SIGN_OUT_REVOKE_TIMEOUT_MS = 5_000;
export const DEVICE_TOKEN_PREFIX = "stl_dev_";

export function base64UrlEncode(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** 32 random bytes as base64url: 43 characters, inside RFC 7636's 43..128. */
export function createCodeVerifier(randomBytes: (length: number) => Uint8Array): string {
  return base64UrlEncode(randomBytes(32));
}

export async function codeChallengeS256(verifier: string, subtle: SubtleCrypto): Promise<string> {
  const digest = await subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return base64UrlEncode(new Uint8Array(digest));
}

export function createState(randomBytes: (length: number) => Uint8Array): string {
  return base64UrlEncode(randomBytes(32));
}

export function isExtensionId(value: unknown): value is string {
  return typeof value === "string" && /^[a-p]{32}$/.test(value);
}

export function redirectUriFor(extensionId: string): string {
  return `https://${extensionId}.chromiumapp.org/connect`;
}

export interface ConnectParams {
  clientId: string;
  redirectUri: string;
  codeChallenge: string;
  state: string;
  deviceLabel: string;
}

export function buildConnectUrl(origin: string, params: ConnectParams): string {
  const url = new URL(CONNECT_PATH, origin);
  url.searchParams.set("client_id", params.clientId);
  url.searchParams.set("redirect_uri", params.redirectUri);
  url.searchParams.set("code_challenge", params.codeChallenge);
  url.searchParams.set("code_challenge_method", "S256");
  url.searchParams.set("state", params.state);
  url.searchParams.set("device_label", params.deviceLabel);
  return url.toString();
}

/** "Chrome on Windows": shown on the approval page and in the account's device list. */
export function deviceLabelFrom(userAgent: string): string {
  const ua = userAgent || "";
  const platform = /Windows/i.test(ua) ? "Windows"
    : /CrOS/i.test(ua) ? "ChromeOS"
      : /Android/i.test(ua) ? "Android"
        : /Macintosh|Mac OS X/i.test(ua) ? "macOS"
          : /Linux/i.test(ua) ? "Linux"
            : "";
  const browser = /Edg\//.test(ua) ? "Edge" : /OPR\//.test(ua) ? "Opera" : "Chrome";
  return platform ? `${browser} on ${platform}` : browser;
}

export interface PendingSignIn {
  verifier: string;
  state: string;
  redirectUri: string;
  /** Epoch milliseconds. */
  createdAt: number;
}

export function isPendingSignIn(value: unknown): value is PendingSignIn {
  if (typeof value !== "object" || value === null) return false;
  const record = value as Record<string, unknown>;
  return typeof record.verifier === "string" && record.verifier.length >= 43
    && typeof record.state === "string" && record.state.length > 0
    && typeof record.redirectUri === "string"
    && typeof record.createdAt === "number" && Number.isFinite(record.createdAt);
}

export type SignInFailure =
  | "no_pending" | "expired" | "cancelled" | "wrong_redirect" | "denied" | "state_mismatch"
  | "missing_code" | "exchange_failed" | "invalid_token";

export type RedirectCheck = { ok: true; code: string } | { ok: false; reason: SignInFailure };

function sameSecret(left: string, right: string): boolean {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) {
    difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return difference === 0;
}

/**
 * Validate the URL the auth window ended on. `pending` must already have been
 * TAKEN from storage (read and deleted), which is what makes a redirect single
 * use: replaying the same URL finds nothing to redeem.
 */
export function checkAuthRedirect(responseUrl: string | undefined, pending: PendingSignIn | null, now: number): RedirectCheck {
  if (!pending) return { ok: false, reason: "no_pending" };
  if (now - pending.createdAt > PENDING_SIGN_IN_TTL_MS || now < pending.createdAt - 60_000) {
    return { ok: false, reason: "expired" };
  }
  if (!responseUrl) return { ok: false, reason: "cancelled" };
  let url: URL;
  let expected: URL;
  try {
    url = new URL(responseUrl);
    expected = new URL(pending.redirectUri);
  } catch {
    return { ok: false, reason: "wrong_redirect" };
  }
  if (url.origin !== expected.origin || url.pathname !== expected.pathname) {
    return { ok: false, reason: "wrong_redirect" };
  }
  const state = url.searchParams.get("state") ?? "";
  if (!sameSecret(state, pending.state)) return { ok: false, reason: "state_mismatch" };
  if (url.searchParams.get("error")) return { ok: false, reason: "denied" };
  const code = url.searchParams.get("code");
  if (!code) return { ok: false, reason: "missing_code" };
  return { ok: true, code };
}

export interface TokenRequestBody {
  grant_type: "authorization_code";
  code: string;
  code_verifier: string;
  redirect_uri: string;
  client_id: string;
  device_label: string;
  extension_version: string;
}

export function tokenRequestBody(input: {
  code: string; verifier: string; redirectUri: string; clientId: string; deviceLabel: string; extensionVersion: string;
}): TokenRequestBody {
  return {
    grant_type: "authorization_code",
    code: input.code,
    code_verifier: input.verifier,
    redirect_uri: input.redirectUri,
    client_id: input.clientId,
    device_label: input.deviceLabel,
    extension_version: input.extensionVersion,
  };
}

export interface DeviceAuth {
  token: string;
  device_id: string;
  /** Unix seconds. */
  idle_expires_at: number;
  /** Unix seconds; the absolute expiry. */
  expires_at: number;
  /** Epoch milliseconds. */
  signed_in_at: number;
}

const TOKEN_SHAPE = /^stl_dev_[A-Za-z0-9_-]{16,}$/;

function toDeviceAuth(value: unknown, signedInAt: number | null): DeviceAuth | null {
  if (typeof value !== "object" || value === null) return null;
  const record = value as Record<string, unknown>;
  if (typeof record.token !== "string" || !TOKEN_SHAPE.test(record.token)) return null;
  if (typeof record.device_id !== "string" || !record.device_id) return null;
  if (typeof record.idle_expires_at !== "number" || !Number.isFinite(record.idle_expires_at)) return null;
  if (typeof record.expires_at !== "number" || !Number.isFinite(record.expires_at)) return null;
  const stamp = signedInAt ?? (typeof record.signed_in_at === "number" ? record.signed_in_at : 0);
  return {
    token: record.token,
    device_id: record.device_id,
    idle_expires_at: record.idle_expires_at,
    expires_at: record.expires_at,
    signed_in_at: stamp,
  };
}

/** `POST /api/v1/devices/token` response. */
export function parseTokenResponse(data: unknown, now: number): DeviceAuth | null {
  return toDeviceAuth(data, now);
}

/** A stored record, as written by the background worker. */
export function normalizeDeviceAuth(value: unknown): DeviceAuth | null {
  return toDeviceAuth(value, null);
}

/**
 * Only the absolute expiry is enforced here. The idle window slides on the
 * server with use, so a local idle check would sign out a token the server
 * still honours; the server's 401 covers that case.
 */
export function isAuthExpired(auth: DeviceAuth, nowMs: number): boolean {
  return auth.expires_at * 1000 <= nowMs;
}

// ---------------------------------------------------------------------------
// Token lifecycle
// ---------------------------------------------------------------------------

export interface AuthState {
  auth: DeviceAuth | null;
  signedOutReason: SignedOutReason;
}

export type AuthEvent =
  | { type: "token_received"; auth: DeviceAuth }
  /** A request came back 401. `tokenUsed` is the token that request carried. */
  | { type: "unauthorized"; tokenUsed: string | null | undefined }
  | { type: "signed_out" }
  | { type: "tick"; now: number };

export function reduceAuth(state: AuthState, event: AuthEvent): AuthState {
  switch (event.type) {
    case "token_received":
      return { auth: event.auth, signedOutReason: "never" };
    case "unauthorized":
      // A 401 for a request made with an OLDER token must not sign out the
      // token that replaced it (e.g. a slow request racing a fresh sign-in).
      if (!state.auth || !event.tokenUsed || event.tokenUsed !== state.auth.token) return state;
      return { auth: null, signedOutReason: "revoked" };
    case "signed_out":
      return { auth: null, signedOutReason: "user" };
    case "tick":
      return state.auth && isAuthExpired(state.auth, event.now) ? { auth: null, signedOutReason: "expired" } : state;
    default:
      return state;
  }
}

// ---------------------------------------------------------------------------
// Orchestration
// ---------------------------------------------------------------------------

/** Opens `connectUrl` and resolves with the URL the flow finally redirected to. */
export type AuthFlowLauncher = (connectUrl: string) => Promise<string | undefined>;

export interface SignInDeps {
  origin: string;
  extensionId: string;
  deviceLabel: string;
  extensionVersion: string;
  randomBytes: (length: number) => Uint8Array;
  subtle: SubtleCrypto;
  now: () => number;
  savePending: (pending: PendingSignIn) => Promise<void>;
  /** Read AND delete the pending record: a redirect can be redeemed once. */
  takePending: () => Promise<PendingSignIn | null>;
  launch: AuthFlowLauncher;
  exchange: (body: TokenRequestBody) => Promise<{ ok: true; data: unknown } | { ok: false; error: ApiError }>;
  saveAuth: (auth: DeviceAuth) => Promise<void>;
}

export type SignInResult = { ok: true; auth: DeviceAuth } | { ok: false; reason: SignInFailure; error?: ApiError };

export async function runSignIn(deps: SignInDeps): Promise<SignInResult> {
  const verifier = createCodeVerifier(deps.randomBytes);
  const state = createState(deps.randomBytes);
  const redirectUri = redirectUriFor(deps.extensionId);
  const codeChallenge = await codeChallengeS256(verifier, deps.subtle);
  await deps.savePending({ verifier, state, redirectUri, createdAt: deps.now() });

  let responseUrl: string | undefined;
  try {
    responseUrl = await deps.launch(buildConnectUrl(deps.origin, {
      clientId: deps.extensionId, redirectUri, codeChallenge, state, deviceLabel: deps.deviceLabel,
    }));
  } catch {
    // The window was closed or the flow could not start: nothing to redeem.
    await deps.takePending();
    return { ok: false, reason: "cancelled" };
  }

  const pending = await deps.takePending();
  const check = checkAuthRedirect(responseUrl, pending, deps.now());
  if (!check.ok || !pending) return { ok: false, reason: check.ok ? "no_pending" : check.reason };

  const exchanged = await deps.exchange(tokenRequestBody({
    code: check.code,
    verifier: pending.verifier,
    redirectUri: pending.redirectUri,
    clientId: deps.extensionId,
    deviceLabel: deps.deviceLabel,
    extensionVersion: deps.extensionVersion,
  }));
  if (!exchanged.ok) return { ok: false, reason: "exchange_failed", error: exchanged.error };
  const auth = parseTokenResponse(exchanged.data, deps.now());
  if (!auth) return { ok: false, reason: "invalid_token" };
  await deps.saveAuth(auth);
  return { ok: true, auth };
}

/**
 * Revoke on the server (best effort, bounded), then clear locally no matter
 * what: a sign-out the user asked for must never depend on the network.
 */
export async function signOutWithRevoke(
  revoke: (signal: AbortSignal) => Promise<boolean>,
  clear: () => Promise<void>,
  timeoutMs = SIGN_OUT_REVOKE_TIMEOUT_MS,
): Promise<{ revoked: boolean }> {
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  let revoked = false;
  try {
    await Promise.race([
      revoke(controller.signal).then(result => { revoked = result; }, () => undefined),
      new Promise<void>(resolve => {
        timer = setTimeout(() => {
          controller.abort();
          resolve();
        }, timeoutMs);
      }),
    ]);
  } finally {
    if (timer !== undefined) clearTimeout(timer);
    await clear();
  }
  return { revoked };
}
