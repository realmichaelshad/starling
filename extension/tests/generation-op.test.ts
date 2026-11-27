import { describe, expect, it } from "vitest";
import {
  cardViewFor,
  createOp,
  isTerminal,
  shouldPruneOnNewTurn,
  transitionOp,
  type GenerationOp,
  type OpEvent,
} from "../shared/generation-op";

function op(overrides: Partial<GenerationOp> = {}): GenerationOp {
  return { ...createOp("enhance", "generating", 1_000), ...overrides };
}

describe("operation lifecycle transitions", () => {
  it("follows preparing → generating → typing → completed", () => {
    let current = op({ phase: "preparing" });
    current = transitionOp(current, { type: "target-opened" });
    expect(current.phase).toBe("generating");
    current = transitionOp(current, { type: "typing-started" });
    expect(current.phase).toBe("typing");
    current = transitionOp(current, { type: "completed", hasContent: true });
    expect(current.phase).toBe("completed");
    expect(current.hasContent).toBe(true);
  });

  it("cancel and fail apply from every non-terminal phase", () => {
    for (const phase of ["preparing", "generating", "typing"] as const) {
      expect(transitionOp(op({ phase }), { type: "cancelled" }).phase).toBe("cancelled");
      expect(transitionOp(op({ phase }), { type: "failed", error: "boom" }).phase).toBe("failed");
    }
  });

  it("terminal phases absorb EVERY event by returning the same reference", () => {
    const events: OpEvent[] = [
      { type: "target-opened" },
      { type: "typing-started" },
      { type: "tokens" },
      { type: "heartbeat", note: "Still working. Almost there…" },
      { type: "completed" },
      { type: "cancelled" },
      { type: "failed", error: "late" },
    ];
    for (const phase of ["completed", "cancelled", "failed"] as const) {
      const terminal = op({ phase });
      for (const event of events) {
        // Identity, not just equality: applyOpEvent uses the reference check
        // to skip rendering, so a leaked timer can never repaint the card.
        expect(transitionOp(terminal, event)).toBe(terminal);
      }
    }
  });

  it("heartbeats update the note only while non-terminal", () => {
    const beating = transitionOp(op(), { type: "heartbeat", note: "Still writing…" });
    expect(beating.note).toBe("Still writing…");
    const cancelled = transitionOp(beating, { type: "cancelled" });
    expect(transitionOp(cancelled, { type: "heartbeat", note: "zombie" })).toBe(cancelled);
  });

  it("out-of-order phase events are identity transitions", () => {
    const generating = op();
    expect(transitionOp(generating, { type: "target-opened" })).toBe(generating);
    const preparing = op({ phase: "preparing" });
    expect(transitionOp(preparing, { type: "typing-started" })).toBe(preparing);
    // A preparing op has no in-flight request; "completed" cannot apply to it.
    expect(transitionOp(preparing, { type: "completed" })).toBe(preparing);
  });

  it("tokens flip hasContent exactly once, during generating", () => {
    const first = transitionOp(op(), { type: "tokens" });
    expect(first.hasContent).toBe(true);
    expect(transitionOp(first, { type: "tokens" })).toBe(first);
    const preparing = op({ phase: "preparing" });
    expect(transitionOp(preparing, { type: "tokens" })).toBe(preparing);
  });

  it("a cancel can keep its card to say what stopping cost", () => {
    const stopped = transitionOp(op(), { type: "cancelled", keep: true });
    expect(stopped.phase).toBe("cancelled");
    expect(cardViewFor(stopped, "…")).toEqual({ kind: "result-stopped" });
    // Without `keep`, an empty cancelled card still goes away.
    expect(cardViewFor(transitionOp(op(), { type: "cancelled" }), "…")).toEqual({ kind: "remove" });
  });

  it("cancel records whether the original draft was restored", () => {
    const restored = transitionOp(op({ phase: "typing" }), { type: "cancelled", restoredOriginal: true });
    expect(restored.restoredOriginal).toBe(true);
  });

  it("isTerminal matches the three absorbing phases", () => {
    expect(isTerminal(op())).toBe(false);
    expect(isTerminal(op({ phase: "preparing" }))).toBe(false);
    expect(isTerminal(op({ phase: "completed" }))).toBe(true);
    expect(isTerminal(op({ phase: "cancelled" }))).toBe(true);
    expect(isTerminal(op({ phase: "failed" }))).toBe(true);
  });

  it("createOp issues unique ids", () => {
    expect(createOp("chat").id).not.toBe(createOp("chat").id);
  });
});

