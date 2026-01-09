/**
 * KNOWLEDGE SOURCES E2E TESTS
 *
 * Tests the knowledge sources feature:
 * - Page loads and shows empty state or list
 * - User can add a URL knowledge source
 * - User can see knowledge sources in list
 * - User can search knowledge
 * - User can delete a knowledge source
 *
 * Strategy:
 * - Each test validates ONE invariant
 * - All waits are deterministic (API responses, element states)
 * - No arbitrary timeouts or networkidle waits
 * - Tests are isolated (reset DB per test)
 */

import { test, expect, type Page } from './fixtures';

// Reset DB before each test for clean, isolated state
test.beforeEach(async ({ request }) => {
  await request.post('/admin/reset-database', { data: { reset_type: 'clear_data' } });
});

// ============================================================================
// HELPERS - Reusable, deterministic operations
// ============================================================================

/**
 * Navigate to knowledge sources page and wait for it to load.
 */
async function navigateToKnowledgeSources(page: Page): Promise<void> {
  await page.goto('/settings/knowledge');

  // Wait for either the "Add Source" button (loaded state) or empty state
  await expect(
    page.locator('[data-testid="add-knowledge-source-btn"]')
  ).toBeVisible({ timeout: 15000 });
}

/**
 * Create a URL knowledge source via API for faster test setup.
 */
async function createUrlSourceViaApi(
  request: import('@playwright/test').APIRequestContext,
  name: string,
  url: string
): Promise<{ id: number }> {
  const response = await request.post('/api/knowledge/sources', {
    data: {
      name,
      source_type: 'url',
      config: { url },
    },
  });
  expect(response.ok()).toBeTruthy();
  return response.json();
}

/**
 * Delete a knowledge source via API.
 */
async function deleteSourceViaApi(
  request: import('@playwright/test').APIRequestContext,
  sourceId: number
): Promise<void> {
  const response = await request.delete(`/api/knowledge/sources/${sourceId}`);
  expect(response.status()).toBe(204);
}

// ============================================================================
// SMOKE TESTS - Core knowledge sources functionality
// ============================================================================

test.describe('Knowledge Sources - Core', () => {
  test('page loads and shows empty state when no sources exist', async ({ page }) => {
    await navigateToKnowledgeSources(page);

    // Should show "Add Source" button
    await expect(page.locator('[data-testid="add-knowledge-source-btn"]')).toBeVisible();

    // Should show empty state message (using EmptyState component)
    const emptyStateTitle = page.locator('h3:has-text("No knowledge sources configured yet")');
    await expect(emptyStateTitle).toBeVisible({ timeout: 5000 });
  });

  test('page loads and shows source list when sources exist', async ({ page, request }) => {
    // Create a source via API
    const source = await createUrlSourceViaApi(request, 'Test Docs', 'https://example.com/docs.md');

    await navigateToKnowledgeSources(page);

    // Should show the source card
    const sourceCard = page.locator(`[data-testid="knowledge-source-${source.id}"]`);
    await expect(sourceCard).toBeVisible({ timeout: 10000 });

    // Should show source name
    await expect(sourceCard).toContainText('Test Docs');
  });

  test('source card shows correct status and details', async ({ page, request }) => {
    const source = await createUrlSourceViaApi(request, 'My Documentation', 'https://example.com/api-docs.md');

    await navigateToKnowledgeSources(page);

    const sourceCard = page.locator(`[data-testid="knowledge-source-${source.id}"]`);
    await expect(sourceCard).toBeVisible({ timeout: 10000 });

    // Should show "Pending" status (never synced)
    await expect(sourceCard.locator('text=Pending')).toBeVisible();

    // Should show URL type
    await expect(sourceCard.locator('text=URL')).toBeVisible();

    // Should show Sync Now button
    await expect(sourceCard.locator(`[data-testid="sync-source-${source.id}"]`)).toBeVisible();
  });
});

// ============================================================================
// ADD SOURCE TESTS - Modal workflow
// ============================================================================

