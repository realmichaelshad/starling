/**
 * The build verifier, tested: a good package passes and each kind of bad package fails.
 *
 * `scripts/verify-extension-build.mjs` decides whether a package may go to the Chrome Web Store, and
 * the documentation said it was tested when nothing tested it. The security review then found that
 * its `web_accessible_resources` check had a clause that could never be true and never looked at
 * `extension_ids`, and that it searched the bundle for nine product wordings but not for the one thing
 * it exists to find: code loaded from somewhere else. Each case below is a regression it must catch.
 */
import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterAll, describe, expect, it } from "vitest";

const VERIFIER = join(__dirname, "..", "scripts", "verify-extension-build.mjs");
const ORIGIN = "https://app.starling-hosting.com";
const made: string[] = [];

const goodManifest = () => ({
  manifest_version: 3,
  name: "Starling - AI writing assistant for X",
  description: "Drafts replies, quotes and posts on X in your own voice. You review every draft and press Post yourself.",
  version: "1.0.0",
  minimum_chrome_version: "120",
  homepage_url: ORIGIN,
  permissions: ["storage", "identity"],
  host_permissions: ["https://x.com/*", "https://twitter.com/*", `${ORIGIN}/*`],
  background: { service_worker: "background.js" },
  content_scripts: [{ matches: ["https://x.com/*", "https://twitter.com/*"], js: ["content-scripts/content.js"] }],
  web_accessible_resources: [{ resources: ["assets/starling-wordmark.png"], matches: ["https://x.com/*", "https://twitter.com/*"] }],
});

function build(manifest: object, files: Record<string, string> = {}): string {
  const dir = mkdtempSync(join(tmpdir(), "starling-verify-"));
  made.push(dir);
  mkdirSync(join(dir, "content-scripts"));
  writeFileSync(join(dir, "manifest.json"), JSON.stringify(manifest));
  writeFileSync(join(dir, "background.js"), "const origin = 'https://app.starling-hosting.com';");
  writeFileSync(join(dir, "content-scripts", "content.js"), "document.title;");
  for (const [name, text] of Object.entries(files)) writeFileSync(join(dir, name), text);
  return dir;
}

function verify(dir: string, ...flags: string[]) {
  const run = spawnSync(process.execPath, [VERIFIER, dir, ...flags], { encoding: "utf8" });
  return { code: run.status, output: `${run.stdout}${run.stderr}` };
}

afterAll(() => {
  for (const dir of made) rmSync(dir, { recursive: true, force: true });
});

describe("the extension build verifier", () => {
  it("passes a package shaped like the real store build", () => {
    const result = verify(build(goodManifest()));
    expect(result.output).toContain("PASS");
    expect(result.code).toBe(0);
  });

  it.each([
    ["an extra permission", (m: any) => { m.permissions.push("tabs"); }, /permissions must be exactly/],
    ["a host permission for every site", (m: any) => { m.host_permissions.push("<all_urls>"); }, /host_permissions must be exactly/],
    ["a pinned key", (m: any) => { m.key = "MIIB"; }, /must not contain "key"/],
    ["externally_connectable", (m: any) => { m.externally_connectable = { matches: ["https://x.com/*"] }; }, /externally_connectable/],
    ["a version name in a store build", (m: any) => { m.version_name = "1.0.0 e2e"; }, /version_name/],
    ["a local service address", (m: any) => { m.homepage_url = "http://127.0.0.1:8765"; m.host_permissions[2] = "http://127.0.0.1:8765/*"; }, /homepage_url/],
    ["a content script in the page's own world", (m: any) => { m.content_scripts[0].world = "MAIN"; }, /MAIN world/],
    ["a content script on another site", (m: any) => { m.content_scripts[0].matches.push("https://example.org/*"); }, /outside X/],
    ["a resource shown to another site", (m: any) => { m.web_accessible_resources[0].matches.push("https://evil.example/*"); }, /web_accessible_resources/],
    ["a resource shown to other extensions", (m: any) => { m.web_accessible_resources = [{ resources: ["assets/x.png"], extension_ids: ["*"] }]; }, /web_accessible_resources/],
    ["a resource entry with no matches at all", (m: any) => { m.web_accessible_resources = [{ resources: ["assets/x.png"] }]; }, /web_accessible_resources/],
    ["an extension page any site could frame", (m: any) => { m.web_accessible_resources[0].resources.push("onboarding.html"); }, /exposes a page or script/],
  ])("fails %s", (_label, spoil, message) => {
    const manifest = goodManifest();
    spoil(manifest);
    const result = verify(build(manifest));
    expect(result.code).toBe(1);
    expect(result.output).toMatch(message);
  });

  it.each([
    ["eval", "const run = eval('1 + 1');", /eval\(\)/],
    ["new Function", "const run = new Function('return 1');", /new Function\(\)/],
    ["a module from another host", "import('https://cdn.example.net/x.js');", /imported from another host/],
    ["importScripts from another host", "importScripts('https://cdn.example.net/x.js');", /importScripts from another host/],
    ["a source map reference", "var a = 1;\n//# sourceMappingURL=background.js.map", /source map/],
    ["native messaging", "chrome.runtime.connectNative('com.example');", /native messaging/],
    ["the retired companion wording", "const css = '/* Only rendered when the companion refused */';", /"companion" wording/],
    ["the companion wording in capitals", "const note = 'Starling Companion';", /"companion" wording/],
  ])("fails a bundle that contains %s", (_label, code, message) => {
    const result = verify(build(goodManifest(), { "background.js": code }));
    expect(result.code).toBe(1);
    expect(result.output).toMatch(message);
  });

  it("fails a page that loads a script from another host", () => {
    const result = verify(build(goodManifest(), { "onboarding.html": '<script src="https://cdn.example.net/x.js"></script>' }));
    expect(result.code).toBe(1);
    expect(result.output).toMatch(/script loaded from another host/);
  });

  it("does not mistake ordinary code for remote code", () => {
    const harmless = "const retrieval = medieval(); obj.eval(1); const s = 'see https://starling.example/docs';";
    expect(verify(build(goodManifest(), { "background.js": harmless })).code).toBe(0);
  });

  it("allows a local address only when told the build is for the local test stack", () => {
    const manifest: any = goodManifest();
    manifest.homepage_url = "http://127.0.0.1:8765";
    manifest.host_permissions[2] = "http://127.0.0.1:8765/*";
    manifest.version_name = "1.0.0 e2e";
    const dir = build(manifest, { "background.js": "const origin = 'http://127.0.0.1:8765';" });
    expect(verify(dir).code).toBe(1);
    expect(verify(dir, "--allow-local").code).toBe(0);
  });
});
