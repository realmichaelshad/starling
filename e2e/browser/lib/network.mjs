/**
 * The rule for every browser context: a request goes to 127.0.0.1 or localhost, is answered
 * from the X fixture, or is aborted and remembered as a violation.
 *
 * `context.route("**\/*")` sees page requests and, since Playwright 1.5x, the requests of the
 * extension's service worker too (the spike in `spikes/` prints both). The forward proxy in
 * `proxy.mjs` is the second line: even a request this handler never saw cannot leave the machine.
 */
import { isXHost, xFixtureResponse } from "./x-fixture.mjs";

const LOCAL_HOSTS = new Set(["127.0.0.1", "localhost", "[::1]"]);
const BROWSER_SCHEMES = new Set(["chrome-extension:", "chrome:", "devtools:", "data:", "blob:", "about:"]);

export async function guardNetwork(context) {
  /** Every request the browser made, as `{ n, at, url, method, from, verdict }`. */
  const requests = [];
  /** The ones that were neither local nor the fixture. A journey with any of these fails. */
  const violations = [];
  let counter = 0;

  await context.route("**/*", async route => {
    const request = route.request();
    const url = new URL(request.url());
    counter += 1;
    const entry = {
      n: counter,
      at: Date.now(),
      url: url.href,
      host: url.host,
      path: url.pathname,
      method: request.method(),
      from: request.serviceWorker() ? "service-worker" : "page",
      resourceType: request.resourceType(),
    };
    requests.push(entry);
    if (BROWSER_SCHEMES.has(url.protocol)) {
      entry.verdict = "browser";
      return route.continue();
    }
    if (LOCAL_HOSTS.has(url.hostname)) {
      entry.verdict = "local";
      return route.continue();
    }
    if (isXHost(url.hostname)) {
      entry.verdict = "x-fixture";
      return route.fulfill(xFixtureResponse(url.href));
    }
    entry.verdict = "aborted";
    violations.push(entry);
    return route.abort("blockedbyclient");
  });

  return {
    requests,
    violations,
    mark: () => requests.length,
    since: mark => requests.slice(mark),
    /** Requests to the Starling service (the gateway origin). */
    toService: (origin, mark = 0) => requests.slice(mark).filter(entry => entry.verdict === "local" && entry.url.startsWith(origin)),
  };
}

/**
 * Chrome opens a socket for a page it is about to load before Playwright's interception answers
 * it from the fixture, so the proxy sees (and refuses) a CONNECT to x.com that no page ever
 * used. That is the only refusal a healthy run may contain.
 */
const SPECULATIVE = /^(?:[a-z0-9-]+\.)?(?:x|twitter)\.com(?::\d+)?$/i;

/**
 * Chrome's OWN services: the network clock, the account reconciler, the update and variations
 * endpoints. The launch flags switch most of it off and the rest is still attempted (measured on
 * the first real run of journey 1: 67 refused connections to www.google.com, accounts.google.com
 * and clients2.google.com in 17 seconds, on pages that are nothing but Starling's own website).
 * The proxy refuses every one, so none of it leaves the machine, and none of it is the product:
 * anything a PAGE or the EXTENSION asks for goes through the route guard above, which knows who
 * asked and allows no such host. So these are counted and reported, and do not fail a journey.
 */
const CHROME_ITSELF = /^(?:[a-z0-9-]+\.)*(?:google\.com|googleapis\.com|gstatic\.com|gvt1\.com|gvt2\.com)(?::\d+)?$/i;

/**
 * Nothing left the machine, and nothing TRIED to except Chrome's speculative connections.
 *
 * Two independent records have to agree. The route guard sees every request a page or the
 * extension's service worker made, with who made it; the forward proxy sees every connection
 * the browser opened at all, including the ones no page event would report. A journey that
 * passed while the extension quietly talked to a third party would be worthless, so this runs
 * at the end of every one of them.
 */
export function assertNoEscape(guard, proxy, expect) {
  expect(guard.violations.map(entry => `${entry.from} ${entry.method} ${entry.url}`),
    "the page and the extension asked for nothing outside 127.0.0.1 and the X fixture").toEqual([]);
  const refused = (proxy?.violations ?? []).filter(entry => !SPECULATIVE.test(entry.host));
  const escaped = refused.filter(entry => !CHROME_ITSELF.test(entry.host));
  expect(escaped.map(entry => `${entry.method} ${entry.url}`),
    "the browser tried to reach nothing but 127.0.0.1, the X fixture and Chrome's own services (all refused)").toEqual([]);
  return { refusedChromeServices: refused.length - escaped.length };
}
