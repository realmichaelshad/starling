import { afterEach, describe, expect, it, vi } from "vitest";
import {
  BACKOFF_MS,
  apiRequest,
  backoffDelay,
  codeForStatus,
  errorFromResponse,
  parseRetryAfter,
  pollReplay,
  replayBody,
  streamAssist,
  type TransportDeps,
} from "../shared/transport";

const NOW = Date.parse("2026-09-14T12:00:00Z");
const KEY = "11111111-2222-4333-8444-555555555555";
// A fake device token, assembled so the release scanner's token pattern never matches test source.
const TOKEN = `stl_dev_${"currentTokenValue1234"}`;
const API = "https://app.example.com/api/v1";

interface Call {
  url: string;
  init: RequestInit;
}

type Responder = (url: string, init: RequestInit, index: number) => Response | Promise<Response>;

function makeDeps(responder: Responder, overrides: Partial<TransportDeps> = {}) {
  const calls: Call[] = [];
  const sleeps: number[] = [];
  let uuids = 0;
  const deps: TransportDeps = {
    fetch: async (url, init) => {
      calls.push({ url, init });
      return responder(url, init, calls.length - 1);
    },
    apiBase: API,
    extensionVersion: "1.0.0",
    timeZone: () => "Europe/Berlin",
    isOnline: () => true,
    canUseNetwork: () => true,
    randomUUID: () => `00000000-0000-4000-8000-${String(++uuids).padStart(12, "0")}`,
    sleep: async ms => {
      sleeps.push(ms);
    },
    random: () => 0.5,
    now: () => NOW,
    ...overrides,
  };
  return { deps, calls, sleeps };
}

const json = (status: number, body?: unknown, headers: Record<string, string> = {}) =>
  new Response(body === undefined ? "" : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", "X-Request-Id": "req_123", ...headers },
  });

const errorBody = (code: string, extra: Record<string, unknown> = {}) => ({ error: { code, message: `server: ${code}`, ...extra } });

const header = (init: RequestInit, name: string) => new Headers(init.headers).get(name);

const networkFailure = () => Promise.reject(new TypeError("Failed to fetch"));

/** A fetch that only settles when its signal aborts, like a real hung request. */
const hang = (_url: string, init: RequestInit) => new Promise<Response>((_, reject) => {
  init.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
});

function liveBody() {
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const body = new ReadableStream<Uint8Array>({
    start(value) {
      controller = value;
    },
  });
  const encoder = new TextEncoder();
  return {
    body,
    send(event: unknown) {
      try {
        controller.enqueue(encoder.encode(`${JSON.stringify(event)}\n`));
      } catch {
        // The transport already cancelled the body (timeout or abort).
      }
    },
    close() {
      try {
        controller.close();
      } catch {
        // Already cancelled by the transport.
      }
    },
  };
}

const ndjson = (body: ReadableStream<Uint8Array>) => new Response(body, {
  status: 200,
  headers: { "Content-Type": "application/x-ndjson", "X-Request-Id": "req_stream" },
});

const chatBody = { mode: "chat", message: "Draft something" };

afterEach(() => {
  vi.useRealTimers();
});

