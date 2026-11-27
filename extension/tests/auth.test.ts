import { afterEach, describe, expect, it, vi } from "vitest";
import {
  PENDING_SIGN_IN_TTL_MS,
  base64UrlEncode,
  buildConnectUrl,
  checkAuthRedirect,
  codeChallengeS256,
  createCodeVerifier,
  createState,
  deviceLabelFrom,
  isAuthExpired,
  normalizeDeviceAuth,
  parseTokenResponse,
  redirectUriFor,
  reduceAuth,
  runSignIn,
  signOutWithRevoke,
  type AuthState,
  type DeviceAuth,
  type PendingSignIn,
  type SignInDeps,
  type TokenRequestBody,
} from "../shared/auth";

/** A fake device token, assembled so the release scanner's token pattern never matches test source. */
const devToken = (body: string) => `stl_dev_${body}`;

// RFC 7636, Appendix B.
const RFC_BYTES = [116, 24, 223, 180, 151, 153, 224, 37, 79, 250, 96, 125, 216, 173, 187, 186, 22, 212, 37, 77, 105, 214, 191, 240, 91, 88, 5, 88, 83, 132, 141, 121];
const RFC_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk";
const RFC_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM";

const EXTENSION_ID = "abcdefghijklmnopabcdefghijklmnop";
const ORIGIN = "https://app.example.com";
const NOW = Date.parse("2026-09-14T12:00:00Z");

const auth = (token = devToken("firstTokenValue123456"), overrides: Partial<DeviceAuth> = {}): DeviceAuth => ({
  token,
  device_id: "dev_1",
  idle_expires_at: Math.floor(NOW / 1000) + 60 * 86_400,
  expires_at: Math.floor(NOW / 1000) + 365 * 86_400,
  signed_in_at: NOW,
  ...overrides,
});

afterEach(() => {
  vi.useRealTimers();
});

describe("PKCE", () => {
  it("matches the RFC 7636 Appendix B vector", async () => {
    expect(createCodeVerifier(() => Uint8Array.from(RFC_BYTES))).toBe(RFC_VERIFIER);
    expect(await codeChallengeS256(RFC_VERIFIER, crypto.subtle)).toBe(RFC_CHALLENGE);
  });

  it("makes 43-character base64url verifiers and states from 32 random bytes", () => {
    const random = (length: number) => crypto.getRandomValues(new Uint8Array(length));
    const verifier = createCodeVerifier(random);
    const state = createState(random);
    expect(verifier).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(state).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(verifier).not.toBe(createCodeVerifier(random));
    expect(base64UrlEncode(Uint8Array.from([251, 255]))).toBe("-_8");
  });

  it("builds the connect URL with every parameter the gateway checks", () => {
    const url = new URL(buildConnectUrl(ORIGIN, {
      clientId: EXTENSION_ID,
      redirectUri: redirectUriFor(EXTENSION_ID),
      codeChallenge: RFC_CHALLENGE,
      state: "state123",
      deviceLabel: "Chrome on Windows",
    }));
    expect(`${url.origin}${url.pathname}`).toBe(`${ORIGIN}/connect`);
    expect(Object.fromEntries(url.searchParams)).toEqual({
      client_id: EXTENSION_ID,
      redirect_uri: `https://${EXTENSION_ID}.chromiumapp.org/connect`,
      code_challenge: RFC_CHALLENGE,
      code_challenge_method: "S256",
      state: "state123",
      device_label: "Chrome on Windows",
    });
  });

  it("labels the device without identifying the person", () => {
    expect(deviceLabelFrom("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")).toBe("Chrome on Windows");
    expect(deviceLabelFrom("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/140.0.0.0 Safari/537.36")).toBe("Chrome on macOS");
    expect(deviceLabelFrom("Mozilla/5.0 (X11; CrOS x86_64 14541.0.0) Chrome/140.0.0.0")).toBe("Chrome on ChromeOS");
    expect(deviceLabelFrom("Mozilla/5.0 (Windows NT 10.0) Chrome/140.0.0.0 Edg/140.0.0.0")).toBe("Edge on Windows");
    expect(deviceLabelFrom("")).toBe("Chrome");
  });
});

