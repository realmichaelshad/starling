/**
 * The steps every journey is written out of: creating an account, the real extension sign-in,
 * opening the X fixture, taking a draft, and reading the same number out of the three places
 * that must agree.
 *
 * Everything here drives the product the way a person does — a form filled in, a button
 * pressed, a keyboard shortcut — except `apiContext`, which is test plumbing: it creates the
 * accounts that do not need a browser and reads the development outbox. That context is
 * outside the browser on purpose, so it never appears in the browser's own request log, which
 * journey 1 asserts against.
 */
import { expect } from "playwright/test";
import { dock, draftsLeft, starling, syncClosedRoots, toggleDockFromNav, watchClosedRoots } from "./starling-ui.mjs";

/** Long, with spaces: the release scanner reads a long unspaced literal as a secret. */
export const PASSWORD = "a long enough passphrase";

let emailCounter = 0;

export function uniqueEmail(prefix = "journey") {
  emailCounter += 1;
  return `${prefix}-${Date.now().toString(36)}-${emailCounter}@starling.example`;
}

// --------------------------------------------------------------------------- the website, over HTTP

/**
 * An HTTP client for the gateway that is NOT the browser: no proxy, no route guard, its own
 * cookie jar. `clientIp` becomes `X-Forwarded-For`, which the gateway trusts from 127.0.0.1,
 * so each account looks like it came from its own network — real customers do, and the
 * per-prefix sign-up limit would otherwise cap a run at ten accounts.
 */
export async function apiContext(request, gatewayUrl, clientIp) {
  return request.newContext({
    baseURL: gatewayUrl,
    extraHTTPHeaders: clientIp ? { "X-Forwarded-For": clientIp } : {},
  });
}

function csrfToken(html) {
  const match = /name="csrf_token"\s+value="([^"]*)"/.exec(html);
  if (!match) throw new Error("the page carried no CSRF token");
  return match[1];
}

async function postForm(api, gatewayUrl, pathname, fields, { tokenFrom = "/login" } = {}) {
  const page = await api.get(tokenFrom);
  const form = { ...fields, csrf_token: csrfToken(await page.text()) };
  return api.post(pathname, { form, headers: { Origin: gatewayUrl }, maxRedirects: 0 });
}

/** The link the development outbox holds for this address, e.g. `/verify-email`. */
export async function outboxLink(api, email, pathname) {
  const response = await api.get("/_dev/outbox", { params: { to: email } });
  expect(response.status(), "the development outbox answers").toBe(200);
  const messages = (await response.json()).messages ?? [];
  const pattern = new RegExp(`https?://[^\\s"']*${pathname}\\?token=[A-Za-z0-9_-]+`);
  for (const message of [...messages].reverse()) {
    const found = pattern.exec(message.body ?? "");
    if (found) return found[0];
  }
  throw new Error(`no ${pathname} link was sent to ${email}`);
}

/** Sign up and confirm the address without a browser, for the accounts a journey only needs to exist. */
export async function createVerifiedAccount(api, gatewayUrl, { email = uniqueEmail(), password = PASSWORD } = {}) {
  const signup = await postForm(api, gatewayUrl, "/signup", { email, password }, { tokenFrom: "/signup" });
  expect(signup.status(), `sign-up for ${email}`).toBe(303);
  const link = await outboxLink(api, email, "/verify-email");
  const token = new URL(link).searchParams.get("token");
  const verify = await postForm(api, gatewayUrl, "/verify-email", { token }, { tokenFrom: "/login" });
  expect(verify.status(), "confirming the address").toBe(303);
  return { email, password };
}

/** Open the emailed link on another device: an HTTP client with none of the browser's cookies. */
export async function confirmEmailElsewhere(api, gatewayUrl, email) {
  const link = await outboxLink(api, email, "/verify-email");
  const token = new URL(link).searchParams.get("token");
  const verify = await postForm(api, gatewayUrl, "/verify-email", { token }, { tokenFrom: "/login" });
  expect(verify.status(), "confirming the address on the other device").toBe(303);
  return verify;
}

// --------------------------------------------------------------------------- the website, in the browser

export async function signUpInBrowser(page, gatewayUrl, { email = uniqueEmail(), password = PASSWORD } = {}) {
  await page.goto(`${gatewayUrl}/signup`);
  await page.getByLabel("Email address").fill(email);
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Create account" }).click();
  await page.waitForURL("**/verify-email/sent");
  return { email, password };
}

/** Open the emailed link and press Confirm email. Confirming does NOT sign the visitor in. */
export async function confirmEmailInBrowser(page, api, email) {
  await page.goto(await outboxLink(api, email, "/verify-email"));
  await page.getByRole("button", { name: "Confirm email" }).click();
  await page.waitForLoadState();
}

export async function loginInBrowser(page, gatewayUrl, email, password = PASSWORD) {
  await page.goto(`${gatewayUrl}/login`);
  await page.getByLabel("Email address").fill(email);
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL(url => !url.pathname.startsWith("/login"));
}

// --------------------------------------------------------------------------- the extension

export async function acceptConsent(app) {
  const onboarding = await app.onboardingPage();
  await onboarding.bringToFront();
  await onboarding.getByRole("button", { name: "Agree and continue" }).click();
  await onboarding.getByRole("button", { name: "Sign in" }).waitFor();
  return onboarding;
}

