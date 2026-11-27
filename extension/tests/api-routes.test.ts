import { describe, expect, it } from "vitest";
import {
  MESSAGE_KINDS,
  OWN_MATERIAL_ID,
  REMOVED_MESSAGE_KINDS,
  SESSION_ROUTE,
  apiRouteFor,
  assistStreamRoute,
  cancelRoute,
  consentRoute,
  openSiteTarget,
  parseConsentAction,
  replayRoute,
  tokenExchangeRoute,
  type RoutableMessage,
} from "../shared/api-routes";
import { SITE_PAGES } from "../shared/config";

const context = { author: "Sample Author", text: "hello", links: [], media: [], visibility: "public" };

describe("message kinds map to the gateway contract (CONTRACTS 6.1)", () => {
  it("maps every kept network kind", () => {
    const cases: Array<[RoutableMessage, string, string]> = [
      [{ kind: MESSAGE_KINDS.getBridgeState }, "GET", "/session"],
      [{ kind: MESSAGE_KINDS.refreshSession }, "GET", "/session"],
      [{ kind: MESSAGE_KINDS.assist, payload: { mode: "context", context } }, "POST", "/assist"],
      [{ kind: MESSAGE_KINDS.assist, payload: { mode: "schedule", schedule: { op: "list_due" } } }, "POST", "/assist"],
      [{ kind: MESSAGE_KINDS.captureProfile, payload: { handle: "sample" } }, "POST", "/snapshots/profile"],
      [{ kind: MESSAGE_KINDS.captureMetric, payload: { post_id: "1" } }, "POST", "/snapshots/metrics"],
      [{ kind: MESSAGE_KINDS.recordOutcome, payload: { generation_id: "g", status: "inserted" } }, "POST", "/outcomes"],
      [{ kind: MESSAGE_KINDS.imageFeedback, payload: { generation_id: "g", choice: "used" } }, "POST", "/images/feedback"],
      [{ kind: MESSAGE_KINDS.confirmPreference, payload: { rule: "r", confirm: true } }, "POST", "/preferences/confirm"],
      [{ kind: MESSAGE_KINDS.ownMaterial, payload: { action: "list" } }, "GET", "/own-material"],
      [{ kind: MESSAGE_KINDS.ownMaterial, payload: { action: "add", body: { kind: "voice", text: "a sample of mine" } } }, "POST", "/own-material"],
      [{ kind: MESSAGE_KINDS.ownMaterial, payload: { action: "remove", body: { id: "abc_123-X" } } }, "DELETE", "/own-material/abc_123-X"],
      [{ kind: MESSAGE_KINDS.consent, payload: { action: "withdraw" } }, "POST", "/consent/withdraw"],
      [{ kind: MESSAGE_KINDS.signOut }, "POST", "/devices/current/revoke"],
    ];
    for (const [message, method, path] of cases) {
      const result = apiRouteFor(message);
      expect(result.ok, message.kind).toBe(true);
      if (!result.ok) continue;
      expect(result.route.method, message.kind).toBe(method);
      expect(result.route.path, message.kind).toBe(path);
      expect(result.route.auth).toBe("device");
    }
  });

  it("uses the contract timeouts and retry classes", () => {
    expect(SESSION_ROUTE).toMatchObject({ timeoutMs: 15_000, retry: "session" });
    const assist = apiRouteFor({ kind: MESSAGE_KINDS.assist, payload: { mode: "context", context } });
    expect(assist.ok && assist.route).toMatchObject({ timeoutMs: 60_000, retry: "assist" });
    const capture = apiRouteFor({ kind: MESSAGE_KINDS.captureMetric, payload: { post_id: "1" } });
    expect(capture.ok && capture.route).toMatchObject({ timeoutMs: 15_000, retry: "capture" });
    const revoke = apiRouteFor({ kind: MESSAGE_KINDS.signOut });
    expect(revoke.ok && revoke.route).toMatchObject({ timeoutMs: 5_000, retry: "none" });
    expect(tokenExchangeRoute({})).toMatchObject({ method: "POST", path: "/devices/token", auth: "none" });
    expect(consentRoute({ version: "v", passive_learning: false })).toMatchObject({ method: "POST", path: "/consent", body: { version: "v", passive_learning: false } });
  });

  it("requires passive-learning consent for capture nobody asked for", () => {
    const profile = apiRouteFor({ kind: MESSAGE_KINDS.captureProfile, payload: { handle: "sample" } });
    const passiveMetric = apiRouteFor({ kind: MESSAGE_KINDS.captureMetric, payload: { post_id: "1" } });
    const publication = apiRouteFor({ kind: MESSAGE_KINDS.captureMetric, purpose: "publication", payload: { post_id: "1" } });
    const outcome = apiRouteFor({ kind: MESSAGE_KINDS.recordOutcome, payload: { generation_id: "g" } });
    expect(profile.ok && profile.route.consent).toBe("passive");
    expect(passiveMetric.ok && passiveMetric.route.consent).toBe("passive");
    expect(publication.ok && publication.route.consent).toBe("main");
    expect(outcome.ok && outcome.route.consent).toBe("main");
  });

  it("keeps one-shot and streamed modes on their own routes", () => {
    expect(apiRouteFor({ kind: MESSAGE_KINDS.assist, payload: { mode: "chat", message: "hi" } })).toMatchObject({ ok: false, code: "bad_request" });
    expect(apiRouteFor({ kind: MESSAGE_KINDS.assist, payload: { mode: "nonsense" } })).toMatchObject({ ok: false, code: "bad_request" });
    const chat = assistStreamRoute({ mode: "chat", message: "hi" });
    expect(chat.ok && chat.route).toMatchObject({ method: "POST", path: "/assist/stream", timeoutMs: 240_000 });
    const enhance = assistStreamRoute({ mode: "enhance_composer", composer: { action: "post", draft_text: "", intent: "create" } });
    expect(enhance.ok).toBe(true);
    expect(assistStreamRoute({ mode: "context", context })).toMatchObject({ ok: false, code: "bad_request" });
  });

  it("builds cancel and replay paths only from a well-formed key", () => {
    const key = "11111111-2222-4333-8444-555555555555";
    expect(cancelRoute(key)).toMatchObject({ method: "POST", path: `/requests/${key}/cancel` });
    expect(replayRoute(key)).toMatchObject({ method: "GET", path: `/requests/${key}` });
    for (const bad of ["", "../session", "a/b", "x".repeat(65), "key with spaces"]) {
      expect(cancelRoute(bad)).toBeNull();
      expect(replayRoute(bad)).toBeNull();
    }
  });
});

