import { describe, expect, it } from "vitest";
import { composerMode } from "../shared/composer-flow";

describe("dual-mode composer button", () => {
  it("empty composer means create — X's disabled submit must not block it", () => {
    expect(composerMode({ text: "", isComposing: false, xAcceptsDraft: false })).toBe("create");
    expect(composerMode({ text: "   \n ", isComposing: false, xAcceptsDraft: false })).toBe("create");
  });

  it("any text means enhance — Starling only drafts into the box, never submits", () => {
    expect(composerMode({ text: "my rough draft", isComposing: false, xAcceptsDraft: true })).toBe("enhance");
    // X's own submit-readiness must NOT disable the pill: a dead click when X's
    // button detection is momentarily off is the bug we are fixing here.
    expect(composerMode({ text: "draft while X's submit looks not-ready", isComposing: false, xAcceptsDraft: false })).toBe("enhance");
  });

  it("IME composition always blocks", () => {
    expect(composerMode({ text: "", isComposing: true, xAcceptsDraft: false })).toBe("blocked");
    expect(composerMode({ text: "draft", isComposing: true, xAcceptsDraft: true })).toBe("blocked");
  });
});
