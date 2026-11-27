import { describe, expect, it } from "vitest";
import { finalText, initialStreamState, markCancelled, newRequestId, reduceStream, stoppedStreamOutcome, streamStageLabel } from "../shared/stream";
import type { AssistPortEvent, AssistResponse } from "../shared/types";

const REQUEST = "req-1";

function run(events: AssistPortEvent[]) {
  return events.reduce(reduceStream, initialStreamState(REQUEST));
}

describe("assist stream state machine", () => {
  it("accumulates tokens in order and tracks phases", () => {
    const state = run([
      { type: "accepted", requestId: REQUEST },
      { type: "status", requestId: REQUEST, stage: "thinking" },
      { type: "token", requestId: REQUEST, text: "Sharp " },
      { type: "token", requestId: REQUEST, text: "point." },
    ]);
    expect(state.phase).toBe("streaming");
    expect(state.preview).toBe("Sharp point.");
    expect(state.stage).toBe("thinking");
  });

  it("final response wins and exposes the gated draft only", () => {
    const response: AssistResponse = { ok: true, decision: { action: "reply", rationale: "r", reasons: [], likely_reaction: "", confidence: "directional", draft: "Gated final." } };
    const state = run([
      { type: "token", requestId: REQUEST, text: "Preview that may differ" },
      { type: "final", requestId: REQUEST, response },
    ]);
    expect(state.phase).toBe("done");
    expect(finalText(state)).toBe("Gated final.");
  });

  it("never trusts preview text as final output", () => {
    const state = run([{ type: "token", requestId: REQUEST, text: "preview" }]);
    expect(finalText(state)).toBeUndefined();
  });

  it("terminal states ignore late events", () => {
    const failed = run([
      { type: "error", requestId: REQUEST, error: "boom", retryable: true },
      { type: "token", requestId: REQUEST, text: "late" },
    ]);
    expect(failed.phase).toBe("error");
    expect(failed.preview).toBe("");

    const done = run([
      { type: "final", requestId: REQUEST, response: { ok: true, answer: "hi" } },
      { type: "error", requestId: REQUEST, error: "late", retryable: true },
    ]);
    expect(done.phase).toBe("done");
  });

  it("ignores events for other requests", () => {
    const state = reduceStream(initialStreamState(REQUEST), { type: "token", requestId: "other", text: "x" });
    expect(state.preview).toBe("");
  });

  it("a failed final becomes a retryable error unless cancelled", () => {
    const failed = run([{ type: "final", requestId: REQUEST, response: { ok: false, error: "no model" } }]);
    expect(failed.phase).toBe("error");
    expect(failed.retryable).toBe(true);

    const cancelled = run([{ type: "final", requestId: REQUEST, response: { ok: false, error: "Stopped.", cancelled: true } }]);
    expect(cancelled.cancelled).toBe(true);
    expect(cancelled.retryable).toBe(false);
  });

  it("markCancelled is terminal and idempotent-safe", () => {
    const state = markCancelled(run([{ type: "token", requestId: REQUEST, text: "partial" }]));
    expect(state.phase).toBe("error");
    expect(state.cancelled).toBe(true);
    expect(markCancelled(state)).toEqual(state);
  });

  it("always gives a live request a clear, non-blank stage label", () => {
    expect(streamStageLabel("starting")).toBe("Sending to Starling…");
    expect(streamStageLabel("accepted")).toBe("Sending to Starling…");
    expect(streamStageLabel("queued")).toBe("Starling is busy. You're next…");
    expect(streamStageLabel("reconnecting")).toBe("Reconnecting to Starling…");
    expect(streamStageLabel("warming")).toBe("Warming up. The first draft takes a few extra seconds…");
    expect(streamStageLabel("writing")).toBe("Writing…");
    expect(streamStageLabel("thinking")).toBe("Thinking through that…");
    expect(streamStageLabel("")).toBe("Working…");
    expect(streamStageLabel("   ")).toBe("Working…");
  });

  it("carries the error code, Retry-After and support reference into the state", () => {
    const failed = run([
      { type: "token", requestId: REQUEST, text: "shown" },
      { type: "error", requestId: REQUEST, error: "Slow down", retryable: true, code: "rate_limited", retryAfterS: 30, requestRef: "req_1" },
    ]);
    expect(failed).toMatchObject({ phase: "error", code: "rate_limited", retryAfterS: 30, requestRef: "req_1", exposed: true, cancelled: false });
    const stopped = run([{ type: "error", requestId: REQUEST, error: "Stopped.", retryable: true, code: "cancelled", exposed: false }]);
    expect(stopped).toMatchObject({ cancelled: true, exposed: false });
  });

  // The server narrates its own substages in plain English ("trying another
  // angle (2/3)"). Collapsing those into a generic "Working…" threw away the
  // only thing on the card that said what was actually happening.
  it("passes an unrecognised server stage through instead of flattening it", () => {
    expect(streamStageLabel("trying another angle (2/3)")).toBe("Trying another angle (2/3)…");
    expect(streamStageLabel("checking sources.")).toBe("Checking sources.");
    // A stage name this build does not know yet reads as words, not as a code.
    expect(streamStageLabel("drafting_thread")).toBe("Drafting thread…");
  });

  it("names each stage the engine sends while it does that step", () => {
    expect(streamStageLabel("reading_links")).toBe("Reading the links…");
    expect(streamStageLabel("checking_web")).toBe("Checking the web…");
    expect(streamStageLabel("finding_topics")).toBe("Finding topics…");
    expect(streamStageLabel("writing")).toBe("Writing…");
    expect(streamStageLabel("checking")).toBe("Checking the draft…");
    // No longer sent; an older engine still reads sensibly.
    expect(streamStageLabel("researching")).toBe("Checking the context…");
  });

  it("reads what a stopped stream cost from the server's answer to the stop", () => {
    const final = (response: AssistResponse): AssistPortEvent => ({ type: "final", requestId: REQUEST, response });
    expect(stoppedStreamOutcome(final({ ok: false, cancelled: true, used_after_stop: true }))).toBe("used_after_stop");
    expect(stoppedStreamOutcome(final({ ok: false, cancelled: true }))).toBe("nothing");
    expect(stoppedStreamOutcome(final({ ok: true, answer: "Here is the whole answer." }))).toBe("finished");
    expect(stoppedStreamOutcome(final({ ok: true, withheld: true, answer: "Nothing passed the checks." }))).toBe("nothing");
    expect(stoppedStreamOutcome(final({ ok: true, unchanged: true, decision: { action: "post", rationale: "", reasons: [], likely_reaction: "", confidence: "limited", draft: "his own words" } }))).toBe("nothing");
    expect(stoppedStreamOutcome(final({ ok: true, answer: "   " }))).toBe("nothing");
    expect(stoppedStreamOutcome({ type: "error", requestId: REQUEST, error: "Starling's AI isn't responding.", retryable: true, code: "ai_unavailable" })).toBe("nothing");
    // A late word or status settles nothing.
    expect(stoppedStreamOutcome({ type: "token", requestId: REQUEST, text: "late" })).toBeNull();
    expect(stoppedStreamOutcome({ type: "status", requestId: REQUEST, stage: "checking" })).toBeNull();
  });

  it("request ids are unique", () => {
    expect(newRequestId()).not.toBe(newRequestId());
  });
});
