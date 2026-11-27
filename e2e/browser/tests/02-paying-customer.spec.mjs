/**
 * Journey 2: customers who pay, in a real browser.
 *
 * The payment pages are the harness's stand-in for Stripe's hosted Checkout and Customer Portal
 * (https on 127.0.0.1, signed webhooks back to the gateway). Nothing here is evidence about the
 * real Stripe; it is evidence that the WEBSITE never strands a paying customer: a checkout they
 * walk away from, a declined card, a cancellation, a change of mind and an upgrade all end on a
 * page that says what happened and what they have now.
 */
import { expect, test } from "./fixtures.mjs";
import { createVerifiedAccount, loginInBrowser, uniqueEmail } from "../lib/flows.mjs";

async function billingPlan(page, gatewayUrl) {
  await page.goto(`${gatewayUrl}/account/billing`);
  return page.locator("[data-plan]").first().getAttribute("data-plan");
}

/** Webhooks are asynchronous: wait for the billing page to show the plan, as a customer would refresh. */
async function waitForPlan(page, gatewayUrl, plan, { timeout = 60_000 } = {}) {
  const deadline = Date.now() + timeout;
  let seen = null;
  while (Date.now() < deadline) {
    seen = await billingPlan(page, gatewayUrl);
    if (seen === plan) return;
    await page.waitForTimeout(1000);
  }
  throw new Error(`the billing page never showed ${plan} (last: ${seen})`);
}

async function allowance(page, gatewayUrl) {
  await page.goto(`${gatewayUrl}/account`);
  return Number((await page.locator("[data-usage=allowance]").first().innerText()).replace(/[^\d]/g, ""));
}

const backOnTheSite = gatewayUrl => url => url.origin === new URL(gatewayUrl).origin;

/** A customer who does not see the change yet refreshes the page; so does the journey. */
async function untilVisible(page, locator, { timeout = 60_000, visible = true } = {}) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if ((await locator.count()) > 0 === visible) return;
    await page.waitForTimeout(1000);
    await page.reload();
  }
  throw new Error(`the page never ${visible ? "showed" : "dropped"} ${locator}`);
}

const untilGone = (page, locator, options = {}) => untilVisible(page, locator, { ...options, visible: false });