describe("card render rules", () => {
  it("non-terminal phases show the loading view with heartbeat note override", () => {
    expect(cardViewFor(op({ phase: "preparing" }), "Opening your reply…"))
      .toEqual({ kind: "loading", label: "Opening your reply…", done: [] });
    expect(cardViewFor(op({ note: "Still writing…", steps: ["Still writing…"] }), "Writing your post"))
      .toEqual({ kind: "loading", label: "Still writing…", done: ["Writing your post"] });
  });

  // A single label that silently swapped every 30 seconds told the user
  // nothing about what happened in between; the card keeps the whole trail
  // with the current step live at the bottom.
  it("keeps every substage as a trail, current step last", () => {
    let current = op();
    for (const note of ["Reading this post…", "Sending to Starling…", "Typing it into X…"]) {
      current = transitionOp(current, { type: "heartbeat", note });
    }
    expect(current.steps).toEqual(["Reading this post…", "Sending to Starling…", "Typing it into X…"]);
    expect(cardViewFor(current, "Starting…")).toEqual({
      kind: "loading",
      label: "Typing it into X…",
      done: ["Starting…", "Reading this post…", "Sending to Starling…"],
    });
  });

  it("a repeated heartbeat is absorbed — no duplicate step, no repaint", () => {
    const beating = transitionOp(op(), { type: "heartbeat", note: "Still writing…" });
    expect(transitionOp(beating, { type: "heartbeat", note: "Still writing…" })).toBe(beating);
    expect(beating.steps).toEqual(["Still writing…"]);
  });

  it("chat streams live text once tokens arrive, dots before that", () => {
    const chat = createOp("chat", "generating", 1_000);
    expect(cardViewFor(chat, "…").kind).toBe("loading");
    expect(cardViewFor(transitionOp(chat, { type: "tokens" }), "…").kind).toBe("stream-text");
  });

  it("cancelled with no content removes the card; with content shows result-stopped", () => {
    expect(cardViewFor(op({ phase: "cancelled" }), "…")).toEqual({ kind: "remove" });
    expect(cardViewFor(op({ phase: "cancelled", hasContent: true }), "…"))
      .toEqual({ kind: "result-stopped" });
  });

  it("completed renders the result; failed renders a retryable error", () => {
    expect(cardViewFor(op({ phase: "completed" }), "…")).toEqual({ kind: "result" });
    expect(cardViewFor(op({ phase: "failed", error: "helper unavailable" }), "…"))
      .toEqual({ kind: "error", message: "helper unavailable", retryable: true });
    expect(cardViewFor(op({ phase: "failed" }), "…"))
      .toEqual({ kind: "error", message: "Starling stopped without reporting a reason. Try again.", retryable: true });
  });
});

describe("new-turn pruning policy", () => {
  it("sweeps orphaned cards and cancelled leftovers, keeps history", () => {
    expect(shouldPruneOnNewTurn(undefined)).toBe(true);
    expect(shouldPruneOnNewTurn(op({ phase: "cancelled" }))).toBe(true);
    expect(shouldPruneOnNewTurn(op({ phase: "completed" }))).toBe(false);
    expect(shouldPruneOnNewTurn(op({ phase: "failed" }))).toBe(false);
    expect(shouldPruneOnNewTurn(op())).toBe(false);
  });
});