describe("the redirect check", () => {
  const pending: PendingSignIn = { verifier: RFC_VERIFIER, state: "expected-state", redirectUri: redirectUriFor(EXTENSION_ID), createdAt: NOW };
  const redirect = (query: string) => `${pending.redirectUri}?${query}`;

  it("accepts the matching state and returns the code", () => {
    expect(checkAuthRedirect(redirect("code=abc&state=expected-state"), pending, NOW + 5_000)).toEqual({ ok: true, code: "abc" });
  });

  it("rejects a state mismatch, including on an error redirect", () => {
    expect(checkAuthRedirect(redirect("code=abc&state=forged"), pending, NOW)).toEqual({ ok: false, reason: "state_mismatch" });
    expect(checkAuthRedirect(redirect("code=abc"), pending, NOW)).toEqual({ ok: false, reason: "state_mismatch" });
    expect(checkAuthRedirect(redirect("error=access_denied&state=forged"), pending, NOW)).toEqual({ ok: false, reason: "state_mismatch" });
  });

  it("rejects the wrong redirect target, a denial, and a missing code", () => {
    expect(checkAuthRedirect("https://ponmlkjihgfedcbaponmlkjihgfedcba.chromiumapp.org/connect?code=abc&state=expected-state", pending, NOW))
      .toEqual({ ok: false, reason: "wrong_redirect" });
    expect(checkAuthRedirect(`https://${EXTENSION_ID}.chromiumapp.org/other?code=abc&state=expected-state`, pending, NOW))
      .toEqual({ ok: false, reason: "wrong_redirect" });
    expect(checkAuthRedirect(redirect("error=access_denied&state=expected-state"), pending, NOW)).toEqual({ ok: false, reason: "denied" });
    expect(checkAuthRedirect(redirect("state=expected-state"), pending, NOW)).toEqual({ ok: false, reason: "missing_code" });
    expect(checkAuthRedirect(undefined, pending, NOW)).toEqual({ ok: false, reason: "cancelled" });
  });

  it("expires a pending sign-in", () => {
    expect(checkAuthRedirect(redirect("code=abc&state=expected-state"), pending, NOW + PENDING_SIGN_IN_TTL_MS + 1))
      .toEqual({ ok: false, reason: "expired" });
  });

  it("is single use: without a pending record there is nothing to redeem", () => {
    expect(checkAuthRedirect(redirect("code=abc&state=expected-state"), null, NOW)).toEqual({ ok: false, reason: "no_pending" });
  });
});

describe("token lifecycle", () => {
  const signedIn: AuthState = { auth: auth(), signedOutReason: "never" };

  it("validates the token response", () => {
    const data = { token: devToken("abcdefghijklmnop1234"), device_id: "dev_9", idle_expires_at: 1_800_000_000, expires_at: 1_900_000_000 };
    expect(parseTokenResponse(data, NOW)).toEqual({ ...data, signed_in_at: NOW });
    expect(parseTokenResponse({ ...data, token: ["sk", "not-a-device-token-123456789"].join("-") }, NOW)).toBeNull();
    expect(parseTokenResponse({ ...data, device_id: "" }, NOW)).toBeNull();
    expect(parseTokenResponse({ ...data, expires_at: "later" }, NOW)).toBeNull();
    expect(normalizeDeviceAuth({ ...data, signed_in_at: 5 })?.signed_in_at).toBe(5);
    expect(normalizeDeviceAuth(devToken("abcdefghijklmnop1234"))).toBeNull();
  });

  it("signs in, and a 401 for the current token signs out as revoked", () => {
    const fresh = reduceAuth({ auth: null, signedOutReason: "user" }, { type: "token_received", auth: auth() });
    expect(fresh).toEqual(signedIn);
    expect(reduceAuth(fresh, { type: "unauthorized", tokenUsed: auth().token })).toEqual({ auth: null, signedOutReason: "revoked" });
  });

  it("a stale 401 for an older token does not clear the new token", () => {
    const next = reduceAuth(signedIn, { type: "token_received", auth: auth(devToken("secondTokenValue654321")) });
    expect(reduceAuth(next, { type: "unauthorized", tokenUsed: devToken("firstTokenValue123456") })).toBe(next);
    expect(reduceAuth(next, { type: "unauthorized", tokenUsed: null })).toBe(next);
    expect(reduceAuth({ auth: null, signedOutReason: "user" }, { type: "unauthorized", tokenUsed: "x" }).signedOutReason).toBe("user");
  });

  it("expires at the absolute expiry only", () => {
    const expiring: AuthState = { auth: auth(undefined, { expires_at: Math.floor(NOW / 1000) + 60, idle_expires_at: Math.floor(NOW / 1000) - 10 }), signedOutReason: "never" };
    // A passed idle hint alone does not sign out: the idle window slides on the server.
    expect(reduceAuth(expiring, { type: "tick", now: NOW })).toBe(expiring);
    expect(isAuthExpired(expiring.auth!, NOW + 61_000)).toBe(true);
    expect(reduceAuth(expiring, { type: "tick", now: NOW + 61_000 })).toEqual({ auth: null, signedOutReason: "expired" });
  });

  it("an explicit sign-out records why", () => {
    expect(reduceAuth(signedIn, { type: "signed_out" })).toEqual({ auth: null, signedOutReason: "user" });
  });
});

function memoryDeps(overrides: Partial<SignInDeps> = {}) {
  const store: { pending: PendingSignIn | null; auth: DeviceAuth | null } = { pending: null, auth: null };
  const exchanges: TokenRequestBody[] = [];
  let clock = NOW;
  const deps: SignInDeps = {
    origin: ORIGIN,
    extensionId: EXTENSION_ID,
    deviceLabel: "Chrome on Windows",
    extensionVersion: "1.0.0",
    randomBytes: length => crypto.getRandomValues(new Uint8Array(length)),
    subtle: crypto.subtle,
    now: () => clock,
    savePending: async pending => {
      store.pending = pending;
    },
    takePending: async () => {
      const pending = store.pending;
      store.pending = null;
      return pending;
    },
    launch: async connectUrl => {
      const state = new URL(connectUrl).searchParams.get("state");
      return `${redirectUriFor(EXTENSION_ID)}?code=one-time-code&state=${state}`;
    },
    exchange: async body => {
      exchanges.push(body);
      return { ok: true, data: { token: devToken("issuedTokenValue000000"), device_id: "dev_7", idle_expires_at: 1_800_000_000, expires_at: 1_900_000_000 } };
    },
    saveAuth: async value => {
      store.auth = value;
    },
    ...overrides,
  };
  return { deps, store, exchanges, advance: (ms: number) => { clock += ms; } };
}

