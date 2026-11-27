#!/usr/bin/env node
/**
 * Release check for a built Starling extension.
 *
 *   node scripts/verify-extension-build.mjs <dir> [--allow-local]
 *
 * <dir> is the folder holding manifest.json, or a build output folder with a
 * single chrome-mv3* child (e.g. .output-store). Exits 0 when every check
 * passes, 1 otherwise, and prints each failure.
 *
 * --allow-local accepts an http 127.0.0.1/localhost service origin, for e2e
 * builds only. A store build must pass without it.
 */
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { extname, join, relative } from "node:path";

const args = process.argv.slice(2);
const allowLocal = args.includes("--allow-local");
const target = args.find(arg => !arg.startsWith("--"));
if (!target) {
  console.error("usage: node scripts/verify-extension-build.mjs <build dir> [--allow-local]");
  process.exit(2);
}

function locate(dir) {
  if (existsSync(join(dir, "manifest.json"))) return dir;
  if (!existsSync(dir) || !statSync(dir).isDirectory()) return null;
  const builds = readdirSync(dir).filter(name => name.startsWith("chrome-mv3") && existsSync(join(dir, name, "manifest.json")));
  return builds.length === 1 ? join(dir, builds[0]) : null;
}

const root = locate(target);
if (!root) {
  console.error(`FAIL: no manifest.json in ${target} (or in exactly one chrome-mv3* folder inside it)`);
  process.exit(1);
}

const failures = [];
const fail = message => failures.push(message);

const X_HOSTS = ["https://x.com/*", "https://twitter.com/*"];
const EXPECTED_PERMISSIONS = ["storage", "identity"];
const ASCII = /^[\x20-\x7e]*$/;

function sameSet(actual, expected) {
  return Array.isArray(actual)
    && actual.length === expected.length
    && new Set(actual).size === actual.length
    && expected.every(value => actual.includes(value));
}

let manifest = null;
try {
  manifest = JSON.parse(readFileSync(join(root, "manifest.json"), "utf8"));
} catch (error) {
  fail(`manifest.json is not valid JSON: ${error.message}`);
}

if (manifest) {
  if (manifest.manifest_version !== 3) fail(`manifest_version must be 3; got ${JSON.stringify(manifest.manifest_version)}`);

  if (!sameSet(manifest.permissions ?? [], EXPECTED_PERMISSIONS)) {
    fail(`permissions must be exactly ${JSON.stringify(EXPECTED_PERMISSIONS)}; got ${JSON.stringify(manifest.permissions ?? [])}`);
  }
  for (const key of ["optional_permissions", "optional_host_permissions"]) {
    if (Array.isArray(manifest[key]) && manifest[key].length) fail(`${key} must be empty; got ${JSON.stringify(manifest[key])}`);
  }
  for (const key of ["key", "update_url", "externally_connectable", "oauth2", "content_security_policy", "chrome_url_overrides"]) {
    if (key in manifest) fail(`manifest must not contain "${key}"`);
  }

  let origin = null;
  try {
    const url = new URL(manifest.homepage_url);
    origin = url.origin;
    if (manifest.homepage_url !== origin) fail(`homepage_url must be the bare service origin; got ${manifest.homepage_url}`);
    const local = /^(?:localhost|127\.\d+\.\d+\.\d+|\[::1\])$/i.test(url.hostname);
    if (url.protocol !== "https:" && !(allowLocal && local)) fail(`homepage_url must use https; got ${manifest.homepage_url}`);
    if (local && !allowLocal) fail(`homepage_url points at a local host: ${manifest.homepage_url}`);
  } catch {
    fail(`homepage_url is missing or not a URL: ${JSON.stringify(manifest.homepage_url)}`);
  }
  if (origin) {
    const expectedHosts = [...X_HOSTS, `${origin}/*`];
    if (!sameSet(manifest.host_permissions ?? [], expectedHosts)) {
      fail(`host_permissions must be exactly ${JSON.stringify(expectedHosts)}; got ${JSON.stringify(manifest.host_permissions ?? [])}`);
    }
  }

  if (typeof manifest.name !== "string" || !manifest.name.trim()) fail("name is missing");
  else {
    if (!ASCII.test(manifest.name)) fail(`name must be printable ASCII; got ${JSON.stringify(manifest.name)}`);
    if (manifest.name.length > 75) fail(`name is ${manifest.name.length} characters; the limit is 75`);
  }
  if (typeof manifest.description !== "string" || !manifest.description.trim()) fail("description is missing");
  else {
    if (!ASCII.test(manifest.description)) fail(`description must be printable ASCII; got ${JSON.stringify(manifest.description)}`);
    if (manifest.description.length > 132) fail(`description is ${manifest.description.length} characters; the limit is 132`);
  }
  if (manifest.short_name !== undefined && (typeof manifest.short_name !== "string" || manifest.short_name.length > 12)) {
    fail(`short_name must be at most 12 characters; got ${JSON.stringify(manifest.short_name)}`);
  }
  if (!/^\d+\.\d+\.\d+$/.test(String(manifest.version))) fail(`version must be x.y.z; got ${JSON.stringify(manifest.version)}`);
  if (!allowLocal && manifest.version_name !== undefined) fail(`a store build carries no version_name; got ${JSON.stringify(manifest.version_name)}`);
  if (manifest.minimum_chrome_version !== "120") fail(`minimum_chrome_version must be "120"; got ${JSON.stringify(manifest.minimum_chrome_version)}`);

  const worker = manifest.background?.service_worker;
  if (typeof worker !== "string" || !existsSync(join(root, worker))) fail(`background.service_worker is missing: ${JSON.stringify(worker)}`);

  for (const script of manifest.content_scripts ?? []) {
    const outside = (script.matches ?? []).filter(match => !X_HOSTS.includes(match));
    if (outside.length) fail(`a content script runs outside X: ${JSON.stringify(outside)}`);
    if (script.world === "MAIN") fail("a content script runs in the page's MAIN world");
    if (script.all_frames) fail("a content script runs in all frames");
  }
  for (const entry of manifest.web_accessible_resources ?? []) {
    // A resource may be shown to the two X hosts and to nothing else: not to other sites, not to other
    // extensions (`extension_ids`), and an entry with no `matches` at all is not "nobody", it is unchecked.
    const matches = entry.matches ?? [];
    const outside = matches.filter(match => !X_HOSTS.includes(match));
    if (!matches.length || outside.length || (entry.extension_ids ?? []).length) {
      fail(`web_accessible_resources not limited to X: ${JSON.stringify(entry)}`);
    }
    const pages = (entry.resources ?? []).filter(resource => /\.(?:html?|js|mjs)$/i.test(resource));
    if (pages.length) fail(`web_accessible_resources exposes a page or script: ${JSON.stringify(pages)}`);
  }
}

