/**
 * One Chrome profile with the real, unpacked Starling extension loaded.
 *
 * - Playwright's cached Chromium, in the new headless mode (`channel: "chromium"`): extensions
 *   do not load in the old headless shell. `STARLING_E2E_HEADED=1` shows the window instead.
 * - A fresh profile folder per launch under `e2e/.run/browser/<run>/profiles/`, so every
 *   journey starts as a new install: `chrome.runtime.onInstalled` fires and onboarding opens.
 * - All traffic goes through the forward proxy (loopback only) and the `**\/*` route guard.
 */
import fs from "node:fs";
import path from "node:path";
import { resolveBrowsersPath } from "./browsers.mjs";
import { guardNetwork } from "./network.mjs";
import { EXTENSION_DIR, ensureDir } from "./paths.mjs";
import { registerStarlingEngine } from "./starling-ui.mjs";

const EXTENSION_ID_RE = /^[a-p]{32}$/;

/**
 * Chrome talks to Google on its own account — component updates, safe browsing, autofill,
 * password-leak checks, the optimisation-hints service. None of it can leave the machine (the
 * proxy refuses it), but every attempt is noise in the log a journey reads to prove the
 * EXTENSION asked for nothing it should not. Switching it off leaves a log about the product.
 */
const QUIET_CHROME = [
  "--disable-background-networking",
  "--disable-component-update",
  "--disable-domain-reliability",
  "--disable-client-side-phishing-detection",
  "--disable-sync",
  "--no-default-browser-check",
  "--no-first-run",
  "--no-pings",
  "--safebrowsing-disable-auto-update",
  "--disable-features=OptimizationHints,AutofillServerCommunication,PasswordLeakDetection,Translate,MediaRouter,InterestFeedContentSuggestions,CalculateNativeWinOcclusion,NetworkTimeServiceQuerying",
];

export async function launchStarling({
  profileDir, proxy, viewport = { width: 1280, height: 800 }, extension = true, clientIp, colorScheme,
} = {}) {
  // Playwright reads PLAYWRIGHT_BROWSERS_PATH when it is first imported, so resolve the cache first.
  resolveBrowsersPath();
  const { chromium, selectors } = await import("playwright");
  await registerStarlingEngine(selectors);
  if (extension && !fs.existsSync(path.join(EXTENSION_DIR, "manifest.json"))) {
    throw new Error(`no built extension in ${EXTENSION_DIR}: run "node e2e/browser/run.mjs" (it builds one)`);
  }
  ensureDir(profileDir);
  const args = [...QUIET_CHROME];
  if (extension) args.push(`--disable-extensions-except=${EXTENSION_DIR}`, `--load-extension=${EXTENSION_DIR}`);
  const context = await chromium.launchPersistentContext(profileDir, {
    channel: "chromium",
    headless: process.env.STARLING_E2E_HEADED !== "1",
    viewport,
    colorScheme,
    // The fake Stripe pages are https on 127.0.0.1 with a throwaway self-signed certificate.
    ignoreHTTPSErrors: true,
    proxy: proxy ? { server: proxy.url } : undefined,
    args,
  });
  context.setDefaultTimeout(15_000);
  context.setDefaultNavigationTimeout(20_000);
  // Each journey's browser looks like a customer on its own network. The gateway trusts
  // 127.0.0.1 as a proxy, so this is the address it rate-limits sign-ups by; without it every
  // account in a run would share one prefix and the tenth sign-up of the run would be refused.
  if (clientIp) await context.setExtraHTTPHeaders({ "X-Forwarded-For": clientIp });
  const guard = await guardNetwork(context);

  let extensionId = null;
  let worker = null;
  if (extension) {
    worker = context.serviceWorkers()[0] ?? await context.waitForEvent("serviceworker", { timeout: 20_000 });
    extensionId = new URL(worker.url()).host;
    if (!EXTENSION_ID_RE.test(extensionId)) throw new Error(`unexpected service worker url: ${worker.url()}`);
  }

  return {
    context,
    guard,
    proxy,
    extensionId,
    /** The extension's background worker. Chrome stops it when idle; a new one takes its place. */
    serviceWorker() {
      const live = context.serviceWorkers().find(candidate => candidate.url().startsWith(`chrome-extension://${extensionId}/`));
      return live ?? worker;
    },
    extensionUrl(file) {
      return `chrome-extension://${extensionId}/${file.replace(/^\//, "")}`;
    },
    /** The page Chrome opened on install, or a new one on the same URL. */
    async onboardingPage({ openIfMissing = true } = {}) {
      const url = `chrome-extension://${extensionId}/onboarding.html`;
      const deadline = Date.now() + 5_000;
      while (Date.now() < deadline) {
        const open = context.pages().find(page => page.url().startsWith(url));
        if (open) return open;
        await new Promise(resolve => setTimeout(resolve, 100));
      }
      if (!openIfMissing) return null;
      const page = await context.newPage();
      await page.goto(url);
      return page;
    },
    /** What the extension has stored, with the device token reduced to its presence. */
    async storedState() {
      return this.serviceWorker().evaluate(async () => {
        const all = await chrome.storage.local.get(null);
        const auth = all["starling:auth"];
        return {
          consent: all["starling:consent"] ?? null,
          signedOutReason: all["starling:signed-out-reason"] ?? null,
          outdated: all["starling:outdated"] ?? null,
          hasToken: Boolean(auth && typeof auth.token === "string" && auth.token.startsWith("stl_dev_")),
          deviceId: auth?.device_id ?? null,
        };
      });
    },
    /** `GET /api/v1/session` exactly as the extension sends it, made from the worker with its own token. */
    async apiSession() {
      return this.serviceWorker().evaluate(async () => {
        const auth = (await chrome.storage.local.get("starling:auth"))["starling:auth"];
        if (!auth?.token) return { status: 0, body: null };
        const origin = new URL(chrome.runtime.getManifest().homepage_url).origin;
        const response = await fetch(`${origin}/api/v1/session`, {
          headers: {
            Accept: "application/json",
            Authorization: `Bearer ${auth.token}`,
            "X-Starling-Extension": chrome.runtime.getManifest().version,
            "X-Starling-Time-Zone": "UTC",
          },
          credentials: "omit",
          cache: "no-store",
        });
        return { status: response.status, body: await response.json().catch(() => null) };
      });
    },
    async close() {
      await context.close().catch(() => undefined);
    },
  };
}
