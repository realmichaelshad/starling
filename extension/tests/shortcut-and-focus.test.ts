import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

/**
 * Two promises the panel makes about the keyboard, pinned in the source because they are about
 * wiring rather than logic: the shortcut is Chrome's (so it works on a Mac and never fights
 * Chrome's own Alt+F menu key), and a card appearing never takes the keyboard away from X.
 */
const read = (relative: string) => readFileSync(fileURLToPath(new URL(relative, import.meta.url)), "utf8");
const strip = (source: string) => source.split("\n")
  .filter(line => !line.trim().startsWith("//") && !line.trim().startsWith("*") && !line.trim().startsWith("/*"))
  .join("\n");
const CONFIG = read("../wxt.config.ts");
const CONTENT = strip(read("../entrypoints/content.ts"));
const WORKER = strip(read("../entrypoints/background.ts"));

describe("the keyboard shortcut", () => {
  it("is a Chrome command, suggested as Alt+Shift+S and Control+Shift+S on a Mac", () => {
    expect(CONFIG).toMatch(/_execute_action:\s*\{\s*suggested_key:\s*\{\s*default:\s*"Alt\+Shift\+S",\s*mac:\s*"MacCtrl\+Shift\+S"\s*\}/);
    // A command is not a permission: the manifest's permission list is unchanged.
    expect(CONFIG).toContain('permissions: ["storage", "identity"]');
  });

  it("has no page-level listener left to fight Chrome's menu key", () => {
    expect(CONTENT).not.toMatch(/altKey[\s\S]{0,120}toLowerCase\(\)\s*===\s*"f"/);
  });

  it("is read from Chrome, so every tooltip names the key actually bound", () => {
    expect(WORKER).toContain("chrome.commands.getAll()");
    expect(CONTENT).toContain("bridgeState.shortcut");
  });
});

describe("focus", () => {
  it("only an open the customer asked for moves the keyboard into the panel", () => {
    const openDock = CONTENT.slice(CONTENT.indexOf("function openDock("), CONTENT.indexOf("function closeDock("));
    expect(openDock).toMatch(/if \(options\.focusInput\) shadow\.querySelector<HTMLTextAreaElement>\("textarea"\)\?\.focus\(\);/);
    // Cards (every progress card, every notice) open the panel without focusing it.
    const addCard = CONTENT.slice(CONTENT.indexOf("function addCard("), CONTENT.indexOf("function setFeedScrollTop("));
    expect(addCard).toContain("openDock();");
    expect(addCard).not.toContain("focusInput");
  });
});
