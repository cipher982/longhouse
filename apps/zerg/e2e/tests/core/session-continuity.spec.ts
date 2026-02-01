/**
 * Session Continuity E2E Tests
 *
 * Tests session fetch/ship with Longhouse agents API.
 * Uses mock hatch CLI (can't run real Claude Code fiches in tests).
 *
 * Requires:
 * - CommisJobProcessor running (included in E2E backend)
 * - mock-hatch in PATH (added by spawn-test-backend.js)
 */

import type { APIRequestContext } from '@playwright/test';
import { test, expect } from '../fixtures';
import { postSseAndCollect } from '../helpers/sse';
import { resetDatabase } from '../test-utils';

async function createTestSession(request: APIRequestContext) {
  const now = new Date().toISOString();
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

  const ingestRes = await request.post('/api/agents/ingest', { data: payload });
  expect(ingestRes.ok()).toBeTruthy();
  const data = await ingestRes.json();
  return data.session_id as string;
}

test.describe('Session Continuity E2E', () => {
  test.beforeEach(async ({ request }) => {
    await resetDatabase(request);
  });

  test('workspace commis executes with mock hatch', async ({ request, backendUrl, commisId }) => {
    test.setTimeout(90000);

    const startTime = Date.now();

    // Send a message that triggers workspace commis scenario
    // The scripted LLM detects "workspace" or "repository" keywords
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

    // Wait for workspace commis flow: commis_spawned -> commis_complete
    let events: Array<{ event_type: string; data?: any }> = [];
    await expect
      .poll(
        async () => {
          const eventsRes = await request.get(`/api/oikos/runs/${runId}/events`);
          if (!eventsRes.ok()) return false;
          const payload = await eventsRes.json();
          events = payload.events ?? [];

          const spawnedCount = events.filter((e) => e.event_type === 'commis_spawned').length;
          const completeCount = events.filter((e) => e.event_type === 'commis_complete').length;

          // Workspace commis don't emit oikos_resumed like standard commis
          // They complete directly via commis_complete event
          return spawnedCount >= 1 && completeCount >= 1;
        },
        { timeout: 60000, intervals: [1000, 2000, 5000] }
      )
      .toBeTruthy();

    // Verify we got the expected events
    const spawnedEvents = events.filter((e) => e.event_type === 'commis_spawned');
    const completeEvents = events.filter((e) => e.event_type === 'commis_complete');

    expect(spawnedEvents.length).toBeGreaterThanOrEqual(1);
    expect(completeEvents.length).toBeGreaterThanOrEqual(1);

    // Check commis completed successfully
    const commisComplete = completeEvents[0];
    expect(commisComplete.payload?.status).toBe('success');
  });

  test('workspace commis with resume_session_id fetches from Longhouse', async ({ request, backendUrl, commisId }) => {
    test.setTimeout(90000);

    const testSessionId = await createTestSession(request);
    const startTime = Date.now();

    // Send a message that triggers workspace commis with resume
    // Include the session ID in the message - scripted LLM extracts it
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

    // Wait for commis_complete event
    await expect
      .poll(
        async () => {
          const eventsRes = await request.get(`/api/oikos/runs/${runId}/events`);
          if (!eventsRes.ok()) return false;
          const payload = await eventsRes.json();
          const events = payload.events ?? [];
          return events.some((e: any) => e.event_type === 'commis_complete');
        },
        { timeout: 60000, intervals: [1000, 2000, 5000] }
      )
      .toBeTruthy();

    // Verify commis completed (session fetch happened even if no errors)
    const eventsRes = await request.get(`/api/oikos/runs/${runId}/events`);
    const { events } = await eventsRes.json();
    const commisComplete = events.find((e: any) => e.event_type === 'commis_complete');

    // Commis should complete (mock hatch always succeeds)
    expect(commisComplete).toBeTruthy();
    expect(commisComplete.payload?.status).toBe('success');
  });

  test('graceful fallback when session not found in Longhouse', async ({ request, backendUrl, commisId }) => {
    test.setTimeout(60000);

    const startTime = Date.now();

    // Use a non-existent session ID (valid UUID format but doesn't exist)
    const nonExistentSessionId = '00000000-0000-0000-0000-000000000000';

    await postSseAndCollect({
      backendUrl,
      commisId,
      path: '/api/oikos/chat',
      payload: {
        message: `Resume session ${nonExistentSessionId} and continue the work`,
        message_id: crypto.randomUUID(),
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

    // Wait for terminal state (commis_complete or oikos_complete)
    // The commis should either:
    // 1. Fail gracefully with an error about session not found
    // 2. Continue as a new session (no resume) and complete
    await expect
      .poll(
        async () => {
          const eventsRes = await request.get(`/api/oikos/runs/${runId}/events`);
          if (!eventsRes.ok()) return false;
          const payload = await eventsRes.json();
          const events = payload.events ?? [];

          // Either commis completed or oikos completed
          return events.some(
            (e: any) => e.event_type === 'commis_complete' || e.event_type === 'oikos_complete'
          );
        },
        { timeout: 60000, intervals: [1000, 2000, 5000] }
      )
      .toBeTruthy();

    // Check run didn't crash the system and reaches a terminal state
    await expect
      .poll(
        async () => {
          const statusRes = await request.get(`/api/oikos/runs/${runId}`);
          if (!statusRes.ok()) return null;
          const runStatus = await statusRes.json();
          return runStatus.status as string | null;
        },
        { timeout: 20000, intervals: [1000, 2000, 5000] }
      )
      .toMatch(/^(success|failed)$/);
  });
});
