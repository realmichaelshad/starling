import { describe, expect, it } from "vitest";
import { existsSync, readFileSync, readdirSync } from "node:fs";

/**
 * The device token must never enter x.com's renderer.
 *
 * Content scripts run inside the page's process. The token lives in
 * chrome.storage.local and is read and written by the background worker only,
 * so no file that ends up in a content script may name its key, read the whole
 * store with `get(null)`, or subscribe to storage changes (a change event
 * carries the new token value to every listener).
 *
 * Structural on purpose: it follows the real import graph from every content
 * entrypoint, so a shared helper that starts reading storage is caught too.
 */
const ROOT = new URL("../", import.meta.url);

function read(relative: string): string {
  return readFileSync(new URL(relative, ROOT), "utf8");
}

function resolveImport(fromRelative: string, specifier: string): string | null {
  const target = new URL(specifier, new URL(fromRelative, ROOT)).href.slice(ROOT.href.length);
  for (const candidate of [target, `${target}.ts`, `${target}/index.ts`]) {
    if (/\.ts$/.test(candidate) && existsSync(new URL(candidate, ROOT))) return candidate;
  }
  return null;
}

function contentFiles(): string[] {
  const entries = readdirSync(new URL("entrypoints/", ROOT))
    .filter(name => name === "content.ts" || name.endsWith(".content.ts"))
    .map(name => `entrypoints/${name}`);
  const seen = new Set<string>();
  const queue = [...entries];
  while (queue.length) {
    const file = queue.shift()!;
    if (seen.has(file)) continue;
    seen.add(file);
    const source = read(file);
    const specifiers = [
      ...source.matchAll(/\b(?:import|export)\b[^;]*?\bfrom\s*["']([^"']+)["']/g),
      ...source.matchAll(/\bimport\s*\(\s*["']([^"']+)["']\s*\)/g),
      ...source.matchAll(/^\s*import\s*["']([^"']+)["']/gm),
    ].map(match => match[1]);
    for (const specifier of specifiers) {
      if (!specifier.startsWith(".")) continue;
      const resolved = resolveImport(file, specifier);
      if (resolved) queue.push(resolved);
    }
  }
  return [...seen].sort();
}

describe("the device token stays out of content scripts", () => {
  const files = contentFiles();

  it("follows the whole content import graph", () => {
    expect(files).toEqual(expect.arrayContaining([
      "entrypoints/content.ts",
      "entrypoints/early.content.ts",
      "shared/session-state.ts",
      "shared/usage-ui.ts",
    ]));
  });

  it("no content-side file names the token's storage key", () => {
    for (const file of files) {
      expect(read(file), file).not.toContain("starling:auth");
    }
  });

  it("no content-side file reads all of storage or listens for storage changes", () => {
    for (const file of files) {
      const source = read(file);
      expect(source, file).not.toMatch(/\.get\(\s*null\s*\)/);
      expect(source, file).not.toMatch(/\.get\(\s*\)/);
      expect(source, file).not.toMatch(/storage\s*\.\s*(?:local\s*\.\s*|sync\s*\.\s*|session\s*\.\s*)?onChanged/);
    }
  });

  it("no content-side file opens extension storage at all", () => {
    // Structural, not textual. The three checks above search for the key's NAME, and a computed key
    // ("starling" + ":auth") or a key imported from elsewhere passes all of them. Since the security
    // review a content script does not call chrome.storage: its own preferences go through the worker
    // (shared/page-prefs.ts), and the worker closes the area to everything but itself and extension pages.
    for (const file of files) {
      const code = read(file).replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
      expect(code, file).not.toMatch(/chrome\s*\.\s*storage/);
      expect(code, file).not.toMatch(/browser\s*\.\s*storage/);
    }
  });

  it("the worker closes the token's storage area to content scripts, every time it starts", () => {
    const worker = read("entrypoints/background.ts");
    expect(worker).toMatch(/chrome\.storage\.local\.setAccessLevel\(\{\s*accessLevel:\s*"TRUSTED_CONTEXTS"\s*\}\)/);
    expect(worker).toMatch(/defineBackground\(\(\) => \{\s*lockStorageToTrustedContexts\(\);/);
  });

  it("no content-side file talks to the network or a native host", () => {
    for (const file of files) {
      const source = read(file);
      expect(source, file).not.toMatch(/\bfetch\s*\(/);
      expect(source, file).not.toMatch(/connectNative|sendNativeMessage/);
      expect(source, file).not.toMatch(/\bXMLHttpRequest\b|\bWebSocket\b|\bEventSource\b/);
    }
  });

  it("only the background worker touches the token's key", () => {
    const owners: string[] = [];
    for (const dir of ["entrypoints", "shared"]) {
      for (const name of readdirSync(new URL(`${dir}/`, ROOT), { recursive: true }) as string[]) {
        const file = `${dir}/${String(name).replace(/\\/g, "/")}`;
        if (!/\.(ts|html)$/.test(file)) continue;
        if (read(file).includes("starling:auth")) owners.push(file);
      }
    }
    expect(owners).toEqual(["entrypoints/background.ts"]);
  });
});
