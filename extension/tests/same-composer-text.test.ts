import { describe, expect, it } from "vitest";
import { sameComposerText } from "../shared/composer-flow";

// Regression guard for the "X kept rejecting the typed text. Copy the finished
// version instead." bug: the verification used to compare X's re-rendered
// innerText to the draft EXACTLY, so any whitespace reshaping read as a failure.
describe("sameComposerText — content match, whitespace-insensitive", () => {
  it("matches a multi-line draft against X's block-reflowed innerText", () => {
    // A real create-post draft has blank lines; X drops the doubled newline.
    const draft = "Claude 3.5 vs. Opus 4.8? Let's talk numbers.\n\nThe real story is tool-use.";
    const xRerendered = "Claude 3.5 vs. Opus 4.8? Let's talk numbers.\nThe real story is tool-use.";
    expect(sameComposerText(draft, xRerendered)).toBe(true);
  });

  it("matches when X trims trailing whitespace and adds zero-width chars", () => {
    const draft = "Same take here — tool use is the story.  ";
    const xRerendered = "Same take here ​— tool use is the story.";
    expect(sameComposerText(draft, xRerendered)).toBe(true);
  });

  it("matches identical single-line text", () => {
    expect(sameComposerText("hello world", "hello world")).toBe(true);
  });

  it("still reports a REAL difference (different words) as not-equal", () => {
    expect(sameComposerText("my reply about grok", "a totally different draft")).toBe(false);
  });

  it("treats empty vs non-empty as not-equal", () => {
    expect(sameComposerText("", "something landed")).toBe(false);
  });
});
