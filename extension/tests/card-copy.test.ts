import { describe, expect, it } from "vitest";
import { cardIcon, decideWaitingCopy, decisionBadgeLabel, draftHasCheckableClaims, formatElapsed, resumeActionLabel, topicLine } from "../shared/card-copy";

describe("the words a waiting ✦ card says", () => {
  it("are true at every moment, and the last one says stopping is free", () => {
    const copy = decideWaitingCopy();
    expect(copy.label).toBe("Writing your reply…");
    expect(copy.beats).toEqual([
      [8_000, "Still writing your reply…"],
      [25_000, "Still writing. Busy moments can take up to a minute."],
      [45_000, "Taking longer than usual. Keep waiting, or Cancel. Nothing is used until a draft arrives."],
    ]);
    // The worker gives up at 60 s: every beat must land before that.
    for (const [at] of copy.beats) expect(at).toBeLessThan(60_000);
    expect(decideWaitingCopy("quote").label).toBe("Writing your quote…");
    expect(decideWaitingCopy("reply", true).label).toBe("Writing your self-reply…");
  });

  it("never pretends to know a stage the one-shot request does not report", () => {
    const all = [decideWaitingCopy().label, ...decideWaitingCopy().beats.map(([, text]) => text)].join(" ");
    expect(all).not.toMatch(/Reading the post|its thread and your history|Sending to Starling/);
  });

  it("counts the wait in whole seconds", () => {
    expect(formatElapsed(0)).toBe("0s");
    expect(formatElapsed(12_900)).toBe("12s");
    expect(formatElapsed(65_000)).toBe("1m 05s");
  });
});

describe("verdict badges", () => {
  it("read as advice, not as error codes", () => {
    expect(decisionBadgeLabel("wait")).toBe("Better to wait");
    expect(decisionBadgeLabel("skip")).toBe("Worth skipping");
    expect(decisionBadgeLabel("reply")).toBe("Reply");
    expect(decisionBadgeLabel("quote")).toBe("Quote");
  });
});

describe("the button that picks a refused draft back up", () => {
  it("names the draft it will write", () => {
    expect(resumeActionLabel({ kind: "decide", handle: "@adeveloper" })).toBe("Draft the reply to @adeveloper");
    expect(resumeActionLabel({ kind: "enhance", action: "reply", handle: "adeveloper" })).toBe("Draft the reply to @adeveloper");
    expect(resumeActionLabel({ kind: "enhance", action: "quote", handle: "@patships" })).toBe("Draft the quote of @patships");
    expect(resumeActionLabel({ kind: "enhance", action: "post" })).toBe("Draft the post");
    expect(resumeActionLabel({ kind: "decide", own: true })).toBe("Draft the self-reply");
    expect(resumeActionLabel({ kind: "decide" })).toBe("Draft the reply");
    expect(resumeActionLabel({ kind: "chat" })).toBe("Ask again");
  });

  it("gives each card kind its glyph", () => {
    expect(cardIcon("decide")).toBe("✦");
    expect(cardIcon("enhance", "reply")).toBe("↩");
    expect(cardIcon("enhance", "quote")).toBe("❝");
    expect(cardIcon("enhance", "post")).toBe("✎");
  });
});

describe("when the no-web-check caveat is worth showing", () => {
  it("only for drafts that carry numbers, @names or dates", () => {
    for (const draft of [
      "Cut the build from 9 minutes to 4.",
      "Same thing @patships said last week.",
      "Shipping on October 21.",
      "Out Oct 3 for everyone.",
      "We tried this on Monday.",
      "Version 2 fixes it.",
    ]) {
      expect(draftHasCheckableClaims(draft), draft).toBe(true);
    }
    for (const draft of [
      "Deleting steps nobody runs is the most honest optimisation there is.",
      "You may be right about the timeout.",
      "The boring option wins again, and that is the point.",
      "Email me at someone@example.com if you want the details",
      "",
      undefined,
    ]) {
      expect(draftHasCheckableClaims(draft), String(draft)).toBe(false);
    }
  });
});

describe("the story a created post is about", () => {
  it("names it in one line", () => {
    expect(topicLine("Open-weight model tops the coding benchmark")).toBe("About: Open-weight model tops the coding benchmark");
    expect(topicLine("  two\n  lines  ")).toBe("About: two lines");
  });

  it("says nothing when there is no topic", () => {
    for (const value of [undefined, null, "", "   ", 7, {}]) expect(topicLine(value), String(value)).toBeNull();
  });

  it("keeps a long headline to one line", () => {
    const line = topicLine("word ".repeat(60));
    expect(line!.length).toBeLessThanOrEqual("About: ".length + 140);
    expect(line!.endsWith("…")).toBe(true);
  });
});
