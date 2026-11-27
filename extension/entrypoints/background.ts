import { defineBackground } from "wxt/utils/define-background";
import {
  BROADCAST_KINDS,
  MESSAGE_KINDS,
  SESSION_ROUTE,
  apiRouteFor,
  assistStreamRoute,
  consentRoute,
  openSiteTarget,
  parseConsentAction,
  tokenExchangeRoute,
  type ApiRoute,
} from "../shared/api-routes";
import { invalidAssistResponse, isAssistResponse } from "../shared/assist-contract";
import {
  deviceLabelFrom,
  isPendingSignIn,
  normalizeDeviceAuth,
  reduceAuth,
  runSignIn,
  signOutWithRevoke,
  type AuthEvent,
  type AuthState,
  type SignInFailure,
} from "../shared/auth";
import { API_BASE, STARLING_ORIGIN, UNINSTALL_PATH, sitePageUrl } from "../shared/config";
import {
  acceptConsent,
  consentAllows,
  consentBody,
  hasCurrentConsent,
  normalizeConsent,
  serverConsentNeedsSync,
  withPassiveLearning,
  type ConsentRecord,
} from "../shared/consent";
import {
  initialBridgeState,
  isBelowMinimumVersion,
  normalizeSession,
  normalizeUsage,
  requiresNewerDisclosure,
  withUsage,
  type BridgeError,
  type BridgeState,
  type SessionJson,
  type SignedOutReason,
  type UsageBlock,
} from "../shared/session-state";
import {
  SERVER_ERROR_CODES,
  TIMEOUT_MS,
  apiRequest,
  clientError,
  pollReplay,
  sendCancel,
  streamAssist,
  type ApiError,
  type ApiResult,
  type TransportDeps,
} from "../shared/transport";
import { PAGE_PREFS_KIND, parsePagePrefsRequest } from "../shared/page-prefs";
import { InflightAssists, type InflightAssist } from "../shared/assist-inflight";
import type { AssistPortEvent, AssistPortRequest, ClientAssistResponse } from "../shared/types";

/**
 * The background worker is the only part of Starling that talks to the
 * network and the only part that ever holds the device token.
 *
 * Content scripts ask through runtime messages and the "starling-assist" port;
 * this worker checks consent and sign-in, attaches the token, calls the hosted
 * gateway over HTTPS (shared/transport.ts) and hands back results and state.
 * Nothing leaves the browser until the user has agreed to the data notice.
 */

// Storage keys. The token key appears in this file and nowhere else.
const AUTH_KEY = "starling:auth";
const SIGNED_OUT_REASON_KEY = "starling:signed-out-reason";
const CONSENT_KEY = "starling:consent";
const OUTDATED_KEY = "starling:outdated";
const SETTINGS_KEY = "starling:settings";
const DISABLED_PAGES_KEY = "starling:disabled-pages";
// storage.session: trusted contexts only, cleared when the browser closes.
const SESSION_CACHE_KEY = "starling:session-cache";
const PENDING_SIGN_IN_KEY = "starling:pending-sign-in";
/** `starling:open-dock:<tabId>`: open the panel on that tab's first state request (after a reload). */
const OPEN_DOCK_PREFIX = "starling:open-dock:";
const OPEN_DOCK_TTL_MS = 60_000;

const X_TAB_PATTERNS = ["https://x.com/*", "https://twitter.com/*"];
const SESSION_TTL_MS = 60_000;
/** Forward at most one keep-alive status per stream in this window. */
const HEARTBEAT_FORWARD_MS = 20_000;
/**
 * A stream stopped after part of the draft was shown keeps reading this long for the server's final
 * answer, which says whether the stop used a draft (`used_after_stop`), before it is cut off.
 */
const STOP_GRACE_MS = 10_000;
/** Calling any extension API resets the worker's idle timer. */
const KEEPALIVE_MS = 20_000;
const UPDATE_CHECK_MS = 60 * 60_000;
const WITHDRAW_TIMEOUT_MS = 5_000;

interface SessionCache {
  session: SessionJson | null;
  fetched_at: number;
  device_id: string;
  error: BridgeError | null;
}

type MessageRecord = Record<string, unknown> & { kind: string };
type Handler = (message: MessageRecord, sender: chrome.runtime.MessageSender) => Promise<unknown>;

/**
 * `chrome.storage.local` holds the device token. By default a content script can read that area, and
 * the content script runs inside x.com's renderer. Closing the area to everything but extension pages
 * and this worker makes the token unreachable from there whatever the content script's code does;
 * its own five preferences go through `handlePagePrefs` instead. Set on every start: it is cheap and
 * the default can never come back silently.
 */
function lockStorageToTrustedContexts() {
  try {
    void chrome.storage.local.setAccessLevel({ accessLevel: "TRUSTED_CONTEXTS" }).catch(() => undefined);
  } catch {
    // Older Chrome than the manifest's minimum: the area keeps its default and nothing else changes.
  }
}

