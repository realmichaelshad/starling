/**
 * The only door out of the test browser: a forward proxy on 127.0.0.1 that every request of the
 * browser profile goes through (pages, the extension's service worker, the sign-in window and
 * Chrome's own background traffic alike).
 *
 * It does four jobs:
 *
 * 1. **Nothing reaches the internet.** Only loopback destinations are forwarded. Anything else
 *    is refused and recorded in `violations`; the journeys fail when a page or the extension
 *    caused one.
 * 2. **A request log** with one entry per request, in arrival order. It is what "the service saw
 *    nothing before consent" is asserted against: unlike page events, it cannot miss a request.
 * 3. **Offline.** `setOffline(true)` drops every connection, which is what a dead network looks
 *    like to `fetch`.
 * 4. **A stream cut.** `cutNextStream()` forwards the beginning of the next matching response
 *    and then destroys the browser's side of the connection, while still reading the gateway's
 *    side to the end. That is a connection lost in transit: the gateway never notices, finishes
 *    the draft and stores it, and the extension has to reconnect and ask for it.
 */
import http from "node:http";
import net from "node:net";

const LOOPBACK = new Set(["127.0.0.1", "localhost", "[::1]", "::1"]);
const HOP_BY_HOP = new Set(["proxy-connection", "proxy-authorization", "connection", "keep-alive", "te", "trailer", "upgrade"]);

function isLoopback(hostname) {
  return LOOPBACK.has(String(hostname).toLowerCase());
}