test("walking away from checkout, or a declined card, charges nothing and says so", async ({ app, api, gatewayUrl, session, shot }) => {
  const account = await createVerifiedAccount(api, gatewayUrl, { email: uniqueEmail("declined") });
  const plans = session.state.settings.plans.codes;
  const page = await app.context.newPage();
  await loginInBrowser(page, gatewayUrl, account.email);

  // ---------------------------------------------------------------- looking at the prices
  await page.goto(`${gatewayUrl}/pricing`);
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  for (const code of ["free", "starter", "pro"]) {
    await expect(page.getByText(new RegExp(`\\b${plans[code].monthly_credits}\\b`)).first(), `${code} allowance is on the page`).toBeVisible();
  }
  await shot(page, "pricing-signed-in");

  // ---------------------------------------------------------------- walking away
  await page.goto(`${gatewayUrl}/account/billing`);
  await shot(page, "billing-free");
  await page.getByRole("button", { name: "Choose Starter" }).click();
  await page.waitForURL(/\/c\/pay\//);                       // the site's own policy has to allow this redirect
  await shot(page, "hosted-checkout");
  await page.getByTestId("checkout-leave").click();
  await page.waitForURL(backOnTheSite(gatewayUrl));
  await shot(page, "came-back-without-paying");
  expect(await billingPlan(page, gatewayUrl), "walking away changes nothing").toBe("free");
  await expect(page.locator("[data-payment-notice]"), "and nothing claims a payment problem").toHaveCount(0);

  // ---------------------------------------------------------------- a declined card
  await page.getByRole("button", { name: "Choose Starter" }).click();
  await page.waitForURL(/\/c\/pay\//);
  await page.getByTestId("checkout-decline").click();
  await page.waitForLoadState();
  // The return page must not "confirm" a payment that was declined: waiting cannot help. It says
  // so (reloading itself if the news arrives a moment after the customer does) and offers the way out.
  await expect(page.getByRole("heading", { level: 1 })).toContainText("Your payment is not finished", { timeout: 60_000 });
  await expect(page.getByRole("button", { name: "Finish paying for Starter" })).toBeVisible();
  await expect(page.getByText("Confirming your subscription")).toHaveCount(0);
  await shot(page, "after-a-declined-card");

  expect(await billingPlan(page, gatewayUrl), "a declined card grants nothing").toBe("free");
  const notice = page.locator('[data-payment-notice="first"]');
  await expect(notice, "the customer is told what happened").toBeVisible();
  const words = (await notice.innerText()).replace(/\s+/g, " ");
  expect(words).toMatch(/Your Starter payment is not finished/);
  expect(words).toMatch(/not been charged/i);
  expect(words).toMatch(/still on Free/i);
  expect(words, "a free customer is never told they keep paid access").not.toMatch(/keep paid access|to keep Free/i);
  expect(words, "Stripe does not retry a first payment, and the portal cannot pay it").not.toMatch(/tries the payment again/i);
  await expect(notice.getByRole("button", { name: "Finish paying for Starter" })).toBeVisible();
  await expect(notice.getByRole("button", { name: "Update payment method" })).toHaveCount(0);
  await shot(page, "billing-after-a-declined-card");
  expect(await allowance(page, gatewayUrl)).toBe(plans.free.monthly_credits);
  await expect(page.locator('[data-payment-notice="first"]'), "the overview says the same thing").toBeVisible();
  await expect(page.getByRole("link", { name: "Go to Billing" })).toBeVisible();
  await shot(page, "overview-after-a-declined-card");
});

test("a declined card is not a dead end: finish the payment, or choose another plan", async ({ app, api, gatewayUrl, session, stack, shot }) => {
  const plans = session.state.settings.plans.codes;

  // ---------------------------------------------------------------- finishing the payment that was declined
  const finisher = await createVerifiedAccount(api, gatewayUrl, { email: uniqueEmail("finisher") });
  const page = await app.context.newPage();
  await loginInBrowser(page, gatewayUrl, finisher.email);
  await page.goto(`${gatewayUrl}/account/billing`);
  await page.getByRole("button", { name: "Choose Starter" }).click();
  await page.waitForURL(/\/c\/pay\//);
  await page.getByTestId("checkout-decline").click();
  await page.waitForLoadState();

  await page.goto(`${gatewayUrl}/account/billing`);
  // Stripe's invoice page has no way back to the site, so it opens in a new tab and the Billing tab
  // watches for the payment. The site's `form-action` policy has to allow the redirect as well: a
  // browser that blocks it shows nothing at all, and the customer can never pay.
  const [invoice] = await Promise.all([
    app.context.waitForEvent("page"),
    page.locator('[data-payment-notice="first"]').getByRole("button", { name: "Finish paying for Starter" }).click(),
  ]);
  await invoice.waitForURL(/\/i\//);
  await shot(invoice, "hosted-invoice");
  await invoice.getByTestId("invoice-pay").click();
  await expect(invoice.getByTestId("invoice-paid")).toBeVisible();
  await shot(invoice, "hosted-invoice-paid");
  await invoice.close();

  // The Billing tab updates by itself once the payment lands: nobody has to come back and refresh.
  await expect(page.locator("[data-payment-notice]"), "the billing tab noticed the payment").toHaveCount(0, { timeout: 60_000 });
  await waitForPlan(page, gatewayUrl, "starter");
  await expect(page.locator("[data-payment-notice]"), "nothing is left to warn about").toHaveCount(0);
  await shot(page, "billing-starter-after-finishing");
  expect(await allowance(page, gatewayUrl)).toBe(plans.starter.monthly_credits);
  const finisherId = await stack.accountId(finisher.email);
  const finisherSubs = await stack.sql("SELECT status FROM subscriptions WHERE account_id = ?", [finisherId]);
  expect(finisherSubs.map(row => row.status), "one subscription, the one they started").toEqual(["active"]);

  // ---------------------------------------------------------------- declined on Starter, then chooses Pro
  await app.context.clearCookies();                          // somebody else, on a clean browser
  const switcher = await createVerifiedAccount(api, gatewayUrl, { email: uniqueEmail("switcher") });
  await loginInBrowser(page, gatewayUrl, switcher.email);
  await page.goto(`${gatewayUrl}/account/billing`);
  await page.getByRole("button", { name: "Choose Starter" }).click();
  await page.waitForURL(/\/c\/pay\//);
  await page.getByTestId("checkout-decline").click();
  await page.waitForLoadState();

  await page.goto(`${gatewayUrl}/account/billing`);
  await expect(page.locator('[data-payment-notice="first"]')).toContainText("the unfinished payment is cancelled");
  await page.getByRole("button", { name: "Choose Pro" }).click();
  await page.waitForURL(/\/c\/pay\//);                       // a checkout of its own, never Starter's invoice
  await shot(page, "hosted-checkout-for-the-other-plan");
  await page.getByTestId("checkout-pay").click();
  await page.waitForURL(backOnTheSite(gatewayUrl));
  await expect(page.getByRole("heading", { level: 1 })).toContainText("You are on Pro", { timeout: 90_000 });
  await waitForPlan(page, gatewayUrl, "pro");
  await expect(page.locator("[data-payment-notice]"), "the abandoned Starter payment is gone").toHaveCount(0);
  await shot(page, "billing-pro-after-switching");
  expect(await allowance(page, gatewayUrl)).toBe(plans.pro.monthly_credits);
  const switcherId = await stack.accountId(switcher.email);
  const switcherSubs = await stack.sql("SELECT plan_code, status FROM subscriptions WHERE account_id = ? ORDER BY plan_code", [switcherId]);
  expect(switcherSubs.filter(row => row.status === "active").map(row => row.plan_code)).toEqual(["pro"]);
  expect(switcherSubs.filter(row => row.plan_code === "starter").map(row => row.status)).toEqual(["canceled"]);
});

test("a customer pays, cancels, changes their mind and upgrades", async ({ app, api, gatewayUrl, session, shot }) => {
  const account = await createVerifiedAccount(api, gatewayUrl, { email: uniqueEmail("payer") });
  const plans = session.state.settings.plans.codes;
  const page = await app.context.newPage();
  await loginInBrowser(page, gatewayUrl, account.email);

  // ---------------------------------------------------------------- paying
  await page.goto(`${gatewayUrl}/account/billing`);
  await page.getByRole("button", { name: "Choose Starter" }).click();
  await page.waitForURL(/\/c\/pay\//);
  await page.getByTestId("checkout-pay").click();
  await page.waitForURL(backOnTheSite(gatewayUrl));
  await shot(page, "back-from-paying");
  // The return page waits for the webhook, then says which plan the customer is on.
  await expect(page.getByRole("heading", { level: 1 })).toContainText("You are on Starter", { timeout: 90_000 });
  await shot(page, "you-are-on-starter");
  await waitForPlan(page, gatewayUrl, "starter");
  await shot(page, "billing-starter");
  expect(await allowance(page, gatewayUrl), "the paid allowance is there at once").toBe(plans.starter.monthly_credits);
  await shot(page, "overview-starter");

  // A second purchase must be impossible: a subscriber is offered an upgrade, not another checkout.
  await page.goto(`${gatewayUrl}/account/billing`);
  await expect(page.getByRole("button", { name: "Choose Starter" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Upgrade to Pro" })).toBeVisible();

  // ---------------------------------------------------------------- cancelling, then staying
  await page.getByRole("button", { name: "Cancel subscription" }).click();
  await page.waitForURL(/\/p\/session\//);
  await shot(page, "hosted-portal-cancel");
  await page.getByTestId("portal-cancel").click();
  await page.waitForURL(backOnTheSite(gatewayUrl));
  await page.goto(`${gatewayUrl}/account/billing`);
  await expect(page.getByRole("button", { name: "Keep Starter" }).first(), "a cancelled plan offers a way back").toBeVisible({ timeout: 60_000 });
  await shot(page, "billing-cancelling");
  expect(await billingPlan(page, gatewayUrl), "access stays until the period ends").toBe("starter");

  // Keeping the plan is one click on the site: no trip to Stripe's pages.
  await page.getByRole("button", { name: "Keep Starter" }).first().click();
  await page.waitForLoadState();
  await expect(page.getByText(/Starter is back on/)).toBeVisible();
  await expect(page.getByRole("button", { name: "Cancel subscription" }), "resumed: cancelling is offered again").toBeVisible({ timeout: 60_000 });
  await shot(page, "billing-resumed");

  // ---------------------------------------------------------------- upgrading
  await page.getByRole("button", { name: "Upgrade to Pro" }).click();
  await page.waitForLoadState();
  if (/\/p\/session\//.test(page.url())) {
    await shot(page, "hosted-portal-upgrade");
    await page.getByTestId("portal-confirm-update").click();
    await page.waitForURL(backOnTheSite(gatewayUrl));
  }
  await waitForPlan(page, gatewayUrl, "pro");
  await shot(page, "billing-pro");
  const upgraded = await allowance(page, gatewayUrl);
  expect(upgraded, "an upgrade adds drafts for the rest of the period").toBeGreaterThan(plans.starter.monthly_credits);
  expect(upgraded).toBeLessThanOrEqual(plans.pro.monthly_credits);
  await shot(page, "overview-pro");

  // ---------------------------------------------------------------- downgrading, then thinking better of it
  // Nothing visible changes for weeks after a downgrade, and an undo only makes a panel vanish,
  // so each has to be answered in words as well as shown.
  await page.goto(`${gatewayUrl}/account/billing`);
  await page.getByRole("button", { name: /Switch to Starter on/ }).click();
  await page.waitForLoadState();
  await expect(page.getByText("Your downgrade is scheduled")).toBeVisible();
  await untilVisible(page, page.getByRole("heading", { name: "Scheduled change" }));
  await expect(page.getByText(/You move to Starter on/)).toBeVisible();
  await expect(page.getByRole("button", { name: /Switch to Starter on/ }), "it cannot be scheduled twice").toHaveCount(0);
  await shot(page, "billing-downgrade-scheduled");
  expect(await billingPlan(page, gatewayUrl), "Pro is kept until the period ends").toBe("pro");

  await page.getByRole("button", { name: "Stay on Pro" }).click();
  await page.waitForLoadState();
  await expect(page.getByText("The scheduled change is cancelled")).toBeVisible();
  await untilGone(page, page.getByRole("heading", { name: "Scheduled change" }));
  await expect(page.getByRole("button", { name: /Switch to Starter on/ }), "and can be chosen again").toBeVisible();
  await shot(page, "billing-downgrade-undone");
  expect(await billingPlan(page, gatewayUrl)).toBe("pro");
});