export default defineBackground(() => {
  lockStorageToTrustedContexts();
  if (STARLING_ORIGIN) {
    chrome.runtime.setUninstallURL(`${STARLING_ORIGIN}${UNINSTALL_PATH}`, () => void chrome.runtime.lastError);
  }

  chrome.runtime.onInstalled.addListener(details => {
    if (details.reason === "install") void openOnboarding();
  });

  chrome.runtime.onMessage.addListener((message: unknown, sender, sendResponse) => {
    // Only this extension's own pages and content scripts.
    if (sender.id !== chrome.runtime.id) return;
    if (!isRecord(message) || typeof message.kind !== "string") return;
    const handler = HANDLERS[message.kind];
    if (!handler) return;
    void keepAlive(handler(message as MessageRecord, sender)).then(
      sendResponse,
      () => sendResponse({ ok: false, code: "bad_request", error: "Starling couldn't handle that request." }),
    );
    return true;
  });

  chrome.runtime.onConnect.addListener(port => {
    if (port.name !== "starling-assist") return;
    if (port.sender?.id !== chrome.runtime.id) {
      port.disconnect();
      return;
    }
    handleAssistPort(port);
  });

  // The toolbar icon, and the "Open or close Starling" shortcut (`commands._execute_action` in the
  // manifest), which Chrome delivers as the same click.
  chrome.action.onClicked.addListener(tab => void onToolbarClick(tab));

  // A tab that closes with a ✦ still waiting would otherwise leave the server drafting (and holding
  // the account's one slot) for nobody.
  chrome.tabs.onRemoved.addListener(tabId => {
    inflightAssists.cancelScope(`tab:${tabId}`, (token, key) => {
      if (token) sendCancel(transport(), token, key);
    });
    void chrome.storage.session.remove(`${OPEN_DOCK_PREFIX}${tabId}`).catch(() => undefined);
  });

  // Content scripts never subscribe to storage changes (a change event would
  // carry the token into x.com's renderer), so the worker relays the two
  // browser-side preferences that tabs need to share.
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== "local") return;
    if (changes[SETTINGS_KEY]) void broadcastToX({ kind: BROADCAST_KINDS.settingsChanged });
    if (changes[DISABLED_PAGES_KEY]) void broadcastToX({ kind: BROADCAST_KINDS.pagePreferenceChanged });
  });

  self.addEventListener("online", () => void refreshSession());
  self.addEventListener("offline", () => void broadcastState());
});

// ---------------------------------------------------------------------------
// Transport
// ---------------------------------------------------------------------------

function extensionVersion(): string {
  try {
    return chrome.runtime.getManifest().version;
  } catch {
    return "0.0.0";
  }
}

function transport(): TransportDeps {
  return {
    fetch: (url, init) => fetch(url, init),
    apiBase: API_BASE,
    extensionVersion: extensionVersion(),
    timeZone: () => Intl.DateTimeFormat().resolvedOptions().timeZone,
    isOnline: () => navigator.onLine !== false,
    canUseNetwork: async () => hasCurrentConsent(await readConsent()),
    randomUUID: () => crypto.randomUUID(),
    sleep: ms => new Promise(resolve => setTimeout(resolve, ms)),
    random: () => Math.random(),
    now: () => Date.now(),
  };
}

/** Keep the worker awake while a long request (or the sign-in window) is pending. */
async function keepAlive<T>(work: Promise<T>): Promise<T> {
  const timer = setInterval(() => {
    void chrome.runtime.getPlatformInfo().catch(() => undefined);
  }, KEEPALIVE_MS);
  try {
    return await work;
  } finally {
    clearInterval(timer);
  }
}

/** Send one routed request with the device token, after the consent check for that route. */
async function send(route: ApiRoute, options: { signal?: AbortSignal; idempotencyKey?: string } = {}): Promise<ApiResult> {
  const consent = await readConsent();
  if (!consentAllows(route.consent, consent)) {
    return {
      ok: false,
      error: clientError("consent_required", null, route.consent === "passive" && hasCurrentConsent(consent)
        ? { message: "Learning from your own profile and posts is switched off." }
        : {}),
    };
  }
  let token: string | null = null;
  if (route.auth === "device") {
    const { auth } = await readAuthState();
    if (!auth) return { ok: false, error: clientError("signed_out", null) };
    token = auth.token;
  }
  const result = await apiRequest(transport(), {
    method: route.method,
    path: route.path,
    body: route.body,
    token,
    timeoutMs: route.timeoutMs,
    retry: route.retry,
    signal: options.signal,
    idempotencyKey: options.idempotencyKey,
  });
  if (!result.ok) await handleApiError(result.error);
  return result;
}

/** What each failure means for this browser's state. */
async function handleApiError(error: ApiError): Promise<void> {
  let changed = false;
  if (error.status === 401) {
    const before = await readAuthState();
    const after = await applyAuthEvent({ type: "unauthorized", tokenUsed: error.tokenUsed });
    changed = before.auth !== null && after.auth === null;
  }
  if (error.code === "extension_outdated") {
    await markOutdated();
    changed = true;
  }
  if (error.code === "consent_required" && error.responseReceived) {
    const consent = await readConsent();
    if (hasCurrentConsent(consent)) void syncConsent(consent);
  }
  if (error.status === 402 || error.status === 403) {
    void refreshSession();
  } else if (changed) {
    void broadcastState();
  }
}

function bridgeError(error: ApiError): BridgeError {
  return { code: error.code, message: error.message, requestRef: error.requestId, retryAfterS: error.retryAfterS };
}

function failure(error: ApiError): Record<string, unknown> {
  return {
    ok: false,
    code: error.code,
    error: error.message,
    retry_after_s: error.retryAfterS,
    request_ref: error.requestId,
    upgrade_path: error.upgradePath,
  };
}

// ---------------------------------------------------------------------------
// Stored state
// ---------------------------------------------------------------------------

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

async function readLocal(keys: string[]): Promise<Record<string, unknown>> {
  try {
    return await chrome.storage.local.get(keys);
  } catch {
    return {};
  }
}

async function readConsent(): Promise<ConsentRecord | null> {
  return normalizeConsent((await readLocal([CONSENT_KEY]))[CONSENT_KEY]);
}

function normalizeReason(value: unknown): SignedOutReason {
  return value === "user" || value === "revoked" || value === "expired" ? value : "never";
}

let authChain: Promise<unknown> = Promise.resolve();

/** Serialize token changes so a stale 401 and a fresh sign-in cannot interleave. */
function withAuthLock<T>(work: () => Promise<T>): Promise<T> {
  const run = authChain.then(work, work);
  authChain = run.then(() => undefined, () => undefined);
  return run;
}

