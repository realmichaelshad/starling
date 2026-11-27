import { describe, expect, it } from "vitest";
import { isAllowedNativeClick, isSafeComposerCandidate, selectSingleSafeComposerCandidate, type NativePurpose } from "../shared/x-action-guards";

describe("composer safety matrix", () => {
  const purposes: NativePurpose[] = ["reply", "repost_menu", "quote", "compose"];
  const prohibited = [
    "tweetButton",
    "tweetButtonInline",
    "replyButton",
    "delete",
    "like",
    "unlike",
    "follow",
    "unfollow",
    "message",
    "dm",
    "mediaUpload",
    "addMedia",
  ];

  it("never allowlists publish, social, destructive, messaging, or media controls", () => {
    for (const testId of prohibited) {
      for (const purpose of purposes) {
        expect(isAllowedNativeClick({ testId, label: "Quote", role: "menuitem", composeLink: true }, purpose)).toBe(false);
      }
    }
  });

  it("requires the precise control shape for each composer-opening path", () => {
    expect(isAllowedNativeClick({ testId: "reply" }, "reply")).toBe(true);
    expect(isAllowedNativeClick({ testId: "reply", role: "menuitem" }, "quote")).toBe(false);

    expect(isAllowedNativeClick({ testId: "retweet" }, "repost_menu")).toBe(true);
    expect(isAllowedNativeClick({ testId: "unretweet" }, "repost_menu")).toBe(true);
    expect(isAllowedNativeClick({ testId: "repost" }, "repost_menu")).toBe(false);

    expect(isAllowedNativeClick({ role: "menuitem", label: "Quote" }, "quote")).toBe(true);
    expect(isAllowedNativeClick({ role: "button", label: "Quote" }, "quote")).toBe(false);
    expect(isAllowedNativeClick({ role: "menuitem", label: "Quote post" }, "quote")).toBe(false);

    expect(isAllowedNativeClick({ testId: "SideNav_NewTweet_Button" }, "compose")).toBe(true);
    expect(isAllowedNativeClick({ composeLink: true }, "compose")).toBe(true);
    expect(isAllowedNativeClick({ testId: "tweetButton" }, "compose")).toBe(false);
  });

  it("fails closed for all pre-existing composers except the exact inline reply destination", () => {
    for (const intent of ["reply", "quote", "post"] as const) {
      expect(isSafeComposerCandidate(intent, { wasVisibleBefore: true })).toBe(false);
      expect(isSafeComposerCandidate(intent, { wasVisibleBefore: true, belongsToTargetPost: false })).toBe(false);
    }

    expect(isSafeComposerCandidate("reply", { wasVisibleBefore: true, belongsToTargetPost: true })).toBe(true);
  });

  it("binds an explicit X action only when exactly one composer is safe", () => {
    const existing = { id: "existing", wasVisibleBefore: true, belongsToTargetPost: false };
    const fresh = { id: "fresh", wasVisibleBefore: false };
    expect(selectSingleSafeComposerCandidate("post", [existing, fresh])).toEqual(fresh);
    expect(selectSingleSafeComposerCandidate("quote", [fresh, { id: "also-fresh", wasVisibleBefore: false }])).toBeNull();
    expect(selectSingleSafeComposerCandidate("reply", [{ id: "inline", wasVisibleBefore: true, belongsToTargetPost: true }])).toMatchObject({ id: "inline" });
  });
});
