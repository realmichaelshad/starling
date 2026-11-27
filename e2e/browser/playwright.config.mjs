/**
 * Playwright Test's configuration for the browser journeys.
 *
 * The journeys do not use Playwright's own `browser`/`page` fixtures: every one of them needs a
 * persistent profile with the real unpacked extension loaded, which only
 * `chromium.launchPersistentContext` can do. `tests/fixtures.mjs` builds that context, and this
 * file only decides how the journeys are scheduled and where their evidence lands.
 *
 * - **Serial, one worker.** One stack, one gateway port, one extension id, and journeys that
 *   pause the whole service or restart the gateway. Nothing here may run beside anything else.
 * - **A fresh profile per journey file**, so every file starts from a real first install.
 * - Screenshots per step and a trace for a failure, under `e2e/.run/browser/<run>/`, never
 *   among the tracked files.
 */
import path from "node:path";
import { defineConfig } from "playwright/test";
import { resolveBrowsersPath } from "./lib/browsers.mjs";
import { BROWSER_RUN_ROOT } from "./lib/paths.mjs";

// `run.mjs` normally does this before the runner starts; repeat it for a bare `npx playwright test`.
resolveBrowsersPath();

const runDir = process.env.STARLING_E2E_RUN_DIR || path.join(BROWSER_RUN_ROOT, "adhoc");

export default defineConfig({
  testDir: "./tests",
  outputDir: path.join(runDir, "test-results"),
  fullyParallel: false,
  workers: 1,
  forbidOnly: true,
  retries: 0,
  // A journey signs up, signs in, drafts through a real engine worker and waits out the free
  // plan's rate limit; the first one also pays for the engine's cold start.
  timeout: 5 * 60_000,
  globalTimeout: 60 * 60_000,
  expect: { timeout: 20_000 },
  reporter: [
    ["list"],
    ["json", { outputFile: path.join(runDir, "journeys.json") }],
    ["html", { outputFolder: path.join(runDir, "report"), open: "never" }],
  ],
});