test.describe('Knowledge Sources - Add Source', () => {
  test('clicking Add Source opens modal with type selection', async ({ page }) => {
    await navigateToKnowledgeSources(page);

    await page.locator('[data-testid="add-knowledge-source-btn"]').click();

    // Modal should open with type selection
    const modal = page.locator('.modal-container');
    await expect(modal).toBeVisible({ timeout: 5000 });

    // Should show both type options
    await expect(page.locator('[data-testid="source-type-url"]')).toBeVisible();
    // Use more specific selector - the GitHub button contains an h3 with the text
    await expect(modal.locator('h3:has-text("GitHub Repository")')).toBeVisible();
  });

  test('selecting URL type shows URL configuration form', async ({ page }) => {
    await navigateToKnowledgeSources(page);

    await page.locator('[data-testid="add-knowledge-source-btn"]').click();
    await page.locator('[data-testid="source-type-url"]').click();

    // Should show URL form
    await expect(page.locator('[data-testid="url-input"]')).toBeVisible({ timeout: 5000 });

    // Should show name input
    await expect(page.locator('input[placeholder="My Documentation"]')).toBeVisible();

    // Submit button should be disabled without URL
    await expect(page.locator('[data-testid="submit-url-source"]')).toBeDisabled();
  });

  test('can add URL knowledge source via UI', async ({ page }) => {
    await navigateToKnowledgeSources(page);

    // Open modal and select URL type
    await page.locator('[data-testid="add-knowledge-source-btn"]').click();
    await page.locator('[data-testid="source-type-url"]').click();

    // Fill in the URL
    await page.locator('[data-testid="url-input"]').fill('https://httpbin.org/robots.txt');

    // Submit button should now be enabled
    const submitBtn = page.locator('[data-testid="submit-url-source"]');
    await expect(submitBtn).toBeEnabled({ timeout: 5000 });

    // Wait for API response when submitting
    await Promise.all([
      page.waitForResponse(
        (r) => r.url().includes('/api/knowledge/sources') && r.request().method() === 'POST' && r.status() === 201,
        { timeout: 15000 }
      ),
      submitBtn.click(),
    ]);

    // Modal should close
    await expect(page.locator('.modal-container')).not.toBeVisible({ timeout: 5000 });

    // New source should appear in the list
    const sourceCard = page.locator('[data-testid^="knowledge-source-"]').first();
    await expect(sourceCard).toBeVisible({ timeout: 10000 });
  });

  test('can set custom name for URL source', async ({ page }) => {
    await navigateToKnowledgeSources(page);

    await page.locator('[data-testid="add-knowledge-source-btn"]').click();
    await page.locator('[data-testid="source-type-url"]').click();

    // Fill in custom name and URL
    await page.locator('input[placeholder="My Documentation"]').fill('Custom Source Name');
    await page.locator('[data-testid="url-input"]').fill('https://example.com/docs.md');

    // Submit
    await Promise.all([
      page.waitForResponse(
        (r) => r.url().includes('/api/knowledge/sources') && r.request().method() === 'POST' && r.status() === 201,
        { timeout: 15000 }
      ),
      page.locator('[data-testid="submit-url-source"]').click(),
    ]);

    // Source should appear with custom name
    await expect(page.locator('text=Custom Source Name')).toBeVisible({ timeout: 10000 });
  });

  test('can cancel adding source via Cancel button', async ({ page }) => {
    await navigateToKnowledgeSources(page);

    await page.locator('[data-testid="add-knowledge-source-btn"]').click();
    await expect(page.locator('.modal-container')).toBeVisible({ timeout: 5000 });

    // Click Cancel
    await page.locator('button:has-text("Cancel")').click();

    // Modal should close
    await expect(page.locator('.modal-container')).not.toBeVisible({ timeout: 5000 });
  });

  test('can go back from URL form to type selection', async ({ page }) => {
    await navigateToKnowledgeSources(page);

    await page.locator('[data-testid="add-knowledge-source-btn"]').click();
    await page.locator('[data-testid="source-type-url"]').click();

    // Should be on URL form
    await expect(page.locator('[data-testid="url-input"]')).toBeVisible({ timeout: 5000 });

    // Click Back
    await page.locator('button:has-text("Back")').click();

    // Should be back on type selection
    await expect(page.locator('[data-testid="source-type-url"]')).toBeVisible({ timeout: 5000 });
    await expect(page.locator('[data-testid="url-input"]')).not.toBeVisible();
  });
});

// ============================================================================
// SEARCH TESTS - Knowledge search panel
// ============================================================================

test.describe('Knowledge Sources - Search', () => {
  test('search panel appears when sources exist', async ({ page, request }) => {
    // Create a source so search panel shows
    await createUrlSourceViaApi(request, 'Test Docs', 'https://example.com/');

    await navigateToKnowledgeSources(page);

    // Search panel should be visible
    await expect(page.locator('[data-testid="knowledge-search-panel"]')).toBeVisible({ timeout: 10000 });
    await expect(page.locator('[data-testid="knowledge-search-input"]')).toBeVisible();
  });

  test('search panel does not appear when no sources exist', async ({ page }) => {
    await navigateToKnowledgeSources(page);

    // With no sources, we should see empty state, not search panel
    await expect(page.locator('h3:has-text("No knowledge sources configured yet")')).toBeVisible({ timeout: 5000 });
    await expect(page.locator('[data-testid="knowledge-search-panel"]')).not.toBeVisible();
  });

  test('typing less than 2 characters shows hint', async ({ page, request }) => {
    await createUrlSourceViaApi(request, 'Test Docs', 'https://example.com/');

    await navigateToKnowledgeSources(page);

    const searchInput = page.locator('[data-testid="knowledge-search-input"]');
    await expect(searchInput).toBeVisible({ timeout: 10000 });

    // Type single character
    await searchInput.fill('a');

    // Should show hint about minimum characters
    await expect(page.locator('text=at least 2 characters')).toBeVisible({ timeout: 5000 });
  });

  test('search executes with valid query', async ({ page, request }) => {
    await createUrlSourceViaApi(request, 'Test Docs', 'https://example.com/');

    await navigateToKnowledgeSources(page);

    const searchInput = page.locator('[data-testid="knowledge-search-input"]');
    await expect(searchInput).toBeVisible({ timeout: 10000 });

    // Type valid query
    await searchInput.fill('test query');

    // Wait for search API call (debounced)
    await page.waitForResponse(
      (r) => r.url().includes('/api/knowledge/search') && r.request().method() === 'GET',
      { timeout: 10000 }
    );

    // Should show either results or "no results" message
    const hasResults = await page.locator('[data-testid="knowledge-search-results"]').isVisible();
    const hasNoResults = await page.locator('text=No results found').isVisible();

    expect(hasResults || hasNoResults).toBeTruthy();
  });
});