describe("removed kinds stay removed", () => {
  it("has no route for any kind of the local product", () => {
    for (const kind of REMOVED_MESSAGE_KINDS) {
      expect(apiRouteFor({ kind, payload: { action: "start" } })).toMatchObject({ ok: false, code: "unknown_kind" });
    }
    expect(Object.values(MESSAGE_KINDS)).not.toEqual(expect.arrayContaining([...REMOVED_MESSAGE_KINDS]));
  });
});

describe("own material id validation", () => {
  it("accepts only 1..64 characters of letters, digits, _ and -", () => {
    expect(OWN_MATERIAL_ID.test("a".repeat(64))).toBe(true);
    for (const id of ["", "a".repeat(65), "../../session", "a/b", "a b", "a%2Fb", "émoji"]) {
      expect(apiRouteFor({ kind: MESSAGE_KINDS.ownMaterial, payload: { action: "remove", body: { id } } }), JSON.stringify(id))
        .toMatchObject({ ok: false, code: "bad_request" });
    }
    expect(apiRouteFor({ kind: MESSAGE_KINDS.ownMaterial, payload: { action: "remove", body: { id: 42 } } })).toMatchObject({ ok: false });
    expect(apiRouteFor({ kind: MESSAGE_KINDS.ownMaterial, payload: { action: "add", body: { kind: "facts", text: "x" } } })).toMatchObject({ ok: false });
    expect(apiRouteFor({ kind: MESSAGE_KINDS.ownMaterial, payload: { action: "purge" } })).toMatchObject({ ok: false });
  });
});

describe("open-site allowlist", () => {
  it("opens only the fixed website pages", () => {
    for (const page of Object.keys(SITE_PAGES)) expect(openSiteTarget({ page })).toBe(page);
    for (const page of ["evil", "https://evil.example", "/account", "__proto__", "constructor", "toString", "", null, 3]) {
      expect(openSiteTarget({ page }), String(page)).toBeNull();
    }
    expect(openSiteTarget(undefined)).toBeNull();
  });

  it("parses consent actions strictly", () => {
    expect(parseConsentAction({ action: "get" })).toEqual({ action: "get" });
    expect(parseConsentAction({ action: "accept", passive_learning: true })).toEqual({ action: "accept", passive_learning: true });
    expect(parseConsentAction({ action: "set-passive", passive_learning: false })).toEqual({ action: "set-passive", passive_learning: false });
    expect(parseConsentAction({ action: "withdraw" })).toEqual({ action: "withdraw" });
    expect(parseConsentAction({ action: "accept" })).toBeNull();
    expect(parseConsentAction({ action: "accept", passive_learning: "yes" })).toBeNull();
    expect(parseConsentAction({ action: "grant-all" })).toBeNull();
  });
});
