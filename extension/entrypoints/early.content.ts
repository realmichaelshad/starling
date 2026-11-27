import { defineContentScript } from "wxt/utils/define-content-script";
import { isShieldableKey, readDockHandle } from "../shared/dock-handle";
import { shouldShieldTyping } from "../shared/keyboard-isolation";

/**
 * Runs at document_start for one reason: to register the typing shield before
 * X's app registers its keyboard shortcuts.
 *
 * X reads "is the user typing?" from the event target. Keys from Starling's
 * chat retarget to its host element, so X sees a plain <div>, decides nobody is
 * typing, and fires its shortcut — "?" opened the shortcuts dialog and "n" threw
 * the user into X's composer mid-sentence. The dock's shadow root already seals
 * bubble-phase listeners, but a capture-phase listener runs before the event
 * ever reaches the shadow tree, and whoever registers first wins. Hence this
 * script: it is the first listener on the page, so nothing downstream — either
 * phase, registered whenever — can see the user's typing.
 *
 * Stopping the event does not stop the typing: default actions are independent
 * of propagation (verified in a browser harness), so characters still land in
 * the box.
 */
export default defineContentScript({
  matches: ["https://x.com/*", "https://twitter.com/*"],
  runAt: "document_start",
  allFrames: false,
  main() {
    const shield = (event: Event) => {
      const key = event as KeyboardEvent;
      if (!isShieldableKey(key)) return;
      const dock = readDockHandle();
      if (!dock) return;
      const field = dock.shadow.querySelector<HTMLTextAreaElement>("textarea");
      // A page-level listener only ever sees the host; the closed root belongs
      // to us, so its activeElement is what proves the user is typing in it.
      if (!shouldShieldTyping(event, dock.host, dock.shadow.activeElement, field)) return;
      // The event stops here, so the dock's own shortcuts are handled here too.
      if (event.type === "keydown") dock.onKeydown(key);
      event.stopImmediatePropagation();
    };
    for (const type of ["keydown", "keypress", "keyup"]) {
      document.addEventListener(type, shield, true);
    }
  },
});
