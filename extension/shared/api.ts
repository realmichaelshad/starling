// Content scripts never talk to the network. Every request goes through the
// background worker (runtime messages and the "starling-assist" port), which
// owns the device token and the HTTPS transport in shared/transport.ts.
export type { BridgeState } from "./session-state";

/**
 * The address of the page a post was seen on, as it is sent to the service: scheme, host and path.
 *
 * The query string and the fragment stay in the browser. On X they hold what the user searched for
 * (`/search?q=...`), share-tracking identifiers and the like; the service uses the address only to
 * tell a profile from a thread from the feed, which the path says. The disclosure lists the post and
 * nearby posts, not the user's search terms, so they are not sent.
 */
export function pageAddressForService(rawUrl: string): string {
  try {
    const url = new URL(rawUrl);
    if (url.protocol !== "https:" && url.protocol !== "http:") return "";
    return `${url.origin}${url.pathname}`;
  } catch {
    return ""; // an address that cannot be read is not sent at all
  }
}

export function pagePreferenceKey(rawUrl: string): string | null {
  try {
    const url = new URL(rawUrl);
    if (url.hostname !== "x.com" && url.hostname !== "twitter.com" && !url.hostname.endsWith(".x.com")) return null;
    const pathname = url.pathname.length > 1 ? url.pathname.replace(/\/+$/, "") : "/";
    return `${url.hostname}${pathname}${url.search}`;
  } catch {
    return null;
  }
}
