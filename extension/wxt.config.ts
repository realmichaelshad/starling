import { defineConfig } from "wxt";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { validateStarlingOrigin } from "./shared/config";

/**
 * Build configuration for Starling, the hosted-service extension.
 *
 * WXT_STARLING_ORIGIN is the only address the extension ever talks to. It is
 * validated here when the configuration loads and compiled into the bundle
 * (see shared/config.ts for the rules). STARLING_STORE_BUILD=1 marks the
 * artifact uploaded to the Chrome Web Store: it refuses local and private
 * hosts and carries no development version name.
 *
 *   WXT_STARLING_ORIGIN=http://127.0.0.1:8765 wxt build --mode e2e
 *   STARLING_STORE_BUILD=1 WXT_STARLING_ORIGIN=https://app.yourcompany.com wxt build
 *
 * STARLING_OUT_DIR moves the output folder (e.g. .output-e2e, .output-store) so
 * two builds never overwrite each other. Without it, every build lands in
 * .output\chrome-mv3* (chrome-mv3 for production, chrome-mv3-e2e for e2e).
 */
const PACKAGE = JSON.parse(readFileSync(join(__dirname, "package.json"), "utf8")) as { version: string };
if (!/^\d+\.\d+\.\d+$/.test(PACKAGE.version)) {
  throw new Error(`package.json "version" must be x.y.z for the Chrome Web Store; got "${PACKAGE.version}".`);
}

const STORE_BUILD = process.env.STARLING_STORE_BUILD === "1";
const X_MATCHES = ["https://x.com/*", "https://twitter.com/*"];

// ASCII only, and within the store's limits (name 75, description 132).
const NAME = "Starling - AI writing assistant for X";
const DESCRIPTION = "Drafts replies, quotes and posts on X in your voice with Starling's hosted AI. Account required. You always press Post.";

let resolvedOrigin: { mode: string; origin: string } | null = null;

/** Validated once per mode; a bad value stops the build with the reason. */
function starlingOrigin(mode: string): string {
  if (resolvedOrigin?.mode !== mode) {
    resolvedOrigin = {
      mode,
      origin: validateStarlingOrigin(process.env.WXT_STARLING_ORIGIN, { mode, storeBuild: STORE_BUILD }),
    };
  }
  return resolvedOrigin.origin;
}

export default defineConfig({
  outDir: process.env.STARLING_OUT_DIR || ".output",
  vite: env => ({
    // Starling ships only as a Chrome MV3 extension; streaming uses
    // TextDecoderStream and pipeThrough, available from Chrome 120 on.
    build: { target: "chrome120", sourcemap: false },
    define: {
      __STARLING_ORIGIN__: JSON.stringify(starlingOrigin(env.mode)),
    },
  }),
  manifest: env => {
    const origin = starlingOrigin(env.mode);
    return {
      name: NAME,
      description: DESCRIPTION,
      version: PACKAGE.version,
      ...(STORE_BUILD ? {} : { version_name: `${PACKAGE.version} ${env.mode}` }),
      minimum_chrome_version: "120",
      homepage_url: origin,
      // storage: settings, consent and the device token (background only).
      // identity: chrome.identity.launchWebAuthFlow for sign-in.
      // Copy buttons use the async Clipboard API from a user click in the
      // focused X tab, which needs no extension permission.
      permissions: ["storage", "identity"],
      host_permissions: [...X_MATCHES, `${origin}/*`],
      // WXT only auto-registers icons named icon-16/icon/16 (with a separator);
      // ours are icon16.png, so declare them explicitly.
      icons: { 16: "icon16.png", 32: "icon32.png", 48: "icon48.png", 128: "icon128.png" },
      action: {
        // Chrome adds the bound shortcut to this tooltip by itself.
        default_title: "Open Starling",
        default_icon: { 16: "icon16.png", 32: "icon32.png" },
      },
      // A keyboard shortcut, not a permission: no install warning, and the customer can change it
      // at chrome://extensions/shortcuts. `_execute_action` arrives as a toolbar click, so the one
      // handler (background.ts onToolbarClick) serves both. The old page-level Alt+F never fired on
      // a Mac (Option+F types "ƒ") and fought Chrome's own menu key on Windows and Linux.
      commands: {
        _execute_action: {
          suggested_key: { default: "Alt+Shift+S", mac: "MacCtrl+Shift+S" },
          description: "Open or close Starling",
        },
      },
      // The dock's wordmark is loaded by the content script through
      // chrome.runtime.getURL, so x.com must be allowed to fetch it.
      web_accessible_resources: [
        { resources: ["assets/starling-wordmark.png"], matches: X_MATCHES },
      ],
    };
  },
});
