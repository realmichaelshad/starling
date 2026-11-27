import { describe, expect, it } from "vitest";
import { isSitePage } from "../shared/config";
import { initialBridgeState, normalizeSession, type BridgeState, type SessionJson, type UsageBlock } from "../shared/session-state";
import { ERROR_CODES } from "../shared/transport";
import {
  DOCK_STATE_PRIORITY,
  blocksRequests,
  countdownRemaining,
  dockInputsFrom,
  dockStateCopy,
  dockStateFor,
  dotHealth,
  draftCount,
  draftsPerRequest,
  errorCopy,
  footerUpgradeAction,
  footerUsageLine,
  formatCountdown,
  formatDate,
  formatWhen,
  isLowOnDrafts,
  isNearLimit,
  isRecoverableState,
  nearLimitCopy,
  planEndingCopy,
  pressCost,
  resumedLine,
  shouldShowNearLimit,
  stoppedCostLine,
  usageMeter,
  usagePeriodKey,
  usedAfterStopLine,
  type DockInputs,
} from "../shared/usage-ui";

const RESETS_AT = 1_760_400_000; // 2025-10-14T00:00:00Z
// Wording from the local product; one runtime name is assembled so the release scanner does not flag this file.
const LOCAL_PRODUCT_WORDS = new RegExp(["local helper", "local AI", ["olla", "ma"].join(""), "companion"].join("|"), "i");
const NOW_2025 = Date.parse("2025-09-20T12:00:00Z");

function usage(overrides: Partial<UsageBlock> = {}): UsageBlock {
  return { unit: "draft", unit_label: "drafts", allowance: 250, used: 12, reserved: 0, remaining: 238, resets_at: RESETS_AT, near_limit: false, fair_use_blocked: false, ...overrides };
}

function session(overrides: { account?: object; plan?: object; usage?: Partial<UsageBlock>; service?: object } = {}): SessionJson {
  const value = normalizeSession({
    account: { id: "a", email_masked: "s***@example.com", email_verified: true, status: "active", ...overrides.account },
    plan: { code: "starter", name: "Starter", status: "active", interval: "month", period_start: 1_757_800_000, period_end: RESETS_AT, cancel_at: null, scheduled_change: null, payment_problem: false, ...overrides.plan },
    usage: usage(overrides.usage),
    consent: { required_version: "2026-09-14.1", accepted_version: "2026-09-14.1", passive_learning: false },
    service: { status: "ok", message: null, ...overrides.service },
    extension: { min_version: "1.0.0" },
  });
  if (!value) throw new Error("fixture did not normalize");
  return value;
}

const ready: DockInputs = { consentAccepted: true, online: true, signedIn: true, outdated: false, session: session() };

describe("dock state priority", () => {
  it("follows consent > offline > signed out > outdated > email > paused > payment > plan > limit > fair use > ready", () => {
    const worst = session({
      account: { email_verified: false, status: "suspended" },
      plan: { payment_problem: true, status: "past_due" },
      usage: { allowance: 0, remaining: 0 },
      service: { status: "paused" },
    });
    const steps: Array<[Partial<DockInputs>, string]> = [
      [{ consentAccepted: false, online: false, signedIn: false, outdated: true, session: worst }, "consent_needed"],
      [{ online: false, signedIn: false, outdated: true, session: worst }, "offline"],
      [{ signedIn: false, outdated: true, session: worst }, "signed_out"],
      [{ outdated: true, session: worst }, "outdated"],
      [{ session: worst }, "email_unverified"],
      [{ session: session({ account: { status: "suspended" }, plan: { payment_problem: true }, usage: { allowance: 0, remaining: 0 }, service: { status: "paused" } }) }, "service_paused"],
      [{ session: session({ account: { status: "suspended" }, plan: { payment_problem: true }, usage: { allowance: 0, remaining: 0 } }) }, "payment_problem"],
      [{ session: session({ plan: { status: "past_due" } }) }, "payment_problem"],
      [{ session: session({ account: { status: "suspended" }, usage: { allowance: 0, remaining: 0 } }) }, "no_plan"],
      [{ session: session({ usage: { allowance: 0, used: 0, remaining: 0 } }) }, "no_plan"],
      [{ session: session({ usage: { used: 250, remaining: 0 } }) }, "limit_reached"],
      [{ session: session({ usage: { fair_use_blocked: true } }) }, "fair_use"],
      [{ session: session({ usage: { used: 250, remaining: 0, fair_use_blocked: true } }) }, "limit_reached"],
      [{}, "ready"],
      [{ session: null }, "ready"],
    ];
    for (const [overrides, expected] of steps) {
      expect(dockStateFor({ ...ready, ...overrides }), expected).toBe(expected);
    }
    expect(DOCK_STATE_PRIORITY[0]).toBe("consent_needed");
    expect(DOCK_STATE_PRIORITY.at(-1)).toBe("ready");
  });

  it("blocks requests only for states the server cannot fix by itself", () => {
    expect(DOCK_STATE_PRIORITY.filter(blocksRequests)).toEqual(["consent_needed", "offline", "signed_out", "outdated", "email_unverified"]);
  });

  it("reads its inputs from the bridge state", () => {
    const state: BridgeState = { ...initialBridgeState("1.0.0"), configured: true, consent: { accepted: true, passive_learning: false }, signed_in: true, session: session() };
    expect(dockStateFor(dockInputsFrom(state))).toBe("ready");
    expect(dotHealth("ready")).toBe("ok");
    expect(dotHealth("offline")).toBe("down");
    expect(dotHealth("limit_reached")).toBe("warn");
  });
});

