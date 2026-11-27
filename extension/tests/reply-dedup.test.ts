import { describe, expect, it } from "vitest";
import { findOwnReplyToTarget } from "../shared/reply-dedup";

describe("reply duplicate guard", () => {
  const target = { postId: "root-1", authorHandle: "@source" };

  it("blocks a visible reply with the exact parent post id", () => {
    const match = findOwnReplyToTarget(target, "@me", [{
      postId: "reply-1", authorHandle: "me", parentPostId: "root-1", isReply: true,
    }]);
    expect(match?.postId).toBe("reply-1");
  });

  it("uses the detail-page fallback only for the signed-in author and target handle", () => {
    expect(findOwnReplyToTarget(target, "me", [{
      postId: "reply-2", authorHandle: "@me", repliedToHandle: "source",
      isReply: true, followsTargetOnDetailPage: true,
    }])?.postId).toBe("reply-2");
    expect(findOwnReplyToTarget(target, "me", [{
      postId: "other-reply", authorHandle: "other", repliedToHandle: "source",
      isReply: true, followsTargetOnDetailPage: true,
    }])).toBeNull();
  });

  it("does not mistake an unrelated reply to the same author for this post", () => {
    expect(findOwnReplyToTarget(target, "me", [{
      postId: "reply-3", authorHandle: "me", repliedToHandle: "source", isReply: true,
    }])).toBeNull();
  });
});
