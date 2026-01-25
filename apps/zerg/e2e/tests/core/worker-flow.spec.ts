import { test, expect } from '../fixtures';
import { resetDatabase } from '../test-utils';

test.describe('Core Worker Flow', () => {
  test.beforeEach(async ({ request }) => {
    await resetDatabase(request);
  });

  test('spawns a worker and completes', async ({ request }) => {
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

    let runId: number | null = null;
    await expect
      .poll(async () => {
        const runsRes = await request.get('/api/jarvis/runs?limit=25');
        if (!runsRes.ok()) return false;
        const runs = (await runsRes.json()) as Array<{ id: number; created_at: string; trigger: string }>;

        const candidate = runs.find((run) => {
          const createdAt = Date.parse(run.created_at);
          return Number.isFinite(createdAt) && createdAt >= startTime - 2000 && run.trigger !== 'worker';
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
      throw new Error('Failed to locate worker run');
    }

    // In async model: supervisor spawns worker and completes immediately
    // Worker runs in background, completes later
    let events: Array<{ event_type: string }> = [];
    await expect
      .poll(async () => {
        const eventsRes = await request.get(`/api/jarvis/runs/${runId}/events`);
        if (!eventsRes.ok()) return false;
        const payload = await eventsRes.json();
        events = payload.events ?? [];

        const spawnedCount = events.filter((e) => e.event_type === 'worker_spawned').length;
        const completeCount = events.filter((e) => e.event_type === 'worker_complete').length;

        // Async model: supervisor doesn't wait, so no supervisor_resumed
        return spawnedCount >= 1 && completeCount >= 1;
      }, {
        timeout: 60000,
        intervals: [1000, 2000, 5000],
      })
      .toBeTruthy();

    const spawnedCount = events.filter((e) => e.event_type === 'worker_spawned').length;
    const completeCount = events.filter((e) => e.event_type === 'worker_complete').length;

    expect(spawnedCount).toBeGreaterThanOrEqual(1);
    expect(completeCount).toBeGreaterThanOrEqual(1);

    let runStatus: { status: string; result?: string } | null = null;
    await expect
      .poll(async () => {
        const statusRes = await request.get(`/api/jarvis/runs/${runId}`);
        if (!statusRes.ok()) return false;
        runStatus = await statusRes.json();
        return runStatus.status === 'success' || runStatus.status === 'failed';
      }, {
        timeout: 60000,
        intervals: [1000, 2000, 5000],
      })
      .toBeTruthy();

    expect(runStatus?.status).toBe('success');
  });
});
