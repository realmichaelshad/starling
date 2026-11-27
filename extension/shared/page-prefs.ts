/**
 * The content script's own preferences, kept for it by the background worker.
 *
 * The content script runs inside x.com's renderer. It used to read and write `chrome.storage.local`
 * directly, and that is the area the device token lives in: nothing structural stopped code in the
 * page's renderer from reading a bearer token that is valid for up to a year. Only a test that
 * searched the source for the key's name stood in the way.
 *
 * Now the worker sets `chrome.storage.local` to trusted contexts only (extension pages and the
 * worker itself), so a content script cannot open it at all, and the few things the content script
 * does need to remember go through one message with a fixed list of keys. The token is not on it.
 */

/** Every key a content script may ask for. Anything else is refused by the worker. */
export const PAGE_PREF_KEYS = [
  "starling:disabled-pages",
  "starling:pending-publications",
  "starling:settings",
  "starling:dock-rect-v3",
  "starling:near-limit-shown",
  // The one-time "You're ready to draft" card on the first signed-in X page.
  "starling:first-run",
] as const;

export type PagePrefKey = (typeof PAGE_PREF_KEYS)[number];

/** One preference is small. The largest real one is the pending-publication map, a few kilobytes. */
export const MAX_PAGE_PREF_BYTES = 64 * 1024;

export const PAGE_PREFS_KIND = "starling:page-prefs";

export type PagePrefsRequest =
  | { op: "get"; keys: PagePrefKey[] }
  | { op: "set"; items: Partial<Record<PagePrefKey, unknown>> }
  | { op: "remove"; keys: PagePrefKey[] };

export function isPagePrefKey(value: unknown): value is PagePrefKey {
  return typeof value === "string" && (PAGE_PREF_KEYS as readonly string[]).includes(value);
}

function keyList(value: unknown): PagePrefKey[] | null {
  const list = typeof value === "string" ? [value] : value;
  if (!Array.isArray(list) || list.length === 0 || list.length > PAGE_PREF_KEYS.length) return null;
  return list.every(isPagePrefKey) ? [...new Set(list)] : null;
}

/** Validate a message from a content script. Null means refuse: an unknown key, shape or an oversized value. */
export function parsePagePrefsRequest(message: unknown): PagePrefsRequest | null {
  if (typeof message !== "object" || message === null) return null;
  const { op, keys, items } = message as { op?: unknown; keys?: unknown; items?: unknown };
  if (op === "get" || op === "remove") {
    const list = keyList(keys);
    return list ? { op, keys: list } : null;
  }
  if (op !== "set" || typeof items !== "object" || items === null || Array.isArray(items)) return null;
  const entries = Object.entries(items as Record<string, unknown>);
  if (entries.length === 0 || entries.length > PAGE_PREF_KEYS.length) return null;
  const accepted: Partial<Record<PagePrefKey, unknown>> = {};
  for (const [key, value] of entries) {
    if (!isPagePrefKey(key) || value === undefined) return null;
    let size: number;
    try {
      size = JSON.stringify(value).length;
    } catch {
      return null;
    }
    if (size > MAX_PAGE_PREF_BYTES) return null;
    accepted[key] = value;
  }
  return { op: "set", items: accepted };
}

type Reply = { ok?: boolean; values?: Record<string, unknown> } | undefined;

async function ask(request: PagePrefsRequest): Promise<Reply> {
  return (await chrome.runtime.sendMessage({ kind: PAGE_PREFS_KIND, ...request })) as Reply;
}

/** Drop-in for the three `chrome.storage.local` calls the content script used. */
export const pagePrefs = {
  async get(keys: PagePrefKey | PagePrefKey[]): Promise<Record<string, unknown>> {
    const reply = await ask({ op: "get", keys: Array.isArray(keys) ? keys : [keys] });
    return reply?.ok && reply.values ? reply.values : {};
  },
  async set(items: Partial<Record<PagePrefKey, unknown>>): Promise<void> {
    const reply = await ask({ op: "set", items });
    if (!reply?.ok) throw new Error("Starling could not save this preference.");
  },
  async remove(keys: PagePrefKey | PagePrefKey[]): Promise<void> {
    await ask({ op: "remove", keys: Array.isArray(keys) ? keys : [keys] });
  },
};
