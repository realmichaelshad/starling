/**
 * Reaching Starling's UI on X from a test.
 *
 * Everything Starling adds to x.com (the dock, the nav item, the ✦ control on each post, the
 * Create/Enhance pill in a composer) lives in a CLOSED shadow root, so the page cannot read a
 * draft or an account number out of it. Playwright's selectors cannot enter a closed root
 * either. The Chrome DevTools Protocol can: `DOM.getDocument({ pierce: true })` lists closed
 * roots, and `DOM.resolveNode` turns one into a page object. `syncClosedRoots` hands each root
 * to the page under `window.__starlingE2ERoots`, and the `starling=` selector engine below
 * searches them. From there everything is an ordinary Playwright locator, with auto-waiting,
 * real mouse clicks and real key presses:
 *
 *     page.locator("starling=.foot-usage")                       // anywhere in Starling's UI
 *     page.locator("article").first().locator("starling=button.vf-post")   // the ✦ on one post
 *
 * No production code knows about any of this, and nothing is injected into the extension.
 */

export const STARLING_ENGINE = "starling";

/** Runs in the page (main world). `root` is the document or the element a locator is chained from. */
function createStarlingEngine() {
  const search = (root, selector) => {
    const roots = window.__starlingE2ERoots;
    if (!roots) return [];
    const found = [];
    for (const [host, shadow] of roots) {
      if (!host.isConnected) {
        roots.delete(host);
        continue;
      }
      if (root !== document && !(root instanceof Node && root.contains(host))) continue;
      found.push(...shadow.querySelectorAll(selector));
    }
    return found;
  };
  return {
    query: (root, selector) => search(root, selector)[0] ?? null,
    queryAll: (root, selector) => search(root, selector),
  };
}

let registered = false;

/** Register the `starling=` engine once per process, before any page is created. */
export async function registerStarlingEngine(selectors) {
  if (registered) return;
  registered = true;
  await selectors.register(STARLING_ENGINE, createStarlingEngine);
}

const sessions = new WeakMap();

async function cdpFor(page) {
  let session = sessions.get(page);
  if (!session) {
    session = await page.context().newCDPSession(page);
    sessions.set(page, session);
  }
  return session;
}

function collectClosedRoots(node, out) {
  for (const shadow of node.shadowRoots ?? []) {
    if (shadow.shadowRootType === "closed") out.push({ host: node.backendNodeId, root: shadow.backendNodeId });
    collectClosedRoots(shadow, out);
  }
  for (const child of node.children ?? []) collectClosedRoots(child, out);
  if (node.contentDocument) collectClosedRoots(node.contentDocument, out);
}

/** Hand every closed shadow root that exists right now to the `starling=` engine. Returns how many. */
export async function syncClosedRoots(page) {
  if (page.isClosed()) return 0;
  const cdp = await cdpFor(page);
  const { root } = await cdp.send("DOM.getDocument", { depth: -1, pierce: true });
  const pairs = [];
  collectClosedRoots(root, pairs);
  for (const pair of pairs) {
    const [host, shadow] = await Promise.all([
      cdp.send("DOM.resolveNode", { backendNodeId: pair.host }),
      cdp.send("DOM.resolveNode", { backendNodeId: pair.root }),
    ]);
    await cdp.send("Runtime.callFunctionOn", {
      objectId: shadow.object.objectId,
      functionDeclaration: "function (host) { (window.__starlingE2ERoots ||= new Map()).set(host, this); }",
      arguments: [{ objectId: host.object.objectId }],
    });
  }
  return pairs.length;
}

/**
 * Keep the engine's view current while a page is open: new ✦ controls appear as posts scroll
 * in and a composer pill appears when X opens a reply box, each with a root of its own.
 */
export function watchClosedRoots(page, intervalMs = 200) {
  let busy = false;
  let stopped = false;
  const tick = async () => {
    if (busy || stopped || page.isClosed()) return;
    busy = true;
    try {
      await syncClosedRoots(page);
    } catch {
      // Navigating or closing: the next tick sees the new document.
    } finally {
      busy = false;
    }
  };
  const timer = setInterval(tick, intervalMs);
  page.once("close", () => {
    stopped = true;
    clearInterval(timer);
  });
  void tick();
  return () => {
    stopped = true;
    clearInterval(timer);
  };
}

/** `starling=<css>` on the page, or chained from a locator to stay inside one post or composer. */
export function starling(scope, css) {
  return scope.locator(`${STARLING_ENGINE}=${css}`);
}

/**
 * Starling's own item in X's left nav ("✦ Starling"): a real click on it opens or closes the dock.
 * The extension's keyboard shortcut is a Chrome command (Alt+Shift+S by default), which Chrome
 * handles in the browser itself, so key presses a test sends into the page never reach it.
 */
export async function toggleDockFromNav(page) {
  await syncClosedRoots(page);
  await page.locator(`${STARLING_ENGINE}=button.vf-nav`).click();
}

/** The dock: the panel Starling mounts in X's right rail, opened from its nav item. */
export function dock(page) {
  const inDock = css => page.locator("#starling-chat").locator(`${STARLING_ENGINE}=${css}`);
  return {
    panel: inDock("section.panel"),
    /** The always-visible line: "24 of 25 drafts left · resets Oct 18", "Signed out", "Offline"... */
    footer: inDock(".foot-usage"),
    /** The card above the conversation that says what stands between the user and a draft. */
    stateCard: inDock("section.state .state-card"),
    stateTitle: inDock("section.state .state-title"),
    stateBody: inDock("section.state .state-body"),
    stateAction: name => inDock("section.state .state-card button").filter({ hasText: name }),
    healthDot: inDock(".dot"),
    feed: inDock(".feed"),
    cards: inDock(".feed > *"),
    input: inDock("form.composer textarea"),
    send: inDock("form.composer button.send"),
    gear: inDock("button.gear"),
    settings: inDock("section.settings"),
    locator: inDock,
    async open() {
      await syncClosedRoots(page);
      if (await inDock("section.panel").isVisible().catch(() => false)) return;
      await toggleDockFromNav(page);
      await syncClosedRoots(page);
      await inDock("section.panel").waitFor({ state: "visible" });
    },
  };
}

/** "24 of 25 drafts left · resets Oct 18" (or "24 drafts left") -> 24; null for the lines that carry no number. */
export function draftsLeft(footerText) {
  const match = /^(\d[\d,]*)\s+(?:of\s+\d[\d,]*\s+)?\S+\s+left\b/.exec(footerText.trim());
  return match ? Number(match[1].replace(/,/g, "")) : null;
}
