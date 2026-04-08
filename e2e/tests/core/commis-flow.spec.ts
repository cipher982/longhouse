import { test, expect } from '../fixtures';
import { resetDatabase } from '../test-utils';

type RunEvent = {
  event_type: string;
  payload?: Record<string, any>;
};

test.describe('Core Commis Flow', () => {
  test.beforeEach(async ({ request }) => {
    await resetDatabase(request);
  });

  test('spawns spawn_commis, marks the run waiting, then completes', async ({ request }) => {
    test.setTimeout(90000);

    const startTime = Date.now();

    const chatPromise = request.post('/api/oikos/chat', {
      data: {
        message: 'Check disk space on cube',
        message_id: crypto.randomUUID(),
        model: 'gpt-scripted',
      },
    });
    chatPromise.catch(() => {});

    let runId: number | null = null;
    await expect
      .poll(
        async () => {
          const runsRes = await request.get('/api/oikos/runs?limit=25');
          if (!runsRes.ok()) return false;
          const runs = (await runsRes.json()) as Array<{ id: number; created_at: string; trigger: string }>;

          const candidate = runs.find((run) => {
            const createdAt = Date.parse(run.created_at);
            return Number.isFinite(createdAt) && createdAt >= startTime - 2000 && run.trigger !== 'commis';
          });

          if (!candidate) return false;
          runId = candidate.id;
          return true;
        },
        { timeout: 20000, intervals: [500, 1000, 2000] }
      )
      .toBeTruthy();

    if (!runId) {
      throw new Error('Failed to locate commis run');
    }

    let events: RunEvent[] = [];
    await expect
      .poll(
        async () => {
          const eventsRes = await request.get(`/api/oikos/runs/${runId}/events`);
          if (!eventsRes.ok()) return false;
          const payload = await eventsRes.json();
          events = payload.events ?? [];

          const hasSpawn = events.some(
            (event) => event.event_type === 'oikos_tool_started' && event.payload?.tool_name === 'spawn_commis'
          );
          const hasWaiting = events.some((event) => event.event_type === 'oikos_waiting');

          return hasSpawn && hasWaiting;
        },
        { timeout: 60000, intervals: [1000, 2000, 5000] }
      )
      .toBeTruthy();

    let runStatus: { status: string; result?: string | null } | null = null;
    await expect
      .poll(
        async () => {
          const statusRes = await request.get(`/api/oikos/runs/${runId}`);
          if (!statusRes.ok()) return null;
          runStatus = await statusRes.json();
          return runStatus.status;
        },
        { timeout: 90000, intervals: [1000, 2000, 5000] }
      )
      .toMatch(/^(success|failed)$/);

    expect(runStatus?.status).toBe('success');
    expect(runStatus?.result ?? '').toContain('45%');
  });
});
