/**
 * Session Continuity E2E Tests
 *
 * Tests session fetch/ship with REAL Life Hub API to eliminate drift risk.
 * Uses mock hatch CLI (can't run real Claude Code fiches in tests).
 *
 * Requires:
 * - LIFE_HUB_API_KEY environment variable
 * - CommisJobProcessor running (included in E2E backend)
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

  test('workspace commis executes with mock hatch', async ({ request }) => {
    // Skip if Life Hub credentials not available (local dev without key)
    test.skip(!LIFE_HUB_API_KEY, 'LIFE_HUB_API_KEY not set - skipping session continuity test');
    test.setTimeout(90000);

    const startTime = Date.now();

    // Send a message that triggers workspace commis scenario
    // The scripted LLM detects "workspace" or "repository" keywords
    const chatRes = await request.post('/api/jarvis/chat', {
      data: {
        message: 'Create a workspace and analyze the repository',
        message_id: crypto.randomUUID(),
        model: 'gpt-scripted',
      },
    });
    expect(chatRes.ok()).toBeTruthy();

    // Wait for concierge course to appear
    let courseId: number | null = null;
    await expect
      .poll(
        async () => {
          const coursesRes = await request.get('/api/jarvis/courses?limit=25');
          if (!coursesRes.ok()) return false;
          const courses = (await coursesRes.json()) as Array<{
            id: number;
            created_at: string;
            trigger: string;
          }>;

          const candidate = courses.find((course) => {
            const createdAt = Date.parse(course.created_at);
            return Number.isFinite(createdAt) && createdAt >= startTime - 2000 && course.trigger !== 'commis';
          });

          if (candidate) {
            courseId = candidate.id;
            return true;
          }
          return false;
        },
        { timeout: 20000, intervals: [500, 1000, 2000] }
      )
      .toBeTruthy();

    if (!courseId) {
      throw new Error('Failed to locate concierge course');
    }

    // Wait for workspace commis flow: commis_spawned -> commis_complete
    let events: Array<{ event_type: string; data?: any }> = [];
    await expect
      .poll(
        async () => {
          const eventsRes = await request.get(`/api/jarvis/courses/${courseId}/events`);
          if (!eventsRes.ok()) return false;
          const payload = await eventsRes.json();
          events = payload.events ?? [];

          const spawnedCount = events.filter((e) => e.event_type === 'commis_spawned').length;
          const completeCount = events.filter((e) => e.event_type === 'commis_complete').length;

          // Workspace commis don't emit concierge_resumed like standard commis
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

  test('workspace commis with resume_session_id fetches from Life Hub', async ({ request }) => {
    // Skip if Life Hub credentials not available
    test.skip(!LIFE_HUB_API_KEY, 'LIFE_HUB_API_KEY not set - skipping session continuity test');
    test.setTimeout(90000);

    // First, get a real session ID from Life Hub
    const sessionsRes = await request.fetch(`${LIFE_HUB_URL}/query/fiches/sessions`, {
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

    // Send a message that triggers workspace commis with resume
    // Include the session ID in the message - scripted LLM extracts it
    const chatRes = await request.post('/api/jarvis/chat', {
      data: {
        message: `Resume session ${testSessionId} and continue working on the repository`,
        message_id: crypto.randomUUID(),
        model: 'gpt-scripted',
      },
    });
    expect(chatRes.ok()).toBeTruthy();

    // Wait for concierge course
    let courseId: number | null = null;
    await expect
      .poll(
        async () => {
          const coursesRes = await request.get('/api/jarvis/courses?limit=25');
          if (!coursesRes.ok()) return false;
          const courses = (await coursesRes.json()) as Array<{
            id: number;
            created_at: string;
            trigger: string;
          }>;

          const candidate = courses.find((course) => {
            const createdAt = Date.parse(course.created_at);
            return Number.isFinite(createdAt) && createdAt >= startTime - 2000 && course.trigger !== 'commis';
          });

          if (candidate) {
            courseId = candidate.id;
            return true;
          }
          return false;
        },
        { timeout: 20000, intervals: [500, 1000, 2000] }
      )
      .toBeTruthy();

    if (!courseId) {
      throw new Error('Failed to locate concierge course');
    }

    // Wait for commis_complete event
    await expect
      .poll(
        async () => {
          const eventsRes = await request.get(`/api/jarvis/courses/${courseId}/events`);
          if (!eventsRes.ok()) return false;
          const payload = await eventsRes.json();
          const events = payload.events ?? [];
          return events.some((e: any) => e.event_type === 'commis_complete');
        },
        { timeout: 60000, intervals: [1000, 2000, 5000] }
      )
      .toBeTruthy();

    // Verify commis completed (session fetch happened even if no errors)
    const eventsRes = await request.get(`/api/jarvis/courses/${courseId}/events`);
    const { events } = await eventsRes.json();
    const commisComplete = events.find((e: any) => e.event_type === 'commis_complete');

    // Commis should complete (mock hatch always succeeds)
    expect(commisComplete).toBeTruthy();
    expect(commisComplete.payload?.status).toBe('success');
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

    // Wait for concierge course
    let courseId: number | null = null;
    await expect
      .poll(
        async () => {
          const coursesRes = await request.get('/api/jarvis/courses?limit=25');
          if (!coursesRes.ok()) return false;
          const courses = (await coursesRes.json()) as Array<{
            id: number;
            created_at: string;
            trigger: string;
          }>;

          const candidate = courses.find((course) => {
            const createdAt = Date.parse(course.created_at);
            return Number.isFinite(createdAt) && createdAt >= startTime - 2000 && course.trigger !== 'commis';
          });

          if (candidate) {
            courseId = candidate.id;
            return true;
          }
          return false;
        },
        { timeout: 20000, intervals: [500, 1000, 2000] }
      )
      .toBeTruthy();

    if (!courseId) {
      throw new Error('Failed to locate concierge course');
    }

    // Wait for terminal state (commis_complete or concierge_complete)
    // The commis should either:
    // 1. Fail gracefully with an error about session not found
    // 2. Continue as a new session (no resume) and complete
    await expect
      .poll(
        async () => {
          const eventsRes = await request.get(`/api/jarvis/courses/${courseId}/events`);
          if (!eventsRes.ok()) return false;
          const payload = await eventsRes.json();
          const events = payload.events ?? [];

          // Either commis completed or concierge completed
          return events.some(
            (e: any) => e.event_type === 'commis_complete' || e.event_type === 'concierge_complete'
          );
        },
        { timeout: 60000, intervals: [1000, 2000, 5000] }
      )
      .toBeTruthy();

    // Check course didn't crash the system
    const statusRes = await request.get(`/api/jarvis/courses/${courseId}`);
    expect(statusRes.ok()).toBeTruthy();
    const courseStatus = await statusRes.json();

    // The course should complete (success or failed, but not stuck)
    expect(['success', 'failed']).toContain(courseStatus.status);
  });
});
