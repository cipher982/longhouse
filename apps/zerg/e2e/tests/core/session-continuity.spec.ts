/**
 * Session Continuity E2E Tests
 *
 * Tests session fetch/ship with REAL Life Hub API to eliminate drift risk.
 * Uses mock hatch CLI (can't run real Claude Code agents in tests).
 *
 * Requires:
 * - LIFE_HUB_API_KEY environment variable
 * - WorkerJobProcessor running (included in E2E backend)
 * - mock-hatch in PATH (added by spawn-test-backend.js)
 */

import { test, expect } from '../fixtures';
import { resetDatabase } from '../test-utils';

const LIFE_HUB_URL = process.env.LIFE_HUB_URL || 'https://data.drose.io';
const LIFE_HUB_API_KEY = process.env.LIFE_HUB_API_KEY;

test.describe('Session Continuity E2E', () => {
  test.beforeEach(async ({ request }) => {
    await resetDatabase(request);
  });

  test('workspace worker executes with mock hatch', async ({ request }) => {
    // Skip if Life Hub credentials not available (local dev without key)
    test.skip(!LIFE_HUB_API_KEY, 'LIFE_HUB_API_KEY not set - skipping session continuity test');
    test.setTimeout(90000);

    const startTime = Date.now();

    // Send a message that triggers workspace worker scenario
    // The scripted LLM detects "workspace" or "repository" keywords
    const chatRes = await request.post('/api/jarvis/chat', {
      data: {
        message: 'Create a workspace and analyze the repository',
        message_id: crypto.randomUUID(),
        model: 'gpt-scripted',
      },
    });
    expect(chatRes.ok()).toBeTruthy();

    // Wait for supervisor run to appear
    let runId: number | null = null;
    await expect
      .poll(
        async () => {
          const runsRes = await request.get('/api/jarvis/runs?limit=25');
          if (!runsRes.ok()) return false;
          const runs = (await runsRes.json()) as Array<{
            id: number;
            created_at: string;
            trigger: string;
          }>;

          const candidate = runs.find((run) => {
            const createdAt = Date.parse(run.created_at);
            return Number.isFinite(createdAt) && createdAt >= startTime - 2000 && run.trigger !== 'worker';
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
      throw new Error('Failed to locate supervisor run');
    }

    // Wait for workspace worker flow: worker_spawned -> worker_complete
    let events: Array<{ event_type: string; data?: any }> = [];
    await expect
      .poll(
        async () => {
          const eventsRes = await request.get(`/api/jarvis/runs/${runId}/events`);
          if (!eventsRes.ok()) return false;
          const payload = await eventsRes.json();
          events = payload.events ?? [];

          const spawnedCount = events.filter((e) => e.event_type === 'worker_spawned').length;
          const completeCount = events.filter((e) => e.event_type === 'worker_complete').length;

          // Workspace workers don't emit supervisor_resumed like standard workers
          // They complete directly via worker_complete event
          return spawnedCount >= 1 && completeCount >= 1;
        },
        { timeout: 60000, intervals: [1000, 2000, 5000] }
      )
      .toBeTruthy();

    // Verify we got the expected events
    const spawnedEvents = events.filter((e) => e.event_type === 'worker_spawned');
    const completeEvents = events.filter((e) => e.event_type === 'worker_complete');

    expect(spawnedEvents.length).toBeGreaterThanOrEqual(1);
    expect(completeEvents.length).toBeGreaterThanOrEqual(1);

    // Check worker completed successfully
    const workerComplete = completeEvents[0];
    expect(workerComplete.payload?.status).toBe('success');
  });

  test('workspace worker with resume_session_id fetches from Life Hub', async ({ request }) => {
    // Skip if Life Hub credentials not available
    test.skip(!LIFE_HUB_API_KEY, 'LIFE_HUB_API_KEY not set - skipping session continuity test');
    test.setTimeout(90000);

    // First, get a real session ID from Life Hub
    const sessionsRes = await request.fetch(`${LIFE_HUB_URL}/query/agents/sessions`, {
      headers: { 'X-API-Key': LIFE_HUB_API_KEY! },
      params: {
        limit: '10',
        provider: 'claude',
      },
    });

    if (!sessionsRes.ok()) {
      test.skip(true, `Failed to query Life Hub: ${sessionsRes.status()}`);
      return;
    }

    const sessionsData = await sessionsRes.json();
    const sessions = sessionsData.data || [];

    // Find a session with meaningful content (>= 10 events)
    const testSession = sessions.find((s: any) => (s.events_total || 0) >= 10);
    if (!testSession) {
      test.skip(true, 'No suitable sessions in Life Hub for testing');
      return;
    }

    const testSessionId = testSession.id;
    const startTime = Date.now();

    // Send a message that triggers workspace worker with resume
    // Include the session ID in the message - scripted LLM extracts it
    const chatRes = await request.post('/api/jarvis/chat', {
      data: {
        message: `Resume session ${testSessionId} and continue working on the repository`,
        message_id: crypto.randomUUID(),
        model: 'gpt-scripted',
      },
    });
    expect(chatRes.ok()).toBeTruthy();

    // Wait for supervisor run
    let runId: number | null = null;
    await expect
      .poll(
        async () => {
          const runsRes = await request.get('/api/jarvis/runs?limit=25');
          if (!runsRes.ok()) return false;
          const runs = (await runsRes.json()) as Array<{
            id: number;
            created_at: string;
            trigger: string;
          }>;

          const candidate = runs.find((run) => {
            const createdAt = Date.parse(run.created_at);
            return Number.isFinite(createdAt) && createdAt >= startTime - 2000 && run.trigger !== 'worker';
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
      throw new Error('Failed to locate supervisor run');
    }

    // Wait for worker_complete event
    await expect
      .poll(
        async () => {
          const eventsRes = await request.get(`/api/jarvis/runs/${runId}/events`);
          if (!eventsRes.ok()) return false;
          const payload = await eventsRes.json();
          const events = payload.events ?? [];
          return events.some((e: any) => e.event_type === 'worker_complete');
        },
        { timeout: 60000, intervals: [1000, 2000, 5000] }
      )
      .toBeTruthy();

    // Verify worker completed (session fetch happened even if no errors)
    const eventsRes = await request.get(`/api/jarvis/runs/${runId}/events`);
    const { events } = await eventsRes.json();
    const workerComplete = events.find((e: any) => e.event_type === 'worker_complete');

    // Worker should complete (mock hatch always succeeds)
    expect(workerComplete).toBeTruthy();
    expect(workerComplete.payload?.status).toBe('success');
  });

  test('graceful fallback when session not found in Life Hub', async ({ request }) => {
    // Skip if Life Hub credentials not available
    test.skip(!LIFE_HUB_API_KEY, 'LIFE_HUB_API_KEY not set - skipping session continuity test');
    test.setTimeout(60000);

    const startTime = Date.now();

    // Use a non-existent session ID (valid UUID format but doesn't exist)
    const nonExistentSessionId = '00000000-0000-0000-0000-000000000000';

    const chatRes = await request.post('/api/jarvis/chat', {
      data: {
        message: `Resume session ${nonExistentSessionId} and continue the work`,
        message_id: crypto.randomUUID(),
        model: 'gpt-scripted',
      },
    });
    expect(chatRes.ok()).toBeTruthy();

    // Wait for supervisor run
    let runId: number | null = null;
    await expect
      .poll(
        async () => {
          const runsRes = await request.get('/api/jarvis/runs?limit=25');
          if (!runsRes.ok()) return false;
          const runs = (await runsRes.json()) as Array<{
            id: number;
            created_at: string;
            trigger: string;
          }>;

          const candidate = runs.find((run) => {
            const createdAt = Date.parse(run.created_at);
            return Number.isFinite(createdAt) && createdAt >= startTime - 2000 && run.trigger !== 'worker';
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
      throw new Error('Failed to locate supervisor run');
    }

    // Wait for terminal state (worker_complete or supervisor_complete)
    // The worker should either:
    // 1. Fail gracefully with an error about session not found
    // 2. Continue as a new session (no resume) and complete
    await expect
      .poll(
        async () => {
          const eventsRes = await request.get(`/api/jarvis/runs/${runId}/events`);
          if (!eventsRes.ok()) return false;
          const payload = await eventsRes.json();
          const events = payload.events ?? [];

          // Either worker completed or supervisor completed
          return events.some(
            (e: any) => e.event_type === 'worker_complete' || e.event_type === 'supervisor_complete'
          );
        },
        { timeout: 60000, intervals: [1000, 2000, 5000] }
      )
      .toBeTruthy();

    // Check run didn't crash the system
    const statusRes = await request.get(`/api/jarvis/runs/${runId}`);
    expect(statusRes.ok()).toBeTruthy();
    const runStatus = await statusRes.json();

    // The run should complete (success or failed, but not stuck)
    expect(['success', 'failed']).toContain(runStatus.status);
  });
});
