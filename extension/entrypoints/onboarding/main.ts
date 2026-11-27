import { BROADCAST_KINDS, MESSAGE_KINDS } from "../../shared/messages";
import { STARLING_ORIGIN, sitePageUrl } from "../../shared/config";
import { DISCLOSURE } from "../../shared/consent";
import { isBridgeState, type BridgeState } from "../../shared/session-state";
import { isErrorCode } from "../../shared/error-codes";
import { errorCopy } from "../../shared/usage-ui";

/**
 * First-run page: the data notice and the consent choice, then sign-in, then (for an address that is
 * not confirmed yet) the confirmation, then X.
 *
 * Opened by the background worker on install. It is an extension page, so it
 * talks to the background through runtime messages like every other part of
 * Starling, and it shows exactly the disclosure text the dock shows.
 */

const root = document.getElementById("app") as HTMLElement;
let state: BridgeState | null = null;
let busy = false;
/** What went wrong, in red; or, when `problemNeutral`, a plain note (the customer closed the window). */
let problem = "";
let problemNeutral = false;
/** The worker did not answer at all: the only fix is a reload of this page. */
let unreachable = false;
/** Set 8 s into a sign-in, when the window may have opened behind this one. */
let signInSlow = false;
let signInTimer: number | null = null;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function el<K extends keyof HTMLElementTagNameMap>(tag: K, className?: string, text?: string): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function button(label: string, kind: "primary" | "secondary", onClick: () => void, disabled = busy): HTMLButtonElement {
  const node = el("button", kind, label);
  node.type = "button";
  node.disabled = disabled;
  node.addEventListener("click", onClick);
  return node;
}

async function ask(message: Record<string, unknown>): Promise<Record<string, unknown> | null> {
  try {
    const reply: unknown = await chrome.runtime.sendMessage(message);
    return isRecord(reply) ? reply : null;
  } catch {
    return null;
  }
}

function adopt(reply: Record<string, unknown> | null) {
  if (reply && isBridgeState(reply.state)) state = reply.state;
}

function setProblem(reply: Record<string, unknown> | null) {
  problemNeutral = false;
  if (!reply) {
    unreachable = true;
    problem = "Starling isn't responding.";
    return;
  }
  if (reply.code === "cancelled") {
    // Closing the window is a choice, not a failure: say where things stand, in grey.
    problemNeutral = true;
    problem = "The sign-in window was closed. Sign in whenever you're ready.";
    return;
  }
  if (typeof reply.error === "string" && reply.error.trim()) problem = reply.error;
  else problem = isErrorCode(reply.code) ? errorCopy(reply.code).title : "Something went wrong. Try again.";
}

async function closeThisTab() {
  try {
    const tab = await chrome.tabs.getCurrent();
    if (tab?.id !== undefined) {
      await chrome.tabs.remove(tab.id);
      return;
    }
  } catch {
    /* fall back below */
  }
  window.close();
}

async function run(work: () => Promise<void>) {
  if (busy) return;
  busy = true;
  problem = "";
  problemNeutral = false;
  render();
  try {
    await work();
  } finally {
    busy = false;
    render();
  }
}

function siteLink(label: string, page: "privacy" | "data" | "account"): HTMLAnchorElement {
  const link = el("a", undefined, label);
  link.href = sitePageUrl(STARLING_ORIGIN, page);
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  return link;
}

/** "Alt+Shift+S" drawn as keys, or null when Chrome left the command unbound. */
function shortcutLine(): HTMLElement | null {
  const shortcut = state?.shortcut?.trim();
  if (!shortcut) return null;
  const line = el("p", "muted");
  line.append("Open or close Starling on X any time with ", el("kbd", undefined, shortcut), ".");
  return line;
}

function renderConsent(card: HTMLElement) {
  card.append(el("h1", undefined, DISCLOSURE.title), el("p", "lead", DISCLOSURE.intro));
  for (const section of DISCLOSURE.sections) {
    card.append(el("h2", undefined, section.heading));
    const list = el("ul");
    for (const item of section.items) list.append(el("li", undefined, item));
    card.append(list);
  }
  if (STARLING_ORIGIN) {
    const links = el("p", "links");
    links.append(siteLink("Privacy policy", "privacy"), siteLink("Your data", "data"));
    card.append(links);
  }

  const check = el("label", "check");
  const box = el("input");
  box.type = "checkbox";
  box.checked = false;
  box.disabled = busy;
  check.append(box, el("span", undefined, DISCLOSURE.passive.label), el("p", "muted", DISCLOSURE.passive.help));
  card.append(check);

  const actions = el("div", "actions");
  actions.append(
    button(DISCLOSURE.agree, "primary", () => void run(async () => {
      const reply = await ask({ kind: MESSAGE_KINDS.consent, payload: { action: "accept", passive_learning: box.checked } });
      adopt(reply);
      if (reply?.ok !== true) setProblem(reply);
    })),
    button(DISCLOSURE.decline, "secondary", () => void closeThisTab()),
  );
  card.append(actions);
}

function clearSignInTimer() {
  if (signInTimer !== null) window.clearTimeout(signInTimer);
  signInTimer = null;
  signInSlow = false;
}

