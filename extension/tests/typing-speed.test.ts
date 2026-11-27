import { describe, expect, it } from "vitest";
import { TYPING_SPEEDS, isTypingSpeed, typingDelayMs } from "../shared/typing-speed";

describe("typing speed (the one setting that outlived model settings)", () => {
  it("offers four speeds in order, slowest first", () => {
    expect(TYPING_SPEEDS.map(speed => speed.id)).toEqual(["slow", "normal", "fast", "instant"]);
    expect(TYPING_SPEEDS.map(speed => speed.label)).toEqual(["Slow", "Normal", "Fast", "Instant"]);
  });

  it("maps each speed to a tick delay, with instant writing at once", () => {
    expect(TYPING_SPEEDS.map(speed => typingDelayMs(speed.id))).toEqual([40, 16, 8, 0]);
  });

  it("recognises only the known speeds", () => {
    expect(isTypingSpeed("fast")).toBe(true);
    expect(isTypingSpeed("warp")).toBe(false);
    expect(isTypingSpeed(undefined)).toBe(false);
  });
});
