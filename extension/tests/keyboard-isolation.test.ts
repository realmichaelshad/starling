import { describe, expect, it, vi } from "vitest";
import { SEALED_EVENTS, isEditableTarget, sealShadowKeyboard, shouldSealEvent, shouldShieldTyping } from "../shared/keyboard-isolation";

/** Minimal stand-ins: this suite runs without a DOM, like the rest of the repo. */
const element = (tagName: string, contentEditable = false) =>
  ({ nodeType: 1, tagName, isContentEditable: contentEditable }) as unknown as HTMLElement;

function fakeEvent(type: string, from: unknown) {
  const stopPropagation = vi.fn();
  return { event: { type, target: from, composedPath: () => [from], stopPropagation } as unknown as Event, stopPropagation };
}

function fakeRoot() {
  const listeners = new Map<string, (event: Event) => void>();
  const root = {
    addEventListener: (type: string, listener: (event: Event) => void) => listeners.set(type, listener),
    removeEventListener: (type: string) => listeners.delete(type),
  } as unknown as ShadowRoot;
  return { root, listeners };
}

describe("sealShadowKeyboard", () => {
  it("seals the whole keyboard, clipboard and IME family", () => {
    const { root, listeners } = fakeRoot();
    sealShadowKeyboard(root);
    expect([...listeners.keys()].sort()).toEqual([...SEALED_EVENTS].sort());
    // The keys X steals mid-sentence must all be covered.
    for (const type of ["keydown", "keypress", "keyup"]) expect(listeners.has(type)).toBe(true);
  });

  it("stops events from leaving the shadow root", () => {
    const { root, listeners } = fakeRoot();
    sealShadowKeyboard(root);
    // "?" is the key that opened X's shortcuts dialog while the user typed.
    const { event, stopPropagation } = fakeEvent("keydown", element("TEXTAREA"));
    listeners.get("keydown")!(event);
    expect(stopPropagation).toHaveBeenCalledTimes(1);
  });

  it("editableOnly withholds keys only from real fields, never from buttons", () => {
    const { root, listeners } = fakeRoot();
    sealShadowKeyboard(root, { editableOnly: true });
    const keydown = listeners.get("keydown")!;

    // A pill/card button holds focus: X must still get its shortcut.
    const fromButton = fakeEvent("keydown", element("BUTTON"));
    keydown(fromButton.event);
    expect(fromButton.stopPropagation).not.toHaveBeenCalled();

    // A text field on the same surface is protected.
    const fromField = fakeEvent("keydown", element("INPUT"));
    keydown(fromField.event);
    expect(fromField.stopPropagation).toHaveBeenCalledTimes(1);
  });

  it("unseals cleanly and tolerates a missing root", () => {
    const { root, listeners } = fakeRoot();
    const unseal = sealShadowKeyboard(root);
    expect(listeners.size).toBe(SEALED_EVENTS.length);
    unseal();
    expect(listeners.size).toBe(0);
    expect(() => sealShadowKeyboard(null)()).not.toThrow();
    expect(() => sealShadowKeyboard(undefined)()).not.toThrow();
  });
});

describe("capture-phase shield", () => {
  const host = element("DIV");
  const field = element("TEXTAREA");

  it("fires when the event comes from our host and our field holds focus", () => {
    // A page-level listener sees the retargeted HOST, never the inner field —
    // the browser harness proved an inner-node check never matches.
    expect(shouldShieldTyping(fakeEvent("keydown", host).event, host, field, field)).toBe(true);
    // A key the PAGE made up. The shield hands keys to the dock's own handler, where Enter SENDS the
    // message, and focus inside a closed root is visible from the page (document.activeElement is the
    // host): x.com's script could wait until the user was typing and dispatch Enter at the host.
    const synthetic = { ...fakeEvent("keydown", host).event, isTrusted: false } as unknown as Event;
    expect(shouldShieldTyping(synthetic, host, field, field)).toBe(false);
    const real = { ...fakeEvent("keydown", host).event, isTrusted: true } as unknown as Event;
    expect(shouldShieldTyping(real, host, field, field)).toBe(true);
  });

  it("stays out of the way otherwise", () => {
    // Focus is on a button in our dock, not the field: X keeps its shortcut.
    expect(shouldShieldTyping(fakeEvent("keydown", host).event, host, element("BUTTON"), field)).toBe(false);
    // Nothing in the dock is focused.
    expect(shouldShieldTyping(fakeEvent("keydown", host).event, host, null, field)).toBe(false);
    // The key came from X's own page, not from us.
    expect(shouldShieldTyping(fakeEvent("keydown", element("DIV")).event, host, field, field)).toBe(false);
    // The dock does not exist yet.
    expect(shouldShieldTyping(fakeEvent("keydown", host).event, null, field, field)).toBe(false);
    expect(shouldShieldTyping(fakeEvent("keydown", host).event, host, field, null)).toBe(false);
  });
});

describe("seal decisions", () => {
  it("recognises the fields a user types into", () => {
    expect(isEditableTarget(element("TEXTAREA"))).toBe(true);
    expect(isEditableTarget(element("INPUT"))).toBe(true);
    expect(isEditableTarget(element("DIV", true))).toBe(true);
    expect(isEditableTarget(element("BUTTON"))).toBe(false);
    expect(isEditableTarget(element("DIV"))).toBe(false);
    expect(isEditableTarget(null)).toBe(false);
    expect(isEditableTarget({ nodeType: 3 } as unknown as EventTarget)).toBe(false);
  });

  it("defaults to sealing everything, and falls back to target without composedPath", () => {
    expect(shouldSealEvent(fakeEvent("keydown", element("BUTTON")).event)).toBe(true);
    const legacy = { type: "keydown", target: element("TEXTAREA") } as unknown as Event;
    expect(shouldSealEvent(legacy, { editableOnly: true })).toBe(true);
  });
});
