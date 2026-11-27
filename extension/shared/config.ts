/**
 * Where Starling's hosted service lives, fixed when the extension is built.
 *
 * `WXT_STARLING_ORIGIN` is checked by `validateStarlingOrigin` when
 * wxt.config.ts loads, then compiled into the bundle as a literal. Nothing at
 * runtime (a page, a message, a stored value) can point the extension at a
 * different server, and every other address the extension uses is derived from
 * that one origin.
 *
 * Pure: no chrome.* and no DOM, so the rules are unit-tested and the build
 * config can import the validator.
 */

declare const __STARLING_ORIGIN__: string | undefined;

/** The compiled-in origin, or "" where nothing was compiled in (unit tests). */
export const STARLING_ORIGIN: string = typeof __STARLING_ORIGIN__ === "string" ? __STARLING_ORIGIN__ : "";

export function apiBaseFor(origin: string): string {
  return `${origin}/api/v1`;
}

/** Base of every extension <-> gateway route (CONTRACTS section 6). */
export const API_BASE = apiBaseFor(STARLING_ORIGIN);

/** Website pages the extension may open. Nothing outside this map is ever opened. */
export const SITE_PAGES = {
  pricing: "/pricing?from=extension",
  limit: "/pricing?from=extension&reason=limit",
  account: "/account",
  billing: "/account/billing",
  devices: "/account/devices",
  verify: "/account",
  data: "/account/data",
  privacy: "/privacy",
  // "Contact support" on a server error opens this, with the support reference already copied.
  contact: "/contact",
} as const;

export type SitePage = keyof typeof SITE_PAGES;

/** Where sign-in starts (CONTRACTS section 9). */
export const CONNECT_PATH = "/connect";
/** The page Chrome opens after the extension is removed. */
export const UNINSTALL_PATH = "/extension/removed";

export function isSitePage(value: unknown): value is SitePage {
  return typeof value === "string" && Object.prototype.hasOwnProperty.call(SITE_PAGES, value);
}

export function sitePageUrl(origin: string, page: SitePage): string {
  return `${origin}${SITE_PAGES[page]}`;
}

/**
 * A server-supplied path (`upgrade_path`) is honoured only as a same-origin
 * path: exactly one leading "/", no scheme or host, no backslash, no whitespace
 * or control characters.
 */
export function isSafeSitePath(value: unknown): value is string {
  if (typeof value !== "string" || value.length === 0 || value.length > 512) return false;
  if (!value.startsWith("/") || value.startsWith("//")) return false;
  if (/[\\\s\x00-\x1f\x7f]/.test(value)) return false;
  const probe = "https://origin.invalid";
  try {
    return new URL(value, probe).origin === probe;
  } catch {
    return false;
  }
}

/** The allowlisted page a safe server path names, or null. */
export function sitePageForPath(value: unknown): SitePage | null {
  if (!isSafeSitePath(value)) return null;
  for (const [page, path] of Object.entries(SITE_PAGES)) {
    if (path === value) return page as SitePage;
  }
  return null;
}

// ---------------------------------------------------------------------------
// Build-time origin validation (used by wxt.config.ts)
// ---------------------------------------------------------------------------

export interface OriginRules {
  /** The WXT build mode: "production", "development", "e2e", ... */
  mode: string;
  /** True for the artifact uploaded to the Chrome Web Store (STARLING_STORE_BUILD=1). */
  storeBuild: boolean;
}

