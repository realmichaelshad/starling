/**
 * Shared handle to Starling's chat dock.
 *
 * The keyboard shield has to be registered at document_start — before X's app
 * boots — or X's own listeners sit upstream of it and win. The dock itself is
 * built much later, by the main content script. Content scripts of one
 * extension share a single isolated world per frame, so the main script
 * publishes the dock here and the early script reads it when a key arrives.
 */
export interface DockHandle {
  host: Element;
  shadow: ShadowRoot;
  /** The dock's own key behaviour (Enter sends, Escape closes). */
  onKeydown(event: KeyboardEvent): void;
}

const KEY = "__starlingDockHandle";

export function publishDockHandle(handle: DockHandle): void {
  (globalThis as Record<string, unknown>)[KEY] = handle;
}

export function readDockHandle(): DockHandle | null {
  return ((globalThis as Record<string, unknown>)[KEY] as DockHandle | undefined) ?? null;
}

/**
 * Modifier combos are left alone: Chrome's own shortcuts (Starling's "Open or
 * close Starling" command among them) and Ctrl/Cmd editing actions. X's
 * stealing hotkeys ("?", "n", "j", "k", "/", "l", "r") are all unmodified
 * single keys, so shielding those alone fixes the bug without side effects.
 */
export function isShieldableKey(event: KeyboardEvent): boolean {
  return !event.ctrlKey && !event.metaKey && !event.altKey;
}
