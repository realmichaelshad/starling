/**
 * Render the brand images from their HTML sources.
 *
 *     node brand/source/render.mjs
 *
 * Writes brand/web/og.png (1200x630) from social-image.html, and the X banner, X profile image and
 * Chrome Web Store promo tile from brand-tiles.html into this folder. It uses the Playwright that is
 * already installed for the browser journeys (e2e/browser); nothing is downloaded, and the pages
 * load nothing but local files (the site's own fonts, grain and wordmark).
 */
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, "..", "..");
const { chromium } = await import(
  pathToFileURL(path.join(repo, "e2e", "browser", "node_modules", "playwright", "index.mjs")).href
);

const browser = await chromium.launch();
try {
  const page = await browser.newPage({ viewport: { width: 1600, height: 1300 }, deviceScaleFactor: 1 });
  const outside = [];
  page.on("request", request => { if (!request.url().startsWith("file:")) outside.push(request.url()); });

  await page.setViewportSize({ width: 1200, height: 630 });
  await page.goto(pathToFileURL(path.join(here, "social-image.html")).href);
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(repo, "brand", "web", "og.png"), type: "png" });
  console.log("wrote brand/web/og.png");

  await page.setViewportSize({ width: 1600, height: 1300 });
  await page.goto(pathToFileURL(path.join(here, "brand-tiles.html")).href);
  await page.evaluate(() => document.fonts.ready);
  for (const [id, file] of [
    ["banner", "x-banner-1500x500.png"],
    ["profile", "x-profile-400x400.png"],
    ["promo", "store-promo-tile-440x280.png"],
  ]) {
    await page.locator(`#${id}`).screenshot({ path: path.join(here, file), type: "png" });
    console.log(`wrote brand/source/${file}`);
  }
  if (outside.length) throw new Error(`the brand pages asked for something that is not a local file: ${outside.join(", ")}`);
} finally {
  await browser.close();
}
