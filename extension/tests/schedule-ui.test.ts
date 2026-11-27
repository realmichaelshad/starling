import { describe, expect, it } from "vitest";
import {
  CLICKABLE_TESTIDS, FORBIDDEN_TESTIDS, defaultPickerTime, mayClick, planSchedule,
  readManualTime, scheduleReadyLine, toPickerValue,
} from "../shared/schedule-ui";

const NOW = new Date("2026-07-24T12:00:00");

describe("driving X's schedule dialog without ever publishing", () => {
  it("never permits a publish control", () => {
    for (const id of FORBIDDEN_TESTIDS) expect(mayClick(id)).toBe(false);
    for (const id of CLICKABLE_TESTIDS) expect(mayClick(id)).toBe(true);
    expect(mayClick("anythingElse")).toBe(false);
    // The commit buttons must never appear in the allowed list.
    expect(CLICKABLE_TESTIDS.some(id => FORBIDDEN_TESTIDS.includes(id as never))).toBe(false);
  });

  it("maps a local time onto X's dropdown values", () => {
    const plan = planSchedule("2026-07-25T18:30:00", NOW);
    expect(plan.refused).toBeUndefined();
    expect(plan.fields).toEqual({
      month: "7", day: "25", year: "2026", hour: "6", minute: "30", meridiem: "PM",
    });
  });

  it("handles noon and midnight, where 12-hour clocks break", () => {
    expect(planSchedule("2026-07-25T12:00:00", NOW).fields).toMatchObject(
      { hour: "12", meridiem: "PM" });
    expect(planSchedule("2026-07-25T00:15:00", NOW).fields).toMatchObject(
      { hour: "12", minute: "15", meridiem: "AM" });
  });

  it("refuses what X itself would refuse", () => {
    expect(planSchedule("2026-07-24T12:01:00", NOW).refused).toContain("few minutes");
    expect(planSchedule("2026-07-23T12:00:00", NOW).refused).toContain("few minutes");
    expect(planSchedule("2040-01-01T10:00:00", NOW).refused).toContain("too far ahead");
    expect(planSchedule("not a date", NOW).refused).toContain("not a valid time");
  });

  it("tells the user the last click is theirs", () => {
    expect(scheduleReadyLine("tomorrow 6 PM", 1)).toContain("post is written and scheduled");
    expect(scheduleReadyLine("tomorrow 6 PM", 3)).toContain("3-part thread");
    expect(scheduleReadyLine(undefined, 1)).toContain("I never post for you");
  });
});

describe("the time the user picks themselves", () => {
  it("reads a local wall time as the instant the user meant", () => {
    const picked = readManualTime("2026-07-25T18:30", NOW);
    expect(picked.error).toBeUndefined();
    expect(new Date(picked.iso!).getTime()).toBe(new Date("2026-07-25T18:30:00").getTime());
    expect(picked.label).toBe("Sat, Jul 25 at 6:30 PM");
  });

  it("refuses a time that cannot be scheduled, before anything is committed", () => {
    expect(readManualTime("", NOW).error).toContain("Pick a date");
    expect(readManualTime("nonsense", NOW).error).toContain("not a valid");
    expect(readManualTime("2026-07-24T12:01", NOW).error).toContain("at least 5 minutes");
    expect(readManualTime("2026-07-23T12:00", NOW).error).toContain("at least 5 minutes");
    expect(readManualTime("2040-01-01T10:00", NOW).error).toContain("too far ahead");
  });

  it("round-trips through the picker's own value format without shifting the clock", () => {
    // toISOString() here would hand the input a UTC hour and silently pre-fill
    // the wrong time for anyone not on UTC.
    const when = new Date("2026-07-25T06:05:00");
    expect(toPickerValue(when)).toBe("2026-07-25T06:05");
    expect(readManualTime(toPickerValue(when), NOW).iso).toBe(when.toISOString());
  });

  it("opens on the recommended time only when there is one to stand behind", () => {
    const target = new Date("2026-07-25T18:30:00");
    expect(defaultPickerTime(target.toISOString(), NOW).getTime()).toBe(target.getTime());
    // No recommendation, or a stale one, falls back to the next clean hour —
    // a starting point, not a suggestion.
    expect(defaultPickerTime(null, NOW).getTime()).toBe(new Date("2026-07-24T13:00:00").getTime());
    expect(defaultPickerTime("2026-07-23T09:00:00", NOW).getTime())
      .toBe(new Date("2026-07-24T13:00:00").getTime());
  });
});