describe("status to error code (CONTRACTS 6.3)", () => {
  it("maps every documented status and code", () => {
    const table: Array<[number, unknown, string]> = [
      [400, errorBody("bad_request"), "bad_request"],
      [422, errorBody("bad_request"), "bad_request"],
      [422, undefined, "bad_request"],
      [401, errorBody("signed_out"), "signed_out"],
      [402, errorBody("limit_reached"), "limit_reached"],
      [402, errorBody("payment_required"), "payment_required"],
      [402, errorBody("fair_use_exceeded"), "fair_use_exceeded"],
      [403, errorBody("email_unverified"), "email_unverified"],
      [403, errorBody("consent_required"), "consent_required"],
      [403, errorBody("account_suspended"), "account_suspended"],
      [404, errorBody("not_found"), "not_found"],
      [409, errorBody("busy"), "busy"],
      [409, errorBody("duplicate_request"), "duplicate_request"],
      [409, errorBody("idempotency_mismatch"), "idempotency_mismatch"],
      [413, errorBody("payload_too_large"), "payload_too_large"],
      [426, errorBody("extension_outdated"), "extension_outdated"],
      [429, errorBody("rate_limited"), "rate_limited"],
      [500, errorBody("server_error"), "server_error"],
      [503, errorBody("ai_unavailable"), "ai_unavailable"],
      [503, errorBody("service_paused"), "service_paused"],
      // A code documented for another status never leaks through.
      [402, errorBody("signed_out"), "limit_reached"],
      [403, errorBody("limit_reached"), "account_suspended"],
      [409, undefined, "busy"],
      // A proxy error with no JSON body.
      [502, undefined, "server_error"],
      [503, undefined, "server_error"],
      [504, undefined, "server_error"],
      [418, undefined, "bad_request"],
    ];
    for (const [status, body, code] of table) {
      const error = errorFromResponse(status, body, new Headers(), NOW, TOKEN);
      expect(error.code, `${status} ${JSON.stringify(body)}`).toBe(code);
      expect(error.status).toBe(status);
      expect(error.responseReceived).toBe(true);
      expect(error.tokenUsed).toBe(TOKEN);
    }
    expect(codeForStatus(302, undefined)).toBe("protocol_error");
  });

  it("reads Retry-After as seconds, as an HTTP date, or from the body", () => {
    expect(parseRetryAfter("30", NOW)).toBe(30);
    expect(parseRetryAfter(new Date(NOW + 90_000).toUTCString(), NOW)).toBe(90);
    expect(parseRetryAfter(new Date(NOW - 5_000).toUTCString(), NOW)).toBe(0);
    expect(parseRetryAfter("soon", NOW)).toBeUndefined();
    expect(parseRetryAfter(null, NOW)).toBeUndefined();

    const fromHeader = errorFromResponse(429, errorBody("rate_limited"), new Headers({ "Retry-After": "17" }), NOW, TOKEN);
    expect(fromHeader.retryAfterS).toBe(17);
    const fromDate = errorFromResponse(503, errorBody("service_paused"), new Headers({ "Retry-After": new Date(NOW + 120_000).toUTCString() }), NOW, TOKEN);
    expect(fromDate.retryAfterS).toBe(120);
    const bodyWins = errorFromResponse(429, errorBody("rate_limited", { retry_after_s: 42 }), new Headers({ "Retry-After": "17" }), NOW, TOKEN);
    expect(bodyWins.retryAfterS).toBe(42);
  });

  it("keeps usage and a same-origin upgrade path, and drops anything else", () => {
    const usage = { unit: "draft", unit_label: "drafts", allowance: 250, used: 250, reserved: 0, remaining: 0, resets_at: 1760400000, near_limit: false, fair_use_blocked: false };
    const limited = errorFromResponse(402, errorBody("limit_reached", { usage, upgrade_path: "/pricing?from=extension&reason=limit" }), new Headers({ "X-Request-Id": "req_9" }), NOW, TOKEN);
    expect(limited.usage?.allowance).toBe(250);
    expect(limited.upgradePath).toBe("/pricing?from=extension&reason=limit");
    expect(limited.requestId).toBe("req_9");
    for (const bad of ["https://evil.example/pricing", "//evil.example/pricing", "/\\evil.example", "javascript:alert(1)"]) {
      expect(errorFromResponse(402, errorBody("limit_reached", { upgrade_path: bad }), new Headers(), NOW, TOKEN).upgradePath).toBeUndefined();
    }
  });
});

describe("request headers", () => {
  it("sends the device token, extension version and time zone; a key only on POST and DELETE", async () => {
    const { deps, calls } = makeDeps(() => json(200, { ok: true }));
    await apiRequest(deps, { method: "GET", path: "/session", token: TOKEN, timeoutMs: 15_000 });
    await apiRequest(deps, { method: "POST", path: "/outcomes", body: { generation_id: "g" }, token: TOKEN, timeoutMs: 15_000 });
    await apiRequest(deps, { method: "DELETE", path: "/own-material/abc", token: TOKEN, timeoutMs: 15_000 });

    const [get, post, del] = calls;
    expect(get.url).toBe(`${API}/session`);
    expect(header(get.init, "Authorization")).toBe(`Bearer ${TOKEN}`);
    expect(header(get.init, "X-Starling-Extension")).toBe("1.0.0");
    expect(header(get.init, "X-Starling-Time-Zone")).toBe("Europe/Berlin");
    expect(header(get.init, "Idempotency-Key")).toBeNull();
    expect(get.init.credentials).toBe("omit");

    expect(header(post.init, "Idempotency-Key")).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    expect(header(post.init, "Content-Type")).toBe("application/json");
    expect(post.init.body).toBe(JSON.stringify({ generation_id: "g" }));
    expect(header(del.init, "Idempotency-Key")).not.toBeNull();
  });

  it("falls back to UTC for an unusable time zone and omits Authorization without a token", async () => {
    const { deps, calls } = makeDeps(() => json(200, {}), { timeZone: () => "Not a zone; drop table" });
    await apiRequest(deps, { method: "POST", path: "/devices/token", body: {}, timeoutMs: 15_000 });
    expect(header(calls[0].init, "X-Starling-Time-Zone")).toBe("UTC");
    expect(header(calls[0].init, "Authorization")).toBeNull();
  });
});

