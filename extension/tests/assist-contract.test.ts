import { describe, expect, it } from "vitest";
import { assistFailureReason, invalidAssistResponse, isAssistResponse, isBrowserAssistRequest } from "../shared/assist-contract";

describe("browser assist contract", () => {
  it("accepts only actionable chat, composer, and scheduling requests", () => {
    expect(isBrowserAssistRequest({ mode: "chat", message: "Draft a post" })).toBe(true);
    expect(isBrowserAssistRequest({ mode: "enhance_composer", composer: { action: "reply", draft_text: "", intent: "create" } })).toBe(true);
    expect(isBrowserAssistRequest({ mode: "schedule", schedule: { op: "mark_posted", draft_id: 12 } })).toBe(true);
    expect(isBrowserAssistRequest({ mode: "schedule", schedule: { op: "save", text: "Draft", target_at: "2026-07-23T15:00:00Z" } })).toBe(true);
  });

  it("rejects incomplete or unsafe requests before they reach the host", () => {
    expect(isBrowserAssistRequest({ mode: "chat", message: "   " })).toBe(false);
    expect(isBrowserAssistRequest({ mode: "enhance_composer", composer: { action: "reply", draft_text: "" } })).toBe(false);
    expect(isBrowserAssistRequest({ mode: "schedule", schedule: { op: "save", text: "Draft" } })).toBe(false);
    expect(isBrowserAssistRequest({ mode: "schedule", schedule: { op: "dismiss", draft_id: 0 } })).toBe(false);
    expect(isBrowserAssistRequest({ mode: "schedule", schedule: { op: "unknown" } })).toBe(false);
  });

  it("requires a real success/failure marker on every response", () => {
    expect(isAssistResponse({ ok: true, answer: "Ready" })).toBe(true);
    expect(isAssistResponse({ ok: false, error: "No model" })).toBe(true);
    expect(isAssistResponse({ answer: "Missing status" })).toBe(false);
    expect(invalidAssistResponse()).toEqual({
      ok: false,
      error: "Starling couldn't read its own answer. Nothing in X changed.",
    });
  });

  it("holds chat messages to the hosted 2,000-character limit", () => {
    expect(isBrowserAssistRequest({ mode: "chat", message: "x".repeat(2_000) })).toBe(true);
    expect(isBrowserAssistRequest({ mode: "chat", message: "x".repeat(2_001) })).toBe(false);
  });
});

describe("why a draft did not arrive", () => {
  // The companion answers a withheld draft with ok:true and the reason in
  // `decision.rationale`; `error` is only set when the REQUEST failed. Reading
  // `error` alone is what rendered all four causes below as one blank sentence.
  const withheld = (rationale: string) => ({
    ok: true,
    decision: { action: "post", rationale, reasons: [], likely_reaction: "", draft: "", native_intent: undefined },
  }) as any;

  it("hands back the companion's own reason instead of a generic failure", () => {
    // The four things the companion actually distinguishes, all of which used
    // to arrive as "Starling could not finish this draft."
    const outOfClock = "The model is running but it answered too slowly to finish this draft.";
    const gatesRejected = "I drafted a few but none passed my own checks. Try again, or tell me the angle you want.";
    const nothingBack = "Nothing came back from the model this time.";
    const duplicate = "I drafted one, but it came out too close to a reply I already showed you this session.";
    for (const reason of [outOfClock, gatesRejected, nothingBack, duplicate]) {
      expect(assistFailureReason(withheld(reason), "post")).toBe(reason);
    }
  });

  it("never renders the reasonless sentence when the companion explained itself", () => {
    const reason = assistFailureReason(withheld("The model answered too slowly."), "post");
    expect(reason).not.toMatch(/could not finish/i);
    expect(reason.trim()).not.toBe("");
  });

  it("prefers an explicit request error over a rationale", () => {
    const response = { ok: false, error: "No model", decision: { rationale: "unused" } } as any;
    expect(assistFailureReason(response, "post")).toBe("No model");
  });

  it("does not quote a rationale that describes a draft for another surface", () => {
    // The composer moved between the ask and the answer. The rationale here is
    // a SUCCESS note about a draft that exists, so repeating it over an empty
    // reply box would describe something the reader cannot see.
    const mismatched = {
      ok: true,
      decision: { rationale: "Written from scratch for this composer.", draft: "A real post.", native_intent: "post" },
    } as any;
    const reason = assistFailureReason(mismatched, "reply");
    expect(reason).toContain("post");
    expect(reason).toContain("reply");
    expect(reason).not.toContain("Written from scratch");
  });

  it("says so plainly when the companion explained nothing at all", () => {
    // ok, no draft, no rationale, no error is a shape the companion should not
    // be able to produce — so name that, rather than blaming the draft.
    const reason = assistFailureReason({ ok: true, decision: { draft: "" } } as any, "post");
    expect(reason).toBe("Starling came back without a draft. Try again.");
    expect(assistFailureReason(undefined, "post")).toMatch(/without a draft/i);
  });
});

describe("no failure can reach the reader without a cause (regression guard)", () => {
  // The original bug was not a missing message — it was a message that named
  // no cause, produced by reading a field the companion does not set on the
  // response it actually sends. These two tests exist so that specific shape
  // of mistake fails here rather than in front of him.

  it("always returns something a reader can act on, for every response shape", () => {
    const shapes: Array<[string, unknown]> = [
      ["undefined", undefined],
      ["empty object", {}],
      ["ok, nothing else", { ok: true }],
      ["ok with empty decision", { ok: true, decision: {} }],
      ["ok, blank draft, blank rationale", { ok: true, decision: { draft: "", rationale: "" } }],
      ["ok, whitespace rationale", { ok: true, decision: { draft: "", rationale: "   " } }],
      ["not ok, no error", { ok: false }],
      ["not ok, whitespace error", { ok: false, error: "  " }],
      ["not ok, error and rationale", { ok: false, error: "Starling is unavailable", decision: { rationale: "r" } }],
      ["draft for another surface", { ok: true, decision: { draft: "x", native_intent: "post", rationale: "ok" } }],
      ["null decision", { ok: true, decision: null }],
    ];
    for (const [name, response] of shapes) {
      for (const expected of ["post", "reply", "quote", undefined] as const) {
        const reason = assistFailureReason(response as any, expected);
        expect(reason.trim(), `${name} / ${expected}`).not.toBe("");
        // The exact sentence this whole fix exists to delete, and its siblings.
        expect(reason, `${name} / ${expected}`).not.toMatch(/could not finish/i);
        // A cause names something. One bare word never did.
        //
        // Applied only to text WE generate: when the companion set `error`
        // explicitly that string is authoritative and goes through verbatim,
        // however terse — paraphrasing or padding the server's own wording is
        // how the four distinct causes collapsed into one in the first place.
        const verbatim = (response as { error?: string } | undefined)?.error?.trim();
        if (reason !== verbatim) {
          expect(reason.trim().split(/\s+/).length, `${name} / ${expected}`).toBeGreaterThan(3);
        }
      }
    }
  });

  it("keeps the companion's wording verbatim rather than paraphrasing it", () => {
    // If this ever starts rewriting the reason, the four causes the companion
    // distinguishes collapse back into one and the bug returns wearing new
    // words. The reason must survive the trip unchanged.
    const exact = "The model is running but it answered too slowly to finish this draft. "
      + "That usually means something else on this computer is using it — check what "
      + "else is running, then try again.";
    expect(assistFailureReason({ ok: true, decision: { draft: "", rationale: exact } } as any, "post")).toBe(exact);
  });
});