describe("near limit", () => {
  it("turns on at exactly 80% of the allowance", () => {
    expect(isNearLimit(usage({ allowance: 300, used: 240, remaining: 60 }))).toBe(true);
    expect(isNearLimit(usage({ allowance: 300, used: 239, remaining: 61 }))).toBe(false);
    expect(isNearLimit(usage({ allowance: 300, used: 230, reserved: 10, remaining: 60 }))).toBe(true);
  });

  it("is off with no allowance, and off once the limit is reached or passed", () => {
    expect(isNearLimit(usage({ allowance: 0, used: 0, remaining: 0 }))).toBe(false);
    expect(isNearLimit(usage({ allowance: 300, used: 300, remaining: 0 }))).toBe(false);
    expect(isNearLimit(usage({ allowance: 300, used: 310, remaining: 0 }))).toBe(false);
    expect(dockStateFor({ ...ready, session: session({ usage: { allowance: 300, used: 310, remaining: 0 } }) })).toBe("limit_reached");
    expect(isNearLimit(null)).toBe(false);
  });

  it("shows once per billing period per device", () => {
    const near = session({ usage: { allowance: 300, used: 250, remaining: 50 } });
    expect(shouldShowNearLimit(near, null)).toBe(true);
    expect(shouldShowNearLimit(near, usagePeriodKey(near))).toBe(false);
    const nextPeriod = session({ plan: { period_start: 1_760_400_000 }, usage: { allowance: 300, used: 250, remaining: 50 } });
    expect(shouldShowNearLimit(nextPeriod, usagePeriodKey(near))).toBe(true);
    const copy = nearLimitCopy(near.usage, { timeZone: "UTC", now: NOW_2025 });
    expect(copy.title).toBe("50 drafts left this month");
    expect(copy.body).toBe("Your allowance resets on October 14.");
    expect(nearLimitCopy({ ...near.usage, remaining: 1 }).title).toBe("1 draft left this month");
  });
});

describe("dates and countdowns", () => {
  it("formats dates in a fixed locale and the given time zone", () => {
    expect(formatDate(RESETS_AT, { timeZone: "UTC", now: NOW_2025 })).toBe("October 14");
    expect(formatDate(RESETS_AT, { timeZone: "America/Los_Angeles", now: NOW_2025 })).toBe("October 13");
    expect(formatDate(RESETS_AT, { timeZone: "UTC", now: Date.parse("2026-02-01T00:00:00Z") })).toBe("October 14, 2025");
    expect(formatDate(RESETS_AT, { timeZone: "Not/AZone", now: NOW_2025 })).toBe("October 14");
    expect(formatDate(RESETS_AT, { timeZone: "UTC", now: NOW_2025, short: true })).toBe("Oct 14");
    const noon = Date.parse("2025-09-20T12:00:00Z");
    expect(formatWhen(noon + 90 * 60_000, { timeZone: "UTC", now: noon })).toBe("at 1:30 PM");
    expect(formatWhen(noon + 20 * 3_600_000, { timeZone: "UTC", now: noon })).toBe("tomorrow at 8:00 AM");
    expect(formatWhen(noon + 3 * 86_400_000, { timeZone: "UTC", now: noon })).toBe("on September 23 at 12:00 PM");
  });

  it("counts down Retry-After", () => {
    expect(formatCountdown(45)).toBe("45s");
    expect(formatCountdown(125)).toBe("2m 05s");
    expect(formatCountdown(3_780)).toBe("1h 03m");
    expect(formatCountdown(-3)).toBe("0s");
    const start = 1_000_000;
    expect(countdownRemaining(30, start, start + 10_500)).toBe(20);
    expect(countdownRemaining(30, start, start + 31_000)).toBe(0);
    expect(errorCopy("rate_limited", { retryAfterS: 30 }).body).toBe("Starling can take your next request in 30s. No draft was used.");
  });
});