async function loadAuthState(): Promise<AuthState> {
  const stored = await readLocal([AUTH_KEY, SIGNED_OUT_REASON_KEY]);
  return { auth: normalizeDeviceAuth(stored[AUTH_KEY]), signedOutReason: normalizeReason(stored[SIGNED_OUT_REASON_KEY]) };
}

async function persistAuthState(state: AuthState): Promise<void> {
  if (state.auth) {
    await chrome.storage.local.set({ [AUTH_KEY]: state.auth, [SIGNED_OUT_REASON_KEY]: "never" });
  } else {
    await chrome.storage.local.remove(AUTH_KEY);
    await chrome.storage.local.set({ [SIGNED_OUT_REASON_KEY]: state.signedOutReason });
    await clearSessionCache();
  }
}

function applyAuthEvent(event: AuthEvent): Promise<AuthState> {
  return withAuthLock(async () => {
    const current = await loadAuthState();
    const next = reduceAuth(current, event);
    if (next !== current) await persistAuthState(next);
    return next;
  });
}

/** The current token, after enforcing its absolute expiry. */
function readAuthState(): Promise<AuthState> {
  return applyAuthEvent({ type: "tick", now: Date.now() });
}

async function readSessionCache(): Promise<SessionCache | null> {
  try {
    const value = (await chrome.storage.session.get(SESSION_CACHE_KEY))[SESSION_CACHE_KEY];
    if (!isRecord(value) || typeof value.device_id !== "string" || typeof value.fetched_at !== "number") return null;
    return {
      session: normalizeSession(value.session),
      fetched_at: value.fetched_at,
      device_id: value.device_id,
      error: isRecord(value.error) && typeof value.error.code === "string" ? value.error as unknown as BridgeError : null,
    };
  } catch {
    return null;
  }
}

async function writeSessionCache(cache: SessionCache): Promise<void> {
  await chrome.storage.session.set({ [SESSION_CACHE_KEY]: cache }).catch(() => undefined);
}

async function clearSessionCache(): Promise<void> {
  await chrome.storage.session.remove(SESSION_CACHE_KEY).catch(() => undefined);
}

async function markSessionStale(): Promise<void> {
  const cache = await readSessionCache();
  if (cache) await writeSessionCache({ ...cache, fetched_at: 0 });
}

let lastUpdateCheck = 0;

async function readOutdated(): Promise<boolean> {
  const value = (await readLocal([OUTDATED_KEY]))[OUTDATED_KEY];
  // Tied to the version that was refused: an updated extension starts clean.
  return isRecord(value) && value.version === extensionVersion();
}

async function markOutdated(): Promise<void> {
  await chrome.storage.local.set({ [OUTDATED_KEY]: { version: extensionVersion(), at: Date.now() } }).catch(() => undefined);
  if (Date.now() - lastUpdateCheck > UPDATE_CHECK_MS) {
    lastUpdateCheck = Date.now();
    // Chrome installs a found update once the extension is idle.
    void chrome.runtime.requestUpdateCheck().catch(() => undefined);
  }
}

async function clearOutdated(): Promise<void> {
  await chrome.storage.local.remove(OUTDATED_KEY).catch(() => undefined);
}

// ---------------------------------------------------------------------------
// Bridge state
// ---------------------------------------------------------------------------

let shortcutCache: { value: string; at: number } | null = null;

/**
 * The key Chrome actually bound to "Open or close Starling". The manifest only SUGGESTS
 * Alt+Shift+S (MacCtrl+Shift+S on a Mac); the customer can change it at chrome://extensions/shortcuts,
 * and Chrome leaves it unbound when another extension already holds it. Tooltips say what is true.
 */
async function readShortcut(): Promise<string> {
  if (shortcutCache && Date.now() - shortcutCache.at < 30_000) return shortcutCache.value;
  let value = "";
  try {
    const commands = await chrome.commands.getAll();
    value = commands.find(command => command.name === "_execute_action")?.shortcut ?? "";
  } catch {
    value = "";
  }
  shortcutCache = { value, at: Date.now() };
  return value;
}

async function composeState(): Promise<BridgeState> {
  const [consent, authState, cache, outdated, shortcut] = await Promise.all([
    readConsent(), readAuthState(), readSessionCache(), readOutdated(), readShortcut(),
  ]);
  const auth = authState.auth;
  const sameDevice = Boolean(auth && cache && cache.device_id === auth.device_id);
  const session = sameDevice ? cache!.session : null;
  const version = extensionVersion();
  return {
    ...initialBridgeState(version),
    configured: Boolean(STARLING_ORIGIN),
    online: navigator.onLine !== false,
    consent: { accepted: hasCurrentConsent(consent), passive_learning: consentAllows("passive", consent) },
    signed_in: Boolean(auth),
    signed_out_reason: auth ? "never" : authState.signedOutReason,
    outdated: outdated || isBelowMinimumVersion(session, version) || requiresNewerDisclosure(session),
    session,
    error: sameDevice ? cache!.error : null,
    shortcut,
  };
}

async function refreshDue(): Promise<boolean> {
  if (!STARLING_ORIGIN || navigator.onLine === false) return false;
  const [consent, { auth }, cache] = await Promise.all([readConsent(), readAuthState(), readSessionCache()]);
  if (!hasCurrentConsent(consent) || !auth) return false;
  return !cache || cache.device_id !== auth.device_id || Date.now() - cache.fetched_at >= SESSION_TTL_MS;
}

/** Answer at once from the cache; refresh behind it when stale and broadcast the result. */
async function stateForReader(): Promise<BridgeState> {
  const state = await composeState();
  if (await refreshDue()) void refreshSession();
  return state;
}

let refreshInFlight: Promise<BridgeState> | null = null;

