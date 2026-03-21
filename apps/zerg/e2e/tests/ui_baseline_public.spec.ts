import { test, expect, type Page } from './fixtures';
import { PUBLIC_PAGES } from './helpers/page-list';

async function waitForPublicPageReady(page: Page) {
  await page.waitForLoadState('load');
  await page.evaluate(async () => {
    if (document.fonts?.ready) {
      try {
        await document.fonts.ready;
      } catch {
        // Ignore font readiness errors and fall back to loaded markup.
      }
    }

    await Promise.all(
      Array.from(document.images)
        .filter((image) => !image.complete)
        .map(
          (image) =>
            new Promise<void>((resolve) => {
              image.addEventListener('load', () => resolve(), { once: true });
              image.addEventListener('error', () => resolve(), { once: true });
            }),
        ),
    );

    await Promise.all(
      Array.from(document.querySelectorAll('video')).map(
        (video) =>
          new Promise<void>((resolve) => {
            const finish = () => resolve();
            if (video.readyState >= 2 || video.error) {
              finish();
              return;
            }

            const timeoutId = window.setTimeout(finish, 1500);
            const done = () => {
              clearTimeout(timeoutId);
              finish();
            };

            video.addEventListener('loadeddata', done, { once: true });
            video.addEventListener('error', done, { once: true });
          }),
      ),
    );

    for (const video of Array.from(document.querySelectorAll('video'))) {
      try {
        video.pause();
        video.currentTime = 0;
      } catch {
        // Best-effort only; some browsers reject currentTime assignment early.
      }
      video.removeAttribute('controls');
    }
  });
}

async function captureBaseline(page: Page, path: string, name: string) {
  await page.goto(path);
  await waitForPublicPageReady(page);
  await expect(page).toHaveScreenshot(`${name}.png`, {
    fullPage: true,
    animations: 'disabled',
    maxDiffPixelRatio: 0.02,
  });
}

test.describe('UI baseline: public pages', () => {
  for (const pageDef of PUBLIC_PAGES) {
    test(`baseline: ${pageDef.name}`, async ({ page }) => {
      await captureBaseline(page, pageDef.path, pageDef.name);
    });
  }
});
