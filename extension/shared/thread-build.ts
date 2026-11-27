/**
 * Building a thread in X's composer, one part per box.
 *
 * Until now Starling put part 1 in the composer and handed over "Copy part 2",
 * "Copy part 3" chips — the user pressed X's "+" and pasted each one. That is
 * the most mechanical possible task and exactly what the tool should do.
 *
 * The invariant is unchanged: this FILLS composer boxes, it never submits.
 * Adding a box and typing into it is the same class of action as writing part
 * one; the Post button stays the human's.
 *
 * The DOM work lives in content.ts; the rules live here so they can be tested
 * without a browser.
 */

/** X's own limit on parts in a single thread compose session. */
export const MAX_THREAD_PARTS = 25;

export interface ThreadPlan {
  /** Parts to write, in order. Index 0 goes in the box that already exists. */
  parts: string[];
  /** How many NEW boxes must be created (parts.length - 1, never negative). */
  boxesToAdd: number;
  /** Why we are not building, when parts is empty. */
  skipped?: string;
}

/**
 * Decide what to build from a decision's thread_parts.
 *
 * Rejects the shapes that would produce a broken thread rather than trying:
 * a single part is not a thread, blank parts would post an empty box, and
 * over-long parts would be silently truncated by X.
 */
export function planThread(rawParts: unknown, charLimit = 280): ThreadPlan {
  if (!Array.isArray(rawParts)) return { parts: [], boxesToAdd: 0, skipped: "no thread parts" };
  const parts = rawParts
    .map(part => (typeof part === "string" ? part.trim() : ""))
    .filter(Boolean)
    .slice(0, MAX_THREAD_PARTS);
  if (parts.length < 2) {
    return { parts: [], boxesToAdd: 0, skipped: "a thread needs at least two parts" };
  }
  const overLong = parts.findIndex(part => [...part].length > charLimit);
  if (overLong >= 0) {
    // Better to hand back copy chips than to let X truncate a part mid-word.
    return { parts: [], boxesToAdd: 0,
             skipped: `part ${overLong + 1} is longer than ${charLimit} characters` };
  }
  return { parts, boxesToAdd: parts.length - 1 };
}

/**
 * Which composer box should receive part N.
 *
 * X numbers its boxes tweetTextarea_0, _1, _2… and a reply composer starts at
 * 0 as well, so the mapping is positional: the Nth visible box gets part N.
 * Returns -1 when the expected box has not appeared yet and the caller should
 * wait rather than write into the wrong one — writing part 3 into part 2's box
 * is the failure this function exists to prevent.
 */
export function boxIndexForPart(partIndex: number, visibleBoxes: number): number {
  if (partIndex < 0 || partIndex >= visibleBoxes) return -1;
  return partIndex;
}

/** A human progress line for the card while the thread is being built. */
export function threadProgressLine(written: number, total: number): string {
  if (written >= total) return `Thread ready — ${total} parts in the composer. You post it.`;
  return `Writing part ${Math.min(written + 1, total)} of ${total}…`;
}
