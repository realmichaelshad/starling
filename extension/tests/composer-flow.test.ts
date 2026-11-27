import { describe, expect, it } from "vitest";
import {
  canReplaceComposerDraft, composerAnchor, composerControlCopy, isComposerControlDisabled, isEnhanceReady,
  isNativeComposerSubmitReady, missingSourceLine, resolveComposerControlState, undoDecision, type ComposerUndo,
} from "../shared/composer-flow";

describe("a reply or quote box whose post Starling cannot read", () => {
  it("says what is missing, what to do, and that nothing was used", () => {
    const reply = missingSourceLine("reply");
    expect(reply).toContain("couldn't read the post you're replying to");
    expect(reply).toContain("press ✦ on the post itself");
    expect(reply).toMatch(/No draft was used\.$/);
    expect(missingSourceLine("quote")).toContain("the post you're quoting");
  });

  it("never blames the AI for it", () => {
    expect(missingSourceLine("reply")).not.toMatch(/AI|responding|try again in a moment/i);
  });
});

describe("composer enhancement flow", () => {
  it("mirrors X readiness without inventing a minimum draft length", () => {
    expect(isEnhanceReady({ text: "", isComposing: false, xAcceptsDraft: true })).toBe(false);
    expect(isEnhanceReady({ text: "   ", isComposing: false, xAcceptsDraft: true })).toBe(false);
    expect(isEnhanceReady({ text: "I", isComposing: false, xAcceptsDraft: true })).toBe(true);
    expect(isEnhanceReady({ text: "I", isComposing: true, xAcceptsDraft: true })).toBe(false);
    expect(isEnhanceReady({ text: "I", isComposing: false, xAcceptsDraft: false })).toBe(false);
  });

  it("fails closed when X's native submit control is missing or ambiguous", () => {
    expect(isNativeComposerSubmitReady({ matchingSubmitCount: 0, enabled: true })).toBe(false);
    expect(isNativeComposerSubmitReady({ matchingSubmitCount: 2, enabled: true })).toBe(false);
    expect(isNativeComposerSubmitReady({ matchingSubmitCount: 1, enabled: false })).toBe(false);
    expect(isNativeComposerSubmitReady({ matchingSubmitCount: 1, enabled: true })).toBe(true);
  });

  it("rejects replacement for a stale, hidden, closed, or edited composer", () => {
    const safe = { expectedOriginal: "my real thought", currentText: "my real thought", sessionRequestId: 4, requestId: 4, connected: true, visible: true };
    expect(canReplaceComposerDraft(safe)).toBe(true);
    expect(canReplaceComposerDraft({ ...safe, currentText: "my edited thought" })).toBe(false);
    expect(canReplaceComposerDraft({ ...safe, sessionRequestId: 5 })).toBe(false);
    expect(canReplaceComposerDraft({ ...safe, connected: false })).toBe(false);
    expect(canReplaceComposerDraft({ ...safe, visible: false })).toBe(false);
    // X's own invisible re-normalization (collapsed whitespace, zero-width
    // chars) must NOT read as a user edit — that caused spurious "draft changed".
    expect(canReplaceComposerDraft({ ...safe, currentText: "my  real​ thought " })).toBe(true);
  });

  it("keeps the dock anchored to the exact X action and visible context", () => {
    expect(composerAnchor("post")).toEqual({ label: "New post", excerpt: "Your words, sharpened without changing the point." });
    expect(composerAnchor("reply", "@sample_author", "  This is a useful point\nwith context. ")).toEqual({
      label: "Replying to @sample_author", excerpt: "This is a useful point with context."
    });
    expect(composerAnchor("quote", "@sample_author", "Source text")).toEqual({ label: "Quoting @sample_author", excerpt: "Source text" });
  });

  it("names the exact reply, quote, or post action in the composer control", () => {
    expect(composerControlCopy("reply", "create", "ready")).toEqual({
      visible: "Create reply", ariaLabel: "Create a reply with Starling"
    });
    expect(composerControlCopy("reply", "enhance", "sending")).toEqual({
      visible: "Enhancing reply", ariaLabel: "Cancel enhancing a reply with Starling"
    });
    expect(composerControlCopy("quote", "create", "typing")).toEqual({
      visible: "Writing quote", ariaLabel: "Cancel writing your quote with Starling"
    });
    // Right after a draft: "✓ Done", never an invitation to rewrite it.
    expect(composerControlCopy("post", "enhance", "updated")).toEqual({
      visible: "Done", ariaLabel: "Done. Your post is in X's box"
    });
    // Then, while the box still holds Starling's draft: a DIFFERENT draft, priced.
    expect(composerControlCopy("reply", "enhance", "another")).toEqual({
      visible: "Try another", ariaLabel: "Write a different reply with Starling. Uses 1 draft"
    });
    // Priced from the session's usage.per_request, with the right plural.
    expect(composerControlCopy("reply", "enhance", "another", true, false, 2).ariaLabel)
      .toBe("Write a different reply with Starling. Uses 2 drafts");
    expect(composerControlCopy("reply", "enhance", "another", true, false, 0).ariaLabel)
      .toBe("Write a different reply with Starling. Uses 1 draft");
    expect(composerControlCopy("reply", "enhance", "error")).toEqual({
      visible: "Try again", ariaLabel: "Reply did not finish. Try again"
    });
    expect(composerControlCopy("reply", "blocked", "idle", true)).toEqual({
      visible: "Enhance reply", ariaLabel: "Finish editing this reply before enhancing with Starling"
    });
    expect(composerControlCopy("post", "blocked", "idle", false)).toEqual({
      visible: "Create post", ariaLabel: "Finish composing before creating a post with Starling"
    });
  });

  it("never names an internal code name in any control label", () => {
    for (const action of ["reply", "quote", "post"] as const) {
      for (const mode of ["create", "enhance", "blocked"] as const) {
        for (const state of [undefined, "idle", "ready", "sending", "typing", "updated", "error"] as const) {
          expect(composerControlCopy(action, mode, state, true).ariaLabel).not.toMatch(/forge/i);
        }
      }
    }
  });
});