describe("gates before any network call", () => {
  it("sends nothing at all before consent", async () => {
    const { deps, calls } = makeDeps(() => json(200, {}), { canUseNetwork: () => false });
    const request = await apiRequest(deps, { method: "GET", path: "/session", token: TOKEN, timeoutMs: 15_000, retry: "session" });
    const stream = await streamAssist(deps, { body: chatBody, token: TOKEN, idempotencyKey: KEY });
    expect(request).toMatchObject({ ok: false, error: { code: "consent_required" } });
    expect(stream).toMatchObject({ ok: false, error: { code: "consent_required" } });
    expect(calls).toHaveLength(0);
  });

  it("reports offline immediately when the browser says so", async () => {
    const { deps, calls } = makeDeps(() => json(200, {}), { isOnline: () => false });
    expect(await apiRequest(deps, { method: "GET", path: "/session", token: TOKEN, timeoutMs: 15_000, retry: "session" }))
      .toMatchObject({ ok: false, error: { code: "offline", retryable: true } });
    expect(await streamAssist(deps, { body: chatBody, token: TOKEN, idempotencyKey: KEY }))
      .toMatchObject({ ok: false, error: { code: "offline" } });
    expect(calls).toHaveLength(0);
  });

  it("tells a failed fetch apart: offline when the browser went offline, network otherwise", async () => {
    let online = true;
    const { deps } = makeDeps(() => {
      online = false;
      return networkFailure();
    }, { isOnline: () => online });
    expect(await apiRequest(deps, { method: "POST", path: "/outcomes", body: {}, timeoutMs: 15_000 }))
      .toMatchObject({ ok: false, error: { code: "offline" } });
    const { deps: onlineDeps } = makeDeps(networkFailure);
    expect(await apiRequest(onlineDeps, { method: "POST", path: "/outcomes", body: {}, timeoutMs: 15_000 }))
      .toMatchObject({ ok: false, error: { code: "network", responseReceived: false } });
  });

  it("refuses to run without a compiled-in origin", async () => {
    const { deps, calls } = makeDeps(() => json(200, {}), { apiBase: "/api/v1" });
    expect(await apiRequest(deps, { method: "GET", path: "/session", timeoutMs: 15_000 }))
      .toMatchObject({ ok: false, error: { code: "not_configured" } });
    expect(calls).toHaveLength(0);
  });
});

describe("timeouts and cancellation", () => {
  it("times out a one-shot request", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const { deps, calls } = makeDeps(hang);
    const pending = apiRequest(deps, { method: "POST", path: "/assist", body: {}, token: TOKEN, timeoutMs: 60_000, retry: "assist" });
    await vi.advanceTimersByTimeAsync(60_001);
    expect(await pending).toMatchObject({ ok: false, error: { code: "timeout" } });
    // A timeout is never retried: the server may still be working on it.
    expect(calls).toHaveLength(1);
  });

  it("reports a user abort as cancelled", async () => {
    const controller = new AbortController();
    const { deps } = makeDeps((url, init) => {
      queueMicrotask(() => controller.abort());
      return hang(url, init);
    });
    expect(await apiRequest(deps, { method: "GET", path: "/own-material", token: TOKEN, timeoutMs: 15_000, retry: "session", signal: controller.signal }))
      .toMatchObject({ ok: false, error: { code: "cancelled" } });
  });
});

