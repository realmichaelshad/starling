import { describe, expect, it } from "vitest";
import { REPLY_MODAL_GRACE_MS, isAllowedNativeClick, isBehindXModal, isSafeComposerCandidate, isXComposeRoute, mayReuseOpenReplyBox, selectLauncherComposer, shouldRetainComposerIntentOnNavigation } from "../shared/x-action-guards";

describe("X native-action allowlist", () => {
  it("allows only the exact native controls required to open a composer", () => {
    expect(isAllowedNativeClick({ testId: "reply", label: "Reply" }, "reply")).toBe(true);
    expect(isAllowedNativeClick({ testId: "retweet", label: "Repost" }, "repost_menu")).toBe(true);
    expect(isAllowedNativeClick({ role: "menuitem", label: "Quote" }, "quote")).toBe(true);
    expect(isAllowedNativeClick({ testId: "SideNav_NewTweet_Button", label: "Post" }, "compose")).toBe(true);
    expect(isAllowedNativeClick({ composeLink: true, label: "Post" }, "compose")).toBe(true);
    // X renders Quote as a compose-path link inside the repost menu. It must
    // stay reserved for the quote flow rather than being captured as a post.
    expect(isAllowedNativeClick({ composeLink: true, role: "menuitem", label: "Quote" }, "compose")).toBe(false);
  });

  it("fails closed for final submit and every prohibited account action", () => {
    for (const testId of ["tweetButton", "replyButton", "delete", "like", "unlike", "follow", "message", "mediaUpload"]) {
      expect(isAllowedNativeClick({ testId, label: "Post" }, "compose")).toBe(false);
      expect(isAllowedNativeClick({ testId, label: "Reply" }, "reply")).toBe(false);
    }
  });

  it("rejects ambiguous labels and wrong action paths", () => {
    expect(isAllowedNativeClick({ role: "menuitem", label: "Quote post" }, "quote")).toBe(false);
    expect(isAllowedNativeClick({ testId: "retweet" }, "reply")).toBe(false);
    expect(isAllowedNativeClick({ testId: "reply" }, "compose")).toBe(false);
  });

  it("never types into an unrelated composer that was already open", () => {
    expect(isSafeComposerCandidate("reply", { wasVisibleBefore: true, belongsToTargetPost: true })).toBe(true);
    expect(isSafeComposerCandidate("reply", { wasVisibleBefore: true, belongsToTargetPost: false })).toBe(false);
    expect(isSafeComposerCandidate("quote", { wasVisibleBefore: true })).toBe(false);
    expect(isSafeComposerCandidate("post", { wasVisibleBefore: true })).toBe(false);
    expect(isSafeComposerCandidate("reply", { wasVisibleBefore: false })).toBe(true);
    expect(isSafeComposerCandidate("quote", { wasVisibleBefore: false })).toBe(true);
    expect(isSafeComposerCandidate("post", { wasVisibleBefore: false })).toBe(true);
  });
});

describe("first-party launcher composer binding", () => {
  const candidate = (overrides: Partial<Parameters<typeof selectLauncherComposer>[0][number]> = {}) =>
    ({ insideSurface: false, submitMatches: false, existedBefore: false, ...overrides });

  it("binds outright to the single composer tied to the launcher's surface or submit", () => {
    const inSurface = candidate({ insideSurface: true, existedBefore: true });
    expect(selectLauncherComposer([inSurface, candidate({ existedBefore: true })])).toBe(inSurface);
    const bySubmit = candidate({ submitMatches: true, existedBefore: true });
    expect(selectLauncherComposer([bySubmit])).toBe(bySubmit);
  });

  it("pre-existing composers are fine when directly tied — the pre-mounted inline reply box", () => {
    // This is the exact shape the strict native matrix fails closed on:
    // X pre-mounts the reply composer BESIDE the post before any click.
    const preMounted = candidate({ insideSurface: true, submitMatches: true, existedBefore: true });
    expect(selectLauncherComposer([preMounted])).toBe(preMounted);
  });

  it("two directly-tied composers refuse to guess", () => {
    expect(selectLauncherComposer([
      candidate({ insideSurface: true }),
      candidate({ submitMatches: true }),
    ])).toBe(null);
  });

  it("with no direct tie, exactly one fresh composer wins (X swapped the subtree)", () => {
    const fresh = candidate();
    expect(selectLauncherComposer([fresh, candidate({ existedBefore: true })])).toBe(fresh);
    expect(selectLauncherComposer([candidate(), candidate()])).toBe(null);
    expect(selectLauncherComposer([candidate({ existedBefore: true })])).toBe(null);
    expect(selectLauncherComposer([])).toBe(null);
  });
});

