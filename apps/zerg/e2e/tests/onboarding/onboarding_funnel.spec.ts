import fs from 'fs';
import path from 'path';
import { test, expect } from '@playwright/test';

type OnboardingCTA = {
  label: string;
  selector: string;
};

type OnboardingContract = {
  primary_route?: string;
  cta_buttons?: OnboardingCTA[];
};

function findReadme(startDir: string): string {
  let dir = startDir;
  for (let i = 0; i < 8; i += 1) {
    const candidate = path.join(dir, 'README.md');
    if (fs.existsSync(candidate)) return candidate;
    const parent = path.dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  throw new Error('README.md not found for onboarding contract');
}

function loadOnboardingContract(): OnboardingContract {
  const readmePath = findReadme(__dirname);
  const content = fs.readFileSync(readmePath, 'utf8');
  const pattern = /<!-- onboarding-contract:start -->\s*```json\s*([\s\S]*?)\s*```\s*<!-- onboarding-contract:end -->/;
  const match = content.match(pattern);
  if (!match) {
    throw new Error('Onboarding contract block missing in README.md');
  }
  return JSON.parse(match[1].trim());
}

test.describe('Onboarding Funnel (Docs-as-Source)', () => {
  test('README contract matches timeline funnel', async ({ page }) => {
    const contract = loadOnboardingContract();
    const primaryRoute = contract.primary_route || '/timeline';

    await page.goto(primaryRoute);
    await page.waitForSelector('[data-ready="true"]', { timeout: 15000 });

    const ctas = contract.cta_buttons || [];
    for (const cta of ctas) {
      await expect(page.locator(cta.selector)).toBeVisible();
    }

    const demoCTA = ctas.find((cta) => cta.label.toLowerCase().includes('demo'));
    if (demoCTA) {
      await page.locator(demoCTA.selector).click();
      await expect(page.locator('.session-card')).toBeVisible({ timeout: 15000 });
    }
  });
});
