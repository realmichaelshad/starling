/**
 * A due-draft card may leave the dock only after the local scheduler confirms
 * the exact draft and terminal state requested. This rejects malformed,
 * stale, and cross-tab responses without trusting their shape.
 */
export function isScheduleMutationConfirmed(value: unknown, draftId: number,
                                            status: "posted" | "dismissed"): boolean {
  if (typeof value !== "object" || value === null) return false;
  const response = value as Record<string, unknown>;
  return response.ok === true && Number(response.id) === draftId && response.status === status;
}

/** A save card may settle only after the local store echoes the exact held
 * draft and target. An arbitrary id is not proof that this user action won. */
export function isScheduleSaveConfirmed(value: unknown, text: string, targetAt: string,
                                        targetLabel?: string): boolean {
  if (typeof value !== "object" || value === null) return false;
  const response = value as Record<string, unknown>;
  if (response.ok !== true || typeof response.scheduled !== "object" || response.scheduled === null) return false;
  const scheduled = response.scheduled as Record<string, unknown>;
  const requestedTime = Date.parse(targetAt);
  const savedTime = typeof scheduled.target_at === "string" ? Date.parse(scheduled.target_at) : Number.NaN;
  return Number.isSafeInteger(Number(scheduled.id))
    && Number(scheduled.id) > 0
    && scheduled.status === "scheduled"
    && scheduled.action === "post"
    && scheduled.text === text.trim()
    && Number.isFinite(requestedTime)
    && savedTime === requestedTime
    && (targetLabel == null || scheduled.target_label === targetLabel);
}
