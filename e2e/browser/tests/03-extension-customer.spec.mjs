/**
 * Journey 3: a customer with the real extension, from first install to the limit and past it.
 *
 * The real unpacked extension in a real Chrome profile, the real `chrome.identity` sign-in, the
 * real gateway, services process and engine. The X page is the harness's fixture (never x.com),
 * the AI provider and the payment pages are the harness's fakes.
 *
 * What it proves: a person can install, connect, draft, run out, pay and carry on drafting
 * without leaving a dead end, and the number of drafts left is the same in the extension, in
 * the API and on the website at every step. Starling never presses Post or Reply.
 */
import { expect, test } from "./fixtures.mjs";
import {
  createVerifiedAccount, expectUsageAgrees, installAndSignIn, loginInBrowser, openX, postStar, refreshDock, returnToTab, uniqueEmail,
} from "../lib/flows.mjs";
import { dock, draftsLeft, starling, syncClosedRoots } from "../lib/starling-ui.mjs";

const POSTS = ["1900000000000000001", "1900000000000000002", "1900000000000000003"];

async function draftAReply(xPage, postId) {
  await postStar(xPage, postId).click();
  const dialog = xPage.locator('[role="dialog"]');
  try {
    await dialog.waitFor({ state: "visible", timeout: 60_000 });
  } catch (error) {
    // Say what the panel said instead: a journey that only reports "no dialog" explains nothing.
    await syncClosedRoots(xPage);
    const said = await dock(xPage).feed.innerText().catch(() => "(the panel's feed could not be read)");
    throw new Error(`no reply box opened for post ${postId}. The panel says: ${said.replace(/\s+/g, " ")}`);
  }
  const box = dialog.locator('[data-testid="tweetTextarea_0"]');
  await expect.poll(async () => ((await box.innerText().catch(() => "")) || "").trim().length, { timeout: 90_000 }).toBeGreaterThan(20);
  // Let the typing finish: the text stops changing.
  let last = "";
  for (let attempt = 0; attempt < 60; attempt += 1) {
    const now = ((await box.innerText()) || "").trim();
    if (now === last) break;
    last = now;
    await xPage.waitForTimeout(250);
  }
  return { dialog, box, text: last };
}

async function closeWithoutPosting(xPage, dialog) {
  await dialog.locator('[aria-label="Close"]').click();
  await dialog.waitFor({ state: "hidden" });
  const record = await xPage.evaluate(() => window.__xFixture?.submitClicks ?? null);
  expect(record ?? [], "nobody pressed Post or Reply").toEqual([]);
}

