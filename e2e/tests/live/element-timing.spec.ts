import { expect, test } from "@playwright/test";
import { waitForElementPaint } from "./cohort-journey-helpers";

test("captures buffered and post-boundary element paints", async ({ page }) => {
  const navigationStartedAt = Date.now();
  await page.setContent(`
    <main>
      <article class="production-like-row">
        <div style="display: flex"><span elementtiming="buffered-marker">Buffered paint</span></div>
      </article>
    </main>
  `);
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));

  const bufferedPaintAt = await waitForElementPaint(page, "buffered-marker", navigationStartedAt);
  expect(bufferedPaintAt).toBeGreaterThanOrEqual(navigationStartedAt);

  const appendStartedAt = Date.now();
  const pendingPaint = waitForElementPaint(page, "appended-marker", appendStartedAt);
  await page.evaluate(() => {
    const container = document.createElement("div");
    container.style.display = "flex";
    const element = document.createElement("span");
    element.setAttribute("elementtiming", "appended-marker");
    element.textContent = "Appended paint";
    container.append(element);
    document.querySelector("main")?.append(container);
  });

  const appendedPaintAt = await pendingPaint;
  expect(appendedPaintAt).toBeGreaterThanOrEqual(appendStartedAt);

  await page.setContent('<div elementtiming="nested-only-marker"><div>Nested paint</div></div>');
  await expect(waitForElementPaint(page, "nested-only-marker", Date.now(), 250)).rejects.toThrow(
    "paint_evidence_unavailable",
  );
});