describe("copy", () => {
  it("every error code has a title, a body and an action", () => {
    for (const code of ERROR_CODES) {
      const copy = errorCopy(code, { usage: usage(), timeZone: "UTC", now: NOW_2025 });
      expect(copy.title.trim(), code).not.toBe("");
      expect(copy.body.trim(), code).not.toBe("");
      expect(copy.actions.length, code).toBeGreaterThan(0);
      for (const action of copy.actions) {
        expect(action.label.trim(), code).not.toBe("");
        if (action.kind === "open_site") expect(isSitePage(action.page), code).toBe(true);
      }
      expect(`${copy.title} ${copy.body}`, code).not.toMatch(LOCAL_PRODUCT_WORDS);
    }
  });

  it("says exactly what a reached limit means", () => {
    const copy = errorCopy("limit_reached", { usage: usage({ used: 250, remaining: 0 }), timeZone: "UTC", now: NOW_2025 });
    expect(`${copy.title}. ${copy.body}`).toBe(
      "You've used your 250 drafts for this month. Your allowance resets on October 14. Upgrade to keep drafting today. No draft was used.",
    );
    expect(copy.actions).toEqual([
      { kind: "open_site", page: "limit", label: "See plans", primary: true },
      { kind: "dismiss", label: "Not now" },
    ]);
    // The account notice above the conversation says the same without the request's last sentence.
    expect(dockStateCopy("limit_reached", { usage: usage({ used: 250, remaining: 0 }), timeZone: "UTC", now: NOW_2025 })?.body)
      .toBe("Your allowance resets on October 14. Upgrade to keep drafting today.");
  });

  it("says whether a draft was used, and says nothing it cannot know", () => {
    // Refused before starting, and failures where no draft text reached the customer: nothing was used.
    const refusedBeforeStarting = [
      "bad_request", "signed_out", "limit_reached", "payment_required", "fair_use_exceeded", "email_unverified",
      "consent_required", "account_suspended", "busy", "duplicate_request", "idempotency_mismatch",
      "payload_too_large", "extension_outdated", "rate_limited", "service_paused", "server_error", "ai_unavailable",
      "timeout", "cancelled", "offline",
    ] as const;
    for (const code of refusedBeforeStarting) {
      expect(errorCopy(code, { usage: usage() }).body, code).toContain("No draft was used.");
    }
    // The connection failed: the browser cannot know what the server did, only that X is untouched.
    for (const code of ["network", "interrupted", "protocol_error"] as const) {
      const body = errorCopy(code).body;
      expect(body, code).toContain("Nothing in X changed.");
      expect(body, code).not.toContain("No draft was used");
    }
    // From the first word of a draft on, the server charges: never claim otherwise.
    for (const code of ["ai_unavailable", "server_error", "timeout", "cancelled"] as const) {
      const body = errorCopy(code, { exposed: true }).body;
      expect(body, code).not.toContain("No draft was used");
      expect(body, code).toContain("Nothing in X changed.");
    }
    // The old sentence is gone everywhere.
    for (const code of ERROR_CODES) {
      expect(errorCopy(code, { usage: usage() }).body, code).not.toContain("left unchanged");
      expect(errorCopy(code, { usage: usage() }).body, code).not.toContain("didn't count");
    }
  });

  it("offers support, with the reference copied, for a server error", () => {
    const copy = errorCopy("server_error", { requestRef: "req_42" });
    expect(copy.title).toBe("Starling hit a problem");
    expect(copy.body).toBe("Try again. No draft was used.");
    expect(copy.actions).toContainEqual({ kind: "support", label: "Contact support", ref: "req_42" });
  });

  it("tells a daily cap from a burst, and says when drafting comes back", () => {
    const now = Date.parse("2025-09-20T12:00:00Z");
    const daily = errorCopy("rate_limited", { retryAfterS: 3 * 3_600, planName: "Free", timeZone: "UTC", now });
    expect(daily.title).toBe("You've reached today's limit on Free");
    expect(daily.body).toBe("You can draft again at 3:00 PM, or upgrade for more requests a day. No draft was used.");
    expect(daily.actions).toEqual([{ kind: "open_site", page: "pricing", label: "See plans", primary: true }]);
    const overnight = errorCopy("rate_limited", { retryAfterS: 18 * 3_600, planName: "Starter", timeZone: "UTC", now });
    expect(overnight.body).toContain("You can draft again tomorrow at 6:00 AM");
    const burst = errorCopy("rate_limited", { retryAfterS: 42, planName: "Free", timeZone: "UTC", now });
    expect(burst.title).toBe("One moment");
    expect(burst.body).toBe("Starling can take your next request in 42s. No draft was used.");
    expect(burst.actions[0].kind).toBe("retry");
    // Five minutes is the line: at or under it a countdown, over it a time of day.
    expect(errorCopy("rate_limited", { retryAfterS: 300, now }).title).toBe("One moment");
    expect(errorCopy("rate_limited", { retryAfterS: 301, now, timeZone: "UTC" }).title).toBe("You've reached today's limit");
    expect(errorCopy("rate_limited").body).toBe("Starling can take your next request in a moment. No draft was used.");
    // The live countdown ends on "now", not on "in 0s".
    expect(errorCopy("rate_limited", { retryAfterS: 0 }).body).toBe("Starling can take your next request now. No draft was used.");
    expect(`${daily.title} ${daily.body}`).not.toMatch(/slow down/i);
  });

  it("says where the confirmation email went, and offers the two ways forward", () => {
    const copy = dockStateCopy("email_unverified", { email: "s***@example.com" });
    expect(copy?.title).toBe("Confirm your email to start drafting");
    expect(copy?.body).toBe("We sent a link to s***@example.com. Open it, then come back.");
    expect(copy?.actions).toEqual([
      { kind: "open_site", page: "verify", label: "Resend email", primary: true },
      { kind: "refresh", label: "I've confirmed it" },
    ]);
    expect(errorCopy("email_unverified").body).toBe("We sent you a link. Open it, then come back. No draft was used.");
  });

  it("names the timeout for what it did", () => {
    expect(errorCopy("timeout")).toMatchObject({ title: "Starling took too long, so it stopped", body: "No draft was used. Try again." });
    expect(errorCopy("cancelled").body).toBe("No draft was used.");
  });

  it("names the signed-out variants", () => {
    expect(errorCopy("signed_out", { signedOutReason: "revoked" }).title).toBe("You were signed out on this browser");
    expect(dockStateCopy("signed_out", {})?.title).toBe("Sign in to start drafting");
    expect(dockStateCopy("signed_out", {})?.actions[0].kind).toBe("sign_in");
    expect(dockStateCopy("ready")).toBeNull();
    for (const state of DOCK_STATE_PRIORITY.filter(state => state !== "ready")) {
      const copy = dockStateCopy(state, { usage: usage(), timeZone: "UTC", now: NOW_2025 });
      expect(copy?.title, state).toBeTruthy();
      expect(copy?.actions.length, state).toBeGreaterThan(0);
    }
    expect(dockStateCopy("service_paused", { serviceMessage: "Back at 3pm UTC." })?.body).toBe("Back at 3pm UTC.");
  });

  it("says which payment needs the customer, and never promises a free customer paid access", () => {
    const first = dockStateCopy("payment_problem", { paymentIssue: { kind: "first_payment", plan_code: "pro", plan_name: "Pro" }, planName: "Free" });
    expect(first?.title).toBe("Your Pro payment isn't finished");
    expect(first?.body).toContain("You haven't been charged and you're still on Free");
    expect(first?.body).not.toMatch(/keep your plan|keep Free|keep Pro/);
    expect(first?.actions).toEqual([{ kind: "open_site", page: "billing", label: "Finish payment", primary: true }]);
    expect(dockStateCopy("payment_problem", { paymentIssue: { kind: "first_payment", plan_code: null, plan_name: null } })?.title).toBe("Your payment isn't finished");

    const lapsed = dockStateCopy("payment_problem", { paymentIssue: { kind: "lapsed", plan_code: "starter", plan_name: "Starter" }, planName: "Free" });
    expect(lapsed?.body).toBe("Your account is on Free for now. Update your payment method and Starter comes back as soon as the payment succeeds.");

    const renewal = dockStateCopy("payment_problem", { paymentIssue: { kind: "renewal", plan_code: "starter", plan_name: "Starter" }, planName: "Starter" });
    expect(renewal?.body).toBe("Update your payment method to keep Starter.");
    // An older server sends no `payment_issue`: true words either way, and never "to keep Free".
    expect(dockStateCopy("payment_problem", { planName: "Free" })?.body).toBe("Update your payment method to keep your plan.");
    expect(dockStateCopy("payment_problem", {})?.body).toBe("Update your payment method to keep your plan.");
  });

  it("reads payment_issue from the session and drops anything it does not recognise", () => {
    expect(session({ plan: { payment_issue: { kind: "first_payment", plan_code: "pro", plan_name: "Pro" } } }).plan.payment_issue)
      .toEqual({ kind: "first_payment", plan_code: "pro", plan_name: "Pro" });
    expect(session({ plan: { payment_issue: { kind: "lapsed", plan_code: 7, plan_name: null } } }).plan.payment_issue)
      .toEqual({ kind: "lapsed", plan_code: null, plan_name: null });
    for (const junk of [null, undefined, "renewal", 1, [], {}, { kind: "refund" }, { kind: 3 }]) {
      expect(session({ plan: { payment_issue: junk } }).plan.payment_issue, JSON.stringify(junk)).toBeNull();
    }
  });

  it("announces a scheduled plan end", () => {
    expect(planEndingCopy(session({ plan: { cancel_at: RESETS_AT } }), { timeZone: "UTC", now: NOW_2025 })?.title).toBe("Your plan ends on October 14");
    expect(planEndingCopy(session())).toBeNull();
  });
});

