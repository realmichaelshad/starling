/**
 * Journey 4: a customer who signs up with Google, in a real Chrome with the real extension.
 *
 * The Google pages are the harness's stand-in (`e2e/harness/fake_google.py`): an account chooser a
 * person really clicks through, real RS256 ID tokens, real PKCE, the gateway's full token checks.
 * Real Google sign-in is checked by whoever deploys Starling (docs/DEPLOYMENT.md).
 *
 * What it proves: "Sign up with Google" makes a confirmed account with no confirmation email and no
 * password; and the extension's sign-in window connects through the same Google sign-in, without a
 * password typed anywhere.
 */
import { expect, test } from "./fixtures.mjs";
import { acceptConsent, uniqueEmail } from "../lib/flows.mjs";

test("sign up with Google, then connect the extension through Google", async ({ app, gatewayUrl, stack, shot }) => {
  const email = uniqueEmail("google").replace(/@.*/, "@gmail.com");
  const page = await app.context.newPage();

  // ---------------------------------------------------------------- sign up with Google
  await page.goto(`${gatewayUrl}/signup`);
  await shot(page, "signup-with-google");
  await page.getByRole("link", { name: "Sign up with Google" }).click();
  await page.getByTestId("google-email").fill(email);
  await shot(page, "google-account-chooser");
  await page.getByTestId("google-continue").click();
  await page.waitForURL(`${gatewayUrl}/account`);
  await expect(page.getByText("Welcome to Starling"), "the first page says hello").toBeVisible();
  await expect(page.getByRole("heading", { name: "Get started" }), "and what to do next").toBeVisible();
  await shot(page, "account-after-google-sign-up");

  const accountId = await stack.accountId(email);
  const [row] = await stack.sql("SELECT email_verified_at, password_hash FROM accounts WHERE id = ?", [accountId]);
  expect(row.email_verified_at, "Google confirmed the address, so no email was needed").not.toBeNull();
  expect(row.password_hash, "an account made with Google has no password").toBe("!none");

  // ---------------------------------------------------------------- Security says how the account signs in
  await page.goto(`${gatewayUrl}/account/security`);
  await expect(page.getByRole("heading", { name: "Sign-in methods" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Add a password" }), "a password is offered, not required").toBeVisible();
  await shot(page, "security-google-only");

  // ---------------------------------------------------------------- the extension, through Google
  await page.getByRole("button", { name: /sign out/i }).first().click();
  await page.waitForLoadState();
  const onboarding = await acceptConsent(app);
  const authOpened = app.context.waitForEvent("page", { timeout: 30_000 });
  await onboarding.getByRole("button", { name: "Sign in" }).click();
  const auth = await authOpened;
  await auth.waitForLoadState();
  await auth.getByRole("link", { name: "Continue with Google" }).click();
  await auth.getByTestId("google-email").fill(email);
  await auth.getByTestId("google-continue").click();
  await auth.getByRole("button", { name: "Connect this browser" }).click();
  await onboarding.getByRole("heading", { name: "You're all set" }).waitFor({ timeout: 30_000 });
  await shot(onboarding, "extension-connected-through-google");

  const devices = await stack.sql("SELECT COUNT(*) AS n FROM devices WHERE account_id = ? AND revoked_at IS NULL", [accountId]);
  expect(devices[0].n, "one browser connected, with no password typed").toBe(1);
});