function renderSignIn(card: HTMLElement) {
  const reason = state?.signed_out_reason;
  const title = reason === "revoked" ? "You were signed out on this browser"
    : reason === "expired" ? "Your sign-in expired"
      : "Sign in to Starling";
  // No sign-in method is promised here: which ones the service offers is its own setting.
  card.append(
    el("h1", undefined, title),
    el("p", "lead", "A sign-in window opens. Sign in, or create your free account there. No card needed."),
    el("p", "muted", "Then press Connect this browser. You'll come straight back here."),
  );
  const actions = el("div", "actions");
  actions.append(
    button(busy ? "Waiting for the sign-in window…" : "Sign in or create account", "primary", () => void run(async () => {
      clearSignInTimer();
      signInTimer = window.setTimeout(() => {
        signInSlow = true;
        render();
      }, 8_000);
      try {
        const reply = await ask({ kind: MESSAGE_KINDS.beginSignIn });
        adopt(reply);
        if (reply?.ok !== true) setProblem(reply);
      } finally {
        clearSignInTimer();
      }
    })),
    button("Later", "secondary", () => void closeThisTab()),
  );
  card.append(actions);
  if (busy && signInSlow) card.append(el("p", "note", "Can't see it? It may be behind this window."));
  card.append(el("p", "muted footnote", "You can sign in any time from ✦ Starling in X's sidebar."));
}

/** Signed in, but the address is not confirmed yet: every draft would be refused until it is. */
function renderConfirmEmail(card: HTMLElement) {
  const email = state?.session?.account.email_masked?.trim();
  card.append(
    el("h1", undefined, "One last step: confirm your email"),
    el("p", "lead", `We sent a link to ${email || "your email address"}. Open it, then come back here. This page updates by itself.`),
  );
  const actions = el("div", "actions");
  actions.append(
    button("I've confirmed it", "primary", () => void run(async () => {
      const reply = await ask({ kind: MESSAGE_KINDS.refreshSession });
      adopt(reply);
      if (!reply) setProblem(null);
      else if (state?.session && !state.session.account.email_verified) {
        problemNeutral = true;
        problem = "Not confirmed yet. Open the link in the email, then press I've confirmed it.";
      }
    })),
    // The account page has the resend form: the extension never sends mail itself.
    button("Resend the email", "secondary", () => void ask({ kind: MESSAGE_KINDS.openSite, payload: { page: "verify" } })),
  );
  card.append(actions);
}

function renderReady(card: HTMLElement) {
  const who = state?.session?.account.email_masked;
  card.append(
    el("h1", undefined, "You're all set"),
    el("p", "lead", who ? `Signed in as ${who}.` : "This browser is signed in."),
    el("p", undefined, "On X, press ✦ under any post and Starling writes a reply into X's reply box. Or press ✦ Create post beside Post. You always press Post yourself."),
  );
  const shortcut = shortcutLine();
  if (shortcut) card.append(shortcut);
  const actions = el("div", "actions");
  actions.append(
    button("Open X", "primary", () => void ask({ kind: MESSAGE_KINDS.openX })),
    button("Manage account", "secondary", () => void ask({ kind: MESSAGE_KINDS.openSite, payload: { page: "account" } })),
  );
  card.append(actions, el("p", "muted note", "If X is already open, Starling reloads that tab once so it can appear there."));
}

function render() {
  const card = el("section", "card");
  if (!state) {
    if (unreachable) {
      card.append(el("h1", undefined, "Starling isn't responding"), el("p", "lead", "Reload this page to try again."));
      const actions = el("div", "actions");
      actions.append(button("Reload this page", "primary", () => location.reload(), false));
      card.append(actions);
      root.replaceChildren(card);
      return;
    }
    card.append(el("p", "muted", "Loading…"));
  } else if (!state.configured) {
    card.append(
      el("h1", undefined, "Starling isn't set up"),
      el("p", undefined, "This build has no service address. Install Starling from the Chrome Web Store."),
    );
  } else if (!state.consent.accepted) {
    renderConsent(card);
  } else if (!state.signed_in) {
    renderSignIn(card);
  } else if (state.session && !state.session.account.email_verified) {
    renderConfirmEmail(card);
  } else {
    renderReady(card);
  }
  const alert = el("p", problemNeutral ? "problem neutral" : "problem", problem);
  alert.setAttribute("role", problemNeutral ? "status" : "alert");
  card.append(alert);
  if (unreachable && state) {
    const actions = el("div", "actions");
    actions.append(button("Reload this page", "secondary", () => location.reload(), false));
    card.append(actions);
  }
  root.replaceChildren(card);
}

chrome.runtime.onMessage.addListener((message: unknown, sender) => {
  if (sender.id !== chrome.runtime.id) return; // our own worker only, as background.ts checks its callers
  if (!isRecord(message) || message.kind !== BROADCAST_KINDS.bridgeState || !isBridgeState(message.state)) return;
  state = message.state;
  if (!busy) render();
});

// Back from the email (or from the website in another tab): look again at once, so "This page updates
// by itself" is true without waiting for the worker's cache.
document.addEventListener("visibilitychange", () => {
  if (document.hidden || busy || !state?.signed_in) return;
  if (state.session && state.session.account.email_verified) return;
  void ask({ kind: MESSAGE_KINDS.refreshSession }).then(reply => {
    adopt(reply);
    if (!busy) render();
  });
});

render();
void ask({ kind: MESSAGE_KINDS.getBridgeState }).then(reply => {
  adopt(reply);
  if (!reply) setProblem(null);
  render();
});
