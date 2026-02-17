/**
 * Sessions Timeline E2E Tests
 *
 * Tests the agent sessions list and detail pages.
 * Note: These tests require sessions to be present in the database.
 * In a fresh test DB, the empty state will be shown.
 */

import { randomUUID } from 'crypto';
import { test, expect, type Page } from '../fixtures';

async function ensureDemoProviders(page: Page): Promise<void> {
  // Hero empty state has no toolbar — seed demos first if visible
  const heroEmpty = page.locator('.sessions-hero-empty');
  if (await heroEmpty.isVisible({ timeout: 2000 }).catch(() => false)) {
    const loadDemo = page.getByRole('button', { name: /Load demo/i });
    await loadDemo.click();
    // Wait for toolbar to appear (hero state is replaced by normal timeline)
    await page.waitForSelector('.sessions-toolbar', { timeout: 15000 });
  }

  // Open filter panel if collapsed
  const filterPanel = page.locator('#filter-panel');
  if (!(await filterPanel.isVisible().catch(() => false))) {
    const toggleBtn = page.locator('.sessions-filter-toggle');
    if (await toggleBtn.isVisible()) {
      await toggleBtn.click();
    }
  }

  const providerSelect = page.locator('select[aria-label="provider"]');
  const hasClaude = await providerSelect.locator('option[value="claude"]').count();
  if (hasClaude > 0) {
    return;
  }

  // Fallback: try loading demos if provider not yet available
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

    // Should show either sessions list or hero empty state
    const hasSessions = await page.locator('.session-card').count() > 0;
    const hasHeroEmpty = await page.locator('.sessions-hero-empty').isVisible();

    expect(hasSessions || hasHeroEmpty).toBe(true);
  });

  test('Filter bar is visible and interactive', async ({ page }) => {
    await page.goto('/timeline');
    await page.waitForSelector('[data-ready="true"]', { timeout: 10000 });

    // Seed demos first so toolbar is visible (hero state has no toolbar)
    await ensureDemoProviders(page);

    // Toolbar should be visible
    const toolbar = page.locator('.sessions-toolbar');
    await expect(toolbar).toBeVisible();

    // Search input should be present on the toolbar
    await expect(toolbar.locator('input[type="search"]')).toBeVisible();

    // Filter toggle should be present
    await expect(page.locator('.sessions-filter-toggle')).toBeVisible();

    // Filter panel should be open (ensureDemoProviders opened it)
    const filterPanel = page.locator('#filter-panel');
    await expect(filterPanel).toBeVisible();
    await expect(filterPanel.locator('select').first()).toBeVisible();
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

    // If hero state, seed demos first so search input is on toolbar
    const heroEmpty = page.locator('.sessions-hero-empty');
    if (await heroEmpty.isVisible({ timeout: 2000 }).catch(() => false)) {
      await page.getByRole('button', { name: /Load demo/i }).click();
      await page.waitForSelector('.sessions-toolbar', { timeout: 15000 });
    }

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
        environment: 'development',
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
    await page.waitForSelector('body[data-ready="true"]', { timeout: 10000 });
    const highlight = page.locator('.event-highlight');
    await expect(highlight).toContainText(magicToken, { timeout: 15000 });
  });

  test('Clear filters button removes all filters', async ({ page }) => {
    // Navigate with pre-set filters — filtersOpen auto-opens from URL params
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
