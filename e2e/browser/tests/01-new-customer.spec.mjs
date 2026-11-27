/**
 * Journey 1: a new customer on the website, making the mistakes people make.
 *
 * Everything is done the way a person does it: forms filled in and buttons pressed in a real
 * browser, emails opened from the (development) inbox. Local stack, mocked providers.
 *
 * What it proves: nobody gets stuck. Every mistake gets a sentence that says what to do, no
 * page dead-ends, an old link never works twice, a changed or reset password really takes
 * effect, the data export is a real file, and a deleted account is really gone.
 */
import fs from "node:fs";
import { expect, test } from "./fixtures.mjs";
import { PASSWORD, confirmEmailElsewhere, outboxLink, uniqueEmail } from "../lib/flows.mjs";

const NEW_PASSWORD = "another long enough passphrase";
const RESET_PASSWORD = "a third passphrase that is long";

async function summary(page) {
  // A page can carry one summary per form, and the ones with nothing to say are hidden.
  const box = page.locator(".error-summary:visible, [role=alert]:visible").first();
  await expect(box, "the page says what went wrong").toBeVisible();
  return (await box.innerText()).replace(/\s+/g, " ").trim();
}

async function signIn(page, gatewayUrl, email, password) {
  await page.goto(`${gatewayUrl}/login`);
  await page.getByLabel("Email address").fill(email);
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForLoadState();
}

