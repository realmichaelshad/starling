import { describe, expect, it } from "vitest";
import { MAX_THREAD_PARTS, boxIndexForPart, planThread, threadProgressLine } from "../shared/thread-build";

describe("planning a thread before touching the composer", () => {
  it("plans one new box per part after the first", () => {
    const plan = planThread(["one", "two", "three"]);
    expect(plan.parts).toHaveLength(3);
    expect(plan.boxesToAdd).toBe(2);
    expect(plan.skipped).toBeUndefined();
  });

  it("refuses to call a single part a thread", () => {
    expect(planThread(["only one"]).boxesToAdd).toBe(0);
    expect(planThread(["only one"]).skipped).toContain("at least two");
    expect(planThread([]).parts).toEqual([]);
    expect(planThread(undefined).skipped).toBe("no thread parts");
  });

  it("drops blank parts rather than adding an empty box", () => {
    const plan = planThread(["real", "   ", "", "also real"]);
    expect(plan.parts).toEqual(["real", "also real"]);
    expect(plan.boxesToAdd).toBe(1);
  });

  it("refuses when a part would be truncated by X", () => {
    const plan = planThread(["fine", "x".repeat(281)]);
    expect(plan.parts).toEqual([]);
    expect(plan.skipped).toContain("part 2");
  });

  it("counts emoji as X counts them, not as code units", () => {
    // 140 astral emoji = 140 characters to X, not 280.
    const plan = planThread(["fine", "🔥".repeat(140)]);
    expect(plan.skipped).toBeUndefined();
    expect(plan.boxesToAdd).toBe(1);
  });

  it("caps at X's own maximum", () => {
    const many = Array.from({ length: 40 }, (_, i) => `part ${i}`);
    expect(planThread(many).parts).toHaveLength(MAX_THREAD_PARTS);
  });

  it("never writes into a box that does not exist yet", () => {
    expect(boxIndexForPart(0, 1)).toBe(0);
    expect(boxIndexForPart(2, 3)).toBe(2);
    // Part 3 while only two boxes are rendered: wait, do not overwrite part 2.
    expect(boxIndexForPart(2, 2)).toBe(-1);
    expect(boxIndexForPart(-1, 5)).toBe(-1);
  });

  it("reports progress in human terms", () => {
    expect(threadProgressLine(0, 3)).toBe("Writing part 1 of 3…");
    expect(threadProgressLine(2, 3)).toBe("Writing part 3 of 3…");
    expect(threadProgressLine(3, 3)).toContain("Thread ready — 3 parts");
  });
});
