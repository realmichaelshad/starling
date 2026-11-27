import type { ComposerAction } from "./types";

export interface ComposerReadiness {
  text: string;
  isComposing: boolean;
  xAcceptsDraft: boolean;
}

/** X decides whether a non-empty composer is publishable; Starling only mirrors it. */
export function isEnhanceReady({ text, isComposing, xAcceptsDraft }: ComposerReadiness): boolean {
  return Boolean(text.trim()) && !isComposing && xAcceptsDraft;
}

/**
 * True when two composer strings hold the SAME content, ignoring whitespace
 * shape. X's editor re-renders a draft into its own block elements (newlines
 * become blocks, trailing space is trimmed), so the composer's innerText
 * legitimately differs from the source string by whitespace. Comparing exactly
 * made a perfectly inserted multi-line draft look like a failure — the bogus
 * "X kept rejecting the typed text. Copy the finished version instead." error.
 * Verify the CONTENT landed, not its exact whitespace.
 */
export function sameComposerText(a: string, b: string): boolean {
  const normalize = (value: string) => value
    .replace(/[​-‍﻿]/g, "") // zero-width chars X sprinkles in
    .replace(/\s+/g, " ")
    .trim();
  return normalize(a) === normalize(b);
}

export type ComposerMode = "create" | "enhance" | "blocked";

/**
 * `updated` is the moment right after Starling typed a draft: "✓ Done", disabled for 1.6 s, so a
 * second click in that moment cannot quietly rewrite the reply and use another draft. `another` is
 * the calm state after it, for as long as the box still holds exactly what Starling typed: the pill
 * offers a DIFFERENT draft and says it costs one. The moment the customer edits, it is Enhance again.
 */
export type ComposerControlState = "idle" | "ready" | "sending" | "typing" | "updated" | "another" | "error";

export interface ComposerControlCopy {
  visible: string;
  ariaLabel: string;
}

/**
 * Clear, action-specific copy keeps a reply control from reading like a post control.
 *
 * `perRequest` is what one press uses (the session's `usage.per_request`, 1 by default), so "Try
 * another" never claims one draft when a press costs more.
 */
export function composerControlCopy(
  action: ComposerAction,
  mode: ComposerMode,
  state: ComposerControlState | undefined,
  hasDraft = false,
  isOwnPost = false,
  perRequest = 1,
): ComposerControlCopy {
  // Replying to your own post is a different move from replying to a stranger,
  // and the button is the last place the user checks before writing. Naming it
  // "self-reply" means there is never a moment where they cannot tell whose
  // post they are about to answer.
  const selfPrefix = isOwnPost && action !== "post" ? "self-" : "";
  const target = selfPrefix + (action === "reply" ? "reply" : action === "quote" ? "quote" : "post");
  const targetWithArticle = action === "post" ? "a post" : `a ${target}`;
  const targetTitle = target[0].toUpperCase() + target.slice(1);
  const isEnhancement = mode === "enhance" || (mode === "blocked" && hasDraft);
  const verb = isEnhancement ? "Enhance" : "Create";
  const workingVerb = isEnhancement ? "Enhancing" : "Creating";

  if (state === "sending") {
    return { visible: `${workingVerb} ${target}`, ariaLabel: `Cancel ${workingVerb.toLowerCase()} ${targetWithArticle} with Starling` };
  }
  if (state === "typing") {
    return { visible: `Writing ${target}`, ariaLabel: `Cancel writing your ${target} with Starling` };
  }
  if (state === "updated") {
    return { visible: "Done", ariaLabel: `Done. Your ${target} is in X's box` };
  }
  if (state === "another") {
    const cost = Number.isInteger(perRequest) && perRequest > 0 ? perRequest : 1;
    return { visible: "Try another", ariaLabel: `Write a different ${target} with Starling. Uses ${cost} ${cost === 1 ? "draft" : "drafts"}` };
  }
  if (state === "error") {
    return { visible: "Try again", ariaLabel: `${targetTitle} did not finish. Try again` };
  }
  if (mode === "blocked") {
    return {
      visible: `${verb} ${target}`,
      ariaLabel: hasDraft
        ? `Finish editing this ${target} before enhancing with Starling`
        : `Finish composing before creating ${targetWithArticle} with Starling`
    };
  }
  return { visible: `${verb} ${target}`, ariaLabel: `${verb} ${targetWithArticle} with Starling` };
}

/**
 * The composer button is always present and dual-mode: an EMPTY composer means
 * the user wants Starling to write from scratch; text in the composer means
 * enhance. Starling only ever DRAFTS into the composer (it never submits), so X's
 * own submit-readiness must NOT gate the pill — requiring it caused the pill to
 * be disabled and clicks to silently do nothing whenever X's button detection
 * was momentarily off. The only hard block is an in-progress IME composition.
 */
