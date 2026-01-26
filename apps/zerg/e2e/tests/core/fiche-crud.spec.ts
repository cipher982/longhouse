/**
 * Fiche CRUD Tests - Core Suite
 *
 * Tests basic fiche create/read operations.
 *
 * CORE SUITE: 0 skipped, 0 flaky, retries: 0
 */

import { test, expect, type Page } from '../fixtures';
import { resetDatabase } from '../test-utils';

// Reset DB before each test for clean state
// Uses strict reset that throws on failure to fail fast
test.beforeEach(async ({ request }) => {
  await resetDatabase(request);
});

test.describe('Fiche CRUD - Core', () => {
  test('create fiche - fiche appears in dashboard', async ({ page }) => {
    await page.goto('/');

    const createBtn = page.locator('[data-testid="create-fiche-btn"]');
    await expect(createBtn).toBeVisible({ timeout: 10000 });
    await expect(createBtn).toBeEnabled({ timeout: 5000 });

    const ficheRows = page.locator('tr[data-fiche-id]');
    const initialCount = await ficheRows.count();

    // Wait for API response
    await Promise.all([
      page.waitForResponse(
        (r) => r.url().includes('/api/fiches') && r.request().method() === 'POST' && r.status() === 201,
        { timeout: 10000 }
      ),
      createBtn.click(),
    ]);

    // Use polling to wait for new row
    await expect.poll(async () => await ficheRows.count(), { timeout: 10000 }).toBe(initialCount + 1);

    const newRow = ficheRows.first();
    await expect(newRow).toBeVisible();

    const ficheId = await newRow.getAttribute('data-fiche-id');
    expect(ficheId).toBeTruthy();
    expect(ficheId).toMatch(/^\d+$/);
  });

  test('backend auto-generates placeholder name', async ({ request }) => {
    // Create fiche via API (no name field sent)
    const response = await request.post('/api/fiches', {
      data: {
        system_instructions: 'Test instructions',
        task_instructions: 'Test task',
        model: 'gpt-5.2',
      },
    });

    expect(response.ok()).toBeTruthy();
    const fiche = await response.json();

    // Should have auto-generated name "New Fiche"
    expect(fiche.name).toBe('New Fiche');
  });

  test('idempotency key prevents duplicate creation', async ({ request }) => {
    const idempotencyKey = `test-${Date.now()}-${Math.random()}`;

    // Create fiche with idempotency key
    const response1 = await request.post('/api/fiches', {
      headers: { 'Idempotency-Key': idempotencyKey },
      data: {
        system_instructions: 'Test instructions',
        task_instructions: 'Test task',
        model: 'gpt-5.2',
      },
    });
    expect(response1.ok()).toBeTruthy();
    const fiche1 = await response1.json();

    // Retry with same idempotency key (simulates double-click)
    const response2 = await request.post('/api/fiches', {
      headers: { 'Idempotency-Key': idempotencyKey },
      data: {
        system_instructions: 'Different instructions',
        task_instructions: 'Different task',
        model: 'gpt-5.2',
      },
    });
    expect(response2.ok()).toBeTruthy();
    const fiche2 = await response2.json();

    // Should return the SAME fiche (not create a new one)
    expect(fiche2.id).toBe(fiche1.id);
    expect(fiche2.name).toBe(fiche1.name);
  });
});