function refreshSession(): Promise<BridgeState> {
  if (refreshInFlight) return refreshInFlight;
  refreshInFlight = (async () => {
    try {
      const consent = await readConsent();
      const { auth } = await readAuthState();
      if (!STARLING_ORIGIN || !hasCurrentConsent(consent) || !auth) return await broadcastState();
      const result = await send(SESSION_ROUTE);
      const previous = await readSessionCache();
      const kept = previous?.device_id === auth.device_id ? previous.session : null;
      if (result.ok) {
        const session = normalizeSession(result.data);
        if (session) {
          await clearOutdated();
          await writeSessionCache({ session, fetched_at: Date.now(), device_id: auth.device_id, error: null });
          if (serverConsentNeedsSync(consent, session.consent)) void syncConsent(consent);
        } else {
          await writeSessionCache({
            session: kept, fetched_at: Date.now(), device_id: auth.device_id,
            error: bridgeError(clientError("protocol_error", auth.token, { responseReceived: true })),
          });
        }
      } else if ((await readAuthState()).auth?.device_id === auth.device_id) {
        await writeSessionCache({ session: kept, fetched_at: Date.now(), device_id: auth.device_id, error: bridgeError(result.error) });
      }
      return await broadcastState();
    } finally {
      refreshInFlight = null;
    }
  })();
  return refreshInFlight;
}

async function broadcastToX(message: Record<string, unknown>): Promise<void> {
  const tabs = await chrome.tabs.query({ url: X_TAB_PATTERNS }).catch(() => [] as chrome.tabs.Tab[]);
  await Promise.all(tabs.map(tab => tab.id === undefined
    ? undefined
    : chrome.tabs.sendMessage(tab.id, message).catch(() => undefined)));
}

async function broadcastState(): Promise<BridgeState> {
  const state = await composeState();
  const message = { kind: BROADCAST_KINDS.bridgeState, state };
  await broadcastToX(message);
  // Extension pages (onboarding) listen on runtime messages.
  void chrome.runtime.sendMessage(message).catch(() => undefined);
  return state;
}

async function applyUsage(usage: UsageBlock): Promise<void> {
  const cache = await readSessionCache();
  if (cache?.session) await writeSessionCache({ ...cache, session: withUsage(cache.session, usage) });
  await broadcastState();
  void refreshSession();
}

/**
 * A metered answer carries the account's usage after it (CONTRACTS 6.2): shown at once, so the
 * panel's numbers move with the draft instead of on the next open. Without one, the next reader
 * refreshes them.
 */
function applyUsageFrom(usage: unknown): void {
  const block = normalizeUsage(usage);
  if (block) void applyUsage(block);
  else void markSessionStale();
}

// ---------------------------------------------------------------------------
// Message handlers
// ---------------------------------------------------------------------------

const HANDLERS: Record<string, Handler> = {
  [MESSAGE_KINDS.getBridgeState]: handleGetBridgeState,
  [MESSAGE_KINDS.refreshSession]: async () => ({ ok: true, state: await refreshSession() }),
  [MESSAGE_KINDS.consent]: handleConsent,
  [MESSAGE_KINDS.beginSignIn]: handleSignIn,
  [MESSAGE_KINDS.signOut]: handleSignOut,
  [MESSAGE_KINDS.openSite]: handleOpenSite,
  [MESSAGE_KINDS.openX]: async () => {
    await openX({ openDock: true });
    return { ok: true };
  },
  [MESSAGE_KINDS.assist]: handleAssist,
  [MESSAGE_KINDS.assistCancel]: handleAssistCancel,
  [MESSAGE_KINDS.captureProfile]: handleRouted,
  [MESSAGE_KINDS.captureMetric]: handleRouted,
  [MESSAGE_KINDS.recordOutcome]: handleRouted,
  [MESSAGE_KINDS.imageFeedback]: handleRouted,
  [MESSAGE_KINDS.confirmPreference]: handleRouted,
  [MESSAGE_KINDS.ownMaterial]: handleRouted,
  [PAGE_PREFS_KIND]: handlePagePrefs,
};

/** A content script's own preferences: a fixed list of keys, none of them the token. */
async function handlePagePrefs(message: MessageRecord): Promise<unknown> {
  const request = parsePagePrefsRequest(message);
  if (!request) return { ok: false, code: "bad_request", error: "Starling couldn't handle that request." };
  if (request.op === "get") return { ok: true, values: await readLocal(request.keys) };
  try {
    if (request.op === "set") await chrome.storage.local.set(request.items);
    else await chrome.storage.local.remove(request.keys);
    return { ok: true };
  } catch {
    return { ok: false, code: "bad_request", error: "Starling couldn't save that preference." };
  }
}

async function handleRouted(message: MessageRecord): Promise<unknown> {
  const resolved = apiRouteFor({ kind: message.kind, payload: message.payload, purpose: message.purpose });
  if (!resolved.ok) return failure(clientError("bad_request", null, { message: resolved.message }));
  const result = await send(resolved.route);
  return result.ok ? { ok: true, data: result.data } : failure(result.error);
}

/** A tab's state request: the state, plus "open the panel" when this tab was reloaded to show it. */
async function handleGetBridgeState(_message: MessageRecord, sender: chrome.runtime.MessageSender): Promise<unknown> {
  const state = await stateForReader();
  const tabId = sender.tab?.id;
  if (tabId === undefined) return { ok: true, state };
  const flagKey = `${OPEN_DOCK_PREFIX}${tabId}`;
  const flagged = await chrome.storage.session.get(flagKey).then(values => values[flagKey], () => undefined);
  if (flagged === undefined) return { ok: true, state };
  await chrome.storage.session.remove(flagKey).catch(() => undefined);
  const fresh = typeof flagged === "number" && Date.now() - flagged < OPEN_DOCK_TTL_MS;
  return { ok: true, state, open_dock: fresh };
}