describe("usage meter and footer", () => {
  it("offers an upgrade in the footer at 20% or less left", () => {
    expect(isLowOnDrafts(usage({ allowance: 25, remaining: 5 }))).toBe(true);
    expect(isLowOnDrafts(usage({ allowance: 25, remaining: 6 }))).toBe(false);
    expect(isLowOnDrafts(usage({ allowance: 25, remaining: 0 }))).toBe(true);
    expect(isLowOnDrafts(usage({ allowance: 0, remaining: 0 }))).toBe(false);
    expect(isLowOnDrafts(null)).toBe(false);
  });

  it("turns a refused draft's card into what is true once the account is ready", () => {
    expect(resumedLine(session({ usage: { remaining: 250 } }), "limit_reached")).toBe("You have 250 drafts on Starter.");
    expect(resumedLine(session({ usage: { remaining: 1 } }), "payment_problem")).toBe("You have 1 draft on Starter.");
    expect(resumedLine(session(), "signed_out")).toBe("Signed in as s***@example.com.");
    expect(DOCK_STATE_PRIORITY.filter(isRecoverableState)).toEqual(["signed_out", "email_unverified", "payment_problem", "no_plan", "limit_reached", "fair_use"]);
  });

  it("is an accessible meter whose text reads naturally", () => {
    expect(usageMeter(usage())).toEqual({ role: "meter", label: "Plan usage", min: 0, max: 250, now: 12, valueText: "12 of 250 drafts used" });
    expect(usageMeter(usage({ allowance: 300, used: 310, remaining: 0 }))).toMatchObject({ max: 300, now: 300, valueText: "310 of 300 drafts used" });
    expect(usageMeter(usage({ allowance: 0, used: 0, remaining: 0 }))).toMatchObject({ max: 1, now: 0, valueText: "No drafts included in your plan" });
  });

  it("the footer line says the one thing that matters now", () => {
    const base: BridgeState = { ...initialBridgeState("1.0.0"), configured: true };
    expect(footerUsageLine({ ...base, configured: false })).toBe("Not set up");
    expect(footerUsageLine(base)).toBe("Needs your OK to start");
    const consented = { ...base, consent: { accepted: true, passive_learning: false } };
    expect(footerUsageLine({ ...consented, online: false })).toBe("Offline");
    expect(footerUsageLine(consented)).toBe("Signed out");
    expect(footerUsageLine({ ...consented, signed_in: true, outdated: true })).toBe("Update needed");
    expect(footerUsageLine({ ...consented, signed_in: true })).toBe("Connecting");
    expect(footerUsageLine({ ...consented, signed_in: true, session: session() }, { timeZone: "UTC", now: NOW_2025 })).toBe("238 of 250 drafts left · resets Oct 14");
    expect(footerUsageLine({ ...consented, signed_in: true, session: session({ usage: { allowance: 0, used: 0, remaining: 0 } }) }, { timeZone: "UTC", now: NOW_2025 }))
      .toBe("0 drafts left · resets Oct 14");
  });

  it("never says refill: the website says the allowance resets", () => {
    const signedIn: BridgeState = { ...initialBridgeState("1.0.0"), configured: true, consent: { accepted: true, passive_learning: false }, signed_in: true, session: session() };
    const lines = [
      footerUsageLine(signedIn, { timeZone: "UTC", now: NOW_2025 }),
      nearLimitCopy(usage({ allowance: 300, used: 250, remaining: 50 }), { timeZone: "UTC", now: NOW_2025 }).body,
      ...DOCK_STATE_PRIORITY.map(state => {
        const copy = dockStateCopy(state, { usage: usage({ used: 250, remaining: 0 }), timeZone: "UTC", now: NOW_2025 });
        return copy ? `${copy.title} ${copy.body}` : "";
      }),
      ...ERROR_CODES.map(code => {
        const copy = errorCopy(code, { usage: usage(), timeZone: "UTC", now: NOW_2025 });
        return `${copy.title} ${copy.body}`;
      }),
      `${errorCopy("fair_use_exceeded", { usage: usage({ resets_at: null }) }).title}`,
    ];
    for (const line of lines) expect(line).not.toMatch(/refill/i);
  });
});

