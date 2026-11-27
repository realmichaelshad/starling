/** How fast a finished draft is typed into X's composer. A browser-side preference only. */

export type TypingSpeed = "slow" | "normal" | "fast" | "instant";

export const TYPING_SPEEDS: ReadonlyArray<{ id: TypingSpeed; label: string }> = [
  { id: "slow", label: "Slow" },
  { id: "normal", label: "Normal" },
  { id: "fast", label: "Fast" },
  { id: "instant", label: "Instant" },
];

export function isTypingSpeed(value: unknown): value is TypingSpeed {
  return TYPING_SPEEDS.some(speed => speed.id === value);
}

/** Delay between typewriter ticks. "instant" writes the whole draft at once. */
export function typingDelayMs(speed: TypingSpeed): number {
  return speed === "slow" ? 40 : speed === "fast" ? 8 : speed === "instant" ? 0 : 16;
}
