/**
 * Keyboard isolation for Starling's shadow UI.
 *
 * X handles its hotkeys with page-level listeners that decide "is the user
 * typing?" from the event target. Events raised inside a shadow root retarget
 * to the HOST element on their way out — a plain <div>/<span> — so X concludes
 * nothing is focused and runs its shortcut instead: "?" opens the keyboard
 * shortcuts dialog, "n" jumps into X's post composer, "/" focuses search, "j"
 * and "k" scroll the feed. The user is mid-sentence in Starling's chat and X
 * steals the keystroke.
 *
 * Sealing the shadow root ends that at the source. These listeners sit at the
 * top of the shadow tree, so everything inside Starling has already handled
 * the event (bubble order) by the time the seal stops it from leaving the
 * host. Starling's own page-level handlers (the composer-edit flush) are
 * registered in the CAPTURE phase and therefore still see every key.
 */

/** Events that must never escape Starling's UI into X's page handlers. */
export const SEALED_EVENTS = [
  "keydown",
  "keypress",
  "keyup",
  "beforeinput",
  "input",
  "paste",
  "cut",
  "copy",
  "compositionstart",
  "compositionupdate",
  "compositionend",
] as const;

export interface SealOptions {
  /**
   * Seal only events coming from a real editable field inside the shadow.
   * Used for button-only surfaces (pills, cards) so a key pressed while one
   * of them happens to hold focus still reaches X — only genuine typing is
   * ever withheld.
   */
  editableOnly?: boolean;
}

/** Is this node a field the user types into? */
export function isEditableTarget(node: EventTarget | null | undefined): boolean {
  const element = node as HTMLElement | null;
  if (!element || element.nodeType !== 1) return false;
  const tag = element.tagName;
  if (tag === "TEXTAREA" || tag === "INPUT" || tag === "SELECT") return true;
  return element.isContentEditable === true;
}

/** True when this event should be stopped at the shadow boundary. */
export function shouldSealEvent(event: Event, options: SealOptions = {}): boolean {
  if (!options.editableOnly) return true;
  const path = typeof event.composedPath === "function" ? event.composedPath() : [];
  return isEditableTarget(path[0] ?? event.target);
}

/**
 * Second layer, for the one surface the user actually types into.
 *
 * Verified in a browser harness: a shadow-root seal stops page hotkeys wired
 * in the BUBBLE phase, but a CAPTURE-phase listener on document still sees the
 * key — it runs before the event ever reaches the shadow tree. Stopping the
 * event during capture is the only way to beat that, and it is safe: a stopped
 * event still performs its default action, so the character still lands in the
 * field (the harness typed a full sentence through such a shield).
 *
 * Identification is the subtle part. A page-level listener CANNOT see inside
 * the shadow: `event.target` is retargeted to the host, and `composedPath()`
 * is trimmed to start at the host as well — an inner-node check silently never
 * matches (it didn't, until the harness caught it). What a page-level listener
 * can trust is the retargeted host plus, because we hold the closed root
 * ourselves, `shadowRoot.activeElement`.
 *
 * True when the event came from our dock and its field really holds focus.
 *
 * REAL keystrokes only. The shield does not just stop propagation: it hands the key to the dock's
 * own handler, and Enter there SENDS the message. Focus inside a closed root is visible from the
 * page (`document.activeElement` is the host), so X's script, or anything injected into x.com, could
 * wait until the user was typing and dispatch a synthetic Enter at the host: a half-written message
 * and its attachments sent, and a draft used, at a moment the page chose. A key the page made up has
 * `isTrusted === false`; shielding one is pointless anyway, because nothing was typed.
 */
export function shouldShieldTyping(
  event: Event,
  host: Element | null | undefined,
  shadowActiveElement: Element | null | undefined,
  field: Element | null | undefined,
): boolean {
  if (!host || !field) return false;
  if ((event as { isTrusted?: boolean }).isTrusted === false) return false;
  if (event.target !== host) return false;
  return shadowActiveElement === field;
}

/**
 * Stop keyboard/clipboard/IME events from escaping a shadow root.
 * Returns a function that removes the seal.
 */
export function sealShadowKeyboard(root: ShadowRoot | null | undefined, options: SealOptions = {}): () => void {
  if (!root) return () => {};
  const seal = (event: Event) => {
    if (shouldSealEvent(event, options)) event.stopPropagation();
  };
  for (const type of SEALED_EVENTS) root.addEventListener(type, seal);
  return () => {
    for (const type of SEALED_EVENTS) root.removeEventListener(type, seal);
  };
}