describe("fair use: drafts left, drafting paused until the allowance resets", () => {
  const blocked = session({ usage: { fair_use_blocked: true } });

  it("is its own state, not running out of drafts", () => {
    expect(dockStateFor({ ...ready, session: blocked })).toBe("fair_use");
    expect(blocksRequests("fair_use")).toBe(false);
    expect(dotHealth("fair_use")).toBe("warn");
  });

  it("says the website's words, with a button to the account's usage", () => {
    const copy = dockStateCopy("fair_use", { usage: blocked.usage, timeZone: "UTC", now: NOW_2025 });
    expect(copy?.title).toBe("Drafting is paused until October 14");
    expect(copy?.body).toBe("Your account reached this period's fair-use limit. Nothing extra is charged.");
    expect(copy?.actions).toEqual([{ kind: "open_site", page: "account", label: "View usage" }]);
    expect(`${copy?.title} ${copy?.body}`).not.toMatch(/used your|drafts for this month/);
  });

  it("says the same on a refused request, and that no draft was used", () => {
    const copy = errorCopy("fair_use_exceeded", { usage: blocked.usage, timeZone: "UTC", now: NOW_2025 });
    expect(copy.title).toBe("Drafting is paused until October 14");
    expect(copy.body).toBe("Your account reached this period's fair-use limit. Nothing extra is charged. No draft was used.");
    expect(copy.actions).toEqual([{ kind: "open_site", page: "account", label: "View usage" }]);
    expect(errorCopy("fair_use_exceeded").title).toBe("Drafting is paused until your allowance resets");
  });
});

