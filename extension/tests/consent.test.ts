import { describe, expect, it } from "vitest";
import {
  CONSENT_VERSION,
  DISCLOSURE,
  DISCLOSURE_SHA256,
  acceptConsent,
  consentAllows,
  consentBody,
  disclosureText,
  hasCurrentConsent,
  normalizeConsent,
  serverConsentNeedsSync,
  withPassiveLearning,
} from "../shared/consent";
import {
  canCapturePassive,
  canMountDock,
  canRequest,
  canScanPosts,
  initialBridgeState,
  isBridgeState,
  isExcludedRoute,
  type BridgeState,
  type PageGate,
} from "../shared/session-state";

async function sha256Hex(text: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("");
}

const NOW = Date.parse("2026-09-14T12:00:00Z");
const HOME: PageGate = { pageDisabled: false, pathname: "/home" };

function bridge(overrides: Partial<BridgeState> = {}): BridgeState {
  return {
    ...initialBridgeState("1.0.0", NOW),
    configured: true,
    consent: { accepted: true, passive_learning: false },
    signed_in: true,
    ...overrides,
  };
}

describe("the disclosure", () => {
  it("is pinned to CONSENT_VERSION by its hash", async () => {
    expect(
      await sha256Hex(disclosureText()),
      "The disclosure text changed. Bump CONSENT_VERSION, then set DISCLOSURE_SHA256 to the new hash.",
    ).toBe(DISCLOSURE_SHA256);
    expect(CONSENT_VERSION).toMatch(/^\d{4}-\d{2}-\d{2}\.\d+$/);
  });

  it("names everything that is sent, who gets it, and what never happens", () => {
    const text = disclosureText();
    for (const phrase of [
      "post you ask about", "author's name and handle", "links", "image addresses and descriptions", "counts", "nearby posts",
      "composer", "small copies of images", "Create or Enhance",
      "type or attach in the Starling panel",
      "used, edited or posted",
      "account and usage information",
      "Starling's servers, operated by the service owner", "AI provider",
      "not sold", "advertising", "export or delete", "never presses Post", "direct messages",
    ]) {
      expect(text, phrase).toContain(phrase);
    }
    expect(DISCLOSURE.passive.label).toBe("Also learn from my own profile and posts while I browse");
    expect(DISCLOSURE.agree).toBe("Agree and continue");
    expect(DISCLOSURE.decline).toBe("Not now");
  });
});

describe("consent gating", () => {
  it("allows nothing without consent", () => {
    expect(hasCurrentConsent(null)).toBe(false);
    expect(consentAllows("main", null)).toBe(false);
    expect(consentAllows("passive", undefined)).toBe(false);
  });

  it("main consent covers requests and the post-confirmation check, not passive capture", () => {
    const record = acceptConsent(false, NOW);
    expect(record).toEqual({ version: CONSENT_VERSION, passive_learning: false, accepted_at: NOW });
    expect(consentAllows("main", record)).toBe(true);
    expect(consentAllows("passive", record)).toBe(false);
    expect(consentBody(record)).toEqual({ version: CONSENT_VERSION, passive_learning: false });
  });

  it("the passive-learning switch turns passive capture on and off", () => {
    const on = withPassiveLearning(acceptConsent(false, NOW), true);
    expect(consentAllows("passive", on)).toBe(true);
    const off = withPassiveLearning(on, false);
    expect(consentAllows("passive", off)).toBe(false);
    expect(consentAllows("main", off)).toBe(true);
    // A switch without consent is not consent.
    expect(withPassiveLearning(null, true)).toBeNull();
  });

  it("consent to an older disclosure does not count", () => {
    const stale = { version: "2026-01-01.1", passive_learning: true, accepted_at: NOW };
    expect(consentAllows("main", stale)).toBe(false);
    expect(withPassiveLearning(stale, true)).toBeNull();
  });

  it("withdrawal returns everything to the no-consent state", () => {
    const withdrawn = normalizeConsent(undefined);
    expect(withdrawn).toBeNull();
    expect(consentAllows("main", withdrawn)).toBe(false);
    const state = bridge({ consent: { accepted: false, passive_learning: false } });
    expect(canMountDock(state, HOME)).toBe(true);
    expect(canScanPosts(state, HOME)).toBe(false);
    expect(canRequest(state, HOME)).toBe(false);
    expect(canCapturePassive(state, HOME)).toBe(false);
  });

  it("reads stored records defensively", () => {
    expect(normalizeConsent({ version: CONSENT_VERSION, passive_learning: "yes", accepted_at: "then" }))
      .toEqual({ version: CONSENT_VERSION, passive_learning: false, accepted_at: 0 });
    expect(normalizeConsent({ passive_learning: true })).toBeNull();
    expect(normalizeConsent([CONSENT_VERSION])).toBeNull();
  });

  it("re-sends this browser's answer only when the server disagrees and can accept it", () => {
    const record = acceptConsent(true, NOW);
    expect(serverConsentNeedsSync(record, { required_version: CONSENT_VERSION, accepted_version: null, passive_learning: false })).toBe(true);
    expect(serverConsentNeedsSync(record, { required_version: CONSENT_VERSION, accepted_version: CONSENT_VERSION, passive_learning: false })).toBe(true);
    expect(serverConsentNeedsSync(record, { required_version: CONSENT_VERSION, accepted_version: CONSENT_VERSION, passive_learning: true })).toBe(false);
    expect(serverConsentNeedsSync(record, { required_version: "2027-01-01.1", accepted_version: null, passive_learning: false })).toBe(false);
    expect(serverConsentNeedsSync(null, { required_version: CONSENT_VERSION, accepted_version: null, passive_learning: false })).toBe(false);
  });
});

describe("page gates", () => {
  it("never touches direct messages, chat, settings or login flows", () => {
    for (const path of ["/messages", "/messages/123-456", "/i/chat", "/i/chat/abc", "/settings", "/settings/account", "/i/flow/login", "/Messages"]) {
      expect(isExcludedRoute(path), path).toBe(true);
      const page = { pageDisabled: false, pathname: path };
      expect(canMountDock(bridge(), page)).toBe(false);
      expect(canScanPosts(bridge(), page)).toBe(false);
      expect(canRequest(bridge(), page)).toBe(false);
    }
    for (const path of ["/home", "/sample/status/1", "/messagesfromfans", "/i/bookmarks", "/settingsguru"]) {
      expect(isExcludedRoute(path), path).toBe(false);
    }
  });

  it("each gate adds exactly one requirement", () => {
    expect(canMountDock(bridge(), { pageDisabled: true, pathname: "/home" })).toBe(false);
    expect(canRequest(bridge({ signed_in: false }), HOME)).toBe(false);
    expect(canScanPosts(bridge({ signed_in: false }), HOME)).toBe(true);
    expect(canRequest(bridge({ online: false }), HOME)).toBe(false);
    expect(canRequest(bridge({ outdated: true }), HOME)).toBe(false);
    expect(canRequest(bridge({ configured: false }), HOME)).toBe(false);
    expect(canRequest(bridge(), HOME)).toBe(true);
    expect(canCapturePassive(bridge(), HOME)).toBe(false);
    expect(canCapturePassive(bridge({ consent: { accepted: true, passive_learning: true } }), HOME)).toBe(true);
    expect(canCapturePassive(bridge({ consent: { accepted: true, passive_learning: true }, signed_in: false }), HOME)).toBe(false);
  });

  it("recognises a bridge state and rejects the old local shape", () => {
    expect(isBridgeState(bridge())).toBe(true);
    expect(isBridgeState({ healthy: true, authenticated: true, checked_at: "x" })).toBe(false);
  });
});