describe("sign-in orchestration", () => {
  it("exchanges the code with the stored verifier and saves the device token", async () => {
    const { deps, store, exchanges } = memoryDeps();
    const result = await runSignIn(deps);
    expect(result).toMatchObject({ ok: true, auth: { device_id: "dev_7" } });
    expect(store.auth?.token).toBe(devToken("issuedTokenValue000000"));
    expect(exchanges).toHaveLength(1);
    const [body] = exchanges;
    expect(body).toMatchObject({
      grant_type: "authorization_code",
      code: "one-time-code",
      redirect_uri: redirectUriFor(EXTENSION_ID),
      client_id: EXTENSION_ID,
      device_label: "Chrome on Windows",
      extension_version: "1.0.0",
    });
    expect(body.code_verifier).toMatch(/^[A-Za-z0-9_-]{43}$/);
    // Single use: the pending record is gone, so the same redirect cannot be replayed.
    expect(store.pending).toBeNull();
    expect(checkAuthRedirect(`${redirectUriFor(EXTENSION_ID)}?code=one-time-code&state=x`, await deps.takePending(), NOW))
      .toEqual({ ok: false, reason: "no_pending" });
  });

  it("stops on a state mismatch without exchanging anything", async () => {
    const { deps, store, exchanges } = memoryDeps({
      launch: async () => `${redirectUriFor(EXTENSION_ID)}?code=stolen&state=attacker`,
    });
    expect(await runSignIn(deps)).toEqual({ ok: false, reason: "state_mismatch" });
    expect(exchanges).toHaveLength(0);
    expect(store.auth).toBeNull();
    expect(store.pending).toBeNull();
  });

  it("refuses a redirect that arrives after the pending sign-in expired", async () => {
    const holder: { advance?: (ms: number) => void } = {};
    const { deps, exchanges, advance } = memoryDeps({
      launch: async connectUrl => {
        holder.advance?.(PENDING_SIGN_IN_TTL_MS + 1_000);
        return `${redirectUriFor(EXTENSION_ID)}?code=late&state=${new URL(connectUrl).searchParams.get("state")}`;
      },
    });
    holder.advance = advance;
    expect(await runSignIn(deps)).toEqual({ ok: false, reason: "expired" });
    expect(exchanges).toHaveLength(0);
  });

  it("treats a closed window as cancelled and clears the pending record", async () => {
    const { deps, store } = memoryDeps({ launch: async () => { throw new Error("The user did not approve access."); } });
    expect(await runSignIn(deps)).toEqual({ ok: false, reason: "cancelled" });
    expect(store.pending).toBeNull();
  });

  it("reports a failed exchange and never stores a token", async () => {
    const error = { code: "bad_request" as const, message: "code already used", retryable: false, tokenUsed: null, responseReceived: true };
    const { deps, store } = memoryDeps({ exchange: async () => ({ ok: false, error }) });
    expect(await runSignIn(deps)).toEqual({ ok: false, reason: "exchange_failed", error });
    expect(store.auth).toBeNull();
    const shapeless = memoryDeps({ exchange: async () => ({ ok: true, data: { token: "nope" } }) });
    expect(await runSignIn(shapeless.deps)).toEqual({ ok: false, reason: "invalid_token" });
  });
});

describe("sign-out", () => {
  it("revokes, then clears", async () => {
    const order: string[] = [];
    const result = await signOutWithRevoke(async () => { order.push("revoke"); return true; }, async () => { order.push("clear"); });
    expect(result).toEqual({ revoked: true });
    expect(order).toEqual(["revoke", "clear"]);
  });

  it("clears after 5 seconds even when the revoke hangs, and aborts it", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    let signal: AbortSignal | undefined;
    const cleared = vi.fn(async () => undefined);
    const pending = signOutWithRevoke(value => {
      signal = value;
      return new Promise<boolean>(() => undefined);
    }, cleared);
    await vi.advanceTimersByTimeAsync(4_999);
    expect(cleared).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(2);
    expect(await pending).toEqual({ revoked: false });
    expect(cleared).toHaveBeenCalledTimes(1);
    expect(signal?.aborted).toBe(true);
  });

  it("clears when the revoke fails", async () => {
    const cleared = vi.fn(async () => undefined);
    expect(await signOutWithRevoke(async () => { throw new TypeError("Failed to fetch"); }, cleared)).toEqual({ revoked: false });
    expect(cleared).toHaveBeenCalledTimes(1);
  });
});