describe("retry rules", () => {
  it("retries GET /session twice on network failure, reusing nothing it should not", async () => {
    const { deps, calls, sleeps } = makeDeps((_url, _init, index) => index < 2 ? networkFailure() : json(200, { ok: 1 }));
    const result = await apiRequest(deps, { method: "GET", path: "/session", token: TOKEN, timeoutMs: 15_000, retry: "session" });
    expect(result.ok).toBe(true);
    expect(calls).toHaveLength(3);
    expect(sleeps).toEqual([...BACKOFF_MS]);
  });

  it("gives up after two session retries", async () => {
    const { deps, calls } = makeDeps(networkFailure);
    expect(await apiRequest(deps, { method: "GET", path: "/session", token: TOKEN, timeoutMs: 15_000, retry: "session" }))
      .toMatchObject({ ok: false, error: { code: "network" } });
    expect(calls).toHaveLength(3);
  });

  it("retries 502 and 504, but never a response the server meant", async () => {
    const { deps, calls } = makeDeps((_url, _init, index) => index === 0 ? json(502) : index === 1 ? json(504) : json(200, {}));
    expect((await apiRequest(deps, { method: "GET", path: "/session", token: TOKEN, timeoutMs: 15_000, retry: "session" })).ok).toBe(true);
    expect(calls).toHaveLength(3);

    const final: Array<[number, unknown]> = [
      [401, errorBody("signed_out")], [402, errorBody("limit_reached")], [403, errorBody("consent_required")],
      [409, errorBody("busy")], [422, errorBody("bad_request")], [429, errorBody("rate_limited")],
      [500, errorBody("server_error")], [503, errorBody("ai_unavailable")], [503, errorBody("service_paused")],
    ];
    for (const [status, body] of final) {
      for (const retry of ["session", "capture", "assist"] as const) {
        const probe = makeDeps(() => json(status, body));
        const outcome = await apiRequest(probe.deps, { method: retry === "session" ? "GET" : "POST", path: "/x", body: retry === "session" ? undefined : {}, token: TOKEN, timeoutMs: 15_000, retry });
        expect(outcome.ok).toBe(false);
        expect(probe.calls, `${status} ${retry}`).toHaveLength(1);
      }
    }
  });

  it("retries a capture POST twice with the same Idempotency-Key", async () => {
    const { deps, calls } = makeDeps((_url, _init, index) => index < 2 ? networkFailure() : json(200, {}));
    expect((await apiRequest(deps, { method: "POST", path: "/snapshots/metrics", body: { post_id: "1" }, token: TOKEN, timeoutMs: 15_000, retry: "capture" })).ok).toBe(true);
    expect(calls).toHaveLength(3);
    const keys = new Set(calls.map(call => header(call.init, "Idempotency-Key")));
    expect(keys.size).toBe(1);
  });

  it("retries one-shot assist once, only when no response arrived, with the same key", async () => {
    const { deps, calls } = makeDeps(networkFailure);
    const outcome = await apiRequest(deps, { method: "POST", path: "/assist", body: { mode: "context" }, token: TOKEN, idempotencyKey: KEY, timeoutMs: 60_000, retry: "assist" });
    expect(outcome.ok).toBe(false);
    expect(calls).toHaveLength(2);
    expect(calls.map(call => header(call.init, "Idempotency-Key"))).toEqual([KEY, KEY]);

    const answered = makeDeps(() => json(502));
    await apiRequest(answered.deps, { method: "POST", path: "/assist", body: {}, token: TOKEN, timeoutMs: 60_000, retry: "assist" });
    expect(answered.calls).toHaveLength(1);
  });

  it("jitters backoff by at most 20%", () => {
    expect(backoffDelay(0, () => 0)).toBe(400);
    expect(backoffDelay(0, () => 1)).toBe(600);
    expect(backoffDelay(1, () => 0.5)).toBe(2_000);
    expect(backoffDelay(5, () => 1)).toBe(2_400);
  });
});