test("a new customer signs up, stumbles, recovers and leaves without ever getting stuck", async ({ app, api, gatewayUrl, shot }) => {
  const page = await app.context.newPage();
  const email = uniqueEmail("newcustomer");

  // ---------------------------------------------------------------- arriving
  await page.goto(`${gatewayUrl}/`);
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await shot(page, "landing");
  await page.getByRole("link", { name: /Create (your )?account/i }).first().click();
  await expect(page).toHaveURL(/\/signup/);
  await shot(page, "signup-empty");

  // ---------------------------------------------------------------- the mistakes people make
  await page.getByRole("button", { name: "Create account" }).click();
  expect(await summary(page), "an empty form is answered in words").toMatch(/email|password/i);
  await shot(page, "signup-nothing-filled-in");

  await page.getByLabel("Email address").fill("not-an-address");
  await page.getByLabel("Password", { exact: true }).fill("short");
  await page.getByRole("button", { name: "Create account" }).click();
  const problems = await summary(page);
  expect(problems).toMatch(/email/i);
  expect(problems, "the password rule is stated, not hinted at").toMatch(/10/);
  await expect(page.getByLabel("Email address"), "what was typed is still there").toHaveValue("not-an-address");
  await expect(page.getByLabel("Password", { exact: true }), "a password is never echoed back").toHaveValue("");
  await shot(page, "signup-bad-address-and-short-password");

  // ---------------------------------------------------------------- signing up properly
  await page.getByLabel("Email address").fill(email);
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: "Create account" }).click();
  await page.waitForURL("**/verify-email/sent");
  await expect(page.getByRole("heading", { level: 1 })).toContainText(/check your email/i);
  await shot(page, "check-your-email");

  // Before confirming: signing in works, and the account says what is still missing.
  await signIn(page, gatewayUrl, email, PASSWORD);
  await expect(page).toHaveURL(/\/account/);
  await expect(page.getByText(/confirm|verify/i).first(), "an unconfirmed account is told so").toBeVisible();
  await shot(page, "account-before-confirming");

  const verifyLink = await outboxLink(api, email, "/verify-email");

  // ---------------------------------------------------------------- the emailed link
  await page.goto(verifyLink);
  await shot(page, "confirm-email-page");
  await page.getByRole("button", { name: "Confirm email" }).click();
  await page.waitForLoadState();
  await expect(page.getByText(/confirmed|verified/i).first()).toBeVisible();
  await shot(page, "email-confirmed");

  await page.goto(verifyLink);                                   // the same link a second time
  const confirmAgain = page.getByRole("button", { name: "Confirm email" });
  if (await confirmAgain.count()) await confirmAgain.click();
  await page.waitForLoadState();
  await expect(page.getByText(/already|expired|no longer|used/i).first(), "a used link says so plainly").toBeVisible();
  await shot(page, "confirm-link-used-twice");

  // ---------------------------------------------------------------- signing in, badly then well
  await page.goto(`${gatewayUrl}/account`);
  const signOut = page.getByRole("button", { name: /sign out/i }).first();
  await signOut.click();
  await page.waitForLoadState();
  await signIn(page, gatewayUrl, email, "the wrong passphrase entirely");
  await expect(page).toHaveURL(/\/login/);
  const refused = await summary(page);
  expect(refused, "a wrong password never says which half was wrong").not.toMatch(/no account|not found|unknown (email|address)/i);
  await shot(page, "signin-wrong-password");

  await signIn(page, gatewayUrl, email, PASSWORD);
  await expect(page).toHaveURL(/\/account/);
  await expect(page.locator("[data-usage=remaining]").first()).toBeVisible();
  await shot(page, "account-overview");

  // ---------------------------------------------------------------- every account page opens
  for (const [name, heading] of [["Billing", /billing|plan/i], ["Devices", /devices/i], ["Your data", /your data/i], ["Security", /security/i]]) {
    await page.getByRole("navigation", { name: /account/i }).getByRole("link", { name }).click();
    await expect(page.getByRole("heading", { level: 1 })).toContainText(heading);
    await shot(page, `account-${name}`);
  }

  // ---------------------------------------------------------------- changing the password
  await page.getByLabel("Current password").first().fill("not my password at all");
  await page.getByLabel("New password").fill(NEW_PASSWORD);
  await page.getByRole("button", { name: "Change password" }).click();
  expect(await summary(page)).toMatch(/current password|password/i);
  await shot(page, "change-password-wrong-current");

  await page.getByLabel("Current password").first().fill(PASSWORD);
  await page.getByLabel("New password").fill(NEW_PASSWORD);
  await page.getByRole("button", { name: "Change password" }).click();
  await page.waitForLoadState();
  await shot(page, "password-changed");

  await page.goto(`${gatewayUrl}/account`);
  if (!/\/login/.test(page.url())) await page.getByRole("button", { name: /sign out/i }).first().click();
  await signIn(page, gatewayUrl, email, PASSWORD);                // the OLD password
  await expect(page, "the old password stopped working").toHaveURL(/\/login/);
  await signIn(page, gatewayUrl, email, NEW_PASSWORD);
  await expect(page).toHaveURL(/\/account/);

  // ---------------------------------------------------------------- forgetting it
  await page.getByRole("button", { name: /sign out/i }).first().click();
  await page.goto(`${gatewayUrl}/login`);
  await page.getByRole("link", { name: /forgot/i }).click();
  await expect(page.getByRole("heading", { level: 1 })).toContainText(/reset your password/i);
  await page.getByLabel("Email address").fill(email);
  await page.getByRole("button", { name: "Send reset link" }).click();
  await expect(page.getByRole("heading", { level: 1 })).toContainText(/check your email/i);
  await shot(page, "reset-link-sent");

  const resetLink = await outboxLink(api, email, "/reset-password");
  await page.goto(resetLink);
  await expect(page.getByRole("heading", { level: 1 })).toContainText(/new password/i);
  await page.getByLabel("New password", { exact: true }).fill(RESET_PASSWORD);
  const again = page.getByLabel(/again/i);
  if (await again.count()) {
    await again.fill("not the same passphrase at all");         // a typo in the second box
    await page.getByRole("button", { name: "Save new password" }).click();
    expect(await summary(page), "two different passwords are caught before anything is saved").toMatch(/match|same/i);
    await shot(page, "reset-passwords-do-not-match");
    await page.getByLabel("New password", { exact: true }).fill(RESET_PASSWORD);
    await page.getByLabel(/again/i).fill(RESET_PASSWORD);
  }
  await page.getByRole("button", { name: "Save new password" }).click();
  await page.waitForLoadState();
  await expect(page, "the reset signs this browser in: nobody types the new password twice").toHaveURL(/\/account/);
  await shot(page, "password-reset-done");
  await page.getByRole("button", { name: /sign out/i }).first().click();
  await page.waitForLoadState();

  await page.goto(resetLink);                                     // the reset link a second time
  await expect(page.getByRole("heading", { level: 1 }), "a reset link works once").toContainText(/expired|already used/i);
  await shot(page, "reset-link-used-twice");

  await signIn(page, gatewayUrl, email, NEW_PASSWORD);            // the password from before the reset
  await expect(page).toHaveURL(/\/login/);
  await signIn(page, gatewayUrl, email, RESET_PASSWORD);
  await expect(page).toHaveURL(/\/account/);

  // ---------------------------------------------------------------- taking the data, then leaving
  await page.goto(`${gatewayUrl}/account/data`);
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download my data" }).click();
  const file = await (await download).path();
  const exported = JSON.parse(fs.readFileSync(file, "utf8"));
  expect(exported.account?.email ?? JSON.stringify(exported), "the export is this customer's").toContain(email.split("@")[0]);
  expect(JSON.stringify(exported), "no password hash or token in an export").not.toMatch(/password_hash|token_hash|argon2|scrypt|\$2[aby]\$/i);

  await page.getByLabel("Current password").fill(RESET_PASSWORD);
  await page.getByLabel(/type delete/i).fill("delete it");        // not the word that was asked for (any case is fine)
  await page.getByRole("button", { name: "Delete my account permanently" }).click();
  expect(await summary(page)).toMatch(/DELETE/);
  await shot(page, "delete-wrong-confirmation");

  await page.getByLabel("Current password").fill(RESET_PASSWORD);
  await page.getByLabel(/type delete/i).fill("DELETE");
  await page.getByRole("button", { name: "Delete my account permanently" }).click();
  await page.waitForLoadState();
  await shot(page, "account-deleted");
  await expect(page, "deleting signs the customer out").not.toHaveURL(/\/account\/data/);
  await expect(page.getByRole("heading", { level: 1 }), "and says what happened").toContainText(/deleted/i);

  await signIn(page, gatewayUrl, email, RESET_PASSWORD);
  await expect(page, "a deleted account cannot sign in").toHaveURL(/\/login/);
  await shot(page, "deleted-account-cannot-sign-in");
});

test("confirmed on the phone, the sign-up tab carries on by itself", async ({ app, api, gatewayUrl, shot }) => {
  const page = await app.context.newPage();
  const email = uniqueEmail("phone");
  await page.goto(`${gatewayUrl}/signup`);
  await page.getByLabel("Email address").fill(email);
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: "Create account" }).click();
  await page.waitForURL("**/verify-email/sent");
  await expect(page.getByText(/continues by itself/i), "the page says nothing more is needed here").toBeVisible();
  await shot(page, "waiting-for-the-phone");

  // Nothing moves before the email is confirmed: the page keeps waiting through a check.
  await page.waitForTimeout(4000);
  await expect(page, "no confirmation, no sign-in").toHaveURL(/\/verify-email\/sent/);

  // The phone: another client, none of this browser's cookies, opening the emailed link.
  await confirmEmailElsewhere(api, gatewayUrl, email);
  await page.waitForURL(/\/account/, { timeout: 15_000 });
  await expect(page.getByText(/Your email is confirmed/i).first(), "and it says why").toBeVisible();
  await shot(page, "carried-on-by-itself");
  await page.getByRole("button", { name: /sign out/i }).first().click();
  await page.waitForLoadState();
});
