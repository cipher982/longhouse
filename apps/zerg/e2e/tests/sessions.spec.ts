/**
 * Sessions Timeline E2E Tests
 *
 * Tests the agent sessions list and detail pages.
 * Note: These tests require sessions to be present in the database.
 * In a fresh test DB, the empty state will be shown.
 */

import { randomUUID } from 'crypto';
import { test, expect, type Page } from './fixtures';

async function ensureDemoProviders(page: Page): Promise<void> {
  const providerSelect = page.locator('select[aria-label="provider"]');
  const hasClaude = await providerSelect.locator('option[value="claude"]').count();
  if (hasClaude > 0) {
    return;
  }

  const loadDemo = page.getByRole('button', { name: /Load demo/i });
  if (await loadDemo.isVisible()) {
    await loadDemo.click();
  }

  await expect(providerSelect.locator('option[value="claude"]')).toHaveCount(1, { timeout: 15000 });
}

test.describe('Sessions Page', () => {
  test('Sessions tab renders and shows list or empty state', async ({ page }) => {
    // Navigate to timeline (sessions)
    await page.goto('/timeline');

    // Wait for page to be ready
    await page.waitForSelector('[data-ready="true"]', { timeout: 10000 });

    // The header nav should be visible with Sessions tab
    await expect(page.locator('.header-nav')).toBeVisible();
    await expect(page.locator('.nav-tab:has-text("Timeline")')).toBeVisible();

    // Should show either sessions list or empty state
    const hasSessions = await page.locator('.session-card').count() > 0;
    const hasEmptyState = await page.locator('.timeline-empty').isVisible();

    expect(hasSessions || hasEmptyState).toBe(true);
  });

  test('Filter bar is visible and interactive', async ({ page }) => {
    await page.goto('/timeline');
    await page.waitForSelector('[data-ready="true"]', { timeout: 10000 });

    // Filter bar should be visible
    const filterBar = page.locator('.sessions-filter-bar');
    await expect(filterBar).toBeVisible();

    // Filter selects should be present
    await expect(filterBar.locator('select').first()).toBeVisible();

    // Search input should be present
    await expect(filterBar.locator('input[type="search"]')).toBeVisible();
  });

  test('Filter by provider updates URL', async ({ page }) => {
    await page.goto('/timeline');
    await page.waitForSelector('[data-ready="true"]', { timeout: 10000 });

    // Select a provider filter
    await ensureDemoProviders(page);
    const providerSelect = page.locator('select[aria-label="provider"]');
    await providerSelect.selectOption('claude');

    // URL should update with provider param
    await expect(page).toHaveURL(/provider=claude/);
  });

  test('Search input triggers debounced query', async ({ page }) => {
    await page.goto('/timeline');
    await page.waitForSelector('[data-ready="true"]', { timeout: 10000 });

    // Type in search
    const searchInput = page.locator('input[type="search"]');
    await searchInput.fill('test query');

    // URL should include query param (auto-polls for debounce)
    await expect(page).toHaveURL(/query=test\+query|query=test%20query/);
  });

  test('Search results show snippet and jump to matching event', async ({ page, request }) => {
    const sessionId = randomUUID();
    const timestamp = new Date().toISOString();
    const magicToken = 'krypton-needle';

    const ingest = await request.post('/api/agents/ingest', {
      data: {
        id: sessionId,
        provider: 'claude',
        environment: 'test',
        project: 'fts-e2e',
        device_id: 'e2e-device',
        cwd: '/tmp',
        git_repo: null,
        git_branch: null,
        started_at: timestamp,
        events: [
          {
            role: 'user',
            content_text: `Find ${magicToken} in this session`,
            timestamp,
            source_path: '/tmp/session.jsonl',
            source_offset: 0,
          },
        ],
      },
    });

    expect(ingest.ok()).toBe(true);

    await page.goto('/timeline');
    await page.waitForSelector('[data-ready="true"]', { timeout: 10000 });

    const searchInput = page.locator('input[type="search"]');
    await searchInput.fill(magicToken);
    await expect(page).toHaveURL(new RegExp(`query=${magicToken}`));

    const sessionCard = page.locator('.session-card', { hasText: 'fts-e2e' }).first();
    await expect(sessionCard).toBeVisible();

    const snippet = sessionCard.locator('.session-card-snippet');
    await expect(snippet).toContainText(magicToken);
    await expect(snippet.locator('mark.search-highlight')).toBeVisible();

    await sessionCard.click();

    await expect(page).toHaveURL(new RegExp(`/timeline/${sessionId}.*event_id=`));
    const highlight = page.locator('.event-highlight');
    await expect(highlight).toContainText(magicToken);
  });

  test('Clear filters button removes all filters', async ({ page }) => {
    // Navigate with pre-set filters
    await page.goto('/timeline?provider=claude&project=zerg');
    await page.waitForSelector('[data-ready="true"]', { timeout: 10000 });

    // Clear button should be visible
    const clearButton = page.getByRole('button', { name: 'Clear', exact: true });
    await expect(clearButton).toBeVisible();

    // Click clear
    await clearButton.click();

    // URL should no longer have filter params
    await expect(page).toHaveURL('/timeline');
  });
});

test.describe('Session Detail Page', () => {
  test('Shows error for invalid session ID', async ({ page }) => {
    // Navigate to a non-existent session
    await page.goto('/timeline/00000000-0000-0000-0000-000000000000');
    await page.waitForSelector('[data-ready="true"]', { timeout: 10000 });

    // Should show error state
    await expect(page.locator('.ui-empty-state')).toBeVisible();
    await expect(page.locator('text=Error loading session')).toBeVisible();

    // Back button should be visible
    const backButton = page.locator('button:has-text("Back")');
    await expect(backButton).toBeVisible();
  });

  test('Back button navigates to sessions list', async ({ page }) => {
    // Navigate to invalid session to get error state
    await page.goto('/timeline/00000000-0000-0000-0000-000000000000');
    await page.waitForSelector('[data-ready="true"]', { timeout: 10000 });

    // Click back button
    await page.locator('button:has-text("Back")').click();

    // Should be back on sessions list
    await expect(page).toHaveURL('/timeline');
  });
});

test.describe('Sessions Navigation', () => {
  test('Sessions tab in nav links to /sessions', async ({ page }) => {
    await page.goto('/dashboard');
    await page.waitForSelector('.header-nav', { timeout: 10000 });

    // Click Timeline tab
    await page.locator('.nav-tab:has-text("Timeline")').click();

    // Should navigate to timeline page
    await expect(page).toHaveURL('/timeline');
  });
});
