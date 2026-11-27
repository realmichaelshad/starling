/**
 * A deliberately small, DOM-free duplicate-reply check. The content script
 * turns X's rendered articles into these records; keeping the decision here
 * makes the identity rule testable without guessing from page proximity.
 */
export interface ReplyTarget {
  postId?: string;
  authorHandle?: string;
}

export interface VisibleReplyRecord {
  postId?: string;
  authorHandle?: string;
  parentPostId?: string;
  repliedToHandle?: string;
  isReply: boolean;
  /** Only true on the target's own detail page, after the target article. */
  followsTargetOnDetailPage?: boolean;
}

function handle(value?: string): string {
  return (value || "").trim().replace(/^@/, "").toLowerCase();
}

/**
 * Return an existing reply only with exact evidence: a parent post id is best;
 * X's detail-page "Replying to @…" layout is the narrowly-scoped fallback.
 */
export function findOwnReplyToTarget(
  target: ReplyTarget,
  signedInHandle: string | undefined,
  replies: readonly VisibleReplyRecord[],
): VisibleReplyRecord | null {
  const author = handle(signedInHandle);
  const targetId = (target.postId || "").trim();
  const targetAuthor = handle(target.authorHandle);
  if (!author) return null;

  for (const reply of replies) {
    if (!reply.isReply || handle(reply.authorHandle) !== author) continue;
    if (targetId && reply.parentPostId === targetId) return reply;
    if (targetId && reply.followsTargetOnDetailPage
      && targetAuthor && handle(reply.repliedToHandle) === targetAuthor) return reply;
  }
  return null;
}
