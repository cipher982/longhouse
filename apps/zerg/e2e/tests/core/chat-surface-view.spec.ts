import { test, expect } from '../fixtures';

test.describe('Oikos Surface View', () => {
  test('all-activity toggle reloads history and shows Telegram badge', async ({ page }) => {
    const historyUrls: string[] = [];

    await page.route('**/api/oikos/history**', async (route) => {
      const url = route.request().url();
      historyUrls.push(url);
      const parsed = new URL(url);
      const isAllView = parsed.searchParams.get('view') === 'all';

      const payload = isAllView
        ? {
            messages: [
              {
                role: 'user',
                content: 'Web question',
                timestamp: '2026-03-04T12:00:00Z',
                origin_surface_id: 'web',
                delivery_surface_id: 'web',
                visibility: 'surface-local',
              },
              {
                role: 'assistant',
                content: 'Telegram follow-up sent.',
                timestamp: '2026-03-04T12:00:01Z',
                origin_surface_id: 'telegram',
                delivery_surface_id: 'telegram',
                visibility: 'cross-surface',
              },
            ],
            total: 2,
          }
        : {
            messages: [
              {
                role: 'user',
                content: 'Web question',
                timestamp: '2026-03-04T12:00:00Z',
                origin_surface_id: 'web',
                delivery_surface_id: 'web',
                visibility: 'surface-local',
              },
            ],
            total: 1,
          };

      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(payload),
      });
    });

    await page.goto('/chat');
    await expect(page.getByTestId('chat-input')).toBeVisible({ timeout: 15000 });

    await expect.poll(() => historyUrls.length, { timeout: 10000 }).toBeGreaterThan(0);
    expect(historyUrls[0]).toContain('surface_id=web');
    expect(historyUrls[0]).not.toContain('view=all');

    const toggle = page.getByTestId('surface-view-toggle');
    await expect(toggle).toHaveText(/Web only/i);
    await toggle.click();

    await expect.poll(() => historyUrls.some((u) => u.includes('view=all')), { timeout: 10000 }).toBeTruthy();
    await expect(toggle).toHaveText(/All activity/i);

    const telegramBadge = page.locator('[data-testid="message-surface-badge"][data-surface-id="telegram"]');
    await expect(telegramBadge).toBeVisible({ timeout: 10000 });
    await expect(telegramBadge).toContainText('Telegram');
  });
});
