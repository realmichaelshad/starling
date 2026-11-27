import { describe, expect, it } from "vitest";
import { isContextOrphaned } from "../shared/orphan";

describe("a content script left behind by an update", () => {
  it("is live while the extension still answers to its id", () => {
    expect(isContextOrphaned(() => ({ id: "abcdefghijklmnopabcdefghijklmnop" }))).toBe(false);
  });

  it("is orphaned once the id is gone, the runtime is gone, or reading it throws", () => {
    expect(isContextOrphaned(() => ({ id: undefined }))).toBe(true);
    expect(isContextOrphaned(() => ({}))).toBe(true);
    expect(isContextOrphaned(() => undefined)).toBe(true);
    expect(isContextOrphaned(() => null)).toBe(true);
    expect(isContextOrphaned(() => { throw new Error("Extension context invalidated."); })).toBe(true);
  });
});