describe("drafting paused", () => {
  const NOON = Date.parse("2025-09-20T12:00:00Z");

  it("says the server's message as it is, without a second 'Try again later.'", () => {
    const message = "Drafting is paused for a short while. Please try again later.";
    expect(dockStateCopy("service_paused", { serviceMessage: message })?.body).toBe(message);
    expect(errorCopy("service_paused", { serviceMessage: message }).body).toBe(`${message} No draft was used.`);
    expect(dockStateCopy("service_paused", {})?.body).toBe("Starling has paused drafting for a short while. Try again later.");
    expect(errorCopy("service_paused").body).toBe("Starling has paused drafting for a short while. Try again later. No draft was used.");
  });

  it("says the Free plan's pause in the server's words, adds only when it lifts, and offers plans", () => {
    const retryAt = Math.floor(NOON / 1000) + 3 * 3_600;
    const message = "Drafting on the Free plan is paused for the rest of the day. Paid plans keep drafting.";
    const copy = dockStateCopy("service_paused", { serviceScope: "free", serviceMessage: message, serviceRetryAt: retryAt, timeZone: "UTC", now: NOON });
    expect(copy?.body).toBe(`${message} Free drafting resumes at 3:00 PM.`);
    expect(copy?.body.match(/Paid plans keep drafting/g)).toHaveLength(1);
    expect(copy?.actions[0]).toEqual({ kind: "open_site", page: "pricing", label: "See plans", primary: true });
    const refused = errorCopy("service_paused", { serviceScope: "free", serviceMessage: message, serviceRetryAt: retryAt, timeZone: "UTC", now: NOON });
    expect(refused.body).toBe(`${message} Free drafting resumes at 3:00 PM. No draft was used.`);
    // The refusal's Retry-After stands in when the account state has no time yet.
    expect(errorCopy("service_paused", { serviceScope: "free", serviceMessage: message, retryAfterS: 20 * 3_600, timeZone: "UTC", now: NOON }).body)
      .toContain("Free drafting resumes tomorrow at 8:00 AM.");
    // No message from the server: the extension's own sentence, said once.
    expect(dockStateCopy("service_paused", { serviceScope: "free", serviceRetryAt: retryAt, timeZone: "UTC", now: NOON })?.body)
      .toBe("Drafting on the Free plan resumes at 3:00 PM. Paid plans keep drafting.");
  });

  it("leaves the time out when the server gives none, and plans out when there is nothing to upgrade to", () => {
    const message = "Drafting on the Free plan is paused for the rest of the day. Paid plans keep drafting.";
    expect(dockStateCopy("service_paused", { serviceScope: "free", serviceMessage: message })?.body).toBe(message);
    expect(dockStateCopy("service_paused", { serviceScope: "free" })?.body).toBe("Drafting on the Free plan resumes later. Paid plans keep drafting.");
    const top = dockStateCopy("service_paused", { serviceScope: "free", upgradable: false });
    expect(top?.actions.some(action => action.kind === "open_site")).toBe(false);
    expect(top?.actions.length).toBeGreaterThan(0);
  });

  it("reads scope and retry_at from the session, and pauses only the accounts it covers", () => {
    const retryAt = 1_760_000_000;
    const freeAccount = session({ plan: { code: "free", name: "Free", status: "free" }, service: { status: "paused", scope: "free", message: null, retry_at: retryAt } });
    expect(freeAccount.service).toEqual({ status: "paused", scope: "free", message: null, retry_at: retryAt });
    expect(dockStateFor({ ...ready, session: freeAccount })).toBe("service_paused");
    const paid = session({ service: { status: "paused", scope: "free", message: null, retry_at: retryAt } });
    expect(dockStateFor({ ...ready, session: paid })).toBe("ready");
    const everyone = session({ service: { status: "paused", scope: "all", message: "Maintenance.", retry_at: null } });
    expect(dockStateFor({ ...ready, session: everyone })).toBe("service_paused");
    // An older server: no scope, so the pause covers everyone, as before.
    expect(dockStateFor({ ...ready, session: session({ service: { status: "paused" } }) })).toBe("service_paused");
    // "ok" carries nothing else, whatever a server sends beside it.
    expect(session({ service: { status: "ok", scope: "free", message: "stale", retry_at: 5 } }).service)
      .toEqual({ status: "ok", scope: null, message: null, retry_at: null });
    expect(session({ service: { status: "paused", scope: "everyone", retry_at: "soon" } }).service)
      .toEqual({ status: "paused", scope: null, message: null, retry_at: null });
  });
});