const inflightAssists = new InflightAssists();

/** Which tab a message came from, so one tab can only ever stop its own ✦. */
function senderScope(sender: chrome.runtime.MessageSender): string {
  return sender.tab?.id !== undefined ? `tab:${sender.tab.id}` : `page:${sender.url ?? ""}`;
}

function pageRequestId(message: MessageRecord): string | null {
  const value = message.requestId;
  return typeof value === "string" && value.length > 0 && value.length <= 120 ? value : null;
}

/**
 * One ✦ press: `POST /assist` in "context" mode (and the hold-and-remind commands, which cost nothing).
 *
 * The press carries its own Idempotency-Key and AbortController, registered under the page's request
 * id, so the page can stop it (`starling:assist-cancel`). A request the worker gives up on at 60 s is
 * cancelled on the server by that key before the page hears about it: the server keeps working
 * otherwise, holding the account's one drafting slot (the next press comes back "busy") and able to
 * charge for a draft nobody will see.
 */
async function handleAssist(message: MessageRecord, sender: chrome.runtime.MessageSender): Promise<ClientAssistResponse> {
  const resolved = apiRouteFor({ kind: MESSAGE_KINDS.assist, payload: message.payload });
  if (!resolved.ok) return { ok: false, error: resolved.message, error_code: "bad_request" };
  const drafting = isRecord(message.payload) && message.payload.mode === "context";
  const key = crypto.randomUUID();
  const startedAt = Date.now();
  const requestId = pageRequestId(message);
  const scope = senderScope(sender);
  // Registered before the first await, so a Cancel pressed at once still finds this request (and
  // the request is then never sent) instead of arriving before it exists.
  const entry = drafting && requestId ? inflightAssists.start(scope, requestId, key, null) : null;
  try {
    if (await readOutdated()) {
      const error = clientError("extension_outdated", null);
      return { ok: false, error: error.message, error_code: error.code };
    }
    const token = drafting ? (await readAuthState()).auth?.token ?? null : null;
    if (entry) entry.token = token;
    const result = await send(resolved.route, { idempotencyKey: key, signal: entry?.controller.signal });
    if (!result.ok) {
      if (drafting && result.error.code === "duplicate_request" && !entry?.cancelled) {
        // The key is this press's own, so a duplicate is this press: the transport's one automatic
        // retry (sent when nothing came back) found the first attempt still running. Wait for that
        // attempt's stored result instead of calling it "another tab" and losing the draft.
        return await replayFirstAttempt(token, key, startedAt, entry);
      }
      if (drafting && result.error.code === "timeout" && !entry?.cancelled) sendCancel(transport(), token, key);
      // A refusal (402, 503) that knows the account's usage moves the panel's numbers too.
      if (result.error.usage) void applyUsage(result.error.usage);
      return assistFailure(result.error);
    }
    if (!isAssistResponse(result.data)) return { ...invalidAssistResponse(), error_code: "protocol_error" };
    // A metered request moved the usage numbers: the answer carries them.
    if (drafting) applyUsageFrom((result.data as unknown as Record<string, unknown>).usage);
    else void markSessionStale();
    return result.data;
  } finally {
    if (entry && requestId) inflightAssists.finish(scope, requestId, entry);
  }
}

function assistFailure(error: ApiError): ClientAssistResponse {
  return {
    ok: false,
    error: error.message,
    error_code: error.code,
    retry_after_s: error.retryAfterS,
    request_ref: error.requestId,
    upgrade_path: error.upgradePath,
    cancelled: error.code === "cancelled" ? true : undefined,
  };
}

/**
 * `GET /requests/{key}` until the first attempt of this press has a stored result, within the press's
 * own 60 s. A Cancel from the page still stops it (and the server, by the same key).
 */
async function replayFirstAttempt(token: string | null, key: string, startedAt: number,
                                  entry: InflightAssist | null): Promise<ClientAssistResponse> {
  const replay = await pollReplay(transport(), {
    token,
    key,
    startedAt,
    totalMs: TIMEOUT_MS.assist,
    signal: entry?.controller.signal,
    shouldContinue: async () => (await readAuthState()).auth?.token === token,
  });
  switch (replay.kind) {
    case "done":
      applyUsageFrom(replay.usage);
      return replay.response;
    case "cancelled":
      return assistFailure(clientError("cancelled", token));
    case "stopped":
      return assistFailure(clientError("signed_out", null));
    case "error":
      await handleApiError(replay.error);
      return assistFailure(replay.error);
    case "timeout":
      // Still running after the press's minute: stop it on the server, as a plain timeout does.
      sendCancel(transport(), token, key);
      return assistFailure(clientError("timeout", token, { responseReceived: true }));
    default:
      // Finished without a stored result (a verdict, or a failure): nothing was used for it.
      void markSessionStale();
      return assistFailure(clientError("interrupted", token, { responseReceived: true }));
  }
}

/** The page stopped a ✦ it started: abort the request here and cancel it on the server. */
async function handleAssistCancel(message: MessageRecord, sender: chrome.runtime.MessageSender): Promise<unknown> {
  const requestId = pageRequestId(message);
  if (!requestId) return { ok: false, code: "bad_request", error: "Nothing to stop." };
  // No token yet means the request was never sent: aborting it here is all there is to do.
  const stopped = inflightAssists.cancel(senderScope(sender), requestId, (token, key) => {
    if (token) sendCancel(transport(), token, key);
  });
  return { ok: true, stopped };
}

async function syncConsent(record: ConsentRecord): Promise<boolean> {
  const { auth } = await readAuthState();
  if (!auth || !hasCurrentConsent(record)) return false;
  const result = await send(consentRoute(consentBody(record)));
  if (result.ok) await markSessionStale();
  return result.ok;
}

