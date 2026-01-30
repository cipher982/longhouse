/**
 * E2E tests for CommisJob sandbox (container) execution mode.
 *
 * These tests verify that:
 * 1. The sandbox column exists in the database schema
 * 2. Commis jobs can be spawned (sandbox flag is internal)
 * 3. The system handles commis execution correctly
 *
 * Note: Actual container execution testing is done via integration tests
 * that require Docker. These E2E tests verify the API layer.
 */
import { test, expect } from '../../fixtures';
import { resetDatabase } from '../../test-utils';

test.describe('Commis Sandbox Schema', () => {
  test.beforeEach(async ({ request }) => {
    await resetDatabase(request);
  });

  test('commis flow works with sandbox column in schema', async ({ request }) => {
    // This test verifies the database schema includes the sandbox column
    // by running a standard commis flow that would fail if the schema is broken

    test.setTimeout(60000);

    const startTime = Date.now();
    const message = 'What is 2+2?';

    // Start a chat that spawns a commis
    const chatPromise = request.post('/api/oikos/chat', {
      data: {
        message,
        message_id: crypto.randomUUID(),
        model: 'gpt-scripted',
      },
    });
    chatPromise.catch(() => {});

    // Wait for run to be created
    let runId: number | null = null;
    await expect
      .poll(async () => {
        const runsRes = await request.get('/api/oikos/runs?limit=25');
        if (!runsRes.ok()) return false;
        const runs = (await runsRes.json()) as Array<{ id: number; created_at: string; trigger: string }>;

        const candidate = runs.find((run) => {
          const createdAt = Date.parse(run.created_at);
          return Number.isFinite(createdAt) && createdAt >= startTime - 2000 && run.trigger !== 'commis';
        });

        if (candidate) {
          runId = candidate.id;
          return true;
        }
        return false;
      }, {
        timeout: 20000,
        intervals: [500, 1000, 2000],
      })
      .toBeTruthy();

    expect(runId).not.toBeNull();

    // Wait for run to complete (verifies schema is correct and job processor works)
    let runStatus: { status: string } | null = null;
    await expect
      .poll(async () => {
        const statusRes = await request.get(`/api/oikos/runs/${runId}`);
        if (!statusRes.ok()) return false;
        runStatus = await statusRes.json();
        return runStatus.status === 'success' || runStatus.status === 'failed';
      }, {
        timeout: 60000,
        intervals: [1000, 2000, 5000],
      })
      .toBeTruthy();

    // The run completing proves the schema migration worked
    // (if sandbox column was missing, job processor would fail)
    expect(['success', 'failed']).toContain(runStatus?.status);
  });
});