describe("composerAnchor names a self-reply as its own move", () => {
  it("says self-reply / self-quote on your own post", () => {
    expect(composerAnchor("reply", "@sample_self", "x", true).label)
      .toBe("Self-reply to your post");
    expect(composerAnchor("quote", "@sample_self", "x", true).label)
      .toBe("Self-quote of your post");
  });

  it("still names the other account when the post is not yours", () => {
    expect(composerAnchor("reply", "@claudeai", "x", false).label)
      .toBe("Replying to @claudeai");
    expect(composerAnchor("quote", "@claudeai", "x").label)
      .toBe("Quoting @claudeai");
  });

  it("leaves a standalone post alone either way", () => {
    expect(composerAnchor("post", undefined, undefined, true).label).toBe("New post");
    expect(composerAnchor("post", undefined, undefined, false).label).toBe("New post");
  });
});

describe("the composer button names a self-reply", () => {
  it("says self-reply / self-quote on your own post", () => {
    expect(composerControlCopy("reply", "create", undefined, false, true).visible)
      .toBe("Create self-reply");
    expect(composerControlCopy("quote", "enhance", undefined, true, true).visible)
      .toBe("Enhance self-quote");
    expect(composerControlCopy("reply", "create", "sending", false, true).visible)
      .toBe("Creating self-reply");
  });

  it("is unchanged on someone else's post", () => {
    expect(composerControlCopy("reply", "create", undefined, false, false).visible)
      .toBe("Create reply");
    expect(composerControlCopy("quote", "enhance", undefined, true).visible)
      .toBe("Enhance quote");
  });

  it("never says self-post — a standalone post has no parent", () => {
    expect(composerControlCopy("post", "create", undefined, false, true).visible)
      .toBe("Create post");
  });
});

describe("after Starling types a draft", () => {
  const undo: ComposerUndo = { original: "my rough line", inserted: "A sharper line." };

  it("shows Done, then offers a different draft only while the box holds Starling's", () => {
    // The two seconds after a draft: kept as is, and disabled.
    expect(resolveComposerControlState({ previous: "updated", mode: "enhance", text: "A sharper line.", undo })).toBe("updated");
    expect(isComposerControlDisabled("updated", "enhance")).toBe(true);
    // Settled, untouched: "Try another".
    expect(resolveComposerControlState({ previous: "idle", mode: "enhance", text: "A sharper line.", undo })).toBe("another");
    expect(resolveComposerControlState({ previous: "another", mode: "enhance", text: " A  sharper line.\u200b", undo })).toBe("another");
    expect(isComposerControlDisabled("another", "enhance")).toBe(false);
    // The customer edits: Enhance again, for THEIR text.
    expect(resolveComposerControlState({ previous: "another", mode: "enhance", text: "A sharper line!", undo })).toBe("ready");
    // Undone, or never drafted: the ordinary states.
    expect(resolveComposerControlState({ previous: "another", mode: "enhance", text: "A sharper line.", undo: { ...undo, restored: true } })).toBe("ready");
    expect(resolveComposerControlState({ previous: undefined, mode: "create", text: "", undo: null })).toBe("ready");
    expect(resolveComposerControlState({ previous: "ready", mode: "blocked", text: "x", undo })).toBe("idle");
  });

  it("never disables the pill while it is the Cancel button", () => {
    expect(isComposerControlDisabled("sending", "blocked")).toBe(false);
    expect(isComposerControlDisabled("typing", "blocked")).toBe(false);
    expect(resolveComposerControlState({ previous: "typing", mode: "enhance", text: "A sharper line.", undo })).toBe("typing");
  });

  it("lets Undo restore only what Starling typed, never the customer's edit", () => {
    expect(undoDecision("A sharper line.", undo)).toBe("restore");
    expect(undoDecision("A sharper line.  ", undo)).toBe("restore");
    expect(undoDecision("A sharper line, edited.", undo)).toBe("edited");
    expect(undoDecision(null, undo)).toBe("gone");
  });
});