const TEMPLATE_MARKER = /\[\[|\]\]|\{\{|\}\}|[<>]|\$\{|%[A-Z_]+%/;
/** RFC 2606's documentation domains: the commonest "I'll fill this in later" value there is. */
const DOCUMENTATION_HOST = /(?:^|\.)example(?:\.(?:com|net|org|edu))?$/;
const PLACEHOLDER_LABEL = /^(?:placeholder|your-?domain|your-?origin|your-?company|your-?app|your-?host|change-?me|replace-?me|todo|tbd|x{3,})$/i;
const LOCAL_SUFFIXES = [".localhost", ".local", ".internal", ".test", ".invalid", ".example", ".lan", ".home.arpa", ".corp"];

/** True for loopback, private, link-local and reserved local names. */
export function isLocalHost(host: string): boolean {
  const name = host.toLowerCase();
  if (name === "localhost" || LOCAL_SUFFIXES.some(suffix => name.endsWith(suffix))) return true;
  const v4 = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(name);
  if (v4) {
    const a = Number(v4[1]);
    const b = Number(v4[2]);
    return a === 127 || a === 10 || a === 0
      || (a === 172 && b >= 16 && b <= 31)
      || (a === 192 && b === 168)
      || (a === 169 && b === 254)
      || (a === 100 && b >= 64 && b <= 127);
  }
  if (name.startsWith("[")) {
    const inner = name.slice(1, -1);
    return inner === "::1" || inner === "::" || /^f[cd]/.test(inner) || /^fe[89ab]/.test(inner) || inner.startsWith("::ffff:");
  }
  return false;
}

export function isIpLiteral(host: string): boolean {
  return /^\d{1,3}(?:\.\d{1,3}){3}$/.test(host) || host.startsWith("[");
}

/**
 * The one gate for WXT_STARLING_ORIGIN. Returns the normalized origin or throws
 * with a message that says exactly what to fix.
 *
 * - origin only: scheme, host and optional port; no path, query or credentials
 * - https, except http on 127.0.0.1 or localhost in an e2e build
 * - a store build refuses local, private and IP-literal hosts
 * - every build refuses template placeholders
 */
export function validateStarlingOrigin(raw: string | undefined, rules: OriginRules): string {
  const value = (raw ?? "").trim();
  if (!value) {
    throw new Error("WXT_STARLING_ORIGIN is not set. Build with WXT_STARLING_ORIGIN=https://<service host>.");
  }
  if (TEMPLATE_MARKER.test(value)) {
    throw new Error(`WXT_STARLING_ORIGIN still holds a placeholder: "${value}".`);
  }
  const bare = value.endsWith("/") ? value.slice(0, -1) : value;
  if (!/^https?:\/\/[^/?#@\\\s]+$/i.test(bare)) {
    throw new Error(`WXT_STARLING_ORIGIN must be an origin only (scheme://host[:port]) with no path, query, fragment or credentials; got "${value}".`);
  }
  let url: URL;
  try {
    url = new URL(bare);
  } catch {
    throw new Error(`WXT_STARLING_ORIGIN is not a valid URL: "${value}".`);
  }
  const host = url.hostname.toLowerCase();
  if (host.split(".").some(label => PLACEHOLDER_LABEL.test(label))) {
    throw new Error(`WXT_STARLING_ORIGIN still holds a placeholder host: "${value}".`);
  }
  if (DOCUMENTATION_HOST.test(host)) {
    // example.com, .net and .org are real, resolvable hosts that belong to IANA. A package built with
    // one sends every device token and every post the user asks about to a server nobody here runs.
    throw new Error(`WXT_STARLING_ORIGIN still holds a documentation host: "${value}". Use your own service address.`);
  }
  if (url.protocol === "http:") {
    const loopback = host === "127.0.0.1" || host === "localhost";
    if (!(rules.mode === "e2e" && loopback)) {
      throw new Error(`WXT_STARLING_ORIGIN must use https. Plain http is allowed only for 127.0.0.1 or localhost in an e2e build (--mode e2e); got "${value}".`);
    }
  } else if (url.protocol !== "https:") {
    throw new Error(`WXT_STARLING_ORIGIN must use https; got "${value}".`);
  }
  if (rules.storeBuild && (isLocalHost(host) || isIpLiteral(host))) {
    throw new Error(`A store build (STARLING_STORE_BUILD=1) must use the public service host name, not "${value}".`);
  }
  return url.origin;
}
