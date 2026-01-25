/**
 * Swarm Ops Signal Tests - Core Suite
 *
 * Validates that Swarm Ops surfaces real signal for recent runs.
 */

import { test, expect } from '../fixtures';
import { resetDatabase } from '../test-utils';

test.beforeEach(async ({ request }) => {
  await resetDatabase(request);
});

test.describe('Swarm Ops - Core', () => {
  test('shows signal and last event for a recent run', async ({ page, request }) => {
    test.setTimeout(60000);

    const startTime = Date.now();
    const message = 'Check disk space on cube';

    const chatPromise = request.post('/api/jarvis/chat', {
      data: {
        message,
        message_id: crypto.randomUUID(),
        model: 'gpt-scripted',
      },
    });
    chatPromise.catch(() => {});

    let signalText: string | null = null;
    let statusText: string | null = null;
    await expect
      .poll(async () => {
        const runsRes = await request.get('/api/jarvis/runs?limit=10');
        if (!runsRes.ok()) return false;
        const runs = (await runsRes.json()) as Array<{
          created_at: string;
          status?: string | null;
          signal?: string | null;
          summary?: string | null;
        }>;

        const candidate = runs.find((run) => {
          const createdAt = Date.parse(run.created_at);
          return Number.isFinite(createdAt) && createdAt >= startTime - 2000;
        });

        if (!candidate) return false;

        signalText = candidate.signal || candidate.summary || null;
        statusText = candidate.status ?? null;

        if (!signalText || signalText.length === 0) return false;

        const terminal = statusText === 'success' || statusText === 'failed' || statusText === 'cancelled';
        const hasFinalSignal = /cube is at|task completed|ok/i.test(signalText);

        return terminal && hasFinalSignal;
      }, {
        timeout: 60000,
        intervals: [500, 1000, 2000, 5000],
      })
      .toBeTruthy();

    if (!signalText) {
      throw new Error('Expected signal text to be available');
    }

    const signalSnippet = signalText.slice(0, 24);

    await page.goto('/swarm');

    await page.getByRole('button', { name: 'All' }).click();

    const runItem = page.locator('.swarm-ops-item', { hasText: signalSnippet });
    await expect(runItem).toBeVisible({ timeout: 30000 });
    await expect(runItem).toContainText('Last:', { timeout: 30000 });

    await runItem.click();

    const detailPanel = page.locator('.swarm-ops-detail');
    await expect(detailPanel).toContainText('Signal', { timeout: 10000 });
    await expect(detailPanel).toContainText(signalSnippet, { timeout: 10000 });
    await expect(detailPanel).toContainText('Last event', { timeout: 10000 });
  });
});
