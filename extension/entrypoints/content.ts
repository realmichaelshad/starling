import { defineContentScript } from "wxt/utils/define-content-script";
import { pageAddressForService, pagePreferenceKey } from "../shared/api";
import { canReplaceComposerDraft, composerAnchor, composerControlCopy, composerMode, isComposerControlDisabled, isNativeComposerSubmitReady, missingSourceLine, resolveComposerControlState, sameComposerText, undoDecision, type ComposerControlState, type ComposerMode, type ComposerUndo } from "../shared/composer-flow";
import { cardIcon, decideWaitingCopy, decisionBadgeLabel, draftHasCheckableClaims, formatElapsed, resumeActionLabel, topicLine, type ResumeTarget } from "../shared/card-copy";
import { isContextOrphaned } from "../shared/orphan";
import { chooseInsertionBeforeSubmit, isCompactRow, type RowNode, type RowRect } from "../shared/composer-insert";
import { parseCompactCount } from "../shared/locale";
import { boxIndexForPart, planThread, threadProgressLine } from "../shared/thread-build";
import { MIN_LEAD_MINUTES, defaultPickerTime, mayClick, planSchedule, readManualTime, scheduleReadyLine, toPickerValue } from "../shared/schedule-ui";
import { computeAnchor, releaseThreshold, shouldReleaseAnchor } from "../shared/feed-anchor";
import { cardViewFor, createOp, isTerminal, shouldPruneOnNewTurn, transitionOp, type GenerationOp, type OpEvent, type OpKind, type OpPhase } from "../shared/generation-op";
import { publishDockHandle } from "../shared/dock-handle";
import { sealShadowKeyboard } from "../shared/keyboard-isolation";
import { isScheduleMutationConfirmed, isScheduleSaveConfirmed } from "../shared/schedule-receipt";
import { finalText, initialStreamState, newRequestId, reduceStream, stoppedStreamOutcome, streamStageLabel, type StoppedOutcome, type StreamState } from "../shared/stream";
import { attachToComposer, isLocalImageCopy, type RightsState } from "../shared/image-attach";
import { MAX_COMPOSER_IMAGES, droppedImagesNote, encodeWithinBudget, fitImagesToRequest, fitWithin } from "../shared/composer-images";
import { matchesComposerHandoffTarget, sameComposerPostReference } from "../shared/composer-handoff";
import { isAllowedNativeClick, isBehindXModal, isXComposeRoute, mayReuseOpenReplyBox, selectLauncherComposer, selectSingleSafeComposerCandidate, shouldRetainComposerIntentOnNavigation } from "../shared/x-action-guards";
import { findOwnReplyToTarget } from "../shared/reply-dedup";
import { CHAT_MESSAGE_MAX_CHARS, assistFailureReason } from "../shared/assist-contract";
import { mergeSources, sourceLinks } from "../shared/sources";
import { importRetryDelayMs, importSummary, isOwnProfilePath, ownPostTexts, type VisiblePost } from "../shared/own-posts";
import { PILL_PALETTE, isThemePreference, pillThemeFor, resolveTheme, themeHelp, toneFromRgb, type SurfaceTone, type ThemePreference } from "../shared/theme";
import { TYPING_SPEEDS, isTypingSpeed, typingDelayMs, type TypingSpeed } from "../shared/typing-speed";
import { BROADCAST_KINDS, MESSAGE_KINDS } from "../shared/messages";
import { pagePrefs } from "../shared/page-prefs";
import { STARLING_ORIGIN, sitePageForPath, type SitePage } from "../shared/config";
import { DISCLOSURE } from "../shared/consent";
import { canCapturePassive, canMountDock, canRequest, canScanPosts, initialBridgeState, isBridgeState, isExcludedRoute, type BridgeState, type PageGate, type SessionJson } from "../shared/session-state";
import { isErrorCode, type ErrorCode } from "../shared/error-codes";
import { NOTHING_CHANGED, blocksRequests, countdownRemaining, dockInputsFrom, draftsPerRequest, footerUpgradeAction, isNearLimit, isRecoverableState, dockStateCopy, dockStateFor, dotHealth, errorCopy, footerUsageLine, formatCountdown, formatDate, nearLimitCopy, planEndingCopy, pressCost, resumedLine, shouldShowNearLimit, stoppedCostLine, usageMeter, usagePeriodKey, type CopyContext, type DockState, type UiAction, type UiCopy } from "../shared/usage-ui";
import type {
  VisualReceipt,
  AssistPortEvent,
  AssistDecision,
  AssistResponse,
  ClientAssistResponse,
  ComposerAction,
  MetricSnapshotPayload,
  PollOption,
  PostContext,
  ProfileSnapshotPayload,
  ScheduleCommand,
  ScheduleRecommendation,
  VisibleMetrics
} from "../shared/types";

const POST_SELECTOR = 'article[data-testid="tweet"], article[role="article"]';
const CHAT_HOST_ID = "starling-chat";
const NAV_HOST_ID = "starling-nav-control";
const DISABLED_PAGES_KEY = "starling:disabled-pages";
// A native X submit can cause a tab reload before the new reply has been
// rendered back into the timeline. Keep the tiny, exact publication witness
// outside the content-script heap so that reload cannot make a real reply
// invisible to the durable duplicate ledger. Every witness lives under ONE key,
// so this script reads exactly that key and never the whole store.
const PENDING_PUBLICATIONS_KEY = "starling:pending-publications";
// X has used both direct tweetTextarea contenteditables and unlabeled inline
// reply editables that appear only after the reply row expands. Resolve the
// editable node first; the composer-intent checks below decide whether it is
// actually an X post, quote, or reply surface before adding Starling.
const COMPOSER_SELECTOR = [
  '[data-testid^="tweetTextarea_"]',
  '[contenteditable="true"][role="textbox"]',
  '[contenteditable="true"][aria-label*="reply" i]',
  '[contenteditable="true"][aria-label*="post" i]',
  '[contenteditable="true"]',
  '[role="dialog"] [contenteditable="true"][role="textbox"]',
  '[role="dialog"] [contenteditable="true"][aria-label*="comment" i]',
  '[role="dialog"] [contenteditable="true"][aria-label*="post" i]'
].join(", ");
const COMPOSER_SUBMIT_SELECTOR = '[data-testid="tweetButton"], [data-testid="tweetButtonInline"], [data-testid^="tweetButton"]';
/** How long a launcher-claimed composer may wait for a measurable submit
 *  button before its session mounts invisibly and starts writing anyway.
 *  Long enough to cover X's row-expand animation, short enough that the user
 *  never perceives the first click as having been ignored. */
const LAUNCHER_PLACEMENT_GRACE_MS = 1_200;
const PASSIVE_CAPTURE_DELAY_MS = 800;
const PASSIVE_ATTEMPT_INTERVAL_MS = 15_000;
const UNCHANGED_CANDIDATE_TTL_MS = 2 * 60 * 1_000;
const MAX_VISIBLE_METRIC_CANDIDATES = 8;

interface SignedInIdentity {
  handle: string;
  displayName?: string;
}

interface CandidateAttempt {
  fingerprint: string;
  attemptedAt: number;
}

/** A manual X submit is not proof by itself. Keep this short-lived witness
 * until X renders a brand-new visible post with the exact text and account. */
interface PendingPublicationProof {
  generationId: string;
  candidateId?: string;
  action: ComposerAction;
  text: string;
  context?: PostContext;
  visiblePostIdsBeforeSubmit: Set<string>;
  expiresAt: number;
  reportedPostId?: string;
}

interface StoredPendingPublicationProof extends Omit<PendingPublicationProof, "visiblePostIdsBeforeSubmit"> {
  visiblePostIdsBeforeSubmit: string[];
}

interface ComposerIntent {
  action: ComposerAction;
  context?: PostContext;
  sourceArticle?: HTMLElement;
  /** An explicit Starling action may type after proving the exact target even
   *  if X has not yet exposed a usable toolbar for Starling's own pill. */
  handoff?: boolean;
  createdAt: number;
  expiresAt: number;
  visibleBefore: Set<HTMLElement>;
  /** Launcher-claimed intents carry the submit button the user clicked next
   *  to. Right after X expands a collapsed row, walking up from the composer
   *  often can't see its submit yet (mid-animation, zero-size) — this known
   *  button keeps the pill on the real placement path instead of the
   *  append-at-toolbar-end fallback that wrapped it into a clipped sliver. */
  launcherSubmit?: HTMLElement;
}

interface QuoteIntent {
  context: PostContext;
  sourceArticle: HTMLElement;
  expiresAt: number;
}

interface ComposerSession {
  id: string;
  action: ComposerAction;
  composer: HTMLElement;
  host: HTMLElement;
  context?: PostContext;
  /** The precise post element captured when Starling opened this composer.
   *  This remains reliable when X temporarily omits a permalink or status id
   *  from its rendered reply surface. */
  sourceArticle?: HTMLElement;
  control: HTMLButtonElement;
  requestId: number;
  composing: boolean;
  writing: boolean;
  /** Which write last raised `writing`, so an earlier write's frame cannot lower it early. */
  writeToken?: number;
  inputRevision: number;
  disposed: boolean;
  abortController: AbortController;
  generationId?: string;
  candidateId?: string;
  lastRecordedText?: string;
  editTimer?: number;
  cancelActive?: () => void;
  activeOriginal?: string;
  activeInputRevision?: number;
  activeOpId?: string;
  placementFrame?: number;
  /** X's Post/Reply button this pill is anchored to; scans re-check placement
   *  against it because X mounts the counter/+ a beat AFTER the input event. */
  nativePostButton?: HTMLElement;
  /** What Starling last typed here and what it replaced, for Undo and "Try another". */
  undo?: ComposerUndo;
  /** When the pill last re-read the surface it sits on (X can switch theme with no reload). */
  themeCheckedAt?: number;
}

/** The exact X surface a Starling-initiated action is allowed to use. Keeping
 * this with the wait prevents a reply/quote from landing in another visible
 * composer just because it happens to have the same action type. */
interface ComposerSessionTarget {
  context?: PostContext;
  sourceArticle?: HTMLElement;
  existing?: ReadonlySet<ComposerSession>;
}

interface CollapsedComposerLauncher {
  action: ComposerAction;
  sourceArticle?: HTMLElement;
  surface: HTMLElement;
  submit: HTMLElement;
  host: HTMLElement;
  control: HTMLButtonElement;
  abortController: AbortController;
  /** Live operation started by this launcher; re-clicks are no-ops while it runs. */
  activeOpId?: string;
  placementFrame?: number;
}

let bridgeState: BridgeState = initialBridgeState();
let pageDisabled = false;
let currentPageKey = pagePreferenceKey(location.href);
let currentPageUrl = location.href;
let scanFrame: number | null = null;
let passiveCaptureTimer: number | null = null;
const passiveCandidateAttempts = new Map<string, CandidateAttempt>();
const pendingPublicationProofs = new Map<string, PendingPublicationProof>();
let activeContext: PostContext | undefined;
// The post's avatar for the dock's context chip. Client-only on purpose: the
// engine's PostContext schema is strict, so this never rides along with it.
let activeContextAvatar: string | undefined;
// Avatar paired to its exact context object — the chip reads ONLY this, so a
// leftover avatar from a previously viewed post can never appear on an
// unrelated "New post" or another post's chip.
const contextAvatars = new WeakMap<PostContext, string>();
let chatShadow: ShadowRoot | null = null;
let chatHost: HTMLElement | null = null;
let navButton: HTMLButtonElement | null = null;
let pendingComposerIntent: ComposerIntent | null = null;
let pendingQuoteIntent: QuoteIntent | null = null;
// A programmatic click on X's own allowed control bubbles through the same
// capture handler as a user's click. Hold its exact target synchronously so
// that handler cannot replace Starling's richer post/context claim with a
// generic one (notably the top-level Create post control has no article).
let starlingNativeClickTarget: HTMLElement | null = null;
// First-party channel: the user clicked Starling's OWN launcher pill, so the
// action/surface/submit are certain. Kept separate from pendingComposerIntent
// so the strict fail-closed matrix for native X clicks stays untouched.
interface LauncherIntent {
  intent: ComposerIntent;
  surface: HTMLElement;
  submit: HTMLElement;
  opId: string;
  expiresAt: number;
}
let pendingLauncherIntent: LauncherIntent | null = null;
let composerSessions = new WeakMap<HTMLElement, ComposerSession>();
const activeComposerSessions = new Set<ComposerSession>();
const collapsedComposerLaunchers = new WeakMap<HTMLElement, CollapsedComposerLauncher>();
const activeCollapsedComposerLaunchers = new Set<CollapsedComposerLauncher>();
// ---------------------------------------------------------------------------
// Assist stream transport: one long-lived port to the background worker, one
// reducer per request. Streamed tokens are DISPLAY-ONLY; only the gated final
// response acts. The worker owns the network and the device token.
// ---------------------------------------------------------------------------
interface StreamHandlers {
  onUpdate: (state: StreamState) => void;
  /** After a Stop once text had appeared: what the server says the stop cost, when it says it. */
  onStopped?: (outcome: StoppedOutcome) => void;
}
let assistPort: chrome.runtime.Port | null = null;
const activeStreams = new Map<string, { state: StreamState; handlers: StreamHandlers }>();
/**
 * Streams stopped after text had appeared. The card settled at once; the worker read on for the
 * server's final answer, which says whether the stop used a draft (`used_after_stop`).
 */
const stoppedStreams = new Map<string, { onStopped: (outcome: StoppedOutcome) => void; timer: number }>();
/** Longer than the worker's own wait (10 s), so its answer is never missed here. */
const STOPPED_LISTEN_MS = 15_000;

function settleStoppedStream(requestId: string, event: AssistPortEvent) {
  const stopped = stoppedStreams.get(requestId);
  if (!stopped) return;
  const outcome = stoppedStreamOutcome(event);
  if (!outcome) return;
  window.clearTimeout(stopped.timer);
  stoppedStreams.delete(requestId);
  try {
    stopped.onStopped(outcome);
  } catch {
    /* the card that asked is gone */
  }
}
let activeScheduleActions = 0;
/** While any stream runs, a ping this often keeps the background worker awake. */
const STREAM_PING_MS = 20_000;
let streamPingTimer: number | null = null;

function syncStreamPing() {
  if (activeStreams.size > 0 && streamPingTimer == null) {
    streamPingTimer = window.setInterval(() => {
      try { assistPort?.postMessage({ type: "ping" }); } catch { /* the port's disconnect settles the streams */ }
    }, STREAM_PING_MS);
  } else if (activeStreams.size === 0 && streamPingTimer != null) {
    window.clearInterval(streamPingTimer);
    streamPingTimer = null;
  }
}

/** Said on a card whose request could not start because this copy of Starling was replaced. */
const UPDATED_LINE = "Starling was updated. Reload X to switch to the new version.";

function ensureAssistPort(): chrome.runtime.Port {
  if (assistPort) return assistPort;
  assistPort = chrome.runtime.connect({ name: "starling-assist" });
  assistPort.onMessage.addListener((message: unknown) => {
    if (!isRecord(message) || typeof message.requestId !== "string") return;
    const entry = activeStreams.get(message.requestId);
    if (!entry) {
      settleStoppedStream(message.requestId, message as AssistPortEvent);
      return;
    }
    entry.state = reduceStream(entry.state, message as AssistPortEvent);
    entry.handlers.onUpdate(entry.state);
    if (entry.state.phase === "done" || entry.state.phase === "error") {
      activeStreams.delete(message.requestId);
      syncStreamPing();
    }
  });
  assistPort.onDisconnect.addListener(() => {
    assistPort = null;
    // A stopped stream's answer can no longer arrive; its card keeps saying only "Stopped."
    for (const stopped of stoppedStreams.values()) window.clearTimeout(stopped.timer);
    stoppedStreams.clear();
    // An update or reload of the extension cuts the port too. That is not a dropped connection,
    // and "try again" cannot fix it: only reloading X can.
    const orphaned = handleOrphaned();
    for (const [requestId, entry] of [...activeStreams]) {
      entry.state = reduceStream(entry.state, orphaned
        ? { type: "error", requestId, retryable: false, error: UPDATED_LINE }
        : {
          type: "error", requestId, retryable: true, code: "interrupted",
          error: "The connection to Starling ended before the draft arrived. Nothing in X changed.",
        });
      entry.handlers.onUpdate(entry.state);
      activeStreams.delete(requestId);
    }
    syncStreamPing();
  });
  return assistPort;
}

function startAssist(payload: Record<string, unknown>, handlers: StreamHandlers): { requestId: string; cancel: () => void } {
  const requestId = newRequestId();
  const entry = { state: initialStreamState(requestId), handlers };
  activeStreams.set(requestId, entry);
  noteStarlingRequest();
  try {
    if (handleOrphaned()) throw new Error("orphaned");
    ensureAssistPort().postMessage({ type: "start", requestId, payload });
    syncStreamPing();
  } catch {
    // The extension was updated or reloaded underneath this page.
    handleOrphaned();
    entry.state = reduceStream(entry.state, {
      type: "error", requestId, retryable: false, error: UPDATED_LINE,
    });
    handlers.onUpdate(entry.state);
    activeStreams.delete(requestId);
  }
  return {
    requestId,
    cancel: () => {
      try { assistPort?.postMessage({ type: "cancel", requestId }); } catch { /* gone */ }
      const active = activeStreams.get(requestId);
      if (active) {
        // Text had appeared: the worker waits for the server to say what stopping cost.
        const shown = Boolean(active.state.preview);
        active.state = reduceStream(active.state, {
          type: "final", requestId,
          response: { ok: false, error: "Stopped.", cancelled: true }
        });
        active.handlers.onUpdate(active.state);
        activeStreams.delete(requestId);
        const onStopped = active.handlers.onStopped;
        if (shown && onStopped) {
          stoppedStreams.set(requestId, {
            onStopped,
            timer: window.setTimeout(() => stoppedStreams.delete(requestId), STOPPED_LISTEN_MS),
          });
        }
        syncStreamPing();
      }
    }
  };
}

// ---------------------------------------------------------------------------
// The line to the worker. Every message goes through `sendToWorker`, which checks first whether this
// copy of Starling was left behind by an update (then no message can get through, and the page says
// so once instead of failing every button with Chrome's raw "Extension context invalidated.").
// ---------------------------------------------------------------------------

/** True once this script has noticed it was orphaned and shown the "Starling was updated" card. */
let orphanNoticed = false;

function handleOrphaned(): boolean {
  if (!isContextOrphaned(() => chrome.runtime)) return false;
  if (!orphanNoticed) {
    orphanNoticed = true;
    extensionReloaded = true;
    // Deferred: this runs inside click handlers and catch blocks that are still mid-way through.
    queueMicrotask(showOrphanedState);
  }
  return true;
}

async function sendToWorker(message: Record<string, unknown>): Promise<unknown> {
  if (handleOrphaned()) return null;
  noteStarlingRequest();
  try {
    return await chrome.runtime.sendMessage(message);
  } catch {
    handleOrphaned();
    return null;
  }
}

// Keeping the service warm. The engine that writes drafts stops after ten quiet minutes, and a tab
// left open never asks for anything by itself, so the next ✦ after a long read paid a cold start of
// about three seconds. Asking the worker for the account state when the customer comes back to the
// page (or points at a ✦) wakes it early. The worker's own 60 s session cache decides whether that
// reaches the server; nothing here polls.
const WARM_AFTER_IDLE_MS = 5 * 60_000;
const WARM_HOVER_THROTTLE_MS = 30_000;
let lastStarlingRequestAt = Date.now();
let lastHoverWarmAt = 0;

function noteStarlingRequest() {
  lastStarlingRequestAt = Date.now();
}

function warmUp(reason: "activity" | "hover") {
  if (!canRequest(bridgeState, pageGate()) || extensionReloaded) return;
  const now = Date.now();
  if (reason === "activity" && now - lastStarlingRequestAt < WARM_AFTER_IDLE_MS) return;
  if (reason === "hover" && now - lastHoverWarmAt < WARM_HOVER_THROTTLE_MS) return;
  lastHoverWarmAt = now;
  void loadBridgeState();
}

export default defineContentScript({
  matches: ["https://x.com/*", "https://twitter.com/*"],
  runAt: "document_idle",
  main() {
    void initialize();
  }
});

async function initialize() {
  await Promise.all([loadPagePreference(), loadBridgeState(), loadDockSettings(), loadPendingPublicationProofs(), loadNearLimitShown(), loadFirstRun()]);
  applyMountState();
  if (openDockOnLoad) {
    // This tab was reloaded to bring Starling in (the toolbar icon, or "Open X" on the welcome page).
    openDockOnLoad = false;
    if (canMountDock(bridgeState, pageGate())) openDock({ focusInput: true });
  }

  // Only mutations that can change what Starling mounts are worth a scan: a post, a composer, a
  // button, a dialog, the nav, or one of Starling's own controls being taken away by X's re-render.
  // X mutates the page constantly (counters, timestamps, hover cards); scanning for every one of
  // them measured the whole timeline many times a second for nothing.
  const observer = new MutationObserver(records => {
    flushDetachedComposerEdits();
    if (records.some(isRelevantMutation)) scheduleScan();
  });
  observer.observe(document.documentElement, { childList: true, subtree: true });

  // Scanning was mutation-driven ONLY, and scanPostButtons deliberately skips
  // posts that are off screen. Scrolling over rows X has already rendered
  // mutates nothing, so those posts scrolled into view with no ✦ and no
  // paperclip and stayed that way until something else happened to change the
  // DOM — the "some posts just don't have the buttons" gap. While scrolling, a
  // scan runs at most once every 150 ms: a post needs a ✦ by the time the eye
  // lands on it, not on every frame of the fling.
  addEventListener("scroll", event => {
    // Capture phase also sees the dock's own feed scrolling (shadow events
    // retarget to the host). Rescanning X's timeline because a chat card
    // streamed a line is pure waste — ignore anything inside Starling's UI.
    const target = event.target;
    if (target instanceof Node && (chatHost?.contains(target) || target === chatHost)) return;
    warmUp("activity");
    scheduleScrollScan();
  }, { passive: true, capture: true });
  addEventListener("resize", scheduleScan, { passive: true });

  chrome.runtime.onMessage.addListener((message: unknown, _sender, sendResponse) => {
    if (!isRecord(message) || typeof message.kind !== "string") return;

    // "Is Starling running in this tab?" Answering at all is the answer; an orphan cannot hear it.
    if (message.kind === BROADCAST_KINDS.ping) {
      sendResponse({ ok: true });
      return;
    }

    if (message.kind === BROADCAST_KINDS.bridgeState && isBridgeState(message.state)) {
      bridgeState = message.state;
      applyMountState();
      return;
    }

    if (message.kind === BROADCAST_KINDS.toggleSummon) {
      // Answered first: the worker reloads a tab whose Starling does not answer.
      sendResponse({ ok: true });
      if (isBridgeState(message.state)) bridgeState = message.state;
      // THE TOOLBAR ICON IS THE UN-HIDE, because it is the only "toolbar
      // control" there is and it is what the docs point at to bring Starling
      // back. Refusing here made "turn off on this page" one-way and permanent:
      // the key went into chrome.storage.local and nothing anywhere removed it,
      // so the footer, the panel and the nav item were all gone for that page
      // forever, and clicking the icon just re-showed the status banner. The
      // keyboard shortcut arrives here too (it is the toolbar click).
      if (pageDisabled) {
        void enableStarlingOnThisPage();
        return;
      }
      if (isExcludedRoute(location.pathname)) {
        showPageNotice("Starling stays off on direct messages, chat, settings and sign-in pages.");
        return;
      }
      if (message.mode === "open") openDock({ focusInput: true });
      else toggleDock();
      return;
    }

    // Another X tab hid or showed Starling on a page, or changed a setting.
    // The background relays both, because this script never listens to
    // storage changes: a change event would carry the device token here too.
    if (message.kind === BROADCAST_KINDS.pagePreferenceChanged) {
      void loadPagePreference().then(applyMountState);
      return;
    }

    if (message.kind === BROADCAST_KINDS.settingsChanged) {
      void loadDockSettings().then(() => {
        applyDockTheme();
        if (settingsOpen) renderSettings();
      });
    }
  });

  // SPA navigation detection only — the old 1s command poll was a no-op.
  setInterval(() => {
    const nextPageKey = pagePreferenceKey(location.href);
    if (nextPageKey !== currentPageKey) {
      const previousPageUrl = currentPageUrl;
      currentPageKey = nextPageKey;
      currentPageUrl = location.href;
      const retainComposerIntent = pendingComposerIntent != null
        && shouldRetainComposerIntentOnNavigation(pendingComposerIntent.action, previousPageUrl, currentPageUrl);
      if (retainComposerIntent) {
        // A native Quote/Post click can navigate X to its own compose route.
        // Keep only the exact pending composer claim for that expected route;
        // menu and launcher state must never cross a navigation boundary.
        pendingQuoteIntent = null;
        pendingLauncherIntent = null;
      } else {
        clearComposerIntents();
      }
      void loadPagePreference().then(applyMountState);
      reconcileOpenDockAfterNavigation();
    }
    // The 2.5 s safety net: whatever the mutation filter and the scroll throttle let through.
    if (canScanPosts(bridgeState, pageGate())) scheduleScan();
    // Before consent, and on a page where Starling is off, X can still re-render its nav and take
    // Starling's item with it; that item is the only way back.
    else if (!extensionReloaded) ensureStarlingNavControl();
  }, 2_500);

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) return;
    // Back on this tab after an update in another one: say so now, not on the next failed click.
    if (handleOrphaned()) return;
    void loadBridgeState().then(() => {
      applyMountState();
      // Confirming the email, paying, choosing a plan: all of it happens in another tab. Coming
      // back is when it matters, so ask the server now instead of waiting out the worker's cache.
      const returning = openedSitePage;
      openedSitePage = false;
      if (returning || isRecoverableState(dockStateFor(dockInputsFrom(bridgeState)))) void refreshSession();
    });
    scheduleScan();
  });
  // No page-level shortcut any more: "Open or close Starling" is a Chrome command (Alt+Shift+S by
  // default, MacCtrl+Shift+S on a Mac) that arrives as the toolbar click. The old Alt+F never fired
  // on a Mac and fought Chrome's own menu key everywhere else.
  document.addEventListener("keydown", () => warmUp("activity"), { capture: true, passive: true });
  document.addEventListener("click", captureComposerIntent, true);
  document.addEventListener("pointerdown", flushPublishedComposerEdit, true);
  document.addEventListener("keydown", flushPublishedComposerEdit, true);
  document.addEventListener("input", scheduleScan, true);
  document.addEventListener("compositionend", scheduleScan, true);
  window.addEventListener("pagehide", flushAllComposerEdits);
  window.addEventListener("scroll", schedulePassiveCapture, { passive: true });
  // Offline is known locally at once; back online, ask for a fresh session.
  window.addEventListener("offline", () => {
    bridgeState = { ...bridgeState, online: false };
    applyMountState();
  });
  window.addEventListener("online", () => {
    bridgeState = { ...bridgeState, online: true };
    applyMountState();
    void refreshSession();
  });
}

/**
 * The dock's own keyboard behaviour. It lives here rather than on the textarea
 * because the early content script stops these keys during capture (so X can
 * never see them), which means they never reach the field's own listeners.
 */
function handleDockKeydown(event: KeyboardEvent) {
  if (event.key === "Enter" && !event.shiftKey) {
    // Settings has its own text fields. Without this the chat form would
    // submit from inside one of them.
    if (settingsOpen) return;
    event.preventDefault();
    chatShadow?.querySelector<HTMLFormElement>("form")?.requestSubmit();
  } else if (event.key === "Escape") {
    // One layer at a time: an open dropdown, then Settings, then Starling.
    if (closeOpenDropdown) {
      closeOpenDropdown();
      return;
    }
    if (settingsOpen) {
      closeSettings();
      return;
    }
    closeDock();
  }
}

/** The worker asked for the panel to open once this tab has loaded (it reloaded the tab to show it). */
let openDockOnLoad = false;
/** This tab opened a website page (pricing, billing, account): the account is read again on return. */
let openedSitePage = false;

async function loadBridgeState() {
  const response = await sendToWorker({ kind: MESSAGE_KINDS.getBridgeState });
  if (isRecord(response) && isBridgeState(response.state)) {
    bridgeState = response.state;
    if (response.open_dock === true) openDockOnLoad = true;
  }
}

/** Ask the worker for a fresh session (dock open, back online); the broadcast updates every tab. */
async function refreshSession() {
  if (!canRequest(bridgeState, pageGate()) && !(bridgeState.consent.accepted && bridgeState.signed_in)) return;
  const response = await sendToWorker({ kind: MESSAGE_KINDS.refreshSession });
  if (isRecord(response) && isBridgeState(response.state)) {
    bridgeState = response.state;
    applyMountState();
  }
}

async function loadPagePreference() {
  currentPageKey = pagePreferenceKey(location.href);
  currentPageUrl = location.href;
  let stored: Record<string, unknown> = {};
  try {
    stored = await pagePrefs.get(DISABLED_PAGES_KEY);
  } catch {
    handleOrphaned();
  }
  const disabledPages = Array.isArray(stored[DISABLED_PAGES_KEY]) ? stored[DISABLED_PAGES_KEY] as string[] : [];
  pageDisabled = currentPageKey != null && disabledPages.includes(currentPageKey);
}

function pageGate(): PageGate {
  return { pageDisabled, pathname: location.pathname };
}

/**
 * Apply the bridge state and this page to what Starling shows.
 *
 * - A page Starling never touches (direct messages, chat, settings, sign-in
 *   flows): nothing at all.
 * - A page the customer turned Starling off on: nothing but a dimmed
 *   "Starling (off here)" in X's sidebar, which is the way back.
 * - No consent yet: no buttons, no scanning, no capture. The nav item and the
 *   dock still open (and the dock shows the notice); the nav item reads
 *   nothing from the page.
 * - Otherwise scan and mount; the dock's state card says what is missing.
 * - Left behind by an update: nothing that could fail, and one card saying so.
 *
 * A network failure never tears the UI down: the dock says what happened.
 */
function applyMountState() {
  const page = pageGate();
  if (extensionReloaded) {
    cancelPassiveCapture();
    removeInjectedControls();
    updateDockState();
    return;
  }
  if (!canMountDock(bridgeState, page)) {
    cancelPassiveCapture();
    removeStarlingUi();
    if (pageDisabled && !isExcludedRoute(page.pathname)) ensureStarlingNavControl();
    return;
  }
  if (!canScanPosts(bridgeState, page)) {
    cancelPassiveCapture();
    removeInjectedControls({ keepNav: true });
    ensureStarlingNavControl();
    updateDockState();
    return;
  }
  scheduleScan();
  updateDockState();
  maybeShowFirstRun();
}

/** Does this batch of DOM changes add or take away anything Starling mounts beside? */
const RELEVANT_ADDED = 'article, [contenteditable="true"], button, [role="button"], [role="dialog"], [role="menuitem"], nav, [data-testid="toolBar"], [data-testid="sidebarColumn"], [data-testid^="tweetButton"], [data-testid="primaryColumn"], h1, h2, [role="heading"]';
const STARLING_HOSTS = `#${CHAT_HOST_ID}, #${NAV_HOST_ID}, [data-starling-post-control], [data-starling-composer-control], [data-starling-collapsed-composer-launcher]`;

function isRelevantMutation(record: MutationRecord): boolean {
  for (const node of record.addedNodes) {
    if (!(node instanceof Element)) continue;
    // Starling's own additions are never a reason to scan again.
    if (node.matches(STARLING_HOSTS)) continue;
    if (node.matches(RELEVANT_ADDED) || node.querySelector(RELEVANT_ADDED)) return true;
  }
  for (const node of record.removedNodes) {
    if (!(node instanceof Element)) continue;
    if (node.matches(STARLING_HOSTS) || node.querySelector(STARLING_HOSTS)) return true;
  }
  return false;
}

const SCROLL_SCAN_MS = 150;
let lastScrollScanAt = 0;
let scrollScanTimer: number | null = null;

/** A scan from scrolling: at most one every 150 ms, and always one at the end of the scroll. */
function scheduleScrollScan() {
  const now = Date.now();
  const wait = SCROLL_SCAN_MS - (now - lastScrollScanAt);
  if (wait <= 0) {
    lastScrollScanAt = now;
    scheduleScan();
    return;
  }
  if (scrollScanTimer != null) return;
  scrollScanTimer = window.setTimeout(() => {
    scrollScanTimer = null;
    lastScrollScanAt = Date.now();
    scheduleScan();
  }, wait);
}

// Lazily initialized — module scope runs under Node during the WXT build,
// where `location` does not exist.
let lastDockContextPath: string | null = null;

function scheduleScan() {
  if (scanFrame != null || extensionReloaded || !canScanPosts(bridgeState, pageGate())) return;
  scanFrame = requestAnimationFrame(() => {
    scanFrame = null;
    if (extensionReloaded) return;
    // The signed-in account is read once per scan, not once per post: the account switcher's text
    // is a layout read, and a scan used to do it up to 25 times.
    scanIdentity = getSignedInIdentity();
    refreshHostThemesIfPageChanged();
    // STRICT context chip: navigating away from the post makes any reply/quote
    // context stale — clear it unless a generation is actively using it.
    if (lastDockContextPath == null) lastDockContextPath = location.pathname;
    else if (location.pathname !== lastDockContextPath) {
      lastDockContextPath = location.pathname;
      const busy = [...opBindings.values()].some(binding =>
        binding.op.kind === "enhance" && !isTerminal(binding.op));
      if (!busy) clearDockContext();
    }
    flushDetachedComposerEdits();
    ensureStarlingNavControl();
    ensureDockSidebarMount();
    scanComposerEnhancers();
    scanCollapsedComposerLaunchers();
    scanPostButtons();
    schedulePassiveCapture();
  });
}

/** The identity read at the start of the current scan (see scheduleScan). */
let scanIdentity: SignedInIdentity | null = null;
/** X's page tone at the last scan; when it changes, every host Starling drew re-reads its surface. */
let lastPageTone: SurfaceTone | null = null;

/**
 * X switches between Default, Dim and Lights out with no reload. Everything Starling drew inside X's
 * own chrome (the nav item, each ✦, the composer pills) chose its skin when it mounted, so a switch
 * left them wearing the old one: dark chips in a white toolbar. One body read per scan notices.
 */
function refreshHostThemesIfPageChanged() {
  const tone = surfaceTone(document.body);
  if (tone === lastPageTone) return;
  const first = lastPageTone === null;
  lastPageTone = tone;
  if (first) return;
  const nav = document.getElementById(NAV_HOST_ID);
  if (nav) nav.dataset.theme = isDarkSurface(document.body) ? "dark" : "light";
  document.querySelectorAll<HTMLElement>("[data-starling-post-control]").forEach(host => {
    const article = host.closest<HTMLElement>(POST_SELECTOR);
    host.dataset.theme = isDarkSurface(article ?? document.body) ? "dark" : "light";
  });
  for (const session of activeComposerSessions) session.themeCheckedAt = undefined;
  for (const launcher of activeCollapsedComposerLaunchers) {
    launcher.host.dataset.theme = pillThemeFor(surfaceTone(launcher.surface));
  }
}

function schedulePassiveCapture() {
  if (passiveCaptureTimer != null || document.hidden || !canRequest(bridgeState, pageGate())) return;
  passiveCaptureTimer = window.setTimeout(() => {
    passiveCaptureTimer = null;
    void capturePassiveSnapshots();
  }, PASSIVE_CAPTURE_DELAY_MS);
}

function cancelPassiveCapture() {
  if (passiveCaptureTimer == null) return;
  clearTimeout(passiveCaptureTimer);
  passiveCaptureTimer = null;
}

async function capturePassiveSnapshots() {
  const page = pageGate();
  if (document.hidden || !canRequest(bridgeState, page)) return;
  const identity = getSignedInIdentity();
  if (!identity) return;

  // Confirming a post the user published from a draft is covered by the main
  // consent. Everything else here is passive and needs the separate opt-in.
  const passive = canCapturePassive(bridgeState, page);
  const profile = passive ? extractOwnProfileSnapshot(identity) : null;
  const submissions: Promise<unknown>[] = [];
  if (profile && shouldAttemptPassiveCandidate(`profile:${identity.handle}`, profileSnapshotFingerprint(profile))) {
    submissions.push(sendToWorker({
      kind: MESSAGE_KINDS.captureProfile,
      payload: profile
    }).catch(() => undefined));
  }

  capturePendingPublications(identity, submissions);

  if (passive) {
    const articles = Array.from(document.querySelectorAll<HTMLElement>(POST_SELECTOR))
      .filter(article => article.isConnected && isInViewport(article))
      .slice(0, MAX_VISIBLE_METRIC_CANDIDATES);
    for (const article of articles) {
      const metric = extractOwnMetricSnapshot(article, identity, profile?.followers_count);
      if (!metric || !shouldAttemptPassiveCandidate(`post:${metric.post_id}`, metricSnapshotFingerprint(metric))) continue;
      submissions.push(sendToWorker({
        kind: MESSAGE_KINDS.captureMetric,
        purpose: "passive",
        payload: metric
      }).catch(() => undefined));
    }
  }
  await Promise.allSettled(submissions);
}

function serialisePendingPublicationProof(proof: PendingPublicationProof): StoredPendingPublicationProof {
  return {
    ...proof,
    // Chrome storage cannot serialize a Set. Persist only status ids that
    // were already visible BEFORE the manual click; those prevent an old post
    // with matching text from being mistaken for a new publication.
    visiblePostIdsBeforeSubmit: [...proof.visiblePostIdsBeforeSubmit],
  };
}

function restorePendingPublicationProof(value: unknown): PendingPublicationProof | null {
  if (!isRecord(value)
    || typeof value.generationId !== "string"
    || !["reply", "quote", "post"].includes(String(value.action))
    || typeof value.text !== "string"
    || !Array.isArray(value.visiblePostIdsBeforeSubmit)
    || typeof value.expiresAt !== "number") return null;
  if (value.expiresAt <= Date.now()) return null;
  return {
    generationId: value.generationId,
    candidateId: typeof value.candidateId === "string" ? value.candidateId : undefined,
    action: value.action as ComposerAction,
    text: value.text,
    context: isRecord(value.context) ? value.context as PostContext : undefined,
    visiblePostIdsBeforeSubmit: new Set(value.visiblePostIdsBeforeSubmit.filter((postId): postId is string => typeof postId === "string")),
    expiresAt: value.expiresAt,
    reportedPostId: typeof value.reportedPostId === "string" ? value.reportedPostId : undefined,
  };
}

async function loadPendingPublicationProofs() {
  try {
    const stored = await pagePrefs.get(PENDING_PUBLICATIONS_KEY);
    const witnesses = stored[PENDING_PUBLICATIONS_KEY];
    if (!isRecord(witnesses)) return;
    for (const value of Object.values(witnesses)) {
      const proof = restorePendingPublicationProof(value);
      if (proof) pendingPublicationProofs.set(proof.generationId, proof);
    }
  } catch {
    // Persistence is a recovery aid, never a precondition for a user-posted
    // reply. The in-memory watcher still covers the normal same-tab path.
  }
}

/** Read-modify-write the one witness map, dropping expired entries on the way. */
async function updatePendingPublications(change: (witnesses: Record<string, unknown>) => void) {
  const stored = await pagePrefs.get(PENDING_PUBLICATIONS_KEY);
  const current = stored[PENDING_PUBLICATIONS_KEY];
  const witnesses: Record<string, unknown> = isRecord(current) ? { ...current } : {};
  change(witnesses);
  for (const [generationId, value] of Object.entries(witnesses)) {
    if (!restorePendingPublicationProof(value)) delete witnesses[generationId];
  }
  if (Object.keys(witnesses).length) await pagePrefs.set({ [PENDING_PUBLICATIONS_KEY]: witnesses });
  else await pagePrefs.remove(PENDING_PUBLICATIONS_KEY);
}

function persistPendingPublicationProof(proof: PendingPublicationProof) {
  void updatePendingPublications(witnesses => {
    witnesses[proof.generationId] = serialisePendingPublicationProof(proof);
  }).catch(() => undefined);
}

function forgetPendingPublicationProof(generationId: string) {
  pendingPublicationProofs.delete(generationId);
  void updatePendingPublications(witnesses => {
    delete witnesses[generationId];
  }).catch(() => undefined);
}

function capturePendingPublications(identity: SignedInIdentity, submissions: Promise<unknown>[]) {
  if (!pendingPublicationProofs.size) return;
  const now = Date.now();
  const articles = Array.from(document.querySelectorAll<HTMLElement>(POST_SELECTOR)).slice(0, 80);
  for (const [generationId, proof] of pendingPublicationProofs) {
    if (proof.expiresAt <= now) {
      forgetPendingPublicationProof(generationId);
      continue;
    }
    const match = articles.find(article => {
      const postId = postIdFromUrl(findPermalink(article)?.href);
      if (!postId || proof.visiblePostIdsBeforeSubmit.has(postId) || proof.reportedPostId === postId) return false;
      if (extractAuthorHandle(article) !== identity.handle || inferPassivePostType(article) !== proof.action) return false;
      const visibleText = findPrimaryTextElement(article)?.innerText.trim() || "";
      return sameComposerText(visibleText, proof.text);
    });
    if (!match) continue;
    const snapshot = extractOwnMetricSnapshot(match, identity);
    if (!snapshot) continue;
    const postId = snapshot.post_id;
    proof.reportedPostId = postId;
    const targetId = proof.action === "reply" ? (proof.context?.post_id ?? postIdFromUrl(proof.context?.post_url)) : undefined;
    const payload: MetricSnapshotPayload = targetId ? {
      ...snapshot,
      metadata: {
        ...snapshot.metadata,
        parent_post_id: targetId,
        parent_text: proof.context?.text || snapshot.metadata.parent_text,
        // THE HANDLE, NOT THE DISPLAY NAME — and this is the writer that
        // actually fires. `extractContext` sets `author` to the human-readable
        // name and `author_handle` to the @handle; this took `author`, so the
        // only values ever written to `parent_author` were 'Grok', 'Sam Altman'
        // and 'Tibo'. Every consumer reads it as a handle —
        // `relationships.reply_target` calls `.lstrip("@")` on it, which is
        // meaningless on a name with a space in it.
        //
        // The passive writer at :1195 was repaired for this months ago and
        // fires 0 times out of 4 on real replies (its parent extraction is
        // gated to /status/ pages); THIS one fires 3 of 3. The fix landed on
        // the path that never runs, so the bug survived its own repair — which
        // is why both writers had to change in one edit. Rebuilding with only
        // :1195 would have produced no observable change and certified it.
        //
        // `.replace(/^@/, "")` because `author_handle` carries the "@" and the
        // passive writer stores it stripped; two writers, one shape.
        parent_author: proof.context?.author_handle?.replace(/^@/, "")
          || snapshot.metadata.parent_author,
        parent_display_name: proof.context?.author || snapshot.metadata.parent_display_name,
        parent_post_url: proof.context?.post_url || snapshot.metadata.parent_post_url,
      },
    } : snapshot;
    submissions.push(sendToWorker({
      kind: MESSAGE_KINDS.captureMetric, purpose: "publication",
      // The server decides consent from `capture_mode`: a publication proof needs only the main disclosure.
      payload: { ...payload, metadata: { ...payload.metadata, capture_mode: "publication_proof" } },
    }).then(response => {
      // The background reports failures as `{ ok: false }` rather than
      // rejecting. Treat only the server's explicit receipt as proof it was
      // saved; otherwise retain this witness for the next pass.
      if (isRecord(response) && response.ok === true) {
        forgetPendingPublicationProof(generationId);
        return;
      }
      const live = pendingPublicationProofs.get(generationId);
      if (live?.reportedPostId === postId) live.reportedPostId = undefined;
    }).catch(() => {
      const live = pendingPublicationProofs.get(generationId);
      if (live?.reportedPostId === postId) live.reportedPostId = undefined;
    }));
  }
}

// Once we have seen who is signed in, REMEMBER it. Both DOM probes below are
// layout-dependent: on a compact viewport, mid-navigation, or in one of X's A/B
// layouts they can both come back empty for a moment. When that happened
// `isOwnPost` silently became false and the user's own post was treated as a
// stranger's — which is exactly the "it doesn't know it's my account" fault.
// A signed-in handle does not change between page loads, so a remembered one is
// strictly better evidence than a failed lookup.
let cachedIdentity: SignedInIdentity | null = null;

function getSignedInIdentity(): SignedInIdentity | null {
  const found = readSignedInIdentityFromDom();
  if (found) {
    cachedIdentity = found;
    return found;
  }
  return cachedIdentity;
}

function readSignedInIdentityFromDom(): SignedInIdentity | null {
  const switcher = document.querySelector<HTMLElement>('[data-testid="SideNav_AccountSwitcher_Button"]');
  if (switcher) {
    const lines = switcher.innerText.split("\n").map(line => line.trim()).filter(Boolean);
    const handleLine = lines.filter(line => /^@[A-Za-z0-9_]{1,15}$/.test(line)).at(-1);
    const handle = normalizeHandle(handleLine);
    if (handle) {
      const displayName = lines.find(line => !line.startsWith("@") && !/^(account|accounts|more)$/i.test(line));
      return { handle, displayName };
    }
  }
  // X occasionally omits the desktop account switcher (compact/mobile and
  // some A/B layouts). Its own Profile tab still points only at the signed-in
  // account, so use that narrow first-party fallback rather than abandoning
  // visible-reply capture altogether.
  const profileLink = document.querySelector<HTMLAnchorElement>('a[data-testid="AppTabBar_Profile_Link"][href]');
  const profileHandle = profileLink?.getAttribute("href")?.match(/^\/([A-Za-z0-9_]{1,15})\/?$/)?.[1];
  const handle = normalizeHandle(profileHandle);
  return handle ? { handle } : null;
}

function extractOwnProfileSnapshot(identity: SignedInIdentity): ProfileSnapshotPayload | null {
  if (!isOwnProfilePage(identity.handle)) return null;
  const primary = document.querySelector<HTMLElement>('[data-testid="primaryColumn"], main[role="main"]');
  if (!primary) return null;
  const profileName = primary.querySelector<HTMLElement>('[data-testid="UserName"]');
  const nameLines = profileName?.innerText.split("\n").map(line => line.trim()).filter(Boolean) ?? [];
  const visibleHandle = normalizeHandle(nameLines.filter(line => /^@[A-Za-z0-9_]{1,15}$/.test(line)).at(-1) ?? identity.handle);
  if (!visibleHandle || visibleHandle !== identity.handle) return null;

  const displayName = nameLines.find(line => !line.startsWith("@") && !isTimestampLabel(line)) ?? identity.displayName;
  const bio = primary.querySelector<HTMLElement>('[data-testid="UserDescription"]')?.innerText.trim() || undefined;
  const followersCount = readProfileCount(primary, "followers");
  const followingCount = readProfileCount(primary, "following");
  const postsCount = readProfileCount(primary, "posts");
  const pinnedPostId = Array.from(primary.querySelectorAll<HTMLElement>(POST_SELECTOR)).flatMap(article => {
    if (!/(^|\n)pinned(\n|$)/i.test(article.innerText)) return [];
    const articleHandle = extractAuthorHandle(article);
    const postId = postIdFromUrl(findPermalink(article)?.href);
    return articleHandle === identity.handle && postId ? [postId] : [];
  })[0];

  return {
    captured_at: new Date().toISOString(),
    handle: identity.handle,
    display_name: displayName,
    bio,
    profile_url: `https://x.com/${identity.handle}`,
    pinned_post_id: pinnedPostId,
    followers_count: followersCount,
    following_count: followingCount,
    posts_count: postsCount,
    source: "extension",
    metadata: {
      is_own_profile: true,
      signed_in_handle: identity.handle,
      source_page: pageAddressForService(location.href),
      capture_mode: "passive_visible_page"
    }
  };
}

function extractOwnMetricSnapshot(
  article: HTMLElement,
  identity: SignedInIdentity,
  followersCount?: number
): MetricSnapshotPayload | null {
  // THIS LINE IS THE ONLY THING THAT MAKES "his posts" TRUE ANYWHERE.
  //
  // It reads like a local filter on a local function. It is not. The engine's
  // `recent_own_posts` (main.py:1471) is documented as "the user's most recent
  // AUTHORED posts" and its query is `SELECT text FROM posts WHERE deleted_at
  // IS NULL` — no author predicate at all. Six call sites trust that docstring
  // (main.py:437, :937, :1148, :1461, :2706, :3972), feeding duplication
  // checks, `analyze_context`'s recent_user_texts, and the material his voice
  // is measured from. Nothing downstream re-checks authorship, because this
  // gate is what guarantees it before a row is ever written.
  //
  // So widening it does not "capture more context" — it puts other people's
  // sentences into the corpus his drafts are calibrated against, silently, and
  // that is item 2's defect inverted: a stranger's writing entering his voice
  // instead of his entering theirs.
  //
  // There IS a good reason to want foreign posts: seeing an announcement on the
  // timeline at the moment it happens, which no RSS feed can deliver. That
  // wants a SIGHTING — {handle, post_id, text, seen_at}, own table, short TTL,
  // radar pool only — not a relaxed snapshot. A sighting is ephemeral signal; a
  // snapshot is durable evidence. Do not make one serve the other.
  const authorHandle = extractAuthorHandle(article);
  if (!authorHandle || authorHandle !== identity.handle) return null;
  const permalink = findPermalink(article);
  const postId = postIdFromUrl(permalink?.href);
  if (!postId || !permalink?.href) return null;
  // A visible post by the signed-in account is still proof of authorship when
  // X has not rendered engagement counts yet. Newly posted replies routinely
  // have no numeric labels, and treating that as "nothing to capture" left
  // their generation stuck at "edited" â€” so the duplicate guard had no
  // durable reply identity to consult on the next attempt.
  const metrics = extractVisibleMetrics(article) ?? {};
  const userName = findPrimaryUserName(article);
  const postText = findPrimaryTextElement(article)?.innerText.trim() || undefined;
  const media = renderedMediaStats(article);
  const postType = inferPassivePostType(article);
  const parent = postType === "reply" ? extractVisibleParentContext(article) : undefined;
  const visibility = inferVisibility(article, userName);
  const time = permalink.querySelector<HTMLTimeElement>("time")?.dateTime
    ?? article.querySelector<HTMLTimeElement>("time[datetime]")?.dateTime;

  return {
    post_id: postId,
    captured_at: new Date().toISOString(),
    followers_count: followersCount,
    ...metrics,
    source: "extension",
    metadata: {
      is_own_post: true,
      account_handle: identity.handle,
      author_handle: authorHandle,
      post_url: permalink.href,
      post_text: postText,
      parent_post_id: parent?.postId,
      parent_text: parent?.text,
      // `parent_author` is the HANDLE now, which is what every consumer already
      // assumed; the human-readable name travels beside it instead of instead
      // of it, and fills the `display_name` column that has been NULL since the
      // table was created.
      parent_author: parent?.authorHandle,
      parent_display_name: parent?.displayName,
      parent_post_url: parent?.url,
      media_count: media.count,
      has_image: media.hasImage,
      source_page: pageAddressForService(location.href),
      capture_mode: "passive_visible_post",
      visibility,
      post_type: postType,
      created_at: time && Number.isFinite(Date.parse(time)) ? time : undefined
    }
  };
}

function isOwnProfilePage(handle: string): boolean {
  const segments = location.pathname.split("/").filter(Boolean).map(segment => decodeURIComponent(segment));
  if (normalizeHandle(segments[0]) !== handle) return false;
  return segments.length === 1 || ["with_replies", "media", "likes", "highlights"].includes(segments[1]?.toLowerCase());
}

function readProfileCount(scope: HTMLElement, label: "followers" | "following" | "posts"): number | undefined {
  const headings = Array.from(scope.querySelectorAll<HTMLElement>('h2, [role="heading"]')).slice(0, 6);
  const candidates = label === "posts"
    ? [...headings, ...headings.flatMap(heading => [heading.parentElement, heading.parentElement?.parentElement])]
      .filter((element): element is HTMLElement => element instanceof HTMLElement)
    : Array.from(scope.querySelectorAll<HTMLElement>('a[href]'));
  const matching = candidates.find(element => new RegExp(`\\b${label}\\b`, "i").test(element.innerText));
  if (!matching) return undefined;
  const match = matching.innerText.replace(/,/g, "").match(new RegExp(`(\\d+(?:\\.\\d+)?\\s*[KMB]?)\\s+${label}\\b`, "i"));
  return match ? parseCompactNumber(match[1]) : undefined;
}

function findPrimaryUserName(article: HTMLElement): HTMLElement | null {
  return Array.from(article.querySelectorAll<HTMLElement>('[data-testid="User-Name"]')).find(element =>
    element.closest(POST_SELECTOR) === article
      && !element.closest('[data-testid="quoteTweet"], [role="blockquote"]')) ?? null;
}

function extractAuthorHandle(article: HTMLElement): string | null {
  const permalink = findPermalink(article)?.href;
  if (permalink) {
    try {
      const pathHandle = new URL(permalink).pathname.match(/^\/([^/]+)\/status\/\d+/)?.[1];
      const normalized = normalizeHandle(pathHandle);
      if (normalized && normalized !== "i") return normalized;
    } catch {
      // Fall through to X's visible author row.
    }
  }
  const userName = findPrimaryUserName(article);
  const handle = userName?.innerText.split("\n").map(line => line.trim())
    .filter(line => /^@[A-Za-z0-9_]{1,15}$/.test(line)).at(-1);
  return normalizeHandle(handle) || null;
}

function inferPassivePostType(article: HTMLElement): PostContext["post_type"] {
  if (article.querySelector('[role="progressbar"]')) return "poll";
  if (article.querySelector('[data-testid="quoteTweet"], [role="blockquote"]')) return "quote";
  if (/replying to/i.test(article.innerText)) return "reply";
  return "original";
}

function renderedMediaStats(article: HTMLElement): { count: number; hasImage: boolean } {
  const isOwnRenderedMedia = (element: Element) =>
    element.closest(POST_SELECTOR) === article && !element.closest('[data-testid="quoteTweet"], [role="blockquote"]');
  const images = Array.from(article.querySelectorAll('[data-testid="tweetPhoto"]')).filter(isOwnRenderedMedia);
  const videos = Array.from(article.querySelectorAll('video')).filter(isOwnRenderedMedia);
  const cards = Array.from(article.querySelectorAll('[data-testid="card.wrapper"]')).filter(isOwnRenderedMedia);
  // Metrics need only the visible shape of a post. Do not read image, video,
  // or card URLs, and do not send media content through the enhancement path.
  return { count: images.length + videos.length + cards.length, hasImage: images.length > 0 };
}

function isInViewport(element: HTMLElement): boolean {
  const rect = element.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0 && rect.bottom >= 0 && rect.top <= window.innerHeight;
}

function shouldAttemptPassiveCandidate(target: string, fingerprint: string): boolean {
  const now = Date.now();
  const previous = passiveCandidateAttempts.get(target);
  if (previous && (now - previous.attemptedAt < PASSIVE_ATTEMPT_INTERVAL_MS
    || (previous.fingerprint === fingerprint && now - previous.attemptedAt < UNCHANGED_CANDIDATE_TTL_MS))) {
    return false;
  }
  passiveCandidateAttempts.set(target, { fingerprint, attemptedAt: now });
  if (passiveCandidateAttempts.size > 250) {
    const oldest = [...passiveCandidateAttempts.entries()]
      .sort(([, a], [, b]) => a.attemptedAt - b.attemptedAt)
      .slice(0, passiveCandidateAttempts.size - 250);
    oldest.forEach(([key]) => passiveCandidateAttempts.delete(key));
  }
  return true;
}

function profileSnapshotFingerprint(snapshot: ProfileSnapshotPayload): string {
  return JSON.stringify([
    snapshot.handle,
    snapshot.display_name ?? "",
    snapshot.bio ?? "",
    snapshot.pinned_post_id ?? "",
    snapshot.followers_count ?? null,
    snapshot.following_count ?? null,
    snapshot.posts_count ?? null
  ]);
}

function metricSnapshotFingerprint(snapshot: MetricSnapshotPayload): string {
  return JSON.stringify([
    snapshot.post_id,
    snapshot.followers_count ?? null,
    snapshot.replies ?? null,
    snapshot.reposts ?? null,
    snapshot.likes ?? null,
    snapshot.bookmarks ?? null,
    snapshot.views ?? null,
    snapshot.metadata.post_url,
    snapshot.metadata.post_text ?? "",
    snapshot.metadata.post_type ?? "unknown",
    snapshot.metadata.media_count ?? null,
    snapshot.metadata.has_image ?? null,
    snapshot.metadata.created_at ?? "",
    snapshot.metadata.visibility ?? "unknown"
  ]);
}

function normalizeHandle(value: unknown): string {
  if (typeof value !== "string") return "";
  const handle = value.trim().replace(/^@/, "").toLowerCase();
  return /^[a-z0-9_]{1,15}$/.test(handle) ? handle : "";
}

function captureComposerIntent(event: Event) {
  // Only a REAL click counts as user intent. `isTrusted` is false for anything
  // dispatched by script, so page JS on x.com could otherwise fire a synthetic
  // click carrying data-testid="reply" and make Starling capture a context and
  // arm a composer session the user never opened. The page cannot fake this
  // flag — the browser sets it.
  //
  // Starling's own synthetic clicks are untrusted too, and are already meant to
  // be ignored here (see starlingNativeClickTarget below), so this is strictly
  // stronger rather than a behaviour change.
  if (!event.isTrusted) return;
  const now = Date.now();
  clearExpiredComposerIntents(now);
  const target = event.target instanceof Element ? event.target.closest<HTMLElement>("button, a, [role='menuitem']") : null;
  if (!target) {
    // A click outside X's repost menu dismisses any pending quote context.
    pendingQuoteIntent = null;
    return;
  }
  if (target === starlingNativeClickTarget) return;
  const testId = target.getAttribute("data-testid") || "";
  const label = (target.getAttribute("aria-label") || target.innerText || "").trim();
  const article = target.closest<HTMLElement>(POST_SELECTOR);
  const composeLink = target.matches("a[href^='/compose/post']");
  const control = { testId, label, role: target.getAttribute("role"), composeLink };
  const enabled = isNativeControlEnabled(target);

  if (enabled && isAllowedNativeClick(control, "compose")) {
    pendingComposerIntent = createComposerIntent("post");
    pendingQuoteIntent = null;
    return;
  }
  if (enabled && article && !isReplyControlDisabled(article) && isAllowedNativeClick(control, "reply")) {
    pendingComposerIntent = createComposerIntent("reply", extractContext(article), article);
    pendingQuoteIntent = null;
    return;
  }
  if (enabled && article && isAllowedNativeClick(control, "repost_menu")) {
    pendingQuoteIntent = { context: extractContext(article), sourceArticle: article, expiresAt: now + 8_000 };
    pendingComposerIntent = null;
    return;
  }
  if (target.getAttribute("role") === "menuitem" && pendingQuoteIntent) {
    if (enabled && isAllowedNativeClick(control, "quote")) {
      const quote = pendingQuoteIntent;
      pendingComposerIntent = createComposerIntent("quote", quote.context, quote.sourceArticle);
    }
    // Choosing anything but the exact Quote control—or closing its menu—must
    // not leave a context around for a later unrelated composer.
    pendingQuoteIntent = null;
    return;
  }
  pendingQuoteIntent = null;
}

function clickStarlingNativeControl(control: HTMLElement) {
  starlingNativeClickTarget = control;
  try {
    control.click();
  } finally {
    starlingNativeClickTarget = null;
  }
}

function createComposerIntent(action: ComposerAction, context?: PostContext, sourceArticle?: HTMLElement,
                              options?: { handoff?: boolean }): ComposerIntent {
  const createdAt = Date.now();
  return {
    action,
    context,
    sourceArticle,
    handoff: options?.handoff,
    createdAt,
    // Must outlive the LONGEST composer wait (decide flow waits 10s): an
    // intent that expires at 8s made any editor mounting in seconds 8-10
    // unbindable — the wait then "timed out" even though the box was there.
    expiresAt: createdAt + 15_000,
    visibleBefore: new Set(findVisibleComposers()),
  };
}

function clearExpiredComposerIntents(now = Date.now()) {
  if (pendingComposerIntent && pendingComposerIntent.expiresAt <= now) pendingComposerIntent = null;
  if (pendingQuoteIntent && pendingQuoteIntent.expiresAt <= now) pendingQuoteIntent = null;
  if (pendingLauncherIntent && pendingLauncherIntent.expiresAt <= now) pendingLauncherIntent = null;
}

function clearComposerIntents() {
  pendingComposerIntent = null;
  pendingQuoteIntent = null;
  pendingLauncherIntent = null;
}

function isNativeControlEnabled(control: HTMLElement): boolean {
  return control.getAttribute("aria-disabled") !== "true" && !control.hasAttribute("disabled");
}

function visibleComposerSubmitButtons(scope: HTMLElement): HTMLElement[] {
  // The regular Home composer can expose a plain labelled Post button rather
  // than X's tweetButton test id. Treat both shapes as the same native submit
  // control so Home follows the identical Starling-before-Post path as replies
  // and dialogs.
  return Array.from(new Set([
    ...scope.querySelectorAll<HTMLElement>(COMPOSER_SUBMIT_SELECTOR),
    ...Array.from(scope.querySelectorAll<HTMLElement>('button, [role="button"]')).filter(isPostOrReplyLabelled),
  ])).filter(isVisible);
}

/**
 * A button whose own label is exactly "Post" or "Reply". `textContent`, not `innerText`: a button's
 * label is its text whatever the layout, and `innerText` forces a style and layout read on every
 * button on the page, many times a second.
 */
function isPostOrReplyLabelled(control: HTMLElement): boolean {
  const label = (control.getAttribute("aria-label") || control.textContent || "").replace(/\s+/g, " ").trim();
  return /^(post|reply)$/i.test(label);
}

function nearestComposerSubmitButton(composer: HTMLElement): HTMLElement | null {
  const dialog = composer.closest<HTMLElement>("[role='dialog']");
  if (dialog) {
    const dialogButtons = visibleComposerSubmitButtons(dialog);
    if (dialogButtons.length === 1) return dialogButtons[0];
  }

  // Inline reply composers sit underneath a post and can share a distant
  // article ancestor with that post's action row. Stop at the first wrapper
  // with one submit button: that button belongs to this composer.
  let scope = composer.parentElement;
  for (let depth = 0; scope && depth < 12; depth += 1, scope = scope.parentElement) {
    const buttons = visibleComposerSubmitButtons(scope);
    if (buttons.length === 1) return buttons[0];
  }
  return null;
}

function localComposerScope(composer: HTMLElement): HTMLElement | null {
  let scope = composer.parentElement;
  for (let depth = 0; scope && depth < 12; depth += 1, scope = scope.parentElement) {
    if (visibleComposerSubmitButtons(scope).length === 1) return scope;
  }
  return null;
}

/** Serialize Post's ancestry + every visible control near it into plain data
 *  for the pure insertion chooser in shared/composer-insert. Starling's own
 *  hosts are excluded so re-placement never trips over the pill itself. */
function serializeSubmitAncestry(submit: HTMLElement, maxClimb = 8):
  { nodes: RowNode[]; submitId: number; byId: Map<number, HTMLElement> } {
  const byId = new Map<number, HTMLElement>();
  const idOf = new Map<HTMLElement, number>();
  let nextId = 0;
  const assign = (el: HTMLElement): number => {
    let id = idOf.get(el);
    if (id == null) {
      id = nextId;
      nextId += 1;
      idOf.set(el, id);
      byId.set(id, el);
    }
    return id;
  };
  const rowRect = (el: HTMLElement): RowRect => {
    const r = el.getBoundingClientRect();
    return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width, height: r.height };
  };
  const ancestors: HTMLElement[] = [];
  for (let el = submit.parentElement, depth = 0; el && depth < maxClimb; el = el.parentElement, depth += 1) {
    ancestors.push(el);
  }
  const chain = new Set<HTMLElement>([submit, ...ancestors]);
  const nodes: RowNode[] = [{
    id: assign(submit),
    parentId: submit.parentElement && chain.has(submit.parentElement) ? assign(submit.parentElement) : null,
    isControl: true,
    visible: isVisible(submit),
    rect: rowRect(submit),
  }];
  for (const el of ancestors) {
    nodes.push({
      id: assign(el),
      parentId: el.parentElement && chain.has(el.parentElement) ? assign(el.parentElement) : null,
      isControl: false,
      visible: true,
      rect: rowRect(el),
    });
  }
  const root = ancestors[ancestors.length - 1] ?? submit;
  for (const control of root.querySelectorAll<HTMLElement>('button, [role="button"], [role="progressbar"]')) {
    if (control === submit || idOf.has(control) || !isVisible(control)) continue;
    if (control.closest("[data-starling-composer-control], [data-starling-collapsed-composer-launcher], [data-starling-post-control]")) continue;
    let holder: HTMLElement | null = control.parentElement;
    while (holder && !chain.has(holder)) holder = holder.parentElement;
    if (!holder) continue;
    nodes.push({
      id: assign(control),
      parentId: assign(holder),
      isControl: true,
      visible: true,
      rect: rowRect(control),
    });
  }
  // Non-control content the pill must also stay right of: every DIRECT child
  // of a chain ancestor that is not on the submit's path and not a Starling host
  // (avatar, "Post your reply" placeholder, dividers). Without these, the
  // collapsed reply row — which has no controls left of Reply — planned the
  // pill at the row's left edge instead of hugging the Reply button.
  for (const ancestor of ancestors) {
    for (const child of ancestor.children) {
      if (!(child instanceof HTMLElement) || idOf.has(child) || chain.has(child)) continue;
      if (child.hasAttribute("data-starling-composer-control")
        || child.hasAttribute("data-starling-collapsed-composer-launcher")
        || child.hasAttribute("data-starling-post-control")) continue;
      nodes.push({
        id: assign(child),
        parentId: assign(ancestor),
        isControl: false,
        isContent: true,
        visible: isVisible(child),
        rect: rowRect(child),
      });
    }
  }
  return { nodes, submitId: idOf.get(submit)!, byId };
}

/**
 * Required order, left→right with a draft: [counter ring] [+] [Starling pill]
 * [Post] — real DOM insertion (DOM order = visual order, no transforms). The
 * insertion point comes from measured geometry (shared/composer-insert), not
 * from guessing X's nesting, which broke this repeatedly. Returns false when
 * no compact anchor exists — the caller skips mounting and retries on the
 * next scan, so the failure mode is a briefly absent pill, never a clipped
 * or misplaced one.
 */
function placeStarlingBeforeSubmit(host: HTMLElement, submit: HTMLElement): boolean {
  const { nodes, submitId, byId } = serializeSubmitAncestry(submit);
  const plan = chooseInsertionBeforeSubmit(nodes, submitId);
  if (!plan) return false;
  const parent = byId.get(plan.parentId);
  const before = byId.get(plan.beforeId);
  if (!parent || !before || before.parentElement !== parent) return false;
  parent.style.setProperty("display", "flex", "important");
  parent.style.setProperty("flex-direction", "row", "important");
  parent.style.setProperty("align-items", "center", "important");
  parent.insertBefore(host, before);
  return true;
}

function scanComposerEnhancers() {
  // X re-renders its composer; a session whose composer or pill has been
  // detached is stale — dispose it (and remove its orphaned pill) so the only
  // visible pill always belongs to a live composer and its click works.
  for (const session of [...activeComposerSessions]) {
    if (!session.composer.isConnected || !session.control.isConnected || !session.host.isConnected) {
      disposeComposerSession(session);
    }
  }
  const visible = findVisibleComposers();
  for (const composer of visible) {
    const existing = composerSessions.get(composer);
    if (existing && (!existing.control.isConnected || existing.disposed)) disposeComposerSession(existing);
    const session = composerSessions.get(composer) ?? createComposerSession(composer, visible);
    if (session) {
      updateComposerControl(session);
      // A session that had to mount invisibly (its submit button was not
      // measurable in time) has a working pill with nowhere to be. The moment
      // X finishes drawing the row, adopt that button and let the normal
      // placement path promote the hidden host into the toolbar — so "the
      // button is missing" is at worst a fraction of a second, never the rest
      // of that composer's life.
      if (!session.nativePostButton?.isConnected) {
        const submit = nearestComposerSubmitButton(session.composer);
        if (submit && isVisible(submit)) session.nativePostButton = submit;
      }
      // X mounts the counter ring and + a beat AFTER the input event that
      // created the draft — an input-driven re-check alone leaves the pill on
      // the wrong side until the NEXT keystroke (the "flashes wrong, then
      // snaps right" bug). Scans run on every DOM mutation, so re-checking
      // here corrects the order the moment X's controls actually appear.
      if (session.nativePostButton?.isConnected) ensureStarlingPlacement(session, session.nativePostButton);
    }
  }
}

function scanCollapsedComposerLaunchers() {
  const expandedHomeComposer = hasExpandedHomeComposer();
  for (const launcher of [...activeCollapsedComposerLaunchers]) {
    if (!launcher.submit.isConnected || !launcher.host.isConnected || (expandedHomeComposer && launcher.action === "post")) {
      disposeCollapsedComposerLauncher(launcher);
      continue;
    }
    // Same repositioning contract as session pills: if X mounts its counter
    // ring beside a collapsed row's Reply, the launcher must slide to keep
    // [ring][pill][Reply] — launchers previously never re-checked and got
    // stranded left of the ring.
    ensurePillPlacement(launcher, launcher.submit, () => !activeCollapsedComposerLaunchers.has(launcher));
  }

  // X changes submit test IDs across composer variants. The visible Post / Reply
  // label is a safe fallback because collapsedComposerTarget still verifies the
  // surrounding native composer surface before we insert anything.
  const submits = composerSubmitCandidates();
  for (const submit of submits) {
    if (!isVisible(submit)) continue;
    const target = collapsedComposerTarget(submit);
    if (!target || (target.sourceArticle && target.action === "reply" && isReplyControlDisabled(target.sourceArticle))) continue;
    if (expandedHomeComposer && target.action === "post" && /^\/home\/?$/.test(location.pathname)) continue;

    const existing = collapsedComposerLaunchers.get(submit);
    const composerSession = [...activeComposerSessions].find(session =>
      !session.disposed
      && session.action === target.action
      && session.composer.isConnected
      && (nearestComposerSubmitButton(session.composer) === submit
        || (target.sourceArticle != null && session.composer.closest<HTMLElement>(POST_SELECTOR) === target.sourceArticle)
        || target.surface.contains(session.composer)));
    if (composerSession) {
      if (existing) disposeCollapsedComposerLauncher(existing);
      continue;
    }
    if (existing) continue;
    createCollapsedComposerLauncher(target.action, target.sourceArticle, target.surface, submit);
  }

  // The Home prompt is initially a collapsed composer in several X layouts.
  // Give it an explicit pass so Starling is present beside Post before the user
  // clicks “What’s happening?”, instead of relying on the expanded editor.
  if (!expandedHomeComposer) ensureHomeComposerLauncher();
}

function hasExpandedHomeComposer(): boolean {
  return /^\/home\/?$/.test(location.pathname)
    && findVisibleComposers().some(composer => !composer.closest<HTMLElement>(POST_SELECTOR));
}

function composerSubmitCandidates(): HTMLElement[] {
  return Array.from(new Set([
    ...document.querySelectorAll<HTMLElement>(COMPOSER_SUBMIT_SELECTOR),
    ...Array.from(document.querySelectorAll<HTMLElement>('button, [role="button"]')).filter(isPostOrReplyLabelled),
  ]));
}

function collapsedComposerTarget(submit: HTMLElement): { action: ComposerAction; sourceArticle?: HTMLElement; surface: HTMLElement } | null {
  const replySurface = inlineReplySurface(submit);
  if (replySurface) {
    return { action: "reply", sourceArticle: sourceArticleForInlineReplySurface(replySurface, submit) ?? undefined, surface: replySurface };
  }
  const homeSurface = homeComposerSurface(submit);
  return homeSurface ? { action: "post", surface: homeSurface } : null;
}

function inlineReplySurface(submit: HTMLElement): HTMLElement | null {
  let surface = submit.parentElement;
  for (let depth = 0; surface && depth < 10; depth += 1, surface = surface.parentElement) {
    // innerText ON PURPOSE, although it is a layout read. textContent also reads <script> and
    // hidden text: tried for speed, it found "Post your reply" in a page script ten ancestors up
    // from the Home composer's Post button and hung a REPLY pill beside it.
    const text = (surface.innerText || "").replace(/\s+/g, " ").trim();
    if (/\b(post your reply|replying to\s+@)/i.test(text)) return surface;
  }
  return null;
}

function homeComposerSurface(submit: HTMLElement): HTMLElement | null {
  if (!/^\/home\/?$/.test(location.pathname)) return null;
  const prompt = /what['\u2019]s happening\??/i;
  let surface = submit.parentElement;
  for (let depth = 0; surface && depth < 10; depth += 1, surface = surface.parentElement) {
    // innerText, for the reason given in inlineReplySurface.
    const text = (surface.innerText || "").replace(/\s+/g, " ").trim();
    const labelledPrompt = Array.from(surface.querySelectorAll<HTMLElement>("[aria-label], [placeholder]"))
      .some(element => prompt.test(element.getAttribute("aria-label") || element.getAttribute("placeholder") || ""));
    if (prompt.test(text) || labelledPrompt) return surface;
  }

  // Some X builds render the prompt in an element whose text is intentionally
  // excluded from its ancestor's innerText. Pair that element with the Post
  // button by finding their nearest shared wrapper instead.
  const promptElement = homeComposerPromptElement();
  if (!promptElement) return null;
  for (let shared = submit.parentElement; shared; shared = shared.parentElement) {
    if (shared.contains(promptElement)) return shared;
  }
  return null;
}

function ensureHomeComposerLauncher() {
  if (!/^\/home\/?$/.test(location.pathname)) return;
  const promptElement = homeComposerPromptElement();
  if (!promptElement) return;

  const target = composerSubmitCandidates()
    .filter(submit => /^(post)$/i.test((submit.getAttribute("aria-label") || submit.textContent || "").replace(/\s+/g, " ").trim()) && isVisible(submit))
    .map(submit => ({ submit, surface: sharedHomeComposerSurface(promptElement, submit) }))
    .filter((item): item is { submit: HTMLElement; surface: HTMLElement } => item.surface != null)
    .sort((left, right) => {
      const leftBounds = left.surface.getBoundingClientRect();
      const rightBounds = right.surface.getBoundingClientRect();
      return (leftBounds.width * leftBounds.height) - (rightBounds.width * rightBounds.height);
    })[0];
  if (!target) return;

  const existing = collapsedComposerLaunchers.get(target.submit);
  const composerSession = [...activeComposerSessions].find(session =>
    !session.disposed
    && session.action === "post"
    && session.composer.isConnected
    && (nearestComposerSubmitButton(session.composer) === target.submit || target.surface.contains(session.composer)));
  if (composerSession) {
    if (existing) disposeCollapsedComposerLauncher(existing);
    return;
  }
  if (!existing) createCollapsedComposerLauncher("post", undefined, target.surface, target.submit);
}

const HOME_PROMPT = /what['\u2019]s happening\??/i;
/** Where X draws "What's happening?": its editor, a labelled field, or the Draft.js placeholder. */
const HOME_PROMPT_SELECTOR = '[data-testid^="tweetTextarea_"], .public-DraftEditorPlaceholder-inner, [aria-label], [placeholder]';
let homePromptElement: HTMLElement | null = null;
let lastBroadPromptScanAt = 0;

function homePromptText(element: HTMLElement): string {
  return element.getAttribute("aria-label") || element.getAttribute("placeholder") || element.innerText || "";
}

function smallestElement(elements: HTMLElement[]): HTMLElement | null {
  let best: HTMLElement | null = null;
  let bestArea = Infinity;
  for (const element of elements) {
    const bounds = element.getBoundingClientRect();
    const area = bounds.width * bounds.height;
    if (area < bestArea) {
      best = element;
      bestArea = area;
    }
  }
  return best;
}

/**
 * The Home composer's "What's happening?" prompt. This used to read every `div` and `span` on the
 * page, with a layout read for each, on every scan of /home. It now looks where X puts the prompt,
 * inside the primary column, remembers the element while it stays on the page, and falls back to
 * the old whole-column search at most once every 2.5 s for an X layout that draws it elsewhere.
 */
function homeComposerPromptElement(): HTMLElement | null {
  if (homePromptElement?.isConnected && HOME_PROMPT.test(homePromptText(homePromptElement)) && isVisible(homePromptElement)) {
    return homePromptElement;
  }
  homePromptElement = null;
  const scope: ParentNode = document.querySelector('[data-testid="primaryColumn"]') ?? document;
  const matching = (selector: string, text: (element: HTMLElement) => string) =>
    Array.from(scope.querySelectorAll<HTMLElement>(selector)).filter(element => HOME_PROMPT.test(text(element)) && isVisible(element));
  let found = smallestElement(matching(HOME_PROMPT_SELECTOR, homePromptText));
  if (!found && Date.now() - lastBroadPromptScanAt >= 2_500) {
    lastBroadPromptScanAt = Date.now();
    // Visible text only (innerText): a visible wrapper around a HIDDEN prompt must not match.
    found = smallestElement(matching("div, span", element => element.innerText || ""));
  }
  homePromptElement = found;
  return found;
}

function sharedHomeComposerSurface(promptElement: HTMLElement, submit: HTMLElement): HTMLElement | null {
  for (let shared = submit.parentElement; shared && shared !== document.body && shared !== document.documentElement; shared = shared.parentElement) {
    if (shared.contains(promptElement)) return shared;
  }
  return null;
}

function homeComposerSubmitButton(composer: HTMLElement): HTMLElement | null {
  const composerBounds = composer.getBoundingClientRect();
  return composerSubmitCandidates()
    .filter(submit => {
      if (!isVisible(submit) || submit.closest<HTMLElement>(POST_SELECTOR)) return false;
      const label = (submit.getAttribute("aria-label") || submit.innerText || submit.textContent || "").replace(/\s+/g, " ").trim();
      return /^post$/i.test(label);
    })
    .map(submit => ({ submit, surface: sharedHomeComposerSurface(composer, submit) }))
    .filter((item): item is { submit: HTMLElement; surface: HTMLElement } => {
      if (!item.surface) return false;
      const submitBounds = item.submit.getBoundingClientRect();
      const gap = submitBounds.top - composerBounds.bottom;
      return gap >= -40 && gap <= 160;
    })
    .sort((left, right) => {
      const leftBounds = left.surface.getBoundingClientRect();
      const rightBounds = right.surface.getBoundingClientRect();
      return (leftBounds.width * leftBounds.height) - (rightBounds.width * rightBounds.height);
    })[0]?.submit ?? null;
}

/**
 * X's real reply composer is usually a SIBLING section right after the post's
 * <article>, not a descendant of it — so a plain composer.closest(POST_SELECTOR)
 * never matches the post it's replying to, and the "belongs to this post" safety
 * check always failed (the reply pill looked like it timed out, but the real
 * cause was this ancestor-only match guaranteeing zero valid candidates). Try
 * the ancestor case first (nested replies further down a thread), then fall
 * back to the same nearest-overlapping-article heuristic used when the launcher
 * first identified this composer's source post.
 */
function articleForComposer(composer: HTMLElement): HTMLElement | null {
  const nested = composer.closest<HTMLElement>(POST_SELECTOR);
  if (nested) return nested;
  const submit = nearestComposerSubmitButton(composer);
  if (submit) {
    const nestedViaSubmit = submit.closest<HTMLElement>(POST_SELECTOR);
    if (nestedViaSubmit) return nestedViaSubmit;
  }
  const composerBounds = composer.getBoundingClientRect();
  return Array.from(document.querySelectorAll<HTMLElement>(POST_SELECTOR))
    .filter(article => {
      if (!isVisible(article)) return false;
      const bounds = article.getBoundingClientRect();
      const overlaps = bounds.left < composerBounds.right && bounds.right > composerBounds.left;
      return overlaps && bounds.bottom <= composerBounds.top + 96;
    })
    .sort((left, right) => right.getBoundingClientRect().bottom - left.getBoundingClientRect().bottom)[0] ?? null;
}

function sourceArticleForInlineReplySurface(surface: HTMLElement, submit: HTMLElement): HTMLElement | null {
  const nested = submit.closest<HTMLElement>(POST_SELECTOR);
  if (nested) return nested;

  // On post-detail pages the reply row may be a sibling immediately after the
  // source article. Use the nearest preceding, overlapping visible post only
  // after confirming this is an actual “Post your reply” surface.
  const surfaceBounds = surface.getBoundingClientRect();
  return Array.from(document.querySelectorAll<HTMLElement>(POST_SELECTOR))
    .filter(article => {
      if (!isVisible(article)) return false;
      const bounds = article.getBoundingClientRect();
      const overlaps = bounds.left < surfaceBounds.right && bounds.right > surfaceBounds.left;
      return overlaps && bounds.bottom <= surfaceBounds.top + 48;
    })
    .sort((left, right) => right.getBoundingClientRect().bottom - left.getBoundingClientRect().bottom)[0] ?? null;
}

/**
 * X does not expose a stable parent id on every rendered reply. Prefer an
 * explicit status link; only fall back to the detail-page relationship when a
 * reply is visibly below this exact source post and names its author. That
 * keeps a reply to another post by the same person from blocking this one.
 */
function visibleOwnReplyToContext(context: PostContext | undefined, sourceArticle?: HTMLElement): HTMLElement | null {
  const identity = getSignedInIdentity();
  const targetId = context?.post_id ?? postIdFromUrl(context?.post_url);
  const targetHandle = normalizeHandle(context?.author_handle);
  if (!identity || !targetId || !targetHandle) return null;
  const articles = Array.from(document.querySelectorAll<HTMLElement>(POST_SELECTOR));
  const source = sourceArticle?.isConnected ? sourceArticle : articles.find(article =>
    postIdFromUrl(findPermalink(article)?.href) === targetId);
  const sourceIndex = source ? articles.indexOf(source) : -1;
  const detailPageForTarget = postIdFromUrl(location.href) === targetId;
  const records = articles.map((article, index) => {
    const parent = extractVisibleParentContext(article);
    const statusLinks = Array.from(article.querySelectorAll<HTMLAnchorElement>('a[href*="/status/"]'))
      .map(link => postIdFromUrl(link.href));
    const replying = extractRepliedToHandle(article);
    return {
      postId: postIdFromUrl(findPermalink(article)?.href),
      authorHandle: extractAuthorHandle(article) ?? undefined,
      parentPostId: parent?.postId ?? statusLinks.find(id => id === targetId),
      repliedToHandle: replying,
      isReply: Boolean(replying) || /replying\s+to/i.test(article.innerText),
      followsTargetOnDetailPage: detailPageForTarget && sourceIndex >= 0 && index > sourceIndex,
      article,
    };
  });
  const match = findOwnReplyToTarget({ postId: targetId, authorHandle: targetHandle }, identity.handle, records);
  return match ? records.find(record => record.postId === match.postId)?.article ?? null : null;
}

const ALREADY_REPLIED = "You've already replied to this post.";

/**
 * "You've already replied to this post." is advice, not a wall: a second reply is sometimes exactly
 * right (the follow-up, the correction). `writeAnother` is what that button does at each site: type
 * a draft Starling already has, or ask again with the engine's own "second reply is fine" override.
 */
function renderAlreadyReplied(container: HTMLElement, writeAnother?: () => void | Promise<void>) {
  const line = document.createElement("div");
  line.className = "deciding";
  line.textContent = ALREADY_REPLIED;
  container.append(line);
  if (writeAnother) {
    addChip(container, "Write another reply", async chip => {
      chip.disabled = true;
      await writeAnother();
      if (chip.isConnected) chip.remove();
    });
  }
}

function showAlreadyRepliedNotice(context?: PostContext, writeAnother?: () => void | Promise<void>) {
  openDock();
  clearDockContext();
  const card = addCard("draft");
  renderAlreadyReplied(card, writeAnother);
  scrollFeed();
}

function createCollapsedComposerLauncher(action: ComposerAction, sourceArticle: HTMLElement | undefined, surface: HTMLElement, submit: HTMLElement) {
  const host = document.createElement("span");
  host.setAttribute("data-starling-collapsed-composer-launcher", action);
  host.style.cssText = "display:inline-flex;align-items:center;align-self:center;flex:none;margin:0 -4px 0 6px;position:relative;z-index:1;";
  const shadow = host.attachShadow({ mode: "closed" });
  sealShadowKeyboard(shadow, { editableOnly: true });
  const control = document.createElement("button");
  control.type = "button";
  control.className = "vf-enhance";
  control.dataset.state = "ready";
  // Identical markup to the session pill on purpose: the launcher runs a real
  // operation (it opens X's box AND writes the draft), so it needs the same
  // working dots and the same hover-Cancel. Without them a click during that
  // window had no visible effect and no way out — which is what made the flow
  // feel like it needed to be clicked again.
  control.innerHTML = `<span class="vf-enhance-mark" aria-hidden="true">✦</span><span class="vf-enhance-text"></span><span class="vf-enhance-cancel">Cancel</span><span class="vf-enhance-working" aria-hidden="true"><i></i><i></i><i></i></span>`;
  const style = document.createElement("style");
  style.textContent = composerStyles;
  shadow.append(style, control);
  host.dataset.theme = pillThemeFor(surfaceTone(surface));

  // No compact anchor row → no mount at all (the next scan retries). Never
  // append into a tall container where X clips the pill into stray slivers.
  if (!placeStarlingBeforeSubmit(host, submit)) return;
  alignPillOnce(host, control, submit);

  const launcher: CollapsedComposerLauncher = {
    action, sourceArticle, surface, submit, host, control, abortController: new AbortController(),
  };
  updateLauncherControl(launcher);
  collapsedComposerLaunchers.set(submit, launcher);
  activeCollapsedComposerLaunchers.add(launcher);
  control.addEventListener("pointerenter", () => warmUp("hover"), { signal: launcher.abortController.signal });
  // The press must not take focus: focus moving into X's collapsed row makes X expand the row under
  // the cursor, the mouseup lands on X's element, and this pill's click never fires (on x.com,
  // 30 Oct 2026, the first press only opened X's box). Swallowing the press keeps the click ours.
  for (const type of ["pointerdown", "mousedown"] as const) {
    control.addEventListener(type, event => {
      event.preventDefault();
      event.stopPropagation();
    }, { signal: launcher.abortController.signal });
  }
  control.addEventListener("click", event => {
    event.preventDefault();
    event.stopPropagation();
    // A click while this launcher's own operation is live means STOP — it can
    // never be a silent no-op. That is what turned one flow into "click, wait,
    // click again, click again": the middle clicks landed on a pill that
    // looked idle, did nothing, and said nothing.
    if (cancelLauncherWork(launcher)) return;
    void launchCollapsedComposerFlow(launcher);
  }, { signal: launcher.abortController.signal });
}

/** Launcher copy comes from the same source as the session pill's, so a
 *  collapsed row and an open composer never disagree about what the button is
 *  about to do — including calling a self-reply a self-reply. */
function updateLauncherControl(launcher: CollapsedComposerLauncher) {
  const state = (launcher.control.dataset.state || "ready") as ComposerControlState;
  const isOwnPost = Boolean(launcher.sourceArticle && isOwnPostArticle(launcher.sourceArticle));
  const copy = composerControlCopy(launcher.action, "create", state, false, isOwnPost);
  const text = launcher.control.querySelector<HTMLElement>(".vf-enhance-text");
  const working = state === "sending" || state === "typing";
  if (text && !working) text.textContent = copy.visible;
  launcher.control.setAttribute("aria-label", copy.ariaLabel);
  launcher.control.setAttribute("aria-busy", String(working));
}

/** A live launcher operation, stopped from the pill itself. Returns false when
 *  there was nothing to stop, so the caller can start a fresh flow instead. */
function cancelLauncherWork(launcher: CollapsedComposerLauncher): boolean {
  const binding = opBindingFor(launcher.activeOpId);
  if (!binding || isTerminal(binding.op)) return false;
  binding.onCancel?.();
  return true;
}

function isOwnPostArticle(article: HTMLElement): boolean {
  const identity = getSignedInIdentity();
  return Boolean(identity && extractAuthorHandle(article) === identity.handle);
}

/**
 * ONE click does everything: the dock card mounts immediately (context +
 * "Opening…" status), X's editor is opened, the fresh session is claimed by
 * direct identity, and generation begins — all against a single operation.
 * A click while the op is live CANCELS it (see the listener); a timeout becomes
 * a failed card with Retry instead of a badge painted onto X's own DOM.
 *
 * Why the second activation attempt exists: expanding a collapsed reply row is
 * the one moment X is most likely to throw the whole subtree away and rebuild
 * it. When that happened between the click and the first claim, the captured
 * surface/submit went stale, no candidate ever matched, and the op sat in
 * `preparing` until it timed out — so the click read as "it only opened the
 * box" and the user had to press the pill again to get a draft. Re-activating
 * once against the NOW-current DOM turns that second press into something
 * Starling does for itself.
 */
async function launchCollapsedComposerFlow(launcher: CollapsedComposerLauncher, options: { secondReplyOk?: boolean } = {}) {
  const { action, sourceArticle, surface, submit } = launcher;
  const liveOp = opBindingFor(launcher.activeOpId);
  if (liveOp && !isTerminal(liveOp.op)) {
    return;
  }
  if (handleOrphaned()) return;

  const context = sourceArticle ? extractContext(sourceArticle) : undefined;
  if (action === "reply" && !options.secondReplyOk && visibleOwnReplyToContext(context, sourceArticle)) {
    showAlreadyRepliedNotice(context, () => launchCollapsedComposerFlow(launcher, { secondReplyOk: true }));
    return;
  }
  activeContext = context ?? activeContext;
  openDock();
  setDockContext(action, context, "working");
  releaseFeedAnchor(); // the fresh flow's card scrolls into view normally
  collapseFinishedCards();
  const isOwnPost = Boolean(context?.is_own_post);
  const label = isOwnPost && action !== "post" ? `self-${actionLabel(action)}` : actionLabel(action);
  const op = beginOp("enhance", "preparing", addCard("draft"),
    `Opening X's ${label} box…`, { target: opTargetLabel(action, context) });
  launcher.activeOpId = op.id;
  launcher.control.dataset.state = "sending";
  updateLauncherControl(launcher);

  let cancelled = false;
  const stopWaiting = () => {
    cancelled = true;
    if (pendingLauncherIntent?.opId === op.id) pendingLauncherIntent = null;
    applyOpEvent(op.id, { type: "cancelled" });
    setDockContext(action, context, "ready");
    releaseLauncherControl(launcher);
  };
  setOpControls(op.id, { onCancel: stopWaiting, target: opTargetLabel(action, context) });

  try {
    // Two attempts against the same op. The first is the normal path; the
    // second only runs if X's editor never produced a claimable session, and
    // it re-reads the live DOM rather than trusting the pre-click capture.
    for (let attempt = 0; attempt < 2 && !cancelled; attempt += 1) {
      const target = attempt === 0
        ? { surface, submit }
        : currentCollapsedTarget(launcher) ?? { surface, submit };
      if (attempt === 1) {
        applyOpEvent(op.id, { type: "heartbeat", note: `X's ${label} box is still settling — opening it again…` });
      }
      const intent = createComposerIntent(action, context, sourceArticle, { handoff: true });
      pendingLauncherIntent = { intent, surface: target.surface, submit: target.submit, opId: op.id, expiresAt: Date.now() + 12_000 };
      activateCollapsedComposer(target.surface, target.submit, action);
      // X can take a while to mount its editor; 3s was too tight and a miss meant
      // the click silently did nothing. Wait longer, and always say what happened.
      const session = await waitForCollapsedComposerSession(action, sourceArticle, target.surface, target.submit, attempt === 0 ? 6_000 : 6_000);
      if (pendingLauncherIntent?.opId === op.id) pendingLauncherIntent = null;
      if (cancelled) return;
      if (session) {
        releaseLauncherControl(launcher);
        void enhanceComposer(session, { existingOpId: op.id, secondReplyOk: options.secondReplyOk });
        return;
      }
    }
    if (cancelled) return;
    releaseLauncherControl(launcher);
    applyOpEvent(op.id, {
      type: "failed",
      error: `Couldn't open X's ${label} box. Click into it, then press ✦ again. No draft was used.`,
    });
  } catch {
    if (pendingLauncherIntent?.opId === op.id) pendingLauncherIntent = null;
    releaseLauncherControl(launcher);
    if (cancelled) return;
    applyOpEvent(op.id, {
      type: "failed",
      error: action === "reply"
        ? "Couldn't open X's reply box. Click Reply on the post, then press ✦ Create reply."
        : `Couldn't open X's ${label} box. Click into it, then press ✦ Create ${label}.`,
    });
  }
}

/** Put the launcher pill back to idle. Its op has moved on (adopted by the
 *  composer session, cancelled, or failed) and the pill must stop claiming to
 *  be working — a pill stuck on dots is another button that "does nothing". */
function releaseLauncherControl(launcher: CollapsedComposerLauncher) {
  if (!launcher.control.isConnected) return;
  launcher.control.dataset.state = "ready";
  updateLauncherControl(launcher);
}

/** The collapsed row this launcher stands in, re-read from the CURRENT DOM.
 *  Used only on retry: after X rebuilds the row, the captured surface/submit
 *  can be detached, and re-clicking a detached node opens nothing. */
function currentCollapsedTarget(launcher: CollapsedComposerLauncher): { surface: HTMLElement; submit: HTMLElement } | null {
  if (launcher.submit.isConnected && isVisible(launcher.submit) && launcher.surface.isConnected) return null;
  for (const candidate of composerSubmitCandidates()) {
    if (!isVisible(candidate)) continue;
    const target = collapsedComposerTarget(candidate);
    if (!target || target.action !== launcher.action) continue;
    if (launcher.sourceArticle && target.sourceArticle && target.sourceArticle !== launcher.sourceArticle) continue;
    return { surface: target.surface, submit: candidate };
  }
  return null;
}

function disposeCollapsedComposerLauncher(launcher: CollapsedComposerLauncher) {
  if (launcher.placementFrame != null) {
    cancelAnimationFrame(launcher.placementFrame);
    launcher.placementFrame = undefined;
  }
  launcher.abortController.abort();
  launcher.host.remove();
  activeCollapsedComposerLaunchers.delete(launcher);
  collapsedComposerLaunchers.delete(launcher.submit);
}

function activateCollapsedComposer(surface: HTMLElement, submit: HTMLElement, action: ComposerAction) {
  // Only ever click a control we've POSITIVELY identified as the composer's own
  // placeholder or textbox. The old last-resort `submit.parentElement
  // .previousElementSibling` could dispatch a real click on an unrelated element
  // when X's DOM shifted \u2014 a source of "it opened something for no reason".
  const placeholder = Array.from(surface.querySelectorAll<HTMLElement>("div, span"))
    .filter(element => isVisible(element) && (action === "reply"
      ? /^(post your reply|reply to this post)$/i.test((element.innerText || element.textContent || "").trim())
      : /^what['\u2019]s happening\??$/i.test((element.innerText || element.textContent || "").trim())))
    .sort((left, right) => left.childElementCount - right.childElementCount)[0]
    ?? surface.querySelector<HTMLElement>('[data-testid^="tweetTextarea_"], [contenteditable="true"], [role="textbox"]');
  if (placeholder) {
    placeholder.focus?.();
    placeholder.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, composed: true }));
  }
  scheduleScan();
}

function waitForCollapsedComposerSession(action: ComposerAction, sourceArticle: HTMLElement | undefined, surface: HTMLElement, submit: HTMLElement, deadlineMs: number): Promise<ComposerSession | null> {
  // X can fully swap the collapsed row for an expanded one (a different DOM
  // subtree entirely, e.g. reply-as-modal) instead of mutating it in place —
  // then `submit`/`surface`, captured before the click, are stale and none of
  // the positional checks below ever match, so this silently times out and
  // the FIRST click looks like it did nothing (the enhance never fires) even
  // though X's editor opened fine. A session that simply didn't exist before
  // this click started is accepted outright as a fallback so one click, not
  // two, is enough once positional matching can't keep up with X's DOM.
  const preexisting = new Set(activeComposerSessions);
  return new Promise(resolve => {
    const startedAt = Date.now();
    const tick = () => {
      scanComposerEnhancers();
      const session = [...activeComposerSessions].find(candidate =>
        !candidate.disposed
        && candidate.action === action
        && isUsableComposer(candidate.composer)
        && (!preexisting.has(candidate)
          || nearestComposerSubmitButton(candidate.composer) === submit
          || (sourceArticle != null && candidate.composer.closest<HTMLElement>(POST_SELECTOR) === sourceArticle)
          || surface.contains(candidate.composer)));
      if (session) return resolve(session);
      if (Date.now() - startedAt >= deadlineMs) return resolve(null);
      window.setTimeout(tick, 100);
    };
    tick();
  });
}

function createComposerSession(composer: HTMLElement, visible: HTMLElement[]): ComposerSession | null {
  const intent = resolveComposerIntent(composer, visible);
  if (!intent) {
    return null;
  }
  const isHomeComposer = intent.action === "post" && /^\/home\/?$/.test(location.pathname) && !composer.closest<HTMLElement>(POST_SELECTOR);
  const host = document.createElement("span");
  const control = document.createElement("button");
  host.setAttribute("data-starling-composer-control", "true");
  host.style.cssText = "display:inline-flex;align-items:center;margin:8px 0 0;position:relative;z-index:1;";
  const shadow = host.attachShadow({ mode: "closed" });
  sealShadowKeyboard(shadow, { editableOnly: true });
  control.type = "button";
  control.className = "vf-enhance";
  control.innerHTML = `<span class="vf-enhance-mark" aria-hidden="true">✦</span><span class="vf-enhance-text">Create ${actionLabel(intent.action)}</span><span class="vf-enhance-cancel">Cancel</span><span class="vf-enhance-working" aria-hidden="true"><i></i><i></i><i></i></span>`;
  const style = document.createElement("style");
  style.textContent = composerStyles;
  shadow.append(style, control);
  // Anchor inside the composer's own toolbar row so the pill can never float
  // over unrelated timeline content. Fall back to the dialog / parent.
  const scope = nativeComposerScope(composer);
  const claimedSubmit = intent.launcherSubmit?.isConnected && isVisible(intent.launcherSubmit)
    ? intent.launcherSubmit : null;
  const nativePostButton = isHomeComposer
    ? homeComposerSubmitButton(composer)
    : nearestComposerSubmitButton(composer) ?? claimedSubmit
      ?? (scope ? visibleComposerSubmitButtons(scope).at(-1) ?? null : null);
  const toolbar = nativePostButton?.closest<HTMLElement>('[data-testid="toolBar"]')
    ?? scope?.querySelector<HTMLElement>('[data-testid="toolBar"]');
  const anchor = toolbar ?? scope;
  let handoffFallback = false;
  const mountHandoffFallback = () => {
    if (handoffFallback) return;
    handoffFallback = true;
    // This is intentionally invisible. A Starling-initiated action already
    // proved this exact composer is safe to use; it must not fail merely
    // because X has not finished drawing a compact toolbar for our optional
    // pill. ensureStarlingPlacement promotes it into that toolbar when available.
    host.dataset.starlingHandoffFallback = "true";
    host.style.cssText = "display:none !important;";
    (document.body ?? document.documentElement).append(host);
  };
  if (!anchor && !intent.handoff) return null;
  // Theme comes from the composer's surfaces, NEVER from a button: X's Post
  // button is white-on-dark, and sampling it painted a near-white pill onto
  // the dark UI (the "white sliver" artifact when it then got clipped). The
  // Home composer used to be forced dark on every theme, which put near-white
  // text on X's white toolbar; it reads its surface like every other composer.
  host.dataset.theme = pillThemeFor(surfaceTone(composerThemeSource([toolbar, scope, composer])));
  // Inline placement for EVERY composer (home included): the pill sits in the
  // composer's own action row next to Post, so it scrolls away with that
  // section and is only visible when the section is on screen. Never fixed.
  if (nativePostButton) {
    host.style.cssText = "display:inline-flex;align-items:center;align-self:center;flex:none;margin:0 -4px 0 6px;position:relative;z-index:1;";
    if (!placeStarlingBeforeSubmit(host, nativePostButton)) {
      if (!intent.handoff) return null;
      mountHandoffFallback();
    }
  } else if (intent.launcherSubmit && Date.now() - intent.createdAt < LAUNCHER_PLACEMENT_GRACE_MS) {
    // A launcher flow KNOWS its submit — if it isn't measurable yet (X is
    // still animating the row open), don't take an append fallback that
    // wraps the pill into a clipped sliver; skip and re-claim next scan.
    //
    // BOUNDED, deliberately. This used to return null unconditionally, so a
    // composer whose submit button never became measurable (X rebuilt the row
    // and left the captured button detached, or kept it zero-size) could never
    // become a session AT ALL — the launcher's wait then timed out with X's
    // reply box sitting open and empty. That is the "first click only opens
    // the box, now click again" bug: the click had already done its job
    // safely, and the only thing missing was somewhere to hang an optional
    // pill. After the grace window the session is created with the INVISIBLE
    // handoff mount instead; generation starts, and ensureStarlingPlacement
    // promotes the pill into the toolbar the moment X draws one.
    return null;
  } else if (intent.launcherSubmit) {
    mountHandoffFallback();
  } else if (toolbar && isCompactRowElement(toolbar)) {
    host.style.cssText = "display:inline-flex;align-items:center;flex:none;margin-left:4px;position:relative;z-index:1;";
    toolbar.append(host);
  } else if (anchor && isCompactRowElement(anchor)) {
    anchor.append(host);
  } else {
    // No compact anchor → don't mount at all; the next scan retries. Appending
    // into a tall container is how the pill got clipped into stray artifacts.
    if (!intent.handoff) return null;
    mountHandoffFallback();
  }
  if (nativePostButton && !handoffFallback) alignPillOnce(host, control, nativePostButton);
  // A native X action can be observed before its toolbar finishes mounting.
  // Only consume its pending claim after this session is ready; otherwise the
  // next scan must keep trying the exact composer X just opened.
  if (pendingComposerIntent === intent) pendingComposerIntent = null;
  const session: ComposerSession = {
    id: crypto.randomUUID(), action: intent.action, composer, host, context: intent.context,
    sourceArticle: intent.sourceArticle,
    control, requestId: 0, composing: false, writing: false, inputRevision: 0,
    disposed: false, abortController: new AbortController(),
    nativePostButton: nativePostButton ?? undefined,
  };
  composerSessions.set(composer, session);
  const listenerOptions = { signal: session.abortController.signal };
  composer.addEventListener("compositionstart", () => {
    if (session.disposed) return;
    session.composing = true;
    updateComposerControl(session);
  }, listenerOptions);
  composer.addEventListener("compositionend", () => {
    if (session.disposed) return;
    session.composing = false;
    updateComposerControl(session);
  }, listenerOptions);
  composer.addEventListener("input", () => {
    if (session.disposed) return;
    if (!session.writing) {
      session.inputRevision += 1;
      scheduleComposerEditCapture(session);
    }
    // X mounts/unmounts its counter ring and + as the draft changes; re-check
    // the insertion plan (rAF-debounced) and re-insert only when the pill is
    // no longer immediately left of Post — DOM order stays the visual order.
    if (nativePostButton) ensureStarlingPlacement(session, nativePostButton);
    updateComposerControl(session);
  }, listenerOptions);
  bindComposerControl(control, session, listenerOptions);
  activeComposerSessions.add(session);
  return session;
}

function bindComposerControl(control: HTMLButtonElement, session: ComposerSession, listenerOptions: AddEventListenerOptions) {
  control.addEventListener("pointerenter", () => warmUp("hover"), listenerOptions);
  control.addEventListener("click", event => {
    if (session.disposed) return;
    event.preventDefault();
    event.stopPropagation();
    if (cancelComposerWork(session)) return;
    const state = control.dataset.state as ComposerControlState | undefined;
    // "✓ Done" is not a button for the moment it shows (it is disabled too): a second click right
    // after a draft used to rewrite the reply and spend another draft.
    if (state === "updated") return;
    void enhanceComposer(session, { another: state === "another" });
  }, listenerOptions);
}

// NOTE: there is deliberately NO per-keystroke measurement or transform-nudge
// of the pill. Every "measure and adjust" pass ran mid-layout while X was
// re-rendering the row and made the pill visibly bob on each keypress. The
// pill's geometry is a CSS constant; the ONLY alignment is alignPillOnce
// below, which runs exactly once per (re)mount and never again.

/** One-shot vertical alignment at MOUNT time only. Post can sit inside a
 *  padded wrapper (the collapsed home row), so pure flex-centering leaves the
 *  pill riding a few px high there. Measured once when the pill enters the
 *  row and ONLY then — typing never re-runs it, so the pill stands still. */
function alignPillOnce(host: HTMLElement, control: HTMLButtonElement, submit: HTMLElement) {
  host.style.removeProperty("transform");
  requestAnimationFrame(() => {
    if (!host.isConnected || !control.isConnected || !submit.isConnected) return;
    const pill = control.getBoundingClientRect();
    const post = submit.getBoundingClientRect();
    if (!pill.height || !post.height) return;
    const delta = Math.round((post.top + post.height / 2) - (pill.top + pill.height / 2));
    if (delta !== 0) host.style.transform = `translateY(${Math.max(-8, Math.min(8, delta))}px)`;
  });
}

/** First non-null candidate that is not (inside) a button — X's white Post
 *  button must never decide the pill's theme. */
function composerThemeSource(candidates: Array<Element | null | undefined>): Element {
  for (const candidate of candidates) {
    if (!candidate) continue;
    if ((candidate as HTMLElement).closest?.('button, [role="button"]')) continue;
    return candidate;
  }
  return document.body;
}

function isCompactRowElement(element: HTMLElement): boolean {
  return isCompactRow(element.getBoundingClientRect().height, 36);
}

/** rAF-debounced re-placement, shared by session pills AND collapsed-row
 *  launchers: when X mounts/unmounts the counter ring or + around the pill,
 *  recompute the insertion plan and re-insert only if the pill is no longer
 *  immediately left of the submit. */
interface PillPlacementRef {
  host: HTMLElement;
  control: HTMLButtonElement;
  placementFrame?: number;
}

function ensurePillPlacement(ref: PillPlacementRef, submit: HTMLElement, isDead: () => boolean) {
  if (ref.placementFrame != null) return;
  ref.placementFrame = requestAnimationFrame(() => {
    ref.placementFrame = undefined;
    if (isDead() || !ref.host.isConnected || !submit.isConnected) return;
    const { nodes, submitId, byId } = serializeSubmitAncestry(submit);
    const plan = chooseInsertionBeforeSubmit(nodes, submitId);
    if (!plan) return;
    const parent = byId.get(plan.parentId);
    const before = byId.get(plan.beforeId);
    if (!parent || !before || before.parentElement !== parent) return;
    // Re-insert ONLY when the pill is genuinely out of position (X mounted or
    // removed its ring/+ around it). No measuring, no nudging, no resizing on
    // the happy path — a settled pill must not move by a single pixel while
    // the user types. Alignment runs once per REAL move, never per keystroke.
    if (ref.host.parentElement !== parent || ref.host.nextElementSibling !== before) {
      if (ref.host.dataset.starlingHandoffFallback === "true") {
        ref.host.removeAttribute("data-starling-handoff-fallback");
        ref.host.style.cssText = "display:inline-flex;align-items:center;align-self:center;flex:none;margin:0 -4px 0 6px;position:relative;z-index:1;";
      }
      parent.style.setProperty("display", "flex", "important");
      parent.style.setProperty("flex-direction", "row", "important");
      parent.style.setProperty("align-items", "center", "important");
      parent.insertBefore(ref.host, before);
      alignPillOnce(ref.host, ref.control, submit);
    }
  });
}

function ensureStarlingPlacement(session: ComposerSession, nativePostButton: HTMLElement) {
  ensurePillPlacement(session, nativePostButton, () => session.disposed);
}

// ---------------------------------------------------------------------------
// Per-post decide button: a small ✦ icon on each post. Click it → Starling reads
// the post and says reply / quote / wait. If it says act, it OPENS the native
// X composer for that action and types the draft in. It never clicks Post —
// you always publish yourself.
// ---------------------------------------------------------------------------
let postButtonHosts = new WeakSet<HTMLElement>();
// The ✦ button lives in a CLOSED shadow root, so a later scan cannot query it
// back out of the host. Keep the reference here so an own-post verdict that
// only becomes knowable later (the account switcher renders after the first
// timeline paint) can still relabel the button it already mounted.
const postControlButtons = new WeakMap<HTMLElement, HTMLButtonElement>();

interface PostButtonPlan {
  article: HTMLElement;
  bar: HTMLElement;
  mounted: HTMLElement | null;
  owned: boolean;
  /** Read only for a post that still needs its buttons. */
  dark?: boolean;
}

/**
 * Two passes: every layout read (which posts are on screen, whose they are, what surface they sit
 * on) first, then every write. Interleaving them made the browser recompute layout once per post.
 */
function scanPostButtons() {
  const identity = scanIdentity ?? getSignedInIdentity();
  const plans: PostButtonPlan[] = [];
  for (const article of document.querySelectorAll<HTMLElement>(POST_SELECTOR)) {
    if (plans.length >= 25) break;
    if (!article.isConnected || !isInViewport(article)) continue;
    const reply = article.querySelector<HTMLElement>('[data-testid="reply"]');
    const bar = reply?.closest<HTMLElement>('[role="group"]');
    if (!bar) continue; // quoted sub-posts and cards have no reply control
    const mounted = bar.querySelector<HTMLElement>('[data-starling-post-control]');
    const owned = Boolean(identity && extractAuthorHandle(article) === identity.handle);
    plans.push({ article, bar, mounted, owned, dark: mounted ? undefined : isDarkSurface(article) });
  }
  for (const plan of plans) injectPostButton(plan);
}

/** Articles whose hover and focus already reveal their paperclip. X reuses article nodes, so the
 *  listeners look the host up when they fire instead of holding on to one. */
const hoverWiredArticles = new WeakSet<HTMLElement>();

function wirePostHover(article: HTMLElement) {
  if (hoverWiredArticles.has(article)) return;
  hoverWiredArticles.add(article);
  const set = (on: boolean) => {
    const host = article.querySelector<HTMLElement>("[data-starling-post-control]");
    if (host) host.dataset.hover = String(on);
  };
  article.addEventListener("pointerenter", () => set(true), { passive: true });
  article.addEventListener("pointerleave", () => set(false), { passive: true });
  article.addEventListener("focusin", () => set(true));
  article.addEventListener("focusout", event => {
    if (!(event.relatedTarget instanceof Node && article.contains(event.relatedTarget))) set(false);
  });
}

function injectPostButton({ article, bar, mounted, owned, dark }: PostButtonPlan) {
  if (mounted) {
    postButtonHosts.add(article);
    // X recycles timeline rows: the same host can end up over a DIFFERENT
    // post, and on first paint the account switcher often has not rendered yet
    // so `identity` was null and every button mounted as "not your post".
    // Re-decide on each scan and relabel when it changed.
    const self = String(owned);
    if (mounted.dataset.self !== self) {
      mounted.dataset.self = self;
      const button = postControlButtons.get(mounted);
      if (button) {
        button.dataset.self = self;
        if (button.dataset.state !== "working") applyPostButtonLabels(button, owned);
      }
    }
    return;
  }
  const isOwnPost = owned;
  const host = document.createElement("div");
  host.setAttribute("data-starling-post-control", "true");
  host.dataset.self = String(isOwnPost);
  host.style.cssText = "display:inline-flex;align-items:center;";
  host.dataset.theme = dark ? "dark" : "light";
  const shadow = host.attachShadow({ mode: "closed" });
  sealShadowKeyboard(shadow, { editableOnly: true });
  const style = document.createElement("style");
  style.textContent = postButtonStyles;
  shadow.append(style);

  // Attach-to-Starling (paperclip): pulls this post into the chat as a card. Shown while the
  // post is hovered or holds focus: a paperclip on every post in the feed was noise.
  const attach = document.createElement("button");
  attach.type = "button";
  attach.className = "vf-attach";
  attach.title = "Attach this post to Starling chat";
  attach.setAttribute("aria-label", "Attach this post to Starling chat");
  attach.innerHTML = `<span class="vf-attach-mark" aria-hidden="true">${PAPERCLIP_SVG}</span>`;
  attach.addEventListener("click", event => {
    event.preventDefault();
    event.stopPropagation();
    attachPostToChat(article);
  });
  shadow.append(attach);
  wirePostHover(article);

  // ✦ decision button. This used to be SKIPPED on your own posts ("should I
  // reply to myself is noise"), which removed the only entry point to a
  // self-reply — a real move on X: adding the follow-up, the caveat, or the
  // receipt under a post that is already travelling. The engine has always
  // understood it (PostContext.is_own_post drives a momentum-aware own-post
  // read in opportunities.py); nothing in the UI could reach it. It is shown
  // on your own posts now, and says plainly that it is a SELF reply or quote so
  // there is never any doubt about which post it is acting on.
  {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "vf-post";
    button.dataset.self = String(isOwnPost);
    applyPostButtonLabels(button, isOwnPost);
    button.innerHTML = `
      <span class="vf-post-mark" aria-hidden="true">✦</span>
      <span class="vf-post-loading" aria-hidden="true"><i></i><i></i><i></i></span>
      <span class="vf-post-stop" aria-hidden="true"></span>
    `;
    button.addEventListener("pointerenter", () => warmUp("hover"));
    button.addEventListener("click", event => {
      event.preventDefault();
      event.stopPropagation();
      void decideOnPost(article, button);
    });
    shadow.append(button);
    postControlButtons.set(host, button);
  }

  bar.append(host);
  // The pointer may already be over the post when its buttons mount.
  if (article.matches(":hover, :focus-within")) host.dataset.hover = "true";
  postButtonHosts.add(article);
}

/** Each ✦ with a decision in flight, so a second click on that ✦ can stop it. */
const postButtonOps = new WeakMap<HTMLButtonElement, string>();

/** The ✦ while its request runs: its click is Cancel, and it says so to a pointer and to a screen reader. */
function setPostButtonWorking(button: HTMLButtonElement, working: boolean) {
  if (working) {
    button.dataset.state = "working";
    button.title = "Cancel";
    button.setAttribute("aria-label", "Cancel drafting with Starling");
    button.setAttribute("aria-busy", "true");
    return;
  }
  button.dataset.state = "idle";
  // Restore the button's OWN labels. Resetting to the generic third-party
  // wording here silently relabelled every ✦ on your own posts the moment it
  // was used once — the one place the user checks whose post this is.
  applyPostButtonLabels(button, button.dataset.self === "true");
  button.removeAttribute("aria-busy");
}

/** One quiet line that replaces a card's contents: "Stopped. No draft used." */
function renderStatusLine(card: HTMLElement, text: string, isError = false) {
  const line = document.createElement("div");
  line.className = `deciding${isError ? " error" : ""}`;
  line.textContent = text;
  card.replaceChildren(line);
}

async function decideOnPost(article: HTMLElement, button: HTMLButtonElement, forceAction?: ComposerAction) {
  if (!canScanPosts(bridgeState, pageGate())) return;
  // A click on a busy ✦ stops it. It used to do nothing at all, and a ✦ has no pill with a
  // Cancel of its own, so there was no way to stop a slow draft.
  if (button.dataset.state === "working") {
    const live = opBindingFor(postButtonOps.get(button));
    if (live && !isTerminal(live.op)) live.onCancel?.();
    else setPostButtonWorking(button, false);
    return;
  }
  if (handleOrphaned()) return;
  // Signed out, offline, unverified, outdated: say so in the dock, send nothing.
  if (showBlockingState()) return;
  const context = extractContext(article);
  activeContext = context;
  openDock();
  setDockContext("reply", context, "working");
  releaseFeedAnchor(); // a fresh decision scrolls into view normally
  collapseFinishedCards();
  const own = Boolean(context.is_own_post);
  const waiting = decideWaitingCopy(forceAction === "quote" ? "quote" : "reply", own);
  // ONE card for the whole decision — no scattered bubbles. Loading = a compact
  // dots bubble; it only becomes a full decision card if the answer is WAIT.
  const op = beginOp("decide", "generating", addCard("decision-card"), waiting.label, {
    target: opTargetLabel("decide", context),
    resume: { kind: "decide", handle: context.author_handle, own },
  });
  postButtonOps.set(button, op.id);
  setPostButtonWorking(button, true);
  const settle = (event: OpEvent, renderTerminal?: (card: HTMLElement) => void, summary?: string) => {
    const binding = opBindings.get(op.id);
    if (binding) {
      binding.renderTerminal = renderTerminal;
      if (summary) binding.summary = summary;
    }
    applyOpEvent(op.id, event);
  };
  /** Cancelled mid-flight: the answer that arrives later must change nothing. */
  const abandoned = () => {
    const binding = opBindings.get(op.id);
    return !binding || isTerminal(binding.op);
  };
  const releaseButton = () => {
    if (postButtonOps.get(button) === op.id) setPostButtonWorking(button, false);
  };
  // While the request runs, stopping it is free: the worker aborts it and cancels it on the
  // server by its key, before a draft exists.
  setOpControls(op.id, {
    target: opTargetLabel("decide", context),
    onCancel: () => {
      void sendToWorker({ kind: MESSAGE_KINDS.assistCancel, requestId: op.id });
      settle({ type: "cancelled", keep: true }, card => renderStatusLine(card, "Stopped. No draft used."), "stopped");
      clearDockContext();
      releaseButton();
    },
  });
  // The request is one call with no progress events, so the card says only what is true while it
  // waits: still writing, when a wait is normal, and near the end that stopping costs nothing.
  // Timers are op-owned: any terminal transition clears them atomically.
  armOpHeartbeats(op.id, waiting.beats);
  try {
    const response = await sendToWorker({
      kind: MESSAGE_KINDS.assist, requestId: op.id, payload: { mode: "context", context },
    }) as ClientAssistResponse | null;
    // Cancelled while Starling was thinking. The answer is discarded whole —
    // Starling must not open a composer for a decision the user already
    // called off.
    if (abandoned()) return;
    releaseButton();
    if (!response && extensionReloaded) {
      // Left behind by an update: the "Starling was updated" card says what to do.
      settle({ type: "cancelled" });
      return;
    }
    if (!response?.ok || !response.decision) {
      if (response && isErrorCode(response.error_code)) {
        // A refusal with a code (sign-in, limit, rate limit…) gets its card
        // and its next step, not just a sentence.
        const failure = requestFailureFromResponse(response);
        settle({ type: "failed", error: failure.message }, card => {
          renderRequestFailure(card, failure, () => { void decideOnPost(article, button, forceAction); });
        }, "not drafted");
        return;
      }
      throw new Error(response?.error || "Starling couldn't read this post. Nothing in X changed.");
    }
    const decision = forceAction ? { ...response.decision, action: forceAction } : response.decision;
    const composerAction: ComposerAction | null =
      decision.action === "reply" || decision.action === "quote" || decision.action === "post" ? decision.action : null;
    if (composerAction === "reply" && decision.draft && visibleOwnReplyToContext(context, article)) {
      // The draft exists (and was paid for): "Write another reply" types it, no new request.
      settle({ type: "completed", hasContent: true }, card => {
        card.replaceChildren();
        renderAlreadyReplied(card, async () => {
          await chatActOnReply(card, decision, context, undefined, response.generation_id, response.candidate_id, { allowSecondReply: true });
        });
      }, "already replied");
      return;
    }
    setDockContext(composerAction ?? "reply", context, composerAction ? "updated" : "ready");

    if (composerAction && decision.draft) {
      // Actionable (reply/quote/post): the card shows ONLY a status line; the
      // draft itself types into X's own composer box — nothing is mirrored in
      // the chat. The card just "says something" while it works.
      const draft = decision.draft;
      applyOpEvent(op.id, { type: "heartbeat", note: `Opening X's ${actionLabel(composerAction)} box…` });
      // From here on the draft exists, so stopping no longer saves one: say so, and keep the draft.
      let deliverySession: ComposerSession | null = null;
      setOpControls(op.id, {
        target: opTargetLabel(composerAction, context),
        onCancel: () => {
          if (deliverySession) cancelComposerWork(deliverySession);
          settle({ type: "cancelled", keep: true }, card => {
            renderStatusLine(card, `Stopped before it reached X. Starling had already written it, so it used ${pressCost(bridgeState.session?.usage)}.`);
            offerDraftCopy(card, draft, composerAction, "Copy Starling's version");
          }, "stopped");
        },
      });

      const existingSessions = new Set(activeComposerSessions);
      const opened = await openNativeComposerForPost(article, composerAction, context);
      const session = opened && !abandoned() ? await waitForComposerSession(composerAction, 10_000, {
        context, sourceArticle: article, existing: existingSessions,
      }) : null;
      if (abandoned()) return;
      if (session) {
        deliverySession = session;
        applyOpEvent(op.id, { type: "typing-started" });
        applyOpEvent(op.id, { type: "heartbeat", note: `Typing the ${actionLabel(composerAction)} into X…` });
        // Types into X's composer ONLY (no card mirror).
        const result = await applyDraftToComposer(session, draft, response.generation_id, response.candidate_id);
        if (abandoned()) return;
        if (result.ok) {
          settle({ type: "completed", hasContent: true }, card => {
            const line = document.createElement("div");
            line.className = "deciding";
            // "Review it, then post it yourself" was the sentence he quoted
            // back at us. It says nothing he does not know and it was the ONLY
            // prose on the card. What changed and what it missed follow below.
            line.textContent = result.warning || `Typed into your ${actionLabel(composerAction)}.`;
            card.replaceChildren(line);
            appendDraftReason(card, response.decision);
            offerUndo(card, session);
          }, "in X");
        } else {
          settle({ type: "failed", error: result.error || `Couldn't finish your ${actionLabel(composerAction)}.` }, card => {
            const status = document.createElement("small");
            status.className = "status error";
            status.textContent = result.error || `Couldn't finish your ${actionLabel(composerAction)}.`;
            card.replaceChildren(status);
            offerDraftCopy(card, draft, composerAction, "Copy Starling's version");
          }, "not in X");
        }
      } else {
        // TWO CAUSES, ONE MESSAGE — and they need different answers. `opened`
        // false means X's own reply/repost control was absent or disabled on
        // this post; `opened` true with no session means the click landed and
        // no composer appeared inside 10s. A tester hit this on a mega-account
        // post and the card could not say which. Nothing logs composer-open
        // failures, so "is the rate high enough to matter" is unanswerable
        // today; naming the branch is what makes it countable. The copy
        // fallback below is unchanged — it is the designed degradation and it
        // worked.
        const why = opened
          ? `X's ${actionLabel(composerAction)} box didn't open in time.`
          : `X's ${actionLabel(composerAction)} button isn't available on this post.`;
        settle({ type: "failed", error: why }, card => {
          const status = document.createElement("small");
          status.className = "status error";
          status.textContent = `${why} Copy it in when ready.`;
          card.replaceChildren(status);
          offerDraftCopy(card, draft, composerAction);
        }, "not in X");
      }
    } else {
      // WAIT / SKIP (or no draft): grow into the full decision card — badge,
      // reason, and the two ways forward. A verdict costs nothing, and says so.
      settle({ type: "completed", hasContent: true }, card => {
        card.classList.add("decision");
        fillDecisionCard(card, decision);
        renderVerdictActions(card, article, context, decision);
      }, decision.action === "skip" ? "worth skipping" : "better to wait");
    }
  } catch (error) {
    if (abandoned()) return;
    releaseButton();
    if (handleOrphaned()) {
      settle({ type: "cancelled" });
      return;
    }
    const message = error instanceof Error && error.message && !/context invalidated/i.test(error.message)
      ? error.message : "Starling couldn't read this post. Nothing in X changed.";
    settle({ type: "failed", error: message }, card => {
      const status = document.createElement("small");
      status.className = "status error";
      status.textContent = message;
      card.replaceChildren(status);
      addChip(card, "Try again", chip => { chip.remove(); void decideOnPost(article, button, forceAction); });
    }, "not drafted");
  } finally {
    releaseButton();
  }
}

/** Idle labels for the ✦ button — self-reply and reply are different moves and
 *  the control must always say which one it is about to make. The tooltip's
 *  second line says what else it may do, and what it costs. */
function applyPostButtonLabels(button: HTMLButtonElement, isOwnPost: boolean) {
  const action = isOwnPost ? "Draft a self-reply with Starling" : "Draft a reply with Starling";
  button.title = `${action}\nIt may suggest a quote, or waiting. Uses ${pressCost(bridgeState.session?.usage)} if it writes one.`;
  button.setAttribute("aria-label", action);
}

/** What a card's header says the operation is acting on. Replying to your
 *  own post is a different move from replying to a stranger, so the card names
 *  it as one — there is never a moment where whose post this is has to be
 *  guessed from the surrounding page. */
function opTargetLabel(kind: "decide" | ComposerAction, context?: PostContext): string {
  const own = Boolean(context?.is_own_post);
  const handle = context?.author_handle ? `@${String(context.author_handle).replace(/^@/, "")}` : "";
  if (kind === "post") return "New post";
  if (kind === "decide") return own ? "Your own post" : handle ? `Post by ${handle}` : "This post";
  const verb = kind === "reply"
    ? (own ? "Self-reply" : "Reply")
    : (own ? "Self-quote" : "Quote");
  if (own) return `${verb} on your post`;
  return handle ? `${verb} to ${handle}` : verb;
}

/** A verdict of "better to wait" is advice, never a dead end: the two ways to act anyway. */
function renderVerdictActions(card: HTMLElement, article: HTMLElement, context: PostContext,
                              decision: NonNullable<AssistResponse["decision"]>) {
  const note = document.createElement("small");
  note.className = "status verdict-cost";
  note.textContent = "No draft used.";
  card.append(note);
  const row = document.createElement("div");
  row.className = "chips verdict-actions";
  const own = Boolean(context.is_own_post);
  const secondReplyOk = Boolean(decision.already_replied);
  const reply = document.createElement("button");
  reply.type = "button";
  reply.className = "copy-pill";
  reply.textContent = own ? "Write a self-reply anyway" : "Write a reply anyway";
  const quote = document.createElement("button");
  quote.type = "button";
  quote.className = "chip";
  quote.textContent = "Quote it instead";
  reply.addEventListener("click", () => void createForPostAnyway(article, context, "reply", card, { secondReplyOk }));
  quote.addEventListener("click", () => void createForPostAnyway(article, context, "quote", card));
  row.append(reply, quote);
  card.append(row);
}

/** "Write a reply anyway" / "Quote it instead" from a verdict card: open X's box and write it. */
async function createForPostAnyway(article: HTMLElement, context: PostContext, action: ComposerAction, card: HTMLElement,
                                   options: { secondReplyOk?: boolean } = {}) {
  const buttons = Array.from(card.querySelectorAll<HTMLButtonElement>(".verdict-actions button"));
  const setBusy = (busy: boolean) => buttons.forEach(button => { button.disabled = busy; });
  setBusy(true);
  const label = actionLabel(action);
  setCardActionStatus(card, `Opening X's ${label} box…`);
  scrollFeed();
  let target = article;
  if (!target.isConnected) {
    const found = findArticleByPostId(context.post_id ?? postIdFromUrl(context.post_url));
    if (!found) {
      setCardActionStatus(card, "Scroll back to the post, then press ✦ on it again. No draft was used.", true);
      setBusy(false);
      return;
    }
    target = found;
  }
  if (action === "reply" && !options.secondReplyOk && visibleOwnReplyToContext(context, target)) {
    setCardActionStatus(card, ALREADY_REPLIED);
    addChip(card, "Write another reply", chip => {
      chip.remove();
      void createForPostAnyway(target, context, "reply", card, { secondReplyOk: true });
    });
    setBusy(false);
    return;
  }
  const existingSessions = new Set(activeComposerSessions);
  const opened = await openNativeComposerForPost(target, action, context);
  const session = opened ? await waitForComposerSession(action, 10_000, {
    context, sourceArticle: target, existing: existingSessions,
  }) : null;
  if (!session) {
    setCardActionStatus(card, action === "quote"
      ? "Couldn't open X's quote box. Choose Repost, then Quote, on the post, and press ✦ Create quote. No draft was used."
      : "Couldn't open X's reply box. Click Reply on the post, then press ✦ Create reply. No draft was used.", true);
    setBusy(false);
    return;
  }
  // Empty composer + create intent → the create pipeline streams a draft in.
  const start = await enhanceComposer(session, { secondReplyOk: options.secondReplyOk });
  if (start !== "started") {
    setBusy(false);
    return;
  }
  setCardActionStatus(card, `Writing your ${label}…`);
  const writing = opBindingFor(session.activeOpId);
  if (writing) {
    writing.onSettled = phase => {
      setCardActionStatus(card, phase === "completed" ? `Your ${label} is in X's box.` : `Not written. The card below says why.`, phase !== "completed");
    };
  }
}

function fillDecisionCard(card: HTMLElement, decision: NonNullable<AssistResponse["decision"]>) {
  card.innerHTML = "";
  const extra = decision as unknown as Record<string, unknown>;
  const actionable = decision.action === "reply" || decision.action === "quote" || decision.action === "post";

  // The call: one tight line — badge · stats (accelerating in green) · Why?.
  const call = document.createElement("div");
  call.className = "call";
  const badge = document.createElement("span");
  badge.className = "action-badge";
  badge.dataset.action = decision.action;
  badge.textContent = decisionBadgeLabel(decision.action);
  const meta = document.createElement("span");
  meta.className = "call-meta";
  const statsText = typeof extra.stats_line === "string" && extra.stats_line ? extra.stats_line : (decision.confidence || "");
  // Highlight "accelerating" — the live-velocity signal — in green.
  const accelMatch = statsText.match(/(still )?accelerating/i);
  if (accelMatch) {
    meta.append(document.createTextNode(statsText.slice(0, accelMatch.index).replace(/·\s*$/, "").trimEnd() + " · "));
    const accel = document.createElement("span");
    accel.className = "accel";
    accel.textContent = accelMatch[0];
    meta.append(accel);
  } else {
    meta.textContent = statsText;
  }
  call.append(badge, meta);
  // WHY? MUST HAVE SOMETHING LEFT TO SAY.
  //
  // On a non-actionable card the rationale is ALREADY printed below as the
  // standing `wait-reason` paragraph, so a Why? whose only content was the
  // rationale printed the same sentence twice — a tester hit it on the first
  // card. The gate now asks whether the click would add anything: reasons the
  // card is not showing, or (on an actionable card, where nothing is standing)
  // the rationale itself.
  if (decision.reasons?.length || (actionable && decision.rationale)) {
    const why = document.createElement("button");
    why.type = "button";
    why.className = "why-btn";
    why.textContent = "Why?";
    why.addEventListener("click", () => {
      why.remove();
      const detail = document.createElement("p");
      detail.className = "why-detail";
      const reasons = (decision.reasons || []).map(r => `• ${r}`).join("\n");
      // The rationale only when the card is not already showing it — see the
      // gate above. On an actionable card the draft is the anchor and nothing
      // is standing, so the detail is its only home.
      detail.textContent = [actionable ? decision.rationale : null, reasons].filter(Boolean).join("\n");
      // Insert the reasoning right after the call line (never insertBefore a node
      // that isn't a child of the card — that threw NotFoundError on drafts).
      call.after(detail);
      scrollFeed();
    });
    call.append(why);
  }
  card.append(call);

  // WAIT/SKIP: the rationale IS the answer — show it. Actionable: the draft is
  // the anchor (decideOnPost adds it), so the reasoning stays behind Why.
  if (!actionable && decision.rationale) {
    const rationale = document.createElement("p");
    rationale.className = "body wait-reason";
    rationale.textContent = decision.rationale;
    card.append(rationale);
  }
}

/**
 * Say what the draft did, and name the bar it missed — under the status line.
 *
 * HIS COMPLAINT WAS THIS CARD. "the enhance post thing needs to be better, it
 * told me to review it myself, like wtf." The card's entire prose was one
 * hardcoded sentence — "Typed into your reply. Review it, then post it
 * yourself." — while the engine computed and transmitted `rationale`,
 * `reasons`, `needs_review` and `review_note` for that same request and the
 * card read none of them.
 *
 * `rationale` already carries BOTH halves: `_rationale_text` composes
 * `[weakness, what-it-did]`, so rendering it is the whole message. `review_note`
 * repeats the weakness verbatim, which is why it is used as a SIGNAL here and
 * not printed again — printing both was the duplicate this project keeps
 * shipping in other forms.
 *
 * SCOPE, deliberately narrow: the test is "does this change whether he posts?"
 * `review_note` passes — an unbacked claim is the thing he would want to catch.
 * `likely_reaction` and `downside` do not; they are commentary, and his other
 * complaint is that the card talks at him.
 */
function appendDraftReason(card: HTMLElement, decision: AssistDecision | undefined, extraSources?: unknown) {
  if (!decision) {
    appendSources(card, extraSources);
    return;
  }
  // THE FIELDS THIS CARD RENDERS, in one place. Adding one is a line here;
  // everything below walks what this produces.
  //   rationale    what the draft did, and (composed upstream) the bar it missed
  //   review_note  the bar, verbatim — appended only if the rationale does not
  //                already carry it, because `_rationale_text` composes
  //                [weakness, what-it-did] and printing both would repeat it
  //   needs_review a SIGNAL, never a sentence: it styles the line
  //   web_checked  ONLY when false — see the census below
  // DECLARED-ONLY and deliberately absent: likely_reaction (required in
  // types.ts, read by nothing but tests), downside, score_band, growth_score,
  // thread_temperature, image_decision. All confirmed dead; none changes
  // whether he posts beyond what review_note already covers.
  //
  // THE CENSUS BEHIND web_checked, 2026-08-08. Five fields were proposed for
  // this card. Four fail the does-it-change-whether-he-posts bar on measurement,
  // not on taste:
  //   reasons            hardcoded `[]` on every draft decision (main.py) —
  //                      populated only on opportunity assessments, which this
  //                      card does not render
  //   likely_reaction    hardcoded "Ready for your review in X." on every draft
  //   downside           absent from the draft decision entirely; present on
  //                      3 of 37 stored assessments
  //   thread_temperature "unknown" on 81 of his 84 real reply contexts (96%)
  // web_checked is the one that carries information: on his real contexts the
  // facts reached the writer on 6% of replies and 71% of quotes.
  //
  // FALSE ONLY, and that is the whole design. A "web checked" badge would
  // credential a draft the same way a quality score would — he should check a
  // named fact either way — while the SKIP is the thing he cannot otherwise
  // see. The engine's own comment calls that skip visible; it was not.
  const rationale = (decision.rationale ?? "").trim();
  const note = (decision.review_note ?? "").trim();
  const parts: string[] = [];
  if (rationale) parts.push(rationale);
  // Substring rather than equality: the composed rationale contains the note
  // plus the account of the work, so an equality test would print it twice.
  if (note && !rationale.includes(note)) parts.unshift(note);
  // Last: it is a caution about provenance, not the account of the work, and
  // it must never displace the bar the draft actually missed. Only for a draft
  // that makes the kind of claim worth checking (a number, an @name, a date):
  // on a draft with none, "no web check" warns about nothing.
  // A search that was due and did not help says exactly why (late, used up, nothing found);
  // otherwise the generic caution, for a draft that makes a claim worth checking.
  const searchNote = typeof decision.search_note === "string" ? decision.search_note.trim() : "";
  if (searchNote) {
    parts.push(searchNote);
  } else if (decision.web_checked === false && draftHasCheckableClaims(decision.draft)) {
    parts.push("No web check on this one — treat any specific claim in it as unverified.");
  }
  const text = parts.join(" ").trim();
  if (text) {
    const line = document.createElement("p");
    line.className = decision.needs_review ? "wait-reason needs-review" : "wait-reason";
    // Whose words these are: the reason line is Starling's note, not part of the draft.
    const label = document.createElement("strong");
    label.className = "note-label";
    label.textContent = "Starling's note: ";
    line.append(label, document.createTextNode(text));
    card.append(line);
  }
  // A chat answer's own research comes after the draft's sources; a page in both is shown once.
  appendSources(card, mergeSources(decision.sources, extraSources));
  appendVoiceNote(card, decision.voice_note);
}

/** A created post names the story Starling picked for it: "About: {topic}". */
function appendTopicLine(card: HTMLElement, decision: AssistDecision | undefined) {
  const text = topicLine(decision?.topic);
  if (!text) return;
  const line = document.createElement("small");
  line.className = "status topic-line";
  line.textContent = text;
  card.append(line);
}

/** A new account's drafts cannot sound like its owner yet. The card says so, with the one step that
 *  fixes it, instead of leaving them to wonder why the draft reads like anyone's. */
function appendVoiceNote(card: HTMLElement, note: unknown) {
  if (typeof note !== "string" || !note.trim()) return;
  const line = document.createElement("p");
  line.className = "voice-note";
  line.append(document.createTextNode(note.trim() + " "));
  const add = document.createElement("button");
  add.type = "button";
  add.className = "note-btn";
  add.textContent = "Add writing samples";
  add.addEventListener("click", () => openWritingSamples());
  line.append(add);
  card.append(line);
}

/** The pages behind a draft or an answer, as links that open in a new tab. OpenAI's web search terms
 *  require clickable citations for anything drawn from its results, and a named fact is worth one
 *  click to check. Each link names its site, so nobody clicks blind. */
function appendSources(card: HTMLElement, raw: unknown) {
  const links = sourceLinks(raw);
  if (!links.length) return;
  const line = document.createElement("p");
  line.className = "sources";
  const label = document.createElement("strong");
  label.className = "note-label";
  label.textContent = links.length === 1 ? "Source: " : "Sources: ";
  line.append(label);
  links.forEach((link, index) => {
    if (index) line.append(document.createTextNode(" · "));
    const anchor = document.createElement("a");
    anchor.href = link.href;
    anchor.target = "_blank";
    anchor.rel = "noopener noreferrer";
    anchor.textContent = link.label;
    line.append(anchor);
    if (link.label !== link.host) {
      const site = document.createElement("span");
      site.className = "source-site";
      site.textContent = ` (${link.host})`;
      line.append(site);
    }
  });
  card.append(line);
}

function offerDraftCopy(card: HTMLElement, draft: string, action: ComposerAction, copyLabel = "Copy") {
  addChip(card, copyLabel, async chip => {
    const copied = await navigator.clipboard.writeText(draft).then(() => true, () => false);
    if (copied) {
      chip.textContent = "Copied";
      return;
    }
    setCardActionStatus(card, "Couldn't copy that draft. Select the text and copy it manually.", true);
  });
  addChip(card, "Download .txt", chip => {
    if (downloadText(`starling-${actionLabel(action)}.txt`, draft)) {
      chip.textContent = "Downloaded";
      return;
    }
    setCardActionStatus(card, "Couldn't start that download. Copy the draft instead.", true);
  });
}

/** Programmatically open X's native composer for one action. Never submits. */
async function openNativeComposerForPost(article: HTMLElement, action: ComposerAction, context: PostContext): Promise<boolean> {
  clearComposerIntents();
  if (action === "reply") {
    const reply = article.querySelector<HTMLElement>('[data-testid="reply"]');
    if (!reply || !isNativeControlEnabled(reply) || isReplyControlDisabled(article)) return false;
    pendingComposerIntent = createComposerIntent("reply", context, article, { handoff: true });
    try { clickStarlingNativeControl(reply); } catch { clearComposerIntents(); return false; }
    return true;
  }
  if (action === "quote") {
    const repost = article.querySelector<HTMLElement>('[data-testid="retweet"], [data-testid="unretweet"]');
    if (!repost) return false;
    pendingQuoteIntent = { context, sourceArticle: article, expiresAt: Date.now() + 15_000 };
    try { clickStarlingNativeControl(repost); } catch { clearComposerIntents(); return false; }
    const menuItem = await waitForQuoteMenuItem(3_000);
    if (!menuItem) {
      // Never leave X's repost menu dangling open.
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      pendingQuoteIntent = null;
      return false;
    }
    pendingQuoteIntent = null;
    pendingComposerIntent = createComposerIntent("quote", context, article, { handoff: true });
    try { clickStarlingNativeControl(menuItem); } catch { clearComposerIntents(); return false; }
    return true;
  }
  const compose = document.querySelector<HTMLElement>('[data-testid="SideNav_NewTweet_Button"]');
  if (!compose) return false;
  pendingComposerIntent = createComposerIntent("post", context, article, { handoff: true });
  try { clickStarlingNativeControl(compose); } catch { clearComposerIntents(); return false; }
  return true;
}

function waitForQuoteMenuItem(timeoutMs: number): Promise<HTMLElement | null> {
  return new Promise(resolve => {
    const start = Date.now();
    const tick = () => {
      const quote = Array.from(document.querySelectorAll<HTMLElement>('[role="menuitem"]'))
        .find(item => /quote/i.test(item.innerText));
      if (quote) return resolve(quote);
      if (Date.now() - start > timeoutMs) return resolve(null);
      requestAnimationFrame(tick);
    };
    tick();
  });
}

function samePostContext(left: PostContext | undefined, right: PostContext | undefined): boolean {
  return sameComposerPostReference(left, right);
}

function sessionMatchesComposerTarget(session: ComposerSession, action: ComposerAction,
                                     target?: ComposerSessionTarget): boolean {
  if (session.action !== action) return false;
  if (!target) return true;
  // Starling-initiated flows always carry the exact context through their
  // pending intent. A status id/permalink can briefly be absent from X's DOM
  // after opening a reply, so accept only the *same element identity* Starling
  // clicked as the equally exact alternate proof. Never use proximity: X can
  // show several nearby replies, and a safe refusal is better than a draft
  // landing in a merely plausible composer.
  if (!matchesComposerHandoffTarget({
    sessionAction: session.action,
    targetAction: action,
    sessionContext: session.context,
    targetContext: target.context,
    sessionSource: session.sourceArticle,
    targetSource: target.sourceArticle,
  })) return false;
  // A pre-existing non-empty composer belongs to the user. Even when it is
  // replying to the same post, never replace it merely because a Starling
  // action opened beside it. A blank matching surface remains safe to use.
  return !target.existing?.has(session) || !readableComposerText(session.composer).trim();
}

function clearPendingComposerTarget(action: ComposerAction, target?: ComposerSessionTarget) {
  const pending = pendingComposerIntent;
  if (!pending || pending.action !== action) return;
  if (!target || samePostContext(pending.context, target.context) || pending.sourceArticle === target.sourceArticle) {
    pendingComposerIntent = null;
  }
}

function waitForComposerSession(action: ComposerAction, deadlineMs: number,
                                target?: ComposerSessionTarget): Promise<ComposerSession | null> {
  return new Promise(resolve => {
    const start = Date.now();
    const tick = () => {
      scanComposerEnhancers();
      // A reply opened by Starling's click lands in X's modal. A box already
      // open on the page under the same post would match too, and X throws it
      // away a moment later: it may take the reply only once X has had time to
      // open its modal and has not (`mayReuseOpenReplyBox`).
      const sessions = [...activeComposerSessions];
      const modalOpen = sessions.some(item => !item.disposed && Boolean(item.composer.closest("[role='dialog']")));
      const pageBoxMayAnswer = action !== "reply" || !target || mayReuseOpenReplyBox(Date.now() - start, modalOpen);
      const session = sessions.find(item =>
        !item.disposed
        && isUsableComposer(item.composer)
        && (pageBoxMayAnswer || Boolean(item.composer.closest("[role='dialog']")))
        && sessionMatchesComposerTarget(item, action, target));
      if (session) return resolve(session);
      if (Date.now() - start > deadlineMs) {
        // Do not let a timed-out Starling click claim some later, unrelated X
        // composer through a stale intent.
        clearPendingComposerTarget(action, target);
        return resolve(null);
      }
      window.setTimeout(tick, 120);
    };
    window.setTimeout(tick, 150);
  });
}

/** Type an already-decided draft into a freshly opened composer (no new model call). */
async function applyDraftToComposer(session: ComposerSession, draft: string, generationId?: string, candidateId?: string,
                                    onProgress?: (prefix: string) => void): Promise<{ ok: boolean; changed?: boolean; error?: string; warning?: string }> {
  const requestId = ++session.requestId;
  session.control.dataset.state = "sending";
  updateComposerControl(session);
  // A fresh composer's "expected original" is its current (normalized-empty)
  // text, not a hard "" — placeholders and zero-width chars vary.
  const freshOriginal = readableComposerText(session.composer);
  session.activeOriginal = freshOriginal;
  session.activeInputRevision = undefined;
  const result = await typewriterInsert(session, freshOriginal, draft, requestId, onProgress);
  if (session.requestId !== requestId) return result;
  if (result.ok && result.changed) {
    // What Undo needs: the box's text before (a fresh box: empty) and exactly what was typed.
    session.undo = { original: freshOriginal, inserted: draft };
  }
  if (result.ok && generationId && result.changed) {
    session.generationId = generationId;
    session.candidateId = candidateId;
    session.lastRecordedText = draft;
    recordComposerOutcome(session, {
      generation_id: generationId, candidate_id: candidateId, status: "inserted",
      final_text: draft, metadata: { source: "per_post_decide", action: session.action },
    });
  }
  clearActiveComposerWork(session);
  // "✓ Done" only when something was typed; a box Starling left alone goes straight back to idle.
  session.control.dataset.state = result.ok ? (result.changed ? "updated" : "idle") : "error";
  updateComposerControl(session);
  window.setTimeout(() => {
    if (session.requestId === requestId && (session.control.dataset.state === "updated" || session.control.dataset.state === "error")) {
      session.control.dataset.state = "idle";
      updateComposerControl(session);
    }
  }, result.ok ? 1_600 : 6_000);
  return result;
}

function resolveComposerIntent(composer: HTMLElement, visible: HTMLElement[]): ComposerIntent | null {
  const now = Date.now();
  clearExpiredComposerIntents(now);
  // Launcher-originated intents bind by direct identity (surface containment /
  // submit-button match), because the click was on Starling's own pill — this is
  // what makes reply creation ONE click on detail pages, where X pre-mounts
  // the reply composer BESIDE the article and the strict native matrix below
  // would fail closed forever.
  if (pendingLauncherIntent && now >= pendingLauncherIntent.expiresAt) pendingLauncherIntent = null;
  if (pendingLauncherIntent) {
    const launcher = pendingLauncherIntent;
    const candidates = visible.map(candidate => ({
      composer: candidate,
      insideSurface: launcher.surface.isConnected && launcher.surface.contains(candidate),
      submitMatches: nearestComposerSubmitButton(candidate) === launcher.submit,
      existedBefore: launcher.intent.visibleBefore.has(candidate),
    }));
    const selected = selectLauncherComposer(candidates);
    if (selected?.composer === composer) {
      // Deliberately NOT consumed here: session creation can still bail (e.g.
      // the submit is zero-size mid-expand-animation) and must re-claim on
      // the next scan tick. launchCollapsedComposerFlow clears the intent
      // when its wait resolves either way, and it expires after 10s.
      return { ...launcher.intent, launcherSubmit: launcher.submit };
    }
    if (selected) return null; // the launcher claims a different composer — this one resolves below
    // No claimable candidate yet: unlike pendingComposerIntent, do NOT block
    // unrelated composers — the launcher channel is scoped to its own surface.
  }
  // Read only when asked: it now reads the post the box answers, which is work
  // a box claimed by a pending intent never needs.
  let inferred: ComposerIntent | null | undefined;
  const inferredReply = () => (inferred === undefined ? (inferred = inferReplyComposerIntent(composer)) : inferred);
  if (pendingComposerIntent) {
    const intent = pendingComposerIntent;
    // X answers a Reply click with its modal composer (at /compose/post), and a
    // box that was already open under the post is about to be thrown away. Only
    // when X has had time to open the modal and has not may that box take the
    // reply; a modal box, when there is one, always wins.
    const modalComposerOpen = visible.some(candidate => Boolean(candidate.closest("[role='dialog']")));
    const mayReuse = mayReuseOpenReplyBox(now - intent.createdAt, modalComposerOpen);
    const candidates = visible
      .filter(candidate => intent.action !== "reply" || mayReuse || Boolean(candidate.closest("[role='dialog']")))
      .map(candidate => ({
        composer: candidate,
        wasVisibleBefore: intent.visibleBefore.has(candidate),
        belongsToTargetPost: intent.action === "reply" && intent.sourceArticle != null
          && articleForComposer(candidate) === intent.sourceArticle,
      }));
    const selected = selectSingleSafeComposerCandidate(intent.action, candidates);
    if (selected?.composer === composer) {
      // Keep this claim alive until the session has actually mounted. X often
      // reveals its Post/Reply toolbar a frame after the editable surface; if
      // mounting has to retry, consuming the intent here strands a real reply
      // box and incorrectly sends the user to Copy instead.
      return intent;
    }
    // Some X reply surfaces are already mounted when the Reply action is
    // clicked. Their explicit “Replying to @…” label is enough to bind the
    // control to that reply instead of treating it as an ambiguous draft —
    // inside X's modal at once, and for a box on the page only on the same
    // terms as above.
    if (intent.action === "reply" && (composer.closest("[role='dialog']") || mayReuse) && inferredReply()) return intent;
    // An explicit X action is still in flight or ambiguous. Never fall back to
    // a different pre-existing composer while it is pending.
    return null;
  }
  const inlineArticle = composer.closest<HTMLElement>(POST_SELECTOR);
  if (inlineArticle && !isReplyControlDisabled(inlineArticle)) {
    return createComposerIntent("reply", extractContext(inlineArticle), inlineArticle);
  }
  const reply = inferredReply();
  if (reply) return reply;
  // The home timeline composer is a persistent, user-visible compose surface.
  // Unlike a reply, it does not need a preceding click before Starling can offer
  // the same Create post action beside X's native Post button.
  if (location.pathname === "/home" && !composer.closest<HTMLElement>(POST_SELECTOR)) {
    return createComposerIntent("post");
  }
  // A direct /compose/post route is itself an unambiguous user-selected X
  // composer. Ordinary pre-existing home-page composers deliberately do not
  // get a control without a captured native action.
  // A modal that shows a post above its box is answering that post (X draws
  // "Replying to" a moment later), never a new post: wait for the next scan.
  if (/^\/compose\/post/.test(location.pathname) && visible.length === 1 && visible[0] === composer
    && !composer.closest<HTMLElement>("[role='dialog']")?.querySelector(POST_SELECTOR)) {
    return createComposerIntent("post");
  }
  return null;
}

function inferReplyComposerIntent(composer: HTMLElement): ComposerIntent | null {
  const replyMarker = /\breplying\s+to\s+@[\w_]+/i;
  const dialog = composer.closest<HTMLElement>("[role='dialog']");
  if (dialog && replyMarker.test(dialog.innerText)) {
    // THE POST IS IN THE MODAL. X draws the post being answered above its reply
    // box. This used to bind a reply with no post at all, so "Create reply" in
    // X's modal sent a request the service can only refuse.
    const source = replyDialogSource(dialog, composer);
    return createComposerIntent("reply", source?.context, source?.sourceArticle);
  }

  // Inline reply forms are not always nested inside the post article. Keep the
  // search local to the composer so an unrelated timeline post cannot opt in.
  let surface = composer.parentElement;
  for (let depth = 0; surface && depth < 12; depth += 1, surface = surface.parentElement) {
    const marker = surface.innerText.match(/\breplying\s+to\s+@([\w_]+)/i);
    if (marker) {
      // The post just above the box, when its author is the one X says this replies to.
      const article = articleForComposer(composer);
      const named = article && extractAuthorHandle(article) === normalizeHandle(marker[1]) ? article : undefined;
      return createComposerIntent("reply", named ? extractContext(named) : undefined, named);
    }
  }
  return null;
}

/**
 * The post X's modal reply box answers, and the same post on the page behind it.
 *
 * X shows the post as an `article` above the box, but without its permalink
 * (checked on x.com, 30 Oct 2026: author, text and time, no status link), so the
 * copy in the modal cannot say which post it is. The post is almost always on
 * the page underneath (the timeline or the post's own page): when exactly one
 * post there has the same author and text, the context is read from it, with
 * its id and address. Otherwise the modal's copy is used as it is.
 */
function replyDialogSource(dialog: HTMLElement, composer: HTMLElement): { context: PostContext; sourceArticle?: HTMLElement } | null {
  const shown = Array.from(dialog.querySelectorAll<HTMLElement>(POST_SELECTOR))
    .filter(article => !article.parentElement?.closest(POST_SELECTOR)
      && Boolean(article.compareDocumentPosition(composer) & Node.DOCUMENT_POSITION_FOLLOWING))
    .at(-1);
  if (!shown) return null;
  const handle = extractAuthorHandle(shown);
  const text = normalizedPostText(shown);
  if (!handle || !text) return null;
  const sameOnPage = Array.from(document.querySelectorAll<HTMLElement>(POST_SELECTOR))
    .filter(article => !article.closest("[role='dialog']")
      && !article.parentElement?.closest(POST_SELECTOR)
      && findPermalink(article)
      && extractAuthorHandle(article) === handle
      && normalizedPostText(article) === text);
  const ids = new Set(sameOnPage.map(article => postIdFromUrl(findPermalink(article)?.href)));
  const onPage = sameOnPage.length > 0 && ids.size === 1 ? sameOnPage[0] : undefined;
  return { context: extractContext(onPage ?? shown), sourceArticle: onPage };
}

function normalizedPostText(article: HTMLElement): string {
  return (findPrimaryTextElement(article)?.innerText ?? "").replace(/\s+/g, " ").trim();
}

/** Read the post a reply or quote box answers again, for a session that was bound without one. */
function recoverComposerSource(session: ComposerSession): { context: PostContext; sourceArticle?: HTMLElement } | null {
  if (session.action === "post") return null;
  const dialog = session.composer.closest<HTMLElement>("[role='dialog']");
  if (dialog) return session.action === "reply" ? replyDialogSource(dialog, session.composer) : null;
  const article = session.sourceArticle?.isConnected ? session.sourceArticle : articleForComposer(session.composer);
  return article ? { context: extractContext(article), sourceArticle: article } : null;
}

function composerModeFor(session: ComposerSession): ComposerMode {
  return composerMode({
    text: readableComposerText(session.composer),
    isComposing: session.composing,
    xAcceptsDraft: xAcceptsComposerDraft(session.composer),
  });
}

function updateComposerControl(session: ComposerSession) {
  const mode = composerModeFor(session);
  const previousState = session.control.dataset.state as ComposerControlState | undefined;
  const text = readableComposerText(session.composer);
  const state = resolveComposerControlState({ previous: previousState, mode, text, undo: session.undo });
  const working = state === "sending" || state === "typing";
  // X can temporarily disable its own Post button while Starling is working.
  // That must never disable Starling's Cancel action. "✓ Done" is disabled for
  // the moment it shows, so a second click cannot rewrite the reply.
  session.control.disabled = isComposerControlDisabled(state, mode);
  session.control.dataset.mode = mode;
  session.control.dataset.state = state;
  refreshPillTheme(session);
  const hasDraft = Boolean(text.trim());
  const copy = composerControlCopy(session.action, mode, state, hasDraft,
                                   Boolean(session.context?.is_own_post), draftsPerRequest(bridgeState.session?.usage));
  const visibleText = session.control.querySelector<HTMLElement>(".vf-enhance-text");
  const mark = session.control.querySelector<HTMLElement>(".vf-enhance-mark");
  if (mark) mark.textContent = state === "updated" ? "✓" : "✦";
  // While working, the dots overlay the (invisible) label — the label's text
  // is deliberately LEFT ALONE so the pill keeps its exact width. Blanking it
  // here is what used to shrink the pill into a tiny dots-circle.
  if (visibleText && !working) visibleText.textContent = copy.visible;
  session.control.setAttribute("aria-busy", String(working));
  // aria-label only — no title: the pill's text already says what it does, and
  // a native tooltip hovering over the compose row reads as browser noise.
  session.control.setAttribute("aria-label", copy.ariaLabel);
  session.control.removeAttribute("title");
}

/**
 * Re-read the surface under a composer pill, at most once a second. X switches theme with no
 * reload, and the pill lives in X's own toolbar: it must keep matching the page it sits on.
 */
function refreshPillTheme(session: ComposerSession) {
  const now = Date.now();
  if (session.themeCheckedAt !== undefined && now - session.themeCheckedAt < 1_000) return;
  session.themeCheckedAt = now;
  const toolbar = session.nativePostButton?.closest<HTMLElement>('[data-testid="toolBar"]');
  const theme = pillThemeFor(surfaceTone(composerThemeSource([toolbar, nativeComposerScope(session.composer), session.composer])));
  if (session.host.dataset.theme !== theme) session.host.dataset.theme = theme;
}

function xAcceptsComposerDraft(composer: HTMLElement): boolean {
  const scope = nativeComposerScope(composer);
  const submits = scope ? visibleComposerSubmitButtons(scope) : [];
  return isNativeComposerSubmitReady({
    matchingSubmitCount: submits.length,
    enabled: submits.length === 1 && isNativeControlEnabled(submits[0]),
  });
}

function isComposerReady(composer: HTMLElement, isComposing = false): boolean {
  // Post-insertion verification: the composer must hold publishable text.
  return composerMode({
    text: readableComposerText(composer),
    isComposing,
    xAcceptsDraft: xAcceptsComposerDraft(composer),
  }) === "enhance";
}

function nativeComposerScope(composer: HTMLElement): HTMLElement | null {
  const dialog = composer.closest<HTMLElement>("[role='dialog']");
  if (dialog) return dialog;

  // Reply forms can live beside—not inside—the source post. Walk only their
  // nearby wrappers until we find the toolbar or submit action that belongs to
  // this exact composer, then stop before we reach the page-level timeline.
  const localScope = localComposerScope(composer);
  if (localScope) return localScope;

  return composer.closest<HTMLElement>(POST_SELECTOR) ?? composer.parentElement;
}

function actionLabel(action: ComposerAction): string {
  return action === "reply" ? "reply" : action === "quote" ? "quote" : "post";
}

function clearActiveComposerWork(session: ComposerSession) {
  session.cancelActive = undefined;
  session.activeOriginal = undefined;
  session.activeInputRevision = undefined;
  session.activeOpId = undefined;
}

function cancelComposerWork(session: ComposerSession): boolean {
  const state = session.control.dataset.state as ComposerControlState | undefined;
  if (state !== "sending" && state !== "typing") return false;
  const binding = opBindingFor(session.activeOpId);
  if (binding && isTerminal(binding.op)) return false;

  const original = session.activeOriginal;
  const canRestore = state === "typing"
    && original != null
    && session.activeInputRevision === session.inputRevision;
  // Invalidate every pending callback before cancelling the transport, because
  // its final event may be delivered synchronously.
  session.requestId += 1;
  session.cancelActive?.();
  typewriterFastForward = false;
  if (canRestore) writeComposerText(session, original);

  const opId = session.activeOpId;
  // Stopped while part of the draft was showing on the card: the server may count that as a draft
  // used, so the card stays and says what the stop cost once the server answers (`onStopped`).
  const shown = state === "sending" && Boolean(binding?.preview);
  clearActiveComposerWork(session);
  session.control.dataset.state = "idle";
  updateComposerControl(session);
  // STRICT: a cancelled generation leaves NOTHING behind — the "Replying to…"
  // chip disappears with it instead of lingering as stale context.
  clearDockContext();
  // One dispatch settles the card: timers cleared, and a cancelled op with no
  // useful content REMOVES its card — the restored draft sitting in X's box is
  // the visible confirmation, not a "Cancelled." line under stale dots.
  if (binding && shown) binding.renderTerminal = card => renderStatusLine(card, `Stopped. ${NOTHING_CHANGED}`);
  if (opId) applyOpEvent(opId, { type: "cancelled", restoredOriginal: canRestore, keep: shown });
  return true;
}

/** Under a stopped card, once the server has said: what the stop cost ("This used 1 draft because…"). */
function showStopCost(card: HTMLElement | undefined, outcome: StoppedOutcome) {
  const text = stoppedCostLine(outcome, bridgeState.session?.usage);
  if (!card?.isConnected || !text) return;
  let line = card.querySelector<HTMLElement>(":scope > [data-stop-cost]");
  if (!line) {
    line = document.createElement("small");
    line.className = "status";
    line.dataset.stopCost = "true";
    card.append(line);
  }
  line.textContent = text;
  scrollFeed();
}

type EnhanceStart = "started" | "busy" | "unavailable" | "blocked" | "already-replied";

interface EnhanceOptions {
  /** A launcher's op, already on screen as "Opening X's reply box…", to continue in the same card. */
  existingOpId?: string;
  /** "Try another": the box still holds Starling's last draft; write a different one from the
   *  customer's own text (or from nothing, for Create), replacing the one in the box. */
  another?: boolean;
  /** "Write another reply" on a post the customer already replied to. */
  secondReplyOk?: boolean;
}

async function enhanceComposer(session: ComposerSession, options: EnhanceOptions = {}): Promise<EnhanceStart> {
  // A click must always DO something — surface every reason we can't proceed
  // instead of returning quietly (that read as "the button does nothing").
  if (handleOrphaned()) {
    failAdoptedOp(options.existingOpId, UPDATED_LINE);
    return "unavailable";
  }
  if (session.disposed) {
    failAdoptedOp(options.existingOpId, "X reloaded that box. Click into it and press ✦ again.");
    return "unavailable";
  }
  if (!canScanPosts(bridgeState, pageGate())) {
    failAdoptedOp(options.existingOpId, "Starling is off on this page, so it did not change your draft.");
    return "unavailable";
  }
  if (blocksRequests(dockStateFor(dockInputsFrom(bridgeState)))) {
    // The state card (sign in, offline, verify…) replaces the pending card.
    const adoptedOp = opBindingFor(options.existingOpId);
    if (adoptedOp && !isTerminal(adoptedOp.op)) applyOpEvent(adoptedOp.op.id, { type: "cancelled" });
    showBlockingState();
    return "unavailable";
  }
  if (!session.control.isConnected || !session.composer.isConnected) {
    failAdoptedOp(options.existingOpId, "X reloaded that box. Click into it and press ✦ again.");
    return "unavailable";
  }
  const state = session.control.dataset.state;
  if (state === "sending" || state === "typing") {
    if (options.existingOpId && session.activeOpId !== options.existingOpId) {
      failAdoptedOp(options.existingOpId, "Starling is already working in that X composer. Wait for it to finish or cancel it there first.");
    }
    return "busy";
  }
  // What is in the box now (and must still be there when the draft is typed over it), and what the
  // request is written from. They differ only for "Try another": the box holds Starling's previous
  // draft, and the new one is written from the customer's own words, not from Starling's.
  const original = readableComposerText(session.composer);
  const undo = session.undo;
  const retrying = Boolean(options.another && undo && !undo.restored && sameComposerText(original, undo.inserted));
  const requestText = retrying && undo ? undo.original : original;
  const boxMode = composerModeFor(session);
  if (boxMode === "blocked") {
    failAdoptedOp(options.existingOpId, "Finish typing that word, then press ✦ again.");
    return "blocked";
  }
  const mode: ComposerMode = requestText.trim() ? "enhance" : "create";
  if (mode === "create" && session.action !== "post" && !session.context) {
    // A reply or quote is written TO a post. A box bound without its post (X's
    // modal, X rebuilding the box) reads it now; with no post to read, nothing
    // is sent: the service can only refuse it, and its refusal reached the
    // reader as "Starling's AI isn't responding".
    const recovered = recoverComposerSource(session);
    if (recovered) {
      session.context = recovered.context;
      session.sourceArticle ??= recovered.sourceArticle;
    } else {
      failAdoptedOp(options.existingOpId, missingSourceLine(session.action));
      return "unavailable";
    }
  }
  if (mode === "create" && session.action === "reply" && !options.secondReplyOk && !retrying
    && visibleOwnReplyToContext(session.context, session.sourceArticle)) {
    const adoptedOp = opBindingFor(options.existingOpId);
    if (adoptedOp && !isTerminal(adoptedOp.op)) applyOpEvent(adoptedOp.op.id, { type: "cancelled" });
    showAlreadyRepliedNotice(session.context, () => { void enhanceComposer(session, { ...options, existingOpId: undefined, secondReplyOk: true }); });
    return "already-replied";
  }
  const requestId = ++session.requestId;
  session.activeOriginal = original;
  session.activeInputRevision = undefined;
  session.control.dataset.state = "sending";
  updateComposerControl(session);
  activeContext = session.context;
  releaseFeedAnchor(); // the "writing…" status card scrolls into view normally
  setDockContext(session.action, session.context, "working");
  const defaultLabel = mode === "create"
    ? `Writing your ${actionLabel(session.action)}…`
    : `Enhancing your ${actionLabel(session.action)}…`;
  const resume: ResumeTarget = {
    kind: "enhance", action: session.action, handle: session.context?.author_handle, own: Boolean(session.context?.is_own_post),
  };
  // Card shows ONLY a status; the draft itself types into X's composer box.
  // A launcher-created op (already on screen as "Opening your reply…") is
  // adopted so the whole flow lives in one card instead of stacking two.
  const adopted = opBindingFor(options.existingOpId);
  let op: GenerationOp;
  if (adopted && !isTerminal(adopted.op)) {
    adopted.defaultLabel = defaultLabel;
    adopted.resume = resume;
    op = applyOpEvent(adopted.op.id, { type: "target-opened" }) ?? adopted.op;
    applyOpEvent(op.id, { type: "heartbeat", note: `X's ${actionLabel(session.action)} box is open` });
    applyOpEvent(op.id, { type: "heartbeat", note: defaultLabel });
  } else {
    collapseFinishedCards();
    op = beginOp("enhance", "generating", addCard("draft"), defaultLabel,
      { target: opTargetLabel(session.action, session.context), resume });
  }
  const opId = op.id;
  session.activeOpId = opId;
  // Ownership of Cancel moves to the composer session the moment it takes over
  // from the launcher. Both routes — this card's Cancel and the pill's — call
  // exactly the same function, so they settle the same op and can never end up
  // showing different states for one piece of work.
  setOpControls(opId, {
    onCancel: () => { cancelComposerWork(session); },
    target: opTargetLabel(session.action, session.context),
  });
  // The stream reports its real stages; these only cover a silence. "Nothing is used" is said only
  // while it is true: before the first word of a draft has arrived.
  armOpHeartbeats(opId, [
    [30_000, "Still writing. Busy moments can take up to a minute."],
    [75_000, () => opBindings.get(opId)?.preview
      ? "Taking longer than usual…"
      : "Taking longer than usual. Keep waiting, or Cancel. Nothing is used until a draft arrives."],
  ]);

  const retry = () => {
    if (session.disposed || !session.composer.isConnected) {
      showDockNotice("X closed that box. Open it again and press ✦. No draft was used.", false);
      return;
    }
    void enhanceComposer(session, { another: options.another, secondReplyOk: options.secondReplyOk });
  };

  const finish = (terminalState: "updated" | "error", response?: AssistResponse, note?: string, changed = true,
                  failure?: RequestFailure) => {
    if (session.requestId !== requestId) return;
    const binding = opBindings.get(opId);
    if (binding) {
      binding.summary = terminalState === "updated" ? (changed ? "in X" : "left as is") : "not drafted";
      binding.renderTerminal = response
        ? card => showEnhancementResult(session, card, response, terminalState === "updated", note, changed)
        : failure
          ? card => renderRequestFailure(card, failure, retry)
          : undefined;
    }
    applyOpEvent(opId, terminalState === "updated"
      ? { type: "completed", hasContent: true }
      // Every error caller above resolves a reason before calling `finish`
      // (`assistFailureReason`, or the stream's own transport line), so this
      // fallback is unreachable by design. It stays as a net, and says the one
      // true thing rather than inventing a cause.
      : { type: "failed", error: note?.trim() || "Starling stopped without reporting a reason. Try again." });
    clearActiveComposerWork(session);
    // "✓ Done" only when Starling actually typed something; otherwise back to idle at once.
    const pillState: ComposerControlState = terminalState === "updated" ? (changed ? "updated" : "idle") : "error";
    session.control.dataset.state = pillState;
    updateComposerControl(session);
    setDockContext(session.action, session.context, terminalState === "updated" ? "updated" : "error");
    window.setTimeout(() => {
      if (session.requestId !== requestId || session.control.dataset.state !== pillState) return;
      session.control.dataset.state = "idle";
      updateComposerControl(session);
    }, pillState === "error" ? 6_000 : 1_600);
  };

  // Read any attached images so the vision model can keep the text consistent
  // with the picture. Best-effort and bounded; no image = unchanged behaviour.
  // They are context, not the request: whatever does not fit the gateway's body
  // limit is left out (and the card says so) rather than failing the request.
  const composerImages = await readComposerImages(session.composer);
  // X can rebuild the box while its images are read. `disposeComposerSession`
  // then hands this op to the rebuilt box; starting the request here as well
  // sent a second request whose answer nobody was listening for, and it was
  // still charged.
  if (session.requestId !== requestId || session.disposed) return "started";
  const request = fitImagesToRequest(composerImages, images => ({
    mode: "enhance_composer",
    composer: {
      action: session.action, draft_text: requestText, intent: mode,
      ...(images.length ? { images } : {}),
    },
    context: session.context,
    ...(options.secondReplyOk ? { second_reply_ok: true } : {}),
  }));
  if (request.images.length) {
    applyOpEvent(opId, {
      type: "heartbeat",
      note: `Looking at the ${request.images.length === 1 ? "image" : `${request.images.length} images`} on this post…`,
    });
  }
  applyOpEvent(opId, { type: "heartbeat", note: droppedImagesNote(request.dropped, composerImages.length) ?? "Sending to Starling…" });
  const stream = startAssist(request.body, {
    onStopped: outcome => showStopCost(opBindings.get(opId)?.card, outcome),
    onUpdate: streamState => {
      if (session.requestId !== requestId) return;
      if (streamState.phase === "starting" || streamState.phase === "waiting") {
        applyOpEvent(opId, { type: "heartbeat", note: streamStageLabel(streamState.stage) });
      } else if (streamState.phase === "streaming") {
        // The words appear in the card as they are written, so the wait shows
        // progress. They are a PREVIEW: only the final, checked draft is typed
        // into X's box, and the card's result replaces this text, including
        // when the checks withhold the draft.
        setOpPreview(opId, streamState.preview);
        setDockContext(session.action, session.context, "working");
      } else if (streamState.phase === "error") {
        // A transport failure (sign-in, limit, connection…) carries its code
        // and gets the matching card and next step. A withheld draft arrives
        // as a final response instead and is explained by assistFailureReason.
        const failure = isErrorCode(streamState.code) ? requestFailureFromStream(streamState) : undefined;
        finish("error", undefined, streamState.error?.trim()
          || "The connection to Starling ended before the draft arrived. Nothing in X changed.", true, failure);
      } else if (streamState.phase === "done" && streamState.response) {
        void completeComposerStream(session, requestId, mode, original, requestText, streamState.response, opId, finish);
      }
    }
  });
  if (session.requestId === requestId && session.control.dataset.state === "sending") {
    session.cancelActive = stream.cancel;
  }
  return "started";
}

/** A guard stopped enhanceComposer before it could adopt the launcher's op:
 *  settle that op as failed (its card shows the reason + stays actionable)
 *  instead of leaving an orphaned "Opening…" card, or — when there is no op
 *  to adopt — fall back to the plain dock notice. */
function failAdoptedOp(existingOpId: string | undefined, message: string) {
  const binding = opBindingFor(existingOpId);
  if (binding && !isTerminal(binding.op)) {
    applyOpEvent(binding.op.id, { type: "failed", error: message });
    return;
  }
  showDockNotice(message);
}

async function completeComposerStream(
  session: ComposerSession,
  requestId: number,
  mode: ComposerMode,
  original: string,
  /** The customer's own text the draft was written from (differs from `original` for "Try another"). */
  undoOriginal: string,
  response: AssistResponse,
  opId: string,
  finish: (terminal: "updated" | "error", response?: AssistResponse, note?: string, changed?: boolean) => void
) {
  if (session.requestId !== requestId) return;
  if (response.ok && response.decision?.already_replied) {
    // The engine found a persisted reply that may be off-screen in X. Keep
    // this blank composer untouched and make the reason visible in the dock.
    finish("updated", response, undefined, false);
    return;
  }
  if (!response.ok || !response.decision?.draft || response.decision.native_intent !== session.action) {
    finish("error", undefined, assistFailureReason(response, session.action));
    return;
  }
  applyOpEvent(opId, { type: "typing-started" });
  applyOpEvent(opId, { type: "heartbeat", note: `Typing it into X's ${actionLabel(session.action)} box…` });
  const gatedDraft = response.decision.draft;
  // The TYPEWRITER: the gated final text is typed into X's composer at a
  // visible pace (the card is NOT a mirror — the draft only lands in X's box).
  // Safety posture unchanged — gates already ran on the full text, user typing
  // aborts and restores, nothing ever auto-publishes.
  const replacement = await typewriterInsert(session, original, gatedDraft, requestId);
  if (!replacement.ok) {
    finish("error", response, replacement.error, false);
    return;
  }
  if (replacement.changed) {
    // Undo puts back the customer's own words (for Create: an empty box), never an earlier draft.
    session.undo = { original: undoOriginal, inserted: gatedDraft };
  }
  if (response.generation_id && replacement.changed) {
    session.generationId = response.generation_id;
    session.candidateId = response.candidate_id;
    session.lastRecordedText = gatedDraft;
    recordComposerOutcome(session, {
      generation_id: response.generation_id,
      candidate_id: response.candidate_id,
      status: "inserted",
      final_text: gatedDraft,
      metadata: { source: mode === "create" ? "native_composer_create" : "native_composer_enhance", action: session.action },
    });
  }
  finish("updated", response, undefined, replacement.changed);
}

// ---------------------------------------------------------------------------
// Typewriter insertion: animated, abortable, always ending in a verified state.
// ---------------------------------------------------------------------------
let typewriterFastForward = false;

function typewriterDelayMs(): number {
  // Snappy by default: with the capped tick count below, "normal" lands a
  // draft in well under a second.
  return typingDelayMs(dockSettings.typingSpeed);
}

async function typewriterInsert(session: ComposerSession, expectedOriginal: string, enhanced: string, requestId: number,
                                onProgress?: (prefix: string) => void): Promise<{ ok: boolean; changed?: boolean; error?: string; warning?: string }> {
  const composer = session.composer;
  if (!canReplaceComposerDraft({
    expectedOriginal,
    currentText: readableComposerText(composer),
    sessionRequestId: session.requestId,
    requestId,
    connected: composer.isConnected,
    visible: isVisible(composer),
  })) {
    return { ok: false, error: "You edited the text, so Starling left it alone." };
  }
  if (enhanced.trim() === expectedOriginal.trim()) return { ok: true, changed: false };
  const inputRevision = session.inputRevision;
  session.activeOriginal = expectedOriginal;
  session.activeInputRevision = inputRevision;
  typewriterFastForward = typewriterDelayMs() === 0;
  session.control.dataset.state = "typing";
  updateComposerControl(session);
  try {
    // Chunked animated writes; each tick rewrites the full prefix through the
    // same guarded path, so a failure at any point can restore the original.
    // onProgress mirrors the typed prefix into the Starling card so the text
    // appears in BOTH places at the same time.
    // Cap the animation at ~22 ticks (was ~80): each tick rewrites the whole
    // prefix and forces a reflow on X's contenteditable, so fewer, larger chunks
    // are both faster and far less janky, while still reading as "typing".
    const step = Math.max(6, Math.ceil(enhanced.length / 22));
    let cursor = typewriterFastForward ? enhanced.length : Math.min(step, enhanced.length);
    for (;;) {
      if (session.requestId !== requestId || session.disposed || !composer.isConnected) {
        return { ok: false, error: "X changed this box, so Starling stopped and left it alone." };
      }
      if (session.inputRevision !== inputRevision) {
        // The user typed during the animation: stop IMMEDIATELY and leave the
        // composer exactly as they made it — never overwrite fresh typing.
        return { ok: false, error: "You edited the text, so Starling left it alone." };
      }
      const intended = enhanced.slice(0, cursor);
      writeComposerText(session, intended);
      // READ BACK ONCE X'S EDITOR HAS APPLIED THE WRITE, not in the same task.
      // Measured on x.com (30 Oct 2026): the very first insertion into a box
      // nobody has typed in yet reads back correctly at once and then doubles
      // ("textText") a moment later, when X's editor (Draft.js) applies its own
      // copy. A second select-all-and-insert replaces it cleanly. The animated
      // typewriter used to hide this (its next tick replaced the doubled
      // prefix); a one-tick write (Instant speed, a very short draft) kept it,
      // failed its check and said X had not kept the text.
      await settleEditor();
      if (!sameComposerText(readableComposerText(composer), intended)) {
        if (session.requestId !== requestId || session.disposed || !composer.isConnected) {
          return { ok: false, error: "X changed this box, so Starling stopped and left it alone." };
        }
        if (session.inputRevision !== inputRevision) {
          return { ok: false, error: "You edited the text, so Starling left it alone." };
        }
        writeComposerText(session, intended);
        await settleEditor();
      }
      // Read back: if a tick did not land exactly, every later tick would build
      // on corrupted text. Stop animating and write the final draft once,
      // atomically, rather than letting the damage compound.
      if (!sameComposerText(readableComposerText(composer), intended)) {
        writeComposerTextAtomic(session, enhanced);
        onProgress?.(enhanced);
        break;
      }
      onProgress?.(intended);
      if (cursor >= enhanced.length) break;
      cursor = typewriterFastForward ? enhanced.length : Math.min(cursor + step, enhanced.length);
      if (!typewriterFastForward) await sleep(typewriterDelayMs());
    }
    await nextFrame();
  } catch {
    if (!expectedOriginal) {
      return { ok: false, error: "X didn't accept the typing, so the box is as you left it." };
    }
    return restoreOriginalComposerText(session, expectedOriginal, requestId, inputRevision);
  } finally {
    typewriterFastForward = false;
  }
  if (liveComposerHasText(session, requestId, enhanced) && session.inputRevision === inputRevision && isComposerReady(composer)) {
    return { ok: true, changed: true };
  }
  if (session.inputRevision !== inputRevision) {
    return { ok: false, error: "You edited the text, so Starling left it alone." };
  }
  // "X doesn't accept": the text may be fully typed while X's submit state
  // lags behind. Settle, nudge one input event, then re-verify before failing.
  await sleep(150);
  await nextFrame();
  try {
    composer.dispatchEvent(new InputEvent("input", { bubbles: true }));
  } catch { /* synthetic nudge only */ }
  await nextFrame();
  if (liveComposerHasText(session, requestId, enhanced) && session.inputRevision === inputRevision) {
    if (isComposerReady(composer)) return { ok: true, changed: true };
    // The text IS in the composer — never wipe it just because X's Post
    // button hasn't woken up yet.
    return { ok: true, changed: true, warning: "Typed in. Click into the box if X hasn't turned Post on yet." };
  }
  if (!expectedOriginal) {
    return { ok: false, error: "X didn't keep the typed text, so the box is as you left it." };
  }
  return restoreOriginalComposerText(session, expectedOriginal, requestId, inputRevision);
}

function sleep(milliseconds: number): Promise<void> {
  return new Promise(resolve => window.setTimeout(resolve, milliseconds));
}

/** One task later: X's editor applies an inserted text after the insert itself returns. */
function settleEditor(): Promise<void> {
  return sleep(0);
}

function scheduleComposerEditCapture(session: ComposerSession) {
  if (session.disposed || !canRequest(bridgeState, pageGate()) || !session.generationId
    || session.control.dataset.state === "sending" || session.control.dataset.state === "typing") return;
  const text = readableComposerText(session.composer);
  if (!text || text === session.lastRecordedText) return;
  if (session.editTimer != null) clearTimeout(session.editTimer);
  session.editTimer = window.setTimeout(() => {
    flushComposerEditCapture(session);
  }, 550);
}

function flushComposerEditCapture(session: ComposerSession) {
  if (session.editTimer != null) {
    clearTimeout(session.editTimer);
    session.editTimer = undefined;
  }
  if (session.disposed || !canRequest(bridgeState, pageGate()) || !session.generationId
    || session.control.dataset.state === "sending" || session.control.dataset.state === "typing") return;
  const finalText = readableComposerText(session.composer);
  if (!finalText || finalText === session.lastRecordedText) return;
  const generationId = session.generationId;
  session.lastRecordedText = finalText;
  recordComposerOutcome(session, {
    generation_id: generationId,
    candidate_id: session.candidateId,
    status: "edited",
    final_text: finalText,
    metadata: { source: "native_composer_edit", action: session.action },
  });
}

function watchForPublishedComposer(session: ComposerSession) {
  if (session.disposed || !session.generationId || !canRequest(bridgeState, pageGate())) return;
  const text = readableComposerText(session.composer) || session.lastRecordedText || "";
  if (!text.trim()) return;
  const visiblePostIdsBeforeSubmit = new Set(
    Array.from(document.querySelectorAll<HTMLElement>(POST_SELECTOR))
      .map(article => postIdFromUrl(findPermalink(article)?.href))
      .filter((postId): postId is string => Boolean(postId)),
  );
  const proof: PendingPublicationProof = {
    generationId: session.generationId,
    candidateId: session.candidateId,
    action: session.action,
    text,
    context: session.context,
    visiblePostIdsBeforeSubmit,
    expiresAt: Date.now() + 2 * 60_000,
  };
  pendingPublicationProofs.set(session.generationId, proof);
  persistPendingPublicationProof(proof);
  schedulePassiveCapture();
}

function flushPublishedComposerEdit(event: Event) {
  if (event instanceof KeyboardEvent) {
    if (event.key === "Escape") {
      pendingQuoteIntent = null;
      return;
    }
    if (event.key !== "Enter" || (!event.metaKey && !event.ctrlKey)) return;
    const target = event.target instanceof Element ? event.target.closest<HTMLElement>(COMPOSER_SELECTOR) : null;
    const composer = target ? resolveEditableComposer(target) ?? target : null;
    const session = composer ? composerSessions.get(composer) : undefined;
    if (session) {
      flushComposerEditCapture(session);
      watchForPublishedComposer(session);
    }
    return;
  }
  const control = event.target instanceof Element ? event.target.closest<HTMLElement>("button") : null;
  if (!control || !/^tweetButton(?:Inline)?/.test(control.getAttribute("data-testid") || "") || !isNativeControlEnabled(control)) return;
  for (const session of activeComposerSessions) {
    const scope = nativeComposerScope(session.composer);
    if (scope?.contains(control)) {
      flushComposerEditCapture(session);
      watchForPublishedComposer(session);
    }
  }
}

function flushDetachedComposerEdits() {
  for (const session of activeComposerSessions) {
    if (session.composer.isConnected) continue;
    flushComposerEditCapture(session);
    disposeComposerSession(session);
  }
}

function flushAllComposerEdits() {
  for (const session of activeComposerSessions) flushComposerEditCapture(session);
}

function recordComposerOutcome(session: ComposerSession, payload: Record<string, unknown>) {
  if (session.disposed || !canRequest(bridgeState, pageGate())) return;
  void sendToWorker({ kind: MESSAGE_KINDS.recordOutcome, payload }).catch(() => undefined);
}

function disposeComposerSession(session: ComposerSession) {
  if (session.disposed) return;
  session.disposed = true;
  if (session.editTimer != null) {
    clearTimeout(session.editTimer);
    session.editTimer = undefined;
  }
  if (session.placementFrame != null) {
    cancelAnimationFrame(session.placementFrame);
    session.placementFrame = undefined;
  }
  // A live generation whose composer just got rebuilt can't deliver into the
  // old DOM node. X routinely swaps the freshly-expanded reply composer right
  // after Starling binds it — failing here made the user's FIRST click look dead
  // and forced a second one. Instead: one automatic re-bind. The stream is
  // cancelled (its callbacks hold the dead session), the card stays in its
  // loading state, and as soon as X's rebuilt composer produces a session the
  // op continues there with a fresh request. Only if no composer returns —
  // or a second rebuild strikes — does the op fail visibly.
  const binding = opBindingFor(session.activeOpId);
  if (binding && !isTerminal(binding.op)) {
    session.requestId += 1;
    session.cancelActive?.();
    if (binding.op.kind === "enhance" && !binding.rebound) {
      binding.rebound = true;
      const opId = binding.op.id;
      const action = session.action;
      applyOpEvent(opId, { type: "heartbeat", note: `X refreshed the box — picking your ${actionLabel(action)} back up…` });
      // Only the rebuilt box for the SAME post. Any open box with the same
      // action used to do: a reply box bound with no post took over, and its
      // request could only be refused.
      const sameTarget: ComposerSessionTarget | undefined = session.context || session.sourceArticle
        ? { context: session.context, sourceArticle: session.sourceArticle }
        : undefined;
      void waitForComposerSession(action, 5_000, sameTarget).then(next => {
        const live = opBindings.get(opId);
        if (!live || isTerminal(live.op)) return;
        if (next && !next.disposed) {
          void enhanceComposer(next, { existingOpId: opId });
        } else {
          applyOpEvent(opId, { type: "failed", error: "X rebuilt that composer while Starling was working. Press ✦ again." });
        }
      });
    } else {
      applyOpEvent(binding.op.id, { type: "failed", error: "X rebuilt that composer while Starling was working. Press ✦ again." });
    }
  }
  clearActiveComposerWork(session);
  session.abortController.abort();
  session.host.remove();
  activeComposerSessions.delete(session);
  composerSessions.delete(session.composer);
}

function liveComposerHasText(session: ComposerSession, requestId: number, text: string): boolean {
  const composer = session.composer;
  return session.requestId === requestId
    && !session.disposed
    && !pageDisabled
    && session.control.isConnected
    && composer.isConnected
    && isVisible(composer)
    && sameComposerText(readableComposerText(composer), text);
}

function writeComposerTextAtomic(session: ComposerSession, value: string) {
  // One DOM replacement, no selection APIs and no execCommand — nothing here
  // can append instead of replace. Used when an animated tick failed to land.
  const composer = session.composer;
  const token = beginComposerWrite(session);
  try {
    composer.replaceChildren(document.createTextNode(value));
    composer.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: value }));
  } finally {
    endComposerWrite(session, token);
  }
}

/** Starling is writing: X's echo of that write must not count as the customer typing. */
function beginComposerWrite(session: ComposerSession): number {
  session.writing = true;
  session.writeToken = (session.writeToken ?? 0) + 1;
  return session.writeToken;
}

/**
 * X's own React reconciliation re-renders the contenteditable AFTER a write
 * returns, firing its own "input" event on a later frame, so the flag is held
 * through that window. Only the LATEST write lowers it: a write repeated a task
 * later (see the typewriter) must not have its window cut short by the first.
 */
function endComposerWrite(session: ComposerSession, token: number) {
  void nextFrame().then(() => {
    if (session.writeToken === token) session.writing = false;
  });
}


function writeComposerText(session: ComposerSession, value: string) {
  const composer = session.composer;
  const token = beginComposerWrite(session);
  try {
    composer.focus();
    const selection = window.getSelection();
    const range = document.createRange();
    // Select the composer's CONTENTS, uncollapsed, so insertText REPLACES them.
    // This used to collapse(false) — a caret at the end selecting nothing — and
    // lean on execCommand("selectAll") to re-expand it. selectAll is document
    // scoped and silently no-ops inside X's React contenteditable; when it did,
    // insertText appended at the caret instead of replacing. The typewriter
    // runs ~80 ticks, so one no-op cascaded into a draft carrying its own tail
    // repeated at every intermediate length.
    range.selectNodeContents(composer);
    selection?.removeAllRanges();
    selection?.addRange(range);
    const replaced = Boolean(selection) && !selection!.isCollapsed;
    // An empty value is a deletion: "insertText" with nothing to insert is not reliably one.
    const inserted = replaced && (value ? document.execCommand("insertText", false, value) : document.execCommand("delete"));
    if (!inserted) {
      composer.replaceChildren(document.createTextNode(value));
      composer.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: value }));
    }
  } finally {
    // X's own React reconciliation re-renders the contenteditable AFTER this
    // synchronous write returns, firing its own "input" event on a later frame.
    // Resetting `writing` immediately let that self-inflicted event look
    // exactly like the user typing — the typewriter's inputRevision check then
    // aborted mid-draft ("you typed while Starling was typing") even though
    // nothing was typed. Hold the flag through X's reconciliation window so
    // only a REAL user keystroke (which arrives well after this) can trip it.
    endComposerWrite(session, token);
  }
}

async function restoreOriginalComposerText(
  session: ComposerSession,
  expectedOriginal: string,
  requestId: number,
  inputRevision: number
): Promise<{ ok: false; error: string }> {
  if (session.inputRevision !== inputRevision || !liveComposerHasText(session, requestId, readableComposerText(session.composer))) {
    return { ok: false, error: "X changed this box, so Starling stopped and left it alone." };
  }
  try {
    writeComposerText(session, expectedOriginal);
    await nextFrame();
  } catch {
    return { ok: false, error: "Starling couldn't confirm what is in X's box, so it stopped. Check the box before you post." };
  }
  if (session.inputRevision === inputRevision && liveComposerHasText(session, requestId, expectedOriginal)) {
    return { ok: false, error: "X did not accept the replacement. Your original draft was restored." };
  }
  return { ok: false, error: "Starling couldn't confirm X put your text back. Check the box before you post." };
}

function nextFrame(): Promise<void> {
  return new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
}

/**
 * A small page-level notice for when the dock itself cannot be shown, optionally with one action
 * ("Undo" after turning Starling off on a page). Drawn in its own closed shadow root, in X's
 * colours, so neither X's styles nor X's keyboard handlers can reach it.
 */
function showPageNotice(message: string, action?: { label: string; run: () => void }, durationMs = 4_200) {
  document.getElementById("starling-page-notice")?.remove();
  const host = document.createElement("div");
  host.id = "starling-page-notice";
  host.style.cssText = "position:fixed;z-index:2147483647;right:18px;top:64px;max-width:340px;";
  host.dataset.theme = isDarkSurface(document.body) ? "dark" : "light";
  const shadow = host.attachShadow({ mode: "closed" });
  sealShadowKeyboard(shadow, { editableOnly: true });
  const style = document.createElement("style");
  style.textContent = pageNoticeStyles;
  const box = document.createElement("div");
  box.className = "notice";
  box.setAttribute("role", "status");
  const text = document.createElement("span");
  text.textContent = message;
  box.append(text);
  if (action) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = action.label;
    button.addEventListener("click", () => {
      host.remove();
      action.run();
    });
    box.append(button);
  }
  shadow.append(style, box);
  (document.body ?? document.documentElement).append(host);
  window.setTimeout(() => host.remove(), durationMs);
}

const pageNoticeStyles = `
  :host { font-family:"TwitterChirp","Chirp",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
  .notice { display:flex; align-items:center; gap:14px; padding:11px 14px; border-radius:12px; font-size:13px; font-weight:600; line-height:1.4;
    background:#ffffff; color:#0f1419; border:1px solid #cfd9de; box-shadow:0 14px 38px rgba(15,20,25,.16); }
  :host([data-theme="dark"]) .notice { background:#16181c; color:#e7e9ea; border-color:#2f3336; box-shadow:0 14px 38px rgba(0,0,0,.5); }
  button { flex:none; margin-left:auto; padding:5px 12px; border-radius:999px; border:1px solid currentColor; background:transparent; color:inherit; font:inherit; font-weight:700; cursor:pointer; }
  button:hover { background:rgba(15,20,25,.08); }
  :host([data-theme="dark"]) button:hover { background:rgba(231,233,234,.1); }
  button:focus-visible { outline:2px solid currentColor; outline-offset:2px; }
`;

// ---------------------------------------------------------------------------
// The Starling dock: a right-rail card chat that can be long-pressed into a
// draggable, resizable panel. It stays gone when it is not needed.
// ---------------------------------------------------------------------------
interface DockSettings {
  typingSpeed: TypingSpeed;
  /** "auto" means X decides — see shared/theme.ts for why that is the default. */
  theme: ThemePreference;
}
let dockSettings: DockSettings = { typingSpeed: "normal", theme: "auto" };
const SETTINGS_KEY = "starling:settings";
// v3 deliberately starts in the sidebar. Old free-floating geometry is not a
// placement preference, so it must not override the new right-rail home.
const DOCK_RECT_KEY = "starling:dock-rect-v3";
const DOCK_MIN_WIDTH = 320;
const DOCK_MIN_HEIGHT = 400;
const DOCK_VIEWPORT_INSET = 8;
const DOCK_DETACH_MOVE_TOLERANCE = 8;
const SIDEBAR_DOCK_TARGET_ID = "starling-sidebar-dock-target";
type DockPlacement = "sidebar" | "floating";
let dockPlacement: DockPlacement = "sidebar";
let savedDockRect: Record<string, number> | null = null;
let dockRectLoaded = false;
let sidebarDockTarget: HTMLElement | null = null;
let dockCloseAnimation: Animation | null = null;
let dockCloseSequence = 0;
let activeChatStream: { cancel: () => void } | null = null;
// One conversation per page session so Starling remembers previous chat turns.
let chatConversationId: string | undefined;

// ---- attachments ----------------------------------------------------------
// Things the user hands Starling to work from: local files attached with the "+"
// button, and X posts attached with the per-post paperclip. Both show as cards
// in the composer and are woven into the next chat message so Starling can read
// them and draft a post. Nothing is sent until the user presses Send.
interface FileAttachment {
  id: number;
  kind: "file";
  name: string;
  size: number;
  /** Line count, for the "N lines" sub-line on the card. */
  lines: number;
  /** Short uppercase format badge (MD, TXT, CSV…). */
  badge: string;
  /** Extracted text; empty when `error` is set. */
  text: string;
  /** Set when the file could not be read as text (binary, too big, unreadable). */
  error?: string;
}
interface PostAttachment {
  id: number;
  kind: "post";
  author: string;
  handle?: string;
  text: string;
  imageUrl?: string;
  avatarUrl?: string;
  url?: string;
  postId?: string;
  /** Full structured context handed to the engine as `context`. */
  context: PostContext;
}
type ChatAttachment = FileAttachment | PostAttachment;
let chatAttachments: ChatAttachment[] = [];
let chatAttachmentSeq = 0;
const MAX_ATTACHMENTS = 6;
// Per-file read ceiling — anything larger is not something to send whole, and
// reading it would just stall the page. What is sent is further cut to the
// service's chat limit (see fitChatMessage).
const MAX_ATTACHMENT_BYTES = 2 * 1024 * 1024;

async function loadDockSettings() {
  try {
    const stored = await pagePrefs.get([SETTINGS_KEY]);
    const value = stored[SETTINGS_KEY];
    if (isRecord(value)) {
      if (isTypingSpeed(value.typingSpeed)) dockSettings.typingSpeed = value.typingSpeed;
      if (isThemePreference(value.theme)) dockSettings.theme = value.theme;
    }
  } catch { /* defaults stand */ }
}

function saveDockSettings() {
  void pagePrefs.set({ [SETTINGS_KEY]: { ...dockSettings } }).catch(() => undefined);
}

/**
 * Paint the dock in the resolved theme.
 *
 * Re-read on every open, not just at mount: X changes theme instantly with no
 * reload, so a dock that mounted before the user switched would wear the old
 * skin for the rest of the session.
 */
function applyDockTheme() {
  if (!chatHost) return;
  const resolved = resolveTheme(dockSettings.theme, surfaceTone(document.body));
  const key = `${resolved.mode}/${resolved.tone}`;
  // Empty on the first paint: a dock that opens already-themed has nothing to
  // cross-fade FROM, and fading it in would just look like a slow open.
  const changed = appliedDockTheme !== "" && appliedDockTheme !== key;
  appliedDockTheme = key;
  chatHost.dataset.theme = resolved.mode;
  chatHost.dataset.tone = resolved.tone;
  if (changed) crossFadeTheme();
}

let appliedDockTheme = "";
let themeFadeTimer: number | null = null;

/**
 * Ease the panel between themes instead of cutting.
 *
 * Swapping the tokens on the host is instant, so flipping the switch snapped the
 * whole panel from black to white while the thumb was still travelling — the
 * switch animated and the thing it controls did not, which reads as broken
 * rather than as fast. A class carries the transition for exactly as long as the
 * swap takes and is then removed, so no hover, card or stream inherits a
 * transition it never asked for.
 */
function crossFadeTheme() {
  const panel = chatShadow?.querySelector<HTMLElement>(".panel");
  if (!panel || matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  panel.classList.add("theming");
  if (themeFadeTimer != null) window.clearTimeout(themeFadeTimer);
  themeFadeTimer = window.setTimeout(() => {
    panel.classList.remove("theming");
    themeFadeTimer = null;
  }, 420);
}

// ---- settings view --------------------------------------------------------
// Account & usage and the data choices come from the server session. Typing
// speed and appearance are browser-side and work even before consent.
let settingsOpen = false;

function settingsRoot(): HTMLElement | null {
  return chatShadow?.querySelector<HTMLElement>(".settings") ?? null;
}

// ---- tooltips -------------------------------------------------------------
// One node, reused. It lives inside the panel (which is position:relative) so
// it is clipped and themed with everything else, unlike the native title=
// bubble the browser draws in the desktop's own font.
let tipNode: HTMLElement | null = null;
let tipTimer: number | null = null;

function hideTip() {
  if (tipTimer != null) window.clearTimeout(tipTimer);
  tipTimer = null;
  tipNode?.classList.remove("show");
}

function showTip(target: HTMLElement, text: string) {
  const panel = chatShadow?.querySelector<HTMLElement>(".panel");
  if (!panel || !target.isConnected) return;
  if (!tipNode || !tipNode.isConnected) {
    tipNode = el("div", "tip");
    tipNode.setAttribute("role", "tooltip");
    panel.append(tipNode);
  }
  tipNode.textContent = text;
  tipNode.classList.remove("show");

  const panelBox = panel.getBoundingClientRect();
  const box = target.getBoundingClientRect();
  const tipBox = tipNode.getBoundingClientRect();
  // Below when the control sits near the top (the header), above otherwise —
  // a tooltip that leaves the card is worse than no tooltip.
  const below = box.top - panelBox.top < panelBox.height / 2;
  const top = below ? box.bottom - panelBox.top + 8 : box.top - panelBox.top - tipBox.height - 8;
  const centred = box.left + box.width / 2 - panelBox.left - tipBox.width / 2;
  const left = Math.min(Math.max(8, centred), Math.max(8, panelBox.width - tipBox.width - 8));
  tipNode.dataset.place = below ? "below" : "above";
  tipNode.style.top = `${Math.round(top)}px`;
  tipNode.style.left = `${Math.round(left)}px`;
  // Keep the arrow pointing at the control even when the bubble was clamped.
  const arrow = box.left + box.width / 2 - panelBox.left - left - 3.5;
  tipNode.style.setProperty("--tip-arrow", `${Math.round(Math.min(Math.max(9, arrow), tipBox.width - 16))}px`);
  tipNode.classList.add("show");
}

/** Attach a tooltip. Hover waits; keyboard focus shows it at once. A function is read each time
 *  the tip shows, for text that can change (the keyboard shortcut Chrome has bound). */
function tip(target: HTMLElement | null | undefined, text: string | (() => string)) {
  if (!target) return;
  const read = () => (typeof text === "function" ? text() : text);
  target.removeAttribute("title");
  target.addEventListener("pointerenter", () => {
    if (tipTimer != null) window.clearTimeout(tipTimer);
    tipTimer = window.setTimeout(() => showTip(target, read()), 380);
  });
  target.addEventListener("pointerleave", hideTip);
  // A tooltip left hanging over the thing you just clicked reads as stuck.
  target.addEventListener("pointerdown", hideTip);
  target.addEventListener("focus", () => showTip(target, read()));
  target.addEventListener("blur", hideTip);
}

function toggleSettings() {
  if (settingsOpen) closeSettings();
  else openSettings();
}

function openSettings() {
  const panel = chatShadow?.querySelector<HTMLElement>(".panel");
  if (!panel) return;
  settingsOpen = true;
  panel.dataset.view = "settings";
  chatShadow?.querySelector<HTMLElement>(".gear")?.setAttribute("aria-expanded", "true");
  renderSettings();
  playSettingsEntrance();
  void refreshSession();
}

/** Settings, at "What Starling learns from", with its first field ready to type in. */
function openWritingSamples() {
  openSettings();
  const group = settingsRoot()?.querySelector<HTMLElement>(".material-group");
  if (!group) return;
  group.scrollIntoView({ block: "start" });
  group.querySelector<HTMLElement>("textarea, input")?.focus({ preventScroll: true });
}

function closeSettings() {
  const panel = chatShadow?.querySelector<HTMLElement>(".panel");
  if (!panel) return;
  settingsOpen = false;
  closeOpenDropdown?.();
  stopSpeedDemo();
  hideTip();
  delete panel.dataset.view;
  const gear = chatShadow?.querySelector<HTMLButtonElement>(".gear");
  gear?.setAttribute("aria-expanded", "false");
  gear?.focus();
}

function el<K extends keyof HTMLElementTagNameMap>(tag: K, className?: string, text?: string) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function settingsField(label: string, control: HTMLElement, help?: string) {
  const field = el("div", "set-field");
  field.append(el("label", "set-label", label), control);
  if (help) field.append(el("p", "set-help", help));
  return field;
}

function icon(className: string, path: string, filled = false) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("fill", filled ? "currentColor" : "none");
  if (!filled) {
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "2.6");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("stroke-linejoin", "round");
  }
  const node = document.createElementNS("http://www.w3.org/2000/svg", "path");
  node.setAttribute("d", path);
  svg.append(node);
  svg.setAttribute("class", className);
  return svg;
}

const CARET_PATH = "M5 9l7 7 7-7";
const TICK_PATH = "M4 12.5l5 5 11-11";

/**
 * A sliding on/off switch: one control with one thumb, instead of two pills
 * where only one is lit. Same monochrome language as `segmented` — the track
 * fills with --fg when on, exactly as the segmented indicator does — because a
 * settings screen that mixes control vocabularies reads as two settings
 * screens. Colour belongs to the health dot and to `themeSwitch`, which has to
 * paint day and night to mean anything.
 *
 * A real <input type=checkbox> carries the role, the toggled state and Space
 * for free; the track and thumb are drawn siblings that follow :checked.
 */
function toggleSwitch(checked: boolean, label: string, onChange: (on: boolean) => void) {
  const wrap = el("span", "sw sw-flag");
  const input = el("input") as HTMLInputElement;
  input.type = "checkbox";
  input.checked = checked;
  input.setAttribute("aria-label", label);
  const track = el("span", "sw-track");
  input.addEventListener("change", () => onChange(input.checked));
  wrap.append(input, track);
  return wrap;
}

/**
 * The day/night switch. The thumb IS the celestial body: an inset shadow offset
 * inside the circle carves a crescent moon, and growing the same shadow's
 * spread until it fills the circle turns it into the sun — so one element
 * becomes both without a sprite or a second node.
 *
 * Adapted from Uiverse.io by JustCode14 (MIT); the palette is X's own — #1d9bf0
 * for the day sky and Dim's #15202b for night, rather than the original's
 * generic blue and grey.
 */
function themeSwitch(light: boolean, onChange: (light: boolean) => void) {
  const wrap = el("span", "sw sw-theme");
  const input = el("input") as HTMLInputElement;
  input.type = "checkbox";
  input.checked = light;
  input.setAttribute("aria-label", "Light mode");
  const track = el("span", "sw-track");
  for (let i = 1; i <= 3; i += 1) track.append(el("span", `sw-star sw-star-${i}`));
  track.append(dayCloud());
  input.addEventListener("change", () => onChange(input.checked));
  wrap.append(input, track);
  return wrap;
}

/** The cloud that fades in over the day sky. */
function dayCloud() {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("class", "sw-cloud");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("transform", "matrix(.77976 0 0 .78395-299.99-418.63)");
  path.setAttribute("fill", "#fff");
  path.setAttribute("d", "m391.84 540.91c-.421-.329-.949-.524-1.523-.524-1.351 0-2.451 1.084-2.485 2.435-1.395.526-2.388 1.88-2.388 3.466 0 1.874 1.385 3.423 3.182 3.667v.034h12.73v-.006c1.775-.104 3.182-1.584 3.182-3.395 0-1.747-1.309-3.186-2.994-3.379.007-.106.011-.214.011-.322 0-2.707-2.271-4.901-5.072-4.901-2.073 0-3.856 1.202-4.643 2.925");
  svg.append(path);
  return svg;
}

/**
 * A switch on the right of its own label, with the explanation underneath.
 *
 * The ROW is the <label>, so the caption is part of the hit target — a 57px
 * switch is a small thing to ask someone to hit when the words next to it are
 * already saying what it does. That is also why the switch itself is a <span>
 * and not a second label: nesting them is invalid, and the browser would have
 * to guess which one owns the input.
 */
function switchField(label: string, control: HTMLElement, help: string | HTMLElement) {
  const field = el("div", "set-field");
  const row = el("label", "set-row");
  row.append(el("span", "set-label", label), control);
  field.append(row, typeof help === "string" ? el("p", "set-help", help) : help);
  return field;
}

// The dropdown that is currently open, so Escape can close it before it closes
// Settings, and opening a second one closes the first.
let closeOpenDropdown: (() => void) | null = null;

function renderSettings() {
  const root = settingsRoot();
  if (!root) return;
  // The speed sample owns a timer, and the old node is about to be discarded.
  stopSpeedDemo();
  closeOpenDropdown?.();
  root.replaceChildren();
  root.append(renderAccountGroup(), renderPrivacyGroup(), renderWritingGroup());
  // What Starling learns from lives on the server, so it needs a signed-in browser.
  if (canRequest(bridgeState, pageGate())) root.append(renderMaterialGroup());
  root.append(renderAppearanceGroup(), renderPageGroup());
}

// ---- your own material ----------------------------------------------------
//
// Everything the voice model knew arrived by one of two accidents: the archive
// imported once at setup, and the drafts he happened to edit. There was no way
// to TELL the account anything, and the engine side of that shipped with no
// way to reach it — which is the same inert-feature failure this codebase
// keeps committing. This is the reach.
//
// Two kinds, and the difference is a safety property rather than a category:
// VOICE joins the example pool the writer sees, BACKGROUND never reaches a
// prompt at all and only steers what gets engaged with. There is deliberately
// no third kind for facts the account may state — a typed "fact" would be an
// unverifiable claim published under his name.
type MaterialKind = "voice" | "background";

interface MaterialItem { id: string; kind: MaterialKind; text: string }

let materialItems: MaterialItem[] | null = null;
let materialBusy = false;

async function ownMaterial(action: "list" | "add" | "remove", body: unknown = {}) {
  try {
    const response = await sendToWorker({
      kind: MESSAGE_KINDS.ownMaterial, payload: { action, body },
    });
    if (isRecord(response) && response.ok === true) return response.data ?? {};
    const error = isRecord(response) ? String(response.error || "") : "";
    return {
      __error: error || "Starling isn't responding right now. Try again.",
      __code: isRecord(response) && typeof response.code === "string" ? response.code : null,
      __retry: isRecord(response) && typeof response.retry_after_s === "number" ? response.retry_after_s : null,
    };
  } catch {
    return { __error: "Starling isn't responding right now. Try again.", __code: "network_error", __retry: null };
  }
}

const MATERIAL_COPY: Record<MaterialKind, { label: string; help: string; placeholder: string }> = {
  voice: {
    label: "Writing of yours",
    help: "Teaches how you write. Starling copies the rhythm, never the content.",
    placeholder: "Paste something you wrote that sounds like you",
  },
  background: {
    label: "Things to keep in mind",
    help: "Steers what Starling engages with. Never quoted, never posted.",
    placeholder: "e.g. I don't care about image models",
  },
};

function renderMaterialGroup() {
  const group = el("section", "set-group material-group");
  group.append(el("h3", "set-title", "What Starling learns from"));

  const list = el("div", "mat-list");
  const status = el("p", "set-help");

  const paint = () => {
    list.replaceChildren();
    if (materialItems === null) {
      status.textContent = "Loading…";
      return;
    }
    status.textContent = materialItems.length
      ? ""
      : "Nothing added yet. Starling also learns from the drafts you edit.";
    for (const item of materialItems) {
      const row = el("div", "mat-row");
      row.dataset.kind = item.kind;
      const body = el("div", "mat-body");
      body.append(el("span", "mat-tag", MATERIAL_COPY[item.kind].label),
                  el("p", "mat-text", item.text));
      const drop = el("button", "mat-drop", "Remove") as HTMLButtonElement;
      drop.type = "button";
      drop.setAttribute("aria-label", `Remove: ${item.text.slice(0, 60)}`);
      drop.addEventListener("click", () => void removeItem(item.id));
      row.append(body, drop);
      list.append(row);
    }
  };

  const refresh = async () => {
    const data = await ownMaterial("list", {});
    if (isRecord(data) && "__error" in data) {
      materialItems = [];
      paint();
      status.textContent = String(data.__error);
      return;
    }
    const items = isRecord(data) && Array.isArray(data.items) ? data.items : [];
    materialItems = items.filter((item): item is MaterialItem =>
      isRecord(item) && typeof item.id === "string" && typeof item.text === "string"
      && (item.kind === "voice" || item.kind === "background")) as MaterialItem[];
    paint();
  };

  const removeItem = async (id: string) => {
    if (materialBusy) return;
    materialBusy = true;
    const data = await ownMaterial("remove", { id });
    materialBusy = false;
    if (isRecord(data) && "__error" in data) {
      status.textContent = String(data.__error);
      return;
    }
    await refresh();
  };

  for (const kind of ["voice", "background"] as MaterialKind[]) {
    const copy = MATERIAL_COPY[kind];
    const box = el("textarea", "mat-input") as HTMLTextAreaElement;
    box.placeholder = copy.placeholder;
    box.rows = 3;
    const add = el("button", "set-link", "Add") as HTMLButtonElement;
    add.type = "button";
    const problem = el("p", "set-help mat-problem");

    const submit = async () => {
      const text = box.value.trim();
      if (!text || materialBusy) return;
      materialBusy = true;
      add.disabled = true;
      const data = await ownMaterial("add", { kind, text });
      materialBusy = false;
      add.disabled = false;
      // THE ENGINE'S OWN SENTENCE, verbatim. It refuses with a reason he can
      // act on ("A voice example needs at least 3 words"), and replacing that
      // with a generic failure is how a settings panel becomes a shrug.
      if (isRecord(data) && "__error" in data) {
        problem.textContent = String(data.__error);
        return;
      }
      problem.textContent = "";
      box.value = "";
      await refresh();
    };

    add.addEventListener("click", () => void submit());
    const row = el("div", "mat-add");
    row.append(box, add);
    group.append(settingsField(copy.label, row, copy.help));
    group.append(problem);
  }

  group.append(renderOwnPostsImport(refresh), list, status);
  paint();
  void refresh();
  return group;
}

/** Every post X has loaded on this page, as the import sees it: its author's handle and its text. */
function visiblePostsForImport(): VisiblePost[] {
  return Array.from(document.querySelectorAll<HTMLElement>(POST_SELECTOR)).map(article => ({
    handle: extractAuthorHandle(article),
    text: findPrimaryTextElement(article)?.innerText || "",
  }));
}

/* "Add my posts from this page": the quickest way to a voice Starling can match. Only on the
   person's own profile, only the posts X has already loaded there (nothing scrolls by itself), and
   only when they press it. Each post goes in as a writing sample, one at a time, and the engine
   refuses one it already has, so pressing twice is harmless. */
function renderOwnPostsImport(refresh: () => Promise<void>) {
  const row = el("div", "mat-import");
  const button = el("button", "set-link", "Add my posts from this page") as HTMLButtonElement;
  button.type = "button";
  const note = el("p", "set-help");
  note.setAttribute("aria-live", "polite");
  const identity = getSignedInIdentity();
  const onOwnProfile = Boolean(identity && isOwnProfilePath(location.pathname, identity.handle));
  button.hidden = !onOwnProfile;
  note.textContent = onOwnProfile
    ? "Adds the posts of yours that X has loaded on this page. Scroll down to load more, then press again. Nothing is posted."
    : "Open your own profile on X and this adds your recent posts in one press.";
  button.addEventListener("click", async () => {
    const me = getSignedInIdentity();
    if (!me || materialBusy) return;
    const texts = ownPostTexts(visiblePostsForImport(), me.handle);
    if (!texts.length) {
      note.textContent = importSummary(0, 0, 0);
      return;
    }
    materialBusy = true;
    button.disabled = true;
    let added = 0;
    let already = 0;
    let refused = 0;
    for (const [index, text] of texts.entries()) {
      note.textContent = `Adding ${index + 1} of ${texts.length}…`;
      let data = await ownMaterial("add", { kind: "voice", text });
      // A new account's engine can still be starting on the first press: wait for it, don't fail.
      for (let attempt = 0; isRecord(data) && "__error" in data; attempt += 1) {
        const wait = importRetryDelayMs(data.__code as string | null, data.__retry as number | null, attempt);
        if (wait === null) break;
        note.textContent = `Starling is getting ready… (${index + 1} of ${texts.length})`;
        await new Promise(resolve => setTimeout(resolve, wait));
        data = await ownMaterial("add", { kind: "voice", text });
      }
      if (!(isRecord(data) && "__error" in data)) added += 1;
      else if (/already added/i.test(String(data.__error))) already += 1;
      else refused += 1;
    }
    materialBusy = false;
    button.disabled = false;
    note.textContent = importSummary(added, already, refused);
    await refresh();
  });
  row.append(button, note);
  return settingsField("Your posts on X", row, "Starling learns how you write from your own posts. It never posts them or reuses their words.");
}

function renderAppearanceGroup() {
  const group = el("section", "set-group appearance-group");
  group.append(el("h3", "set-title", "Appearance"));

  const page = surfaceTone(document.body);
  const help = el("p", "set-help");
  // The way back to X. Hidden rather than absent while the default is live, so
  // showing it later cannot reflow the group out from under a moving thumb.
  const back = el("button", "set-link", "Follow X again") as HTMLButtonElement;
  back.type = "button";

  const control = themeSwitch(resolveTheme(dockSettings.theme, page).mode === "light", light => {
    // Flipping the switch records the SIDE, not a rule: someone who reaches for
    // it has an opinion about right now, and "auto" would be free to disagree
    // with them a minute later.
    dockSettings.theme = light ? "light" : "dark";
    commit();
  });
  const input = control.querySelector("input") as HTMLInputElement;

  /* Update the words around the switch and leave the switch itself alone.
   *
   * Re-rendering the section is what every other control in here does on change,
   * and it is exactly wrong for this one: replacing the node hands the browser a
   * switch that is ALREADY in its end state, so the thumb, the sky, the stars
   * and the cloud all cut instead of travelling. The animation was correct from
   * the first build and had simply never been allowed to run. */
  const reflect = () => {
    input.checked = resolveTheme(dockSettings.theme, page).mode === "light";
    help.textContent = themeHelp(dockSettings.theme, page);
    back.hidden = dockSettings.theme === "auto";
  };

  const commit = () => {
    saveDockSettings();
    applyDockTheme();
    reflect();
  };

  back.addEventListener("click", () => {
    dockSettings.theme = "auto";
    commit();
  });

  group.append(switchField("Light mode", control, help), back);
  reflect();
  return group;
}

/** Play the entrance once, then take the class off so nothing re-animates. */
function playSettingsEntrance() {
  const root = settingsRoot();
  if (!root) return;
  root.classList.add("entering");
  window.setTimeout(() => root.classList.remove("entering"), 460);
}

/** Replace one section in place.
 *
 * Re-rendering the whole view on every change would wipe a half-typed base URL
 * the moment someone touched a Writing chip.
 */
function replaceGroup(selector: string, build: () => HTMLElement) {
  const existing = settingsRoot()?.querySelector<HTMLElement>(selector);
  if (existing) existing.replaceWith(build());
}

// The looping sample under the speed slider.
let speedDemoTimer: number | null = null;
const SPEED_SAMPLE = "This is how fast your draft appears.";

function stopSpeedDemo() {
  if (speedDemoTimer != null) window.clearTimeout(speedDemoTimer);
  speedDemoTimer = null;
}

/** Type the sample at the selected rate, then pause and start over. */
function runSpeedDemo(node: HTMLElement) {
  stopSpeedDemo();
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) {
    node.textContent = SPEED_SAMPLE;
    return;
  }
  let typed = 0;
  const restart = () => {
    speedDemoTimer = window.setTimeout(() => { typed = 0; node.textContent = ""; step(); }, 1500);
  };
  const step = () => {
    const delay = typewriterDelayMs();
    if (delay === 0) {
      // "Instant" has nothing to animate — showing it whole IS the preview.
      node.textContent = SPEED_SAMPLE;
      return restart();
    }
    typed += 1;
    node.textContent = SPEED_SAMPLE.slice(0, typed);
    if (typed >= SPEED_SAMPLE.length) return restart();
    speedDemoTimer = window.setTimeout(step, delay);
  };
  node.textContent = "";
  step();
}

function renderWritingGroup() {
  const group = el("section", "set-group writing-group");
  group.append(el("h3", "set-title", "Writing"));

  const index = Math.max(0, TYPING_SPEEDS.findIndex(speed => speed.id === dockSettings.typingSpeed));
  const block = el("div", "spd");
  const head = el("div", "spd-head");
  const name = el("span", "spd-name", TYPING_SPEEDS[index].label);
  const rate = el("span", "spd-rate");
  head.append(name, rate);

  const rail = el("div", "spd-rail");
  const fill = el("span", "spd-fill");
  const ticks = el("span", "spd-ticks");
  for (let i = 0; i < TYPING_SPEEDS.length; i += 1) ticks.append(el("span", "spd-tick"));
  const input = el("input", "spd-input") as HTMLInputElement;
  input.type = "range";
  input.min = "0";
  input.max = String(TYPING_SPEEDS.length - 1);
  input.step = "1";
  input.value = String(index);
  input.setAttribute("aria-label", "Typing speed");
  const thumbRail = el("span", "spd-thumbrail");
  const thumb = el("span", "spd-thumb");
  thumbRail.append(thumb);
  // Input FIRST so `:hover ~`, `:active ~` and `:focus-visible ~` can reach the
  // drawn thumb; the painted layers sit above it but ignore pointer events.
  rail.append(input, el("span", "spd-track"), fill, ticks, thumbRail);

  const demo = el("p", "spd-demo");

  const reflect = () => {
    const at = Number(input.value);
    const speed = TYPING_SPEEDS[at];
    const ratio = at / (TYPING_SPEEDS.length - 1);
    name.textContent = speed.label;
    input.setAttribute("aria-valuetext", speed.label);
    // Fill and thumb share the same 7px insets, so they ease together instead
    // of the fill arriving first.
    fill.style.width = `calc(7px + (100% - 14px) * ${ratio})`;
    thumb.style.left = `${ratio * 100}%`;
    // Passed stops read as done; the one ahead stays quiet.
    Array.from(ticks.children).forEach((tick, i) => {
      (tick as HTMLElement).style.background = i <= at ? "var(--panel)" : "var(--line)";
    });
    const ms = typewriterDelayMs();
    rate.textContent = ms === 0 ? "no delay" : `${Math.round(1000 / ms)} characters/sec`;
    runSpeedDemo(demo);
  };

  input.addEventListener("input", () => {
    dockSettings.typingSpeed = TYPING_SPEEDS[Number(input.value)].id as TypingSpeed;
    saveDockSettings();
    reflect();
  });

  block.append(head, rail, demo);
  group.append(settingsField("Typing speed", block));
  reflect();

  return group;
}

/**
 * "Turn off on this page" (Settings). Everything Starling draws on this page goes, except a dimmed
 * "Starling (off here)" in X's sidebar, which is how it comes back; the notice offers Undo for 8 s.
 * It used to be a "hide" link in the footer that made Starling vanish with no visible way back.
 */
async function disableStarlingOnThisPage() {
  const key = pagePreferenceKey(location.href);
  if (!key) return;
  try {
    const stored = await pagePrefs.get(DISABLED_PAGES_KEY);
    const pages = Array.isArray(stored[DISABLED_PAGES_KEY]) ? stored[DISABLED_PAGES_KEY] as string[] : [];
    if (!pages.includes(key)) pages.push(key);
    await pagePrefs.set({ [DISABLED_PAGES_KEY]: pages });
  } catch {
    if (handleOrphaned()) return;
    showDockNotice("Starling couldn't save that. Try again.");
    return;
  }
  pageDisabled = true;
  applyMountState();
  showPageNotice("Starling is off on this page.", { label: "Undo", run: () => void enableStarlingOnThisPage({ quiet: true }) }, 8_000);
}

/** The counterpart to `disableStarlingOnThisPage`, which for a long time had none.
 *
 * Nothing removed a page from `starling:disabled-pages`, so turning a page off
 * was permanent. The dimmed nav item, the toolbar icon and the keyboard
 * shortcut all call this now. */
async function enableStarlingOnThisPage(options: { quiet?: boolean } = {}) {
  const key = pagePreferenceKey(location.href);
  if (key) {
    try {
      const stored = await pagePrefs.get(DISABLED_PAGES_KEY);
      const pages = Array.isArray(stored[DISABLED_PAGES_KEY]) ? stored[DISABLED_PAGES_KEY] as string[] : [];
      const kept = pages.filter(page => page !== key);
      await pagePrefs.set({ [DISABLED_PAGES_KEY]: kept });
    } catch {
      if (handleOrphaned()) return;
      /* falls through: the in-page state is still restored below */
    }
  }
  pageDisabled = false;
  applyMountState();
  if (options.quiet) return;
  // Turned back on on a page Starling never touches: say so, rather than
  // leaving an empty page that looks like it failed.
  showPageNotice(isExcludedRoute(location.pathname)
    ? "Starling stays off on direct messages, chat, settings and sign-in pages."
    : "Starling is back on this page.");
}

// Starling belongs with X's primary destinations. This deliberately lives outside
// the card host, so the card can be moved anywhere without looking tethered to
// a floating launcher. It is there before consent too (it opens the data
// notice) and on a page Starling is off on (dimmed, it turns it back on).
function ensureStarlingNavControl() {
  if (extensionReloaded || isExcludedRoute(location.pathname)) return;
  if (!pageDisabled && !canMountDock(bridgeState, pageGate())) return;
  const existing = document.getElementById(NAV_HOST_ID);
  if (existing && navButton?.isConnected) {
    updateStarlingNavState();
    return;
  }
  existing?.remove();
  navButton = null;

  const nav = findPrimarySideNav();
  if (!nav) return;
  const host = document.createElement("div");
  host.id = NAV_HOST_ID;
  host.setAttribute("data-starling-nav-control", "true");
  // Shrink-wrap so the hover pill hugs "✦ Starling" and curves off after it,
  // exactly like X's own nav items — not a full-width bar. align-self stops
  // X's column flex (align-items:stretch) from forcing it full width.
  host.style.cssText = "display:block;width:fit-content;align-self:flex-start;";
  host.dataset.theme = isDarkSurface(document.body) ? "dark" : "light";
  const shadow = host.attachShadow({ mode: "closed" });
  sealShadowKeyboard(shadow, { editableOnly: true });
  const style = document.createElement("style");
  style.textContent = navStyles;
  const button = document.createElement("button");
  button.type = "button";
  button.className = "vf-nav";
  button.setAttribute("aria-label", "Open Starling");
  button.setAttribute("aria-controls", CHAT_HOST_ID);
  button.setAttribute("aria-haspopup", "dialog");
  button.innerHTML = `
    <span class="vf-nav-mark" aria-hidden="true">
      <svg viewBox="0 0 24 24" focusable="false"><path d="M12 2.5c.9 5.6 3.9 8.6 9.5 9.5-5.6.9-8.6 3.9-9.5 9.5-.9-5.6-3.9-8.6-9.5-9.5C8.1 11.1 11.1 8.1 12 2.5Z"/></svg>
    </span>
    <span class="vf-nav-label">Starling</span>`;
  button.addEventListener("click", () => {
    if (pageDisabled) void enableStarlingOnThisPage();
    else toggleDock();
  });
  shadow.append(style, button);

  const grok = findSideNavItem(nav, "Grok", "/i/grok");
  const home = findSideNavItem(nav, "Home", "/home");
  if (grok) sideNavSlot(nav, grok).after(host);
  else if (home) sideNavSlot(nav, home).after(host);
  else nav.append(host);
  navButton = button;
  updateStarlingNavState();
}

function findPrimarySideNav(): HTMLElement | null {
  const navs = Array.from(document.querySelectorAll<HTMLElement>('nav[aria-label="Primary"], nav[aria-label*="Primary" i], nav[role="navigation"]'));
  const primary = navs.find(nav => nav.querySelector('a[href="/home"], [data-testid="AppTabBar_Home_Link"]'));
  if (primary) return primary;
  return document.querySelector<HTMLElement>('a[href="/home"], [data-testid="AppTabBar_Home_Link"]')?.closest<HTMLElement>("nav") ?? null;
}

function findSideNavItem(nav: HTMLElement, label: string, href: string): HTMLElement | null {
  const expectedLabel = label.toLowerCase();
  return Array.from(nav.querySelectorAll<HTMLElement>('a, [role="link"], button')).find(item => {
    const itemHref = item.getAttribute("href") ?? "";
    if (itemHref === href || itemHref.startsWith(`${href}?`)) return true;
    const itemLabel = (item.getAttribute("aria-label") ?? item.innerText ?? item.textContent ?? "")
      .replace(/\s+/g, " ").trim().toLowerCase();
    return itemLabel === expectedLabel;
  }) ?? null;
}

function sideNavSlot(nav: HTMLElement, item: HTMLElement): HTMLElement {
  let slot = item;
  while (slot.parentElement && slot.parentElement !== nav) slot = slot.parentElement;
  return slot.parentElement === nav ? slot : item;
}

function updateStarlingNavState(open = dockIsOpen()) {
  if (!navButton?.isConnected) return;
  const label = navButton.querySelector<HTMLElement>(".vf-nav-label");
  if (pageDisabled) {
    // Dimmed, and it says why: the one visible way to turn Starling back on for this page.
    navButton.dataset.off = "true";
    navButton.dataset.open = "false";
    navButton.removeAttribute("aria-expanded");
    navButton.setAttribute("aria-label", "Starling is off on this page. Turn it back on");
    if (label) label.textContent = "Starling (off here)";
    return;
  }
  delete navButton.dataset.off;
  if (label && label.textContent !== "Starling") label.textContent = "Starling";
  navButton.setAttribute("aria-expanded", String(open));
  navButton.dataset.open = String(open);
  navButton.setAttribute("aria-label", open ? "Close Starling" : "Open Starling");
}

interface SidebarDockMountPoint {
  parent: HTMLElement;
  before: Node | null;
}

function normalizeSidebarLabel(value: string): string {
  return value.replace(/[\u2018\u2019]/g, "'").replace(/\s+/g, " ").trim().toLowerCase();
}

function findSidebarNewsHeading(sidebar: HTMLElement): HTMLElement | null {
  const candidates = sidebar.querySelectorAll<HTMLElement>("h1, h2, h3, [role=heading], span, div");
  return Array.from(candidates).find(candidate => normalizeSidebarLabel(candidate.textContent ?? "") === "today's news") ?? null;
}

// Find the child of the first common flow container. This is more resilient
// than assuming X's right rail has one fixed wrapper around each card.
function sidebarFlowSlot(sidebar: HTMLElement, target: HTMLElement, other?: HTMLElement | null): HTMLElement | null {
  let slot = target;
  while (slot.parentElement && slot.parentElement !== sidebar) {
    const parent = slot.parentElement;
    if (other && parent.contains(other) && !slot.contains(other)) return slot;
    slot = parent;
  }
  return slot.parentElement === sidebar ? slot : null;
}

function findFirstSidebarModuleAnchor(sidebar: HTMLElement, search: HTMLElement | null): HTMLElement | null {
  const searchBottom = search?.getBoundingClientRect().bottom ?? -Infinity;
  const candidates = Array.from(sidebar.querySelectorAll<HTMLElement>('h1, h2, h3, [role="heading"], [data-testid="trend"]'));
  return candidates
    .filter(candidate => isVisible(candidate) && candidate.getBoundingClientRect().top >= searchBottom - 8)
    .sort((left, right) => left.getBoundingClientRect().top - right.getBoundingClientRect().top)[0] ?? null;
}

function findSidebarDockMountPoint(): SidebarDockMountPoint | null {
  const sidebar = document.querySelector<HTMLElement>('[data-testid="sidebarColumn"]');
  if (!sidebar) return null;
  const search = sidebar.querySelector<HTMLElement>('[data-testid="SearchBox_Search_Input"]');
  const news = findSidebarNewsHeading(sidebar);

  // The intended home is exactly between Search and Today’s News.
  if (news) {
    const newsSlot = sidebarFlowSlot(sidebar, news, search);
    if (newsSlot?.parentElement) return { parent: newsSlot.parentElement, before: newsSlot };
  }

  // Detail, profile, and community pages replace Today’s News with modules such
  // as Relevant people. Use the first visible module below Search so an open
  // Starling card follows the user without depending on one particular heading.
  const firstModule = findFirstSidebarModuleAnchor(sidebar, search);
  if (firstModule) {
    const moduleSlot = sidebarFlowSlot(sidebar, firstModule, search);
    if (moduleSlot?.parentElement) return { parent: moduleSlot.parentElement, before: moduleSlot };
  }

  // Last-resort fallback for a rail still hydrating its first module.
  if (search) {
    const searchSlot = sidebarFlowSlot(sidebar, search, news);
    if (searchSlot?.parentElement) return { parent: searchSlot.parentElement, before: searchSlot.nextSibling };
  }
  return null;
}

function setSidebarDockTargetState(target: HTMLElement, locked: boolean) {
  const dark = isDarkSurface(target.parentElement ?? document.body);
  const fg = dark ? "#ffffff" : "#0a0a0a";
  const line = dark ? "#2f3336" : "#d7dbdf";
  target.dataset.locked = String(locked);
  target.textContent = "Drop to dock Starling";
  target.style.border = `2px dashed ${locked ? fg : line}`;
  target.style.background = locked ? (dark ? "rgba(255,255,255,.05)" : "rgba(10,10,10,.04)") : "transparent";
  target.style.color = locked ? fg : (dark ? "#8b98a5" : "#5b6470");
  target.style.transform = "scale(1)";
  target.style.boxShadow = "none";
}

function showSidebarDockTarget(): HTMLElement | null {
  const point = findSidebarDockMountPoint();
  if (!point) return null;
  const target = sidebarDockTarget?.isConnected
    ? sidebarDockTarget
    : document.getElementById(SIDEBAR_DOCK_TARGET_ID) ?? document.createElement("div");
  target.id = SIDEBAR_DOCK_TARGET_ID;
  target.setAttribute("aria-hidden", "true");
  target.style.cssText = "display:flex;align-items:center;justify-content:center;min-height:76px;margin:12px 0;padding:0 14px;border-radius:16px;font:700 12px/1 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;letter-spacing:.01em;user-select:none;pointer-events:none;transition:background .16s ease,border-color .16s ease,box-shadow .16s ease,transform .16s ease,color .16s ease;";
  setSidebarDockTargetState(target, false);
  const alreadyInSlot = target.parentElement === point.parent
    && (target.nextSibling === point.before || target.nextElementSibling === point.before);
  if (!alreadyInSlot) point.parent.insertBefore(target, point.before);
  sidebarDockTarget = target;
  return target;
}

function hideSidebarDockTarget() {
  sidebarDockTarget?.remove();
  document.getElementById(SIDEBAR_DOCK_TARGET_ID)?.remove();
  sidebarDockTarget = null;
}

function isDockTargetHovered(panel: HTMLElement, target: HTMLElement, pointer: PointerEvent): boolean {
  const targetBounds = target.getBoundingClientRect();
  const panelBounds = panel.getBoundingClientRect();
  const pointerInside = pointer.clientX >= targetBounds.left - 20 && pointer.clientX <= targetBounds.right + 20
    && pointer.clientY >= targetBounds.top - 20 && pointer.clientY <= targetBounds.bottom + 20;
  const overlapWidth = Math.max(0, Math.min(panelBounds.right, targetBounds.right) - Math.max(panelBounds.left, targetBounds.left));
  const overlapHeight = Math.max(0, Math.min(panelBounds.bottom, targetBounds.bottom) - Math.max(panelBounds.top, targetBounds.top));
  return pointerInside || (overlapWidth >= Math.min(72, targetBounds.width * .45) && overlapHeight >= 24);
}

function dockFloatingCardInSidebar(host: HTMLElement, panel: HTMLElement) {
  hideSidebarDockTarget();
  panel.classList.remove("dock-targeting");
  dockPlacement = "sidebar";
  host.dataset.placement = "sidebar";
  if (!ensureDockSidebarMount()) {
    dockPlacement = "floating";
    host.dataset.placement = "floating";
    updateDockPlacementUi(panel);
    return;
  }
  panel.classList.add("docking");
  panel.addEventListener("animationend", () => panel.classList.remove("docking"), { once: true });
}

function updateDockPlacementUi(panel: HTMLElement) {
  const bar = panel.querySelector<HTMLElement>(".bar");
  const hint = panel.querySelector<HTMLElement>(".dock-hint");
  const docked = dockPlacement === "sidebar";
  // No tooltip on the bar. `cursor:grab` already says "drag me", and a bubble
  // spanning the whole header would fight the gear's and the close button's
  // every time the pointer crossed it.
  if (bar) bar.removeAttribute("title");
  if (hint) hint.textContent = "";
  bar?.setAttribute("aria-label", docked ? "Drag to detach Starling" : "Drag to move Starling");
  panel.setAttribute("aria-modal", "false");
}

function applySidebarHostLayout(host: HTMLElement, panel: HTMLElement) {
  const hidden = host.style.display === "none";
  host.style.cssText = "display:block;position:relative;width:100%;z-index:1;";
  if (hidden) host.style.display = "none";
  panel.style.width = "";
  panel.style.height = "";
}

function ensureDockSidebarMount(): boolean {
  if (!chatHost || !chatShadow || dockPlacement !== "sidebar") return false;
  const panel = chatShadow.querySelector<HTMLElement>(".panel");
  const point = findSidebarDockMountPoint();
  if (!panel || !point) return false;

  applySidebarHostLayout(chatHost, panel);
  chatHost.dataset.placement = "sidebar";
  const alreadyInSlot = chatHost.parentElement === point.parent
    && (chatHost.nextSibling === point.before || chatHost.nextElementSibling === point.before);
  if (!alreadyInSlot) {
    point.parent.insertBefore(chatHost, point.before);
  }
  updateDockPlacementUi(panel);
  updateStarlingNavState();
  return true;
}

function reconcileOpenDockAfterNavigation() {
  const panel = chatShadow?.querySelector<HTMLElement>(".panel");
  if (dockPlacement !== "sidebar" || !panel || panel.hidden || panel.classList.contains("closing")) return;
  const remount = () => {
    const currentPanel = chatShadow?.querySelector<HTMLElement>(".panel");
    if (dockPlacement === "sidebar" && currentPanel && !currentPanel.hidden && !currentPanel.classList.contains("closing")) {
      ensureDockSidebarMount();
    }
  };
  requestAnimationFrame(remount);
  window.setTimeout(remount, 350);
}

function mountChatDock(): ShadowRoot {
  if (chatHost && chatShadow) {
    if (dockPlacement === "sidebar") ensureDockSidebarMount();
    else if (!chatHost.isConnected) document.documentElement.append(chatHost);
    return chatShadow;
  }
  const host = document.createElement("div");
  host.id = CHAT_HOST_ID;
  const shadow = host.attachShadow({ mode: "closed" });
  // Typing in the dock must never reach X's hotkeys: events leaving a shadow
  // root retarget to this host <div>, so X reads "no input focused" and fires
  // its shortcuts ("?" shortcuts dialog, "n" its composer) mid-sentence.
  sealShadowKeyboard(shadow);
  chatHost = host;
  chatShadow = shadow;
  applyDockTheme();
  // Hand the dock to the early script's shield, which is already listening.
  publishDockHandle({ host, shadow, onKeydown: handleDockKeydown });
  const style = document.createElement("style");
  style.textContent = dockStyles;
  const panel = document.createElement("section");
  panel.className = "panel";
  panel.hidden = true;
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-label", "Starling");
  panel.innerHTML = `
    <header class="bar">
      <span class="dot" data-health="unknown"></span>
      <!-- Left empty: the wordmark image is injected below, because a content
           script cannot reference an extension asset by URL in static markup --
           chrome.runtime.getURL has to run first. aria-label carries the name
           for screen readers, since the <strong> has no text of its own. -->
      <strong class="starling-wordmark" aria-label="STARLING"></strong>
      <span class="dock-hint" aria-hidden="true"></span>
      <button class="gear" type="button" aria-label="Starling settings" aria-expanded="false"><svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M19.14 12.94c.04-.3.06-.61.06-.94 0-.32-.02-.64-.07-.94l2.03-1.58c.18-.14.23-.41.12-.61l-1.92-3.32c-.12-.22-.37-.29-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54A.484.484 0 0 0 14.4 2h-3.84c-.24 0-.43.17-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96c-.22-.08-.47 0-.59.22L2.74 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.05.3-.09.63-.09.94s.02.64.07.94l-2.03 1.58c-.18.14-.23.41-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84c.24 0 .44-.17.47-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6c-1.98 0-3.6-1.62-3.6-3.6s1.62-3.6 3.6-3.6 3.6 1.62 3.6 3.6-1.62 3.6-3.6 3.6z"/></svg></button>
      <button class="close" aria-label="Close Starling">×</button>
    </header>
    <div class="context-chip" hidden>
      <img class="chip-avatar" alt="" hidden>
      <div class="chip-text">
        <div class="chip-line"><span class="chip-icon"></span><span class="chip-label"></span></div>
        <span class="chip-excerpt"></span>
      </div>
    </div>
    <section class="state" hidden aria-live="polite" aria-label="Starling account status"></section>
    <div class="feed" role="log" aria-live="polite"></div>
    <section class="settings" hidden aria-label="Starling settings"></section>
    <form class="composer">
      <input class="attach-input" type="file" multiple hidden
        accept=".txt,.md,.markdown,.mdx,.csv,.tsv,.json,.log,.rtf,.html,.htm,.xml,.yaml,.yml,.ini,.toml,.js,.ts,.jsx,.tsx,.py,.rb,.go,.rs,.java,.c,.cpp,.cs,.php,.sh,.sql,text/*">
      <button class="attach" type="button" aria-label="Attach files for Starling to read"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 5v14"/><path d="M5 12h14"/></svg></button>
      <div class="input-shell">
        <div class="att-zone"><div class="att-clip"><div class="attachments"></div></div></div>
        <textarea rows="1" maxlength="${CHAT_MESSAGE_MAX_CHARS}" placeholder="Ask Starling…" aria-describedby="starling-composer-hint"></textarea>
      </div>
      <button class="send" type="submit" data-mode="send" aria-label="Send"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 19V5"/><path d="M5 12l7-7 7 7"/></svg></button>
    </form>
    <p class="composer-hint" id="starling-composer-hint"><span class="composer-cost"></span><span class="composer-count" hidden></span></p>
    <footer class="foot">
      <span class="foot-status"><button class="foot-usage" type="button"></button><button class="foot-upgrade" type="button" hidden>· Upgrade</button><span class="foot-build"></span></span>
    </footer>
    <div class="edge edge-l" aria-hidden="true"></div>
    <div class="edge edge-r" aria-hidden="true"></div>
    <div class="edge edge-t" aria-hidden="true"></div>
    <div class="edge edge-b" aria-hidden="true"></div>
    <div class="edge edge-tl" aria-hidden="true"></div>
    <div class="edge edge-tr" aria-hidden="true"></div>
    <div class="edge edge-br" aria-hidden="true"></div>
    <div class="edge edge-bl" aria-hidden="true"></div>`;
  shadow.append(style, panel);

  // The wordmark has to be injected rather than written into the template
  // above: chrome.runtime.getURL resolves an extension-internal URL that does
  // not exist until runtime. It also requires the asset to be listed under
  // web_accessible_resources in wxt.config.ts, or x.com is refused access to it
  // and the bar renders an empty box.
  const wordmark = panel.querySelector<HTMLElement>(".starling-wordmark");
  let wordmarkUrl = "";
  try {
    wordmarkUrl = chrome.runtime.getURL("assets/starling-wordmark.png");
  } catch {
    // Left behind by an update: the panel still has to open, to say so.
  }
  if (wordmark && wordmarkUrl) {
    const image = document.createElement("img");
    image.src = wordmarkUrl;
    image.alt = "STARLING";
    // An <img> is natively draggable, and this one sits in the middle of the
    // drag handle: grabbing the letters started an HTML5 image drag — a ghost
    // of the logo trailing the cursor, and a panel that stayed put — instead of
    // moving the dock. The CSS rule cannot do this alone; Chrome honours the
    // attribute where -webkit-user-drag is unreliable on replaced elements.
    image.draggable = false;
    wordmark.append(image);
  }

  void loadDockRect();

  // Show the live build number right here on x.com so the running version is
  // verifiable without opening chrome://extensions.
  try {
    const build = panel.querySelector<HTMLElement>(".foot-build");
    if (build) build.textContent = ` · v${chrome.runtime.getManifest().version}`;
  } catch { /* manifest always present in-extension; ignore if not */ }

  const feedEl = panel.querySelector<HTMLElement>(".feed");
  feedEl?.addEventListener("scroll", () => {
    // Starling's own corrections must never be mistaken for the user taking
    // over — only a scroll nobody scheduled counts as deliberate.
    if (programmaticFeedScrolls > 0) { programmaticFeedScrolls -= 1; return; }
    // A deliberate scroll away from the anchor releases the anchor lock so the
    // user can freely read; the next message re-anchors.
    if (feedAnchorTop != null
        && shouldReleaseAnchor(feedAnchorTop, feedEl.scrollTop, releaseThreshold(feedEl.clientHeight))) {
      releaseFeedAnchor();
    }
  }, { passive: true });

  const textarea = panel.querySelector<HTMLTextAreaElement>("textarea")!;
  // The box grows with what is typed, up to four lines, then scrolls its own
  // text: a one-line box hid everything but the last few words of a longer ask.
  // Past 1,800 characters it counts toward the service's 2,000 limit.
  textarea.addEventListener("input", () => syncChatBox(textarea));
  feedEl?.append(renderEmptyHint());
  // Enter-to-send and Escape-to-close live in shieldDockTyping (document
  // capture): keys are stopped there before they can reach X's hotkeys, which
  // means they never reach this field either. One handler, one source of truth.
  // "+" attach: open the native file picker, read the chosen files' text, and
  // show them as removable chips the AI will read on the next send.
  const attachButton = panel.querySelector<HTMLButtonElement>(".attach")!;
  const attachInput = panel.querySelector<HTMLInputElement>(".attach-input")!;
  attachButton.addEventListener("click", () => attachInput.click());
  attachInput.addEventListener("change", () => {
    void handleAttachmentFiles(attachInput.files);
    // Reset so picking the same file again still fires "change".
    attachInput.value = "";
  });
  // Delegate chip removal (chips are re-rendered, so bind once on the row).
  panel.querySelector<HTMLElement>(".attachments")!.addEventListener("click", event => {
    const remove = (event.target as HTMLElement).closest<HTMLElement>(".att-remove");
    if (!remove) return;
    const id = Number(remove.dataset.id);
    if (Number.isFinite(id)) removeAttachment(id);
  });
  panel.querySelector<HTMLFormElement>("form")!.addEventListener("submit", event => {
    event.preventDefault();
    const send = panel.querySelector<HTMLButtonElement>(".send")!;
    if (send.dataset.mode === "stop") {
      activeChatStream?.cancel();
      return;
    }
    void sendChatMessage(textarea);
  });
  panel.querySelector<HTMLButtonElement>(".close")!.addEventListener("click", closeDock);
  panel.querySelector<HTMLButtonElement>(".gear")!.addEventListener("click", toggleSettings);
  // The footer's usage line is the door to Account & usage; "Upgrade" appears
  // beside it when 20% or less of the month's drafts are left ("Contact
  // support" on the top plan, which has nothing to upgrade to).
  panel.querySelector<HTMLButtonElement>(".foot-usage")!.addEventListener("click", () => {
    if (!settingsOpen) openSettings();
    settingsRoot()?.querySelector<HTMLElement>(".account-group")?.scrollIntoView({ block: "start" });
  });
  panel.querySelector<HTMLButtonElement>(".foot-upgrade")!.addEventListener("click", () => {
    runUiAction(footerUpgradeAction(bridgeState) ?? { kind: "open_site", page: "pricing", label: "See plans" });
  });

  // Every hover hint in the panel, drawn in-panel. The header ones point down,
  // the footer ones up — `showTip` decides from where the control sits.
  tip(panel.querySelector<HTMLElement>(".dot"), "Green: ready to draft. Amber: something needs you. Red: Starling can't draft right now.");
  tip(panel.querySelector<HTMLElement>(".gear"), "Settings");
  tip(panel.querySelector<HTMLElement>(".close"), () => {
    const shortcut = bridgeState.shortcut?.trim();
    return shortcut ? `Close · ${shortcut} reopens` : "Close Starling";
  });
  tip(panel.querySelector<HTMLElement>(".attach"), "Attach files for Starling to read and draft from");
  tip(panel.querySelector<HTMLElement>(".foot-usage"), "Account & usage");
  tip(panel.querySelector<HTMLElement>(".foot-build"), "The version running on this page, matching chrome://extensions");
  // The drag affordance belongs to the bar itself, but only over dead space —
  // the buttons have their own, and two tooltips racing looks broken.
  const bar = panel.querySelector<HTMLElement>(".bar");
  bar?.addEventListener("pointerleave", hideTip);

  installDockDrag(host, panel);
  installDockResize(host, panel);
  if (!ensureDockSidebarMount()) moveDockToFloating(host, panel);
  updateDockState();
  return shadow;
}

/**
 * Show the panel. `focusInput` only when the customer opened it on purpose (the nav item, the
 * toolbar icon, the shortcut, a consent action): every new card opens the panel too, and taking
 * the keyboard away from X's own box each time is how a reply half-typed in X lost its keystrokes.
 */
function openDock(options: { focusInput?: boolean } = {}) {
  if (!canMountDock(bridgeState, pageGate())) return;
  ensureStarlingNavControl();
  const shadow = mountChatDock();
  // X's own theme may have changed since this dock mounted.
  applyDockTheme();
  const host = chatHost;
  const panel = shadow.querySelector<HTMLElement>(".panel");
  const wasClosing = Boolean(panel?.classList.contains("closing"));
  dockCloseSequence += 1;
  dockCloseAnimation?.cancel();
  dockCloseAnimation = null;
  panel?.classList.remove("closing");
  panel?.removeAttribute("aria-hidden");

  // Every fresh open starts in the right rail. A detached card is a temporary
  // working state, not a second permanent launcher.
  if (host && panel && (panel.hidden || wasClosing)) {
    dockPlacement = "sidebar";
    host.dataset.placement = "sidebar";
  }
  if (host && dockPlacement === "sidebar" && !ensureDockSidebarMount() && panel) {
    moveDockToFloating(host, panel);
  }
  if (host) host.style.display = "";
  if (panel && (panel.hidden || wasClosing)) {
    panel.hidden = false;
    panel.classList.add("opening");
    panel.addEventListener("animationend", () => panel.classList.remove("opening"), { once: true });
    // A fresh open is when current numbers matter most.
    void refreshSession();
  }
  updateStarlingNavState(true);
  if (options.focusInput) shadow.querySelector<HTMLTextAreaElement>("textarea")?.focus();
  // Surface any scheduled drafts whose time has come as ready-to-post reminders.
  if (canRequest(bridgeState, pageGate())) void surfaceDueDrafts();
}

function closeDock() {
  const panel = chatShadow?.querySelector<HTMLElement>(".panel");
  const host = chatHost;
  if (!panel || !host) return;
  // Reopening always lands on the conversation, never on a half-filled form.
  settingsOpen = false;
  closeOpenDropdown?.();
  stopSpeedDemo();
  hideTip();
  delete panel.dataset.view;
  chatShadow?.querySelector<HTMLElement>(".gear")?.setAttribute("aria-expanded", "false");
  const closeSequence = ++dockCloseSequence;
  const finish = () => {
    if (closeSequence !== dockCloseSequence) return;
    const completedAnimation = dockCloseAnimation;
    dockCloseAnimation = null;
    completedAnimation?.cancel();
    panel.classList.remove("opening", "closing");
    panel.removeAttribute("aria-hidden");
    hideSidebarDockTarget();
    panel.hidden = true;
    host.style.display = "none";
    dockPlacement = "sidebar";
    host.dataset.placement = "sidebar";
    // Moving the hidden host back into the rail makes the next open reliable
    // and leaves no empty spacer above Today’s News.
    ensureDockSidebarMount();
  };

  updateStarlingNavState(false);
  navButton?.focus({ preventScroll: true });
  if (panel.hidden || panel.classList.contains("closing")) {
    finish();
    return;
  }

  panel.classList.remove("opening");
  panel.classList.add("closing");
  panel.setAttribute("aria-hidden", "true");
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) {
    finish();
    return;
  }
  const animation = panel.animate(
    [
      { opacity: 1, transform: "translateY(0) scale(1)" },
      { opacity: 0, transform: "translateY(-5px) scale(.985)" },
    ],
    { duration: 150, easing: "cubic-bezier(.4,0,1,1)", fill: "both" },
  );
  dockCloseAnimation = animation;
  animation.onfinish = finish;
}

function dockIsOpen(): boolean {
  const panel = chatShadow?.querySelector<HTMLElement>(".panel");
  return Boolean(chatHost?.isConnected && chatHost.style.display !== "none" && panel && !panel.hidden && !panel.classList.contains("closing"));
}

function toggleDock() {
  // One layer at a time, the same order Escape walks: Settings first, then the
  // dock. Hitting the launcher while looking at Settings means "take me back to
  // the conversation" — closing Starling outright loses the thread as well as
  // the settings screen, and reopening lands somewhere the click never asked to
  // go.
  if (dockIsOpen() && settingsOpen) {
    closeSettings();
    chatShadow?.querySelector<HTMLTextAreaElement>("textarea")?.focus();
    return;
  }
  if (dockIsOpen()) closeDock();
  else openDock({ focusInput: true });
}

/** The empty panel: what Starling can do, as buttons that fill the box (and never send). */
function renderEmptyHint(): HTMLElement {
  const hint = el("div", "empty-hint");
  hint.append(el("p", "empty-lead", "Ask Starling anything, or start from one of these:"));
  const row = el("div", "suggest");
  for (const text of ["Give me post ideas for today", "Turn this idea into a post: ", "Make my draft shorter"]) {
    const button = el("button", "suggest-btn", text.trim());
    button.type = "button";
    button.addEventListener("click", () => {
      const textarea = chatShadow?.querySelector<HTMLTextAreaElement>(".composer textarea");
      if (!textarea) return;
      textarea.value = text;
      syncChatBox(textarea);
      textarea.focus();
      textarea.setSelectionRange(text.length, text.length);
    });
    row.append(button);
  }
  hint.append(row);
  return hint;
}

/** The chat box: grow to four lines, and count characters near the service's limit. */
function syncChatBox(textarea: HTMLTextAreaElement) {
  textarea.style.height = "auto";
  textarea.style.height = `${Math.min(textarea.scrollHeight, 100)}px`;
  textarea.style.overflowY = textarea.scrollHeight > 100 ? "auto" : "hidden";
  const count = chatShadow?.querySelector<HTMLElement>(".composer-count");
  if (!count) return;
  const length = textarea.value.length;
  count.hidden = length <= 1_800;
  count.textContent = `${length.toLocaleString("en-US")} / ${CHAT_MESSAGE_MAX_CHARS.toLocaleString("en-US")}`;
  count.classList.toggle("over", length >= CHAT_MESSAGE_MAX_CHARS);
}

/** Under the box: what an answer costs, and how many drafts are left to spend. */
function updateComposerHint() {
  const cost = chatShadow?.querySelector<HTMLElement>(".composer-cost");
  if (!cost) return;
  const session = bridgeState.signed_in ? bridgeState.session : null;
  if (!bridgeState.consent.accepted || !bridgeState.signed_in) {
    cost.textContent = "";
    return;
  }
  const each = `Each answer uses ${pressCost(session?.usage)}`;
  cost.textContent = session ? `${each} · ${Math.max(0, session.usage.remaining)} left` : each;
}

// ---------------------------------------------------------------------------
// The account in the dock: the state card, the header dot, the footer line,
// Settings → Account & usage, and what a failed request offers next. The words
// come from shared/usage-ui.ts and shared/consent.ts.
// ---------------------------------------------------------------------------
const NEAR_LIMIT_SHOWN_KEY = "starling:near-limit-shown";
/** The billing period whose near-limit notice this browser already showed; undefined until read. */
let nearLimitShownKey: string | null | undefined;
/** The near-limit notice on screen in this tab, so a re-render does not hide it. */
let nearLimitVisibleKey: string | null = null;
/** "Not now" on the out-of-drafts card: put away for this billing period, in this tab. */
let limitNoticeDismissedKey: string | null = null;
let consentPassiveChoice = false;
/** The extension was reloaded or updated underneath this page; only a reload fixes that. */
let extensionReloaded = false;
/** A sign-in window opened from the panel is still open. */
let signInPending = false;
/** "Withdraw consent" was pressed once and is waiting for Withdraw or Keep. */
let withdrawConfirming = false;

const FIRST_RUN_KEY = "starling:first-run";
/** Whether this browser has shown the one-time "You're ready to draft" card; undefined until read. */
let firstRunShown: boolean | undefined;

async function loadNearLimitShown() {
  try {
    const stored = await pagePrefs.get(NEAR_LIMIT_SHOWN_KEY);
    const value = stored[NEAR_LIMIT_SHOWN_KEY];
    nearLimitShownKey = typeof value === "string" ? value : null;
  } catch {
    nearLimitShownKey = null;
  }
}

async function loadFirstRun() {
  try {
    const stored = await pagePrefs.get(FIRST_RUN_KEY);
    firstRunShown = stored[FIRST_RUN_KEY] !== undefined;
  } catch {
    // Unknown: never show the welcome card twice by guessing.
    firstRunShown = undefined;
  }
}

function copyContext(extra: CopyContext = {}): CopyContext {
  return {
    usage: bridgeState.session?.usage ?? null,
    serviceMessage: bridgeState.session?.service.message ?? null,
    serviceScope: bridgeState.session?.service.scope ?? null,
    serviceRetryAt: bridgeState.session?.service.retry_at ?? null,
    upgradable: bridgeState.session?.plan.upgradable ?? null,
    signedOutReason: bridgeState.signed_out_reason,
    paymentIssue: bridgeState.session?.plan.payment_issue ?? null,
    planName: bridgeState.session?.plan.name ?? null,
    email: bridgeState.session?.account.email_masked ?? null,
    ...extra,
  };
}

/** The dock state at the last paint, to notice the account coming back to "ready". */
let previousDockState: DockState | null = null;
/** Came back to ready from this state; the refused card is offered again once the session is known. */
let pendingResumeFrom: DockState | null = null;

/** Paint everything that depends on the account: dot, state card, footer, Settings. */
function updateDockState() {
  if (!chatShadow) return;
  const state = dockStateFor(dockInputsFrom(bridgeState));
  // Out of drafts and then paid, signed out and then signed in, unconfirmed and then confirmed: the
  // card of the draft that was refused stops saying so, and offers that draft again.
  if (state === "ready") {
    if (previousDockState && isRecoverableState(previousDockState)) pendingResumeFrom = previousDockState;
    if (pendingResumeFrom && bridgeState.session) {
      resumeRefusedCards(bridgeState.session, pendingResumeFrom);
      pendingResumeFrom = null;
    }
  } else {
    pendingResumeFrom = null;
  }
  previousDockState = state;
  const dot = chatShadow.querySelector<HTMLElement>(".dot");
  if (dot) {
    dot.dataset.health = dotHealth(state);
    dot.setAttribute("role", "img");
    dot.setAttribute("aria-label", dockStateCopy(state, copyContext())?.title ?? "Ready to draft");
  }
  const usage = chatShadow.querySelector<HTMLElement>(".foot-usage");
  if (usage) usage.textContent = footerUsageLine(bridgeState);
  const upgrade = chatShadow.querySelector<HTMLElement>(".foot-upgrade");
  if (upgrade) {
    const next = footerUpgradeAction(bridgeState);
    upgrade.hidden = !next;
    if (next) upgrade.textContent = `· ${next.label}`;
  }
  updateComposerHint();
  renderDockState();
  if (settingsOpen) {
    replaceGroup(".account-group", renderAccountGroup);
    replaceGroup(".privacy-group", renderPrivacyGroup);
  }
}

/** Is there text in any of X's own boxes that a reload would throw away? */
function anyComposerHasText(): boolean {
  try {
    return findVisibleComposers().some(composer => readableComposerText(composer).trim().length > 0);
  } catch {
    return false;
  }
}

/**
 * This copy of Starling was left behind by an update: nothing it has on the page can work any more,
 * so it all goes, and the panel says what happened and what fixes it. It used to show Chrome's raw
 * "Extension context invalidated." under every ✦, with a Retry that failed again.
 */
function showOrphanedState() {
  extensionReloaded = true;
  cancelPassiveCapture();
  removeInjectedControls();
  for (const binding of opBindings.values()) {
    for (const timer of binding.timers) clearTimeout(timer);
    binding.timers.length = 0;
    stopElapsedTicker(binding);
  }
  if (!canMountDock(bridgeState, pageGate())) {
    showPageNotice("Starling was updated. Reload X to switch to the new version.", { label: "Reload X", run: () => location.reload() }, 12_000);
    return;
  }
  openDock();
  if (settingsOpen) closeSettings();
  renderDockState();
}

function renderOrphanedCard(): HTMLElement {
  const card = el("div", "state-card down");
  card.append(el("strong", "state-title", "Starling was updated"), el("p", "state-body", "Reload X to switch to the new version."));
  if (anyComposerHasText()) {
    card.append(el("p", "state-body state-warn", "Post or copy what you're writing first. Reloading clears X's open boxes."));
  }
  const actions = el("div", "chips");
  const reload = el("button", "copy-pill", "Reload X");
  reload.type = "button";
  reload.addEventListener("click", () => location.reload());
  actions.append(reload);
  card.append(actions);
  return card;
}

/** The card above the conversation: the data notice, or what stands between the user and a draft. */
function renderDockState() {
  const region = chatShadow?.querySelector<HTMLElement>(".state");
  if (!region) return;
  region.replaceChildren();
  const state = dockStateFor(dockInputsFrom(bridgeState));
  if (extensionReloaded) {
    region.append(renderOrphanedCard());
  } else if (state === "consent_needed") {
    region.append(renderConsentCard());
  } else {
    const copy = dockStateCopy(state, copyContext());
    const session = bridgeState.session;
    if (copy) {
      if (state === "limit_reached" && session && limitNoticeDismissedKey === usagePeriodKey(session)) {
        // Put away with "Not now". The footer still says 0 left, and a refused draft's card says why.
      } else {
        const onDismiss = state === "limit_reached" && session
          ? () => {
            limitNoticeDismissedKey = usagePeriodKey(session);
            renderDockState();
          }
          : undefined;
        region.append(renderCopyCard(copy, state === "offline" || state === "outdated" || state === "service_paused" ? "down" : "warn", onDismiss));
      }
    } else if (session) {
      const notice = readyNotice(session);
      if (notice) region.append(renderCopyCard(notice.copy, "info", notice.onDismiss));
    }
  }
  region.hidden = region.childElementCount === 0;
}

/** Near the limit (once per billing period on this browser), or a plan that is ending. */
function readyNotice(session: SessionJson): { copy: UiCopy; onDismiss?: () => void } | null {
  const periodKey = usagePeriodKey(session);
  const due = nearLimitVisibleKey === periodKey
    || (nearLimitShownKey !== undefined && shouldShowNearLimit(session, nearLimitShownKey));
  if (due && isNearLimit(session.usage)) {
    if (nearLimitVisibleKey !== periodKey) {
      nearLimitVisibleKey = periodKey;
      nearLimitShownKey = periodKey;
      void pagePrefs.set({ [NEAR_LIMIT_SHOWN_KEY]: periodKey }).catch(() => undefined);
    }
    return {
      copy: nearLimitCopy(session.usage, copyContext()),
      onDismiss: () => {
        nearLimitVisibleKey = null;
        renderDockState();
      },
    };
  }
  const ending = planEndingCopy(session, copyContext());
  return ending ? { copy: ending } : null;
}

/** One action as a button: the card's primary action filled, the rest outlined. */
function uiActionButton(action: UiAction, onClick: () => void): HTMLButtonElement {
  const waiting = action.kind === "sign_in" && signInPending;
  const button = el("button", action.primary || action.kind === "sign_in" || action.kind === "consent" ? "copy-pill" : "chip",
    waiting ? "Waiting for the sign-in window…" : action.label);
  button.type = "button";
  button.disabled = waiting;
  button.addEventListener("click", onClick);
  return button;
}

function renderCopyCard(copy: UiCopy, tone: "warn" | "down" | "info", onDismiss?: () => void): HTMLElement {
  const card = el("div", `state-card ${tone}`);
  card.append(el("strong", "state-title", copy.title), el("p", "state-body", copy.body));
  const actions = el("div", "chips");
  for (const action of copy.actions) {
    if (action.kind === "dismiss" && !onDismiss) continue;
    actions.append(uiActionButton(action, () => {
      if (action.kind === "dismiss") onDismiss?.();
      else runUiAction(action, { card });
    }));
  }
  if (onDismiss && !copy.actions.some(action => action.kind === "dismiss")) {
    const dismiss = el("button", "chip", "Dismiss");
    dismiss.type = "button";
    dismiss.addEventListener("click", () => onDismiss());
    actions.append(dismiss);
  }
  if (actions.childElementCount) card.append(actions);
  return card;
}

/**
 * The data notice and the consent choice, in the dock. Identical text to the onboarding page (the
 * text is versioned and hashed with the server; only the layout is the dock's own). The buttons stay
 * in view at the bottom of the notice, with a fade above them, however far it is scrolled: "Agree and
 * continue" used to sit far below the fold of a scroll area with no scrollbar.
 */
function renderConsentCard(): HTMLElement {
  const card = el("div", "state-card consent");
  card.append(el("strong", "state-title", DISCLOSURE.title), el("p", "state-body", DISCLOSURE.intro));
  for (const section of DISCLOSURE.sections) {
    card.append(el("p", "consent-heading", section.heading));
    const list = el("ul", "consent-list");
    for (const item of section.items) list.append(el("li", undefined, item));
    card.append(list);
  }
  const check = el("label", "consent-check");
  const box = el("input");
  box.type = "checkbox";
  box.checked = consentPassiveChoice;
  box.addEventListener("change", () => { consentPassiveChoice = box.checked; });
  check.append(box, el("span", undefined, DISCLOSURE.passive.label));
  card.append(check, el("p", "state-body", DISCLOSURE.passive.help));
  const actions = el("div", "chips consent-actions");
  const agree = el("button", "copy-pill", DISCLOSURE.agree);
  agree.type = "button";
  agree.addEventListener("click", () => void acceptConsentFromDock(agree));
  const later = el("button", "chip", DISCLOSURE.decline);
  later.type = "button";
  later.addEventListener("click", () => closeDock());
  actions.append(agree, later);
  if (STARLING_ORIGIN) {
    for (const [label, page] of [["Privacy policy", "privacy"], ["Your data", "data"]] as const) {
      const link = el("button", "set-link", label);
      link.type = "button";
      link.addEventListener("click", () => runUiAction({ kind: "open_site", page, label }));
      actions.append(link);
    }
  }
  card.append(actions);
  return card;
}

function adoptReplyState(reply: unknown): boolean {
  if (!isRecord(reply) || !isBridgeState(reply.state)) return false;
  bridgeState = reply.state;
  applyMountState();
  return true;
}

async function acceptConsentFromDock(button: HTMLButtonElement) {
  button.disabled = true;
  const reply = await sendToWorker({
    kind: MESSAGE_KINDS.consent, payload: { action: "accept", passive_learning: consentPassiveChoice },
  });
  if (!adoptReplyState(reply)) {
    button.disabled = false;
    if (!extensionReloaded) showDockNotice("Starling couldn't save that. Try again.");
  }
}

async function setPassiveLearning(on: boolean) {
  const reply = await sendToWorker({
    kind: MESSAGE_KINDS.consent, payload: { action: "set-passive", passive_learning: on },
  });
  adoptReplyState(reply);
}

async function withdrawConsentFromDock(button: HTMLButtonElement) {
  button.disabled = true;
  const reply = await sendToWorker({ kind: MESSAGE_KINDS.consent, payload: { action: "withdraw" } });
  if (adoptReplyState(reply)) {
    withdrawConfirming = false;
    if (settingsOpen) closeSettings();
    return;
  }
  button.disabled = false;
}

/**
 * Sign in from the panel. The button says it is waiting and cannot be pressed twice while the
 * window is open, and closing the window is said plainly, in grey: it is a choice, not an error.
 */
async function beginSignIn() {
  if (signInPending) return;
  signInPending = true;
  renderDockState();
  if (settingsOpen) replaceGroup(".account-group", renderAccountGroup);
  let reply: unknown = null;
  try {
    reply = await sendToWorker({ kind: MESSAGE_KINDS.beginSignIn });
  } finally {
    signInPending = false;
  }
  if (!adoptReplyState(reply)) {
    renderDockState();
    if (settingsOpen) replaceGroup(".account-group", renderAccountGroup);
  }
  if (isRecord(reply) && reply.ok !== true) {
    if (reply.code === "cancelled") showDockNotice("The sign-in window was closed. Sign in whenever you're ready.", false);
    else showDockNotice(typeof reply.error === "string" && reply.error ? reply.error : "Sign-in didn't finish. Try again.");
  }
}

async function signOutFromDock(button: HTMLButtonElement) {
  button.disabled = true;
  const reply = await sendToWorker({ kind: MESSAGE_KINDS.signOut });
  if (!adoptReplyState(reply)) button.disabled = false;
}

/** "I've confirmed it": ask the server now, and say so if the address is still unconfirmed. */
async function recheckAccount() {
  await refreshSession();
  if (dockStateFor(dockInputsFrom(bridgeState)) === "email_unverified") {
    showDockNotice("Not confirmed yet. Open the link in the email, then press I've confirmed it.", false);
  }
}

/** "Contact support": copy the reference (when there is one), then open the contact page. */
async function contactSupport(ref: string | undefined, card?: HTMLElement) {
  if (ref) {
    const copied = await navigator.clipboard.writeText(`Starling reference: ${ref}`).then(() => true, () => false);
    const note = copied ? `Reference ${ref} copied. Paste it into your message.` : `Mention reference ${ref} in your message.`;
    if (card) setCardActionStatus(card, note);
    else showDockNotice(note, false);
  }
  void sendToWorker({ kind: MESSAGE_KINDS.openSite, payload: { page: "contact" } });
}

function runUiAction(action: UiAction, options: { retry?: () => void; upgradePath?: string; card?: HTMLElement } = {}) {
  switch (action.kind) {
    case "sign_in":
      void beginSignIn();
      return;
    case "open_site": {
      // A server-suggested upgrade path is honoured only when it names an allowlisted page.
      const page: SitePage = action.page === "limit" ? sitePageForPath(options.upgradePath) ?? "limit" : action.page;
      openedSitePage = true;
      void sendToWorker({ kind: MESSAGE_KINDS.openSite, payload: { page } });
      return;
    }
    case "consent":
      openDock({ focusInput: true });
      if (settingsOpen) closeSettings();
      renderDockState();
      return;
    case "retry":
      if (options.retry) options.retry();
      else void refreshSession();
      return;
    case "refresh":
      void recheckAccount();
      return;
    case "support":
      void contactSupport(action.ref, options.card);
      return;
    case "dismiss":
      return;
  }
}

/** When the account blocks requests, open the dock on its card instead of sending. */
function showBlockingState(): boolean {
  if (!blocksRequests(dockStateFor(dockInputsFrom(bridgeState)))) return false;
  openDock();
  if (settingsOpen) closeSettings();
  renderDockState();
  return true;
}

/** A request that failed with a known error code. */
interface RequestFailure {
  code: ErrorCode | null;
  message: string;
  retryAfterS?: number;
  requestRef?: string;
  exposed?: boolean;
  upgradePath?: string;
}

function requestFailureFromResponse(response: ClientAssistResponse): RequestFailure {
  return {
    code: isErrorCode(response.error_code) ? response.error_code : null,
    message: response.error || "Starling couldn't finish that request. Nothing in X changed.",
    retryAfterS: response.retry_after_s,
    requestRef: response.request_ref,
    upgradePath: response.upgrade_path,
    exposed: false,
  };
}

function requestFailureFromStream(state: StreamState): RequestFailure {
  return {
    code: isErrorCode(state.code) ? state.code : null,
    message: state.error || "Starling couldn't finish that request. Nothing in X changed.",
    retryAfterS: state.retryAfterS,
    requestRef: state.requestRef,
    exposed: state.exposed,
  };
}

/** Refusals the dock's own account notice repeats, title, body and buttons (`dockStateCopy`). */
const ACCOUNT_NOTICE_CODES: ReadonlySet<string> = new Set([
  "signed_out", "email_unverified", "consent_required", "limit_reached", "payment_required", "fair_use_exceeded",
]);

interface RefusedDraft {
  card: HTMLElement;
  code: string;
  retry?: () => void;
}

/** Cards of drafts the account refused, newest last, waiting for the account to be ready again. */
let refusedDrafts: RefusedDraft[] = [];

/**
 * The account is ready again (paid, signed in, confirmed). The newest refused draft's card says so
 * and offers that exact draft again; older ones settle to one quiet line, so a customer who pressed
 * ✦ on five posts while out of drafts is not offered five drafts at once.
 */
function resumeRefusedCards(session: SessionJson, recoveredFrom: DockState) {
  const live = refusedDrafts.filter(entry => entry.card.isConnected);
  refusedDrafts = [];
  const latest = live.at(-1);
  for (const entry of live) {
    const { card } = entry;
    delete card.dataset.accountRefusal;
    const binding = card.dataset.opId ? opBindings.get(card.dataset.opId) : undefined;
    if (entry === latest && entry.retry) {
      const retry = entry.retry;
      card.replaceChildren(el("div", "deciding", resumedLine(session, recoveredFrom)));
      const row = el("div", "chips");
      const go = el("button", "copy-pill", resumeActionLabel(binding?.resume ?? { kind: "chat" }));
      go.type = "button";
      go.addEventListener("click", () => {
        if (binding) opBindings.delete(binding.op.id);
        card.remove();
        retry();
      });
      row.append(go);
      card.append(row);
    } else {
      card.replaceChildren(el("div", "deciding", "Not drafted. Nothing in X changed."));
    }
    if (binding) ensureCardHeader(binding);
  }
  scrollFeed();
}

/**
 * A failed request card: what happened, and the one or two things to do next.
 *
 * An account-level refusal (out of drafts, signed out, unconfirmed...) is the same thing the notice
 * above the conversation says, and two cards saying it (or worse, disagreeing) read as two problems.
 * While that notice shows, this card is one muted line; if the notice is not there, the full words
 * and buttons are (the stylesheet decides, from what is actually on screen).
 */
function renderRequestFailure(card: HTMLElement, failure: RequestFailure, retry?: () => void) {
  card.classList.remove("loading");
  const ctx = copyContext({ retryAfterS: failure.retryAfterS, requestRef: failure.requestRef, exposed: failure.exposed });
  // A pause the account state has not caught up with yet: the refusal's own words say what it is
  // (the bare default, "Drafting is paused.", would only repeat the card's title).
  const pausedMessage = failure.message.trim();
  if (failure.code === "service_paused" && !ctx.serviceMessage && pausedMessage && !/^drafting is paused\.?$/i.test(pausedMessage)) {
    ctx.serviceMessage = pausedMessage;
  }
  const copy = failure.code ? errorCopy(failure.code, ctx) : null;
  const full = el("div", "refusal-full");
  full.append(el("div", "deciding error", copy ? copy.title : failure.message));
  const body = copy ? el("small", "status", copy.body) : null;
  if (body) full.append(body);
  card.replaceChildren(full);
  for (const action of copy?.actions ?? []) {
    if (action.kind === "dismiss" || (action.kind === "retry" && !retry)) continue;
    const chip = addChip(full, action.label, () => runUiAction(action, { retry, upgradePath: failure.upgradePath, card }));
    if (action.primary) chip.className = "copy-pill";
    if (action.kind === "retry" && failure.retryAfterS && (failure.code === "rate_limited" || failure.code === "service_paused")) {
      // The body counts down with the button: "Starling can take your next request in 12s."
      startCountdown(chip, action.label, failure.retryAfterS, left => {
        if (body && failure.code === "rate_limited") body.textContent = errorCopy("rate_limited", { ...ctx, retryAfterS: left }).body;
      });
    }
  }
  if (!copy && retry) addChip(full, "Try again", () => retry());
  if (failure.code && ACCOUNT_NOTICE_CODES.has(failure.code)) {
    card.dataset.accountRefusal = failure.code;
    card.prepend(el("div", "deciding refusal-short", "Not drafted. See the note above. Nothing in X changed."));
    refusedDrafts = refusedDrafts.filter(entry => entry.card !== card && entry.card.isConnected);
    refusedDrafts.push({ card, code: failure.code, retry });
  }
  // These change what the dock should say; fetch the account state again.
  if (failure.code && ["signed_out", "email_unverified", "consent_required", "extension_outdated", "limit_reached", "payment_required", "account_suspended", "fair_use_exceeded", "service_paused"].includes(failure.code)) {
    void refreshSession();
  }
}

/** Keep a retry disabled until Retry-After has passed, counting down on the chip itself. */
function startCountdown(chip: HTMLButtonElement, label: string, seconds: number, onTick?: (left: number) => void) {
  const started = Date.now();
  const tick = () => {
    if (!chip.isConnected) return;
    const left = countdownRemaining(seconds, started, Date.now());
    onTick?.(left);
    if (left <= 0) {
      chip.disabled = false;
      chip.textContent = label;
      return;
    }
    chip.disabled = true;
    chip.textContent = `${label} in ${formatCountdown(left)}`;
    window.setTimeout(tick, 1_000);
  };
  tick();
}

function settingsButton(label: string, className: string, onClick: () => void): HTMLButtonElement {
  const button = el("button", className, label);
  button.type = "button";
  button.addEventListener("click", onClick);
  return button;
}

function renderAccountGroup(): HTMLElement {
  const group = el("section", "set-group account-group");
  group.append(el("h3", "set-title", "Account & usage"));
  const state = dockStateFor(dockInputsFrom(bridgeState));
  const session = bridgeState.signed_in ? bridgeState.session : null;

  const now = el("div", "set-now");
  now.dataset.state = dotHealth(state) === "ok" ? "ok" : "down";
  const dot = el("span", "dot");
  dot.dataset.health = dotHealth(state);
  const text = el("div", "set-now-text");
  if (!bridgeState.consent.accepted) {
    text.append(el("span", "set-now-model", "Not set up yet"), el("span", "set-now-where", "Agree to the data notice to start"));
  } else if (!bridgeState.signed_in) {
    text.append(el("span", "set-now-model", "Signed out"), el("span", "set-now-where", "Sign in to start drafting"));
  } else if (!session) {
    text.append(el("span", "set-now-model", "Signed in"), el("span", "set-now-where", bridgeState.online ? "Loading your plan…" : "Offline"));
  } else {
    text.append(el("span", "set-now-model", session.account.email_masked || "Signed in"), el("span", "set-now-where", `${session.plan.name} plan`));
  }
  now.append(dot, text);
  group.append(now);

  if (session) {
    const meter = usageMeter(session.usage);
    const bar = el("div", "meter");
    bar.setAttribute("role", meter.role);
    bar.setAttribute("aria-label", meter.label);
    bar.setAttribute("aria-valuemin", String(meter.min));
    bar.setAttribute("aria-valuemax", String(meter.max));
    bar.setAttribute("aria-valuenow", String(meter.now));
    bar.setAttribute("aria-valuetext", meter.valueText);
    const fill = el("span", "meter-fill");
    fill.style.width = `${meter.max > 0 ? Math.round((meter.now / meter.max) * 100) : 0}%`;
    bar.append(fill);
    const resets = session.usage.resets_at ? ` · resets ${formatDate(session.usage.resets_at)}` : "";
    group.append(settingsField("Usage", bar, `${meter.valueText}${resets}`));
    const ending = planEndingCopy(session, copyContext());
    if (ending) group.append(el("p", "set-help", `${ending.title}. ${ending.body}`));
  }
  const stateCopy = state === "consent_needed" || state === "ready" ? null : dockStateCopy(state, copyContext());
  if (stateCopy) group.append(el("p", "set-status error", `${stateCopy.title}. ${stateCopy.body}`));

  const actions = el("div", "set-actions");
  const site = (label: string, page: SitePage, className = "chip") => settingsButton(label, className, () => runUiAction({ kind: "open_site", page, label }));
  if (bridgeState.signed_in) {
    // A Free account had no way to upgrade from here.
    if (session && session.plan.code === "free") actions.append(site("See plans", "pricing"));
    actions.append(site("Manage account", "account"), site("Billing", "billing"), site("Devices", "devices"));
    const out = settingsButton("Sign out", "chip", () => void signOutFromDock(out));
    actions.append(out);
  } else if (bridgeState.consent.accepted) {
    const signIn = settingsButton(signInPending ? "Waiting for the sign-in window…" : "Sign in", "copy-pill", () => void beginSignIn());
    signIn.disabled = signInPending;
    actions.append(signIn, site("See plans", "pricing"));
  }
  if (actions.childElementCount) group.append(actions);
  return group;
}

function renderPrivacyGroup(): HTMLElement {
  const group = el("section", "set-group privacy-group");
  group.append(el("h3", "set-title", "Data & privacy"));
  if (!bridgeState.consent.accepted) {
    group.append(el("p", "set-help", "Starling sends nothing until you agree to the data notice in the panel."));
    return group;
  }
  group.append(switchField(
    "Learn from my own profile and posts",
    toggleSwitch(bridgeState.consent.passive_learning, DISCLOSURE.passive.label, on => void setPassiveLearning(on)),
    DISCLOSURE.passive.help));
  const actions = el("div", "set-actions");
  const withdraw = settingsButton("Withdraw consent", "chip", () => {
    // One more step before everything stops: withdrawing also signs this browser out.
    withdrawConfirming = true;
    replaceGroup(".privacy-group", renderPrivacyGroup);
    settingsRoot()?.querySelector<HTMLButtonElement>(".withdraw-keep")?.focus();
  });
  actions.append(
    settingsButton("Your data", "chip", () => runUiAction({ kind: "open_site", page: "data", label: "Your data" })),
    settingsButton("Privacy policy", "chip", () => runUiAction({ kind: "open_site", page: "privacy", label: "Privacy policy" })),
  );
  if (!withdrawConfirming) actions.append(withdraw);
  group.append(actions);
  if (withdrawConfirming) {
    const confirm = el("div", "set-warn withdraw-confirm");
    confirm.setAttribute("role", "group");
    confirm.setAttribute("aria-label", "Withdraw consent");
    confirm.append(el("p", "set-warn-lead", "This stops all drafting and signs this browser out."));
    const row = el("div", "set-actions");
    const yes = settingsButton("Withdraw", "chip danger", () => void withdrawConsentFromDock(yes));
    const keep = settingsButton("Keep", "copy-pill withdraw-keep", () => {
      withdrawConfirming = false;
      replaceGroup(".privacy-group", renderPrivacyGroup);
    });
    row.append(yes, keep);
    confirm.append(row);
    group.append(confirm);
  }
  group.append(el("p", "set-help",
    "Withdrawing stops everything Starling sends and signs this browser out. Export or delete what Starling holds from the Data page of your account."));
  return group;
}

/** Settings → This page: the one switch that takes Starling off a single X page. */
function renderPageGroup(): HTMLElement {
  const group = el("section", "set-group page-group");
  group.append(el("h3", "set-title", "This page"));
  const off = settingsButton("Turn off on this page", "chip", () => void disableStarlingOnThisPage());
  const path = location.pathname || "/";
  group.append(settingsField("Starling here", off, `Hides ✦, the pills and this panel on ${path}. Turn it back on from ✦ Starling in the sidebar.`));
  return group;
}

/**
 * The first signed-in X page: one card that says what to press, once per browser. "Show me" scrolls
 * to the nearest ✦ and pulses it three times (a still ring under reduced motion).
 */
function maybeShowFirstRun() {
  if (firstRunShown !== false || extensionReloaded || document.hidden) return;
  if (!canRequest(bridgeState, pageGate()) || !bridgeState.session) return;
  if (dockStateFor(dockInputsFrom(bridgeState)) !== "ready") return;
  firstRunShown = true;
  void pagePrefs.set({ [FIRST_RUN_KEY]: { shown_at: Date.now() } }).catch(() => undefined);
  openDock();
  const card = addCard("first-run");
  card.append(
    el("strong", "card-title", "You're ready to draft"),
    el("p", "body", "Press ✦ under any post and Starling writes a reply into X's reply box. Or press ✦ Create post beside Post. You always press Post yourself."),
  );
  const row = el("div", "chips");
  const show = el("button", "copy-pill", "Show me");
  show.type = "button";
  show.addEventListener("click", () => showMeTheStar());
  const done = el("button", "chip", "Got it");
  done.type = "button";
  done.addEventListener("click", () => {
    card.remove();
    if (!chatShadow?.querySelector(".feed .card")) chatShadow?.querySelector(".feed")?.prepend(renderEmptyHint());
  });
  row.append(show, done);
  card.append(row);
}

function showMeTheStar() {
  const hosts = Array.from(document.querySelectorAll<HTMLElement>("[data-starling-post-control]"));
  const host = hosts.find(candidate => isInViewport(candidate)) ?? hosts[0];
  const button = host ? postControlButtons.get(host) : undefined;
  if (!host || !button) {
    showDockNotice("Scroll to a post: its ✦ sits in the row with Reply, Repost and Like.", false);
    return;
  }
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
  host.scrollIntoView({ block: "center", behavior: reduced ? "auto" : "smooth" });
  button.dataset.pulse = reduced ? "still" : "true";
  button.focus({ preventScroll: true });
  window.setTimeout(() => { delete button.dataset.pulse; }, 2_700);
}

function clearDockContext() {
  const chip = chatShadow?.querySelector<HTMLElement>(".context-chip");
  if (chip) chip.hidden = true;
}

function setDockContext(action: ComposerAction, context?: PostContext, state: "ready" | "working" | "updated" | "error" = "ready") {
  const chip = chatShadow?.querySelector<HTMLElement>(".context-chip");
  if (!chip) return;
  // The chip earns its row only when it says something REAL about this
  // generation — the post being replied to or quoted. A bare "New post" line
  // (worse: wearing a leftover avatar from some unrelated post) is noise, and
  // an empty shell is worse — hide entirely instead. And only WHILE a request
  // runs: left up afterwards it labelled the whole feed, so older notes read
  // as advice about the newest post. Each card names its own post instead.
  const anchored = Boolean(context && (context.author_handle || context.author || (context.text ?? "").trim()));
  if (!context || !anchored || state !== "working") {
    chip.hidden = true;
    return;
  }
  const copy = composerAnchor(action, context.author_handle || context.author, context.text,
                              Boolean(context.is_own_post));
  chip.hidden = false;
  chip.dataset.state = state;
  const avatar = chip.querySelector<HTMLImageElement>(".chip-avatar");
  if (avatar) {
    const avatarUrl = contextAvatars.get(context);
    if (avatarUrl) { avatar.src = avatarUrl; avatar.hidden = false; }
    else { avatar.removeAttribute("src"); avatar.hidden = true; }
  }
  chip.querySelector<HTMLElement>(".chip-icon")!.textContent = action === "reply" ? "↩" : action === "quote" ? "❝" : "✎";
  chip.querySelector<HTMLElement>(".chip-label")!.textContent = copy.label;
  chip.querySelector<HTMLElement>(".chip-excerpt")!.textContent = copy.excerpt;
}

// ---- cards ----------------------------------------------------------------

type CardPlacement = "chat" | "standalone";

function addCard(className: string, placement: CardPlacement = "standalone"): HTMLElement {
  openDock();
  // A card that starts its own job (reply, quote, schedule reminder, error,
  // etc.) must never be left below an old chat anchor. Cards emitted as part of
  // the current chat answer deliberately retain that anchor instead.
  if (placement === "standalone") releaseFeedAnchor();
  const feed = chatShadow?.querySelector<HTMLElement>(".feed");
  feed?.querySelector(".empty-hint")?.remove();
  const card = document.createElement("article");
  card.className = `card ${className}`;
  // NEVER append past the anchor spacer. The spacer is blank slack sized to a
  // whole viewport; a card appended after it lands entirely below the fold and
  // reads as "the card never appeared" until the next anchor pass happens to
  // shuffle the spacer back to the end.
  const spacer = feed?.querySelector<HTMLElement>(".feed-spacer");
  if (spacer) spacer.before(card); else feed?.append(card);
  scrollFeed();
  return card;
}

// The chat anchor. A sent chat turn rises to the top, pushing prior
// turns above the fold. It survives streamed cards, card updates, and dock
// resizing, then releases only after a deliberate downward scroll.
let feedAnchorTop: number | null = null;
let feedAnchorElement: HTMLElement | null = null;
let anchorResizeObserver: ResizeObserver | null = null;
let anchorMutationObserver: MutationObserver | null = null;
let anchorRefreshFrame: number | null = null;
// Every scrollTop Starling writes itself fires a `scroll` event that is
// indistinguishable from the user's own. Without this the anchor could hand
// control back to "the user" on its OWN correction — which is exactly what a
// feed that scrolls itself up, then snaps down, then stops obeying looks like.
let programmaticFeedScrolls = 0;

function setFeedScrollTop(feed: HTMLElement, top: number) {
  if (Math.abs(feed.scrollTop - top) < 1) return;
  programmaticFeedScrolls += 1;
  feed.scrollTop = top;
}

function scrollFeed() {
  const feed = chatShadow?.querySelector<HTMLElement>(".feed");
  if (!feed) return;
  if (feedAnchorTop != null) { scheduleFeedAnchor(); return; }
  setFeedScrollTop(feed, feed.scrollHeight);
}

function releaseFeedAnchor() {
  feedAnchorTop = null;
  feedAnchorElement = null;
  anchorResizeObserver?.disconnect();
  anchorResizeObserver = null;
  anchorMutationObserver?.disconnect();
  anchorMutationObserver = null;
  if (anchorRefreshFrame != null) cancelAnimationFrame(anchorRefreshFrame);
  anchorRefreshFrame = null;
  chatShadow?.querySelector<HTMLElement>(".feed .feed-spacer")?.remove();
}

function feedSpacer(feed: HTMLElement): HTMLElement {
  let spacer = feed.querySelector<HTMLElement>(".feed-spacer");
  if (!spacer) {
    spacer = document.createElement("div");
    spacer.className = "feed-spacer";
    spacer.setAttribute("aria-hidden", "true");
    feed.append(spacer);
  } else if (spacer !== feed.lastElementChild) {
    feed.append(spacer); // cards appended after it would sit below the slack
  }
  return spacer;
}

/** Re-measure `el` and size the spacer so the anchor target is EXACTLY
 *  reachable; returns the applied scrollTop (read back, clamp-proof). */
function applyFeedAnchor(feed: HTMLElement, el: HTMLElement): number {
  const spacer = feedSpacer(feed);
  // offsetTop, NOT getBoundingClientRect: `.feed` is the offsetParent, so this
  // is the element's position in the feed's own content box — a number that is
  // immune to both the current scroll position and to CSS transforms. Rects
  // are not: every card runs a translateY pop-in on mount, so a rect-based
  // measurement taken while any card was animating computed a top that was
  // several px off and then corrected itself a frame later. Re-entered on
  // every streamed token and every ResizeObserver tick, that is precisely the
  // anchor visibly jittering up and down.
  const elTop = el.offsetTop;
  // The feed is a flex column with a row gap, so a spacer that is present at
  // height 0 STILL costs one gap. Measured: scrollHeight 732 with the spacer
  // vs 718 without it. Subtracting only `offsetHeight` therefore counted one
  // gap as real content and under-reserved by exactly that much on every pass.
  const gap = spacer.isConnected ? parseFloat(getComputedStyle(feed).rowGap) || 0 : 0;
  const plan = computeAnchor({
    clientHeight: feed.clientHeight,
    contentHeight: feed.scrollHeight - spacer.offsetHeight - gap,
    elTop,
  });
  // The reserve shrinks on its own as streamed content grows, and the cap and
  // the scroll target now come out of computeAnchor together — capping the
  // spacer here without lowering the target is what left the anchor 58px short
  // and stuck. See the note in shared/feed-anchor.ts.
  const height = `${Math.ceil(plan.spacerHeight)}px`;
  if (spacer.style.height !== height) spacer.style.height = height;
  setFeedScrollTop(feed, plan.scrollTop);
  return feed.scrollTop;
}

/** Scroll `el` to the top of the feed (message 2+), pushing prior turns above
 *  the fold — the "looks like a new chat but it just went up" behavior.
 *  While streaming grows the turn below the anchor, a ResizeObserver shrinks
 *  the spacer in step WITHOUT moving the pinned scroll position. */
function scheduleFeedAnchor() {
  if (anchorRefreshFrame != null || feedAnchorTop == null) return;
  anchorRefreshFrame = requestAnimationFrame(() => {
    anchorRefreshFrame = null;
    const feed = chatShadow?.querySelector<HTMLElement>(".feed");
    const el = feedAnchorElement;
    if (!feed || !el || !el.isConnected || !feed.contains(el)) {
      releaseFeedAnchor();
      return;
    }
    feedAnchorTop = applyFeedAnchor(feed, el);
  });
}

function observeFeedAnchor(feed: HTMLElement) {
  anchorResizeObserver?.disconnect();
  anchorResizeObserver = null;
  if (typeof ResizeObserver === "function") {
    anchorResizeObserver = new ResizeObserver(() => scheduleFeedAnchor());
    // Watch every card, not only a streaming response: chips, errors,
    // decisions and scheduled drafts can all change their layout.
    for (const card of feed.querySelectorAll<HTMLElement>(".card")) anchorResizeObserver.observe(card);
    anchorResizeObserver.observe(feed);
  }

  anchorMutationObserver?.disconnect();
  if (typeof MutationObserver === "function") {
    anchorMutationObserver = new MutationObserver(records => {
      if (anchorResizeObserver) {
        for (const record of records) {
          for (const node of record.addedNodes) {
            if (!(node instanceof HTMLElement)) continue;
            if (node.classList.contains("card")) anchorResizeObserver.observe(node);
            for (const card of node.querySelectorAll<HTMLElement>(".card")) anchorResizeObserver.observe(card);
          }
        }
      }
      scheduleFeedAnchor();
    });
    anchorMutationObserver.observe(feed, { childList: true, subtree: true, characterData: true });
  }
}

function installFeedAnchor(el: HTMLElement) {
  const feed = chatShadow?.querySelector<HTMLElement>(".feed");
  if (!feed || !el.isConnected || !feed.contains(el)) { releaseFeedAnchor(); return; }
  // ANCHOR THE FIRST TURN TOO. The prior-card guard fell back to "scroll to the
  // bottom", and on the very first question that pushes the user's own message
  // off the top of the panel as soon as the reply card is taller than the space
  // left — you end up reading an answer to a question you can no longer see.
  // Pinning the turn near the top is exactly as correct with nothing above it.
  feedAnchorElement = el;
  feedAnchorTop = applyFeedAnchor(feed, el);
  observeFeedAnchor(feed);
}

function anchorFeedTo(el: HTMLElement) {
  requestAnimationFrame(() => installFeedAnchor(el));
}

/** A fresh user turn sweeps stale cards BEFORE the anchor is measured:
 *  cancelled leftovers and loading shells whose operation no longer exists
 *  must not sit above the new message or pad the scroll math. Completed
 *  results, failed cards (their Retry is still useful), and chat bubbles are
 *  history — they stay. */
function pruneFeedForNewTurn() {
  const feed = chatShadow?.querySelector<HTMLElement>(".feed");
  if (!feed) return;
  for (const card of [...feed.querySelectorAll<HTMLElement>(".card")]) {
    const opId = card.dataset.opId;
    if (opId) {
      if (shouldPruneOnNewTurn(opBindings.get(opId)?.op)) {
        opBindings.delete(opId);
        card.remove();
      }
      continue;
    }
    // Op-less transient shells (dock notices, orphaned loading stubs) are
    // stale status by definition once a new turn starts. Bubbles and
    // decision/preference cards carry history and have no loading affordance.
    if (card.querySelector(".typing") || card.classList.contains("loading")) card.remove();
  }
}

function addBubble(kind: "user" | "assistant", text: string): HTMLElement {
  const card = addCard(kind === "user" ? "bubble user" : "bubble assistant", "chat");
  card.textContent = text;
  return card;
}

/**
 * Say something in the dock instead of dying quietly. Every path that can stop a
 * button from doing its job routes through here — a click must never be a
 * silent no-op, which is exactly what "nothing happens" looked like.
 */
function showDockNotice(message: string, isError = true): HTMLElement {
  openDock();
  // If the dock could not mount (bridge marked down, page teardown mid-flight),
  // the card below would be appended to nothing and the message would be lost —
  // a silent no-op. Fall back to the page-level banner, which needs no dock.
  if (!chatShadow) {
    showPageNotice(message);
    return document.createElement("article");
  }
  releaseFeedAnchor();
  const card = addCard("draft loading");
  const line = document.createElement("div");
  line.className = `deciding ${isError ? "error" : ""}`.trim();
  line.textContent = message;
  card.append(line);
  scrollFeed();
  return card;
}

function chipRow(card: HTMLElement): HTMLElement {
  let row = card.querySelector<HTMLElement>(".chips");
  if (!row) {
    row = document.createElement("div");
    row.className = "chips";
    card.append(row);
  }
  return row;
}

/** Keep an actionable card honest: one current outcome, never a pile of stale
 * notices or a button that looks successful after its request failed. */
function setCardActionStatus(card: HTMLElement, message: string, isError = false) {
  let note = card.querySelector<HTMLElement>("[data-starling-action-status]");
  if (!note) {
    note = document.createElement("small");
    note.className = "status";
    note.dataset.starlingActionStatus = "true";
    card.append(note);
  }
  note.classList.toggle("error", isError);
  note.textContent = message;
}

function addChip(card: HTMLElement, label: string,
                 onClick: (chip: HTMLButtonElement) => void | Promise<void>): HTMLButtonElement {
  const chip = document.createElement("button");
  chip.type = "button";
  chip.className = "chip";
  chip.textContent = label;
  const recover = () => {
    if (!chip.isConnected) return;
    chip.disabled = false;
    chip.textContent = label;
    setCardActionStatus(card, "Couldn't complete that action. Try again.", true);
  };
  chip.addEventListener("click", () => {
    try {
      void Promise.resolve(onClick(chip)).catch(recover);
    } catch {
      recover();
    }
  });
  chipRow(card).append(chip);
  return chip;
}

// ---------------------------------------------------------------------------
// Operation registry: every generation card belongs to exactly one op, and the
// ONLY way any timer or stream callback may touch a card is by dispatching an
// OpEvent through applyOpEvent. Terminal transitions atomically clear the
// op's timers and rebuild the card's children, so a "Writing your post…" label
// can never survive next to a "Cancelled." line, and a leaked heartbeat is
// absorbed by the state machine instead of repainting a settled card.
// ---------------------------------------------------------------------------
interface OpBinding {
  op: GenerationOp;
  card: HTMLElement;
  /** Label for the loading view when no heartbeat note has replaced it. */
  defaultLabel: string;
  timers: number[];
  /** Set just before dispatching a terminal event to supply a rich renderer
   *  (result lines, copy chips…). Defaults below cover the plain cases. */
  renderTerminal?: (card: HTMLElement, op: GenerationOp) => void;
  /** One automatic re-bind has been used after X rebuilt the composer. */
  rebound?: boolean;
  /** Stops this operation from the CARD. Every source of cancellation ends in
   *  an `{type:"cancelled"}` dispatch on this same op, so the card, the
   *  composer pill and the dock's Stop button can never show different states.
   *  Absent = this op has nothing to abort (nothing to offer the user). */
  onCancel?: () => void;
  /** What the op is acting on ("Reply to @handle"), the card's own one-line
   *  header, so a card is never an anonymous row of dots and never reads as
   *  being about some other post. */
  target?: string;
  /** How this card sums up once it is folded to one line ("in X", "better to wait"). */
  summary?: string;
  /** The draft to offer again when a refusal is resolved ("Draft the reply to @x"). */
  resume?: ResumeTarget;
  /** Called once when the op settles, for a card that started it (the verdict's "Write a reply anyway"). */
  onSettled?: (phase: OpPhase) => void;
  /** The 1 s timer that keeps "12s" on a waiting card honest. */
  ticker?: number;
  /** Streamed words shown while an Enhance or Create is being written (display only). */
  preview?: string;
}

const opBindings = new Map<string, OpBinding>();

function opBindingFor(opId: string | undefined): OpBinding | undefined {
  return opId ? opBindings.get(opId) : undefined;
}

function beginOp(kind: OpKind, phase: OpPhase, card: HTMLElement, defaultLabel: string,
                 options: Partial<Pick<OpBinding, "renderTerminal" | "onCancel" | "target" | "resume">> = {}): GenerationOp {
  const op = createOp(kind, phase);
  card.dataset.opId = op.id;
  const binding: OpBinding = { op, card, defaultLabel, timers: [], ...options };
  opBindings.set(op.id, binding);
  renderOpCard(binding);
  return op;
}

/** Re-point a live op's Cancel (and what it says it is acting on) and repaint,
 *  so ownership handoffs — launcher → composer session — never leave the card
 *  offering a Cancel that stops the wrong thing, or none at all. */
function setOpControls(opId: string | undefined, controls: Pick<OpBinding, "onCancel" | "target">) {
  const binding = opId ? opBindings.get(opId) : undefined;
  if (!binding || isTerminal(binding.op)) return;
  binding.onCancel = controls.onCancel;
  if (controls.target) binding.target = controls.target;
  renderOpCard(binding);
}

function stopElapsedTicker(binding: OpBinding) {
  if (binding.ticker != null) window.clearInterval(binding.ticker);
  binding.ticker = undefined;
}

function applyOpEvent(opId: string, event: OpEvent): GenerationOp | undefined {
  const binding = opBindings.get(opId);
  if (!binding) return undefined;
  const next = transitionOp(binding.op, event);
  if (next === binding.op) return next; // absorbed (stale timer/late chunk) — no render
  binding.op = next;
  if (isTerminal(next)) {
    for (const timer of binding.timers) clearTimeout(timer);
    binding.timers.length = 0;
    stopElapsedTicker(binding);
  }
  renderOpCard(binding);
  if (isTerminal(next) && binding.onSettled) {
    const settled = binding.onSettled;
    binding.onSettled = undefined;
    try { settled(next.phase); } catch { /* the card that asked is gone */ }
  }
  // The "Replying to @x" chip at the top is only for while something is being written: once
  // nothing is, it would label the whole feed (a refusal or an error never cleared it).
  if (isTerminal(next) && ![...opBindings.values()].some(other => !isTerminal(other.op))) clearDockContext();
  return next;
}

/** Heartbeats cover a silence with something true. A function note is read when it fires. */
function armOpHeartbeats(opId: string, beats: Array<[number, string | (() => string)]>) {
  const binding = opBindings.get(opId);
  if (!binding || isTerminal(binding.op)) return;
  for (const [delay, note] of beats) {
    binding.timers.push(window.setTimeout(() => {
      applyOpEvent(opId, { type: "heartbeat", note: typeof note === "function" ? note() : note });
    }, delay));
  }
}

/** The words of a draft as they stream in, under the dots. Only the checked final draft is ever
 *  typed into X; whatever the card ends with (the result, or why nothing was written) replaces this. */
function setOpPreview(opId: string, text: string) {
  const binding = opBindings.get(opId);
  if (!binding || isTerminal(binding.op) || !text.trim()) return;
  binding.preview = text;
  let node = binding.card.querySelector<HTMLElement>(":scope > .op-preview");
  if (!node) {
    node = document.createElement("p");
    node.className = "op-preview";
    const foot = binding.card.querySelector(":scope > .op-foot");
    if (foot) foot.before(node);
    else binding.card.append(node);
  }
  node.textContent = text;
  scrollFeed();
}

/** The trail of what this operation has done, with the live step last.
 *  Only the most recent steps are kept on screen: a long local generation can
 *  report a dozen substages, and a card that grows without limit stops being a
 *  status and becomes a wall. */

function renderOpLog(allDone: string[], live: string): HTMLElement {
  // ONE BUBBLE: three dots and the line that is true right now. The checklist
  // version listed every step it had passed ("Writing your post ✓ / Sending it
  // to Starling ✓ / Connecting ✓ / Thinking ✓"), which grew the card
  // taller with each heartbeat, pushed the conversation up, and told the user
  // nothing they needed — they asked for the state, not the transcript.
  // `allDone` is deliberately ignored rather than removed: the op still tracks
  // its history for logs and for the terminal card.
  void allDone;
  const log = document.createElement("div");
  log.className = "op-log";
  const row = document.createElement("div");
  row.className = "op-step live";
  const mark = document.createElement("span");
  mark.className = "op-mark";
  mark.innerHTML = `<span class="typing"><i></i><i></i><i></i></span>`;
  const text = document.createElement("span");
  text.className = "op-text writing-label";
  text.textContent = live;
  row.append(mark, text);
  log.append(row);
  return log;
}

/**
 * One line under the dots: how long it has been, and Cancel. The seconds are hidden from screen
 * readers (a number changing every second is not news), and Cancel is a real button.
 */
function renderOpFoot(binding: OpBinding): HTMLElement {
  const foot = document.createElement("div");
  foot.className = "op-foot";
  const elapsed = document.createElement("span");
  elapsed.className = "op-elapsed";
  elapsed.setAttribute("aria-hidden", "true");
  elapsed.textContent = formatElapsed(Date.now() - binding.op.startedAt);
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.className = "op-cancel";
  cancel.textContent = "Cancel";
  cancel.addEventListener("click", () => binding.onCancel?.());
  foot.append(elapsed, cancel);
  if (binding.ticker == null) {
    binding.ticker = window.setInterval(() => {
      const live = binding.card.querySelector<HTMLElement>(":scope > .op-foot .op-elapsed");
      if (!binding.card.isConnected || isTerminal(binding.op)) {
        stopElapsedTicker(binding);
        return;
      }
      if (live) live.textContent = formatElapsed(Date.now() - binding.op.startedAt);
    }, 1_000);
  }
  return foot;
}

/** The card's own one-line header: "↩ Reply to @adeveloper". Kept first through every re-render. */
function ensureCardHeader(binding: OpBinding) {
  const card = binding.card;
  if (!binding.target || binding.op.kind === "chat" || !card.isConnected) return;
  let head = card.querySelector<HTMLElement>(":scope > .card-target");
  if (!head) {
    head = document.createElement("div");
    head.className = "card-target";
  }
  if (card.firstElementChild !== head) card.prepend(head);
  const action = binding.op.kind === "decide" ? undefined : actionFromTarget(binding.target);
  const collapsed = card.classList.contains("collapsed");
  const summary = collapsed ? ` · ${binding.summary ?? (binding.op.phase === "failed" ? "not drafted" : "done")}` : "";
  head.textContent = `${cardIcon(binding.op.kind === "decide" ? "decide" : "enhance", action)} ${binding.target}${summary}`;
  head.setAttribute("role", collapsed ? "button" : "presentation");
  head.tabIndex = collapsed ? 0 : -1;
  if (collapsed) head.setAttribute("aria-expanded", "false");
  else head.removeAttribute("aria-expanded");
}

function actionFromTarget(target: string): ComposerAction {
  if (/quote/i.test(target)) return "quote";
  if (/^new post$/i.test(target)) return "post";
  return "reply";
}

/**
 * A new ✦ starts: the finished cards above it fold to their header line, "↩ Reply to @x · in X", so
 * the notes of an older post can never be read as advice about the new one. A folded card opens
 * again when its header is pressed.
 */
function collapseFinishedCards() {
  // The welcome card has done its job once the first ✦ is pressed.
  chatShadow?.querySelectorAll<HTMLElement>(".feed .card.first-run").forEach(card => card.remove());
  for (const binding of opBindings.values()) {
    if (binding.op.kind === "chat" || !binding.target || !isTerminal(binding.op)) continue;
    const card = binding.card;
    if (!card.isConnected || card.classList.contains("collapsed") || card.dataset.accountRefusal) continue;
    card.classList.add("collapsed");
    ensureCardHeader(binding);
    const head = card.querySelector<HTMLElement>(":scope > .card-target");
    if (head && !head.dataset.wired) {
      head.dataset.wired = "true";
      const expand = () => {
        if (!card.classList.contains("collapsed")) return;
        card.classList.remove("collapsed");
        ensureCardHeader(binding);
      };
      head.addEventListener("click", expand);
      head.addEventListener("keydown", event => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          expand();
        }
      });
    }
  }
}

function renderOpCard(binding: OpBinding) {
  const card = binding.card;
  try {
    const view = cardViewFor(binding.op, binding.defaultLabel);
    if (view.kind === "remove") {
      stopElapsedTicker(binding);
      card.remove();
      opBindings.delete(binding.op.id);
      return;
    }
    const waiting = view.kind === "loading" || view.kind === "stream-text";
    card.classList.toggle("loading", view.kind === "loading");
    // Busy while it waits: a screen reader announces the result, not every
    // label change and streamed word on the way to it.
    if (waiting) card.setAttribute("aria-busy", "true");
    else card.removeAttribute("aria-busy");
    if (view.kind === "loading") {
      // The dots and the line that is true right now; under them, how long it
      // has been and Cancel. A ✦ has no pill of its own, so the card is the one
      // place a slow draft can be stopped.
      card.replaceChildren(renderOpLog(view.done, view.label));
      if (binding.preview) {
        const preview = document.createElement("p");
        preview.className = "op-preview";
        preview.textContent = binding.preview;
        card.append(preview);
      }
      if (binding.onCancel) card.append(renderOpFoot(binding));
    } else if (view.kind === "stream-text") {
      // The chat streaming tick owns card.textContent; the transition itself
      // renders nothing so tokens are never clobbered mid-stream.
    } else if (binding.renderTerminal) {
      binding.renderTerminal(card, binding.op);
    } else if (view.kind === "result-stopped") {
      const status = document.createElement("small");
      status.className = "status";
      status.textContent = "Stopped.";
      card.append(status);
    } else if (view.kind === "error") {
      const status = document.createElement("small");
      status.className = "status error";
      status.textContent = view.message;
      card.replaceChildren(status);
    }
  } catch {
    // Rendering must never strand a live request. If a future card renderer
    // misbehaves, retain this card with an honest recovery message instead of
    // letting the stream callback abort before its state is cleaned up.
    card.classList.remove("loading");
    card.removeAttribute("aria-busy");
    const status = document.createElement("small");
    status.className = "status error";
    status.textContent = "Starling could not display that result. It did not make any further changes to X.";
    card.replaceChildren(status);
  } finally {
    ensureCardHeader(binding);
    scrollFeed();
  }
}

/**
 * Undo, on the card of a draft Starling typed into X: puts the customer's own text back (for Create,
 * an empty box), but only while the box still holds exactly what Starling typed. Once they have
 * edited, their edit wins and the card says so.
 */
function offerUndo(card: HTMLElement, session: ComposerSession) {
  const undo = session.undo;
  if (!undo || undo.restored) return;
  addChip(card, "Undo", async chip => {
    const busy = session.control.dataset.state === "sending" || session.control.dataset.state === "typing";
    if (busy) {
      setCardActionStatus(card, "Starling is writing in that box right now. Undo once it's done.");
      return;
    }
    if (session.undo !== undo) {
      chip.disabled = true;
      setCardActionStatus(card, "A newer draft replaced this one. Its own card has the Undo.");
      return;
    }
    const current = session.disposed || !session.composer.isConnected ? null : readableComposerText(session.composer);
    const decision = undoDecision(current, undo);
    if (decision === "gone") {
      chip.disabled = true;
      setCardActionStatus(card, "X closed that box, so there is nothing to undo.");
      return;
    }
    if (decision === "edited") {
      chip.disabled = true;
      setCardActionStatus(card, "You've edited it since, so Starling left it alone.");
      return;
    }
    writeComposerText(session, undo.original);
    await nextFrame();
    undo.restored = true;
    chip.textContent = "Restored";
    chip.disabled = true;
    if (session.generationId) {
      session.lastRecordedText = undo.original;
      recordComposerOutcome(session, {
        generation_id: session.generationId,
        candidate_id: session.candidateId,
        // The customer took the draft back: their own words (an edit), or nothing at all (for Create).
        status: undo.original.trim() ? "edited" : "abandoned",
        ...(undo.original.trim() ? { final_text: undo.original } : {}),
        metadata: { source: "starling_undo", action: session.action },
      });
    }
    session.control.dataset.state = "idle";
    updateComposerControl(session);
  });
}

function showEnhancementResult(session: ComposerSession, card: HTMLElement, response: AssistResponse,
                               inserted: boolean, note?: string, changed = true) {
  const decision = response.decision!;
  // A rights-cleared, on-topic image is attached STRAIGHT into X's composer —
  // no card, no click — so it appears in the post as the draft is written, the
  // same way the text is typed in. The user still reviews and posts it manually;
  // §8.3i's guarantee that nothing is auto-POSTED is untouched.
  // Status-only card: the draft lives in X's composer box, not here. On success
  // we just say so; only if it could NOT be typed in do we offer the draft.
  card.innerHTML = "";
  if (decision.already_replied) {
    // The service found this account's reply to the post (it may be off screen). Advice, not a wall.
    renderAlreadyReplied(card, () => { void enhanceComposer(session, { secondReplyOk: true }); });
    scrollFeed();
    return;
  }
  const line = document.createElement("div");
  line.className = `deciding ${inserted ? "" : "error"}`.trim();
  line.textContent = inserted
    ? (changed ? `Typed into your ${actionLabel(session.action)}.` : "Your draft already works. Starling left it as is.")
    : (note || "Your original draft is untouched in X.");
  card.append(line);
  // A created post: which story it is about, before the reasons.
  const topic = session.action === "post" ? topicLine(decision.topic) : null;
  if (topic) appendTopicLine(card, decision);
  // THE CARD USED TO END HERE, at one hardcoded sentence, while the engine
  // sent rationale / reasons / needs_review / review_note for the same request.
  // `already_replied` already puts its own explanation in `line`, so adding the
  // rationale under it would say the same thing twice.
  if (!decision.already_replied) appendDraftReason(card, decision);
  // Take it back: Enhance replaced the customer's own line, Create filled an empty box.
  if (inserted && changed) offerUndo(card, session);
  // Situational intelligence lines from the engine: what the draft is
  // grounded in, whether now is a good moment, and the radar's next move.
  // Display-only — posting stays entirely in the user's hands.
  const extra = decision as unknown as Record<string, unknown>;
  // User-requested thread: part 1 is already in the composer; the remaining
  // parts get per-part Copy chips. X's "+" button stays a manual, human step.
  const threadParts = Array.isArray(extra.thread_parts) ? (extra.thread_parts as string[]) : [];
  if (threadParts.length >= 2) {
    const plan = planThread(threadParts);
    const note = document.createElement("small");
    note.className = "status";
    card.append(note);
    if (plan.boxesToAdd > 0) {
      // Starling builds the whole thread: it presses X's "+" and writes each
      // part. Filling boxes is the same thing it already does for part 1 —
      // the Post button stays the human's, as always.
      note.textContent = `Thread: ${plan.parts.length} parts ready.`;
      addChip(card, `Build all ${plan.parts.length} parts`, async chip => {
        chip.textContent = "Building…";
        const written = await buildThreadInComposer(plan.parts, line => {
          note.textContent = line;
        });
        chip.textContent = written >= plan.parts.length
          ? "Thread built" : `Stopped at part ${written}`;
        if (written < plan.parts.length) {
          setCardActionStatus(card, "I couldn't add every box — the remaining parts are "
            + "below, copy them in yourself.", true);
        }
      });
    } else {
      note.textContent = `Thread: part 1 is in the composer. ${plan.skipped ?? ""} `
        + `Press X's + to add each next part (${threadParts.length} total).`;
    }
    // The copy chips stay regardless: a fallback that always works.
    threadParts.slice(1).forEach((part, index) => {
      addChip(card, `Copy part ${index + 2}`, async chip => {
        const copied = await navigator.clipboard.writeText(part).then(() => true, () => false);
        if (copied) {
          chip.textContent = `Part ${index + 2} copied`;
          return;
        }
        setCardActionStatus(card, `Couldn't copy part ${index + 2}. Select it in the composer and copy it manually.`, true);
      });
    });
  }
  for (const key of ["stats_line", "timing_line", "plan_hint"] as const) {
    const value = extra[key];
    // "Based on: {the same story} (…)" says again what the About line just said.
    if (key === "stats_line" && topic && typeof value === "string" && typeof decision.topic === "string"
      && value.includes(decision.topic.trim())) continue;
    if (typeof value === "string" && value) {
      const info = document.createElement("small");
      info.className = "status";
      info.textContent = value;
      card.append(info);
    }
  }
  if (!inserted && decision.draft) {
    offerDraftCopy(card, decision.draft, session.action, "Copy Starling's version");
  }
  if (response.visual?.preview_data_url) {
    renderVisualChoice(card, response.visual, response, session);
  }
  // A created post (never a reply) can be held for a better window. The draft is
  // in X's composer; scheduling stores a copy to resurface later — the user
  // still posts it. offer the Schedule button beside the composer status.
  if (decision.schedule) renderScheduleControls(card, decision as unknown as AssistDecision);
  scrollFeed();
}

/** Every visible compose box, in X's own order (tweetTextarea_0, _1, …). */
function visibleComposerBoxes(): HTMLElement[] {
  return Array.from(document.querySelectorAll<HTMLElement>('[data-testid^="tweetTextarea_"]'))
    .filter(box => box.isConnected && isVisible(box))
    .sort((a, b) => {
      const index = (element: HTMLElement) =>
        Number((element.dataset.testid || "").split("_").pop() || 0);
      return index(a) - index(b);
    });
}

/** X's "add another post" button, if the composer is showing one. */
function threadAddButton(): HTMLElement | null {
  return document.querySelector<HTMLElement>('[data-testid="addButton"]');
}

/**
 * Write a whole thread into X's composer: press "+", fill the new box, repeat.
 *
 * NEVER SUBMITS. Adding a box and typing into it is the same class of action
 * as writing part one, which Starling has always done; the Post button remains
 * the human's. Returns how many parts actually landed, so a partial build can
 * be reported honestly instead of claimed as success.
 */
async function buildThreadInComposer(
  parts: string[],
  onProgress?: (line: string) => void,
): Promise<number> {
  let written = 0;
  for (let index = 0; index < parts.length; index += 1) {
    onProgress?.(threadProgressLine(written, parts.length));
    if (index > 0) {
      const add = threadAddButton();
      if (!add) break;                       // no "+" — stop, report honestly
      add.click();
      // Wait for X to render the new box before writing, or part N lands in
      // part N-1's box and silently overwrites it.
      const target = index;
      const appeared = await waitFor(
        () => boxIndexForPart(target, visibleComposerBoxes().length) >= 0, 2_000);
      if (!appeared) break;
    }
    const boxes = visibleComposerBoxes();
    const box = boxes[boxIndexForPart(index, boxes.length)];
    if (!box) break;
    box.focus();
    const value = parts[index];
    const selection = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(box);
    selection?.removeAllRanges();
    selection?.addRange(range);
    const replaced = Boolean(selection) && !selection!.isCollapsed;
    if (!(replaced && document.execCommand("insertText", false, value))) {
      box.replaceChildren(document.createTextNode(value));
      box.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: value }));
    }
    written += 1;
    await nextFrame();
  }
  onProgress?.(threadProgressLine(written, parts.length));
  return written;
}

/**
 * Set X's OWN schedule on the composer: open the dialog, choose the time,
 * confirm the TIME. The composer is then fully prepared and the human presses
 * X's publish button — which this function never touches, by construction:
 * `mayClick` allows exactly two controls and neither of them publishes.
 *
 * Returns a human-readable failure, or null on success.
 */
async function setNativeSchedule(targetIso: string): Promise<string | null> {
  const plan = planSchedule(targetIso);
  if (!plan.fields) return plan.refused ?? "that time cannot be scheduled";

  const openButton = document.querySelector<HTMLElement>('[data-testid="scheduleOption"]');
  if (!openButton || !mayClick("scheduleOption")) {
    return "X's schedule button isn't in this composer";
  }
  openButton.click();

  const selectorFor = (name: string) => `select[aria-label*="${name}" i], select[name*="${name}" i]`;
  const ready = await waitFor(() => Boolean(document.querySelector(selectorFor("month"))), 3_000);
  if (!ready) return "X's schedule dialog didn't open";

  const setSelect = (name: string, value: string): boolean => {
    const select = document.querySelector<HTMLSelectElement>(selectorFor(name));
    if (!select) return false;
    const option = Array.from(select.options).find(
      candidate => candidate.value === value || candidate.text.trim() === value);
    if (!option) return false;
    select.value = option.value;
    select.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  };

  const fields = plan.fields;
  const wrote = [
    setSelect("month", fields.month), setSelect("day", fields.day),
    setSelect("year", fields.year), setSelect("hour", fields.hour),
    setSelect("minute", fields.minute), setSelect("m", fields.meridiem),
  ];
  if (wrote.filter(Boolean).length < 4) return "X's schedule fields look different than expected";

  // Confirms the CHOSEN TIME and returns to the composer. It does not publish:
  // the composer's own button still says "Schedule post" and is the human's.
  const confirm = document.querySelector<HTMLElement>('[data-testid="confirmationSheetConfirm"]');
  if (!confirm || !mayClick("confirmationSheetConfirm")) return "couldn't confirm the time";
  confirm.click();
  return null;
}

/** Poll a condition until it holds or the budget runs out. */
async function waitFor(condition: () => boolean, budgetMs: number): Promise<boolean> {
  const deadline = Date.now() + budgetMs;
  while (Date.now() < deadline) {
    if (condition()) return true;
    await new Promise(resolve => window.setTimeout(resolve, 60));
  }
  return condition();
}

function downloadText(filename: string, text: string): boolean {
  try {
    const blob = new Blob([text], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    anchor.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 5_000);
    return true;
  } catch {
    return false;
  }
}

// ---- attachments ----------------------------------------------------------

// Small monochrome glyphs reused by the cards and the user bubble.
const FILE_ICON_SVG = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 3v4a1 1 0 0 0 1 1h4"/><path d="M17 21H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h7l5 5v11a2 2 0 0 1-2 2z"/></svg>`;
const PAPERCLIP_SVG = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>`;

/** Read the picked files' text locally and show them as cards. */
async function handleAttachmentFiles(list: FileList | null) {
  if (!list || !list.length) return;
  let skipped = 0;
  for (const file of [...list]) {
    if (chatAttachments.length >= MAX_ATTACHMENTS) { skipped += 1; continue; }
    const name = file.name || "file";
    const attachment: FileAttachment = {
      id: ++chatAttachmentSeq, kind: "file", name, size: file.size, lines: 0, badge: fileBadge(name), text: "",
    };
    try {
      if (file.size > MAX_ATTACHMENT_BYTES) {
        attachment.error = `Too big — max ${formatBytes(MAX_ATTACHMENT_BYTES)}`;
      } else {
        const text = await readAttachmentText(file);
        if (text == null) attachment.error = "Not a readable text file";
        else { attachment.text = text; attachment.lines = text ? text.split(/\r\n|\r|\n/).length : 0; }
      }
    } catch {
      attachment.error = "Could not read this file";
    }
    chatAttachments.push(attachment);
  }
  renderAttachments();
  if (skipped > 0) {
    showDockNotice(`Starling holds up to ${MAX_ATTACHMENTS} attachments at once — ${skipped} more ${skipped === 1 ? "was" : "were"} skipped.`, false);
  }
}

/** Short uppercase format tag from a filename, e.g. "notes.md" → "MD". */
function fileBadge(name: string): string {
  const dot = name.lastIndexOf(".");
  const ext = dot > 0 ? name.slice(dot + 1) : "";
  return (ext || "txt").slice(0, 4).toUpperCase();
}

/** Attach the X post rendered in `article` to the Starling chat as a preview card. */
function attachPostToChat(article: HTMLElement) {
  if (!canScanPosts(bridgeState, pageGate())) return;
  const context = extractContext(article); // also refreshes activeContextAvatar
  const postId = context.post_id;
  // The clip is a toggle: clicking it again on an attached post detaches it.
  if (postId) {
    const existing = chatAttachments.find(a => a.kind === "post" && a.postId === postId);
    if (existing) {
      removeAttachment(existing.id);
      return;
    }
  }
  if (chatAttachments.length >= MAX_ATTACHMENTS) {
    showDockNotice(`Starling holds up to ${MAX_ATTACHMENTS} attachments at once. Remove one to add this post.`, false);
    return;
  }
  const attachment: PostAttachment = {
    id: ++chatAttachmentSeq, kind: "post",
    author: context.author,
    handle: context.author_handle,
    text: context.text,
    imageUrl: firstPostImageUrl(article),
    avatarUrl: activeContextAvatar,
    url: context.post_url,
    postId,
    context,
  };
  chatAttachments.push(attachment);
  openDock();
  renderAttachments();
  chatShadow?.querySelector<HTMLTextAreaElement>(".composer textarea")?.focus();
}

/** URL of the post's own first image (ignores quoted-post media), if any. */
function firstPostImageUrl(article: HTMLElement): string | undefined {
  const isOwn = (element: Element) =>
    element.closest(POST_SELECTOR) === article && !element.closest('[data-testid="quoteTweet"], [role="blockquote"]');
  const img = Array.from(article.querySelectorAll<HTMLImageElement>('[data-testid="tweetPhoto"] img[src]')).find(isOwn);
  return img?.src || undefined;
}

/** Decode a file as UTF-8 text; return null when it looks like binary. */
async function readAttachmentText(file: File): Promise<string | null> {
  const raw = await file.text();
  // NUL bytes or a high share of U+FFFD replacement characters mean the file
  // wasn't valid text — not something the model should try to read.
  if (raw.includes("\u0000")) return null;
  const sample = raw.slice(0, 4000);
  if (sample.length) {
    let bad = 0;
    for (const ch of sample) if (ch === "\uFFFD") bad += 1;
    if (bad / sample.length > 0.1) return null;
  }
  return raw;
}

function removeAttachment(id: number) {
  chatAttachments = chatAttachments.filter(attachment => attachment.id !== id);
  renderAttachments();
}

function clearAttachments() {
  if (!chatAttachments.length) return;
  chatAttachments = [];
  renderAttachments();
}

// Ids already on screen — only genuinely new cards get the entrance animation,
// so removing one card never makes its neighbours re-pop.
let renderedAttachmentIds = new Set<number>();

/** Repaint the attachment cards and reflect state on the "+" and input. */
function renderAttachments() {
  const row = chatShadow?.querySelector<HTMLElement>(".attachments");
  const zone = chatShadow?.querySelector<HTMLElement>(".att-zone");
  if (!row || !zone) return;
  const attachButton = chatShadow?.querySelector<HTMLButtonElement>(".attach");
  const textarea = chatShadow?.querySelector<HTMLTextAreaElement>(".composer textarea");
  row.textContent = "";
  if (!chatAttachments.length) {
    // Collapsing the zone animates the input dock shrinking back to one line.
    zone.classList.remove("open");
    renderedAttachmentIds = new Set();
    attachButton?.removeAttribute("data-active");
    if (textarea) textarea.placeholder = "Ask Starling…";
    return;
  }
  attachButton?.setAttribute("data-active", "");
  for (const attachment of chatAttachments) {
    const card = attachment.kind === "post" ? renderPostCard(attachment) : renderFileCard(attachment);
    if (!renderedAttachmentIds.has(attachment.id)) card.classList.add("att-new");
    row.append(card);
  }
  renderedAttachmentIds = new Set(chatAttachments.map(attachment => attachment.id));
  // The input dock itself grows: the zone's grid row expands from 0fr to 1fr,
  // so the cards live INSIDE the same rounded shell as the text input.
  zone.classList.add("open");
  const hasPost = chatAttachments.some(a => a.kind === "post");
  const hasFile = chatAttachments.some(a => a.kind === "file" && !a.error && Boolean(a.text.trim()));
  // Short placeholders only: a long one ellipsizes in the narrow input and
  // reads as broken.
  if (textarea) {
    textarea.placeholder = hasPost ? "Ask about this post" : hasFile ? "Add a note, or just send" : "Ask Starling…";
  }
}

/** The rounded card + floating remove button both attachment kinds share. */
function attachmentCardShell(id: number, removeLabel: string): HTMLElement {
  const card = document.createElement("div");
  card.className = "attachment";
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "att-remove";
  remove.dataset.id = String(id);
  remove.setAttribute("aria-label", removeLabel);
  remove.textContent = "×";
  card.append(remove);
  return card;
}

function renderFileCard(att: FileAttachment): HTMLElement {
  const card = attachmentCardShell(att.id, `Remove ${att.name}`);
  card.classList.add("file");
  if (att.error) card.dataset.error = "";
  tip(card, att.error ? `${att.name} — ${att.error}` : att.name);

  const name = document.createElement("div");
  name.className = "att-name";
  name.textContent = att.name;

  const meta = document.createElement("div");
  meta.className = "att-meta";
  meta.textContent = att.error
    ? att.error
    : att.lines ? `${att.lines.toLocaleString()} line${att.lines === 1 ? "" : "s"}` : formatBytes(att.size);

  const badge = document.createElement("span");
  badge.className = "att-badge";
  badge.textContent = att.error ? "!" : att.badge;

  card.append(name, meta, badge);
  return card;
}

function renderPostCard(att: PostAttachment): HTMLElement {
  const card = attachmentCardShell(att.id, `Remove attached post by ${att.handle || att.author}`);
  card.classList.add("post");
  tip(card, att.text ? `${att.handle || att.author}: ${att.text}` : (att.handle || att.author));

  // Reference layout: text column on the left, rounded image thumb on the right.
  const main = document.createElement("div");
  main.className = "att-post-main";

  const head = document.createElement("div");
  head.className = "att-post-head";
  if (att.avatarUrl) {
    const avatar = document.createElement("img");
    avatar.className = "att-avatar";
    avatar.src = att.avatarUrl;
    avatar.alt = "";
    avatar.addEventListener("error", () => avatar.remove());
    head.append(avatar);
  }
  const who = document.createElement("span");
  who.className = "att-who";
  who.textContent = att.handle || att.author || "X post";
  head.append(who);

  const body = document.createElement("div");
  body.className = "att-post-text";
  body.textContent = att.text || "(no text — media only)";

  const badge = document.createElement("span");
  badge.className = "att-badge post-badge";
  badge.textContent = "𝕏 Post";

  main.append(head, body, badge);
  card.append(main);

  if (att.imageUrl) {
    const thumb = document.createElement("div");
    thumb.className = "att-thumb";
    const img = document.createElement("img");
    img.src = att.imageUrl;
    img.alt = "";
    img.addEventListener("error", () => thumb.remove());
    thumb.append(img);
    card.append(thumb);
  }
  return card;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(bytes < 10 * 1024 ? 1 : 0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/**
 * Weave attached files and X posts into the outgoing chat message so Starling
 * can read them and draft a post. Returns the message that is sent; the
 * user's chat bubble shows only their note plus compact chips, never the dump.
 */
function buildChatPayload(instruction: string): { message: string; hadFiles: boolean; hadPosts: boolean } {
  const files = chatAttachments.filter((a): a is FileAttachment => a.kind === "file" && !a.error && Boolean(a.text.trim()));
  const posts = chatAttachments.filter((a): a is PostAttachment => a.kind === "post");
  if (!files.length && !posts.length) return { message: instruction, hadFiles: false, hadPosts: false };

  const ask = instruction || (posts.length
    ? "Looking at the attached X post, should we post something about this? If so, what could we say?"
    : "Read the attached file(s) and write an X post based on them.");
  const sections: string[] = [];

  if (posts.length) {
    const lines = posts.map(post => {
      const who = [post.author, post.handle].filter(Boolean).join(" ");
      const link = post.url ? `\n${post.url}` : "";
      return `• ${who || "X post"}: "${post.text || "(no text — media only)"}"${post.imageUrl ? " [has image]" : ""}${link}`;
    });
    sections.push(`Attached X post${posts.length === 1 ? "" : "s"} to discuss:\n${lines.join("\n")}`);
  }

  if (files.length) {
    const blocks = files.map(file => `--- Attached file: ${file.name} (${formatBytes(file.size)}) ---\n${file.text.trim()}`);
    sections.push(`Attached file${files.length === 1 ? "" : "s"} to read as source material:\n\n${blocks.join("\n\n")}`);
  }

  return { message: fitChatMessage(ask, sections.join("\n\n")), hadFiles: files.length > 0, hadPosts: posts.length > 0 };
}

/** The service accepts chat messages up to CHAT_MESSAGE_MAX_CHARS: attachments are cut to fit, with a note. */
function fitChatMessage(ask: string, attached: string): string {
  const full = `${ask}\n\n${attached}`;
  if (full.length <= CHAT_MESSAGE_MAX_CHARS) return full;
  const note = "\n…\n(Some attached content was cut to fit.)";
  const head = `${ask.slice(0, CHAT_MESSAGE_MAX_CHARS)}\n\n`;
  const room = CHAT_MESSAGE_MAX_CHARS - head.length - note.length;
  return room > 0 ? `${head}${attached.slice(0, room)}${note}` : ask.slice(0, CHAT_MESSAGE_MAX_CHARS);
}

interface BubbleAtt { kind: "file" | "post"; label: string; }

/** User bubble that also lists compact chips for whatever was attached. */
function addUserBubble(text: string, atts: BubbleAtt[]): HTMLElement {
  const card = addBubble("user", text);
  for (const att of atts) {
    const row = document.createElement("span");
    row.className = "bubble-atts";
    if (att.kind === "post") {
      const mark = document.createElement("span");
      mark.className = "bubble-atts-mark";
      mark.textContent = "𝕏";
      row.append(mark);
    } else {
      row.innerHTML = FILE_ICON_SVG;
    }
    const label = document.createElement("span");
    label.className = "bubble-atts-label";
    label.textContent = att.label;
    row.append(label);
    card.append(row);
  }
  return card;
}

// ---- chat -----------------------------------------------------------------

async function sendChatMessage(textarea: HTMLTextAreaElement) {
  if (activeChatStream) return;
  // Consent, sign-in, connection: the dock's card says what is needed; the
  // typed message stays in the box.
  if (showBlockingState()) return;
  const instruction = textarea.value.trim();
  const files = chatAttachments.filter((a): a is FileAttachment => a.kind === "file" && !a.error && Boolean(a.text.trim()));
  const posts = chatAttachments.filter((a): a is PostAttachment => a.kind === "post");
  // Nothing to act on: no note, no readable file, no attached post.
  if (!instruction && !files.length && !posts.length) return;

  const { message, hadFiles, hadPosts } = buildChatPayload(instruction);
  // Hand the engine the primary attached post as structured context.
  if (posts.length) {
    activeContext = posts[0].context;
    activeContextAvatar = posts[0].avatarUrl;
    if (posts[0].context && posts[0].avatarUrl) contextAvatars.set(posts[0].context, posts[0].avatarUrl);
  }

  const atts: BubbleAtt[] = [
    ...posts.map((post): BubbleAtt => ({ kind: "post", label: post.handle || post.author || "X post" })),
    ...files.map((file): BubbleAtt => ({ kind: "file", label: file.name })),
  ];
  const displayText = instruction
    || (hadPosts ? "Should we post something about this?" : hadFiles ? "Draft a post from the attached file(s)." : "");
  textarea.value = "";
  syncChatBox(textarea);
  clearAttachments();
  runChatTurn(textarea, message, displayText, atts);
}

function runChatTurn(textarea: HTMLTextAreaElement, message: string, displayText: string, atts: BubbleAtt[]) {
  if (activeChatStream) return;
  // Sweep cancelled leftovers and orphaned loading cards BEFORE anything is
  // measured — stale status must not sit above the new turn or pad the math.
  pruneFeedForNewTurn();
  const userBubble = addUserBubble(displayText, atts);
  // A request keeps the post it started with. The page can keep changing while
  // Starling thinks; a late reply must never follow the newer context.
  const requestContext = activeContext;
  const card = addCard("bubble assistant streaming-card", "chat");
  const op = beginOp("chat", "generating", card, "Thinking through that…", {
    // Same op, same Cancel: the dock's Stop button and this card's Cancel both
    // end in the stream's cancel, so stopping from either place settles both.
    onCancel: () => activeChatStream?.cancel(),
  });
  // Chat anchor: float this new turn to the top so it reads like a fresh
  // chat; the streaming card is observed so the bottom slack shrinks in step
  // as the answer grows, without ever moving the pinned position.
  anchorFeedTo(userBubble);
  const send = chatShadow?.querySelector<HTMLButtonElement>(".send");
  if (send) { send.dataset.mode = "stop"; send.setAttribute("aria-label", "Stop"); }

  let chatSettled = false;
  const finishSend = () => {
    chatSettled = true;
    activeChatStream = null;
    if (send) { send.dataset.mode = "send"; send.setAttribute("aria-label", "Send"); }
  };

  const stream = startAssist({ mode: "chat", message, context: requestContext, conversation_id: chatConversationId, composer: openComposerPayload() }, {
    // Stopped with part of the answer on the card: the card keeps it, and says what that cost.
    onStopped: outcome => showStopCost(card, outcome),
    onUpdate: state => {
      const binding = opBindings.get(op.id);
      if (state.phase === "starting" || state.phase === "waiting") {
        applyOpEvent(op.id, { type: "heartbeat", note: streamStageLabel(state.stage) });
        scrollFeed();
      } else if (state.phase === "streaming") {
        applyOpEvent(op.id, { type: "tokens" });
        card.textContent = state.preview;
        scrollFeed();
      } else if (state.phase === "error") {
        if (state.cancelled) {
          // Stopped by the user. With streamed text the card keeps it plus a
          // quiet "Stopped." note; with none the card is removed and the
          // typed text returns to the box — unless they already typed anew.
          const hadContent = binding?.op.hasContent ?? false;
          if (binding) binding.renderTerminal = undefined;
          applyOpEvent(op.id, { type: "cancelled" });
          if (!hadContent && !textarea.value && displayText) {
            textarea.value = displayText;
            syncChatBox(textarea);
          }
        } else {
          const errorText = state.error || "Starling isn't available right now.";
          const retryable = state.retryable;
          const failure = isErrorCode(state.code) ? requestFailureFromStream(state) : null;
          const retry = () => {
            opBindings.delete(op.id);
            card.remove();
            runChatTurn(textarea, message, displayText, atts);
          };
          if (binding) binding.renderTerminal = cardEl => {
            cardEl.classList.add("errored");
            if (failure) {
              renderRequestFailure(cardEl, failure, retryable ? retry : undefined);
              return;
            }
            cardEl.textContent = errorText;
            if (retryable) addChip(cardEl, "Retry", retry);
          };
          applyOpEvent(op.id, { type: "failed", error: errorText });
        }
        finishSend();
      } else if (state.phase === "done" && state.response) {
        // Remember the conversation so the next message has context.
        const response = state.response;
        const cid = (response as unknown as Record<string, unknown>).conversation_id;
        if (typeof cid === "string") chatConversationId = cid;
        if (binding) binding.renderTerminal = cardEl => renderChatResult(cardEl, response, requestContext);
        applyOpEvent(op.id, { type: "completed", hasContent: true });
        finishSend();
      }
    }
  });
  // If the port threw at send time, the error handler above already ran
  // (synchronously) — storing the dead stream then would leave activeChatStream
  // stuck non-null forever, and every later chat submit would silently no-op.
  if (!chatSettled) activeChatStream = stream;
}

/**
 * The visual receipt (spec §8.3i): why this image, where it came from, what
 * rights it carries, and editable alt text — then the user decides.
 *
 * Nothing is attached automatically. X's own media-upload controls are never
 * clicked; `attachToComposer` pastes a DataTransfer, so `x-action-guards` keeps
 * `mediaUpload`/`addMedia` forbidden.
 */
/**
 * Attach a rights-cleared, on-topic image straight into the open X composer, the
 * same way the draft text is typed in — so it just appears in the post, no card
 * and no click. The user still reviews and posts manually; nothing is
 * auto-posted. If the composer isn't open we say so rather than dropping it
 * silently. For an attribution image, the credit line is surfaced so the user
 * can add it before posting.
 */
function recordImageChoice(response: AssistResponse, receipt: VisualReceipt,
                           choice: "used" | "skipped" | "replaced") {
  if (!response.generation_id) return;
  void sendToWorker({
    kind: "starling:image-feedback",
    payload: {
      generation_id: response.generation_id,
      candidate_id: response.candidate_id,
      choice,
      metadata: {
        visual_job: receipt.post_fit?.visual_job ?? null,
        semantic_score: receipt.post_fit?.analyzer_fit?.score ?? null,
        rights: receipt.source?.rights ?? "unknown",
        source: receipt.source?.url ?? null,
      },
    },
  }).catch(() => undefined);
}

function renderVisualChoice(card: HTMLElement, receipt: VisualReceipt,
                            response: AssistResponse, session: ComposerSession) {
  const choice = document.createElement("section");
  choice.className = "visual-choice image-card";
  const heading = document.createElement("div");
  heading.className = "img-head";
  heading.textContent = "Image match";
  choice.append(heading);
  if (receipt.preview_data_url && isLocalImageCopy(receipt.preview_data_url)) {
    const frame = document.createElement("div");
    frame.className = "img-frame";
    const image = document.createElement("img");
    image.src = receipt.preview_data_url;
    image.alt = receipt.alt_text || "Suggested image";
    frame.append(image);
    choice.append(frame);
  }
  const why = document.createElement("p");
  why.className = "img-why";
  why.textContent = receipt.post_fit?.visual_job || receipt.why_this_image || "This image supports the post.";
  choice.append(why);
  const source = document.createElement("small");
  source.className = "status img-meta";
  const who = receipt.source?.creator || receipt.source?.publisher || "Source recorded";
  source.textContent = `${who} · ${receipt.source?.rights || "rights unknown"}`;
  choice.append(source);
  if (receipt.post_fit?.visual_habit?.image_forward) {
    const habit = document.createElement("small");
    habit.className = "status img-meta";
    habit.textContent = "Shown because it fits your own recent image use.";
    choice.append(habit);
  }
  const alt = document.createElement("textarea");
  alt.className = "alt-input";
  alt.rows = 2;
  alt.value = receipt.alt_text || "";
  alt.setAttribute("aria-label", "Image description");
  choice.append(alt);
  const actions = document.createElement("div");
  actions.className = "chips";
  const use = document.createElement("button");
  use.type = "button";
  use.className = "chip";
  use.textContent = "Use image";
  const skip = document.createElement("button");
  skip.type = "button";
  skip.className = "chip";
  skip.textContent = "Skip image";
  actions.append(use, skip);
  choice.append(actions);
  use.addEventListener("click", () => {
    use.disabled = true;
    skip.disabled = true;
    receipt.alt_text = alt.value.trim();
    void autoAttachImage(receipt, choice, session.composer).then(used => {
      if (used) recordImageChoice(response, receipt, "used");
      else { use.disabled = false; skip.disabled = false; }
    });
  });
  skip.addEventListener("click", () => {
    use.disabled = true;
    skip.disabled = true;
    recordImageChoice(response, receipt, "skipped");
    const note = document.createElement("small");
    note.className = "status";
    note.textContent = "Skipped. Future image suggestions will adapt to your choices.";
    choice.append(note);
  });
  card.append(choice);
}

async function autoAttachImage(receipt: VisualReceipt, statusCard: HTMLElement,
                               composer?: HTMLElement): Promise<boolean> {
  const note = document.createElement("small");
  note.className = "status";
  // The live composer editable (same resolution the manual attach used). Fall
  // back to the session's composer if the selector misses.
  const editable = document.querySelector<HTMLElement>(COMPOSER_SELECTOR)
    ?? (composer && composer.isConnected ? composer : null);
  if (!editable || !receipt.preview_data_url || !isLocalImageCopy(receipt.preview_data_url)) {
    note.textContent = "Found a matching image, but your X composer isn't open to add it to.";
    statusCard.append(note);
    return false;
  }
  let result: Awaited<ReturnType<typeof attachToComposer>>;
  try {
    result = await attachToComposer(
      {
        dataUrl: receipt.preview_data_url,
        mimeType: receipt.mime_type ?? "image/png",
        fileName: receipt.file_name ?? "image.png",
        rights: (receipt.source?.rights ?? "unknown") as RightsState,
        altText: receipt.alt_text || "",
        sourceUrl: receipt.source?.url ?? null,
        creator: receipt.source?.creator ?? null,
        requiredCredit: receipt.source?.required_credit ?? null
      },
      { editable }
    );
  } catch {
    note.className = "status error";
    note.textContent = "Couldn't add that image. Your post text is still unchanged and ready to review.";
    statusCard.append(note);
    return false;
  }
  if (result.ok) {
    const credit = receipt.source?.required_credit
      ? ` If you keep it, credit: ${receipt.source.required_credit}.`
      : "";
    note.textContent = `Added a matching image to your post — review it in X (remove it there if you'd rather not).${credit}`;
    statusCard.append(note);
    return true;
  } else {
    note.textContent = result.reason ?? "Couldn't add the image to your composer.";
    statusCard.append(note);
    return false;
  }
}

/** Find the on-page post whose permalink matches this id, so a chat reply can
 * open THAT post's reply box. Returns null if it's not currently rendered. */
function findArticleByPostId(postId?: string): HTMLElement | null {
  if (!postId) return null;
  for (const article of Array.from(document.querySelectorAll<HTMLElement>(POST_SELECTOR))) {
    const href = findPermalink(article)?.href;
    if (href && postIdFromUrl(href) === postId) return article;
  }
  return null;
}

/**
 * The chat actually DOES the write for a reply/quote: locate the post on the
 * page, open its native reply/quote box, and type the draft in — the same
 * open-and-type path the ✦ pill uses. Opening a box is composing, not
 * publishing (the user still posts), so this stays inside the never-auto-post
 * guarantee. If the post isn't on-screen, it shows the draft to copy instead.
 */
async function chatActOnReply(card: HTMLElement, decision: AssistDecision,
                               context?: PostContext, existingStatus?: HTMLElement,
                               generationId?: string, candidateId?: string,
                               options: { allowSecondReply?: boolean } = {}): Promise<boolean> {
  const action = (decision.native_intent ?? decision.action) as ComposerAction;
  const draft = decision.draft || "";
  const article = findArticleByPostId(context?.post_id ?? postIdFromUrl(context?.post_url));
  const status = existingStatus ?? document.createElement("small");
  if (!existingStatus) {
    status.className = "status";
    card.append(status);
  }
  if (action !== "reply" && action !== "quote") {
    status.className = "status error";
    status.textContent = "Starling received an unsupported composer action, so it left X unchanged.";
    offerDraftCopy(card, draft, "reply");
    return false;
  }

  if (!article) {
    status.textContent = `Here's your ${actionLabel(action)}. Scroll to the post and press Put it in X, or copy it.`;
    offerDraftCopy(card, draft, action);
    return false;
  }
  status.textContent = `Opening X's ${actionLabel(action)} box…`;
  if (action === "reply" && !options.allowSecondReply && visibleOwnReplyToContext(context, article)) {
    status.className = "status";
    status.textContent = ALREADY_REPLIED;
    addChip(card, "Write another reply", async chip => {
      chip.remove();
      await chatActOnReply(card, decision, context, status, generationId, candidateId, { allowSecondReply: true });
    });
    return false;
  }
  const existingSessions = new Set(activeComposerSessions);
  let opened = false;
  try {
    opened = await openNativeComposerForPost(article, action, context as PostContext);
  } catch {
    status.className = "status error";
    status.textContent = `Couldn't open X's ${actionLabel(action)} box. Nothing in X changed; you can copy the draft instead.`;
    offerDraftCopy(card, draft, action);
    return false;
  }
  const session = opened ? await waitForComposerSession(action, 10_000, {
    context, sourceArticle: article, existing: existingSessions,
  }) : null;
  if (!session) {
    // See the note at the other site: `opened` distinguishes "X had no usable
    // button" from "the box never appeared". Same wording both places.
    status.className = "status error";
    status.textContent = opened
      ? `X's ${actionLabel(action)} box didn't open in time. Copy it in when ready.`
      : `X's ${actionLabel(action)} button isn't available on this post. Copy it in when ready.`;
    offerDraftCopy(card, draft, action);
    return false;
  }
  status.textContent = `Writing your ${actionLabel(action)}…`;
  let result: Awaited<ReturnType<typeof applyDraftToComposer>>;
  try {
    result = await applyDraftToComposer(session, draft, generationId, candidateId);
  } catch {
    status.className = "status error";
    status.textContent = `Starling could not type into that ${actionLabel(action)}. Your existing text is unchanged.`;
    offerDraftCopy(card, draft, action);
    return false;
  }
  status.className = result.ok ? "status" : "status error";
  status.textContent = result.ok
    ? (result.warning || `Typed into your ${actionLabel(action)}.`)
    : (result.error || `Couldn't finish your ${actionLabel(action)}.`);
  if (!result.ok) offerDraftCopy(card, draft, action, "Copy Starling's version");
  else offerUndo(card, session);
  return result.ok;
}

function renderChatResult(card: HTMLElement, response: AssistResponse, context?: PostContext) {
  const decision = response.decision;
  const actAction = decision?.native_intent ?? decision?.action;
  // Every path below shows the pages behind the answer as links (shared/sources.ts): the draft's
  // own sources first, then the answer's research, each page once. The reply/quote path used to
  // return before any of them, and never showed the draft's notes at all.
  const researchSources = response.research?.sources;
  // A chat reply/quote with a draft: DON'T dump the text in the dock — open the
  // real reply box on X and type it there, the way the user asked for.
  if (decision?.draft && (actAction === "reply" || actAction === "quote")) {
    card.textContent = "";
    const status = document.createElement("small");
    status.className = "status";
    status.textContent = `Your ${actionLabel(actAction)} is ready.`;
    card.append(status);
    // What the draft did and missed, what it checked, and where its facts came from.
    appendDraftReason(card, decision, researchSources);
    const label = `Put it in X's ${actionLabel(actAction)} box`;
    const put = addChip(card, label, async chip => {
      chip.disabled = true;
      chip.textContent = "Opening…";
      const applied = await chatActOnReply(card, decision, context, status,
        response.generation_id, response.candidate_id);
      if (applied) {
        chip.remove();
        return;
      }
      if (chip.isConnected) {
        chip.disabled = false;
        chip.textContent = label;
      }
    });
    put.className = "copy-pill";
    offerDraftCopy(card, decision.draft, actAction);
    scrollFeed();
    return;
  }
  card.textContent = response.answer || response.decision?.draft || "…";
  const extra = response as Record<string, any>;
  if (decision?.action === "post" || decision?.native_intent === "post") appendTopicLine(card, decision);
  // The notes, sources and voice note, but not the rationale: here it is the answer itself, or the
  // decision card below prints it.
  appendDraftReason(card, decision ? { ...decision, rationale: "" } : undefined, researchSources);
  const decisionExtra = decision as unknown as Record<string, unknown> | undefined;
  if (decision && decision.action !== "wait" && decisionExtra && "score" in decisionExtra) {
    renderDecisionCard(decision, context);
  } else if (decision && decision.action !== "wait" && decision.rationale && decision.rationale !== response.answer) {
    renderDecisionCard(decision, context);
  }
  // A created post carries a timing headline and the Schedule button.
  if (decision?.timing_line) {
    const timing = document.createElement("small");
    timing.className = "status";
    timing.textContent = decision.timing_line;
    card.append(timing);
  }
  if (decision?.schedule) renderScheduleControls(card, decision);
  if (isRecord(extra.preference_proposal) && typeof extra.preference_proposal.rule === "string") {
    renderPreferenceChip(extra.preference_proposal as Record<string, any>);
  }
  scrollFeed();
}

function renderDecisionCard(decision: NonNullable<AssistResponse["decision"]>, context?: PostContext) {
  const card = addCard("decision-card", "chat");
  const badge = document.createElement("span");
  badge.className = "action-badge";
  badge.dataset.action = decision.action;
  badge.textContent = decisionBadgeLabel(decision.action);
  const confidence = document.createElement("span");
  confidence.className = "confidence";
  confidence.textContent = decision.confidence;
  const head = document.createElement("div");
  head.className = "card-head";
  head.append(badge, confidence);
  const rationale = document.createElement("p");
  rationale.className = "body";
  rationale.textContent = decision.rationale;
  card.append(head, rationale);
  if (decision.reasons?.length) {
    addChip(card, "Why?", chip => {
      chip.remove();
      const reasons = document.createElement("small");
      reasons.className = "status";
      reasons.textContent = decision.reasons.map(reason => `• ${reason}`).join("  ");
      card.append(reasons);
    });
  }
  if (["reply", "quote", "post"].includes(decision.action)) {
    const candidates = [...activeComposerSessions].filter(session =>
      !session.disposed
      && session.composer.isConnected
      && session.action === decision.action
      && (!context
        ? decision.action === "post"
        : samePostContext(session.context, context)));
    // One exact target only. A generated decision never gets to guess between
    // two visible X composers, even if they happen to be the same action.
    const matching = candidates.length === 1 ? candidates[0] : undefined;
    if (matching) {
      addChip(card, "Draft it", async chip => {
        chip.disabled = true;
        chip.textContent = "Starting…";
        const start = await enhanceComposer(matching);
        if (start === "started") {
          chip.textContent = "Writing…";
          setCardActionStatus(card, `Writing your ${actionLabel(matching.action)} in X. Review it there before posting.`, false);
          return;
        }
        chip.disabled = false;
        chip.textContent = "Draft it";
        setCardActionStatus(card,
          start === "busy"
            ? "Starling is already working in that X composer."
            : `Couldn't start that ${actionLabel(matching.action)}. Nothing in X changed.`,
          true);
      });
    }
  }
}

// ---- scheduling (hold and remind, never auto-post) --------------------------

async function sendScheduleCommand(command: ScheduleCommand): Promise<AssistResponse> {
  try {
    const response = await sendToWorker({
      kind: MESSAGE_KINDS.assist, payload: { mode: "schedule", schedule: command },
    });
    if (!isRecord(response) || typeof response.ok !== "boolean") {
      return { ok: false, error: "Scheduling returned an invalid response. Nothing changed." };
    }
    return response as AssistResponse;
  } catch {
    return { ok: false, error: "Scheduling is unavailable right now." };
  }
}

/**
 * Scheduling controls under a created post.
 *
 * Two decisions live here and they are deliberately separate. The engine
 * decides whether it will RECOMMEND a time — it names one only when holding the
 * draft actually beats posting now, which on most posts is never. The user
 * decides what time to schedule, in the picker, and nothing is held until they
 * confirm it. A chip that scheduled on a single click was the whole reason a
 * four-day target could be committed by accident.
 *
 * Scheduling HOLDS the draft locally and resurfaces it when its moment arrives;
 * Starling never posts it for you.
 */
function renderScheduleControls(card: HTMLElement, decision: AssistDecision) {
  const rec = decision.schedule;
  const draft = decision.draft || "";
  // Scheduling is for standalone posts only — a reply/quote is tied to a live
  // conversation and must never carry a Schedule button.
  if (!rec || !draft.trim() || decision.action !== "post") return;

  // A firm post-now call (a rising conversation, or one already past) is the one
  // case where no time is the right time — no entry point at all.
  if (rec.allow_manual === false) return;

  // Name a time on the chip only when the engine stands behind that exact
  // time. Otherwise it is a plain opener: the door to the picker, not advice.
  const recommends = Boolean(rec.target_iso) && (rec.offer || rec.default === "schedule");
  const label = rec.target_label || "";
  const verb = !recommends ? "Schedule…"
    : rec.default === "schedule" ? `Schedule · ${label}`
    : `Schedule instead · ${label}`;

  addChip(card, verb, chip => {
    chip.remove();
    openSchedulePicker(card, decision, rec, draft, recommends);
  });
}

/**
 * The picker: choose a time, then commit it. Confirming is the ONLY path that
 * writes a schedule — opening this panel changes nothing.
 */
function openSchedulePicker(card: HTMLElement, decision: AssistDecision,
                            rec: ScheduleRecommendation, draft: string, recommends: boolean) {
  const panel = document.createElement("div");
  panel.className = "sched-picker";

  // The timing read, where the timing decision is actually being made. Two
  // lines, not the full breakdown — enough to judge the time being chosen.
  const why = document.createElement("small");
  why.className = "status sched-why";
  why.textContent = [rec.signals?.best_window, rec.signals?.freshness].filter(Boolean).join(" ");
  if (why.textContent) panel.append(why);

  const field = document.createElement("label");
  field.className = "sched-field";
  field.textContent = recommends ? "Hold this draft until" : "Hold this draft until (your call)";
  const input = document.createElement("input");
  input.type = "datetime-local";
  input.className = "sched-input";
  const now = new Date();
  input.value = toPickerValue(defaultPickerTime(recommends ? rec.target_iso : null, now));
  input.min = toPickerValue(new Date(now.getTime() + MIN_LEAD_MINUTES * 60_000));
  field.append(input);
  panel.append(field);

  const note = document.createElement("small");
  note.className = "status";
  note.dataset.starlingScheduleStatus = "true";

  const row = document.createElement("div");
  row.className = "chips sched-actions";
  const confirm = document.createElement("button");
  confirm.type = "button";
  confirm.className = "chip";
  confirm.textContent = "Schedule";
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.className = "chip";
  cancel.textContent = "Cancel";
  row.append(confirm, cancel);
  panel.append(row, note);
  card.append(panel);
  scrollFeed();

  cancel.addEventListener("click", () => {
    panel.remove();
    // Back to the opener, so cancelling is not a one-way door.
    renderScheduleControls(card, decision);
    scrollFeed();
  });

  confirm.addEventListener("click", () => {
    const chosen = readManualTime(input.value);
    if (!chosen.iso || !chosen.label) {
      note.classList.add("error");
      note.textContent = chosen.error || "Pick a time first.";
      scrollFeed();
      return;
    }
    void commitSchedule(card, panel, decision, rec, draft, chosen.iso, chosen.label, confirm, cancel, note);
  });
}

/** Save the held draft, then set X's own dialog up for the same moment. */
async function commitSchedule(card: HTMLElement, panel: HTMLElement, decision: AssistDecision,
                              rec: ScheduleRecommendation, draft: string, targetIso: string,
                              label: string, confirm: HTMLButtonElement, cancel: HTMLButtonElement,
                              note: HTMLElement) {
  confirm.disabled = true;
  cancel.disabled = true;
  confirm.textContent = "Scheduling…";
  note.classList.remove("error");
  activeScheduleActions += 1;
  try {
    const res = await sendScheduleCommand({
      op: "save", text: draft, target_at: targetIso, target_label: label,
      topic: rec.topic_prediction?.trend, freshness: rec.freshness, reason: rec.headline,
    });
    const responseData = res as unknown as Record<string, unknown>;
    const receipt = isScheduleSaveConfirmed(responseData, draft, targetIso, label);
    if (res.ok && !receipt) {
      res.error = "Starling did not receive confirmation for that exact schedule. Nothing was changed — try again.";
    }
    if (!res.ok || !receipt) {
      confirm.disabled = false;
      cancel.disabled = false;
      confirm.textContent = "Schedule";
      note.classList.add("error");
      note.textContent = res.error || "Couldn't schedule that — try again.";
      scrollFeed();
      return;
    }
    // Committed. The picker's job is done; the status line carries the rest.
    panel.replaceChildren(note);
    note.textContent = typeof responseData.message === "string" ? responseData.message
      : `Saved for ${label}. Starling will remind you here. You post it.`;
    // ONE confirmation, both halves of scheduling: the draft is held here AND X
    // is set up — thread parts written into their boxes, X's own schedule dialog
    // filled in. Starling still never presses X's publish control (see
    // schedule-ui.mayClick).
    const parts = (decision as unknown as Record<string, unknown>).thread_parts;
    const plan = planThread(parts);
    const built = plan.boxesToAdd > 0 ? await buildThreadInComposer(plan.parts) : 1;
    const failure = await setNativeSchedule(targetIso);
    if (!failure) note.textContent = scheduleReadyLine(label, Math.max(built, 1));
    scrollFeed();
  } finally {
    activeScheduleActions = Math.max(0, activeScheduleActions - 1);
  }
}

const surfacedDueIds = new Set<number>();
let dueDraftsSurfacedAt = 0;

/**
 * The "remind" half of hold-and-remind. On dock open, surface any scheduled
 * drafts whose time has arrived as ready-to-post cards. The user posts them in
 * X themselves — there is no posting path here, only Copy / Posted it / Dismiss.
 */
const DUE_SURFACE_CAP = 3;   // never flood the feed on open

async function surfaceDueDrafts() {
  const now = Date.now();
  if (now - dueDraftsSurfacedAt < 60_000) return;   // throttle: at most once a minute
  dueDraftsSurfacedAt = now;                         // claim the slot (no concurrent double-fire)
  const res = await sendScheduleCommand({ op: "list_due" });
  const due = (res as unknown as Record<string, unknown>).due;
  if (!res.ok || !Array.isArray(due)) {
    dueDraftsSurfacedAt = 0;                          // failed/empty response — let the next open retry
    return;
  }
  // Only surface drafts not already shown this session (Dismiss/Posted persist
  // server-side, so an acted-on draft never returns). Cap the count so a pile of
  // due drafts never buries the chat — the rest wait for the next open.
  const fresh = (due as Record<string, unknown>[]).filter(raw => {
    const id = Number(raw.id);
    return Number.isFinite(id) && !surfacedDueIds.has(id);
  });
  if (!fresh.length) return;
  for (const raw of fresh.slice(0, DUE_SURFACE_CAP)) {
    const id = Number(raw.id);
    surfacedDueIds.add(id);
    renderDueDraftCard(raw, id);
  }
  const overflow = fresh.length - DUE_SURFACE_CAP;
  if (overflow > 0) {
    const note = addCard("due-card");
    const line = document.createElement("small");
    line.className = "status";
    line.textContent = `+${overflow} more scheduled draft${overflow > 1 ? "s" : ""} ready — I'll surface them as you clear these.`;
    note.append(line);
    scrollFeed();
  }
}

function renderDueDraftCard(draft: Record<string, unknown>, id: number) {
  const text = String(draft.text || "");
  const label = String(draft.target_label || "now");
  if (!text.trim()) return;
  const card = addCard("due-card");
  const head = document.createElement("small");
  head.className = "status";
  head.textContent = `⏰ Scheduled draft ready (${label}). Post it yourself when you're ready — nothing is auto-posted.`;
  const body = document.createElement("p");
  body.className = "body";
  body.textContent = text;
  card.append(head, body);
  addChip(card, "Copy", async chip => {
    const copied = await navigator.clipboard.writeText(text).then(() => true, () => false);
    if (copied) {
      chip.textContent = "Copied";
      return;
    }
    setCardActionStatus(card, "Couldn't copy that draft. Select the text above and copy it manually.", true);
  });
  addChip(card, "Posted it", async chip => {
    chip.disabled = true;
    chip.textContent = "Confirming…";
    const response = await sendScheduleCommand({ op: "mark_posted", draft_id: id });
    if (isScheduleMutationConfirmed(response, id, "posted")) {
      card.remove();
      scrollFeed();
      return;
    }
    chip.disabled = false;
    chip.textContent = "Posted it";
    setCardActionStatus(card, response.error || "Couldn't confirm that post. The reminder is still saved.", true);
  });
  addChip(card, "Dismiss", async chip => {
    chip.disabled = true;   // guard against a double-click firing two dismisses
    chip.textContent = "Dismissing…";
    const response = await sendScheduleCommand({ op: "dismiss", draft_id: id });
    if (isScheduleMutationConfirmed(response, id, "dismissed")) {
      card.remove();
      scrollFeed();
      return;
    }
    chip.disabled = false;
    chip.textContent = "Dismiss";
    setCardActionStatus(card, response.error || "Couldn't dismiss that reminder. It is still saved.", true);
  });
  scrollFeed();
}

function renderPreferenceChip(proposal: Record<string, any>) {
  const card = addCard("pref-card", "chat");
  const label = document.createElement("small");
  label.className = "status";
  label.textContent = `Remember this? “${proposal.rule}”`;
  card.append(label);
  addChip(card, "Save", async chip => {
    chip.disabled = true;
    chip.textContent = "Saving…";
    const response = await sendToWorker({
      kind: "starling:confirm-preference",
      payload: { proposal_id: proposal.id, source_text: proposal.source_text || proposal.rule,
                 rule: proposal.rule, scope: proposal.scope || "strategy", confirm: true },
    }).catch(() => undefined);
    if (!isRecord(response) || response.ok !== true) {
      chip.disabled = false;
      chip.textContent = "Save";
      setCardActionStatus(card, "Couldn't save that preference. It was not changed.", true);
      return;
    }
    card.replaceChildren();
    const saved = document.createElement("small");
    saved.className = "status";
    saved.textContent = "Saved. Starling will use it from now on.";
    card.append(saved);
  });
  addChip(card, "Dismiss", async chip => {
    chip.disabled = true;
    chip.textContent = "Dismissing…";
    const response = await sendToWorker({
      kind: "starling:confirm-preference",
      payload: { proposal_id: proposal.id, source_text: proposal.source_text || proposal.rule,
                 rule: proposal.rule, scope: proposal.scope || "strategy", confirm: false },
    }).catch(() => undefined);
    if (!isRecord(response) || response.ok !== true) {
      chip.disabled = false;
      chip.textContent = "Dismiss";
      setCardActionStatus(card, "Couldn't dismiss that preference. It is still available.", true);
      return;
    }
    card.remove();
    scrollFeed();
  });
}

// ---- drag + resize ----------------------------------------------------------

async function loadDockRect() {
  if (dockRectLoaded) return;
  dockRectLoaded = true;
  try {
    const stored = await pagePrefs.get([DOCK_RECT_KEY]);
    const rect = stored[DOCK_RECT_KEY];
    if (isRecord(rect)) savedDockRect = rect as Record<string, number>;
  } catch { /* the default floating size stands */ }
}

function savedDockNumber(key: "left" | "top" | "width" | "height"): number | undefined {
  const value = savedDockRect?.[key];
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function clampNumber(value: number, low: number, high: number): number {
  return Math.max(low, Math.min(high, value));
}

function dockMaxWidth(): number {
  return Math.max(1, Math.min(Math.round(window.innerWidth * 0.92), window.innerWidth - DOCK_VIEWPORT_INSET * 2));
}

function dockMaxHeight(): number {
  return Math.max(1, Math.min(Math.round(window.innerHeight * 0.92), window.innerHeight - DOCK_VIEWPORT_INSET * 2));
}

function dockMinWidth(): number {
  return Math.min(DOCK_MIN_WIDTH, dockMaxWidth());
}

function dockMinHeight(): number {
  return Math.min(DOCK_MIN_HEIGHT, dockMaxHeight());
}

function clampDockWidth(value: number): number {
  return clampNumber(value, dockMinWidth(), dockMaxWidth());
}

function clampDockHeight(value: number): number {
  return clampNumber(value, dockMinHeight(), dockMaxHeight());
}

function panelBounds(panel: HTMLElement): DOMRect | null {
  const bounds = panel.getBoundingClientRect();
  return bounds.width > 0 && bounds.height > 0 ? bounds : null;
}

function moveDockToFloating(host: HTMLElement, panel: HTMLElement, sourceBounds = panelBounds(panel)) {
  const preferredWidth = savedDockNumber("width");
  const preferredHeight = savedDockNumber("height");
  const width = clampDockWidth(preferredWidth ?? sourceBounds?.width ?? Math.min(440, dockMaxWidth()));
  const height = clampDockHeight(preferredHeight ?? sourceBounds?.height ?? Math.min(580, dockMaxHeight()));
  const fallbackLeft = Math.max(DOCK_VIEWPORT_INSET, window.innerWidth - width - 24);
  const left = clampNumber(
    sourceBounds?.left ?? savedDockNumber("left") ?? fallbackLeft,
    DOCK_VIEWPORT_INSET,
    Math.max(DOCK_VIEWPORT_INSET, window.innerWidth - width - DOCK_VIEWPORT_INSET),
  );
  const top = clampNumber(
    sourceBounds?.top ?? savedDockNumber("top") ?? 88,
    DOCK_VIEWPORT_INSET,
    Math.max(DOCK_VIEWPORT_INSET, window.innerHeight - height - DOCK_VIEWPORT_INSET),
  );

  dockPlacement = "floating";
  host.dataset.placement = "floating";
  host.style.cssText = "display:block;position:fixed;right:auto;z-index:2147483646;";
  host.style.left = `${left}px`;
  host.style.top = `${top}px`;
  panel.style.width = `${width}px`;
  panel.style.height = `${height}px`;
  document.documentElement.append(host);
  updateDockPlacementUi(panel);
}

function saveDockRect(host: HTMLElement, panel: HTMLElement) {
  if (dockPlacement !== "floating" || !host.isConnected) return;
  const bounds = panelBounds(panel);
  if (!bounds) return;
  const rect = { left: bounds.left, top: bounds.top, width: bounds.width, height: bounds.height };
  savedDockRect = rect;
  void pagePrefs.set({ [DOCK_RECT_KEY]: rect }).catch(() => undefined);
}

function beginFloatingDockDrag(host: HTMLElement, panel: HTMLElement, event: PointerEvent) {
  event.preventDefault();
  panel.classList.add("dragging");
  const start = panel.getBoundingClientRect();
  const offsetX = event.clientX - start.left;
  const offsetY = event.clientY - start.top;
  const dockTarget = showSidebarDockTarget();
  let dockTargetLocked = false;
  const updateDockTarget = (move: PointerEvent) => {
    if (!dockTarget?.isConnected) return;
    const locked = isDockTargetHovered(panel, dockTarget, move);
    if (locked === dockTargetLocked) return;
    dockTargetLocked = locked;
    panel.classList.toggle("dock-targeting", locked);
    setSidebarDockTargetState(dockTarget, locked);
  };
  const onMove = (move: PointerEvent) => {
    if (move.pointerId !== event.pointerId) return;
    const maxLeft = Math.max(DOCK_VIEWPORT_INSET, window.innerWidth - start.width - DOCK_VIEWPORT_INSET);
    const maxTop = Math.max(DOCK_VIEWPORT_INSET, window.innerHeight - start.height - DOCK_VIEWPORT_INSET);
    let left = clampNumber(move.clientX - offsetX, DOCK_VIEWPORT_INSET, maxLeft);
    let top = clampNumber(move.clientY - offsetY, DOCK_VIEWPORT_INSET, maxTop);
    // Edge snap makes a deliberately moved card feel settled without making it sticky.
    if (left < DOCK_VIEWPORT_INSET + 16) left = DOCK_VIEWPORT_INSET;
    if (window.innerWidth - (left + start.width) < DOCK_VIEWPORT_INSET + 16) left = maxLeft;
    if (top < DOCK_VIEWPORT_INSET + 16) top = DOCK_VIEWPORT_INSET;
    host.style.right = "auto";
    host.style.left = `${left}px`;
    host.style.top = `${top}px`;
    updateDockTarget(move);
  };
  const finish = (end: PointerEvent) => {
    if (end.pointerId !== event.pointerId) return;
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", finish);
    window.removeEventListener("pointercancel", finish);
    panel.classList.remove("dragging");
    if (end.type === "pointerup" && dockTargetLocked) {
      dockFloatingCardInSidebar(host, panel);
      return;
    }
    hideSidebarDockTarget();
    panel.classList.remove("dock-targeting");
    saveDockRect(host, panel);
  };
  window.addEventListener("pointermove", onMove);
  window.addEventListener("pointerup", finish);
  window.addEventListener("pointercancel", finish);
}

function armSidebarDockDetach(host: HTMLElement, panel: HTMLElement, bar: HTMLElement, event: PointerEvent) {
  // Drag the header and the card floats immediately on the first real move
  // (>8px) — no hold. Matches the design's grab-to-float behavior.
  const pointerId = event.pointerId;
  const originX = event.clientX;
  const originY = event.clientY;
  const cleanup = () => {
    bar.classList.remove("holding");
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", finish);
    window.removeEventListener("pointercancel", finish);
  };
  const onMove = (move: PointerEvent) => {
    if (move.pointerId !== pointerId) return;
    const moved = Math.hypot(move.clientX - originX, move.clientY - originY);
    if (moved <= DOCK_DETACH_MOVE_TOLERANCE) return;
    move.preventDefault();
    const bounds = panelBounds(panel);
    cleanup();
    moveDockToFloating(host, panel, bounds);
    beginFloatingDockDrag(host, panel, move);
  };
  const finish = (end: PointerEvent) => {
    if (end.pointerId !== pointerId) return;
    cleanup();
  };
  window.addEventListener("pointermove", onMove);
  window.addEventListener("pointerup", finish);
  window.addEventListener("pointercancel", finish);
}

function installDockDrag(host: HTMLElement, panel: HTMLElement) {
  const bar = panel.querySelector<HTMLElement>(".bar")!;
  bar.addEventListener("pointerdown", event => {
    if (event.button !== 0 || (event.target instanceof Element && event.target.closest("button"))) return;
    if (dockPlacement === "sidebar") {
      armSidebarDockDetach(host, panel, bar, event);
      return;
    }
    beginFloatingDockDrag(host, panel, event);
  });
}

function installDockResize(host: HTMLElement, panel: HTMLElement) {
  // Resize remains available after detaching; the docked card fills the rail.
  const edges: { selector: string; x: number; y: number }[] = [
    { selector: ".edge-l", x: -1, y: 0 },
    { selector: ".edge-r", x: 1, y: 0 },
    { selector: ".edge-t", x: 0, y: -1 },
    { selector: ".edge-b", x: 0, y: 1 },
    { selector: ".edge-tl", x: -1, y: -1 },
    { selector: ".edge-tr", x: 1, y: -1 },
    { selector: ".edge-br", x: 1, y: 1 },
    { selector: ".edge-bl", x: -1, y: 1 },
  ];
  for (const { selector, x, y } of edges) {
    const handle = panel.querySelector<HTMLElement>(selector);
    if (!handle) continue;
    handle.addEventListener("pointerdown", event => {
      if (dockPlacement !== "floating") return;
      event.preventDefault();
      event.stopPropagation();
      const startRect = panel.getBoundingClientRect();
      const startW = startRect.width;
      const startH = startRect.height;
      const originX = event.clientX;
      const originY = event.clientY;
      handle.setPointerCapture?.(event.pointerId);
      const onMove = (move: PointerEvent) => {
        if (move.pointerId !== event.pointerId) return;
        const dx = move.clientX - originX;
        const dy = move.clientY - originY;
        if (x !== 0) {
          const width = clampDockWidth(startW + dx * x);
          panel.style.width = `${width}px`;
          // Dragging a left edge also moves the anchor so the right stays put.
          if (x < 0) {
            host.style.right = "auto";
            host.style.left = `${clampNumber(startRect.right - width, DOCK_VIEWPORT_INSET, Math.max(DOCK_VIEWPORT_INSET, window.innerWidth - width - DOCK_VIEWPORT_INSET))}px`;
          }
        }
        if (y !== 0) {
          const height = clampDockHeight(startH + dy * y);
          panel.style.height = `${height}px`;
          if (y < 0) {
            host.style.top = `${clampNumber(startRect.bottom - height, DOCK_VIEWPORT_INSET, Math.max(DOCK_VIEWPORT_INSET, window.innerHeight - height - DOCK_VIEWPORT_INSET))}px`;
          }
        }
      };
      const finish = (end: PointerEvent) => {
        if (end.pointerId !== event.pointerId) return;
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", finish);
        window.removeEventListener("pointercancel", finish);
        saveDockRect(host, panel);
      };
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", finish);
      window.addEventListener("pointercancel", finish);
    });
  }
}

/** The author's profile picture for this post, for the dock's context chip. */
function extractAvatarUrl(article: HTMLElement): string | undefined {
  const img = article.querySelector<HTMLImageElement>(
    '[data-testid="Tweet-User-Avatar"] img[src], [data-testid^="UserAvatar-Container"] img[src], img[src*="profile_images"]'
  );
  const src = img?.src;
  return src && /profile_images/.test(src) ? src : undefined;
}

export function extractContext(article: HTMLElement): PostContext {
  // Client-only side effect: remember this post's avatar for the context chip.
  const avatarUrl = extractAvatarUrl(article);
  activeContextAvatar = avatarUrl;
  const capturedAt = new Date().toISOString();
  const permalink = findPermalink(article);
  const postId = permalink?.href.match(/\/status\/(\d+)/)?.[1];
  const textElement = findPrimaryTextElement(article);
  const userName = article.querySelector<HTMLElement>('[data-testid="User-Name"]');
  const userLines = userName?.innerText.split("\n").map(line => line.trim()).filter(Boolean) ?? [];
  const normalizedAuthorHandle = extractAuthorHandle(article);
  const authorHandle = normalizedAuthorHandle ? `@${normalizedAuthorHandle}`
    : userLines.filter(line => /^@[A-Za-z0-9_]+$/.test(line)).at(-1);
  const quote = extractQuoteContext(article);
  const parent = extractVisibleParentContext(article);
  const threadText = extractVisibleThreadText(article, authorHandle, textElement?.innerText.trim() ?? "");
  const postTimestamp = permalink?.querySelector<HTMLTimeElement>("time")?.dateTime
    ?? article.querySelector<HTMLTimeElement>("time[datetime]")?.dateTime;
  const pollOptions = extractPollOptions(article);
  const postType = inferPostType(article, quote.text, parent?.text, threadText, pollOptions);
  const sensitive = /sensitive content|content warning|show content/i.test(article.innerText);
  const signedInIdentity = getSignedInIdentity();
  const signedInHandle = signedInIdentity ? `@${signedInIdentity.handle}` : undefined;

  const context: PostContext = {
    context_id: crypto.randomUUID(),
    post_id: postId,
    post_url: permalink?.href,
    author: userLines.find(line => !line.startsWith("@") && !isTimestampLabel(line)) ?? "Unknown author",
    author_handle: authorHandle,
    author_verified: Boolean(userName?.querySelector('[data-testid="icon-verified"], svg[aria-label*="Verified" i]')),
    post_created_at: postTimestamp && Number.isFinite(Date.parse(postTimestamp)) ? postTimestamp : undefined,
    text: textElement?.innerText.trim() ?? "",
    parent_text: parent?.text || undefined,
    parent_author: parent?.authorHandle,
    // NOT parent_display_name here. The engine's `PostContext` rejects
    // unknown fields (measured: ValidationError), so sending it would 422 every
    // draft. The snapshot path below carries it instead, and that is the path
    // that actually feeds `posts` -> `relationships.rebuild()` -> the
    // `display_name` column. Add it here only alongside a schema field.
    parent_post_id: parent?.postId,
    parent_post_url: parent?.url,
    quote_text: quote.text,
    quoted_post_id: quote.postId,
    thread_text: threadText.length ? threadText : undefined,
    links: extractLinks(article),
    // Enhancement deliberately has no image payload. The relevant source is
    // the rendered text and thread/quote context the user can already see.
    // The post's images ARE part of what it says (his Polymarket screenshot
    // was the entire evidence) — capture their URLs and alt text so the
    // engine's vision model can read them. Registry C30/C36.
    media: Array.from(article.querySelectorAll<HTMLImageElement>('[data-testid="tweetPhoto"] img'))
      .filter(img => img.closest(POST_SELECTOR) === article
        && !img.closest('[data-testid="quoteTweet"], [role="blockquote"]')
        && (img.currentSrc || img.src || "").startsWith("https://"))
      .slice(0, 4)
      .map(img => ({ url: img.currentSrc || img.src, kind: "image" as const,
                     alt: img.alt && img.alt !== "Image" ? img.alt : undefined })),
    language: textElement?.getAttribute("lang") ?? undefined,
    visibility: inferVisibility(article, userName),
    visible_metrics: extractVisibleMetrics(article),
    post_type: postType,
    poll: pollOptions.length ? { options: pollOptions } : undefined,
    poll_options: pollOptions.length ? pollOptions : undefined,
    sensitive,
    is_sensitive: sensitive,
    reply_restricted: isReplyControlDisabled(article),
    // Structural, not English: X marks the expander with its own testid, so
    // "Mehr anzeigen" / "Ver más" count too. The length check already works
    // in any language.
    is_long_post: (textElement?.innerText.length ?? 0) > 280
      || Boolean(article.querySelector('[data-testid="tweet-text-show-more-link"]'))
      || /show more/i.test(article.innerText),
    is_own_post: Boolean(authorHandle && signedInHandle && authorHandle.toLowerCase() === signedInHandle.toLowerCase()),
    // ConversationState: what the thread on screen actually shows. The Tibo
    // incident drafted a second reply while the user's own reply was VISIBLE
    // right below — because nothing ever looked. Now the visible thread is
    // evidence the engine trusts over its own stale records.
    ...threadVisibility(article),
    visible_reply_count: visibleReplyCount(article),
    page_url: pageAddressForService(location.href),
    captured_at: capturedAt
  };
  // Pair the avatar with THIS context object — the chip must never dress one
  // post's context in another post's face (a stale global did exactly that).
  if (avatarUrl) contextAvatars.set(context, avatarUrl);
  return context;
}

// What the visible conversation shows about MY participation. Only meaningful
// on a status page (the thread is rendered there); on the timeline the thread
// is not visible, so thread_scanned stays false and the DB remains the only
// evidence. Bounded to the next 25 articles — enough for any loaded thread.
function threadVisibility(article: HTMLElement): {
  thread_scanned?: boolean; visible_own_reply?: boolean; visible_reply_ages_minutes?: number[];
} {
  try {
    if (!/\/status\/\d+/.test(location.pathname)) return {};
    const me = getSignedInIdentity()?.handle?.toLowerCase();
    const articles = Array.from(document.querySelectorAll<HTMLElement>("article"));
    const start = articles.indexOf(article);
    if (start < 0) return {};
    let ownReply = false;
    const ages: number[] = [];
    for (const later of articles.slice(start + 1, start + 26)) {
      const handle = extractAuthorHandle(later)?.toLowerCase?.();
      if (me && handle === me) ownReply = true;
      const stamp = later.querySelector("time")?.getAttribute("datetime");
      if (stamp) {
        const minutes = (Date.now() - Date.parse(stamp)) / 60000;
        if (Number.isFinite(minutes) && minutes >= 0 && ages.length < 30) {
          ages.push(Math.round(minutes * 10) / 10);
        }
      }
    }
    return { thread_scanned: true, visible_own_reply: ownReply || undefined,
             visible_reply_ages_minutes: ages.length ? ages : undefined };
  } catch {
    return {};
  }
}

// "Replying to @x" in ANY language. The reply header sits above the post text
// and links the parent author's profile, so the STRUCTURE identifies it —
// "Antwort an @x" and "Respondiendo a @x" used to read as not-a-reply, which
// silently broke thread awareness for every non-English user.
export function extractRepliedToHandle(article: HTMLElement): string | undefined {
  const english = (article.innerText.match(/replying\s+to\s+@([a-z0-9_]+)/i) || [])[1];
  if (english) return english;
  const textElement = article.querySelector('[data-testid="tweetText"]');
  for (const link of Array.from(article.querySelectorAll<HTMLAnchorElement>('a[href^="/"]'))) {
    // Must sit BEFORE the post body: mentions inside the text are not headers.
    if (textElement && !(link.compareDocumentPosition(textElement)
        & Node.DOCUMENT_POSITION_FOLLOWING)) continue;
    if (link.closest('[data-testid="User-Name"]')) continue;   // that's the author
    const handle = (link.textContent || "").trim();
    if (/^@[A-Za-z0-9_]{1,15}$/.test(handle)) return handle.slice(1);
  }
  return undefined;
}

// Crowd size from the reply button's own label ("1717 Replies. Reply" / "1.7K").
function visibleReplyCount(article: HTMLElement): number | undefined {
  try {
    const label = article.querySelector('[data-testid="reply"]')?.getAttribute("aria-label") || "";
    return parseCompactCount(label);
  } catch {
    return undefined;
  }
}

function findPermalink(article: HTMLElement): HTMLAnchorElement | undefined {
  const timestampLink = Array.from(article.querySelectorAll<HTMLAnchorElement>('a[href*="/status/"]'))
    .find(link => link.closest(POST_SELECTOR) === article
      && !link.closest('[data-testid="quoteTweet"], [role="blockquote"]')
      && Boolean(link.querySelector("time")));
  if (timestampLink) return timestampLink;
  return Array.from(article.querySelectorAll<HTMLAnchorElement>('a[href*="/status/"]'))
    .find(link => link.closest(POST_SELECTOR) === article
      && !link.closest('[data-testid="quoteTweet"], [role="blockquote"]')
      && /\/status\/\d+/.test(link.href));
}

function findPrimaryTextElement(article: HTMLElement): HTMLElement | null {
  return Array.from(article.querySelectorAll<HTMLElement>('[data-testid="tweetText"]')).find(element => {
    if (element.closest(POST_SELECTOR) !== article) return false;
    return !element.closest('[data-testid="quoteTweet"], [role="blockquote"]');
  }) ?? null;
}

function extractQuoteContext(article: HTMLElement): { text?: string; postId?: string } {
  const quoteRoot = article.querySelector<HTMLElement>('[data-testid="quoteTweet"], [role="blockquote"]');
  const quoteUrl = quoteRoot?.querySelector<HTMLAnchorElement>('a[href*="/status/"]')?.href;
  return {
    text: quoteRoot?.querySelector<HTMLElement>('[data-testid="tweetText"]')?.innerText.trim() || undefined,
    postId: postIdFromUrl(quoteUrl)
  };
}

function extractVisibleParentContext(article: HTMLElement): { text: string; author?: string; authorHandle?: string; displayName?: string; postId?: string; url?: string } | undefined {
  if (!/replying to/i.test(article.innerText) || !/\/status\/\d+/.test(location.pathname)) return undefined;
  const articles = Array.from(document.querySelectorAll<HTMLElement>(POST_SELECTOR));
  const index = articles.indexOf(article);
  if (index <= 0) return undefined;
  const parent = articles[index - 1];
  const text = findPrimaryTextElement(parent)?.innerText.trim();
  const parentId = postIdFromUrl(findPermalink(parent)?.href);
  // Image-only or collapsed source posts have no readable text, but their
  // status id is still the exact reply parent we need for duplicate checks.
  if (!text && !parentId) return undefined;
  // BOTH VALUES, AND THE HANDLE IS THE IDENTITY ONE.
  //
  // This kept only the display name — the predicate deliberately SKIPS the
  // "@handle" line — and wrote it into `parent_author`, which every consumer
  // reads as a handle: `relationships.reply_target` calls `.lstrip("@")` on it,
  // which is meaningless on "Sam Altman". Measured in the store, the only three
  // values ever written were 'Grok', 'Tibo' and 'Sam Altman' against the real
  // handles grok, thsottiaux and sama — two of three could never match
  // anything, and `author_relationships` ended up keyed on a name with a space
  // in it while its own `display_name` column sat NULL. The right column
  // existed and the value was in the wrong one.
  //
  // Note 'Grok' lowercases to its own handle: a test written with Grok as its
  // fixture passes while both real cases fail.
  //
  // Both are kept. The handle identifies; the display name is for showing.
  const parentLines = parent.querySelector<HTMLElement>('[data-testid="User-Name"]')?.innerText.split("\n") ?? [];
  const parentHandle = parentLines
    .map(line => line.trim())
    .find(line => line.startsWith("@") && line.length > 1)?.slice(1);
  const parentDisplayName = parentLines
    .find(line => !line.trim().startsWith("@") && !isTimestampLabel(line.trim()))?.trim();
  const parentUrl = findPermalink(parent)?.href;
  // A SECOND SUPPLIER, because the DOM is not ours. `User-Name` is a testid X
  // can rename tomorrow; a permalink's first path segment is the same handle
  // and moves only if URLs change. Whichever survives, identity survives.
  const resolvedHandle = parentHandle ?? handleFromStatusUrl(parentUrl);
  return {
    text: text ?? "",
    author: resolvedHandle,
    authorHandle: resolvedHandle,
    displayName: parentDisplayName,
    postId: parentId,
    url: parentUrl
  };
}

/** The @handle out of `https://x.com/<handle>/status/<id>`, or undefined. */
function handleFromStatusUrl(url: string | undefined): string | undefined {
  if (!url) return undefined;
  const match = /^https?:\/\/(?:[a-z]+\.)?(?:x|twitter)\.com\/([A-Za-z0-9_]{1,15})\/status\/\d+/i.exec(url);
  return match ? match[1] : undefined;
}

function extractVisibleThreadText(article: HTMLElement, handle: string | undefined, ownText: string): string[] {
  if (!handle || !/\/status\/\d+/.test(location.pathname)) return [];
  // AN EXACT HANDLE, NOT A SUBSTRING OF THE DISPLAYED NAME.
  //
  // This was `name.includes(handle)` against the whole `User-Name` block —
  // display name, "@handle" and timestamp concatenated. A substring test on
  // that matches too much: handle "ben" hits "@benposts", and it also hits a
  // DISPLAY NAME containing the letters, so another account called "Ben's
  // Newsletter" contributed its posts to his thread. It is the same
  // shape-instead-of-meaning failure as a word-boundary regex missing
  // "posting", and it fails in the direction that silently widens.
  //
  // The permalink is the exact supplier: `x.com/<handle>/status/<id>` yields
  // one unambiguous handle per post, and `handleFromStatusUrl` already parses
  // it for the parent extractor above.
  const wanted = handle.replace(/^@/, "").toLowerCase();
  const texts = Array.from(document.querySelectorAll<HTMLElement>(POST_SELECTOR)).flatMap(item => {
    if (item === article) return [];
    const itemHandle = handleFromStatusUrl(findPermalink(item)?.href)?.toLowerCase();
    const text = findPrimaryTextElement(item)?.innerText.trim();
    return itemHandle === wanted && text && text !== ownText ? [text] : [];
  });
  return [...new Set(texts)].slice(0, 12);
}

function extractLinks(article: HTMLElement): string[] {
  const links = Array.from(article.querySelectorAll<HTMLAnchorElement>('a[href^="http"]')).flatMap(link => {
    if (link.closest(POST_SELECTOR) !== article) return [];
    const candidate = isExternalUrl(link.title) ? link.title : link.href;
    return isExternalUrl(candidate) ? [candidate] : [];
  });
  return [...new Set(links)];
}

function extractVisibleMetrics(article: HTMLElement): VisibleMetrics | undefined {
  const metrics: VisibleMetrics = {};
  assignMetric(metrics, "replies", article.querySelector<HTMLElement>('[data-testid="reply"]'));
  assignMetric(metrics, "reposts", article.querySelector<HTMLElement>('[data-testid="retweet"], [data-testid="unretweet"]'));
  assignMetric(metrics, "likes", article.querySelector<HTMLElement>('[data-testid="like"], [data-testid="unlike"]'));
  assignMetric(metrics, "bookmarks", article.querySelector<HTMLElement>('[data-testid="bookmark"], [data-testid="removeBookmark"]'));
  assignMetric(metrics, "views", article.querySelector<HTMLElement>('a[href$="/analytics"], a[aria-label*="view" i][href*="/status/"]'));
  return Object.keys(metrics).length ? metrics : undefined;
}

function assignMetric(metrics: VisibleMetrics, key: keyof VisibleMetrics, element: HTMLElement | null) {
  if (!element) return;
  const label = element.getAttribute("aria-label") ?? element.innerText ?? "";
  const value = parseCompactNumber(label);
  if (value != null) metrics[key] = value;
}

function parseCompactNumber(value: string): number | undefined {
  const match = value.replace(/,/g, "").match(/(\d+(?:\.\d+)?)\s*([KMB])?/i);
  if (!match) return undefined;
  const multiplier = match[2]?.toUpperCase() === "K" ? 1_000
    : match[2]?.toUpperCase() === "M" ? 1_000_000
      : match[2]?.toUpperCase() === "B" ? 1_000_000_000
        : 1;
  return Math.round(Number(match[1]) * multiplier);
}

function extractPollOptions(article: HTMLElement): PollOption[] {
  return Array.from(article.querySelectorAll<HTMLElement>('[role="progressbar"]')).flatMap(progress => {
    if (progress.closest(POST_SELECTOR) !== article) return [];
    const label = progress.getAttribute("aria-label") ?? progress.parentElement?.innerText ?? "";
    const percent = label.match(/(\d+(?:\.\d+)?)\s*%/)?.[1];
    const option = label.split(/\n|,|\d+(?:\.\d+)?\s*%/).map(part => part.trim()).find(Boolean);
    return option ? [{ label: option, percent: percent ? Number(percent) : undefined }] : [];
  });
}

function inferVisibility(article: HTMLElement, userName: HTMLElement | null): PostContext["visibility"] {
  if (userName?.querySelector('[data-testid="icon-lock"], svg[aria-label*="Protected" i]') || /protected account/i.test(article.innerText)) {
    return "protected";
  }
  return article.isConnected ? "public" : "unknown";
}

function inferPostType(
  article: HTMLElement,
  quoteText: string | undefined,
  parentText: string | undefined,
  threadText: string[],
  pollOptions: PollOption[]
): PostContext["post_type"] {
  if (pollOptions.length) return "poll";
  if (quoteText) return "quote";
  if (parentText || /replying to/i.test(article.innerText)) return "reply";
  if (threadText.length) return "thread";
  return "original";
}

/** Whether X has DISABLED the reply control on this post.
 *
 * NAME-VS-BEHAVIOUR, fixed. This was `isReplyRestricted`, which promises a
 * check it has never made: it does not read X's "who can reply" setting. It
 * re-reads the same two attributes `isNativeControlEnabled` just read, so the
 * two guards in `openNativeComposerForPost` are one test written twice.
 *
 * That matters for diagnosis. A post whose replies are restricted BY POLICY can
 * still render an enabled-looking button; both guards then pass, `opened`
 * comes back true, and the failure surfaces as a 10-second timeout instead of
 * "no usable button". Renaming it does not fix that — it stops the name from
 * claiming the gap is already covered.
 */
function isReplyControlDisabled(article: HTMLElement): boolean {
  const reply = article.querySelector<HTMLElement>('[data-testid="reply"]');
  return reply?.getAttribute("aria-disabled") === "true" || reply?.hasAttribute("disabled") === true;
}

function findVisibleComposers(): HTMLElement[] {
  const editable = Array.from(document.querySelectorAll<HTMLElement>(COMPOSER_SELECTOR))
    .map(resolveEditableComposer)
    .filter((item): item is HTMLElement => Boolean(item));
  // The page's own reply box stays mounted (and is even re-mounted fresh) behind X's modal
  // composer. It has a size, so it passed `isVisible`, and it is not where anyone is writing:
  // see `isBehindXModal`.
  return Array.from(new Set(editable)).filter(composer => isVisible(composer) && !composerBehindXModal(composer));
}

function composerBehindXModal(composer: HTMLElement): boolean {
  return isBehindXModal({
    inDialog: Boolean(composer.closest("[role='dialog']")),
    ariaHidden: Boolean(composer.closest("[aria-hidden='true']")),
    composeRoute: isXComposeRoute(location.pathname),
  });
}

/** A composer Starling may type into right now: on the page, drawn, and not hidden behind X's modal. */
function isUsableComposer(composer: HTMLElement): boolean {
  return composer.isConnected && isVisible(composer) && !composerBehindXModal(composer);
}

function resolveEditableComposer(candidate: HTMLElement): HTMLElement | null {
  if (candidate.isContentEditable) return candidate;
  return candidate.querySelector<HTMLElement>('[contenteditable="true"]');
}

function readableComposerText(composer: HTMLElement): string {
  // Fresh X composers carry zero-width characters in their placeholder DOM;
  // strip them so an "empty" composer reads as truly empty.
  return (composer.innerText || composer.textContent || "")
    .replace(/[​-‍⁠﻿]/g, "")
    .replace(/\r\n/g, "\n")
    .trimEnd();
}

// "Format it" typed in the dock needs the draft it is meant to act on, but a
// chat request only ever carried { message, context, conversation_id } — so the
// text the user wanted re-spaced never reached the engine at all. Attach the
// open composer whenever there is one; the engine ignores it unless the
// message actually asks for formatting, and re-spacing never calls the model.
function openComposerPayload(): { action: ComposerAction; draft_text: string; intent: "enhance" } | undefined {
  const session = [...activeComposerSessions].find(candidate =>
    !candidate.disposed && candidate.composer.isConnected
    && readableComposerText(candidate.composer).trim().length > 0);
  if (!session) return undefined;
  return { action: session.action, draft_text: readableComposerText(session.composer), intent: "enhance" };
}

// Downscale one <img> to a small JPEG data-URL. The whole request goes to the
// gateway as one JSON body, which it refuses over 256 KiB, so each image is
// encoded at 768px / q0.6 first and then smaller and rougher until it fits in
// about 90 KB (shared/composer-images.ts). Returns null when even the smallest
// step does not fit, and on any failure (a CORS-tainted canvas, an unloaded
// image): an image is a bonus, never a blocker.
async function downscaleImageToDataUrl(img: HTMLImageElement): Promise<string | null> {
  try {
    if (!img.complete || !img.naturalWidth) {
      await new Promise<void>(resolve => {
        const done = () => resolve();
        img.addEventListener("load", done, { once: true });
        img.addEventListener("error", done, { once: true });
        window.setTimeout(done, 1500);
      });
    }
    const nw = img.naturalWidth || 0, nh = img.naturalHeight || 0;
    if (!nw || !nh) return null;
    const canvas = document.createElement("canvas");
    const ctx = canvas.getContext("2d");
    if (!ctx) return null;
    return await encodeWithinBudget(step => {
      const { width, height } = fitWithin(nw, nh, step.maxSide);
      if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width;
        canvas.height = height;
      }
      ctx.drawImage(img, 0, 0, width, height);
      return canvas.toDataURL("image/jpeg", step.quality);
    });
  } catch {
    return null;
  }
}

// Images the user has attached to THIS composer (not timeline media). X shows
// them as <img> with a blob: source inside the attachment preview near the
// compose box. Read up to two so the vision model can keep the text consistent
// with the picture on Enhance.
async function readComposerImages(composer: HTMLElement): Promise<string[]> {
  try {
    const scope = (composer.closest('[data-testid="primaryColumn"]')
      || composer.closest('[role="dialog"]')
      || composer.closest("form")
      || document) as ParentNode;
    const seen = new Set<string>();
    const imgs = Array.from(scope.querySelectorAll<HTMLImageElement>("img"))
      .filter(im => {
        const src = im.currentSrc || im.src || "";
        if (!src.startsWith("blob:") && !src.startsWith("data:")) return false;
        if (seen.has(src)) return false;
        seen.add(src);
        return true;
      });
    const out: string[] = [];
    for (const im of imgs.slice(0, MAX_COMPOSER_IMAGES)) {
      const url = await downscaleImageToDataUrl(im);
      if (url) out.push(url);
    }
    return out;
  } catch {
    return [];
  }
}

/** Every control Starling adds to X's own page (pills, launchers, ✦ buttons, nav item); not the dock. */
function removeInjectedControls(options: { keepNav?: boolean } = {}) {
  // Disable is a privacy boundary. Do not flush an edit as a side effect of
  // turning Starling off; remove all listeners from any live composer instead.
  for (const session of [...activeComposerSessions]) disposeComposerSession(session);
  composerSessions = new WeakMap<HTMLElement, ComposerSession>();
  for (const launcher of [...activeCollapsedComposerLaunchers]) disposeCollapsedComposerLauncher(launcher);
  clearComposerIntents();
  document.querySelectorAll<HTMLElement>("[data-starling-composer-control], [data-starling-collapsed-composer-launcher], [data-starling-post-control]")
    .forEach(control => control.remove());
  postButtonHosts = new WeakSet<HTMLElement>();
  // The nav item reads nothing from the page: before consent it is the way to the data notice.
  if (options.keepNav) return;
  document.getElementById(NAV_HOST_ID)?.remove();
  navButton = null;
}

function removeStarlingUi() {
  removeInjectedControls();
  dockCloseSequence += 1;
  dockCloseAnimation?.cancel();
  dockCloseAnimation = null;
  hideSidebarDockTarget();
  chatHost?.remove();
  // Full teardown: the dock (and every op card in it) is gone, so release the
  // op registry's timers directly — no events, nothing left to render into.
  for (const binding of opBindings.values()) {
    for (const timer of binding.timers) clearTimeout(timer);
    stopElapsedTicker(binding);
  }
  opBindings.clear();
  refusedDrafts = [];
  chatShadow = null;
  chatHost = null;
  // The next dock is a fresh one: leaving the old key here would make its first
  // paint look like a theme CHANGE and cross-fade an opening panel.
  appliedDockTheme = "";
  if (themeFadeTimer != null) { window.clearTimeout(themeFadeTimer); themeFadeTimer = null; }
  dockPlacement = "sidebar";
  activeChatStream = null;
  try { assistPort?.disconnect(); } catch { /* already gone */ }
  assistPort = null;
}

// A native-feeling destination in X's left rail. The sparkle is the only
// branded gesture; the row otherwise follows X's quiet icon-and-label rhythm.
const navStyles = `
  :host { display:block; color:#e7e9ea; font-family:"TwitterChirp",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
  * { box-sizing:border-box; }
  /* Replicate X's nav item exactly: icon (26.25px) + label (20px), the icon
     sits 12px from the left so it lines up in the same column as Home/Explore,
     and vertical padding of 12px makes the ~50px row. The hover pill hugs the
     content and curves off after the label. */
  .vf-nav { display:inline-flex; align-items:center; gap:16px; width:fit-content; max-width:100%; padding:12px 24px 12px 12px; border:0; border-radius:9999px; background:transparent; color:inherit; cursor:pointer; font-family:inherit; font-size:20px; font-weight:400; line-height:24px; text-align:left; transition:background-color .12s ease; -webkit-tap-highlight-color:transparent; }
  /* Hover ALWAYS shows the pill — including on the active item, exactly like X. */
  .vf-nav:hover { background:rgba(231,233,234,.1); }
  /* Active (card open) = bold label only, NO persistent pill (like X's current
     nav item). The pill still appears on hover via the rule above. */
  .vf-nav[data-open="true"] .vf-nav-label { font-weight:700; }
  .vf-nav-mark { display:inline-flex; flex:0 0 26.25px; align-items:center; justify-content:center; width:26.25px; height:26.25px; line-height:1; }
  .vf-nav-mark svg { display:block; width:26.25px; height:26.25px; fill:currentColor; }
  .vf-nav-label { white-space:nowrap; }
  /* Off on this page: dimmed, and the label says so. Clicking turns Starling back on here. */
  .vf-nav[data-off="true"] { opacity:.55; }
  .vf-nav[data-off="true"]:hover, .vf-nav[data-off="true"]:focus-visible { opacity:.9; }
  :host([data-theme="light"]) { color:#0f1419; }
  :host([data-theme="light"]) .vf-nav:hover { background:rgba(15,20,25,.1); }
  .vf-nav:focus-visible { outline:2px solid currentColor; outline-offset:2px; }
  @media (max-width:1260px) { .vf-nav { justify-content:center; width:50.25px; padding:12px; gap:0; } .vf-nav-label { display:none; } }
  @media (prefers-reduced-motion:reduce) { .vf-nav { transition:none; } }
`;

// Grok-flat visual language: X's own dark/light surfaces, hairline borders,
// card-based conversation, actions living INSIDE cards as chips.
// Solid black-and-white, bold. No glass. The only color is the health dot.
const dockStyles = `
  /* X's OWN design tokens: Chirp typeface, #e7e9ea text (never pure white),
     #71767b secondary, #2f3336 hairlines, #536471 outline-button borders, and
     rgba white washes for hover — so the panel reads as part of X, not a
     foreign app pasted on top. */
  :host { display:block; --bg:#000000; --panel:#000000; --fg:#e7e9ea; --muted:#71767b; --line:#2f3336; --edge:#2f3336; --soft:rgba(255,255,255,.03); --soft-solid:#080808; --btnline:#536471; --wash:rgba(239,243,244,.1); --ok:#00ba7c; --xblue:#1d9bf0; --elev:0 28px 80px rgba(0,0,0,.55), 0 2px 8px rgba(0,0,0,.4); font-family:"TwitterChirp","Chirp",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
  /* Light mode used to inherit the dark elevation — 55% black under a white
     card, which reads as a smudge rather than as height — and drew every
     control border in #eff3f4, X's DIVIDER colour, which on white is 1.05:1 and
     so effectively invisible. Hence two tokens: --line stays the divider,
     --edge is what a panel or an input is allowed to be seen with (X's own
     #cfd9de). In dark they are the same colour, so nothing there moves. */
  :host([data-theme="light"]) { --bg:#ffffff; --panel:#ffffff; --fg:#0f1419; --muted:#536471; --line:#eff3f4; --edge:#cfd9de; --soft:#f7f9f9; --soft-solid:#f7f9f9; --btnline:#cfd9de; --wash:rgba(15,20,25,.1); --ok:#00ba7c; --xblue:#1d9bf0; --elev:0 20px 48px rgba(15,20,25,.13), 0 2px 6px rgba(15,20,25,.08); }
  /* X's Dim, which is neither of the above: a pure-black panel dropped onto
     #15202b looks like a hole punched in the page, so the dock borrows Dim's
     own slate, its lighter hairline (#38444d) and its brighter text. */
  :host([data-tone="dim"]) { --bg:#15202b; --panel:#15202b; --fg:#f7f9f9; --muted:#8b98a5; --line:#38444d; --edge:#38444d; --soft:rgba(255,255,255,.04); --soft-solid:#1e2934; --btnline:#5c6e7e; --wash:rgba(247,249,249,.1); --elev:0 24px 64px rgba(0,0,0,.45), 0 2px 8px rgba(0,0,0,.3); }
  * { box-sizing:border-box; margin:0; } button, textarea { font:inherit; }
  .panel { position:relative; display:flex; flex-direction:column; width:min(468px,calc(100vw - 32px)); height:min(640px,calc(100vh - 96px)); min-width:320px; min-height:400px; color:var(--fg); background:var(--panel); border:1px solid var(--edge); border-radius:16px; box-shadow:var(--elev); overflow:hidden; transform-origin:top right; }
  /* The right-rail dock is deliberately a fixed, quiet tool window. Its feed
     scrolls inside this frame; a loading/result card never stretches the dock
     or pushes the controls away from where the author expects them. */
  :host([data-placement="sidebar"]) .panel { width:100%; height:min(430px,calc(100vh - 24px)); min-width:0; min-height:0; margin:12px 0; box-shadow:none; transform-origin:top center; }
  /* AN ACCOUNT NOTICE NEEDS ROOM. "You've used all 25 drafts", "You were signed out" and the
     consent card are about 150px tall, and in X's sidebar the panel is only 430px: the
     conversation was left a 50px slot that showed the bottom edge of its last card under the
     notice, a second half-row of "See plans / View usage" buttons and a line of clipped text.
     Found by the first browser journey that ran a customer out of drafts. The panel grows while a
     notice is showing, so the notice and the last card are both readable. */
  :host([data-placement="sidebar"]) .panel:has(> .state:not([hidden])) { height:min(590px,calc(100vh - 24px)); }
  /* One message, not two: while the account notice shows, a refused request's card is one muted
     line pointing at it; with no notice on screen (put away with "Not now"), the full words and
     buttons come back. Decided from what is on screen, so the two can never disagree. */
  .refusal-full { display:flex; flex-direction:column; gap:9px; }
  .feed [data-account-refusal] .refusal-short { display:none; }
  .panel:has(> .state:not([hidden])) .feed [data-account-refusal] .refusal-short { display:flex; }
  .panel:has(> .state:not([hidden])) .feed [data-account-refusal] .refusal-full { display:none; }
  .panel.opening { animation:starling-pop .26s ease-out; }
  .panel.closing { pointer-events:none; }
  :host([data-placement="sidebar"]) .panel.detach-ready { box-shadow:0 10px 28px rgba(0,0,0,.22); }
  .panel.dock-targeting { box-shadow:0 0 0 3px rgba(29,155,240,.18), 0 24px 64px rgba(0,0,0,.42); }
  .panel.docking { animation:starling-pop .26s ease-out; }
  :host([data-placement="floating"]) .panel.dragging { transform:scale(1.02); box-shadow:0 44px 100px rgba(0,0,0,.62); cursor:grabbing; }
  /* Lifted states are drawn in near-opaque black, which on a white page is a
     grey smear rather than height. Light mode gets its own, shorter, softer
     versions — same gesture, believable on paper. */
  :host([data-theme="light"]) .panel.detach-ready { box-shadow:0 8px 22px rgba(15,20,25,.14); }
  :host([data-theme="light"]) .panel.dock-targeting { box-shadow:0 0 0 3px rgba(29,155,240,.22), 0 18px 44px rgba(15,20,25,.14); }
  :host([data-theme="light"][data-placement="floating"]) .panel.dragging { box-shadow:0 32px 72px rgba(15,20,25,.2); }
  .bar { display:flex; align-items:center; gap:9px; padding:14px 16px; border-bottom:1px solid var(--line); cursor:grab; user-select:none; touch-action:none; }
  .bar:active { cursor:grabbing; }
  :host([data-placement="sidebar"]) .bar { touch-action:pan-y; }
  .bar.holding { background:var(--soft); cursor:grabbing; }
  .bar strong { font-size:15px; font-weight:800; letter-spacing:-.01em; }
  /* Fixed box with overflow hidden so a wordmark of any aspect ratio cannot
     push the health dot and the close button around as the bar reflows. */
  .bar .starling-wordmark { display:inline-flex; align-items:center; flex:none; width:120px; height:24px; overflow:hidden; user-select:none; -webkit-user-select:none; }
  /* pointer-events:none hands every press on the letters straight to .bar, which
     owns the drag. The image is decorative — the <strong>'s aria-label carries
     the name — so there is nothing on it left to click.

     The wordmark PNG has NO alpha channel (colour type 2): it is white letters
     baked onto a solid black rectangle. On the black panel that was invisible,
     which is why it survived unnoticed — but on Dim and on white it drew a
     black box around the name. Screen blending drops black to the backdrop and
     leaves the letters, so the mark sits on the panel. Light mode inverts
     first and multiplies instead, which is the same trick mirrored: white goes
     transparent and the letters come through dark.

     Done in CSS on purpose. Re-exporting the asset with real transparency would
     be the tidier fix, but that is a brand file, and this needs no new export
     and cannot alter a single letterform. */
  .bar .starling-wordmark img { display:block; width:100%; height:100%; object-fit:contain; pointer-events:none; -webkit-user-drag:none; user-select:none; mix-blend-mode:screen; }
  :host([data-theme="light"]) .bar .starling-wordmark img { filter:invert(1); mix-blend-mode:multiply; }
  .dot { width:8px; height:8px; border-radius:999px; background:var(--muted); flex:none; }
  .dot[data-health="ok"] { background:var(--ok); box-shadow:0 0 0 3px rgba(0,186,124,.16); } .dot[data-health="down"] { background:#f4212e; box-shadow:0 0 0 3px rgba(244,33,46,.16); } .dot[data-health="warn"] { background:#ffd400; box-shadow:0 0 0 3px rgba(255,212,0,.18); }
  .dock-hint { display:none; }
  /* The model name used to sit here. It is gone from the header on purpose —
     which model is writing is a Settings question, and the answer was taking up
     the header every second of every session to be read approximately never.
     Settings' status card and the footer's mode line both still say it.
     The gear inherits the margin-left:auto that the badge was carrying, or both
     header buttons slide left and sit against the wordmark. */
  .gear { margin-left:auto; flex:none; width:28px; height:28px; border:0; border-radius:999px; background:transparent; color:var(--muted); cursor:pointer; display:grid; place-items:center; transition:background .2s cubic-bezier(.22,1,.36,1), color .2s cubic-bezier(.22,1,.36,1); }
  .gear svg { width:16px; height:16px; display:block; }
  .gear:hover, .gear[aria-expanded="true"] { background:var(--wash); color:var(--fg); }
  .close { flex:none; width:28px; height:28px; border:0; border-radius:999px; background:transparent; color:var(--muted); font-size:20px; cursor:pointer; display:grid; place-items:center; }
  .close:hover { background:var(--wash); color:var(--fg); }
  /* Settings replaces the conversation rather than covering it: the sidebar
     panel is only ~430px tall, so an overlay would hide everything anyway, and
     this keeps the header — and therefore the way out — on screen. */
  .settings { flex:1; min-height:0; overflow-y:auto; scrollbar-width:none; padding:14px 16px 18px; flex-direction:column; gap:20px; }
  .settings::-webkit-scrollbar { display:none; }
  /* The author display:flex below beats the UA's [hidden]{display:none} — the
     same trap documented above for .context-chip. Without this rule the panel
     paints an empty padded box under the header. */
  .settings[hidden] { display:none; }
  .panel[data-view="settings"] .settings { display:flex; }
  .panel[data-view="settings"] .feed,
  .panel[data-view="settings"] .composer,
  .panel[data-view="settings"] .context-chip { display:none; }
  /* flex:0 0 auto for the same reason the feed's cards need it: children of a
     scrolling flex column otherwise lose their natural height and compress. */
  .set-group { flex:0 0 auto; display:flex; flex-direction:column; gap:12px; }
  /* One entrance, on open only — the two sections lift in a beat apart. The
     class is stripped once it has played, so toggling a control inside the
     panel never re-animates anything. Re-running this on every change is what
     makes a settings screen feel busy. */
  .settings.entering > .set-group { animation:set-in .34s cubic-bezier(.22,1,.36,1) backwards; }
  .settings.entering > .set-group:nth-child(2) { animation-delay:.06s; }
  .settings.entering > .set-group:nth-child(3) { animation-delay:.12s; }
  @keyframes set-in { from { opacity:0; transform:translateY(7px); } to { opacity:1; transform:none; } }
  /* Section headers rule out to the right — the panel is short, so the rule is
     what separates sections without spending vertical space on padding. */
  .set-title { display:flex; align-items:center; gap:10px; font-size:10.5px; font-weight:800; letter-spacing:.11em; text-transform:uppercase; color:var(--muted); }
  .set-title::after { content:""; flex:1; height:1px; background:var(--line); }
  .set-field { display:flex; flex-direction:column; gap:6px; }
  .set-label { font-size:10.5px; font-weight:800; letter-spacing:.07em; text-transform:uppercase; color:var(--muted); }
  .set-help { font-size:11.5px; line-height:1.5; color:var(--muted); }
  /* Label left, switch hard right. The whole row is the <label>, so the caption
     is part of the hit target and the cursor says so across all of it. */
  .set-row { display:flex; align-items:center; gap:12px; cursor:pointer; }
  .set-row .set-label { cursor:inherit; }
  .set-row .sw { margin-left:auto; }
  /* An undo that only exists once there is something to undo, so it reads as
     quiet text rather than as a third control competing with the switch. */
  .set-link { align-self:flex-start; padding:0; border:0; background:transparent; color:var(--muted); font:inherit; font-size:11.5px; font-weight:700; text-decoration:underline; text-underline-offset:2px; cursor:pointer; transition:color .2s cubic-bezier(.22,1,.36,1); }
  .set-link:hover { color:var(--fg); }
  .set-link:focus-visible { outline:2px solid var(--fg); outline-offset:2px; border-radius:4px; }
  /* Your own material. Borrows .set-input's shape rather than inventing a
     second field vocabulary — a settings screen that mixes them reads as two
     settings screens (the same reasoning as toggleSwitch). The textarea and
     its Add sit on one row so the control never drifts from the box it acts
     on, and the entries below are rows rather than cards: they are short, and
     a card each would push the two inputs off a phone screen. */
  .mat-add { display:flex; align-items:flex-start; gap:8px; }
  .mat-input { flex:1; min-width:0; box-sizing:border-box; padding:9px 11px; border-radius:10px; border:1px solid var(--edge); background:var(--bg); color:var(--fg); font:inherit; font-size:13px; font-weight:600; line-height:1.5; resize:vertical; transition:border-color .2s cubic-bezier(.22,1,.36,1); }
  .mat-input::placeholder { color:var(--muted); font-weight:500; }
  .mat-input:hover { border-color:var(--btnline); }
  .mat-input:focus-visible { outline:2px solid var(--fg); outline-offset:1px; }
  .mat-add .set-link { align-self:center; }
  /* Only rendered when Starling refused, so it is never empty space. */
  .mat-problem:empty { display:none; }
  .mat-list { display:flex; flex-direction:column; gap:6px; }
  .mat-list:empty { display:none; }
  .mat-row { display:flex; align-items:flex-start; gap:10px; padding:8px 10px; border-radius:10px; border:1px solid var(--line); background:var(--bg); }
  .mat-body { flex:1; min-width:0; display:flex; flex-direction:column; gap:3px; }
  .mat-tag { font-size:9.5px; font-weight:800; letter-spacing:.09em; text-transform:uppercase; color:var(--muted); }
  /* Wraps rather than truncates: he needs to recognise which entry this is,
     and an ellipsis on a three-word note hides the whole note. */
  .mat-text { font-size:12.5px; line-height:1.5; color:var(--fg); overflow-wrap:anywhere; }
  .mat-drop { flex:none; padding:0; border:0; background:transparent; color:var(--muted); font:inherit; font-size:11.5px; font-weight:700; cursor:pointer; transition:color .2s cubic-bezier(.22,1,.36,1); }
  .mat-drop:hover { color:var(--fg); }
  .mat-drop:focus-visible { outline:2px solid var(--fg); outline-offset:2px; border-radius:4px; }
  .set-input { width:100%; box-sizing:border-box; padding:9px 11px; border-radius:10px; border:1px solid var(--edge); background:var(--bg); color:var(--fg); font:inherit; font-size:13px; font-weight:600; transition:border-color .2s cubic-bezier(.22,1,.36,1); }
  .set-input::placeholder { color:var(--muted); font-weight:500; }
  .set-input:hover { border-color:var(--btnline); }
  .set-input:focus-visible { outline:2px solid var(--fg); outline-offset:1px; }
  .set-warn { border:1px solid var(--edge); border-left:3px solid var(--fg); border-radius:12px; padding:11px 13px; background:var(--soft); font-size:11.5px; line-height:1.5; color:var(--muted); }
  /* The one sentence in this panel the user actually has to read was also the
     dimmest thing on screen — muted over --soft measures 4.38:1, under AA. The
     lead sentence is primary; the detail stays calm. */
  .set-warn-lead { color:var(--fg); font-weight:800; }
  .set-status { font-size:11.5px; line-height:1.45; color:var(--muted); }
  .set-status.ok { color:var(--ok); }
  .set-status.error { color:#f4212e; }
  .set-actions { display:flex; align-items:center; gap:8px; }
  .set-actions .copy-pill { margin-left:auto; }

  /* The status card: what is writing, right now. Left rule tints like the
     context chip — neutral normally, red when the engine is down. */
  .set-now { display:flex; align-items:center; gap:10px; padding:12px 13px; border:1px solid var(--edge); border-left:3px solid var(--fg); border-radius:14px; background:var(--soft); }
  .set-now[data-state="down"] { border-left-color:#f4212e; }
  .set-now-text { min-width:0; display:flex; flex-direction:column; gap:2px; }
  .set-now-model { font-size:13.5px; font-weight:800; letter-spacing:-.01em; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .set-now-where { font-size:11px; font-weight:600; color:var(--muted); }

  /* Segmented control. The indicator slides rather than the fill jumping —
     with two options it is the cheapest way to read as one control instead of
     two buttons that happen to sit together. */
  .seg { position:relative; display:flex; padding:3px; border:1px solid var(--edge); border-radius:999px; background:var(--bg); }
  .seg-ind { position:absolute; top:3px; bottom:3px; left:3px; width:calc(50% - 3px); border-radius:999px; background:var(--fg); transition:transform .24s cubic-bezier(.22,1,.36,1); }
  .seg[data-active="1"] .seg-ind { transform:translateX(100%); }
  .seg-btn { position:relative; z-index:1; flex:1; padding:7px 10px; border:0; border-radius:999px; background:transparent; color:var(--muted); font:inherit; font-size:12.5px; font-weight:700; cursor:pointer; transition:color .24s cubic-bezier(.22,1,.36,1); }
  .seg-btn[aria-pressed="true"] { color:var(--panel); }
  .seg-btn:focus-visible { outline:2px solid var(--fg); outline-offset:2px; }

  /* ---- switches ----------------------------------------------------------
     Two of them, one skeleton. Both are 3.8em x 2.2em with a 1.2em thumb at a
     .5em inset, so the travel is the same 1.6em and they read as siblings
     rather than as two widgets from two libraries. The em base is the only
     size either one needs.

     The thumb is the track's ::before rather than a node of its own: there is
     nothing to keep in sync, and :checked on the real <input> drives both. */
  .sw { position:relative; display:inline-block; flex:none; width:3.8em; height:2.2em; font-size:15px; border-radius:999px; }
  .sw input { position:absolute; width:0; height:0; opacity:0; }
  .sw-track { position:absolute; inset:0; cursor:pointer; border-radius:999px; overflow:hidden; transition:background .4s cubic-bezier(.22,1,.36,1), box-shadow .4s cubic-bezier(.22,1,.36,1); }
  .sw-track::before { content:""; position:absolute; left:.5em; bottom:.5em; width:1.2em; height:1.2em; border-radius:999px; transition:transform .4s cubic-bezier(.81,-.04,.38,1.5), background .4s cubic-bezier(.22,1,.36,1), box-shadow .4s cubic-bezier(.22,1,.36,1); }
  .sw input:checked + .sw-track::before { transform:translateX(1.6em); }
  .sw input:focus-visible + .sw-track { outline:2px solid var(--fg); outline-offset:2px; }
  /* Only while a theme swap is in flight, and never for longer. The universal
     selector is deliberate: every surface in here takes its colour from the
     tokens, and the ones that would snap are precisely the ones nobody thought
     to list. The switches are cut out because transition is a SHORTHAND —
     applying this to them would replace the thumb's transform transition and the
     star and cloud fades, so the control would stop moving at the exact moment
     it is meant to. (A pseudo-element is safe either way: the universal selector
     never matches one, which is why the thumb keeps its own bouncy travel.) */
  .panel.theming, .panel.theming :not(.sw):not(.sw *) { transition:background-color .34s cubic-bezier(.22,1,.36,1), color .34s cubic-bezier(.22,1,.36,1), border-color .34s cubic-bezier(.22,1,.36,1), box-shadow .34s cubic-bezier(.22,1,.36,1), fill .34s cubic-bezier(.22,1,.36,1) !important; }

  /* On/off. Deliberately monochrome: the track fills with --fg exactly as the
     segmented indicator above does, because this control replaced one of those
     and the panel has one colour story (the health dot) already. */
  .sw-flag .sw-track { background:var(--bg); box-shadow:inset 0 0 0 1px var(--edge); }
  .sw-flag .sw-track::before { background:var(--muted); }
  .sw-flag input:checked + .sw-track { background:var(--fg); box-shadow:inset 0 0 0 1px var(--fg); }
  .sw-flag input:checked + .sw-track::before { background:var(--panel); }

  /* Day/night. The one control allowed colour, because "which theme" is the
     one setting whose value IS a colour — a monochrome version of this would
     be a switch that refuses to say what it does. Night is X's own Dim slate,
     day is X's blue.

     The thumb is both bodies: an inset shadow pushed 8px right carves a
     crescent out of the circle, and growing the SAME shadow's spread to 15px
     floods it back into a full sun. One element, no sprite, no second node.
     After Uiverse.io by JustCode14 (MIT), repainted in X's palette. */
  /* Night is a fixed deep navy rather than Dim's own #15202b, and it is ringed
     in --edge rather than in a white wash: on a Dim panel the slate-on-slate
     version disappeared completely and left a moon floating in the margin. */
  .sw-theme .sw-track { background:#0d1520; box-shadow:inset 0 0 0 1px var(--edge); }
  .sw-theme .sw-track::before { box-shadow:inset 8px -4px 0 0 #e7e9ea; }
  .sw-theme input:checked + .sw-track { background:var(--xblue); box-shadow:inset 0 0 0 1px rgba(15,20,25,.08); }
  .sw-theme input:checked + .sw-track::before { box-shadow:inset 15px -4px 0 15px #ffd45e; }
  .sw-star { position:absolute; width:4px; height:4px; border-radius:50%; background:#fff; opacity:1; transition:opacity .3s cubic-bezier(.22,1,.36,1), transform .4s cubic-bezier(.22,1,.36,1); }
  .sw-star-1 { left:2.3em; top:.5em; }
  .sw-star-2 { left:2.0em; top:1.3em; }
  .sw-star-3 { left:2.85em; top:.95em; }
  /* The stars do not just vanish, they fall back a little as the sun arrives —
     without that the sky changes colour and three dots blink out separately. */
  .sw-theme input:checked + .sw-track .sw-star { opacity:0; transform:translateY(3px) scale(.4); }
  .sw-cloud { position:absolute; left:-1.1em; bottom:-1.45em; width:3.5em; opacity:0; transition:opacity .4s cubic-bezier(.22,1,.36,1), transform .5s cubic-bezier(.22,1,.36,1); transform:translateY(4px); }
  .sw-theme input:checked + .sw-track .sw-cloud { opacity:1; transform:none; }

  /* Custom dropdown. A native <select> renders an OS menu that ignores every
     token above and looks pasted on; this is the same control drawn in the
     panel's own language. */
  .dd { position:relative; }
  .dd-btn { width:100%; box-sizing:border-box; display:flex; align-items:center; gap:8px; padding:9px 11px; border:1px solid var(--line); border-radius:10px; background:var(--bg); color:var(--fg); font:inherit; font-size:13px; font-weight:600; cursor:pointer; text-align:left; }
  .dd-btn { transition:border-color .2s cubic-bezier(.22,1,.36,1); }
  .dd-btn:hover { border-color:var(--btnline); }
  .dd-btn:focus-visible { outline:2px solid var(--fg); outline-offset:1px; }
  .dd[data-open="1"] .dd-btn { border-color:var(--fg); }
  .dd-val { flex:1; min-width:0; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .dd-caret { flex:none; width:11px; height:11px; color:var(--muted); transition:transform .24s cubic-bezier(.22,1,.36,1); }
  .dd[data-open="1"] .dd-caret { transform:rotate(180deg); }
  .dd-list { position:absolute; z-index:6; top:calc(100% + 5px); left:0; right:0; max-height:190px; overflow-y:auto; overscroll-behavior:contain; scrollbar-width:none; padding:5px; margin:0; list-style:none; border:1px solid var(--line); border-radius:12px; background:var(--panel); box-shadow:0 18px 44px rgba(0,0,0,.5), 0 2px 8px rgba(0,0,0,.35); animation:dd-in .18s cubic-bezier(.22,1,.36,1); }
  :host([data-theme="light"]) .dd-list { box-shadow:0 18px 44px rgba(15,20,25,.16), 0 2px 8px rgba(15,20,25,.12); }
  :host([data-theme="light"]) .tip { box-shadow:0 10px 28px rgba(15,20,25,.22); }
  .dd-list::-webkit-scrollbar { display:none; }
  /* Flipped upward when there is no room below — the panel is ~430px tall in
     the rail, so a dropdown near the bottom would otherwise open into nothing. */
  .dd-list.up { top:auto; bottom:calc(100% + 5px); }
  .dd-opt { display:flex; align-items:center; gap:8px; padding:8px 9px; border-radius:8px; transition:background .16s ease; font-size:13px; font-weight:600; color:var(--fg); cursor:pointer; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .dd-opt:hover, .dd-opt.active { background:var(--wash); }
  .dd-opt[aria-selected="true"] { font-weight:800; }
  .dd-tick { flex:none; width:12px; height:12px; opacity:0; }
  .dd-opt[aria-selected="true"] .dd-tick { opacity:1; }
  .dd-opt-sub { margin-left:auto; padding-left:8px; font-size:11px; font-weight:600; color:var(--muted); }
  @keyframes dd-in { from { opacity:0; transform:translateY(-4px); } to { opacity:1; transform:none; } }

  /* Typing speed. A slider because the thing being chosen is a rate, and the
     sample underneath types at whatever is selected — the setting demonstrates
     itself instead of asking the user to guess what "Fast" means. */
  .spd { display:flex; flex-direction:column; gap:10px; }
  .spd-head { display:flex; align-items:baseline; gap:8px; }
  .spd-name { font-size:13.5px; font-weight:800; letter-spacing:-.01em; }
  .spd-rate { margin-left:auto; font-size:11px; font-weight:600; color:var(--muted); font-variant-numeric:tabular-nums; }
  /* The native range thumb cannot be transitioned — it teleports between
     steps. So the real input is kept for dragging, keyboard and focus, made
     invisible, and a thumb that CAN ease is drawn under it. Every moving part
     here shares one curve and one duration so the control glides as a whole
     rather than as four things that happen to move at once. */
  .spd-rail { position:relative; height:20px; display:flex; align-items:center; }
  .spd-rail > span { pointer-events:none; }
  .spd-track { position:absolute; left:0; right:0; height:4px; border-radius:999px; background:var(--line); }
  .spd-fill { position:absolute; left:0; height:4px; border-radius:999px; background:var(--fg); transition:width .24s cubic-bezier(.22,1,.36,1); }
  /* Inset by 7px (the thumb's travel) MINUS half a tick, so each dot's centre
     lands exactly under a thumb stop instead of 2px off it. */
  .spd-ticks { position:absolute; left:5px; right:5px; display:flex; justify-content:space-between; }
  .spd-tick { width:4px; height:4px; border-radius:999px; background:var(--line); transition:background .24s ease; }
  .spd-thumbrail { position:absolute; left:7px; right:7px; height:0; }
  .spd-thumb { position:absolute; top:-8px; left:0; width:16px; height:16px; margin-left:-8px; border-radius:999px; background:var(--fg); border:3px solid var(--panel); box-shadow:0 0 0 1px var(--fg); transition:left .24s cubic-bezier(.22,1,.36,1), transform .18s cubic-bezier(.22,1,.36,1), box-shadow .18s ease; }
  .spd-input { position:absolute; inset:0; width:100%; height:100%; margin:0; padding:0; z-index:3; opacity:0; cursor:pointer; -webkit-appearance:none; appearance:none; background:transparent; }
  .spd-input:hover ~ .spd-thumbrail .spd-thumb { transform:scale(1.1); }
  .spd-input:active ~ .spd-thumbrail .spd-thumb { transform:scale(1.22); }
  .spd-input:focus-visible ~ .spd-thumbrail .spd-thumb { box-shadow:0 0 0 1px var(--fg), 0 0 0 5px var(--wash); }
  .spd-demo { min-height:17px; font-size:11.5px; line-height:1.45; color:var(--muted); }
  .spd-demo::after { content:"▍"; margin-left:1px; animation:spd-caret 1s steps(1,end) infinite; }
  @keyframes spd-caret { 50% { opacity:0; } }

  /* Tooltips. The native title= attribute renders an OS box after a ~1s delay,
     in the desktop's font, outside the shadow root and outside every token
     here — on a black X card it reads as a bug. This is the same affordance,
     inverted like .copy-pill so it separates from the panel without a colour. */
  .tip { position:absolute; z-index:30; max-width:210px; padding:6px 9px; border-radius:8px; background:var(--fg); color:var(--panel); font-size:11px; font-weight:600; line-height:1.4; pointer-events:none; opacity:0; transform:translateY(-3px) scale(.97); transform-origin:top center; transition:opacity .16s cubic-bezier(.22,1,.36,1), transform .16s cubic-bezier(.22,1,.36,1); box-shadow:0 10px 28px rgba(0,0,0,.45); }
  .tip.show { opacity:1; transform:none; }
  .tip[data-place="above"] { transform:translateY(3px) scale(.97); transform-origin:bottom center; }
  .tip[data-place="above"].show { transform:none; }
  .tip::after { content:""; position:absolute; left:var(--tip-arrow,12px); width:7px; height:7px; background:var(--fg); transform:rotate(45deg); border-radius:1px; }
  .tip[data-place="below"]::after { top:-3px; }
  .tip[data-place="above"]::after { bottom:-3px; }
  .context-chip { display:flex; align-items:center; gap:11px; margin:14px 14px 0; padding:11px 13px; border:1px solid var(--line); border-left:3px solid var(--fg); border-radius:16px; background:var(--soft); overflow:hidden; }
  /* The author display:flex above overrides the UA's [hidden]{display:none} —
     without this rule the "hidden" chip still painted as an empty rounded bar
     with a white border-arc (the mystery lozenge in the dock). */
  .context-chip[hidden] { display:none; }
  .context-chip[data-state="working"] { border-left-color:var(--muted); }
  .context-chip[data-state="updated"] { border-left-color:var(--ok); }
  .context-chip[data-state="error"] { border-left-color:#f4212e; }
  .chip-avatar { flex:none; width:34px; height:34px; border-radius:999px; object-fit:cover; background:var(--bg); }
  .chip-text { min-width:0; flex:1; display:flex; flex-direction:column; gap:2px; }
  .chip-line { display:flex; align-items:center; gap:5px; font-size:12.5px; font-weight:700; }
  .chip-icon { flex:none; font-weight:800; } .chip-label { flex:none; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .chip-excerpt { color:var(--muted); font-size:12.5px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  /* overflow-anchor:none — Starling owns this scroll position (the chat anchor);
     the browser's own scroll anchoring would fight the pinned scrollTop while
     cards stream and resize. scrollbar-width is the standards path Chromium
     actually honors (the webkit rule alone left Windows stepper buttons). */
  /* position:relative makes the feed the offsetParent of every card, so
     applyFeedAnchor can measure a card's position with offsetTop — a
     scroll-independent, transform-independent number. Do not remove it. */
  .feed { position:relative; flex:1; min-height:0; overflow-y:auto; overflow-anchor:none; scrollbar-width:none; padding:18px 16px; display:flex; flex-direction:column; gap:14px; }
  .feed::-webkit-scrollbar { display:none; }
  /* ...and on a window too short for that, a slot that cannot show a whole card shows none: a
     sliver of a card reads as a rendering fault, an empty slot does not. */
  .feed { container:starling-feed / size; }
  @container starling-feed (max-height: 84px) { .feed > :not(.empty-hint) { visibility:hidden; } }
  .feed-spacer { flex:0 0 auto; width:100%; height:0; min-height:0; pointer-events:none; }
  :host([data-placement="sidebar"]) .feed { min-height:0; max-height:none; }
  .empty-hint { display:flex; flex-direction:column; align-items:center; gap:10px; color:var(--muted); font-size:12.5px; line-height:1.5; margin:auto 4px; text-align:center; }
  .suggest { display:flex; flex-wrap:wrap; justify-content:center; gap:7px; }
  .suggest-btn { border:1px solid var(--btnline); border-radius:999px; padding:6px 12px; background:transparent; color:var(--fg); font-size:12.5px; font-weight:600; cursor:pointer; transition:background .16s ease; }
  .suggest-btn:hover { background:var(--wash); }
  /* Not while a notice stands between the customer and a draft: the suggestions could not be sent. */
  .panel:has(> .state:not([hidden])) .suggest { display:none; }
  /* X cards: hairline border, 16px radius, TRANSPARENT fill (X never tints
     card interiors) — the panel background shows through, like a timeline post. */
  /* flex:0 0 auto is LOAD-BEARING. The feed is a flex column, so a card is a
     flex item and defaults to flex-shrink:1. Cards that set overflow:hidden
     (.card.decision, attachments) lose their automatic min-height, so once the
     conversation overflowed the panel the flex algorithm was free to compress
     them — that is the card that "gets squished" and the one that shrinks to
     nothing and looks like it never rendered. Cards must keep their natural
     height and let the FEED scroll. */
  .card { flex:0 0 auto; border:1px solid var(--line); border-radius:16px; background:transparent; padding:12px 14px; display:flex; flex-direction:column; gap:9px; animation:starling-pop .26s ease-out; }
  /* Loading / status: a compact bubble that hugs its content — never a
     full-width empty box with three dots stranded in the corner. */
  .card.loading { align-self:flex-start; width:fit-content; min-width:min(230px,100%); max-width:100%; border-bottom-left-radius:4px; gap:0; }
  /* A loading card that is showing the words as they arrive takes the full width. */
  .card.loading:has(> .op-preview) { width:100%; }
  /* Each card names its own post in one quiet line, so an older note is never read as advice
     about the newest post. */
  .card-target { font-size:11px; font-weight:600; line-height:1.35; color:var(--muted); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .card.loading > .card-target { margin-bottom:6px; }
  .card.decision > .card-target { padding:11px 15px 0; }
  /* Folded when a newer ✦ starts: only the header line, which opens it again. */
  .card.collapsed { padding:8px 14px; gap:0; animation:none; }
  .card.collapsed > :not(.card-target) { display:none !important; }
  .card.collapsed > .card-target { cursor:pointer; margin:0; }
  .card.collapsed > .card-target:hover { color:var(--fg); }
  .card.decision.collapsed { padding:0; }
  .card.decision.collapsed > .card-target { padding:9px 15px; }
  .card-title { font-size:14px; font-weight:800; line-height:1.35; }
  .note-label { color:var(--fg); font-weight:700; }
  .sources { margin:8px 0 0; font-size:12px; line-height:1.5; color:var(--muted); overflow-wrap:anywhere; }
  .card.decision .sources { margin:0; padding:0 15px 14px; }
  .sources a { color:var(--fg); text-decoration:underline; text-underline-offset:2px; }
  .sources a:focus-visible { outline:2px solid var(--fg); outline-offset:2px; border-radius:2px; }
  .voice-note { margin:8px 0 0; font-size:12px; line-height:1.5; color:var(--muted); }
  .card.decision .voice-note { margin:0; padding:0 15px 14px; }
  .note-btn { font:inherit; font-weight:700; color:var(--fg); background:transparent; border:0; padding:0; cursor:pointer; text-decoration:underline; text-underline-offset:2px; }
  .note-btn:focus-visible { outline:2px solid var(--fg); outline-offset:2px; border-radius:2px; }
  .card.decision { background:transparent; padding:0; gap:0; overflow:hidden; }
  .card.decision .call { padding:13px 15px; }
  .card.decision .draft-section { border-top:1px solid var(--line); background:var(--soft); padding:14px 15px; display:flex; flex-direction:column; }
  .card.decision .wait-reason { padding:14px 15px; margin:0; }
  .card.decision .why-detail { padding:0 15px 13px; }
  .card.decision .chips { padding:0 15px 14px; }
  .bubble { max-width:82%; font-size:13.5px; line-height:1.45; white-space:pre-wrap; }
  .bubble.user { align-self:flex-end; background:var(--fg); border-color:var(--fg); color:var(--panel); font-weight:600; padding:9px 14px; border-radius:16px 16px 4px 16px; }
  .bubble.assistant { align-self:flex-start; border-bottom-left-radius:4px; }
  .bubble.errored { border-color:#f4212e; }
  .card-head { display:flex; align-items:center; gap:9px; }
  .card-head strong { font-size:12.5px; font-weight:800; }
  .band { margin-left:auto; font-size:9px; font-weight:800; letter-spacing:.08em; text-transform:uppercase; color:var(--fg); border:1px solid var(--line); border-radius:5px; padding:2px 6px; }
  .stage { color:var(--muted); font-size:11px; }
  .deciding { display:flex; align-items:center; gap:8px; color:var(--muted); font-size:12px; line-height:1.45; }
  .deciding.error { color:#f4212e; }
  /* The running account of a live operation: every substage it has passed
     through, oldest first, with the current one highlighted and carrying the
     dots. Waiting should read as a list of things that happened, not as one
     label that silently swaps every 30 seconds. */
  .op-log { display:flex; flex-direction:column; }
  .op-step { display:flex; align-items:center; gap:8px; font-size:12px; line-height:1.45; color:var(--muted); padding:2.5px 0; }
  .op-step .op-mark { flex:none; display:grid; place-items:center; width:13px; height:13px; font-size:10px; }
  .op-step .op-text { min-width:0; flex:1; }
  .op-step.done { opacity:.55; }
  .op-step.done .op-mark { color:var(--ok); }
  .op-step.live { color:var(--fg); font-weight:600; }
  .op-step.live .op-mark { width:auto; }
  /* Cancel lives ON the card, not only on the pill buried in X's compose row:
     whatever Starling is doing, the place that says so is the place that stops
     it. Both routes dispatch the same op event, so they can never disagree. */
  .op-foot { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-top:6px; }
  .op-elapsed { color:var(--muted); font-size:11px; font-weight:600; font-variant-numeric:tabular-nums; }
  .op-cancel { flex:none; border:0; border-radius:6px; padding:2px 4px; margin-right:-4px; background:transparent; color:var(--fg); font-size:12px; font-weight:700; text-decoration:underline; text-underline-offset:2px; cursor:pointer; transition:color .16s ease; }
  .op-cancel:hover { color:#f4212e; }
  :host([data-theme="light"]) .op-cancel:hover { color:#c8232c; }
  /* The words as they stream in: a preview, drawn quieter than the result that replaces it. */
  .op-preview { margin:8px 0 0; font-size:12.5px; line-height:1.5; color:var(--muted); white-space:pre-wrap; overflow-wrap:anywhere; display:-webkit-box; -webkit-line-clamp:6; -webkit-box-orient:vertical; overflow:hidden; }
  .body { font-size:13.5px; line-height:1.55; white-space:pre-wrap; font-weight:500; }
  .draft-mirror { border:1px dashed var(--line); border-radius:12px; padding:14px; background:var(--bg); min-height:78px; font-size:13.5px; line-height:1.6; white-space:pre-wrap; }
  .body.streaming::after { content:""; display:inline-block; width:2px; height:15px; vertical-align:text-bottom; margin-left:1px; background:var(--fg); animation:starling-blink 1s step-end infinite; }
  .status { color:var(--muted); font-size:11.5px; line-height:1.45; }
  .status.stats { font-weight:700; color:var(--fg); }
  .status.error { color:#f4212e; }
  /* The four-signal scheduling breakdown keeps its line breaks so each signal
     reads on its own line. */
  .schedule-why { white-space:pre-line; }
  /* Pick-a-time panel. Set apart from the card's chips so it reads as a
     decision being made, not another action to fire off. */
  .sched-picker { border-top:1px solid var(--line); margin-top:10px; padding-top:10px;
                  display:flex; flex-direction:column; gap:8px; }
  .sched-why { line-height:1.5; }
  .sched-field { display:flex; flex-direction:column; gap:4px; font-size:11.5px; font-weight:700;
                 color:var(--muted); }
  .sched-input { width:100%; box-sizing:border-box; padding:7px 9px; border-radius:8px;
                 border:1px solid var(--line); background:var(--bg); color:var(--fg);
                 font:inherit; font-size:13px; font-weight:600; }
  .sched-input:focus-visible { outline:2px solid var(--fg); outline-offset:1px; }
  .sched-actions { margin-top:2px; }
  .due-card .body { margin-top:4px; }
  .status-row { display:flex; align-items:center; gap:6px; margin-top:12px; }
  .status-row .ok-dot { flex:none; width:5px; height:5px; border-radius:999px; background:var(--ok); }
  .status-row .status { margin-right:auto; }
  /* X's primary (filled) button: solid fg, panel-colored label, dims on hover. */
  .copy-pill { flex:none; color:var(--panel); background:var(--fg); font-size:13px; font-weight:700; padding:7px 16px; border-radius:999px; border:0; cursor:pointer; transition:opacity .16s ease; }
  .copy-pill:hover { opacity:.9; }
  .chips { display:flex; flex-wrap:wrap; gap:7px; }
  /* X's outline button: #536471 border, 700 weight, 13px, pill, and a subtle
     white wash on hover — never a color inversion or a lift. */
  .chip { border:1px solid var(--btnline); border-radius:999px; padding:6px 15px; background:transparent; color:var(--fg); font-size:13px; font-weight:700; cursor:pointer; transition:background .16s ease; }
  .chip:hover:not(:disabled) { background:var(--wash); }
  .chip:disabled { opacity:.4; cursor:default; }
  .call { display:flex; align-items:center; gap:10px; }
  .call-meta { font-size:12px; color:var(--muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .accel { color:var(--ok); font-weight:700; }
  .why-btn { margin-left:auto; flex:none; font-size:12px; font-weight:600; color:var(--muted); background:transparent; border:0; cursor:pointer; padding:0; }
  .why-btn:hover { color:var(--fg); }
  .why-detail { font-size:12px; color:var(--muted); line-height:1.5; white-space:pre-wrap; }
  .wait-reason { font-size:13.5px; line-height:1.55; }
  /* The reason under an enhance status line. The decision card supplies its
     own padding via .card.decision .wait-reason; this one is appended to a
     plain card and needs its own gap.
     NO BACKTICKS IN THIS BLOCK — it lives inside a JS template literal, and a
     backtick in a CSS comment closes the string. Cost one parse error. */
  .deciding + .wait-reason { margin:8px 0 0; font-size:12.5px; color:var(--muted); }
  /* A draft that shipped past a quality or integrity bar. Uses --edge, not
     --line: line is dividers, edge is anything that has to be SEEN. Marked
     rather than shouted — the complaint about this card was that it talks at
     him, so the signal is a rule and a colour, not another sentence. */
  .wait-reason.needs-review { border-left:2px solid var(--edge); padding-left:9px; color:var(--fg); }
  .action-badge { font-size:11px; font-weight:800; letter-spacing:.14em; padding:5px 10px; border-radius:8px; background:var(--fg); color:var(--panel); }
  .action-badge[data-action="wait"], .action-badge[data-action="skip"] { background:transparent; color:var(--fg); border:1px solid var(--fg); letter-spacing:.01em; font-size:11.5px; white-space:nowrap; }
  .card.decision > .status.verdict-cost { padding:0 15px 10px; }
  .card.decision > [data-starling-action-status] { padding:0 15px 12px; }
  .confidence { color:var(--muted); font-size:10px; font-weight:700; text-transform:uppercase; letter-spacing:.04em; }
  .typing { display:inline-flex; align-items:center; gap:5px; padding:3px 2px; }
  .typing i { width:6px; height:6px; border-radius:50%; background:var(--muted); animation:vf-dot 1.3s ease-in-out infinite; }
  .typing i:nth-child(2) { animation-delay:.18s; } .typing i:nth-child(3) { animation-delay:.36s; }
  /* No separator line: the input area merges with the chat background. */
  .composer { display:flex; align-items:flex-end; gap:10px; padding:12px 14px; }
  /* The input dock: one rounded shell holding the attachment cards AND the text
     input, so attaching literally grows the dock instead of stacking a separate
     row above it. */
  .input-shell { flex:1; min-width:0; display:flex; flex-direction:column; border:1px solid var(--line); border-radius:14px; background:var(--bg); transition:border-color .12s ease; }
  .input-shell:focus-within { border-color:var(--fg); }
  /* One line, always: it scrolls its own text rather than growing to follow a
     second line and pushing the card around. */
  /* Single-line box: overflow-y:hidden is what actually kills the native
     up/down scrollbar stepper arrows on Windows — a one-line input never needs
     vertical scroll, so no vertical scrollbar can mount at all. The shell owns
     the border and background; the textarea is a bare line inside it. */
  /* Grows with what is typed, up to four lines (the script sets the height), then scrolls. */
  textarea { resize:none; border:0; border-radius:14px; padding:10px 13px; outline:0; background:transparent; color:var(--fg); font-size:13.5px; line-height:1.45; height:40px; min-height:40px; max-height:100px; overflow-y:hidden; overflow-x:hidden; scrollbar-width:thin; white-space:pre-wrap; overflow-wrap:anywhere; }
  textarea::-webkit-scrollbar { display:none; }
  textarea:focus, textarea:focus-visible { outline:none; }
  textarea::placeholder { color:var(--muted); }
  /* Send: filled X-primary circle. The up-arrow LIFTS on hover (it's about to
     launch) and the button settles on press — smooth spring-ish easing, no
     bounce. Icon motion, not button distortion, keeps the row perfectly still. */
  .send { flex:none; width:38px; height:38px; border:0; border-radius:999px; background:var(--fg); color:var(--panel); cursor:pointer; display:grid; place-items:center; overflow:hidden; transition:opacity .18s ease, transform .22s cubic-bezier(.2,.8,.2,1); }
  .send svg { width:20px; height:20px; display:block; transition:transform .26s cubic-bezier(.2,.8,.2,1); }
  .send:hover { opacity:.92; }
  .send:hover svg { transform:translateY(-2px); }
  .send:active { transform:scale(.93); }
  .send:active svg { transform:translateY(-3px); }
  .send:focus-visible { outline:2px solid var(--xblue); outline-offset:2px; }
  .send[data-mode="stop"] { background:#f4212e; color:#fff; }
  .send[data-mode="stop"] svg { display:none; }
  .send[data-mode="stop"]::after { content:"■"; font-size:14px; }
  /* "+" attach button — mirrors the round send button but stays quiet until used. */
  /* Attach: X outline circle. Deliberately still — no icon spin, no bounce.
     Just a clean wash on hover, exactly like X's own round icon buttons. */
  .attach { flex:none; width:38px; height:38px; border:1px solid var(--btnline); border-radius:999px; background:transparent; color:var(--fg); cursor:pointer; display:grid; place-items:center; transition:background .18s ease, border-color .18s ease; }
  .attach svg { width:20px; height:20px; display:block; }
  .attach:hover { background:var(--wash); border-color:var(--fg); }
  .attach:focus-visible { outline:2px solid var(--xblue); outline-offset:2px; }
  .attach:active { background:var(--wash); opacity:.8; }
  /* Holding attachments: just a brighter ring + fill. No rotation. */
  .attach[data-active] { color:var(--fg); border-color:var(--fg); background:var(--soft); }
  /* The attachment zone lives INSIDE the input shell, above the text input.
     Its grid row animates 0fr→1fr so the dock genuinely grows and shrinks. */
  .att-zone { display:grid; grid-template-rows:0fr; transition:grid-template-rows .26s cubic-bezier(.4,0,.2,1); }
  .att-zone.open { grid-template-rows:1fr; }
  .att-clip { overflow:hidden; min-height:0; }
  /* Responsive row: one card fills the row, each new card shrinks the others
     to share it (halves, thirds, quarters); the 5th starts the next row. */
  .attachments { display:flex; flex-wrap:wrap; gap:8px; padding:10px 10px 2px; }
  .attachment.att-new { animation:vf-att-pop .24s ease-out; }
  @keyframes vf-att-pop { from { opacity:0; transform:translateY(5px) scale(.96); } to { opacity:1; transform:none; } }
  .attachment { position:relative; flex:1 1 calc(25% - 6px); min-width:calc(25% - 6px); max-width:100%; display:flex; flex-direction:column; gap:4px; padding:8px 9px; border:1px solid var(--line); border-radius:10px; background:var(--soft); overflow:hidden; min-height:86px; transition:flex-basis .2s ease; }
  .attachment[data-error] { border-color:#f4212e; }
  .att-remove { position:absolute; top:4px; right:4px; z-index:2; width:17px; height:17px; border:0; border-radius:999px; background:var(--panel); color:var(--muted); font-size:12px; line-height:1; cursor:pointer; display:grid; place-items:center; opacity:0; box-shadow:0 1px 4px rgba(0,0,0,.35); transition:opacity .12s ease, background .12s ease, color .12s ease; }
  .attachment:hover .att-remove, .att-remove:focus-visible { opacity:1; }
  .att-remove:hover { background:var(--fg); color:var(--panel); }
  .att-name { font-size:10.5px; font-weight:700; line-height:1.3; word-break:break-word; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }
  .att-meta { font-size:9px; color:var(--muted); }
  .attachment[data-error] .att-meta { color:#f4212e; }
  .att-badge { align-self:flex-start; margin-top:auto; font-size:8px; font-weight:800; letter-spacing:.05em; text-transform:uppercase; padding:2px 6px; border-radius:5px; background:var(--bg); color:var(--muted); border:1px solid var(--line); }
  .attachment[data-error] .att-badge { color:#f4212e; border-color:#f4212e; }
  /* Compact post card: image on top, handle + text under it, 𝕏 tag at the foot. */
  .att-post-main { flex:1; min-width:0; display:flex; flex-direction:column; gap:3px; }
  .att-thumb { order:-1; width:100%; aspect-ratio:16/10; max-height:110px; border-radius:6px; background:var(--bg); overflow:hidden; }
  .img-head { font-size:11px; font-weight:800; letter-spacing:.04em; text-transform:uppercase;
              color:var(--muted); margin-bottom:2px; }
  /* One quiet preview card: the image itself does the selling, while the
     source, reason, and explicit use/skip choice stay readable at a glance. */
  .visual-choice { border-top:1px solid var(--line); margin-top:4px; padding-top:12px; }
  .visual-choice .chips { margin-top:8px; }
  .img-frame { width:100%; max-height:210px; border-radius:14px; background:var(--bg); overflow:hidden;
               margin:8px 0 6px; border:1px solid var(--line); }
  .img-frame img { width:100%; height:100%; max-height:210px; object-fit:cover; display:block; }
  .img-why { font-size:13px; line-height:1.5; font-weight:600; margin:0 0 6px; }
  .image-card .img-meta { display:block; margin:2px 0; opacity:.9; }
  .image-card .img-rights { font-weight:700; opacity:1; }
  .image-card .alt-input { margin-top:2px; }
  .alt-input { width:100%; box-sizing:border-box; margin:4px 0 2px; padding:6px 8px; border-radius:6px;
               border:1px solid var(--line); background:var(--bg); color:var(--fg); font:inherit; font-size:12px;
               resize:vertical; }
  .att-thumb img { width:100%; height:100%; object-fit:cover; display:block; }
  .att-post-head { display:flex; align-items:center; gap:4px; min-width:0; }
  .att-avatar { flex:none; width:14px; height:14px; border-radius:999px; object-fit:cover; background:var(--bg); }
  .att-who { font-size:10px; font-weight:700; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .att-post-text { font-size:9.5px; line-height:1.35; color:var(--muted); display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }
  .att-badge.post-badge { background:var(--bg); color:var(--fg); border-color:var(--line); letter-spacing:0; text-transform:none; }
  /* Attachment chips echoed under the note inside the sent user bubble. */
  .bubble.user { gap:5px; }
  .bubble-atts { display:inline-flex; align-items:center; gap:5px; font-size:11.5px; font-weight:600; opacity:.9; }
  .bubble-atts svg { width:13px; height:13px; flex:none; }
  .bubble-atts-mark { flex:none; font-weight:800; font-size:12px; }
  .bubble-atts-label { min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:230px; }
  .composer-hint { display:flex; justify-content:space-between; gap:10px; margin:-6px 14px 8px 62px; font-size:11px; font-weight:600; color:var(--muted); }
  .composer-hint:has(> .composer-cost:empty):has(> .composer-count[hidden]) { display:none; }
  .composer-count { margin-left:auto; font-variant-numeric:tabular-nums; }
  .composer-count.over { color:#f4212e; }
  .panel[data-view="settings"] .composer-hint { display:none; }
  .foot { display:flex; align-items:center; gap:14px; padding:11px 14px; border-top:1px solid var(--line); }
  .foot-status { color:var(--muted); font-size:12px; font-weight:600; }
  /* The usage line opens Account & usage; "Upgrade" shows at 20% or less left. */
  .foot-usage, .foot-upgrade { border:0; padding:0; background:transparent; color:inherit; font:inherit; cursor:pointer; }
  .foot-usage:hover { color:var(--fg); text-decoration:underline; text-underline-offset:2px; }
  .foot-upgrade { margin-left:4px; color:var(--fg); font-weight:700; }
  .foot-upgrade:hover { text-decoration:underline; text-underline-offset:2px; }
  .foot-build { opacity:.6; }
  /* The account card above the conversation: the data notice, or what is
     missing before a draft (sign in, verify, plan). Hidden in Settings, which
     has its own Account section. */
  /* A thin scrollbar, so a long notice shows there is more of it. */
  .state { flex:0 0 auto; margin:12px 14px 0; display:flex; flex-direction:column; gap:10px; max-height:60%; overflow-y:auto; scrollbar-width:thin; scrollbar-color:var(--btnline) transparent; }
  .state[hidden] { display:none; }
  .panel[data-view="settings"] .state { display:none; }
  .state-card { border:1px solid var(--edge); border-left:3px solid var(--xblue); border-radius:14px; padding:12px 14px; display:flex; flex-direction:column; gap:8px; background:var(--soft); }
  .state-card.warn { border-left-color:#ffd400; }
  .state-card.down { border-left-color:#f4212e; }
  .state-card.consent { border-left-color:var(--fg); }
  .state-title { font-size:13.5px; font-weight:800; line-height:1.35; }
  .state-body { font-size:12.5px; line-height:1.5; color:var(--muted); }
  .consent-heading { margin-top:4px; font-size:10.5px; font-weight:800; letter-spacing:.07em; text-transform:uppercase; color:var(--muted); }
  .consent-list { margin:0; padding-left:18px; font-size:12.5px; line-height:1.5; }
  .consent-list li { margin:3px 0; }
  .consent-check { display:flex; align-items:flex-start; gap:8px; font-size:12.5px; font-weight:700; cursor:pointer; }
  .consent-check input { margin-top:2px; }
  /* "Agree and continue" stays in view at the bottom of the notice, under a fade, however far the
     notice is scrolled. It used to sit below the fold of a scroll area with no scrollbar. */
  .state-card.consent .consent-actions { position:sticky; bottom:-12px; z-index:1; margin:2px -14px -12px; padding:10px 14px 12px; background:var(--soft-solid); border-radius:0 0 14px 14px; }
  .state-card.consent .consent-actions::before { content:""; position:absolute; left:0; right:0; bottom:100%; height:22px; background:linear-gradient(to bottom, transparent, var(--soft-solid)); pointer-events:none; }
  .state-body.state-warn { color:var(--fg); font-weight:600; }
  .chip.danger { border-color:#f4212e; color:#f4212e; }
  .chip.danger:hover:not(:disabled) { background:rgba(244,33,46,.12); }
  .meter { position:relative; height:8px; border-radius:999px; background:var(--line); overflow:hidden; }
  .meter-fill { position:absolute; top:0; bottom:0; left:0; border-radius:999px; background:var(--fg); }
  .foot-usage { font-variant-numeric:tabular-nums; }
  .edge { position:absolute; touch-action:none; z-index:20; }
  .edge-l { left:-3px; top:14px; bottom:14px; width:7px; cursor:ew-resize; }
  .edge-r { right:-3px; top:14px; bottom:14px; width:7px; cursor:ew-resize; }
  .edge-t { top:-3px; left:14px; right:14px; height:7px; cursor:ns-resize; }
  .edge-b { bottom:-3px; left:14px; right:14px; height:7px; cursor:ns-resize; }
  .edge-tl { left:-3px; top:-3px; width:16px; height:16px; cursor:nwse-resize; z-index:21; }
  .edge-tr { right:-3px; top:-3px; width:16px; height:16px; cursor:nesw-resize; z-index:21; }
  .edge-br { right:-3px; bottom:-3px; width:16px; height:16px; cursor:nwse-resize; z-index:21; }
  .edge-br::after { content:""; position:absolute; right:4px; bottom:4px; width:7px; height:7px; border-right:1px solid var(--muted); border-bottom:1px solid var(--muted); opacity:.8; }
  .edge-bl { left:-3px; bottom:-3px; width:16px; height:16px; cursor:nesw-resize; z-index:21; }
  :host([data-placement="sidebar"]) .edge { display:none; }
  button:focus-visible, .chip:focus-visible { outline:2px solid var(--fg); outline-offset:2px; }
  @keyframes starling-pop { from { opacity:0; transform:translateY(-8px) scale(.97); } to { opacity:1; transform:none; } }
  @keyframes starling-blink { 0%,49% { opacity:1; } 50%,100% { opacity:0; } }
  @keyframes vf-dot { 0%,80%,100% { opacity:.25; transform:scale(.72); } 40% { opacity:1; transform:scale(1); } }
  @media (max-width:480px) { .panel { width:calc(100vw - 20px); min-width:0; height:calc(100vh - 72px); min-height:0; } }
  @media (prefers-reduced-motion:reduce) { .card, .panel.opening, .panel.docking, .att-zone, .attachment.att-new { animation:none; transition:none; } .suggest-btn, .op-cancel { transition:none; } .typing i, .body.streaming::after { animation:none; } .send, .send svg, .attach { transition:none; } .send:hover svg { transform:none; } .dd-list { animation:none; } .dd-caret, .seg-ind, .seg-btn, .spd-fill, .spd-input::-webkit-slider-thumb, .tip { transition:none; } .spd-demo::after { animation:none; }
    /* The switches still change state, they just stop travelling to get there.
       The star/cloud transforms go too, or they arrive by teleport. */
    .sw-track, .sw-track::before, .sw-star, .sw-cloud { transition:none; }
    .sw-theme input:checked + .sw-track .sw-star { transform:none; }
    .sw-cloud { transform:none; } }
`;

// Small icon-only button that lives in each post's action row and reads as a
// native X control. Monochrome to match the card.
const postButtonStyles = `
  /* X's OWN typeface. These buttons sit INSIDE X's action row, inches from
     X's own controls — rendering them in the system UI font (Segoe UI on
     Windows) is the single loudest tell that they are not part of X, and it is
     what made them read as bolted on. Chirp first, system stack as fallback. */
  :host { font-family:"TwitterChirp","Chirp",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
  .vf-post { display:inline-flex; align-items:center; justify-content:center; width:32px; height:32px; margin-left:2px; border:0; border-radius:999px; background:transparent; color:#8b98a5; cursor:pointer; transition:color .15s ease, background .15s ease; }
  .vf-post:hover { color:#e7e9ea; background:rgba(231,233,234,.1); }
  :host([data-theme="light"]) .vf-post { color:#5b6470; }
  :host([data-theme="light"]) .vf-post:hover { color:#0a0a0a; background:rgba(10,10,10,.06); }
  .vf-post-mark { font-size:16px; line-height:1; font-weight:800; }
  .vf-post-loading { display:none; align-items:center; gap:2px; height:16px; }
  .vf-post-loading i { display:block; width:3px; height:3px; border-radius:999px; background:currentColor; animation:vf-post-dot .72s ease-in-out infinite; }
  .vf-post-loading i:nth-child(2) { animation-delay:.12s; }
  .vf-post-loading i:nth-child(3) { animation-delay:.24s; }
  /* Working: the dots; pointed at, a stop square, because the click now stops it. */
  .vf-post[data-state="working"] { color:#e7e9ea; cursor:pointer; }
  :host([data-theme="light"]) .vf-post[data-state="working"] { color:#0f1419; }
  .vf-post[data-state="working"] .vf-post-mark { display:none; }
  .vf-post[data-state="working"] .vf-post-loading { display:inline-flex; }
  .vf-post-stop { display:none; width:9px; height:9px; border-radius:2px; background:currentColor; }
  .vf-post[data-state="working"]:hover .vf-post-loading, .vf-post[data-state="working"]:focus-visible .vf-post-loading { display:none; }
  .vf-post[data-state="working"]:hover .vf-post-stop, .vf-post[data-state="working"]:focus-visible .vf-post-stop { display:inline-block; }
  .vf-post[data-state="working"]:hover { color:#f4212e; background:rgba(244,33,46,.1); }
  /* "Show me" on the first-run card: three pulses, or a still ring under reduced motion. */
  .vf-post[data-pulse="true"] { animation:vf-post-pulse .9s ease-in-out 3; color:#1d9bf0; }
  .vf-post[data-pulse="still"] { box-shadow:0 0 0 2px #1d9bf0; color:#1d9bf0; }
  @keyframes vf-post-pulse { 0%,100% { box-shadow:0 0 0 0 rgba(29,155,240,0); transform:scale(1); } 50% { box-shadow:0 0 0 7px rgba(29,155,240,.3); transform:scale(1.15); } }
  .vf-post:focus-visible { outline:2px solid currentColor; outline-offset:2px; }
  /* Paperclip attach button — same footprint and quiet-until-hover feel as ✦. */
  /* Only while its post is hovered or holds focus (and always on a touch screen, which has no
     hover): a paperclip on every post in the feed was noise. Still in the tab order when faded. */
  .vf-attach { display:inline-flex; align-items:center; justify-content:center; width:32px; height:32px; margin-left:2px; border:0; border-radius:999px; background:transparent; color:#8b98a5; cursor:pointer; opacity:0; transition:color .15s ease, background .15s ease, opacity .15s ease; }
  :host([data-hover="true"]) .vf-attach, .vf-attach:focus-visible { opacity:1; }
  @media (hover:none) { .vf-attach { opacity:1; } }
  .vf-attach:hover { color:#e7e9ea; background:rgba(231,233,234,.1); }
  :host([data-theme="light"]) .vf-attach { color:#5b6470; }
  :host([data-theme="light"]) .vf-attach:hover { color:#0a0a0a; background:rgba(10,10,10,.06); }
  .vf-attach-mark { display:inline-flex; }
  .vf-attach-mark svg { width:17px; height:17px; display:block; }
  .vf-attach:focus-visible { outline:2px solid currentColor; outline-offset:2px; }
  @keyframes vf-post-dot { 0%,65%,100% { opacity:.32; transform:translateY(0); } 32% { opacity:1; transform:translateY(-2px); } }
  @media (prefers-reduced-motion:reduce) { .vf-post[data-state="working"] .vf-post-loading i { animation:none; opacity:1; transform:none; } .vf-post[data-pulse="true"] { animation:none; box-shadow:0 0 0 2px #1d9bf0; } .vf-attach { transition:none; } }
`;

const composerStyles = `
  /* Chirp, like X's own Post/Reply button standing right next to this pill.
     Same face, same weight, same 13px — so the row reads as one set of
     controls instead of one native button plus a visitor. */
  :host { font-family:"TwitterChirp","Chirp",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
  /* Fixed geometry ON PURPOSE: slightly smaller than X's Post button, flex-
     centered by the row, sized only by CSS. No script ever measures or moves
     a mounted pill — that is what kept it bobbing while the user typed. */
  /* Colors follow X's own outline buttons (Following/Unfollow style): plain
     transparent fill, X's exact border grey, X's text tones, flat — no inset
     highlights. Geometry untouched. */
  /* Every colour comes from PILL_PALETTE (shared/theme.ts), where a unit test
     checks each one against X's three page colours for 4.5:1. The light theme
     has its own state colours: the dark theme's mint "Done" is 1.5:1 on white. */
  .vf-enhance { position:relative; display:inline-flex; align-items:center; justify-content:center; gap:5px; box-sizing:border-box; min-width:0; height:32px; border:1px solid ${PILL_PALETTE.dark.border}; border-radius:999px; padding:0 11px; background:transparent; color:${PILL_PALETTE.dark.text}; font-family:inherit; font-size:13px; font-weight:700; line-height:18px; letter-spacing:-.1px; white-space:nowrap; cursor:pointer; transition:background .16s ease,border-color .16s ease,color .16s ease; }
  :host([data-theme="light"]) .vf-enhance { border-color:${PILL_PALETTE.light.border}; background:transparent; color:${PILL_PALETTE.light.text}; }
  .vf-enhance:hover:not(:disabled) { background:rgba(239,243,244,.1); }
  :host([data-theme="light"]) .vf-enhance:hover:not(:disabled) { background:rgba(15,20,25,.1); }
  .vf-enhance[data-state="updated"] { border-color:#00ba7c; background:${PILL_PALETTE.dark.doneWash}; color:${PILL_PALETTE.dark.doneText}; }
  :host([data-theme="light"]) .vf-enhance[data-state="updated"] { background:${PILL_PALETTE.light.doneWash}; color:${PILL_PALETTE.light.doneText}; }
  .vf-enhance[data-state="error"] { border-color:#f4212e; background:${PILL_PALETTE.dark.errorWash}; color:${PILL_PALETTE.dark.errorText}; }
  :host([data-theme="light"]) .vf-enhance[data-state="error"] { background:${PILL_PALETTE.light.errorWash}; color:${PILL_PALETTE.light.errorText}; }
  .vf-enhance[data-state="sending"] { opacity:1; cursor:pointer; }
  .vf-enhance[data-state="typing"] { border-color:#00ba7c; background:${PILL_PALETTE.dark.doneWash}; color:#d8fff0; cursor:pointer; }
  :host([data-theme="light"]) .vf-enhance[data-state="typing"] { background:${PILL_PALETTE.light.doneWash}; color:${PILL_PALETTE.light.doneText}; }
  .vf-enhance[data-state="sending"]:hover, .vf-enhance[data-state="typing"]:hover { border-color:#f4212e; background:rgba(244,33,46,.14); color:#ffb3ba; transform:none; }
  :host([data-theme="light"]) .vf-enhance[data-state="sending"]:hover, :host([data-theme="light"]) .vf-enhance[data-state="typing"]:hover { background:${PILL_PALETTE.light.errorWash}; color:${PILL_PALETTE.light.errorText}; }
  /* "✓ Done" is disabled for its moment on screen, but it is news, not a dead control: full strength. */
  .vf-enhance:disabled:not([data-state="sending"]):not([data-state="updated"]) { opacity:.48; cursor:not-allowed; }
  .vf-enhance[data-state="updated"]:disabled { cursor:default; }
  .vf-enhance:focus-visible { outline:2px solid currentColor; outline-offset:2px; }
  .vf-enhance-mark { flex:none; font-size:16px; line-height:1; }
  .vf-enhance-text { min-width:0; overflow:hidden; text-overflow:ellipsis; transition:opacity .18s ease; }
  /* Working state must NOT change the pill's size: the label stays in the
     layout (visibility:hidden keeps its width) and the dots — and on hover,
     Cancel — are OVERLAYS centered on top. The pill never shrinks. */
  .vf-enhance-cancel { display:none; }
  .vf-enhance-working { display:none; position:absolute; inset:0; align-items:center; justify-content:center; gap:3px; }
  .vf-enhance[data-state="sending"] .vf-enhance-mark, .vf-enhance[data-state="typing"] .vf-enhance-mark { visibility:hidden; }
  /* Loading = three dots only, no words — overlaid, same footprint. */
  .vf-enhance[data-state="sending"] .vf-enhance-text, .vf-enhance[data-state="typing"] .vf-enhance-text { visibility:hidden; }
  .vf-enhance[data-state="sending"] .vf-enhance-working, .vf-enhance[data-state="typing"] .vf-enhance-working { display:inline-flex; }
  .vf-enhance[data-state="sending"]:hover .vf-enhance-working, .vf-enhance[data-state="typing"]:hover .vf-enhance-working { display:none; }
  .vf-enhance[data-state="sending"]:hover .vf-enhance-cancel, .vf-enhance[data-state="typing"]:hover .vf-enhance-cancel { display:inline-flex; position:absolute; inset:0; align-items:center; justify-content:center; }
  .vf-enhance-working i { width:4px; height:4px; border-radius:50%; background:currentColor; animation:vf-dot .72s ease-in-out infinite; } .vf-enhance-working i:nth-child(2) { animation-delay:.1s; } .vf-enhance-working i:nth-child(3) { animation-delay:.2s; }
  @keyframes vf-dot { 0%,65%,100% { opacity:.3; transform:translateY(0); } 32% { opacity:1; transform:translateY(-2px); } }
  @media (prefers-reduced-motion:reduce) { .vf-enhance { transition:none; } .vf-enhance-working i { animation:none; opacity:1; transform:none; } }
`;

function isExternalUrl(value: string): boolean {
  try {
    const url = new URL(value);
    return (url.protocol === "http:" || url.protocol === "https:")
      && url.hostname !== "x.com"
      && url.hostname !== "twitter.com"
      && !url.hostname.endsWith(".x.com")
      && !url.hostname.endsWith(".twitter.com");
  } catch {
    return false;
  }
}

function isTimestampLabel(value: string): boolean {
  return /^\d+[smhd]$|^[A-Z][a-z]{2} \d{1,2}|^\d{1,2}:\d{2}/.test(value);
}

function isDarkSurface(element: Element): boolean {
  return surfaceTone(element) !== "light";
}

/**
 * Which of X's three surfaces this element is painted on.
 *
 * One walk, two answers: everything inline in X's chrome only needs the
 * light/dark bit above, while the dock also needs Dim told apart from Lights
 * out so its panel can borrow the right background.
 */
function surfaceTone(element: Element): SurfaceTone {
  let current: Element | null = element;
  while (current) {
    const color = getComputedStyle(current).backgroundColor;
    const rgb = color.match(/rgba?\((\d+)[,\s]+(\d+)[,\s]+(\d+)(?:[,\s/]+([\d.]+%?))?/);
    if (rgb) {
      // X layers translucent overlays (rgba(255,255,255,.03) etc.) on dark
      // pages — treating those as OPAQUE white painted Starling's pill white on
      // a black UI (the white-strip artifact). A see-through background says
      // nothing about the surface; keep walking until a real one.
      const alphaRaw = rgb[4];
      const alpha = alphaRaw == null ? 1 : alphaRaw.endsWith("%") ? Number(alphaRaw.slice(0, -1)) / 100 : Number(alphaRaw);
      if (alpha >= 0.5) return toneFromRgb(Number(rgb[1]), Number(rgb[2]), Number(rgb[3]));
    }
    current = current.parentElement;
  }
  return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function isVisible(element: HTMLElement): boolean {
  const rect = element.getBoundingClientRect();
  const style = getComputedStyle(element);
  return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
}

function postIdFromUrl(url?: string): string | undefined {
  return url?.match(/\/status\/(\d+)/)?.[1];
}


function isRecord(value: unknown): value is Record<string, any> {
  return typeof value === "object" && value !== null;
}