describe("native composer intent across SPA navigation", () => {
  it("keeps only the expected route transition into X's compose modal", () => {
    expect(shouldRetainComposerIntentOnNavigation("quote", "https://x.com/home", "https://x.com/compose/post")).toBe(true);
    expect(shouldRetainComposerIntentOnNavigation("post", "https://x.com/alice", "https://x.com/compose/post")).toBe(true);
    // X opens a reply in its modal at /compose/post too (x.com, 30 Oct 2026), even from the post's own page.
    expect(shouldRetainComposerIntentOnNavigation("reply", "https://x.com/home", "https://x.com/compose/post")).toBe(true);
    expect(shouldRetainComposerIntentOnNavigation("reply", "https://x.com/alice/status/1", "https://x.com/compose/post")).toBe(true);
    expect(shouldRetainComposerIntentOnNavigation("reply", "https://x.com/alice/status/1", "https://x.com/alice")).toBe(false);
    expect(shouldRetainComposerIntentOnNavigation("quote", "https://x.com/home", "https://x.com/messages")).toBe(false);
    expect(shouldRetainComposerIntentOnNavigation("quote", "https://x.com/compose/post", "https://x.com/compose/post?draft=1")).toBe(false);
    expect(shouldRetainComposerIntentOnNavigation("quote", "not a URL", "https://x.com/compose/post")).toBe(false);
  });
});

describe("X's modal composer and the page's box behind it", () => {
  it("recognises X's compose route", () => {
    expect(isXComposeRoute("/compose/post")).toBe(true);
    expect(isXComposeRoute("/compose/post/")).toBe(true);
    expect(isXComposeRoute("/alice/status/1")).toBe(false);
    expect(isXComposeRoute("/compose/post/unsent/drafts")).toBe(false);
  });

  it("a box inside the modal is the one being written in", () => {
    expect(isBehindXModal({ inDialog: true, ariaHidden: false, composeRoute: true })).toBe(false);
    expect(isBehindXModal({ inDialog: true, ariaHidden: true, composeRoute: true })).toBe(false);
  });

  it("the page's own reply box behind the modal is not", () => {
    // X re-mounts it as a fresh element at /compose/post before the modal's box exists.
    expect(isBehindXModal({ inDialog: false, ariaHidden: false, composeRoute: true })).toBe(true);
    // ...and hides the page with aria-hidden once the modal is up.
    expect(isBehindXModal({ inDialog: false, ariaHidden: true, composeRoute: false })).toBe(true);
  });

  it("an ordinary inline reply box on a post page stays usable", () => {
    expect(isBehindXModal({ inDialog: false, ariaHidden: false, composeRoute: false })).toBe(false);
  });

  it("a reply box already under the post waits for X's modal before taking a reply", () => {
    expect(mayReuseOpenReplyBox(150, false)).toBe(false);
    expect(mayReuseOpenReplyBox(REPLY_MODAL_GRACE_MS - 1, false)).toBe(false);
    expect(mayReuseOpenReplyBox(REPLY_MODAL_GRACE_MS, false)).toBe(true);
    // X opened its modal: that box, never the one under the post.
    expect(mayReuseOpenReplyBox(REPLY_MODAL_GRACE_MS * 5, true)).toBe(false);
  });
});