describe("the assist stream", () => {
  it("delivers events in order and returns the final response", async () => {
    const stream = liveBody();
    const { deps, calls } = makeDeps(() => ndjson(stream.body));
    const seen: string[] = [];
    for (const event of [
      { event: "status", stage: "accepted", request_id: KEY },
      { event: "queued", position: 1 },
      { event: "status", stage: "thinking" },
      { event: "token", text: "Hel" },
      { event: "token", text: "lo" },
      { event: "heartbeat" },
      { event: "final", response: { ok: true, answer: "Hello" } },
      { event: "usage", usage: { unit: "draft", unit_label: "drafts", allowance: 250, used: 13, reserved: 0, remaining: 237, resets_at: null, near_limit: false, fair_use_blocked: false } },
      { event: "status", stage: "accepted" },
    ]) stream.send(event);
    stream.close();

    const outcome = await streamAssist(deps, { body: chatBody, token: TOKEN, idempotencyKey: KEY }, {
      onAccepted: ref => seen.push(`accepted:${ref}`),
      onQueued: position => seen.push(`queued:${position}`),
      onStatus: stage => seen.push(`status:${stage}`),
      onToken: text => seen.push(`token:${text}`),
      onHeartbeat: () => seen.push("heartbeat"),
      onFinal: response => seen.push(`final:${response.answer}`),
      onUsage: usage => seen.push(`usage:${usage.remaining}`),
    });

    expect(seen).toEqual([
      `accepted:${KEY}`, "queued:1", "status:thinking", "token:Hel", "token:lo", "heartbeat", "final:Hello", "usage:237",
    ]);
    expect(outcome).toMatchObject({ ok: true, exposed: true, requestId: "req_stream", response: { ok: true, answer: "Hello" } });
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe(`${API}/assist/stream`);
    expect(header(calls[0].init, "Idempotency-Key")).toBe(KEY);
    expect(header(calls[0].init, "Accept")).toBe("application/x-ndjson");
  });

  it("a heartbeat resets the inactivity timer", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const stream = liveBody();
    const { deps } = makeDeps(() => ndjson(stream.body));
    const pending = streamAssist(deps, { body: chatBody, token: TOKEN, idempotencyKey: KEY, inactivityMs: 45_000 });
    stream.send({ event: "status", stage: "accepted" });
    for (let beat = 0; beat < 3; beat += 1) {
      await vi.advanceTimersByTimeAsync(40_000);
      stream.send({ event: "heartbeat" });
    }
    await vi.advanceTimersByTimeAsync(40_000);
    stream.send({ event: "final", response: { ok: true, answer: "late but alive" } });
    stream.close();
    expect(await pending).toMatchObject({ ok: true, response: { answer: "late but alive" } });
  });

  it("a silent stream times out and cancels with the same key", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const stream = liveBody();
    const { deps, calls } = makeDeps(url => url.endsWith("/assist/stream") ? ndjson(stream.body) : json(200, {}));
    const pending = streamAssist(deps, { body: chatBody, token: TOKEN, idempotencyKey: KEY, inactivityMs: 45_000 });
    stream.send({ event: "status", stage: "accepted" });
    await vi.advanceTimersByTimeAsync(45_001);
    expect(await pending).toMatchObject({ ok: false, error: { code: "timeout" }, headersReceived: true });
    const cancel = calls.find(call => call.url.endsWith("/cancel"));
    expect(cancel?.url).toBe(`${API}/requests/${KEY}/cancel`);
    expect(cancel?.init.keepalive).toBe(true);
  });

  it("stops at the total cap even while events keep coming", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const stream = liveBody();
    const { deps } = makeDeps(url => url.endsWith("/assist/stream") ? ndjson(stream.body) : json(200, {}));
    const pending = streamAssist(deps, { body: chatBody, token: TOKEN, idempotencyKey: KEY });
    for (let second = 0; second < 250; second += 20) {
      stream.send({ event: "heartbeat" });
      await vi.advanceTimersByTimeAsync(20_000);
    }
    expect(await pending).toMatchObject({ ok: false, error: { code: "timeout" } });
  });

  it("a user abort mid-stream stops reading and cancels the same key", async () => {
    const stream = liveBody();
    const controller = new AbortController();
    const { deps, calls } = makeDeps(url => url.endsWith("/assist/stream") ? ndjson(stream.body) : json(200, {}));
    const pending = streamAssist(deps, { body: chatBody, token: TOKEN, idempotencyKey: KEY, signal: controller.signal }, {
      onToken: () => controller.abort(),
    });
    stream.send({ event: "status", stage: "accepted" });
    stream.send({ event: "token", text: "partial" });
    const outcome = await pending;
    expect(outcome).toMatchObject({ ok: false, exposed: true, error: { code: "cancelled" } });
    const cancel = calls.find(call => call.url.endsWith("/cancel"));
    expect(cancel?.url).toBe(`${API}/requests/${KEY}/cancel`);
    expect(header(cancel!.init, "Authorization")).toBe(`Bearer ${TOKEN}`);
  });

  it("a stream that ends without final is a retryable interruption", async () => {
    const stream = liveBody();
    const { deps } = makeDeps(() => ndjson(stream.body));
    stream.send({ event: "status", stage: "accepted" });
    stream.send({ event: "token", text: "half a dra" });
    stream.close();
    const outcome = await streamAssist(deps, { body: chatBody, token: TOKEN, idempotencyKey: KEY });
    expect(outcome).toMatchObject({
      ok: false, exposed: true, headersReceived: true,
      error: {
        code: "interrupted", retryable: true, requestId: "req_stream",
        message: "The connection to Starling ended before the draft arrived. Nothing in X changed.",
      },
    });
  });

  it("a connection that drops mid-stream is an interruption, not a plain network error", async () => {
    let source!: ReadableStreamDefaultController<Uint8Array>;
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        source = controller;
      },
    });
    const { deps } = makeDeps(() => ndjson(body));
    const pending = streamAssist(deps, { body: chatBody, token: TOKEN, idempotencyKey: KEY });
    source.enqueue(new TextEncoder().encode('{"event":"token","text":"half a dra"}\n'));
    await new Promise(resolve => setTimeout(resolve, 20));
    source.error(new TypeError("network changed"));
    expect(await pending).toMatchObject({
      ok: false, exposed: true, headersReceived: true,
      error: { code: "interrupted", retryable: true, requestId: "req_stream" },
    });
  });

  it("passes a final with ok:false through untouched, code and all", async () => {
    const stream = liveBody();
    const { deps } = makeDeps(() => ndjson(stream.body));
    const response = { ok: false, error: "No writer is free right now.", code: "ai_unavailable" };
    stream.send({ event: "final", response });
    stream.close();
    const outcome = await streamAssist(deps, { body: chatBody, token: TOKEN, idempotencyKey: KEY });
    expect(outcome).toMatchObject({ ok: true, exposed: false, response });
  });

  it("an error event is terminal and carries its code and Retry-After", async () => {
    const stream = liveBody();
    const { deps } = makeDeps(() => ndjson(stream.body));
    const tokens: string[] = [];
    stream.send({ event: "error", error: { code: "ai_unavailable", message: "Upstream is overloaded", retry_after_s: 20 } });
    stream.send({ event: "token", text: "never delivered" });
    stream.close();
    const outcome = await streamAssist(deps, { body: chatBody, token: TOKEN, idempotencyKey: KEY }, { onToken: text => tokens.push(text) });
    expect(outcome).toMatchObject({ ok: false, error: { code: "ai_unavailable", retryAfterS: 20, message: "Upstream is overloaded" } });
    expect(tokens).toEqual([]);
  });

  it("maps an HTTP error before the stream starts, without retrying", async () => {
    const { deps, calls } = makeDeps(() => json(402, errorBody("limit_reached")));
    const outcome = await streamAssist(deps, { body: chatBody, token: TOKEN, idempotencyKey: KEY });
    expect(outcome).toMatchObject({ ok: false, headersReceived: true, error: { code: "limit_reached", tokenUsed: TOKEN } });
    expect(calls).toHaveLength(1);
  });

  it("retries the stream once, only before headers, with the same key", async () => {
    const stream = liveBody();
    const { deps, calls } = makeDeps((_url, _init, index) => index === 0 ? networkFailure() : ndjson(stream.body));
    stream.send({ event: "final", response: { ok: true, answer: "second try" } });
    stream.close();
    expect(await streamAssist(deps, { body: chatBody, token: TOKEN, idempotencyKey: KEY })).toMatchObject({ ok: true });
    expect(calls.map(call => header(call.init, "Idempotency-Key"))).toEqual([KEY, KEY]);

    const down = makeDeps(networkFailure);
    expect(await streamAssist(down.deps, { body: chatBody, token: TOKEN, idempotencyKey: KEY })).toMatchObject({ ok: false, error: { code: "network" } });
    expect(down.calls).toHaveLength(2);
  });

  it("treats a malformed line or a malformed final as a protocol error", async () => {
    const encoder = new TextEncoder();
    const raw = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode('{"event":"status","stage":"accepted"}\n{not json}\n'));
        controller.close();
      },
    });
    const broken = makeDeps(() => ndjson(raw));
    expect(await streamAssist(broken.deps, { body: chatBody, token: TOKEN, idempotencyKey: KEY }))
      .toMatchObject({ ok: false, error: { code: "protocol_error" } });

    const stream = liveBody();
    const shapeless = makeDeps(() => ndjson(stream.body));
    stream.send({ event: "final", response: { answer: "no ok bit" } });
    stream.close();
    expect(await streamAssist(shapeless.deps, { body: chatBody, token: TOKEN, idempotencyKey: KEY }))
      .toMatchObject({ ok: false, error: { code: "protocol_error" } });
  });
});

