import { describe, expect, it } from "vitest";
import { isScheduleMutationConfirmed, isScheduleSaveConfirmed } from "../shared/schedule-receipt";

describe("scheduled-draft mutation receipt", () => {
  it("accepts only the exact successful local transition", () => {
    expect(isScheduleMutationConfirmed({ ok: true, id: 42, status: "posted" }, 42, "posted")).toBe(true);
    expect(isScheduleMutationConfirmed({ ok: true, id: "42", status: "dismissed" }, 42, "dismissed")).toBe(true);
  });

  it("rejects malformed, failed, stale, and cross-draft responses", () => {
    expect(isScheduleMutationConfirmed(undefined, 42, "posted")).toBe(false);
    expect(isScheduleMutationConfirmed({ ok: false, id: 42, status: "posted" }, 42, "posted")).toBe(false);
    expect(isScheduleMutationConfirmed({ ok: true, id: 41, status: "posted" }, 42, "posted")).toBe(false);
    expect(isScheduleMutationConfirmed({ ok: true, id: 42, status: "dismissed" }, 42, "posted")).toBe(false);
  });
});

describe("scheduled-draft save receipt", () => {
  const exact = {
    ok: true,
    scheduled: {
      id: 7,
      status: "scheduled",
      action: "post",
      text: "Held draft",
      target_at: "2026-07-23T18:00:00+00:00",
      target_label: "Thursday 2 PM ET",
    },
  };

  it("accepts only the exact local draft and instant", () => {
    expect(isScheduleSaveConfirmed(exact, "Held draft", "2026-07-23T14:00:00-04:00", "Thursday 2 PM ET")).toBe(true);
  });

  it("rejects an id-only, stale, or cross-draft acknowledgement", () => {
    expect(isScheduleSaveConfirmed({ ok: true, scheduled: { id: 7 } }, "Held draft", "2026-07-23T14:00:00-04:00")).toBe(false);
    expect(isScheduleSaveConfirmed({ ...exact, scheduled: { ...exact.scheduled, text: "Other draft" } }, "Held draft", "2026-07-23T14:00:00-04:00")).toBe(false);
    expect(isScheduleSaveConfirmed({ ...exact, scheduled: { ...exact.scheduled, target_at: "2026-07-23T19:00:00+00:00" } }, "Held draft", "2026-07-23T14:00:00-04:00")).toBe(false);
  });
});
