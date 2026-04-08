/**
 * Commis simplification E2E tests.
 *
 * Validate the current minimal contract:
 * - direct Oikos answers stay direct
 * - spawn_commis is recorded as a normal tool call
 * - background work marks the run waiting, then resumes to a terminal state
 */

import { randomUUID } from 'node:crypto';

import { test, expect } from '../fixtures';
import { postSseAndCollect } from '../helpers/sse';
import { resetDatabase } from '../test-utils';

type RunEvent = {
  event_type: string;
  payload?: Record<string, any>;
};

type RunStatus = {
  status: string;
  result?: string | null;
  error?: string | null;
};

async function waitForRecentOikosRun(
  request: import('@playwright/test').APIRequestContext,
  startTime: number
): Promise<number> {
  let runId: number | null = null;

  await expect
    .poll(
      async () => {
        const runsRes = await request.get('/api/oikos/runs?limit=25');
        if (!runsRes.ok()) return false;
        const runs = (await runsRes.json()) as Array<{
          id: number;
          created_at: string;
          trigger: string;
        }>;

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
    throw new Error('Failed to locate oikos run');
  }

  return runId;
}

async function getRunEvents(
  request: import('@playwright/test').APIRequestContext,
  runId: number
): Promise<RunEvent[]> {
  const eventsRes = await request.get(`/api/oikos/runs/${runId}/events`);
  expect(eventsRes.ok()).toBeTruthy();
  const payload = await eventsRes.json();
  return payload.events ?? [];
}

async function waitForSpawnCommisEvents(
  request: import('@playwright/test').APIRequestContext,
  runId: number,
  expectedCount: number
): Promise<RunEvent[]> {
  let events: RunEvent[] = [];

  await expect
    .poll(
      async () => {
        events = await getRunEvents(request, runId);
        const started = events.filter(
          (event) => event.event_type === 'oikos_tool_started' && event.payload?.tool_name === 'spawn_commis'
        );
        const waiting = events.some((event) => event.event_type === 'oikos_waiting');
        return started.length >= expectedCount && waiting;
      },
      { timeout: 60000, intervals: [1000, 2000, 5000] }
    )
    .toBeTruthy();

  return events;
}

async function waitForRunTerminal(
  request: import('@playwright/test').APIRequestContext,
  runId: number
): Promise<RunStatus> {
  let run: RunStatus | null = null;

  await expect
    .poll(
      async () => {
        const statusRes = await request.get(`/api/oikos/runs/${runId}`);
        if (!statusRes.ok()) return null;
        run = (await statusRes.json()) as RunStatus;
        return run.status;
      },
      { timeout: 90000, intervals: [1000, 2000, 5000] }
    )
    .toMatch(/^(success|failed)$/);

  if (!run) {
    throw new Error(`Run ${runId} never reached a terminal state`);
  }

  return run;
}

test.describe('Commis Simplification - Single Execution Mode', () => {
  test.beforeEach(async ({ request }) => {
    await resetDatabase(request);
  });

  test('direct Oikos response without spawning commis', async ({ backendUrl, commisId }) => {
    const events = await postSseAndCollect({
      backendUrl,
      commisId,
      path: '/api/oikos/chat',
      payload: {
        message: '2+2',
        message_id: randomUUID(),
        model: 'gpt-scripted',
      },
      stopEvent: 'oikos_complete',
      timeoutMs: 30000,
    });

    const completeEvent = events.find((e) => e.event === 'oikos_complete');
    expect(completeEvent).toBeTruthy();

    const result = (completeEvent?.data as { payload?: { result?: string } })?.payload?.result ?? '';
    expect(result).toBe('4');

    const startedEvents = events.filter((e) => e.event === 'oikos_tool_started');
    expect(startedEvents.length).toBe(0);
  });

  test('scratch workspace commis uses spawn_commis and resumes successfully', async ({ request }) => {
    const startTime = Date.now();

    const chatPromise = request.post('/api/oikos/chat', {
      data: {
        message: 'Check disk space on cube',
        message_id: randomUUID(),
        model: 'gpt-scripted',
      },
    });
    chatPromise.catch(() => {});

    const runId = await waitForRecentOikosRun(request, startTime);
    const events = await waitForSpawnCommisEvents(request, runId, 1);
    const run = await waitForRunTerminal(request, runId);

    expect(run.status).toBe('success');
    expect(run.result ?? '').toContain('45%');

    const spawnEvent = events.find(
      (event) => event.event_type === 'oikos_tool_started' && event.payload?.tool_name === 'spawn_commis'
    );
    expect(spawnEvent?.payload?.tool_args?.task).toContain('Check disk space on cube');
    expect(spawnEvent?.payload?.tool_args?.git_repo).toBeUndefined();
  });

  test('workspace commis records git_repo in spawn_commis args', async ({ request, backendUrl, commisId }) => {
    const startTime = Date.now();

    await postSseAndCollect({
      backendUrl,
      commisId,
      path: '/api/oikos/chat',
      payload: {
        message: 'Create a workspace and analyze the repository',
        message_id: randomUUID(),
        model: 'gpt-scripted',
      },
      stopOnFirstEvent: true,
      timeoutMs: 20000,
    });

    const runId = await waitForRecentOikosRun(request, startTime);
    const events = await waitForSpawnCommisEvents(request, runId, 1);
    const run = await waitForRunTerminal(request, runId);

    expect(run.status).toBe('success');
    expect(run.result ?? '').toContain('Workspace commis completed successfully');

    const spawnEvent = events.find(
      (event) => event.event_type === 'oikos_tool_started' && event.payload?.tool_name === 'spawn_commis'
    );
    expect(spawnEvent?.payload?.tool_args?.git_repo).toBe('https://github.com/octocat/Hello-World.git');
  });

  test('parallel spawn_commis calls are all recorded before Oikos waits', async ({ request }) => {
    const startTime = Date.now();

    const chatPromise = request.post('/api/oikos/chat', {
      data: {
        message: 'Check disk space on cube, clifford, and zerg in parallel',
        message_id: randomUUID(),
        model: 'gpt-scripted',
      },
    });
    chatPromise.catch(() => {});

    const runId = await waitForRecentOikosRun(request, startTime);
    const events = await waitForSpawnCommisEvents(request, runId, 3);
    const run = await waitForRunTerminal(request, runId);

    expect(run.status).toBe('success');

    const spawnEvents = events.filter(
      (event) => event.event_type === 'oikos_tool_started' && event.payload?.tool_name === 'spawn_commis'
    );
    expect(spawnEvents).toHaveLength(3);
  });
});
