/**
 * Journey 5: a reply to a post that makes a claim, checked on the web, in a real Chrome.
 *
 * The post claims a release "today" and links nothing, so the engine asks for one web search, which
 * the harness's stand-in answers in the Responses API's own shape, with cited pages
 * (`e2e/harness/fake_openai.py`). Real search is checked by whoever deploys Starling (docs/AI-PROVIDER.md).
 *
 * What it proves: the pages whose facts reached the draft are listed under it in the panel as real
 * links, each naming its site and opening in a new tab without a way back into X (OpenAI's rule for
 * anything drawn from web search results), and nobody pressed Post or Reply. The links are checked,
 * not followed: the network fence fails any journey whose browser leaves the local stack.
 */
import { assertionMustFail, expect, test } from "./fixtures.mjs";
import { createVerifiedAccount, installAndSignIn, openX, postStar, uniqueEmail } from "../lib/flows.mjs";
import { dock, syncClosedRoots } from "../lib/starling-ui.mjs";
import { CLAIM_POST, PROFILE_PATH } from "../lib/x-fixture.mjs";

test("a reply that checked the web lists its sources as links", async ({ app, api, gatewayUrl, shot }) => {
  const account = await createVerifiedAccount(api, gatewayUrl, { email: uniqueEmail("websearch") });
  await installAndSignIn(app, account);
  const xPage = await openX(app);

  await postStar(xPage, CLAIM_POST.id).click();
  const dialog = xPage.locator('[role="dialog"]');
  await dialog.waitFor({ state: "visible", timeout: 60_000 });
  const box = dialog.locator('[data-testid="tweetTextarea_0"]');
  await expect.poll(async () => ((await box.innerText().catch(() => "")) || "").trim().length,
    { timeout: 90_000 }).toBeGreaterThan(20);

  const links = dock(xPage).locator(".sources a");
  await expect.poll(async () => {
    await syncClosedRoots(xPage);
    return links.count();
  }, { timeout: 60_000, message: "the draft card lists the pages it used" }).toBeGreaterThan(0);
  expect(await links.count(), "at most three sources").toBeLessThanOrEqual(3);
  const first = links.first();
  await expect(first, "the first source is the searched page").toHaveAttribute("href", /^https:\/\/news\.example\.com\//);
  await expect(first).toHaveAttribute("target", "_blank");
  await expect(first).toHaveAttribute("rel", /noopener/);
  await expect(first).toHaveAttribute("rel", /noreferrer/);
  expect(((await first.innerText()) || "").trim().length, "a source is named, not a bare address").toBeGreaterThan(3);
  await expect(dock(xPage).locator(".sources"), "the line says what the links are").toContainText(/Sources?:/);
  await shot(xPage, "web-checked-draft-with-sources");

  // A brand-new account has none of its own writing: the card says why the draft reads like anyone's,
  // with the step that fixes it.
  const voice = dock(xPage).locator(".voice-note");
  await expect(voice).toContainText(/Drafts sound generic until I know how you write/);
  await expect(voice.locator("button.note-btn")).toHaveText("Add writing samples");

  await assertionMustFail("the link check reads the real address", () =>
    expect(first).toHaveAttribute("href", /^https:\/\/nothing\.invalid\//, { timeout: 1_000 }));

  const clicks = await xPage.evaluate(() => window.__xFixture?.submitClicks ?? []);
  expect(clicks, "nobody pressed Post or Reply").toEqual([]);
});

test("on your own profile, one press adds your posts as writing samples", async ({ app, api, gatewayUrl, shot }) => {
  const account = await createVerifiedAccount(api, gatewayUrl, { email: uniqueEmail("ownposts") });
  await installAndSignIn(app, account);
  const xPage = await openX(app, PROFILE_PATH);
  const panel = dock(xPage);
  await panel.open();
  await panel.gear.click();
  await syncClosedRoots(xPage);
  const button = panel.locator("button").filter({ hasText: "Add my posts from this page" });
  await expect(button, "offered on their own profile").toBeVisible();
  await button.click();
  const note = panel.locator(".mat-import .set-help");
  await expect(note).toContainText("Added 3 of your posts", { timeout: 30_000 });
  // The repost on the page is somebody else's writing, so three of the four posts go in.
  await expect(panel.locator(".mat-list .mat-row")).toHaveCount(3);
  await shot(xPage, "own-posts-added-as-writing-samples");

  await button.click();  // pressing again adds nothing twice
  await expect(note).toContainText("3 were already there", { timeout: 30_000 });
  await expect(panel.locator(".mat-list .mat-row")).toHaveCount(3);

  await assertionMustFail("the count check reads the real list", () =>
    expect(panel.locator(".mat-list .mat-row")).toHaveCount(4, { timeout: 1_000 }));
});
