import { describe, expect, it } from "vitest";
import { matchesComposerHandoffTarget } from "../shared/composer-handoff";

describe("composer handoff target", () => {
  it("uses the exact clicked post when X temporarily exposes no post id or URL", () => {
    const clickedPost = {};
    expect(matchesComposerHandoffTarget({
      sessionAction: "reply",
      targetAction: "reply",
      sessionSource: clickedPost,
      targetSource: clickedPost,
    })).toBe(true);
  });

  it("does not treat a nearby or differently typed composer as the opened target", () => {
    const clickedPost = {};
    expect(matchesComposerHandoffTarget({
      sessionAction: "reply",
      targetAction: "reply",
      sessionSource: {},
      targetSource: clickedPost,
    })).toBe(false);
    expect(matchesComposerHandoffTarget({
      sessionAction: "quote",
      targetAction: "reply",
      sessionSource: clickedPost,
      targetSource: clickedPost,
    })).toBe(false);
  });

  it("also accepts independently captured contexts with the exact same post id", () => {
    expect(matchesComposerHandoffTarget({
      sessionAction: "reply",
      targetAction: "reply",
      sessionContext: { post_id: "123" },
      targetContext: { post_id: "123" },
      sessionSource: {},
      targetSource: {},
    })).toBe(true);
  });
});
