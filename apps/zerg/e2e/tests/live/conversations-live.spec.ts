import { test, expect } from './fixtures';
import type { Page } from '@playwright/test';

const BENIGN_CONSOLE_PATTERNS = [
  /Download the React DevTools/,
  /\[HMR\]/,
  /Failed to load resource.*favicon/i,
  /Content Security Policy/,
];

function attachErrorCollectors(page: Page): {
  consoleErrors: string[];
  serverErrors: string[];
} {
  const consoleErrors: string[] = [];
  const serverErrors: string[] = [];

  page.on('console', (msg) => {
    if (msg.type() === 'error') {
      const text = msg.text();
      if (!BENIGN_CONSOLE_PATTERNS.some((pattern) => pattern.test(text))) {
        consoleErrors.push(text);
      }
    }
  });

  page.on('response', (response) => {
    const url = response.url();
    const status = response.status();
    if (url.includes('/api/') && (status >= 500 || (status >= 400 && status !== 401))) {
      serverErrors.push(`${status} ${url}`);
    }
  });

  return { consoleErrors, serverErrors };
}

async function failWithScreenshot(page: Page, testName: string, message: string): Promise<never> {
  const path = `/tmp/${testName.replace(/\s+/g, '-')}.png`;
  await page.screenshot({ path, fullPage: false }).catch(() => {});
  throw new Error(`${message}\nScreenshot saved: ${path}`);
}

test('conversations inbox loads and renders canonical email surface', async ({ context }) => {
  test.setTimeout(20_000);

  const page = await context.newPage();
  const { consoleErrors, serverErrors } = attachErrorCollectors(page);
  const authErrors: string[] = [];

  page.on('response', (response) => {
    const url = response.url();
    if (url.includes('/api/conversations') && (response.status() === 401 || response.status() === 403)) {
      authErrors.push(`${response.status()} ${url}`);
    }
  });

  await page.goto('/conversations', { waitUntil: 'domcontentloaded' });
  await expect(page).toHaveURL(/\/conversations(\?.*)?$/, { timeout: 10_000 });
  await page.locator('body[data-ready="true"]').waitFor({ timeout: 12_000 });

  await expect(page.getByRole('heading', { name: 'Inbox' })).toBeVisible();
  await expect(page.getByLabel('Search conversations')).toBeVisible();

  if (authErrors.length > 0) {
    await failWithScreenshot(
      page,
      'conversations-auth',
      `Auth failures on conversations route: ${authErrors.join(', ')}`,
    );
  }

  if (serverErrors.length > 0) {
    await failWithScreenshot(
      page,
      'conversations-500',
      `Server errors on conversations route: ${serverErrors.join(', ')}`,
    );
  }

  if (consoleErrors.length > 0) {
    await failWithScreenshot(
      page,
      'conversations-console',
      `JS errors on conversations route: ${consoleErrors.join(' | ')}`,
    );
  }

  const items = page.locator('[data-testid^="conversation-item-"]');
  const itemCount = await items.count();
  if (itemCount > 0) {
    await expect(items.first()).toBeVisible();
    await expect(page.locator('[data-testid="conversation-thread"]')).toContainText(/messages/i);
    await expect(page.getByLabel('Reply')).toBeVisible();
  } else {
    await expect(page.getByText('No email threads yet')).toBeVisible();
    await expect(page.getByText('Select a conversation')).toBeVisible();
  }

  await page.close();
});
