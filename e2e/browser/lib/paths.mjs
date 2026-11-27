/**
 * Where things live. Everything a run writes goes under `e2e/.run/browser/` (ignored by git,
 * never committed); nothing is written outside the repository.
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const BROWSER_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
export const REPO_ROOT = path.resolve(BROWSER_DIR, "..", "..");
export const EXTENSION_SRC = path.join(REPO_ROOT, "extension");
export const RUN_ROOT = path.join(REPO_ROOT, "e2e", ".run");
export const BROWSER_RUN_ROOT = path.join(RUN_ROOT, "browser");

/** The unpacked build. Chrome derives an unpacked extension's id from this absolute path, so it never moves. */
export const EXTENSION_OUT_DIR = path.join(BROWSER_RUN_ROOT, "extension");
export const EXTENSION_DIR = path.join(EXTENSION_OUT_DIR, "chrome-mv3-e2e");

/** Part 1 owns `e2e/.run/state.json`; the browser journeys keep their own state file. */
export const STATE_FILE = path.join(RUN_ROOT, "browser-state.json");
/** What `run.mjs` hands to the test workers: the stack driver's address and this run's folder. */
export const SESSION_FILE = path.join(BROWSER_RUN_ROOT, "session.json");

/** The origin compiled into the e2e build (`extension/.env.e2e`). */
export const GATEWAY_PORT = 8765;
export const GATEWAY_URL = `http://127.0.0.1:${GATEWAY_PORT}`;

export function venvPython() {
  const windows = path.join(REPO_ROOT, "backend", ".venv", "Scripts", "python.exe");
  const posix = path.join(REPO_ROOT, "backend", ".venv", "bin", "python");
  if (fs.existsSync(windows)) return windows;
  if (fs.existsSync(posix)) return posix;
  throw new Error("backend/.venv was not found: create it first (see docs/TESTING.md)");
}

export function ensureDir(dir) {
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}

export function readJson(file) {
  return JSON.parse(fs.readFileSync(file, "utf8"));
}

export function readSession() {
  if (!fs.existsSync(SESSION_FILE)) {
    throw new Error(`no ${path.relative(REPO_ROOT, SESSION_FILE)}: start the journeys with "node e2e/browser/run.mjs"`);
  }
  return readJson(SESSION_FILE);
}