test("install, connect, draft, run out, upgrade and carry on", async ({ app, api, gatewayUrl, stack, session, shot }) => {
  const plans = session.state.settings.plans.codes;
  const account = await createVerifiedAccount(api, gatewayUrl, { email: uniqueEmail("extension") });
  const accountId = await stack.accountId(account.email);

  // ---------------------------------------------------------------- first install
  const onboarding = await installAndSignIn(app, account);
  await expect(onboarding.getByRole("heading", { name: "You're all set" })).toBeVisible();
  await shot(onboarding, "onboarding-all-set");

  // The sign-in window left this profile signed in to the website too, as it does for a customer.
  const site = await app.context.newPage();
  await site.goto(`${gatewayUrl}/account/devices`);
  if (/\/login/.test(site.url())) {
    await loginInBrowser(site, gatewayUrl, account.email);
    await site.goto(`${gatewayUrl}/account/devices`);
  }
  await expect(site.getByRole("button", { name: "Disconnect" }), "the website lists this browser").toHaveCount(1);
  await shot(site, "devices-one-browser");

  // ---------------------------------------------------------------- the first draft
  const xPage = await openX(app);
  const panel = await refreshDock(xPage);
  expect(draftsLeft(await panel.footer.innerText())).toBe(plans.free.monthly_credits);
  await shot(xPage, "x-with-the-panel");

  const first = await draftAReply(xPage, POSTS[0]);
  expect(first.text.length).toBeGreaterThan(20);
  await shot(xPage, "reply-drafted-into-xs-own-box");
  await closeWithoutPosting(xPage, first.dialog);

  await expectUsageAgrees(app, { xPage, sitePage: site, gatewayUrl, remaining: plans.free.monthly_credits - 1 });
  await shot(site, "website-agrees-after-one-draft");

  // ---------------------------------------------------------------- running out
  await stack.debit(accountId, plans.free.monthly_credits - 1, "journey 3: the rest of the free allowance");
  // Spent outside this tab: the panel shows it on its next open, as it does for a customer.
  await refreshDock(xPage);
  await expectUsageAgrees(app, { xPage, sitePage: site, gatewayUrl, remaining: 0 });

  await xPage.bringToFront();
  await postStar(xPage, POSTS[1]).click();
  await syncClosedRoots(xPage);
  const out = dock(xPage);
  await expect(out.stateTitle, "running out is said in words, with the number").toContainText(`used your ${plans.free.monthly_credits}`, { timeout: 60_000 });
  await expect(out.stateBody).toContainText(/Upgrade to keep drafting/i);
  await expect(xPage.locator('[role="dialog"]'), "no reply box is opened for a refused draft").toHaveCount(0);
  await shot(xPage, "limit-reached-in-the-extension");

  // "See plans" opens the website's pricing page, already signed in, saying why it opened.
  const pricingPage = app.context.waitForEvent("page", { timeout: 30_000 });
  await out.stateAction("See plans").click();
  const pricing = await pricingPage;
  await pricing.waitForLoadState();
  expect(new URL(pricing.url()).pathname).toBe("/pricing");
  expect(pricing.url()).toContain("reason=limit");
  await shot(pricing, "pricing-opened-from-the-extension");

  // ---------------------------------------------------------------- paying
  await pricing.getByRole("button", { name: /Choose Starter|Starter/ }).first().click();
  await pricing.waitForLoadState();
  if (!/\/c\/pay\//.test(pricing.url())) {
    // The pricing page sends a signed-in customer to Billing to choose; do it there.
    await pricing.goto(`${gatewayUrl}/account/billing`);
    await pricing.getByRole("button", { name: "Choose Starter" }).click();
  }
  await pricing.waitForURL(/\/c\/pay\//);
  await pricing.getByTestId("checkout-pay").click();
  await pricing.waitForURL(url => url.origin === new URL(gatewayUrl).origin);
  await expect(pricing.getByRole("heading", { level: 1 })).toContainText("You are on Starter", { timeout: 90_000 });
  await shot(pricing, "you-are-on-starter");

  // ---------------------------------------------------------------- carrying on, in the same X tab
  // Back on X after paying: the panel reads the account again by itself, with nobody reopening it.
  await returnToTab(xPage);
  await expectUsageAgrees(app, { xPage, sitePage: site, gatewayUrl, remaining: plans.starter.monthly_credits });
  await shot(xPage, "extension-shows-the-paid-allowance");

  // The same post again. Starling may draft, or may say the post is better left alone: a verdict
  // with no draft in it is an answer about the POST, and it must not cost a draft.
  await postStar(xPage, POSTS[1]).click();
  const replyBox = xPage.locator('[role="dialog"]');
  const verdict = dock(xPage).feed.getByText(/WAIT|SKIP|worth/i).last();
  await expect.poll(async () => {
    await syncClosedRoots(xPage);
    return (await replyBox.isVisible()) || (await verdict.isVisible().catch(() => false));
  }, { timeout: 90_000, message: "the second press answers one way or the other" }).toBe(true);
  let expected = plans.starter.monthly_credits;
  if (await replyBox.isVisible()) {
    await closeWithoutPosting(xPage, replyBox);
    expected -= 1;
  } else {
    await shot(xPage, "a-verdict-instead-of-a-draft");
  }
  await expectUsageAgrees(app, { xPage, sitePage: site, gatewayUrl, remaining: expected });

  // Tightening a rough line in X's own composer: the second surface a customer uses every day.
  await xPage.bringToFront();
  const composer = xPage.locator('[data-fixture="home-composer"]');
  const roughBox = composer.locator('[data-testid="tweetTextarea_0"]');
  const rough = "We deleted two build steps nobody ran and the pipeline got four minutes shorter.";
  await roughBox.click();
  await xPage.keyboard.type(rough, { delay: 8 });
  await syncClosedRoots(xPage);
  const pill = starling(composer, "button.vf-enhance");
  await expect(pill).toContainText(/Enhance/i, { timeout: 20_000 });
  await pill.click();
  await expect.poll(async () => ((await roughBox.innerText()) || "").trim(), { timeout: 120_000, message: "the composer text is replaced or kept, never emptied" })
    .not.toBe("");
  await xPage.waitForTimeout(3000);
  const afterEnhance = ((await roughBox.innerText()) || "").trim();
  if (afterEnhance !== rough) expected -= 1;                  // a rewrite is a product; his own words handed back are not
  await shot(xPage, "composer-after-enhance");
  await expectUsageAgrees(app, { xPage, sitePage: site, gatewayUrl, remaining: expected });
  const posted = await xPage.evaluate(() => window.__xFixture?.submitClicks ?? []);
  expect(posted, "nobody pressed Post").toEqual([]);

  // ---------------------------------------------------------------- disconnecting from the website
  await site.goto(`${gatewayUrl}/account/devices`);
  // Disconnecting cannot be undone from here, so the first click only asks.
  await site.getByRole("button", { name: /^Disconnect/ }).click();
  await site.getByRole("button", { name: /Click again to disconnect/ }).click();
  await site.waitForLoadState();
  await expect(site.getByText(/is disconnected\. It can no longer draft/i), "the page says which browser went").toBeVisible();
  await expect(site.getByText(/No browsers are connected/i)).toBeVisible();
  await shot(site, "devices-none-after-disconnect");

  await xPage.bringToFront();
  await postStar(xPage, POSTS[2]).click();
  await syncClosedRoots(xPage);
  await expect(dock(xPage).stateTitle, "a disconnected browser says so and offers sign-in").toContainText(/signed out|Sign in/i, { timeout: 60_000 });
  await shot(xPage, "extension-after-being-disconnected");
});
