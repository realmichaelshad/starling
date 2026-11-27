/**
 * Playwright's browser cache, resolved to its real location.
 *
 * Playwright looks in `%LOCALAPPDATA%\ms-playwright` (or `~/.cache/ms-playwright`). When this
 * process runs inside a packaged Windows app, that folder is a per-package redirect: Node can
 * read it, but Windows' side-by-side loader cannot, and `chrome.exe` dies before it starts with
 * "side-by-side configuration is incorrect" (`spawn UNKNOWN` in Playwright). Pointing
 * `PLAYWRIGHT_BROWSERS_PATH` at the folder's real path fixes that, and changes nothing on a
 * machine where the folder is an ordinary one. Nothing is ever downloaded here.
 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

function defaultCacheDir() {
  if (process.platform === "win32") {
    return path.join(process.env.LOCALAPPDATA || path.join(os.homedir(), "AppData", "Local"), "ms-playwright");
  }
  if (process.platform === "darwin") return path.join(os.homedir(), "Library", "Caches", "ms-playwright");
  return path.join(process.env.XDG_CACHE_HOME || path.join(os.homedir(), ".cache"), "ms-playwright");
}

/** Sets PLAYWRIGHT_BROWSERS_PATH when the cache is redirected; returns the folder in use. */
export function resolveBrowsersPath(env = process.env) {
  if (env.PLAYWRIGHT_BROWSERS_PATH) return env.PLAYWRIGHT_BROWSERS_PATH;
  const nominal = defaultCacheDir();
  let real = nominal;
  try {
    real = fs.realpathSync.native(nominal);
  } catch {
    return nominal;
  }
  if (path.resolve(real).toLowerCase() !== path.resolve(nominal).toLowerCase()) env.PLAYWRIGHT_BROWSERS_PATH = real;
  return real;
}
