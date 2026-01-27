import { test, expect } from './fixtures';
import { resetDatabase } from './test-utils';
import { postSseAndCollect } from './helpers/sse';

test.describe('Parallel Commis', () => {
  test.describe.configure({ timeout: 120000 });
  test.beforeEach(async ({ request }) => {
    await resetDatabase(request);
  });

  test('spawns multiple commis and completes after all finish', async ({ request, backendUrl, commisId }) => {
    const startTime = Date.now();
    const message = 'Check disk space on cube, clifford, and zerg in parallel';

    // Run SSE chat request until completion to ensure commis events are emitted.
    // Parallel commis use the async model: oikos spawns jobs and continues,
    // commis complete in background, oikos finishes without blocking.
    await postSseAndCollect({
      backendUrl,
      commisId,
      path: '/api/oikos/chat',
      payload: {
        message,
        message_id: crypto.randomUUID(),
        model: 'gpt-scripted',
      },
      stopEvent: 'oikos_complete',
      timeoutMs: 60000,
    });

    let runId: number | null = null;
    await expect
      .poll(async () => {
        const runsRes = await request.get('/api/oikos/runs?limit=50');
        if (!runsRes.ok()) return false;
        const runs = (await runsRes.json()) as Array<{ id: number; created_at: string }>;

        const candidate = runs.find((run) => {
          const createdAt = Date.parse(run.created_at);
          return Number.isFinite(createdAt) && createdAt >= startTime - 2000;
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

    if (!runId) {
      throw new Error('Failed to locate parallel-commis run');
    }

    // Wait for commis events. Parallel spawn uses async model (no interrupt/resume):
    // - commis_spawned: jobs are created and queued
    // - commis_started: commis begin execution
    // - commis_complete: commis finish (may happen after oikos_complete)
    let events: Array<{ event_type: string }> = [];
    await expect
      .poll(async () => {
        const eventsRes = await request.get(`/api/oikos/runs/${runId}/events`);
        if (!eventsRes.ok()) return false;
        const payload = await eventsRes.json();
        events = payload.events ?? [];

        const spawnedCount = events.filter((e) => e.event_type === 'commis_spawned').length;
        const completeCount = events.filter((e) => e.event_type === 'commis_complete').length;

        // Async model: 3 spawned, 3 completed (no waiting/resumed events)
        return spawnedCount >= 3 && completeCount >= 3;
      }, {
        timeout: 60000,
        intervals: [1000, 2000, 5000],
      })
      .toBeTruthy();

    const spawnedCount = events.filter((e) => e.event_type === 'commis_spawned').length;
    const completeCount = events.filter((e) => e.event_type === 'commis_complete').length;

    expect(spawnedCount).toBe(3);
    expect(completeCount).toBe(3);

    // Verify run completed successfully with expected result
    let runStatus: { status: string; result?: string } | null = null;
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

    expect(runStatus?.status).toBe('success');
    // Note: The result is "Task completed successfully" from scripted LLM
    // The actual 45% data flows through the commis_complete events
    // This test verifies the parallel spawn mechanics, not result synthesis
  });
});
