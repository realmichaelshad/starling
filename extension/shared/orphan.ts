/**
 * Is this content script left over from a Starling that has since been updated or reloaded?
 *
 * Chrome does not remove a content script from open tabs when its extension updates. The old one
 * keeps running, but its line to the extension is cut: `chrome.runtime.id` becomes undefined and any
 * message throws "Extension context invalidated.". Every ✦ in that tab then fails with Chrome's raw
 * words and a Retry that fails again. Checking this first lets the page say what actually happened
 * and offer the one thing that fixes it: reloading X.
 *
 * `runtime` is a getter because reading `chrome.runtime` itself can throw once the context is gone.
 */
export function isContextOrphaned(runtime: () => { id?: string } | null | undefined): boolean {
  try {
    return !runtime()?.id;
  } catch {
    return true;
  }
}