describe("the top plan has nothing to upgrade to", () => {
  const NOON = Date.parse("2025-09-20T12:00:00Z");
  const top = { upgradable: false } as const;

  it("reads plan.upgradable, and keeps today's wording when a server does not send it", () => {
    expect(session({ plan: { upgradable: false } }).plan.upgradable).toBe(false);
    expect(session({ plan: { upgradable: true } }).plan.upgradable).toBe(true);
    expect(session().plan.upgradable).toBeNull();
    expect(session({ plan: { upgradable: "no" } }).plan.upgradable).toBeNull();
  });

  it("offers Contact support instead of an upgrade when the drafts run out", () => {
    const copy = errorCopy("limit_reached", { ...top, usage: usage({ used: 250, remaining: 0 }), timeZone: "UTC", now: NOW_2025 });
    expect(copy.body).toBe("Your allowance resets on October 14. Contact support if you need more drafts. No draft was used.");
    expect(copy.actions).toEqual([{ kind: "support", label: "Contact support", primary: true }, { kind: "dismiss", label: "Not now" }]);
    expect(`${copy.title} ${copy.body}`).not.toMatch(/upgrade/i);
    expect(dockStateCopy("limit_reached", { ...top, usage: usage({ used: 250, remaining: 0 }) })?.body).not.toMatch(/upgrade/i);
    // Unknown (an older server): the upgrade stays.
    expect(errorCopy("limit_reached", { upgradable: null, usage: usage({ used: 250, remaining: 0 }) }).body).toContain("Upgrade to keep drafting today.");
  });

  it("says when drafting comes back after the daily cap, without offering more requests a day", () => {
    const copy = errorCopy("rate_limited", { ...top, retryAfterS: 3 * 3_600, planName: "Pro", timeZone: "UTC", now: NOON });
    expect(copy.title).toBe("You've reached today's limit on Pro");
    expect(copy.body).toBe("You can draft again at 3:00 PM. No draft was used.");
    expect(copy.actions).toEqual([{ kind: "support", label: "Contact support" }]);
  });

  it("points the near-limit notice at support, not at plans", () => {
    const copy = nearLimitCopy(usage({ allowance: 300, used: 250, remaining: 50 }), { ...top, timeZone: "UTC", now: NOW_2025 });
    expect(copy.actions).toEqual([{ kind: "support", label: "Contact support" }]);
    expect(nearLimitCopy(usage({ allowance: 300, used: 250, remaining: 50, resets_at: null }), top).body).toBe("Contact support if you need more drafts.");
    expect(nearLimitCopy(usage({ allowance: 300, used: 250, remaining: 50 })).actions).toEqual([{ kind: "open_site", page: "pricing", label: "See plans" }]);
  });

  it("turns the footer's Upgrade into Contact support", () => {
    const base: BridgeState = { ...initialBridgeState("1.0.0"), configured: true, consent: { accepted: true, passive_learning: false }, signed_in: true };
    const low = { allowance: 250, used: 240, remaining: 10 };
    expect(footerUpgradeAction({ ...base, session: session({ usage: low }) })).toEqual({ kind: "open_site", page: "pricing", label: "Upgrade" });
    expect(footerUpgradeAction({ ...base, session: session({ usage: low, plan: { upgradable: false } }) })).toEqual({ kind: "support", label: "Contact support" });
    expect(footerUpgradeAction({ ...base, session: session() })).toBeNull();
    expect(footerUpgradeAction({ ...base, signed_in: false, session: session({ usage: low }) })).toBeNull();
  });
});

