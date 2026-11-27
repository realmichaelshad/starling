/**
 * What every journey gets: a stack it can operate, a browser profile with the real extension
 * in it, a forward proxy that nothing can get past, a screenshot per step and a trace when
 * something fails.
 *
 * Playwright's own `page`/`browser` fixtures are deliberately unused: a Chrome extension only
 * loads into a persistent profile, which `chromium.launchPersistentContext` creates and the
 * built-in fixtures do not. `app` owns that context, and its teardown is where a journey's
 * network isolation is proved.
 */
import fs from "node:fs";
import path from "node:path";
import { expect, test as base } from "playwright/test";
import { launchStarling } from "../lib/browser.mjs";
import { assertNoEscape } from "../lib/network.mjs";
import { ensureDir, readSession } from "../lib/paths.mjs";
import { startProxy } from "../lib/proxy.mjs";

const session = readSession();

/**
 * One documentation-range address per test, so the per-prefix sign-up limit (ten an hour) is
 * never what fails a journey. The browser and the test's own HTTP client get different ones.
 */
let addressCounter = 0;
function clientIpFor(testInfo) {
  const file = Number(path.basename(testInfo.file).replace(/\D+/g, "") || 0);
  addressCounter += 1;
  return `2001:db8:${file.toString(16)}:${addressCounter.toString(16)}::1`;
}

function slug(value) {
  return value.replace(/[^a-z0-9]+/gi, "-").replace(/^-|-$/g, "").slice(0, 60).toLowerCase();
}

class StackControl {
  constructor(request, { controlUrl, controlToken, state }) {
    this.request = request;
    this.url = controlUrl;
    this.token = controlToken;
    this.state = state;
  }

  async call(pathname, body) {
    const response = await this.request.post(this.url + pathname, {
      data: body ?? {},
      headers: { "X-Starling-E2E": this.token },
      timeout: 300_000,
    });
    const payload = await response.json();
    if (response.status() !== 200) throw new Error(`${pathname} failed: ${payload.error ?? response.status()}`);
    return payload;
  }

  /** Restart the gateway with different settings, as an operator rolling out a change would. */
  restartGateway(overrides) {
    return this.call("/restart-gateway", { overrides });
  }

  restartServices(overrides) {
    return this.call("/restart-services", { overrides });
  }

  /** The admin credit debit: the only way to reach the monthly limit without 25 drafts. */
  debit(accountId, amount, reason = "e2e browser journey") {
    return this.call("/credits", { op: "debit", account_id: accountId, amount, reason });
  }

  grant(accountId, amount, reason = "e2e browser journey") {
    return this.call("/credits", { op: "grant", account_id: accountId, amount, reason });
  }

  /** `python -m starling_server.cli ...` against this run's database. */
  async cli(...args) {
    const result = await this.call("/cli", { args });
    if (result.code !== 0) throw new Error(`starling ${args.join(" ")} failed (${result.code}): ${result.stderr || result.stdout}`);
    return result;
  }

  async accountId(email) {
    const result = await this.call("/sql", { sql: "SELECT id FROM accounts WHERE email_norm = ?", params: [email.toLowerCase()] });
    if (!result.rows.length) throw new Error(`no account for ${email}`);
    return String(result.rows[0].id);
  }

  sql(sql, params = []) {
    return this.call("/sql", { sql, params }).then(result => result.rows);
  }
}

export const test = base.extend({
  /** What `run.mjs` wrote: the stack's address, this run's folder, the extension's id. */
  session: [async ({}, use) => { await use(session); }, { scope: "worker" }],

  gatewayUrl: [async ({}, use) => { await use(session.state.gateway_url); }, { scope: "worker" }],

  stack: [async ({ playwright }, use) => {
    const request = await playwright.request.newContext();
    await use(new StackControl(request, session));
    await request.dispose();
  }, { scope: "worker" }],

  /** An HTTP client that is not the browser: for accounts a journey only needs to exist. */
  api: async ({ playwright }, use, testInfo) => {
    const context = await playwright.request.newContext({
      baseURL: session.state.gateway_url,
      extraHTTPHeaders: { "X-Forwarded-For": clientIpFor(testInfo) },
    });
    await use(context);
    await context.dispose();
  },

  /**
   * The only door out of the browser. Its log is evidence in journeys 1 and 6.
   *
   * NOT called `proxy`: that name is one of Playwright Test's own options, and a fixture with the
   * same name silently becomes the proxy setting of every context the runner creates, including
   * `playwright.request.newContext()`, which then fails with "proxy.server: expected string".
   */
  egress: async ({}, use) => {
    const proxy = await startProxy();
    await use(proxy);
    await proxy.close();
  },

  /**
   * A Chrome profile with the real extension freshly installed. Created per test, so every
   * journey starts from `chrome.runtime.onInstalled` and its own empty storage.
   */
  app: async ({ egress: proxy }, use, testInfo) => {
    const runDir = process.env.STARLING_E2E_RUN_DIR;
    const profileDir = ensureDir(path.join(runDir, "profiles", `${slug(path.basename(testInfo.file, ".spec.mjs"))}-${testInfo.workerIndex}-${slug(testInfo.title)}`));
    const app = await launchStarling({ profileDir, proxy, clientIp: clientIpFor(testInfo) });
    await app.context.tracing.start({ screenshots: true, snapshots: true, sources: false });
    app.runDir = runDir;
    await use(app);

    // A trace is kept for a failure only: a passing run would otherwise write hundreds of
    // megabytes nobody opens.
    const tracePath = path.join(runDir, "traces", `${slug(path.basename(testInfo.file, ".spec.mjs"))}-${slug(testInfo.title)}.zip`);
    if (testInfo.status !== testInfo.expectedStatus) {
      await app.context.tracing.stop({ path: tracePath }).catch(() => undefined);
      testInfo.attachments.push({ name: "trace", path: tracePath, contentType: "application/zip" });
      fs.writeFileSync(tracePath.replace(/\.zip$/, "-requests.json"), JSON.stringify({
        guard: app.guard.requests, proxy: proxy.log,
      }, null, 2));
    } else {
      await app.context.tracing.stop().catch(() => undefined);
      // Isolation is only proved on a journey that finished the way it meant to; a failed one
      // has already reported the real problem.
      assertNoEscape(app.guard, proxy, expect);
    }
    await app.close();
  },

  /**
   * One screenshot per journey step, numbered in the order they were taken:
   * `e2e/.run/browser/<run>/screenshots/<journey>/03-draft-typed.png`.
   */
  shot: async ({}, use, testInfo) => {
    const folder = ensureDir(path.join(
      process.env.STARLING_E2E_RUN_DIR, "screenshots",
      `${slug(path.basename(testInfo.file, ".spec.mjs"))}-${slug(testInfo.title)}`,
    ));
    let step = 0;
    await use(async (page, label) => {
      step += 1;
      const file = path.join(folder, `${String(step).padStart(2, "0")}-${slug(label)}.png`);
      await page.screenshot({ path: file }).catch(() => undefined);
      return file;
    });
  },
});

export { expect };

/**
 * The negative control every journey group carries: run an assertion that MUST fail, and fail
 * the journey if it passes. A check that cannot fail proves nothing, and a check written
 * against the wrong hook (a renamed attribute, a selector that matches nothing) silently
 * becomes one.
 */
export async function assertionMustFail(what, body) {
  let failed = false;
  try {
    await body();
  } catch (error) {
    failed = true;
    if (!(error instanceof Error)) throw error;
  }
  if (!failed) throw new Error(`the negative control passed, so the check proves nothing: ${what}`);
}
