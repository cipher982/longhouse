import { test, expect, type Page } from './fixtures';
import { PUBLIC_PAGES } from './helpers/page-list';
import { getPlatformScopedSnapshotName, installDeterministicVisualFonts } from './helpers/visual-baseline';

async function waitForPublicPageReady(page: Page) {
  await page.waitForLoadState('load');
  await page.evaluate(async () => {
    const nextFrame = () =>
      new Promise<void>((resolve) => {
        window.requestAnimationFrame(() => resolve());
      });
    const getScrollHeight = () =>
      Math.max(
        document.documentElement?.scrollHeight ?? 0,
        document.body?.scrollHeight ?? 0,
        document.scrollingElement?.scrollHeight ?? 0,
        document.documentElement?.offsetHeight ?? 0,
        document.body?.offsetHeight ?? 0,
      );

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

    // The landing/footer path can grow for a few frames after fonts and media
    // report ready on Linux CI. Wait for document height to stop changing so
    // full-page screenshots don't capture a half-settled footer.
    let stableFrames = 0;
    let previousHeight = -1;
    const deadline = performance.now() + 4000;
    while (performance.now() < deadline) {
      await nextFrame();
      const currentHeight = getScrollHeight();
      if (currentHeight === previousHeight) {
        stableFrames += 1;
        if (stableFrames >= 8) {
          break;
        }
      } else {
        previousHeight = currentHeight;
        stableFrames = 0;
      }
    }

    await nextFrame();
    await nextFrame();
  });
}

async function captureBaseline(page: Page, path: string, name: string) {
  await page.goto(path);
  await installDeterministicVisualFonts(page);
  await waitForPublicPageReady(page);
  const screenshot = await page.screenshot({
    fullPage: true,
    animations: 'disabled',
  });
  expect(screenshot).toMatchSnapshot(`${getPlatformScopedSnapshotName(name)}.png`, {
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