/**
 * The real `chrome.identity.launchWebAuthFlow` sign-in. Chrome opens the authorisation window
 * as an ordinary page, so it can be filled in like any other; the redirect back to
 * `https://<extension id>.chromiumapp.org/connect` is caught by Chrome itself and never
 * becomes a network request.
 */
export async function signInExtension(app, onboarding, { email, password = PASSWORD } = {}) {
  const authPage = app.context.waitForEvent("page", { timeout: 30_000 });
  await onboarding.getByRole("button", { name: "Sign in" }).click();
  const auth = await authPage;
  await auth.waitForLoadState();
  if (auth.url().includes("/login")) {
    await auth.getByLabel("Email address").fill(email);
    await auth.getByLabel("Password", { exact: true }).fill(password);
    await auth.getByRole("button", { name: "Sign in" }).click();
  }
  await auth.getByRole("button", { name: "Connect this browser" }).click();
  await onboarding.getByRole("heading", { name: "You're all set" }).waitFor({ timeout: 30_000 });
  return auth;
}

/** Consent, sign in, and come back with the onboarding page still open. */
export async function installAndSignIn(app, account) {
  const onboarding = await acceptConsent(app);
  await signInExtension(app, onboarding, account);
  return onboarding;
}

// --------------------------------------------------------------------------- X

/** Open a page of the X fixture with the closed-shadow-root bridge running on it. */
export async function openX(app, pathname = "/home") {
  const page = await app.context.newPage();
  await page.goto(`https://x.com${pathname}`);
  watchClosedRoots(page);
  await page.waitForTimeout(600);
  await syncClosedRoots(page);
  return page;
}

/** The ✦ control on one post, reached through its closed shadow root. */
export function postStar(page, postId) {
  const article = postId ? page.locator(`article[data-post-id="${postId}"]`) : page.locator("article").first();
  return starling(article, "button.vf-post");
}

/**
 * Come back to this tab from another one, as a customer does after paying on the website. Chrome
 * tells the page with a `visibilitychange`, and Starling reads the account again then. The new
 * headless mode keeps every tab "visible" and never sends that event, so it is sent here by hand:
 * the same event, on the same document, that a real tab switch delivers.
 */
export async function returnToTab(page) {
  await page.bringToFront();
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  await syncClosedRoots(page);
}

/** Open the dock and make it ask the server for the current numbers, as a fresh open does. */
export async function refreshDock(page) {
  const panel = dock(page);
  await panel.open();
  await toggleDockFromNav(page); // closes
  await toggleDockFromNav(page); // re-opens, which refreshes the session
  await syncClosedRoots(page);
  await panel.panel.waitFor({ state: "visible" });
  return panel;
}

/**
 * Wait until the dock's footer reports exactly this many drafts left, without touching the panel.
 * A ✦ draft, an Enhance and a chat answer each carry the account's usage, and the panel shows it at
 * once: this used to close and reopen the dock to force a refresh, which hid that a ✦ draft never
 * moved the numbers. A change made outside this tab (a debit, a payment) is the caller's to bring in,
 * the way a customer does: by coming back to the tab, or opening the panel again (`refreshDock`).
 */
export async function expectFooterCount(page, expected, { timeout = 30_000 } = {}) {
  const deadline = Date.now() + timeout;
  let seen = null;
  while (Date.now() < deadline) {
    await syncClosedRoots(page);
    seen = await dock(page).footer.innerText().catch(() => null);
    if (seen && draftsLeft(seen) === expected) return seen;
    await page.waitForTimeout(500);
  }
  throw new Error(`the dock footer never reported ${expected} drafts left (last: ${JSON.stringify(seen)})`);
}

// --------------------------------------------------------------------------- the three numbers

/** What `/account` says, read from the structural hooks rather than any styling. */
export async function accountUsage(page, gatewayUrl) {
  await page.goto(`${gatewayUrl}/account`);
  const read = async name => {
    const cell = page.locator(`[data-usage="${name}"]`).first();
    return (await cell.count()) ? Number((await cell.innerText()).replace(/[^\d]/g, "")) : null;
  };
  return {
    used: await read("used"),
    remaining: await read("remaining"),
    allowance: await read("allowance"),
    plan: await page.locator("[data-plan]").first().getAttribute("data-plan"),
    notices: await page.locator("[data-usage-notice]").evaluateAll(nodes => nodes.map(node => node.dataset.usageNotice)),
  };
}

/**
 * The same number in all three places a customer can see it: the dock's footer in the
 * extension, `/account` on the website, and `GET /api/v1/session` as the extension itself asks
 * for it. A journey that only checked one of them would not notice the other two drifting.
 */
export async function expectUsageAgrees(app, { xPage, sitePage, gatewayUrl, remaining }) {
  const footer = await expectFooterCount(xPage, remaining);
  const api = await app.apiSession();
  expect(api.status, "GET /api/v1/session").toBe(200);
  expect(api.body.usage.remaining, "the API's remaining allowance").toBe(remaining);
  const account = await accountUsage(sitePage, gatewayUrl);
  expect(account.remaining, "the website's remaining allowance").toBe(remaining);
  expect(account.used, "the website's used count").toBe(api.body.usage.used);
  return { footer, api: api.body.usage, account };
}