describe("replay after a dropped stream (CONTRACTS 6.5)", () => {
  const usage = { unit: "draft", unit_label: "drafts", allowance: 250, used: 14, reserved: 0, remaining: 236, resets_at: null, near_limit: false, fair_use_blocked: false };
  const pendingReply = () => json(202, { request_id: KEY, state: "pending" });
  const doneReply = (answer: string) => json(200, { request_id: KEY, state: "done", response: { ok: true, answer }, usage });

  /** Deps whose sleep advances a fake clock, so the 240 s budget is exact and instant. */
  function clocked(responder: Responder, overrides: Partial<TransportDeps> = {}) {
    let clock = NOW;
    const made = makeDeps(responder, {
      now: () => clock,
      sleep: async ms => {
        made.sleeps.push(ms);
        clock += ms;
      },
      ...overrides,
    });
    return made;
  }

  it("polls every 2 s while pending, then delivers the stored response and its usage", async () => {
    const { deps, calls, sleeps } = clocked((_url, _init, index) => index < 2 ? pendingReply() : doneReply("recovered"));
    let pendingSeen = 0;
    const outcome = await pollReplay(deps, { token: TOKEN, key: KEY, startedAt: NOW, onPending: () => { pendingSeen += 1; } });
    expect(outcome).toMatchObject({ kind: "done", response: { ok: true, answer: "recovered" }, usage: { remaining: 236 }, requestId: "req_123" });
    expect(calls).toHaveLength(3);
    for (const call of calls) {
      expect(call.url).toBe(`${API}/requests/${KEY}`);
      expect(call.init.method).toBe("GET");
      expect(header(call.init, "Authorization")).toBe(`Bearer ${TOKEN}`);
      expect(header(call.init, "Idempotency-Key")).toBeNull();
    }
    expect(sleeps).toEqual([2_000, 2_000]);
    expect(pendingSeen).toBe(2);
  });

  it("finds a ✦ press's first attempt when its same-key retry is told it is a duplicate", async () => {
    // The first POST /assist got no answer at all; the transport's one retry reused the key, and the
    // server, still drafting that first attempt, answered duplicate_request. The worker then waits
    // for the first attempt by the same key, within the press's own 60 s, instead of losing it.
    const { deps, calls } = clocked((url, _init, index) => {
      if (url.endsWith("/assist")) return index === 0 ? networkFailure() : json(409, errorBody("duplicate_request"));
      return index < 3 ? pendingReply() : doneReply("the first attempt's draft");
    });
    const sent = await apiRequest(deps, { method: "POST", path: "/assist", body: { mode: "context" }, token: TOKEN, idempotencyKey: KEY, timeoutMs: 60_000, retry: "assist" });
    expect(sent).toMatchObject({ ok: false, error: { code: "duplicate_request", status: 409 } });
    const replay = await pollReplay(deps, { token: TOKEN, key: KEY, startedAt: NOW, totalMs: 60_000 });
    expect(replay).toMatchObject({ kind: "done", response: { ok: true, answer: "the first attempt's draft" }, usage: { remaining: 236 } });
    expect(calls.map(call => call.url.slice(API.length))).toEqual(["/assist", "/assist", `/requests/${KEY}`, `/requests/${KEY}`]);
    expect(calls.map(call => header(call.init, "Idempotency-Key"))).toEqual([KEY, KEY, null, null]);

    // Still running when the press's minute is up: the worker gives up (and cancels it by the key).
    const slow = clocked(() => pendingReply());
    expect(await pollReplay(slow.deps, { token: TOKEN, key: KEY, startedAt: NOW - 55_000, totalMs: 60_000 })).toEqual({ kind: "timeout" });
  });

  it("stops with not_found when the server holds no request under that key", async () => {
    const { deps, calls } = clocked((_url, _init, index) => index === 0 ? pendingReply() : json(404, errorBody("not_found")));
    expect(await pollReplay(deps, { token: TOKEN, key: KEY, startedAt: NOW })).toEqual({ kind: "not_found" });
    expect(calls).toHaveLength(2);
  });

  it("gives up 240 s after the request started, never polling past it", async () => {
    const { deps, calls, sleeps } = clocked(() => pendingReply());
    expect(await pollReplay(deps, { token: TOKEN, key: KEY, startedAt: NOW - 231_000 })).toEqual({ kind: "timeout" });
    // Polls at 231, 233, 235, 237 and 239 s; the last wait is cut to the second that is left.
    expect(calls).toHaveLength(5);
    expect(sleeps).toEqual([2_000, 2_000, 2_000, 2_000, 1_000]);

    const late = clocked(() => pendingReply());
    expect(await pollReplay(late.deps, { token: TOKEN, key: KEY, startedAt: NOW - 240_000 })).toEqual({ kind: "timeout" });
    expect(late.calls).toHaveLength(0);
  });

  it("stops at once on cancel, including in the middle of a wait", async () => {
    const early = new AbortController();
    const first = clocked(() => pendingReply());
    expect(await pollReplay(first.deps, { token: TOKEN, key: KEY, startedAt: NOW, signal: early.signal, onPending: () => early.abort() }))
      .toEqual({ kind: "cancelled" });
    expect(first.calls).toHaveLength(1);

    const midWait = new AbortController();
    const second = clocked(() => pendingReply(), { sleep: () => new Promise<void>(() => undefined) });
    const outcome = pollReplay(second.deps, {
      token: TOKEN, key: KEY, startedAt: NOW, signal: midWait.signal,
      onPending: () => queueMicrotask(() => midWait.abort()),
    });
    expect(await outcome).toEqual({ kind: "cancelled" });
    expect(second.calls).toHaveLength(1);
  });

  it("stops when the user signs out while waiting", async () => {
    let signedIn = true;
    const { deps, calls } = clocked(() => {
      signedIn = false;
      return pendingReply();
    });
    expect(await pollReplay(deps, { token: TOKEN, key: KEY, startedAt: NOW, shouldContinue: () => signedIn })).toEqual({ kind: "stopped" });
    expect(calls).toHaveLength(1);
  });

  it("waits through a blip while polling, but not through a refusal", async () => {
    const { deps, calls } = clocked((_url, _init, index) => index === 0 ? networkFailure() : index === 1 ? json(503) : doneReply("after a blip"));
    expect(await pollReplay(deps, { token: TOKEN, key: KEY, startedAt: NOW })).toMatchObject({ kind: "done", response: { answer: "after a blip" } });
    expect(calls).toHaveLength(3);

    const refused = clocked(() => json(401, errorBody("signed_out")));
    expect(await pollReplay(refused.deps, { token: TOKEN, key: KEY, startedAt: NOW }))
      .toMatchObject({ kind: "error", error: { code: "signed_out", status: 401, tokenUsed: TOKEN } });
    expect(refused.calls).toHaveLength(1);

    const withdrawn = clocked(() => pendingReply(), { canUseNetwork: () => false });
    expect(await pollReplay(withdrawn.deps, { token: TOKEN, key: KEY, startedAt: NOW }))
      .toMatchObject({ kind: "error", error: { code: "consent_required" } });
    expect(withdrawn.calls).toHaveLength(0);
  });

  it("reads the replay body strictly", () => {
    expect(replayBody({ request_id: KEY, state: "pending" })).toEqual({ state: "pending" });
    expect(replayBody({ request_id: KEY, state: "done", response: { ok: true, answer: "a" }, usage }))
      .toMatchObject({ state: "done", response: { ok: true, answer: "a" }, usage: { allowance: 250 } });
    expect(replayBody({ request_id: KEY, state: "done", response: { answer: "no ok bit" } })).toBeNull();
    expect(replayBody({ ok: true, answer: "a bare response is not the contract" })).toBeNull();
    expect(replayBody(null)).toBeNull();
  });
});
