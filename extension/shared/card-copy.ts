/**
 * The words on the dock's cards that are decided by rule rather than written once: what a verdict
 * badge says, what a waiting ✦ card says at each moment, what the button that picks a refused draft
 * back up is called, and when a "not web-checked" caveat is worth showing.
 *
 * Pure, so every rule has a test.
 */
import type { ComposerAction } from "./types";

/** The badge on a verdict card. WAIT and SKIP in capitals read as an error code, not as advice. */
export function decisionBadgeLabel(action: string): string {
  switch (action) {
    case "wait": return "Better to wait";
    case "skip": return "Worth skipping";
    case "reply": return "Reply";
    case "quote": return "Quote";
    case "post": return "Post";
    default: return action ? action[0].toUpperCase() + action.slice(1) : "";
  }
}

export interface WaitingCopy {
  /** Shown from the first moment. */
  label: string;
  /** [milliseconds after the press, what the card says from then on]. */
  beats: Array<[number, string]>;
}

/**
 * What a waiting ✦ card says, and when. The request is one call with no progress events, so these
 * are not stages: they say only what is true (it is still writing), when a wait is normal, and, near
 * the end, that stopping costs nothing. The worker gives up at 60 s.
 */
export function decideWaitingCopy(action: "reply" | "quote" = "reply", ownPost = false): WaitingCopy {
  const what = `${ownPost ? "self-" : ""}${action}`;
  return {
    label: `Writing your ${what}…`,
    beats: [
      [8_000, `Still writing your ${what}…`],
      [25_000, "Still writing. Busy moments can take up to a minute."],
      [45_000, "Taking longer than usual. Keep waiting, or Cancel. Nothing is used until a draft arrives."],
    ],
  };
}

/** "12s", "1m 05s": how long a card has been waiting. */
export function formatElapsed(ms: number): string {
  const whole = Math.max(0, Math.floor(ms / 1000));
  if (whole < 60) return `${whole}s`;
  return `${Math.floor(whole / 60)}m ${String(whole % 60).padStart(2, "0")}s`;
}

export interface ResumeTarget {
  /** "decide" is ✦ on a post; "enhance" is the composer pill; "chat" is the panel's own box. */
  kind: "decide" | "enhance" | "chat";
  action?: ComposerAction;
  /** "@handle" or "handle". */
  handle?: string;
  own?: boolean;
}

/** The button that sends a refused draft again once the account is ready: "Draft the reply to @x". */
export function resumeActionLabel(target: ResumeTarget): string {
  if (target.kind === "chat") return "Ask again";
  const action = target.kind === "decide" ? "reply" : (target.action ?? "reply");
  if (action === "post") return "Draft the post";
  const handle = target.handle ? `@${target.handle.replace(/^@/, "")}` : "";
  if (target.own) return action === "quote" ? "Draft the self-quote" : "Draft the self-reply";
  if (action === "quote") return handle ? `Draft the quote of ${handle}` : "Draft the quote";
  return handle ? `Draft the reply to ${handle}` : "Draft the reply";
}

const MAX_TOPIC_CHARS = 140;

/**
 * "About: {topic}": the story a created post was written about, on its card, so the customer can
 * see what Starling picked before reading the draft. Null when the post names no topic.
 */
export function topicLine(topic: unknown): string | null {
  if (typeof topic !== "string") return null;
  let text = topic.replace(/\s+/g, " ").trim();
  if (!text) return null;
  if (text.length > MAX_TOPIC_CHARS) text = `${text.slice(0, MAX_TOPIC_CHARS - 1).trimEnd()}…`;
  return `About: ${text}`;
}

/** The glyph on a card's one-line header. */
export function cardIcon(kind: "decide" | "enhance" | "chat", action?: ComposerAction): string {
  if (kind === "decide") return "✦";
  if (action === "quote") return "❝";
  if (action === "post") return "✎";
  return "↩";
}

const MONTHS = /\b(?:january|february|march|april|june|july|august|september|october|november|december)\b/i;
const SHORT_MONTH_DAY = /\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)\.?\s+\d/i;
const WEEKDAYS = /\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|yesterday|tomorrow|tonight)\b/i;

/**
 * Does this draft make the kind of claim a reader should check? Numbers, @names and dates are the
 * things a model gets wrong with confidence. A draft with none of them does not need a caveat that
 * nothing was checked on the web: there is nothing in it to check.
 */
export function draftHasCheckableClaims(text: string | undefined | null): boolean {
  const value = text ?? "";
  if (!value.trim()) return false;
  return /\d/.test(value) || /(?:^|[^\w@])@\w{1,15}\b/.test(value) || MONTHS.test(value)
    || SHORT_MONTH_DAY.test(value) || WEEKDAYS.test(value);
}
