import { expect, test } from "@playwright/test";
import { waitForElementPaint } from "./cohort-journey-helpers";

test("captures buffered and post-boundary element paints", async ({ page }) => {
  const navigationStartedAt = Date.now();
  await page.setContent('<main><div elementtiming="buffered-marker">Buffered paint</div></main>');
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));

  const bufferedPaintAt = await waitForElementPaint(page, "buffered-marker", navigationStartedAt);
  expect(bufferedPaintAt).toBeGreaterThanOrEqual(navigationStartedAt - 25);

  const appendStartedAt = Date.now();
  const pendingPaint = waitForElementPaint(page, "appended-marker", appendStartedAt);
  await page.evaluate(() => {
    const element = document.createElement("div");
    element.setAttribute("elementtiming", "appended-marker");
    element.textContent = "Appended paint";
    document.querySelector("main")?.append(element);
  });

  const appendedPaintAt = await pendingPaint;
  expect(appendedPaintAt).toBeGreaterThanOrEqual(appendStartedAt - 25);
});