async function handleConsent(message: MessageRecord): Promise<unknown> {
  const action = parseConsentAction(message.payload);
  if (!action) return failure(clientError("bad_request", null, { message: "Unknown consent action." }));
  switch (action.action) {
    case "get":
      return { ok: true, consent: await readConsent(), state: await composeState() };
    case "accept": {
      const record = acceptConsent(action.passive_learning, Date.now());
      await chrome.storage.local.set({ [CONSENT_KEY]: record });
      if (await syncConsent(record)) return { ok: true, state: await refreshSession() };
      return { ok: true, state: await broadcastState() };
    }
    case "set-passive": {
      const next = withPassiveLearning(await readConsent(), action.passive_learning);
      if (!next) return failure(clientError("consent_required", null));
      await chrome.storage.local.set({ [CONSENT_KEY]: next });
      await syncConsent(next);
      return { ok: true, state: await broadcastState() };
    }
    case "withdraw": {
      const consent = await readConsent();
      const { auth } = await readAuthState();
      const route = apiRouteFor({ kind: MESSAGE_KINDS.consent, payload: { action: "withdraw" } });
      let withdrawn = false;
      if (hasCurrentConsent(consent) && auth && route.ok) {
        // Best effort, bounded: the local withdrawal happens regardless.
        const result = await send(route.route, { signal: AbortSignal.timeout(WITHDRAW_TIMEOUT_MS) }).catch(() => null);
        withdrawn = Boolean(result?.ok);
      }
      await chrome.storage.local.remove(CONSENT_KEY);
      if (auth) {
        // The gateway signs the device out when consent is withdrawn. Mirror
        // that here, and revoke explicitly when the withdrawal did not arrive.
        if (!withdrawn) await revokeDevice(auth.token);
        await applyAuthEvent({ type: "signed_out" });
      }
      await clearSessionCache();
      return { ok: true, state: await broadcastState() };
    }
    default:
      return failure(clientError("bad_request", null));
  }
}

let signInInFlight: Promise<unknown> | null = null;

const SIGN_IN_MESSAGES: Record<SignInFailure, string> = {
  cancelled: "Sign-in was closed before it finished.",
  denied: "Sign-in was declined.",
  expired: "Sign-in took too long. Try again.",
  no_pending: "Sign-in didn't finish. Try again.",
  state_mismatch: "Sign-in didn't finish. Try again.",
  wrong_redirect: "Sign-in didn't finish. Try again.",
  missing_code: "Sign-in didn't finish. Try again.",
  invalid_token: "Starling couldn't finish signing you in. Try again.",
  exchange_failed: "Starling couldn't finish signing you in. Try again.",
};

async function handleSignIn(): Promise<unknown> {
  if (!STARLING_ORIGIN) return { ...failure(clientError("not_configured", null)), state: await composeState() };
  if (!hasCurrentConsent(await readConsent())) return { ...failure(clientError("consent_required", null)), state: await composeState() };
  if (navigator.onLine === false) return { ...failure(clientError("offline", null)), state: await composeState() };
  if (signInInFlight) return signInInFlight;
  signInInFlight = (async () => {
    const result = await runSignIn({
      origin: STARLING_ORIGIN,
      extensionId: chrome.runtime.id,
      deviceLabel: deviceLabelFrom(navigator.userAgent),
      extensionVersion: extensionVersion(),
      randomBytes: length => crypto.getRandomValues(new Uint8Array(length)),
      subtle: crypto.subtle,
      now: () => Date.now(),
      savePending: pending => chrome.storage.session.set({ [PENDING_SIGN_IN_KEY]: pending }),
      takePending: async () => {
        const value = (await chrome.storage.session.get(PENDING_SIGN_IN_KEY))[PENDING_SIGN_IN_KEY];
        await chrome.storage.session.remove(PENDING_SIGN_IN_KEY);
        return isPendingSignIn(value) ? value : null;
      },
      // The one launcher today. A relay-page launcher with this signature can
      // replace it without touching anything else.
      launch: url => chrome.identity.launchWebAuthFlow({ url, interactive: true }),
      exchange: async body => {
        const exchanged = await send(tokenExchangeRoute(body));
        return exchanged.ok ? { ok: true, data: exchanged.data } : { ok: false, error: exchanged.error };
      },
      saveAuth: async auth => {
        await applyAuthEvent({ type: "token_received", auth });
      },
    });
    if (!result.ok) {
      const code = result.reason === "cancelled" ? "cancelled" : result.error?.code ?? "bad_request";
      const messageText = result.reason === "exchange_failed" && result.error ? result.error.message : SIGN_IN_MESSAGES[result.reason];
      return { ok: false, code, error: messageText, state: await composeState() };
    }
    await clearOutdated();
    await clearSessionCache();
    const consent = await readConsent();
    if (consent) await syncConsent(consent);
    return { ok: true, state: await refreshSession() };
  })();
  try {
    return await signInInFlight;
  } finally {
    signInInFlight = null;
  }
}

/** `POST /devices/current/revoke`. It only stops processing, so it is sent even without consent. */
async function revokeDevice(token: string, signal?: AbortSignal): Promise<boolean> {
  const route = apiRouteFor({ kind: MESSAGE_KINDS.signOut });
  if (!route.ok) return false;
  const result = await apiRequest(transport(), {
    method: route.route.method, path: route.route.path, token,
    timeoutMs: route.route.timeoutMs, signal, bypassConsent: true,
  });
  return result.ok;
}

async function handleSignOut(): Promise<unknown> {
  const { auth } = await readAuthState();
  await signOutWithRevoke(
    async signal => (auth ? revokeDevice(auth.token, signal) : false),
    async () => {
      await applyAuthEvent({ type: "signed_out" });
    },
  );
  await clearSessionCache();
  return { ok: true, state: await broadcastState() };
}