// ============================================================================
// DELETE TESTS - Source deletion
// ============================================================================

test.describe('Knowledge Sources - Delete', () => {
  test('delete button is visible on source card', async ({ page, request }) => {
    const source = await createUrlSourceViaApi(request, 'Delete Test', 'https://example.com/');

    await navigateToKnowledgeSources(page);

    const sourceCard = page.locator(`[data-testid="knowledge-source-${source.id}"]`);
    await expect(sourceCard).toBeVisible({ timeout: 10000 });

    // Delete button should be visible
    await expect(sourceCard.locator('button:has-text("Delete")')).toBeVisible();
  });

  test('clicking delete removes source after confirmation', async ({ page, request }) => {
    const source = await createUrlSourceViaApi(request, 'To Be Deleted', 'https://example.com/');

    await navigateToKnowledgeSources(page);

    const sourceCard = page.locator(`[data-testid="knowledge-source-${source.id}"]`);
    await expect(sourceCard).toBeVisible({ timeout: 10000 });

    // Set up dialog handler for confirmation
    page.on('dialog', (dialog) => dialog.accept());

    // Click delete and wait for API response
    await Promise.all([
      page.waitForResponse(
        (r) => r.url().includes(`/api/knowledge/sources/${source.id}`) && r.request().method() === 'DELETE',
        { timeout: 15000 }
      ),
      sourceCard.locator('button:has-text("Delete")').click(),
    ]);

    // Source should be removed from list
    await expect(sourceCard).not.toBeVisible({ timeout: 10000 });
  });

  test('dismissing delete confirmation keeps source', async ({ page, request }) => {
    const source = await createUrlSourceViaApi(request, 'Keep Me', 'https://example.com/');

    await navigateToKnowledgeSources(page);

    const sourceCard = page.locator(`[data-testid="knowledge-source-${source.id}"]`);
    await expect(sourceCard).toBeVisible({ timeout: 10000 });

    // Set up dialog handler to dismiss
    page.on('dialog', (dialog) => dialog.dismiss());

    // Click delete
    await sourceCard.locator('button:has-text("Delete")').click();

    // Source should still be visible
    await expect(sourceCard).toBeVisible({ timeout: 5000 });
  });
});

// ============================================================================
// SYNC TESTS - Source syncing
// ============================================================================

test.describe('Knowledge Sources - Sync', () => {
  test('sync button triggers sync API call', async ({ page, request }) => {
    const source = await createUrlSourceViaApi(request, 'Sync Test', 'https://httpbin.org/robots.txt');

    await navigateToKnowledgeSources(page);

    const syncBtn = page.locator(`[data-testid="sync-source-${source.id}"]`);
    await expect(syncBtn).toBeVisible({ timeout: 10000 });

    // Click sync and wait for API response
    await Promise.all([
      page.waitForResponse(
        (r) => r.url().includes(`/api/knowledge/sources/${source.id}/sync`) && r.request().method() === 'POST',
        { timeout: 30000 }
      ),
      syncBtn.click(),
    ]);

    // After sync completes, button text should return to "Sync Now"
    // (was "Syncing..." during the operation)
    await expect(syncBtn).toContainText('Sync Now', { timeout: 10000 });
  });

  test('sync button shows syncing state during operation', async ({ page, request }) => {
    const source = await createUrlSourceViaApi(request, 'Sync State Test', 'https://httpbin.org/robots.txt');

    await navigateToKnowledgeSources(page);

    const syncBtn = page.locator(`[data-testid="sync-source-${source.id}"]`);
    await expect(syncBtn).toBeVisible({ timeout: 10000 });

    // Start sync (don't wait for response)
    syncBtn.click();

    // Button should show syncing state
    await expect(syncBtn).toContainText('Syncing', { timeout: 5000 });
  });
});
