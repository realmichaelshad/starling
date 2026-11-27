import { describe, expect, it } from "vitest";
import { ANCHOR_TOP_GAP, computeAnchor, releaseThreshold, shouldReleaseAnchor } from "../shared/feed-anchor";

describe("computeAnchor", () => {
  it("the anchored target is EXACTLY reachable: scrollTop equals maxScrollTop", () => {
    const feed = { clientHeight: 400, contentHeight: 1000, elTop: 900 };
    const { spacerHeight, scrollTop } = computeAnchor(feed);
    const maxScrollTop = feed.contentHeight + spacerHeight - feed.clientHeight;
    expect(scrollTop).toBe(feed.elTop - ANCHOR_TOP_GAP);
    expect(maxScrollTop).toBe(scrollTop);
  });

  it("no spacer when enough real content already sits below the anchor", () => {
    // contentBelow = 600 ≥ clientHeight − GAP → nothing to fill.
    expect(computeAnchor({ clientHeight: 400, contentHeight: 1500, elTop: 900 }).spacerHeight).toBe(0);
  });

  it("spacer shrinks monotonically to zero as the streamed answer grows", () => {
    const heights: number[] = [];
    for (const grown of [0, 80, 200, 392, 600]) {
      heights.push(computeAnchor({ clientHeight: 400, contentHeight: 1000 + grown, elTop: 900 }).spacerHeight);
    }
    for (let i = 1; i < heights.length; i += 1) expect(heights[i]).toBeLessThanOrEqual(heights[i - 1]);
    expect(heights[0]).toBe(400 - 100 - ANCHOR_TOP_GAP);
    expect(heights[heights.length - 1]).toBe(0);
  });

  it("clamps at the top for the first visible turn", () => {
    expect(computeAnchor({ clientHeight: 400, contentHeight: 120, elTop: 4 }).scrollTop).toBe(0);
  });

  // The regression this file exists for. Capping the spacer without lowering
  // the scroll target asks for a position the reserve cannot reach; the browser
  // clamps it silently and the caller stores the clamped value as "the anchor",
  // so the pinned turn sits low and never recovers. Reproduced on the real
  // sidebar dock at 350×430: requested 653, got 595, landed 58px down.
  it("never asks for a scrollTop the reserved spacer cannot reach", () => {
    for (const maxSpacer of [0, 20, 137, 500, undefined]) {
      for (const elTop of [0, 120, 661, 2000]) {
        const input = { clientHeight: 274, contentHeight: 718, elTop, maxSpacer };
        const { spacerHeight, scrollTop } = computeAnchor(input);
        const maxScrollTop = Math.max(0, input.contentHeight + spacerHeight - input.clientHeight);
        expect(scrollTop).toBeLessThanOrEqual(maxScrollTop);
        expect(spacerHeight).toBeLessThanOrEqual(maxSpacer ?? input.clientHeight - ANCHOR_TOP_GAP);
      }
    }
  });

  it("reaches the target exactly when the reserve is allowed to be big enough", () => {
    // The measured sidebar case: a turn whose answer has not arrived yet.
    const input = { clientHeight: 274, contentHeight: 718, elTop: 661 };
    const { spacerHeight, scrollTop } = computeAnchor(input);
    expect(scrollTop).toBe(input.elTop - ANCHOR_TOP_GAP);
    expect(input.contentHeight + spacerHeight - input.clientHeight).toBe(scrollTop);
  });

  it("a too-small cap lowers the target instead of overshooting it", () => {
    const input = { clientHeight: 274, contentHeight: 718, elTop: 661, maxSpacer: 137 };
    const { spacerHeight, scrollTop } = computeAnchor(input);
    expect(spacerHeight).toBe(137);
    // Short of the ideal, but honest and therefore stable across re-applies.
    expect(scrollTop).toBe(718 + 137 - 274);
    expect(scrollTop).toBeLessThan(input.elTop - ANCHOR_TOP_GAP);
  });

  it("is idempotent: re-applying with the same geometry does not drift", () => {
    const input = { clientHeight: 274, contentHeight: 718, elTop: 661 };
    const first = computeAnchor(input);
    const second = computeAnchor(input);
    expect(second).toEqual(first);
  });
});

describe("shouldReleaseAnchor", () => {
  it("releases only after a deliberate scroll down past the anchor", () => {
    expect(shouldReleaseAnchor(500, 500)).toBe(false);
    expect(shouldReleaseAnchor(500, 540)).toBe(false);
    expect(shouldReleaseAnchor(500, 649)).toBe(false);
    expect(shouldReleaseAnchor(500, 651)).toBe(true);
    expect(shouldReleaseAnchor(500, 460)).toBe(false);
    expect(shouldReleaseAnchor(500, 430)).toBe(false);
    expect(shouldReleaseAnchor(500, 560, 100)).toBe(false);
  });
});

describe("releaseThreshold", () => {
  it("is easier to escape on the docked card than a flat 150px allowed", () => {
    // 274px is the real feed viewport at the 350×430 sidebar size.
    expect(releaseThreshold(274)).toBe(110);
    expect(releaseThreshold(274)).toBeLessThan(150);
  });

  it("never exceeds what the floating card already used", () => {
    expect(releaseThreshold(424)).toBe(150);
    expect(releaseThreshold(2000)).toBe(150);
  });

  it("stays usable on a very short feed", () => {
    expect(releaseThreshold(120)).toBe(80);
    expect(releaseThreshold(0)).toBe(80);
  });
});
