export interface ComposerPostReference {
  post_id?: string;
  post_url?: string;
}

/** Two independently captured contexts point at the same rendered X post. */
export function sameComposerPostReference(
  left: ComposerPostReference | undefined,
  right: ComposerPostReference | undefined,
): boolean {
  if (!left || !right) return false;
  if (left === right) return true;
  return Boolean(
    (left.post_id && right.post_id && left.post_id === right.post_id)
    || (left.post_url && right.post_url && left.post_url === right.post_url),
  );
}

/**
 * Proves that a composer belongs to one explicit Starling/X handoff. The
 * source reference is object identity, never DOM proximity: it is the exact
 * post element whose native control Starling clicked. This covers the brief
 * X transition where a reply box is mounted without exposing its permalink.
 */
export function matchesComposerHandoffTarget<T>(input: {
  sessionAction: string;
  targetAction: string;
  sessionContext?: ComposerPostReference;
  targetContext?: ComposerPostReference;
  sessionSource?: T;
  targetSource?: T;
}): boolean {
  if (input.sessionAction !== input.targetAction) return false;
  if (!input.targetContext && input.targetSource == null) return true;

  const exactContext = sameComposerPostReference(input.sessionContext, input.targetContext);
  const exactSource = input.targetSource != null && input.sessionSource === input.targetSource;
  return input.targetContext ? exactContext || exactSource : exactSource;
}