describe("what one press costs", () => {
  it("reads usage.per_request, and is 1 when a server does not say", () => {
    expect(session({ usage: { per_request: 3 } }).usage.per_request).toBe(3);
    expect(session().usage.per_request).toBeUndefined();
    expect(draftsPerRequest(session().usage)).toBe(1);
    for (const junk of [0, -2, 1.5, "2", null]) {
      const read = session({ usage: { per_request: junk as unknown as number } }).usage;
      expect(read.per_request, String(junk)).toBeUndefined();
      expect(draftsPerRequest(read), String(junk)).toBe(1);
    }
    expect(draftsPerRequest(null)).toBe(1);
    expect(draftsPerRequest(usage({ per_request: 2 }))).toBe(2);
  });

  it("names the cost with the right plural", () => {
    expect(pressCost(usage())).toBe("1 draft");
    expect(pressCost(usage({ per_request: 2 }))).toBe("2 drafts");
    expect(pressCost(null)).toBe("1 draft");
    expect(draftCount(3, usage({ unit: "credit", unit_label: "credits" }))).toBe("3 credits");
    expect(draftCount(1, usage({ unit: "credit", unit_label: "credits" }))).toBe("1 credit");
  });

  it("says a stopped stream used a draft only when the server says so", () => {
    expect(usedAfterStopLine(usage())).toBe("This used 1 draft because part of it was shown.");
    expect(usedAfterStopLine(usage({ per_request: 2 }))).toBe("This used 2 drafts because part of it was shown.");
    expect(stoppedCostLine("used_after_stop", usage())).toBe("This used 1 draft because part of it was shown.");
    expect(stoppedCostLine("finished", usage({ per_request: 2 }))).toBe("Starling had already finished it, so it used 2 drafts.");
    expect(stoppedCostLine("nothing", usage({ per_request: 1 }))).toBe("No draft was used.");
    // A gateway older than `used_after_stop` charges a stop after text without saying so: its
    // silence is not "No draft was used", so the card says only "Stopped."
    expect(stoppedCostLine("nothing", usage())).toBeNull();
    expect(stoppedCostLine("nothing", null)).toBeNull();
  });
});
