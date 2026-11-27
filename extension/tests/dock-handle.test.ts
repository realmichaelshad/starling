import { beforeEach, describe, expect, it, vi } from "vitest";
import { isShieldableKey, publishDockHandle, readDockHandle, type DockHandle } from "../shared/dock-handle";

const key = (init: Partial<KeyboardEvent>) => init as KeyboardEvent;

beforeEach(() => {
  delete (globalThis as Record<string, unknown>).__starlingDockHandle;
});

describe("dock handle", () => {
  it("is empty until the dock exists, then readable across content scripts", () => {
    expect(readDockHandle()).toBeNull();
    const handle = { host: {}, shadow: {}, onKeydown: vi.fn() } as unknown as DockHandle;
    publishDockHandle(handle);
    // The early script and the main script share one isolated world.
    expect(readDockHandle()).toBe(handle);
  });
});

describe("isShieldableKey", () => {
  it("shields the plain keys X steals", () => {
    for (const k of ["?", "n", "j", "k", "/", "l", "r", "a"]) {
      expect(isShieldableKey(key({ key: k }))).toBe(true);
    }
  });

  it("leaves modifier combos alone so Chrome's shortcuts and editing shortcuts survive", () => {
    expect(isShieldableKey(key({ key: "f", altKey: true }))).toBe(false);
    expect(isShieldableKey(key({ key: "a", ctrlKey: true }))).toBe(false);
    expect(isShieldableKey(key({ key: "v", metaKey: true }))).toBe(false);
  });
});
