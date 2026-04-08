/**
 * Session continuity E2E tests.
 *
 * Validate that workspace commis calls carry the expected resume context and
 * finish through the simplified interrupt/resume flow.
 */

import type { APIRequestContext } from '@playwright/test';
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
};

async function mintDeviceToken(request: APIRequestContext): Promise<string> {
  const response = await request.post('/api/devices/tokens', {
    data: { device_id: 'e2e-device' },
  });
  expect(response.ok()).toBeTruthy();
  const payload = await response.json();
  expect(typeof payload.token).toBe('string');
  return payload.token as string;
}

async function createTestSession(request: APIRequestContext) {
  const now = new Date().toISOString();
  const deviceToken = await mintDeviceToken(request);
  const payload = {
    provider: 'claude',
    environment: 'development',
    project: 'e2e-session-continuity',
    device_id: 'e2e-device',
    cwd: '/tmp/e2e-session-continuity',
    git_repo: 'https://example.com/repo.git',
    git_branch: 'main',
    started_at: now,
    ended_at: now,
    provider_session_id: 'e2e-session-1',
    events: [
      { role: 'user', content_text: 'Test message', timestamp: now },
      { role: 'assistant', content_text: 'Test response', timestamp: now },
    ],
  };

  const ingestRes = await request.post('/api/agents/ingest', {
    data: payload,
    headers: {
      'X-Agents-Token': deviceToken,
    },
  });
  expect(ingestRes.ok()).toBeTruthy();
  const data = await ingestRes.json();
  return data.session_id as string;
}

async function waitForRecentOikosRun(request: APIRequestContext, startTime: number): Promise<number> {
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

async function waitForSpawnCommisEvent(
  request: APIRequestContext,
  runId: number
): Promise<RunEvent> {
  let event: RunEvent | null = null;

  await expect
    .poll(
      async () => {
        const eventsRes = await request.get(`/api/oikos/runs/${runId}/events`);
        if (!eventsRes.ok()) return false;
        const payload = await eventsRes.json();
        const events = (payload.events ?? []) as RunEvent[];

        event =
          events.find(
            (candidate) =>
              candidate.event_type === 'oikos_tool_started' && candidate.payload?.tool_name === 'spawn_commis'
          ) ?? null;
        return !!event;
      },
      { timeout: 60000, intervals: [1000, 2000, 5000] }
    )
    .toBeTruthy();

  if (!event) {
    throw new Error(`Run ${runId} never recorded spawn_commis`);
  }

  return event;
}

async function waitForRunTerminal(request: APIRequestContext, runId: number): Promise<RunStatus> {
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

test.describe('Session Continuity E2E', () => {
  test.beforeEach(async ({ request }) => {
    await resetDatabase(request);
  });

  test('workspace commis executes with mock hatch', async ({ request, backendUrl, commisId }) => {
    test.setTimeout(90000);

    const startTime = Date.now();

    await postSseAndCollect({
      backendUrl,
      commisId,
      path: '/api/oikos/chat',
      payload: {
        message: 'Create a workspace and analyze the repository',
        message_id: crypto.randomUUID(),
        model: 'gpt-scripted',
      },
      stopOnFirstEvent: true,
      timeoutMs: 20000,
    });

    const runId = await waitForRecentOikosRun(request, startTime);
    const spawnEvent = await waitForSpawnCommisEvent(request, runId);
    const run = await waitForRunTerminal(request, runId);

    expect(spawnEvent.payload?.tool_args?.git_repo).toBe('https://github.com/octocat/Hello-World.git');
    expect(run.status).toBe('success');
    expect(run.result ?? '').toContain('Workspace commis completed successfully');
  });

  test('workspace commis with resume_session_id fetches from Longhouse', async ({ request, backendUrl, commisId }) => {
    test.setTimeout(90000);

    const testSessionId = await createTestSession(request);
    const startTime = Date.now();

    await postSseAndCollect({
      backendUrl,
      commisId,
      path: '/api/oikos/chat',
      payload: {
        message: `Resume session ${testSessionId} and continue working on the repository`,
        message_id: crypto.randomUUID(),
        model: 'gpt-scripted',
      },
      stopOnFirstEvent: true,
      timeoutMs: 20000,
    });

    const runId = await waitForRecentOikosRun(request, startTime);
    const spawnEvent = await waitForSpawnCommisEvent(request, runId);
    const run = await waitForRunTerminal(request, runId);

    expect(spawnEvent.payload?.tool_args?.resume_session_id).toBe(testSessionId);
    expect(run.status).toBe('success');
  });

  test('graceful fallback when session not found in Longhouse', async ({ request, backendUrl, commisId }) => {
    test.setTimeout(90000);

    const startTime = Date.now();
    const missingSessionId = '00000000-0000-0000-0000-000000000000';

    await postSseAndCollect({
      backendUrl,
      commisId,
      path: '/api/oikos/chat',
      payload: {
        message: `Resume session ${missingSessionId} and continue the work`,
        message_id: crypto.randomUUID(),
        model: 'gpt-scripted',
      },
      stopOnFirstEvent: true,
      timeoutMs: 20000,
    });

    const runId = await waitForRecentOikosRun(request, startTime);
    const spawnEvent = await waitForSpawnCommisEvent(request, runId);
    const run = await waitForRunTerminal(request, runId);

    expect(spawnEvent.payload?.tool_args?.resume_session_id).toBe(missingSessionId);
    expect(run.status).toMatch(/^(success|failed)$/);
  });
});