export async function startProxy() {
  const log = [];
  const violations = [];
  const sockets = new Set();
  const cuts = [];
  const taps = [];
  let offline = false;
  let counter = 0;

  const record = entry => {
    counter += 1;
    const full = { n: counter, at: Date.now(), ...entry };
    log.push(full);
    return full;
  };

  const server = http.createServer((req, res) => {
    let target;
    try {
      target = new URL(req.url);
    } catch {
      // Not a proxy request (no absolute URL): nothing in the browser sends these.
      res.writeHead(400).end();
      return;
    }
    const entry = record({ kind: "http", method: req.method, url: target.href, host: target.host, path: target.pathname });
    if (offline) {
      entry.outcome = "dropped-offline";
      req.socket.destroy();
      return;
    }
    if (!isLoopback(target.hostname)) {
      entry.outcome = "refused";
      violations.push(entry);
      res.writeHead(403, { "Content-Type": "text/plain" }).end("the e2e proxy forwards loopback destinations only");
      return;
    }

    const headers = {};
    for (const [name, value] of Object.entries(req.headers)) {
      if (!HOP_BY_HOP.has(name)) headers[name] = value;
    }
    const cut = cuts.find(rule => !rule.used && rule.method === req.method && rule.path.test(target.pathname));
    if (cut) {
      cut.used = true;
      entry.cut = true;
    }
    // A tap keeps the bodies of the exchanges it names (never any header, so no
    // device token is ever written down). Journeys use it to read what the
    // gateway actually answered instead of inferring it from the UI.
    const tap = taps.find(rule => rule.path.test(target.pathname));
    let capture = null;
    if (tap) {
      capture = { n: entry.n, at: entry.at, method: req.method, path: target.pathname, request: "", response: "", status: 0 };
      tap.calls.push(capture);
      req.on("data", chunk => { capture.request += chunk.toString("utf8"); });
    }
    const upstream = http.request(
      { host: target.hostname, port: target.port || 80, method: req.method, path: target.pathname + target.search, headers },
      upstreamRes => {
        entry.status = upstreamRes.statusCode;
        if (capture) {
          capture.status = upstreamRes.statusCode;
          upstreamRes.on("data", chunk => { capture.response += chunk.toString("utf8"); });
          upstreamRes.on("end", () => { capture.done = true; });
        }
        const responseHeaders = { ...upstreamRes.headers };
        delete responseHeaders.connection;
        delete responseHeaders["keep-alive"];
        res.writeHead(upstreamRes.statusCode, upstreamRes.statusMessage, responseHeaders);
        res.flushHeaders();
        if (!cut) {
          upstreamRes.pipe(res);
          upstreamRes.on("end", () => { entry.outcome = "forwarded"; });
          return;
        }
        // Forward until the trigger line has passed, then lose the browser's side only.
        let seen = "";
        let clientGone = false;
        cut.upstreamBody = "";
        upstreamRes.setEncoding("utf8");
        upstreamRes.on("data", chunk => {
          cut.upstreamBody += chunk;
          if (clientGone) return;
          res.write(chunk);
          seen += chunk;
          // Only complete lines count; the last piece may still be arriving.
          const lines = seen.split("\n").slice(0, -1).filter(line => line.trim());
          const matched = lines.filter(line => {
            try { return JSON.parse(line).event === cut.event; } catch { return false; }
          }).length;
          if (matched >= cut.count) {
            clientGone = true;
            cut.cutAt = Date.now();
            cut.forwardedLines = lines.length;
            entry.outcome = "cut";
            // Give the bytes a moment to leave, then drop the connection without a clean end.
            setTimeout(() => res.socket?.destroy(), 30);
          }
        });
        upstreamRes.on("end", () => {
          cut.upstreamEndedAt = Date.now();
          cut.done = true;
          if (!clientGone) {
            entry.outcome = "forwarded-uncut";
            res.end();
          }
          for (const waiter of cut.waiters.splice(0)) waiter();
        });
      },
    );
    upstream.on("error", error => {
      entry.outcome = `upstream-error:${error.code || error.message}`;
      if (!res.headersSent) res.writeHead(502);
      res.end();
    });
    // A cut response keeps its gateway side alive even after the browser's side is gone.
    if (!cut) res.on("close", () => { if (!res.writableFinished) upstream.destroy(); });
    req.pipe(upstream);
  });

  server.on("connect", (req, clientSocket, head) => {
    const [hostname, port] = req.url.split(/:(?=\d+$)/);
    const entry = record({ kind: "connect", method: "CONNECT", url: req.url, host: req.url, path: "" });
    clientSocket.on("error", () => undefined);
    if (offline) {
      entry.outcome = "dropped-offline";
      clientSocket.destroy();
      return;
    }
    if (!isLoopback(hostname)) {
      entry.outcome = "refused";
      violations.push(entry);
      clientSocket.end("HTTP/1.1 403 Forbidden\r\n\r\n");
      return;
    }
    const upstream = net.connect(Number(port), hostname, () => {
      entry.outcome = "tunnelled";
      clientSocket.write("HTTP/1.1 200 Connection Established\r\n\r\n");
      if (head?.length) upstream.write(head);
      upstream.pipe(clientSocket);
      clientSocket.pipe(upstream);
    });
    upstream.on("error", () => {
      entry.outcome = "upstream-error";
      clientSocket.destroy();
    });
    clientSocket.on("close", () => upstream.destroy());
  });

  server.on("connection", socket => {
    sockets.add(socket);
    socket.on("close", () => sockets.delete(socket));
  });

  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const port = server.address().port;

  return {
    port,
    url: `http://127.0.0.1:${port}`,
    log,
    violations,
    /** Entries for one origin's paths, e.g. `requestsTo("http://127.0.0.1:8765")`. */
    requestsTo(origin) {
      const host = new URL(origin).host;
      return log.filter(entry => entry.host === host);
    },
    mark() {
      return log.length;
    },
    since(mark) {
      return log.slice(mark);
    },
    /**
     * Keep the request and response bodies of every exchange whose path matches, so a journey
     * can assert on what the gateway really answered. Headers are never kept: the extension's
     * device token travels in one, and nothing may write it down.
     */
    tapBodies(path) {
      const rule = { path, calls: [] };
      taps.push(rule);
      return rule.calls;
    },
    setOffline(value) {
      offline = Boolean(value);
      if (offline) for (const socket of sockets) socket.destroy();
    },
    get offline() {
      return offline;
    },
    /**
     * Cut the next response to `method path` once `count` NDJSON lines with that `event` went through.
     * Returns a handle: `done` resolves once the gateway's side has ended, `upstreamBody` holds
     * everything the gateway sent (the part the browser never received included).
     */
    cutNextStream({ method = "POST", path, event = "token", count = 1 }) {
      const rule = { method, path, event, count, used: false, done: false, waiters: [], upstreamBody: "" };
      cuts.push(rule);
      return {
        rule,
        get used() { return rule.used; },
        get wasCut() { return Boolean(rule.cutAt); },
        get upstreamBody() { return rule.upstreamBody; },
        done: () => (rule.done ? Promise.resolve() : new Promise(resolve => rule.waiters.push(resolve))),
      };
    },
    async close() {
      for (const socket of sockets) socket.destroy();
      await new Promise(resolve => server.close(resolve));
    },
  };
}
