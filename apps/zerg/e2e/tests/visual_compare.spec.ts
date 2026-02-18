/**
 * Visual comparison with LLM triage.
 *
 * Captures fresh screenshots of all app pages, then compares against
 * existing Playwright baselines using pixelmatch + optional Gemini triage.
 *
 * This catches semantic visual regressions (color catastrophes, broken layout)
 * that pixel-perfect baselines miss during updates.
 *
 * Run: make qa-visual-compare
 * Skip LLM: SKIP_LLM=1 make qa-visual-compare
 */

import { test, expect, type Page } from './fixtures';
import { waitForPageReady } from './helpers/ready-signals';
import { APP_PAGES, type PageDef } from './helpers/page-list';
import { resetDatabase } from './test-utils';
import { execSync } from 'child_process';
import path from 'path';
import fs from 'fs';

const BASELINE_DIR = path.resolve(import.meta.dir, 'ui_baseline_app.spec.ts-snapshots');
const CURRENT_DIR = path.resolve(import.meta.dir, '../test-results/visual-compare-current');
const OUTPUT_DIR = path.resolve(import.meta.dir, '../test-results/visual-compare-results');
const SCRIPT_PATH = path.resolve(import.meta.dir, '../../../../scripts/visual-compare.ts');

async function waitForAppReady(page: Page, mode: PageDef['ready']) {
  if (mode === 'page') {
    await waitForPageReady(page, { timeout: 20000 });
    return;
  }
  if (mode === 'settings') {
    await waitForPageReady(page, { timeout: 20000 });
    await expect(page.locator('.settings-page-container')).toBeVisible();
    await expect(page.locator('form.profile-form')).toBeVisible();
  }
  if (mode === 'domcontent') {
    await page.waitForLoadState('domcontentloaded');
  }
}

test.beforeEach(async ({ request }) => {
  await resetDatabase(request);
});

test.describe('Visual comparison: LLM-triaged', () => {
  test('capture and compare all app pages', async ({ page }) => {
    // Skip if baselines don't exist yet
    if (!fs.existsSync(BASELINE_DIR)) {
      test.skip(true, 'No baselines yet — run make qa-ui-baseline-update first');
      return;
    }

    // Capture fresh screenshots
    fs.mkdirSync(CURRENT_DIR, { recursive: true });

    for (const pageDef of APP_PAGES) {
      await page.goto(pageDef.path);
      await waitForAppReady(page, pageDef.ready);
      const screenshot = await page.screenshot({ fullPage: true, animations: 'disabled' });
      // Match baseline naming: {name}-chromium-darwin.png
      fs.writeFileSync(path.join(CURRENT_DIR, `${pageDef.name}-chromium-darwin.png`), screenshot);
    }

    // Run comparison engine
    const skipLlm = process.env.SKIP_LLM === '1' ? '--skip-llm' : '';
    const cmd = `bun run ${SCRIPT_PATH} --baseline-dir ${BASELINE_DIR} --current-dir ${CURRENT_DIR} --output-dir ${OUTPUT_DIR} --json ${skipLlm}`.trim();

    let stdout: string;
    try {
      stdout = execSync(cmd, {
        encoding: 'utf8',
        timeout: 180000,
        cwd: path.resolve(import.meta.dir, '../../../..'),
      });
    } catch (err: unknown) {
      // execSync throws on non-zero exit. Exit code 1 = failures, still has stdout.
      const execErr = err as { stdout?: string; stderr?: string; status?: number };
      if (execErr.status === 1 && execErr.stdout) {
        stdout = execErr.stdout;
      } else {
        throw new Error(`visual-compare.ts error: ${execErr.stderr || String(err)}`);
      }
    }

    const report = JSON.parse(stdout);

    // Attach full report as artifact
    await test.info().attach('visual-compare-report', {
      body: JSON.stringify(report, null, 2),
      contentType: 'application/json',
    });

    // Attach diff images for failing pages
    for (const p of report.pages) {
      if (p.diff_image_path && fs.existsSync(p.diff_image_path)) {
        await test.info().attach(`diff-${p.name}`, {
          path: p.diff_image_path,
          contentType: 'image/png',
        });
      }
    }

    // Fail if any page has visual regression
    const failures = report.pages.filter((p: { verdict: string }) => p.verdict === 'fail');
    if (failures.length > 0) {
      const details = failures
        .map((f: { name: string; diff_ratio: number; llm_triage?: { explanation: string } }) =>
          `  ${f.name}: ${(f.diff_ratio * 100).toFixed(2)}% diff${f.llm_triage ? ` — ${f.llm_triage.explanation}` : ''}`,
        )
        .join('\n');
      expect(failures).toHaveLength(0);
      // Above line fails the test; this is a fallback message
      throw new Error(`Visual regressions detected:\n${details}`);
    }
  });
});
