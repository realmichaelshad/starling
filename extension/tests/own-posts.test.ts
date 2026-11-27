import { describe, expect, it } from "vitest";
import {
  IMPORT_RETRIES, OWN_POST_MAX_CHARS, OWN_POSTS_PER_PRESS, importRetryDelayMs, importSummary, isOwnProfilePath, ownPostTexts,
} from "../shared/own-posts";

describe("ownPostTexts: only the signed-in person's own writing becomes a sample", () => {
  it("keeps their posts and leaves everyone else's out, reposts included", () => {
    expect(ownPostTexts([
      { handle: "mayashipsml", text: "p95 is the only latency number users feel" },
      { handle: "someoneelse", text: "a post Maya reposted, written by somebody else" },
      { handle: null, text: "a post whose author could not be read at all" },
      { handle: "@MayaShipsML", text: "context windows are a budget, not a feature" },
    ], "mayashipsml")).toEqual([
      "p95 is the only latency number users feel",
      "context windows are a budget, not a feature",
    ]);
  });

  it("skips what is too short to carry a voice and anything seen twice", () => {
    expect(ownPostTexts([
      { handle: "maya", text: "lol" },
      { handle: "maya", text: "  turns out the bottleneck was the retry loop  " },
      { handle: "maya", text: "turns out the   bottleneck was the retry loop" },
    ], "maya")).toEqual(["turns out the bottleneck was the retry loop"]);
  });

  it("cuts to the engine's limit and stops at the per-press cap", () => {
    const long = "word ".repeat(1000);
    expect(ownPostTexts([{ handle: "maya", text: long }], "maya")[0].length).toBe(OWN_POST_MAX_CHARS);
    const many = Array.from({ length: 60 }, (_, index) => ({ handle: "maya", text: `a post of mine, number ${index}` }));
    expect(ownPostTexts(many, "maya")).toHaveLength(OWN_POSTS_PER_PRESS);
    expect(ownPostTexts(many, "")).toEqual([]);
  });
});

describe("isOwnProfilePath", () => {
  it("is their profile and its tabs, never another account or a single post", () => {
    expect(isOwnProfilePath("/mayashipsml", "mayashipsml")).toBe(true);
    expect(isOwnProfilePath("/MayaShipsML/with_replies", "mayashipsml")).toBe(true);
    expect(isOwnProfilePath("/mayashipsml/status/1900000000000000001", "mayashipsml")).toBe(false);
    expect(isOwnProfilePath("/someoneelse", "mayashipsml")).toBe(false);
    expect(isOwnProfilePath("/home", "mayashipsml")).toBe(false);
    expect(isOwnProfilePath("/", "mayashipsml")).toBe(false);
  });
});

describe("importRetryDelayMs: a new account's engine still starting is a wait, not a failure", () => {
  it("waits and retries the refusals that mean not yet", () => {
    expect(importRetryDelayMs("busy", null, 0)).toBe(2000);
    expect(importRetryDelayMs("busy", null, 1)).toBe(4000);
    expect(importRetryDelayMs("ai_unavailable", 5, 0)).toBe(5000);
    expect(importRetryDelayMs("rate_limited", 90, 0)).toBe(20000);  // capped: the person is watching
  });
  it("gives up after a few tries, and never retries a real refusal", () => {
    expect(importRetryDelayMs("busy", null, IMPORT_RETRIES)).toBeNull();
    expect(importRetryDelayMs("bad_request", null, 0)).toBeNull();
    expect(importRetryDelayMs("consent_required", null, 0)).toBeNull();
    expect(importRetryDelayMs(null, null, 0)).toBeNull();
  });
});

describe("importSummary", () => {
  it("says what happened in words", () => {
    expect(importSummary(14, 2, 0)).toBe("Added 14 of your posts. 2 were already there. Starling learns how you write from them.");
    expect(importSummary(1, 0, 0)).toBe("Added 1 of your posts. Starling learns how you write from them.");
    expect(importSummary(0, 3, 0)).toBe("3 were already there.");
    expect(importSummary(0, 0, 0)).toMatch(/Scroll down a little/);
    expect(importSummary(5, 0, 2)).toMatch(/2 couldn't be added right now/);
  });
});
