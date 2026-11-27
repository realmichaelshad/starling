/**
 * Pure lifecycle for one generation operation (enhance / chat / decide).
 *
 * Every card in the dock belongs to exactly one operation, and every timer or
 * stream callback mutates the UI only by dispatching an OpEvent through this
 * machine. Terminal phases are absorbing: transitionOp returns the SAME
 * reference for any event on a completed/cancelled/failed op, so a leaked
 * heartbeat or late chunk is structurally incapable of resurrecting a
 * "Writing your post…" label next to a "Cancelled." line.
 */
export type OpKind = "enhance" | "chat" | "decide";

export type OpPhase =
  | "preparing"   // launcher clicked; X's composer not open yet (enhance only)
  | "generating"  // request in flight (stream or one-shot)
  | "typing"      // typewriter inserting into X's composer
  | "completed"
  | "cancelled"
  | "failed";

export interface GenerationOp {
  id: string;
  kind: OpKind;
  phase: OpPhase;
  /** Heartbeat/substage text currently shown on the loading card. */
  note?: string;
  /**
   * Every substage this operation has passed through, oldest first — the
   * running account of what Starling actually did. A single swapped-out label
   * ("Thinking…" → "Writing…") told the user nothing about what happened in
   * between, so waiting read as one long silence; the card now shows the whole
   * trail with the current step live at the bottom.
   */
  steps: string[];
  /** Card holds user-meaningful content (streamed chat text, a result). */
  hasContent: boolean;
  error?: string;
  restoredOriginal?: boolean;
  startedAt: number;
}

export type OpEvent =
  | { type: "target-opened" }
  | { type: "typing-started" }
  | { type: "tokens" }
  | { type: "heartbeat"; note: string }
  | { type: "completed"; hasContent?: boolean }
  /** `keep` leaves the card in place to say what stopping cost ("Stopped. No draft used."). */
  | { type: "cancelled"; restoredOriginal?: boolean; keep?: boolean }
  | { type: "failed"; error: string };

let opCounter = 0;

export function createOp(kind: OpKind, phase: OpPhase = "generating", now = Date.now()): GenerationOp {
  opCounter += 1;
  return {
    id: `op-${now.toString(36)}-${opCounter}`,
    kind,
    phase,
    steps: [],
    hasContent: false,
    startedAt: now,
  };
}

export function isTerminal(op: GenerationOp): boolean {
  return op.phase === "completed" || op.phase === "cancelled" || op.phase === "failed";
}

export function transitionOp(op: GenerationOp, event: OpEvent): GenerationOp {
  if (isTerminal(op)) return op;
  switch (event.type) {
    case "target-opened":
      return op.phase === "preparing" ? { ...op, phase: "generating" } : op;
    case "typing-started":
      return op.phase === "generating" ? { ...op, phase: "typing" } : op;
    case "tokens":
      return op.phase === "generating" && !op.hasContent ? { ...op, hasContent: true } : op;
    case "heartbeat":
      // Repeating the same substage is not news. Returning the SAME reference
      // means renderOpCard is never called for it, so the step list cannot
      // grow a run of identical lines and the card never repaints for nothing.
      if (op.note === event.note) return op;
      return { ...op, note: event.note, steps: [...op.steps, event.note] };
    case "completed":
      // A preparing op has no request whose completion could apply to it.
      return op.phase === "preparing" ? op
        : { ...op, phase: "completed", hasContent: event.hasContent ?? op.hasContent };
    case "cancelled":
      return { ...op, phase: "cancelled", restoredOriginal: event.restoredOriginal, hasContent: op.hasContent || Boolean(event.keep) };
    case "failed":
      return { ...op, phase: "failed", error: event.error };
    default:
      return op;
  }
}

/** What the operation's card should show. The renderer switches on this and
 *  ALWAYS rebuilds the card's children — loading affordances (dots, writing
 *  label) exist only in the "loading" view, so no terminal card can carry
 *  them. */
export type CardView =
  | { kind: "remove" }
  /** `label` is the live step; `done` are the steps already completed. */
  | { kind: "loading"; label: string; done: string[] }
  | { kind: "stream-text" }
  | { kind: "result" }
  | { kind: "result-stopped" }
  | { kind: "error"; message: string; retryable: boolean };

/** The trail shown above the live step: every substage except the current one,
 *  with the default label leading when the op has not reported a stage yet. */
export function loadingSteps(op: GenerationOp, defaultLabel: string): { label: string; done: string[] } {
  const label = op.note ?? defaultLabel;
  const done = op.steps.slice(0, Math.max(0, op.steps.length - 1));
  return { label, done: op.note && op.steps.length ? [defaultLabel, ...done] : done };
}

export function cardViewFor(op: GenerationOp, defaultLabel: string): CardView {
  switch (op.phase) {
    case "preparing":
    case "generating":
    case "typing":
      return op.kind === "chat" && op.hasContent
        ? { kind: "stream-text" }
        : { kind: "loading", ...loadingSteps(op, defaultLabel) };
    case "completed":
      return { kind: "result" };
    case "cancelled":
      // A cancelled card with nothing useful to show is noise — remove it.
      // Chat keeps its partially streamed text with a quiet "Stopped." note.
      return op.hasContent ? { kind: "result-stopped" } : { kind: "remove" };
    case "failed":
      // Every path that fails an op now resolves a reason first
      // (`assistFailureReason`, or the transport's own line), so this is a net
      // rather than a message anyone should see. It still names the state
      // instead of a reasonless "could not finish".
      return { kind: "error", message: op.error || "Starling stopped without reporting a reason. Try again.", retryable: true };
  }
}

/** Pruning policy for a fresh user turn: sweep orphans (card with no op) and
 *  cancelled leftovers; keep completed results and failed cards (their error
 *  + Retry stay useful until the user acts on them). */
export function shouldPruneOnNewTurn(op: GenerationOp | undefined): boolean {
  return op === undefined || op.phase === "cancelled";
}
