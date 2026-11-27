/**
 * Driving X's own "Schedule post" dialog from the composer.
 *
 * THE LINE, and it does not move: Starling may set the composer up completely
 * — text, thread parts, and the scheduled date/time — but the final publish
 * control ("Post" / "Schedule post") is pressed by the human, always. Opening
 * X's schedule dialog and choosing a time configures a draft; it publishes
 * nothing. The commit button is deliberately never selected by any code here.
 *
 * The DOM work lives in content.ts; the rules and the field maths live here so
 * they can be tested without a browser.
 */

/** Controls this module is allowed to click. Anything else is a bug. */
export const CLICKABLE_TESTIDS = Object.freeze([
  "scheduleOption",            // opens the schedule dialog
  "confirmationSheetConfirm",  // confirms the CHOSEN TIME, not the post
]);

/**
 * Controls that publish. Never clicked, and asserted against in tests so a
 * future edit cannot quietly add one.
 */
export const FORBIDDEN_TESTIDS = Object.freeze([
  "tweetButton", "tweetButtonInline", "scheduledConfirmationPrimaryAction",
]);

export interface ScheduleFields {
  month: string;   // "1".."12" — X's select uses unpadded month values
  day: string;     // "1".."31"
  year: string;    // "2026"
  hour: string;    // "1".."12"
  minute: string;  // "0".."59"
  meridiem: "AM" | "PM";
}

export interface SchedulePlan {
  fields?: ScheduleFields;
  /** Why we will not drive the dialog. */
  refused?: string;
}

/** X refuses schedules more than 18 months out; well inside that is sane. */
const MAX_DAYS_AHEAD = 540;

/**
 * Turn a target instant into the values X's dropdowns expect, in the USER's
 * local timezone (the dialog is local-time, the engine stores UTC — mixing
 * them up would schedule a post hours off, silently).
 */
export function planSchedule(targetIso: string, now = new Date()): SchedulePlan {
  const target = new Date(targetIso);
  if (Number.isNaN(target.getTime())) return { refused: "that is not a valid time" };
  const minutesAhead = (target.getTime() - now.getTime()) / 60_000;
  if (minutesAhead < 5) {
    // X itself rejects near-now schedules; catching it here means an honest
    // message instead of a dialog that silently will not confirm.
    return { refused: "X needs a schedule at least a few minutes out" };
  }
  if (minutesAhead / (60 * 24) > MAX_DAYS_AHEAD) {
    return { refused: "that is too far ahead for X to schedule" };
  }
  const hours24 = target.getHours();
  const hour12 = hours24 % 12 === 0 ? 12 : hours24 % 12;
  return {
    fields: {
      month: String(target.getMonth() + 1),
      day: String(target.getDate()),
      year: String(target.getFullYear()),
      hour: String(hour12),
      minute: String(target.getMinutes()),
      meridiem: hours24 >= 12 ? "PM" : "AM",
    },
  };
}

/** The user's own choice of time, before it becomes a schedule. */
export interface ManualTime {
  iso?: string;
  label?: string;
  /** Why this value cannot be scheduled, in words the card can show as-is. */
  error?: string;
}

/** Shortest lead X will accept; also what the picker's `min` is built from. */
export const MIN_LEAD_MINUTES = 5;

/**
 * Read what the user typed into the picker (`datetime-local`, i.e. local wall
 * time with no zone) and turn it into the exact UTC instant we would hold the
 * draft until. Refusing here — rather than at save time — is what lets the card
 * say "pick a time at least a few minutes out" instead of failing after the
 * user has already committed.
 */
export function readManualTime(value: string, now = new Date()): ManualTime {
  if (!value.trim()) return { error: "Pick a date and time first." };
  const chosen = new Date(value);
  if (Number.isNaN(chosen.getTime())) return { error: "That is not a valid date and time." };
  const minutesAhead = (chosen.getTime() - now.getTime()) / 60_000;
  if (minutesAhead < MIN_LEAD_MINUTES) {
    return { error: `Pick a time at least ${MIN_LEAD_MINUTES} minutes from now.` };
  }
  if (minutesAhead / (60 * 24) > MAX_DAYS_AHEAD) {
    return { error: "That is too far ahead to schedule." };
  }
  return { iso: chosen.toISOString(), label: formatScheduleLabel(chosen) };
}

/** "Wed, Jul 29 at 8:00 PM" — the user's own words for the time they picked. */
export function formatScheduleLabel(when: Date): string {
  const hour = when.getHours() % 12 || 12;
  const minute = String(when.getMinutes()).padStart(2, "0");
  const meridiem = when.getHours() >= 12 ? "PM" : "AM";
  const day = when.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
  return `${day} at ${hour}:${minute} ${meridiem}`;
}

/**
 * The value a `datetime-local` input wants: local wall time, no zone, to the
 * minute. Built by hand because toISOString() would shift the clock into UTC
 * and silently pre-fill the picker with the wrong hour.
 */
export function toPickerValue(when: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${when.getFullYear()}-${pad(when.getMonth() + 1)}-${pad(when.getDate())}`
    + `T${pad(when.getHours())}:${pad(when.getMinutes())}`;
}

/**
 * What the picker opens on. The engine's recommended target when there is
 * one worth standing behind; otherwise the next clean hour — a starting point
 * the user is expected to change, not a suggestion dressed up as one.
 */
export function defaultPickerTime(targetIso: string | null | undefined, now = new Date()): Date {
  if (targetIso) {
    const target = new Date(targetIso);
    if (!Number.isNaN(target.getTime()) && target.getTime() - now.getTime() > MIN_LEAD_MINUTES * 60_000) {
      return target;
    }
  }
  const next = new Date(now.getTime());
  next.setMinutes(0, 0, 0);
  next.setHours(next.getHours() + 1);
  return next;
}

/** True only for controls this module is permitted to click. */
export function mayClick(testId: string): boolean {
  return CLICKABLE_TESTIDS.includes(testId) && !FORBIDDEN_TESTIDS.includes(testId);
}

/** The line shown on the card once the composer is set up. */
export function scheduleReadyLine(label: string | undefined, parts: number): string {
  const what = parts >= 2 ? `${parts}-part thread` : "post";
  return `${what} is written and scheduled for ${label || "the chosen time"} in X. `
    + "Press X's own button to commit it — I never post for you.";
}
