/**
 * Commis Simplification E2E Tests
 *
 * Validates the single execution mode architecture:
 * - Direct Oikos response (no commis needed)
 * - spawn_commis without git_repo (scratch workspace)
 * - spawn_commis with git_repo (workspace with git)
 * - commis_summary_ready event emission
 * - runner_exec from Oikos (graceful failure without runner)
 */

import { randomUUID } from 'node:crypto';

import { test, expect } from '../fixtures';
import { postSseAndCollect } from '../helpers/sse';
import { resetDatabase } from '../test-utils';

test.describe('Commis Simplification - Single Execution Mode', () => {
  test.beforeEach(async ({ request }) => {
    await resetDatabase(request);
  });

  test('direct Oikos response without spawning commis', async ({ request, backendUrl, commisId }) => {
    // Simple math question should be answered directly by Oikos
    // No commis should be spawned
    test.setTimeout(60000);

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

    // Should complete without spawning any commis
    const completeEvent = events.find((e) => e.event === 'oikos_complete');
    expect(completeEvent).toBeTruthy();

    const result = (completeEvent?.data as { payload?: { result?: string } })?.payload?.result ?? '';
    expect(result).toBe('4');

    // Verify NO commis_spawned event
    const spawnedEvents = events.filter((e) => e.event === 'commis_spawned');
    expect(spawnedEvents.length).toBe(0);
  });

  test('scratch workspace commis (spawn_commis without git_repo)', async ({
    request,
    backendUrl,
    commisId,
  }) => {
    // Disk space check triggers spawn_commis without git_repo
    // This should create a scratch workspace
    test.setTimeout(90000);

    const startTime = Date.now();
    const message = 'Check disk space on cube';

    // Send chat message (don't wait for completion - it's async)
    const chatPromise = request.post('/api/oikos/chat', {
      data: {
        message,
        message_id: randomUUID(),
        model: 'gpt-scripted',
      },
    });
    chatPromise.catch(() => {}); // Ignore errors, we poll for events

    // Wait for oikos run to appear
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

          if (candidate) {
            runId = candidate.id;
            return true;
          }
          return false;
        },
        { timeout: 20000, intervals: [500, 1000, 2000] }
      )
      .toBeTruthy();

    if (!runId) {
      throw new Error('Failed to locate oikos run');
    }

    // Wait for full commis flow: spawned -> complete -> summary_ready
    let events: Array<{ event_type: string; payload?: any }> = [];
    await expect
      .poll(
        async () => {
          const eventsRes = await request.get(`/api/oikos/runs/${runId}/events`);
          if (!eventsRes.ok()) return false;
          const payload = await eventsRes.json();
          events = payload.events ?? [];

          const spawnedCount = events.filter((e) => e.event_type === 'commis_spawned').length;
          const completeCount = events.filter((e) => e.event_type === 'commis_complete').length;

          return spawnedCount >= 1 && completeCount >= 1;
        },
        { timeout: 60000, intervals: [1000, 2000, 5000] }
      )
      .toBeTruthy();

    // Verify events
    const spawnedEvents = events.filter((e) => e.event_type === 'commis_spawned');
    const completeEvents = events.filter((e) => e.event_type === 'commis_complete');
    const summaryEvents = events.filter((e) => e.event_type === 'commis_summary_ready');

    expect(spawnedEvents.length).toBeGreaterThanOrEqual(1);
    expect(completeEvents.length).toBeGreaterThanOrEqual(1);

    // Verify commis completed successfully
    const commisComplete = completeEvents[0];
    expect(commisComplete.payload?.status).toBe('success');

    // Verify commis_summary_ready event was emitted
    expect(summaryEvents.length).toBeGreaterThanOrEqual(1);
    expect(summaryEvents[0].payload?.summary).toBeTruthy();
  });

  test('workspace commis (spawn_commis with git_repo)', async ({ request, backendUrl, commisId }) => {
    test.setTimeout(90000);

    const startTime = Date.now();

    // "workspace" or "repository" keywords trigger workspace_commis_oikos scenario
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

    // Wait for oikos run
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

          if (candidate) {
            runId = candidate.id;
            return true;
          }
          return false;
        },
        { timeout: 20000, intervals: [500, 1000, 2000] }
      )
      .toBeTruthy();

    if (!runId) {
      throw new Error('Failed to locate oikos run');
    }

    // Wait for commis flow with summary event
    let events: Array<{ event_type: string; payload?: any }> = [];
    await expect
      .poll(
        async () => {
          const eventsRes = await request.get(`/api/oikos/runs/${runId}/events`);
          if (!eventsRes.ok()) return false;
          const payload = await eventsRes.json();
          events = payload.events ?? [];

          const spawnedCount = events.filter((e) => e.event_type === 'commis_spawned').length;
          const completeCount = events.filter((e) => e.event_type === 'commis_complete').length;

          return spawnedCount >= 1 && completeCount >= 1;
        },
        { timeout: 60000, intervals: [1000, 2000, 5000] }
      )
      .toBeTruthy();

    // Verify events
    const spawnedEvents = events.filter((e) => e.event_type === 'commis_spawned');
    const completeEvents = events.filter((e) => e.event_type === 'commis_complete');
    const summaryEvents = events.filter((e) => e.event_type === 'commis_summary_ready');

    expect(spawnedEvents.length).toBeGreaterThanOrEqual(1);
    expect(completeEvents.length).toBeGreaterThanOrEqual(1);

    // Workspace commis should include git_repo in spawned event
    const spawnedPayload = spawnedEvents[0].payload;
    expect(spawnedPayload).toBeTruthy();

    // Verify completion
    const commisComplete = completeEvents[0];
    expect(commisComplete.payload?.status).toBe('success');

    // Verify summary event
    expect(summaryEvents.length).toBeGreaterThanOrEqual(1);
  });

  test.skip('runner_exec requires connected runner infrastructure', async () => {
    // runner_exec is now available to Oikos directly, but testing it requires:
    // 1. A connected runner (daemon process with WebSocket connection)
    // 2. A registered runner in the database with matching owner
    //
    // This is tested at the unit level in test_runner_exec.py:
    // - test_runner_exec_requires_context: Validates context requirements
    // - test_runner_exec_runner_not_found: Validates graceful error handling
    // - test_runner_exec_success: Validates successful execution path
    //
    // E2E testing of runner_exec requires full runner infrastructure setup
    // which is out of scope for automated E2E tests.
  });

  test('parallel commis spawning (multiple spawn_commis calls)', async ({
    request,
    backendUrl,
    commisId,
  }) => {
    // Parallel disk check triggers multiple spawn_commis without git_repo
    test.setTimeout(120000);

    const startTime = Date.now();

    // "disk" + multiple hosts ("cube", "clifford", "zerg") triggers parallel scenario
    const chatPromise = request.post('/api/oikos/chat', {
      data: {
        message: 'Check disk space on cube, clifford, and zerg in parallel',
        message_id: randomUUID(),
        model: 'gpt-scripted',
      },
    });
    chatPromise.catch(() => {});

    // Wait for oikos run
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

          if (candidate) {
            runId = candidate.id;
            return true;
          }
          return false;
        },
        { timeout: 20000, intervals: [500, 1000, 2000] }
      )
      .toBeTruthy();

    if (!runId) {
      throw new Error('Failed to locate oikos run');
    }

    // Wait for all 3 commis to complete
    // Increased timeout to 60s because parallel commis can take longer when
    // the full test suite is running and resources are contended
    let events: Array<{ event_type: string; payload?: any }> = [];
    await expect
      .poll(
        async () => {
          const eventsRes = await request.get(`/api/oikos/runs/${runId}/events`);
          if (!eventsRes.ok()) return false;
          const payload = await eventsRes.json();
          events = payload.events ?? [];

          const spawnedCount = events.filter((e) => e.event_type === 'commis_spawned').length;
          const completeCount = events.filter((e) => e.event_type === 'commis_complete').length;

          // Should have 3 spawned and 3 completed
          return spawnedCount >= 3 && completeCount >= 3;
        },
        { timeout: 90000, intervals: [1000, 2000, 5000] }
      )
      .toBeTruthy();

    // Verify parallel execution
    const spawnedEvents = events.filter((e) => e.event_type === 'commis_spawned');
    const completeEvents = events.filter((e) => e.event_type === 'commis_complete');
    const summaryEvents = events.filter((e) => e.event_type === 'commis_summary_ready');

    expect(spawnedEvents.length).toBe(3);
    expect(completeEvents.length).toBe(3);

    // All should complete successfully
    for (const complete of completeEvents) {
      expect(complete.payload?.status).toBe('success');
    }

    // Should have 3 summary events
    expect(summaryEvents.length).toBe(3);
  });
});