// ---------------------------------------------------------------------------
// Bundle contents
// ---------------------------------------------------------------------------

const TEXT_EXTENSIONS = new Set([".js", ".mjs", ".cjs", ".html", ".css", ".json", ".txt", ".svg", ".map"]);
// The repository's release scanner forbids two of these words in any source
// file, this one included, so they are assembled from parts here.
const word = (...parts) => parts.join("");
const FORBIDDEN = [
  ["native messaging (connectNative)", /connectNative|sendNativeMessage/],
  ["native host name com.starling.local", /com\.starling\.local/],
  ["retired internal code name", new RegExp(word("For", "ge"))],
  ["\"local helper\" wording", /local helper/i],
  ["\"local AI\" wording", /\blocal AI\b/i],
  // Starling is a hosted service and an extension; nothing on the customer's computer runs it.
  ["\"companion\" wording", /companion/i],
  ["local model runtime name", new RegExp(word("olla", "ma"), "i")],
  ["browser LanguageModel API", /LanguageModel/],
  ["scripted scrolling (scrollBy()", /scrollBy\(/],
  ["source map reference", /sourceMappingURL/],
  // Remotely hosted or constructed code, which the Chrome Web Store forbids in Manifest V3 and which is
  // the one thing a "what does this package run" check exists to find.
  ["eval()", /(?<![.\w$])eval\s*\(/],
  ["new Function()", /new\s+Function\s*\(/],
  ["a script loaded from another host", /<script[^>]+src\s*=\s*["']?(?:https?:)?\/\//i],
  ["a module imported from another host", /import\s*\(\s*["'`]https?:/],
  ["importScripts from another host", /importScripts\s*\(\s*["'`]https?:/],
  ["hidden tab (tabs.create with active false)", /\.create\(\s*\{[^}]{0,300}\bactive\s*:\s*(?:false|!1)/],
];
if (!allowLocal) FORBIDDEN.push(["local service address", /https?:\/\/(?:127\.0\.0\.1|localhost)(?::\d+)?/]);

function walk(dir) {
  const files = [];
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) files.push(...walk(path));
    else files.push(path);
  }
  return files;
}

const files = walk(root);
let scanned = 0;
for (const path of files) {
  const rel = relative(root, path).replace(/\\/g, "/");
  const ext = extname(path).toLowerCase();
  if (ext === ".map") fail(`source map shipped: ${rel}`);
  if (!TEXT_EXTENSIONS.has(ext)) continue;
  scanned += 1;
  const text = readFileSync(path, "utf8");
  for (const [label, pattern] of FORBIDDEN) {
    const match = pattern.exec(text);
    if (match) {
      const at = Math.max(0, match.index - 40);
      fail(`${rel}: contains ${label}: ...${text.slice(at, match.index + match[0].length + 40).replace(/\s+/g, " ")}...`);
    }
  }
}

if (failures.length) {
  console.log(`FAIL: ${root} (${failures.length} problem${failures.length === 1 ? "" : "s"}, ${scanned} text files scanned)`);
  for (const message of failures) console.log(`  - ${message}`);
  process.exit(1);
}
console.log(`PASS: ${root}`);
console.log(`  manifest: ${manifest.name} ${manifest.version}; permissions ${JSON.stringify(manifest.permissions)}; host_permissions ${JSON.stringify(manifest.host_permissions)}`);
console.log(`  bundle: ${scanned} text files scanned, ${files.length} files total`);
