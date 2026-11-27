import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { ATTACHABLE_RIGHTS, canAttach, creditLine, toFile } from "../shared/image-attach";
import { isAllowedNativeClick } from "../shared/x-action-guards";

const ok = {
  rights: "user_owned" as const,
  dataUrl: "data:image/png;base64,AAAA",
  altText: "A latency comparison chart",
};

describe("the rights gate is re-checked at the composer", () => {
  it("refuses unknown rights", () => {
    const result = canAttach({ ...ok, rights: "unknown" });
    expect(result.ok).toBe(false);
    expect((result as { reason: string }).reason).toContain("unknown");
  });

  it("refuses blocked rights", () => {
    expect(canAttach({ ...ok, rights: "blocked" }).ok).toBe(false);
  });

  it.each(ATTACHABLE_RIGHTS)("allows %s", rights => {
    expect(canAttach({ ...ok, rights }).ok).toBe(true);
  });
});

describe("only a locally prepared copy may be attached", () => {
  it("refuses a remote URL outright", () => {
    const result = canAttach({ ...ok, dataUrl: "https://example.com/photo.jpg" });
    expect(result.ok).toBe(false);
    expect((result as { reason: string }).reason).toContain("locally prepared");
  });

  it("refuses non-image data URLs", () => {
    expect(canAttach({ ...ok, dataUrl: "data:text/html;base64,PGgxPm5vdC1hbi1pbWFnZTwvaDE+" }).ok).toBe(false);
  });

  it("accepts a blob URL", () => {
    expect(canAttach({ ...ok, dataUrl: "blob:abc" }).ok).toBe(true);
  });

  it("refuses an empty source", () => {
    expect(canAttach({ ...ok, dataUrl: "" }).ok).toBe(false);
  });
});

describe("alt text is required", () => {
  it("refuses an empty description", () => {
    // Worse than none: it looks described when it is not.
    expect(canAttach({ ...ok, altText: "   " }).ok).toBe(false);
  });
});

describe("X's own media controls stay forbidden", () => {
  it("still refuses to click the upload button", () => {
    // Attaching happens by paste, so the click allowlist did NOT need widening.
    expect(isAllowedNativeClick({ testId: "mediaUpload" }, "compose")).toBe(false);
    expect(isAllowedNativeClick({ testId: "addMedia" }, "compose")).toBe(false);
  });

  it("still refuses to click post and reply submit buttons", () => {
    for (const testId of ["tweetButton", "postButton", "replyButton"]) {
      expect(isAllowedNativeClick({ testId }, "compose")).toBe(false);
    }
  });
});

describe("attribution", () => {
  it("uses the required credit verbatim when the licence sets one", () => {
    expect(creditLine({ requiredCredit: "Photo: Jane Doe (CC BY 4.0)" })).toBe(
      "Photo: Jane Doe (CC BY 4.0)"
    );
  });

  it("falls back to creator and source", () => {
    expect(creditLine({ creator: "Jane", sourceUrl: "https://example.com/x" })).toBe(
      "Jane — https://example.com/x"
    );
  });

  it("returns nothing when no attribution is owed", () => {
    expect(creditLine({})).toBeNull();
  });
});

describe("the attach verdict does not come from dispatchEvent", () => {
  // A DOM test would be better and this harness has no jsdom, so the rule is
  // pinned at the source. It is worth pinning because the old code had the
  // verdict exactly INVERTED on the composer X actually ships.
  //
  // `dispatchEvent` returns false only when a handler called preventDefault, and
  // true otherwise — including when nothing handled the event. X's composer is
  // Draft.js, and its `editOnPaste` calls preventDefault as its FIRST statement,
  // before it looks at the pasted files. So every paste that landed reported
  // "X refused the paste; attach the image manually.", and a page with no
  // listener at all reported success. Measured on both shapes.
  const source = readFileSync(
    new URL("../shared/image-attach.ts", import.meta.url), "utf8");

  it("never treats the dispatch result as an attach signal", () => {
    expect(source).not.toMatch(/(?:const|let|var)\s+\w+\s*=\s*[\w.]*dispatchEvent\(/);
    expect(source).not.toMatch(/if\s*\(\s*!\s*delivered\s*\)/);
  });

  it("reads the composer back instead", () => {
    expect(source).toContain("querySelector");
    expect(source).toMatch(/attachments/);
  });

  it("does not assert refusal when it cannot tell", () => {
    // Absence of the media tray means "did not land" OR "the selector is stale",
    // and this code cannot distinguish them. Claiming refusal is the original bug.
    //
    // Matched against RETURNED REASONS, not the whole file: the comment above the
    // fix quotes the old wording on purpose, so a plain substring search over the
    // source finds the explanation and calls it the defect.
    const reasons = [...source.matchAll(/reason:\s*"([^"]*)"/g)].map(m => m[1]);
    expect(reasons.some(r => r.includes("refused the paste"))).toBe(false);
    expect(reasons.some(r => r.includes("couldn't confirm it landed"))).toBe(true);
  });
});

describe("the attach wait cannot hang", () => {
  const source = readFileSync(
    new URL("../shared/image-attach.ts", import.meta.url), "utf8");

  it("races the animation frames against a timer", () => {
    // requestAnimationFrame does not fire in a tab Chrome has stopped painting,
    // so an unconditional `await new Promise(rAF)` never resolves there — the
    // attach hangs and the caller is left on a spinner with no error. Found by a
    // harness that hung on precisely this.
    expect(source).toMatch(/setTimeout\(\s*done\s*,\s*\d+\s*\)/);
    expect(source).toContain("requestAnimationFrame");
    // And it must settle once, not resolve twice.
    expect(source).toMatch(/let\s+settled\s*=\s*false/);
  });
});

describe("the file handed to X's composer is what the data says it is", () => {
  const png = { ...ok, dataUrl: "data:image/png;base64,iVBORw0KGgo=", mimeType: "image/png", fileName: "chart.png" };

  it("ignores the service's declared type and file name", async () => {
    // Found by the security review: the last gate before the user's composer checked the rights, the
    // data URL and the alt text, and took the file's TYPE and NAME on the service's say-so.
    const result = await toFile({ ...png, mimeType: "text/html", fileName: "../../invoice.html" });
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.file.type).toBe("image/png");
    expect(result.file.name).toBe("image.png");
  });

  it("keeps a plain name and gives it the extension of the real type", async () => {
    const result = await toFile({ ...png, dataUrl: "data:image/jpeg;base64,/9j/4AAQ", mimeType: "image/png", fileName: "latency chart.png" });
    expect(result.ok && result.file.name).toBe("latency chart.jpg");
    expect(result.ok && result.file.type).toBe("image/jpeg");
  });

  it.each(["a".repeat(200) + ".png", "chart<script>.png", "folder/x.png", "..png", ".png", ""])("falls back for the name %j", async fileName => {
    const result = await toFile({ ...png, fileName });
    expect(result.ok && result.file.name).toBe("image.png");
  });
});
