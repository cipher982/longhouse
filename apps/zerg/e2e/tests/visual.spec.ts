import { test, expect } from './fixtures';
import fs from 'fs';
import pixelmatch from 'pixelmatch';
import { PNG } from 'pngjs';

const routes = ['/'];

routes.forEach((route) => {
  test(`visual regression – ${route}`, async ({ page }, testInfo) => {
    await page.goto(route);
    await page.waitForLoadState('networkidle');

    const screenshot = await page.screenshot({ fullPage: true });
    const dir = 'visual-baseline';
    const basePath = `${dir}/${route.replace(/\//g, '_') || 'home'}.png`;

    if (!fs.existsSync(dir)) fs.mkdirSync(dir);

    if (!fs.existsSync(basePath)) {
      fs.writeFileSync(basePath, screenshot);
      testInfo.skip(true, 'Baseline created – skipping comparison.');
      return;
    }

    const baseline = PNG.sync.read(fs.readFileSync(basePath));
    const current = PNG.sync.read(screenshot);
    const { width, height } = baseline;
    const diff = new PNG({ width, height });

    const numDiff = pixelmatch(
      baseline.data,
      current.data,
      diff.data,
      width,
      height,
      { threshold: 0.05 }
    );

    const diffRatio = numDiff / (width * height);
    if (diffRatio > 0.001) {
      const diffPath = `${dir}/${route.replace(/\//g, '_')}-diff.png`;
      fs.writeFileSync(diffPath, PNG.sync.write(diff));
    }

    expect(diffRatio).toBeLessThan(0.001);
  });
});
