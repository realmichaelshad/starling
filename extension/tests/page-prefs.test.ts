import { describe, expect, it } from "vitest";
import { MAX_PAGE_PREF_BYTES, PAGE_PREF_KEYS, isPagePrefKey, parsePagePrefsRequest } from "../shared/page-prefs";

/**
 * A content script's preferences go through the worker, which closes `chrome.storage.local` (where
 * the device token lives) to content scripts. This is the gate on that message: a fixed list of keys
 * and nothing else, so code running in x.com's renderer can never ask the worker for the token, the
 * consent record, or anything that is not its own.
 */
describe("the content script's preference gateway", () => {
  it("has a fixed list of keys, and the token is not on it", () => {
    expect([...PAGE_PREF_KEYS].sort()).toEqual([
      "starling:disabled-pages", "starling:dock-rect-v3", "starling:first-run", "starling:near-limit-shown",
      "starling:pending-publications", "starling:settings",
    ]);
    for (const secret of ["starling:auth", "starling:consent", "starling:signed-out-reason", "starling:outdated"]) {
      expect(isPagePrefKey(secret), secret).toBe(false);
    }
  });

  it("remembers the first-run card through the same gate", () => {
    expect(parsePagePrefsRequest({ op: "set", items: { "starling:first-run": { shown_at: 1 } } }))
      .toEqual({ op: "set", items: { "starling:first-run": { shown_at: 1 } } });
    expect(parsePagePrefsRequest({ op: "get", keys: ["starling:first-run"] })).toEqual({ op: "get", keys: ["starling:first-run"] });
  });

  it("reads and removes only listed keys", () => {
    expect(parsePagePrefsRequest({ op: "get", keys: "starling:settings" })).toEqual({ op: "get", keys: ["starling:settings"] });
    expect(parsePagePrefsRequest({ op: "get", keys: ["starling:settings", "starling:dock-rect-v3", "starling:settings"] }))
      .toEqual({ op: "get", keys: ["starling:settings", "starling:dock-rect-v3"] });
    expect(parsePagePrefsRequest({ op: "remove", keys: ["starling:pending-publications"] }))
      .toEqual({ op: "remove", keys: ["starling:pending-publications"] });
    for (const keys of ["starling:auth", ["starling:settings", "starling:auth"], [], null, undefined, 5, {}, [null]]) {
      expect(parsePagePrefsRequest({ op: "get", keys }), JSON.stringify(keys)).toBeNull();
      expect(parsePagePrefsRequest({ op: "remove", keys }), JSON.stringify(keys)).toBeNull();
    }
  });

  it("never reads everything", () => {
    for (const message of [{ op: "get" }, { op: "get", keys: null }, { op: "get", keys: [] }, { op: "get", keys: ["*"] }]) {
      expect(parsePagePrefsRequest(message)).toBeNull();
    }
  });

  it("writes only listed keys, whole or not at all", () => {
    expect(parsePagePrefsRequest({ op: "set", items: { "starling:settings": { theme: "dark" } } }))
      .toEqual({ op: "set", items: { "starling:settings": { theme: "dark" } } });
    // One forbidden key refuses the whole write: nothing is half-applied.
    expect(parsePagePrefsRequest({ op: "set", items: { "starling:settings": {}, "starling:auth": { token: "x" } } })).toBeNull();
    for (const items of [null, undefined, [], "x", {}, { "starling:settings": undefined }]) {
      expect(parsePagePrefsRequest({ op: "set", items }), JSON.stringify(items)).toBeNull();
    }
  });

  it("refuses a value too large to be a preference", () => {
    const big = "x".repeat(MAX_PAGE_PREF_BYTES);
    expect(parsePagePrefsRequest({ op: "set", items: { "starling:settings": { note: big } } })).toBeNull();
    const circular: Record<string, unknown> = {};
    circular.self = circular;
    expect(parsePagePrefsRequest({ op: "set", items: { "starling:settings": circular } })).toBeNull();
  });

  it("refuses anything that is not one of the three operations", () => {
    for (const message of [null, undefined, "get", 5, [], {}, { op: "clear" }, { op: "getBytesInUse", keys: ["starling:settings"] }]) {
      expect(parsePagePrefsRequest(message), JSON.stringify(message)).toBeNull();
    }
  });
});
