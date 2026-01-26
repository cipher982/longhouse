import { test, expect } from './fixtures';
import { resetDatabase } from './test-utils';

test.describe('Parallel Commis Barrier', () => {
  test.beforeEach(async ({ request }) => {
    await resetDatabase(request);
  });

  test('spawns multiple commis in a single interrupt and resumes after all complete', async ({ request }) => {
    test.setTimeout(120000);

    const startTime = Date.now();
    const message = 'Check disk space on cube, clifford, and zerg in parallel';

    // Fire-and-forget chat request (SSE response never completes).
    const chatPromise = request.post('/api/oikos/chat', {
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

    let events: Array<{ event_type: string }> = [];
    await expect
      .poll(async () => {
        const eventsRes = await request.get(`/api/oikos/runs/${runId}/events`);
        if (!eventsRes.ok()) return false;
        const payload = await eventsRes.json();
        events = payload.events ?? [];

        const spawnedCount = events.filter((e) => e.event_type === 'commis_spawned').length;
        const completeCount = events.filter((e) => e.event_type === 'commis_complete').length;
        const waitingCount = events.filter((e) => e.event_type === 'oikos_waiting').length;
        const resumedCount = events.filter((e) => e.event_type === 'oikos_resumed').length;

        return spawnedCount >= 3 && completeCount >= 3 && waitingCount >= 1 && resumedCount >= 1;
      }, {
        timeout: 60000,
        intervals: [1000, 2000, 5000],
      })
      .toBeTruthy();

    const spawnedCount = events.filter((e) => e.event_type === 'commis_spawned').length;
    const completeCount = events.filter((e) => e.event_type === 'commis_complete').length;
    const waitingCount = events.filter((e) => e.event_type === 'oikos_waiting').length;
    const resumedCount = events.filter((e) => e.event_type === 'oikos_resumed').length;

    expect(spawnedCount).toBe(3);
    expect(completeCount).toBe(3);
    expect(waitingCount).toBe(1);
    expect(resumedCount).toBe(1);

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
    expect(runStatus?.result?.toLowerCase()).toContain('45%');
  });
});