async function handleOpenSite(message: MessageRecord): Promise<unknown> {
  const page = openSiteTarget(message.payload);
  if (!page || !STARLING_ORIGIN) return failure(clientError("bad_request", null, { message: "That page can't be opened." }));
  // Whatever happens on the website (paying, confirming the email, signing in again) should show
  // the moment the customer is back on X, not after the session cache's minute runs out.
  await markSessionStale();
  await openOrFocusSitePage(sitePageUrl(STARLING_ORIGIN, page));
  return { ok: true };
}

/**
 * Open a website page, or bring forward the tab already showing it. Only a tab on the SAME page is
 * reused: a tab on another page of the site may be half way through something (a form, a payment
 * page it came back from) and is never navigated away underneath the customer.
 */
async function openOrFocusSitePage(url: string): Promise<void> {
  const target = new URL(url);
  const tabs = await chrome.tabs.query({ url: `${STARLING_ORIGIN}/*` }).catch(() => [] as chrome.tabs.Tab[]);
  const same = tabs.find(tab => {
    try {
      const open = new URL(tab.url ?? "");
      return open.origin === target.origin && open.pathname === target.pathname;
    } catch {
      return false;
    }
  });
  if (same?.id !== undefined) {
    await chrome.tabs.update(same.id, { url, active: true }).catch(() => undefined);
    if (same.windowId !== undefined) await chrome.windows.update(same.windowId, { focused: true }).catch(() => undefined);
    return;
  }
  await chrome.tabs.create({ url });
}

// ---------------------------------------------------------------------------
// Streams
// ---------------------------------------------------------------------------

/**
 * One port per content script; each streamed request gets its own
 * AbortController and Idempotency-Key. Streamed tokens are display-only: the
 * content script acts solely on the gated final response.
 *
 * Stop: before any text was shown, the request is cut off at once (nothing is used). After text was
 * shown, the stop is sent as `POST /requests/{key}/cancel` and the stream is read on for a few
 * seconds: the server's final answer then says whether the stop used a draft (`used_after_stop`),
 * and the page can say so instead of guessing.
 */
function handleAssistPort(port: chrome.runtime.Port) {
  const active = new Map<string, AbortController>();
  const stops = new Map<string, () => void>();
  let disconnected = false;

  const post = (event: AssistPortEvent) => {
    if (disconnected) return;
    try {
      port.postMessage(event);
    } catch {
      disconnected = true;
    }
  };

  port.onMessage.addListener((message: unknown) => {
    if (!isRecord(message) || typeof message.type !== "string") return;
    // A ping needs no answer: its arrival alone keeps the worker awake.
    if (message.type === "ping") return;
    if (typeof message.requestId !== "string" || !message.requestId) return;
    const request = message as AssistPortRequest;
    if (request.type === "start") void startStream(request.requestId, request.payload);
    if (request.type === "cancel") {
      const stop = stops.get(request.requestId);
      if (stop) stop();
      else active.get(request.requestId)?.abort();
    }
  });

  port.onDisconnect.addListener(() => {
    disconnected = true;
    for (const controller of active.values()) controller.abort();
    active.clear();
  });

  const startStream = async (requestId: string, payload: unknown) => {
    if (active.has(requestId)) return;
    const controller = new AbortController();
    active.set(requestId, controller);
    let stopTimer: ReturnType<typeof setTimeout> | undefined;
    const fail = (error: ApiError, exposed = false) => post({
      type: "error", requestId, error: error.message, retryable: error.retryable,
      code: error.code, retryAfterS: error.retryAfterS, requestRef: error.requestId, exposed,
    });
    try {
      const resolved = assistStreamRoute(payload);
      if (!resolved.ok) return fail(clientError("bad_request", null, { message: resolved.message }));
      if (!consentAllows("main", await readConsent())) return fail(clientError("consent_required", null));
      const { auth } = await readAuthState();
      if (!auth) return fail(clientError("signed_out", null));
      if (await readOutdated()) return fail(clientError("extension_outdated", auth.token));

      const key = crypto.randomUUID();
      const startedAt = Date.now();
      let stage = "starting";
      let lastForward = Date.now();
      let exposed = false;
      let stopping = false;
      stops.set(requestId, () => {
        if (!exposed) {
          controller.abort();
          return;
        }
        if (stopping) return;
        stopping = true;
        sendCancel(transport(), auth.token, key);
        stopTimer = setTimeout(() => controller.abort(), STOP_GRACE_MS);
      });
      const forward = (event: AssistPortEvent) => {
        lastForward = Date.now();
        post(event);
      };
      const outcome = await streamAssist(transport(), {
        body: resolved.route.body, token: auth.token, idempotencyKey: key, signal: controller.signal,
      }, {
        onAccepted: () => forward({ type: "accepted", requestId }),
        onStatus: next => {
          stage = next || stage;
          forward({ type: "status", requestId, stage });
        },
        onQueued: () => {
          stage = "queued";
          forward({ type: "status", requestId, stage });
        },
        onToken: text => {
          exposed = true;
          forward({ type: "token", requestId, text });
        },
        onHeartbeat: () => {
          if (Date.now() - lastForward >= HEARTBEAT_FORWARD_MS) forward({ type: "status", requestId, stage });
        },
        onUsage: usage => void applyUsage(usage),
        onFinal: response => forward({ type: "final", requestId, response }),
      });
      if (outcome.ok) return;
      // Cancelled here means the user stopped it; the card already settled.
      if (controller.signal.aborted) return;
      if (stopping) {
        // Stopped after text was shown, and the server answered with an error: it released the
        // draft, and the page may say nothing was used. A dropped connection proves nothing either
        // way, so the page hears nothing and says nothing.
        if ((SERVER_ERROR_CODES as readonly string[]).includes(outcome.error.code)) fail(outcome.error, outcome.exposed);
        return;
      }
      // A refusal that knows the account's usage (a 402 before the stream began) moves the numbers.
      if (outcome.error.usage) void applyUsage(outcome.error.usage);
      if (outcome.headersReceived && (outcome.error.code === "interrupted" || outcome.error.code === "offline")) {
        // The connection dropped, not the request: the server may still finish
        // (and charge for) it. Wait for that exact request by its key.
        stage = "reconnecting";
        forward({ type: "status", requestId, stage });
        const replay = await keepAlive(pollReplay(transport(), {
          token: auth.token,
          key,
          startedAt,
          signal: controller.signal,
          shouldContinue: async () => (await readAuthState()).auth?.token === auth.token,
          onPending: () => {
            if (Date.now() - lastForward >= HEARTBEAT_FORWARD_MS) forward({ type: "status", requestId, stage });
          },
        }));
        switch (replay.kind) {
          case "done":
            forward({ type: "final", requestId, response: replay.response });
            if (replay.usage) void applyUsage(replay.usage);
            else void markSessionStale();
            return;
          case "cancelled":
            // Stopped (or the tab went away) while waiting: stop the server too.
            sendCancel(transport(), auth.token, key);
            return;
          case "stopped":
            return fail(clientError("signed_out", null), outcome.exposed);
          case "error":
            await handleApiError(replay.error);
            return fail(replay.error, outcome.exposed);
          default:
            // 404, or still pending when the 240 s budget ran out.
            return fail(clientError("interrupted", auth.token, { responseReceived: true, requestId: outcome.error.requestId }), outcome.exposed);
        }
      }
      await handleApiError(outcome.error);
      fail(outcome.error, outcome.exposed);
    } catch {
      fail(clientError("network", null, { message: "Starling couldn't finish that request. Nothing in X changed." }));
    } finally {
      if (stopTimer !== undefined) clearTimeout(stopTimer);
      stops.delete(requestId);
      active.delete(requestId);
    }
  };
}

