import { describe, expect, it } from "vitest";
import { SITE_PAGES, isLocalHost, isSafeSitePath, sitePageForPath, sitePageUrl, validateStarlingOrigin } from "../shared/config";

const production = { mode: "production", storeBuild: false };
const store = { mode: "production", storeBuild: true };
const e2e = { mode: "e2e", storeBuild: false };

describe("WXT_STARLING_ORIGIN validation", () => {
  it("accepts an https origin and normalizes it", () => {
    expect(validateStarlingOrigin("https://app.starling-hosting.com", store)).toBe("https://app.starling-hosting.com");
    expect(validateStarlingOrigin("  https://App.Starling-Hosting.com/  ", store)).toBe("https://app.starling-hosting.com");
    expect(validateStarlingOrigin("https://app.starling-hosting.com:8443", production)).toBe("https://app.starling-hosting.com:8443");
  });

  it("accepts an origin only", () => {
    for (const value of [
      "https://app.starling-hosting.com/api/v1", "https://app.starling-hosting.com?x=1", "https://app.starling-hosting.com#top",
      "https://user:pass@app.starling-hosting.com", "app.starling-hosting.com", "ftp://app.starling-hosting.com", "",
    ]) {
      expect(() => validateStarlingOrigin(value, production), value).toThrowError(/WXT_STARLING_ORIGIN/);
    }
    expect(() => validateStarlingOrigin(undefined, production)).toThrowError(/not set/);
  });

  it("allows plain http only for loopback in an e2e build", () => {
    expect(validateStarlingOrigin("http://127.0.0.1:8765", e2e)).toBe("http://127.0.0.1:8765");
    expect(validateStarlingOrigin("http://localhost:8765", e2e)).toBe("http://localhost:8765");
    expect(() => validateStarlingOrigin("http://127.0.0.1:8765", production)).toThrowError(/https/);
    expect(() => validateStarlingOrigin("http://app.starling-hosting.com", e2e)).toThrowError(/https/);
    expect(() => validateStarlingOrigin("http://192.168.1.20:8765", e2e)).toThrowError(/https/);
  });

  it("a store build refuses local, private and IP-literal hosts", () => {
    for (const value of [
      "http://127.0.0.1:8765", "https://127.0.0.1", "https://localhost", "https://10.0.0.5", "https://192.168.0.2",
      "https://starling.local", "https://gateway.internal", "https://app.test", "https://[::1]", "https://203.0.113.9",
    ]) {
      expect(() => validateStarlingOrigin(value, { ...store, mode: value.startsWith("http:") ? "e2e" : "production" }), value).toThrowError();
    }
  });

  it("every build refuses the documentation domains, which are real hosts nobody here runs", () => {
    // Found by the security review: `https://app.example.com` passed every check, the build verifier
    // certified the package, and this suite asserted it. example.com/.net/.org resolve, and belong to IANA.
    for (const value of ["https://app.example.com", "https://example.org", "https://api.example.net", "https://EXAMPLE.com", "https://app.example.edu"]) {
      for (const rules of [store, production, e2e]) {
        expect(() => validateStarlingOrigin(value, rules), value).toThrowError(/documentation host/);
      }
    }
    // A host that merely contains the word is not one.
    expect(validateStarlingOrigin("https://app.examplestudio.com", store)).toBe("https://app.examplestudio.com");
    expect(validateStarlingOrigin("https://example-apps.com", store)).toBe("https://example-apps.com");
  });

  it("every build refuses placeholders", () => {
    for (const value of ["https://your-domain.com", "https://[[OPERATOR_DOMAIN]]", "https://{{origin}}", "https://changeme.example.org", "https://todo.app", "<origin>"]) {
      expect(() => validateStarlingOrigin(value, production), value).toThrowError();
    }
  });

  it("knows local hosts", () => {
    for (const host of ["localhost", "127.0.0.1", "10.1.2.3", "172.16.0.1", "172.31.255.255", "192.168.1.1", "169.254.10.10", "100.64.0.1", "0.0.0.0", "[::1]", "[fd12::1]", "[fe80::1]", "api.localhost", "box.local"]) {
      expect(isLocalHost(host), host).toBe(true);
    }
    for (const host of ["app.starling-hosting.com", "172.32.0.1", "8.8.8.8", "localhost.example.com"]) {
      expect(isLocalHost(host), host).toBe(false);
    }
  });
});

describe("website pages", () => {
  it("builds page URLs from the origin", () => {
    expect(sitePageUrl("https://app.example.com", "limit")).toBe("https://app.example.com/pricing?from=extension&reason=limit");
    expect(SITE_PAGES).toEqual({
      pricing: "/pricing?from=extension",
      limit: "/pricing?from=extension&reason=limit",
      account: "/account",
      billing: "/account/billing",
      devices: "/account/devices",
      verify: "/account",
      data: "/account/data",
      privacy: "/privacy",
      contact: "/contact",
    });
  });

  it("accepts an upgrade path only as a same-origin path", () => {
    expect(isSafeSitePath("/pricing?from=extension&reason=limit")).toBe(true);
    expect(isSafeSitePath("/account")).toBe(true);
    for (const value of ["//evil.example", "https://evil.example/pricing", "/\\evil.example", "/a b", "pricing", "", "/\u0000x", null, 5]) {
      expect(isSafeSitePath(value), String(value)).toBe(false);
    }
    expect(sitePageForPath("/pricing?from=extension&reason=limit")).toBe("limit");
    expect(sitePageForPath("/pricing?from=elsewhere")).toBeNull();
    expect(sitePageForPath("https://app.example.com/account")).toBeNull();
  });
});
