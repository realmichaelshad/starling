/**
 * Pure math for the chat feed's message anchor: a newly
 * sent user message pins near the top of the feed viewport and the response
 * streams in below it.
 *
 * Bottom slack is mathematically REQUIRED to put a late message at the
 * viewport top: the scroll range must extend to elTop + clientHeight, so when
 * the content below the anchor is shorter than the viewport, the difference
 * has to be blank. The spacer holding that blank is measured per layout,
 * shrinks to zero as the streamed answer fills the space, and is removed when
 * the anchor releases — it is not a fixed-height or permanent pad.
 */
export const ANCHOR_TOP_GAP = 8;

export interface AnchorInput {
  /** feed.clientHeight — the viewport. */
  clientHeight: number;
  /**
   * Content height EXCLUDING the spacer AND the flex gap the spacer brings
   * with it. The feed is a flex column with a row gap, so a spacer that is
   * present at height 0 still costs one gap — counting it as content made the
   * anchor reserve exactly one gap too little, every time.
   */
  contentHeight: number;
  /** Anchored bubble's offset from the content top. */
  elTop: number;
  /**
   * Largest slack worth reserving. Defaults to the only value that can ever be
   * needed: enough to lift ANY element to the top of an empty viewport.
   */
  maxSpacer?: number;
}

export interface AnchorResult {
  spacerHeight: number;
  scrollTop: number;
}

/**
 * With the same gap on both sides, the target is EXACTLY reachable:
 * maxScrollTop = contentHeight + spacer − clientHeight = elTop − GAP =
 * scrollTop — so the recorded anchor is a real position, not a clamped wish
 * (the old −24 reserve vs −8 target mismatch left it 16px short forever).
 *
 * The spacer cap and the scroll target are decided TOGETHER, here, because
 * capping one without the other silently breaks the anchor. Measured on the
 * 350×430 sidebar dock: a turn whose answer had not arrived asked to scroll to
 * 653 with the spacer capped at half the viewport, the browser clamped it to
 * 595, and the pinned turn sat 58px below its 8px target — stably, because
 * every re-apply recomputed the same unreachable number and accepted the same
 * clamped result. Reserving what is actually needed puts it at 8px; asking for
 * only what the reserve can reach means the returned position is always real.
 */
export function computeAnchor({ clientHeight, contentHeight, elTop, maxSpacer }: AnchorInput): AnchorResult {
  const contentBelow = contentHeight - elTop;
  const ceiling = maxSpacer ?? Math.max(0, clientHeight - ANCHOR_TOP_GAP);
  const spacerHeight = Math.min(Math.max(0, clientHeight - contentBelow - ANCHOR_TOP_GAP), ceiling);
  // Never ask for a position the reserved slack cannot reach. The browser
  // clamps silently and the caller records the clamped value as "the anchor".
  const maxScrollTop = Math.max(0, contentHeight + spacerHeight - clientHeight);
  return {
    spacerHeight,
    scrollTop: Math.min(Math.max(0, elTop - ANCHOR_TOP_GAP), maxScrollTop),
  };
}

/**
 * A deliberate scroll down past an anchored turn hands control back to the
 * user. Scrolling upward is how someone reads earlier turns, so it must not
 * cancel the lock and cause the next streaming resize to snap them away.
 */
export function shouldReleaseAnchor(anchorTop: number, currentScrollTop: number, threshold = 150): boolean {
  return currentScrollTop > anchorTop + threshold;
}

/**
 * How far down counts as "the user took over", for a given viewport.
 *
 * A flat 150px was tuned for the floating card and is over half the visible
 * feed on the docked one (274px on a 350×430 rail) — so on the placement that
 * is the DEFAULT, taking control back meant scrolling most of a screen while
 * the anchor kept pulling. Proportional below that, and never past the value
 * the floating card already used.
 */
export function releaseThreshold(clientHeight: number): number {
  return Math.min(150, Math.max(80, Math.round(clientHeight * 0.4)));
}