// ---------------------------------------------------------------------------
// Toolbar and onboarding
// ---------------------------------------------------------------------------

async function openOnboarding(): Promise<void> {
  await chrome.tabs.create({ url: chrome.runtime.getURL("/onboarding.html") }).catch(() => undefined);
}

function isXTab(tab: chrome.tabs.Tab | undefined): boolean {
  return Boolean(tab && tab.id !== undefined && /^https:\/\/(?:x|twitter)\.com\//.test(tab.url ?? ""));
}

/** Is Starling's content script alive in this tab? An old one left by an update cannot answer. */
async function pingTab(tabId: number): Promise<boolean> {
  const reply = await chrome.tabs.sendMessage(tabId, { kind: BROADCAST_KINDS.ping }).then(value => value, () => null);
  return isRecord(reply) && reply.ok === true;
}

/** Open the panel on this tab's first state request: the tab is about to (re)load. */
async function flagOpenDock(tabId: number): Promise<void> {
  await chrome.storage.session.set({ [`${OPEN_DOCK_PREFIX}${tabId}`]: Date.now() }).catch(() => undefined);
}

async function showDockIn(tabId: number): Promise<boolean> {
  const state = await composeState();
  const reply = await chrome.tabs.sendMessage(tabId, { kind: BROADCAST_KINDS.toggleSummon, state, mode: "open" })
    .then(value => value, () => null);
  return isRecord(reply) && reply.ok === true;
}

/**
 * Take the customer to X with Starling showing: the X tab they already have if there is one
 * (reloaded once when it was opened before Starling was installed or updated, because only a reload
 * puts Starling in it), otherwise a new x.com/home tab. The tabs API needs no extra permission for
 * this: the query only matches X, which the extension's host permissions already cover.
 */
async function openX(options: { openDock: boolean }): Promise<void> {
  const tabs = await chrome.tabs.query({ url: X_TAB_PATTERNS }).catch(() => [] as chrome.tabs.Tab[]);
  const tab = [...tabs].sort((a, b) => (b.lastAccessed ?? 0) - (a.lastAccessed ?? 0))[0];
  if (!tab || tab.id === undefined) {
    const created = await chrome.tabs.create({ url: "https://x.com/home" }).catch(() => undefined);
    if (options.openDock && created?.id !== undefined) await flagOpenDock(created.id);
    return;
  }
  await chrome.tabs.update(tab.id, { active: true }).catch(() => undefined);
  if (tab.windowId !== undefined) await chrome.windows.update(tab.windowId, { focused: true }).catch(() => undefined);
  if (await pingTab(tab.id)) {
    if (options.openDock) await showDockIn(tab.id);
    return;
  }
  if (options.openDock) await flagOpenDock(tab.id);
  await chrome.tabs.reload(tab.id).catch(() => undefined);
}

async function onToolbarClick(tab: chrome.tabs.Tab): Promise<void> {
  const consented = hasCurrentConsent(await readConsent());
  if (!isXTab(tab) || tab.id === undefined) {
    // Not an X tab: show the notice (and sign-in) if it is still needed, otherwise go to X.
    if (!consented) await openOnboarding();
    else await openX({ openDock: true });
    return;
  }
  const state = await composeState();
  const delivered = await chrome.tabs.sendMessage(tab.id, { kind: BROADCAST_KINDS.toggleSummon, state })
    .then(value => isRecord(value) && value.ok === true, () => false);
  if (delivered) return;
  // Opened before Starling was installed, or left behind by an update: only a reload puts the
  // current Starling in it. The panel opens as soon as it has loaded.
  await flagOpenDock(tab.id);
  await chrome.tabs.reload(tab.id).catch(() => undefined);
}
