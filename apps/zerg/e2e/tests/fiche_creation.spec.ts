import { test, expect } from './fixtures';
import { resetDatabase } from './test-utils';

test.describe('Fiche Creation', () => {
  // Uses strict reset that throws on failure to fail fast
  test.beforeEach(async ({ request }) => {
    await resetDatabase(request);
  });

  test('creates fiches with "New Fiche" placeholder name', async ({ page }) => {
    await page.goto('/');
    await page.waitForLoadState('networkidle');

    // Wait for create button to be ready
    await expect(page.locator('[data-testid="create-fiche-btn"]')).toBeVisible({ timeout: 5000 });

    // Create first fiche with deterministic wait for API response
    await Promise.all([
      page.waitForResponse(
        (r) => r.url().includes('/api/fiches') && r.request().method() === 'POST' && r.status() === 201,
        { timeout: 10000 }
      ),
      page.click('[data-testid="create-fiche-btn"]'),
    ]);

    // Create second fiche with deterministic wait
    await Promise.all([
      page.waitForResponse(
        (r) => r.url().includes('/api/fiches') && r.request().method() === 'POST' && r.status() === 201,
        { timeout: 10000 }
      ),
      page.click('[data-testid="create-fiche-btn"]'),
    ]);

    // Create third fiche with deterministic wait
    await Promise.all([
      page.waitForResponse(
        (r) => r.url().includes('/api/fiches') && r.request().method() === 'POST' && r.status() === 201,
        { timeout: 10000 }
      ),
      page.click('[data-testid="create-fiche-btn"]'),
    ]);

    // Wait for all 3 fiche rows to appear
    await expect(page.locator('#fiches-table-body tr[data-fiche-id]')).toHaveCount(3, { timeout: 10000 });

    // Get all fiche rows
    const ficheRows = page.locator('#fiches-table-body tr[data-fiche-id]');

    // Check fiche names are all "New Fiche"
    const firstFicheName = await ficheRows.nth(0).locator('td[data-label="Name"]').textContent();
    const secondFicheName = await ficheRows.nth(1).locator('td[data-label="Name"]').textContent();
    const thirdFicheName = await ficheRows.nth(2).locator('td[data-label="Name"]').textContent();

    // Should all be "New Fiche"
    expect(firstFicheName).toBe('New Fiche');
    expect(secondFicheName).toBe('New Fiche');
    expect(thirdFicheName).toBe('New Fiche');
  });

  test('backend auto-generates "New Fiche" placeholder name', async ({ request }) => {
    // Create fiche (no name field sent)
    const response = await request.post('/api/fiches', {
      data: {
        system_instructions: 'Test instructions',
        task_instructions: 'Test task',
        model: 'gpt-5.2'
      }
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
        model: 'gpt-5.2'
      }
    });
    expect(response1.ok()).toBeTruthy();
    const fiche1 = await response1.json();

    // Retry with same idempotency key (simulates double-click)
    const response2 = await request.post('/api/fiches', {
      headers: { 'Idempotency-Key': idempotencyKey },
      data: {
        system_instructions: 'Different instructions',
        task_instructions: 'Different task',
        model: 'gpt-5.2'
      }
    });
    expect(response2.ok()).toBeTruthy();
    const fiche2 = await response2.json();

    // Should return the SAME fiche (not create a new one)
    expect(fiche2.id).toBe(fiche1.id);
    expect(fiche2.name).toBe(fiche1.name);
  });
});