export function composerMode({ text, isComposing }: ComposerReadiness): ComposerMode {
  if (isComposing) return "blocked";
  if (!text.trim()) return "create";
  return "enhance";
}

export interface NativeSubmitReadiness {
  /** Number of visible X submit controls associated with this one composer. */
  matchingSubmitCount: number;
  /** X's own enabled/disabled state for that exact control. */
  enabled: boolean;
}

/**
 * A missing or ambiguous X submit control is not proof that a draft is ready.
 * Keep Enhance unavailable until the native composer gives us one clear, enabled
 * control to mirror.
 */
export function isNativeComposerSubmitReady({ matchingSubmitCount, enabled }: NativeSubmitReadiness): boolean {
  return matchingSubmitCount === 1 && enabled;
}

export interface ReplacementGuard {
  expectedOriginal: string;
  currentText: string;
  sessionRequestId: number;
  requestId: number;
  connected: boolean;
  visible: boolean;
}

/** Fail closed whenever the live composer is no longer the exact request target.
 *
 * The text check uses sameComposerText (zero-width + whitespace normalized), not
 * strict ===, so X's own invisible re-normalization of the box during the
 * multi-second model call does NOT read as "the user changed the draft". A real
 * edit still differs after normalization and still fails closed. */
export function canReplaceComposerDraft(guard: ReplacementGuard): boolean {
  return guard.sessionRequestId === guard.requestId
    && guard.connected
    && guard.visible
    && sameComposerText(guard.currentText, guard.expectedOriginal);
}

/**
 * Said when Starling cannot read the post a reply or quote box answers, so it sends nothing.
 *
 * A reply written from scratch is written TO a post; without the post there is nothing true to
 * write, and the service refuses it. That refusal used to travel all the way to the server and
 * come back as "Starling's AI isn't responding" (the gateway reports every engine refusal as the
 * AI being down), which sent the reader to wait for an AI that was answering fine. The box knows
 * before anything is sent, so it says so, and nothing is used.
 */
export function missingSourceLine(action: ComposerAction): string {
  const what = action === "quote" ? "you're quoting" : "you're replying to";
  return `Starling couldn't read the post ${what}, so it didn't write anything. `
    + `Close this box, then press ✦ on the post itself. No draft was used.`;
}

export function composerAnchor(action: ComposerAction, author?: string, sourceText?: string,
                               isOwnPost = false): {
  label: string;
  excerpt: string;
} {
  const target = author?.trim() || "your draft";
  // Replying to your own post is a different move from replying to a stranger
  // — it is a follow-up, a caveat, or a receipt on something already
  // travelling. The chip says so outright, so there is never a doubt about
  // whose post is being acted on.
  const label = action === "reply"
    ? (isOwnPost ? "Self-reply to your post" : `Replying to ${target}`)
    : action === "quote"
      ? (isOwnPost ? "Self-quote of your post" : `Quoting ${target}`)
      : "New post";
  const excerpt = sourceText?.replace(/\s+/g, " ").trim().slice(0, 90)
    || "Your words, sharpened without changing the point.";
  return { label, excerpt };
}

/**
 * What Starling typed into one X box, kept so the customer can take it back.
 *
 * `original` is the customer's own text before Starling touched the box (empty for Create), and
 * `inserted` is exactly what Starling typed. Undo only ever restores `original`, and only while the
 * box still holds `inserted`: once the customer has edited, their edit wins.
 */
export interface ComposerUndo {
  original: string;
  inserted: string;
  /** Undo has been used; the pill no longer offers "Try another" for this draft. */
  restored?: boolean;
}

export interface ControlStateInputs {
  previous: ComposerControlState | undefined;
  mode: ComposerMode;
  /** The box's current text. */
  text: string;
  undo?: ComposerUndo | null;
}

/** The pill's state after a repaint: working and terminal states are kept, the rest follow the box. */
export function resolveComposerControlState({ previous, mode, text, undo }: ControlStateInputs): ComposerControlState {
  if (previous === "sending" || previous === "typing" || previous === "updated" || previous === "error") return previous;
  if (mode !== "blocked" && undo && !undo.restored && text.trim() && sameComposerText(text, undo.inserted)) return "another";
  return mode === "blocked" ? "idle" : "ready";
}

/**
 * Disabled while "✓ Done" shows, and while an IME composition is open. Never while Starling is
 * working: the pill is the Cancel button then.
 */
export function isComposerControlDisabled(state: ComposerControlState, mode: ComposerMode): boolean {
  if (state === "sending" || state === "typing") return false;
  return state === "updated" || mode === "blocked";
}

export type UndoDecision = "restore" | "edited" | "gone";

/** May Undo put the customer's text back? Only while the box holds exactly what Starling typed. */
export function undoDecision(currentText: string | null, undo: ComposerUndo): UndoDecision {
  if (currentText === null) return "gone";
  return sameComposerText(currentText, undo.inserted) ? "restore" : "edited";
}
