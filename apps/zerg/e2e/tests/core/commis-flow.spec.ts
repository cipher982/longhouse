import { test, expect } from '../fixtures';
import { resetDatabase } from '../test-utils';

test.describe('Core Commis Flow', () => {
  test.beforeEach(async ({ request }) => {
    await resetDatabase(request);
  });

  test('spawns a commis and completes', async ({ request }) => {
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

    let courseId: number | null = null;
    await expect
      .poll(async () => {
        const coursesRes = await request.get('/api/jarvis/courses?limit=25');
        if (!coursesRes.ok()) return false;
        const courses = (await coursesRes.json()) as Array<{ id: number; created_at: string; trigger: string }>;

        const candidate = courses.find((course) => {
          const createdAt = Date.parse(course.created_at);
          return Number.isFinite(createdAt) && createdAt >= startTime - 2000 && course.trigger !== 'commis';
        });

        if (candidate) {
          courseId = candidate.id;
          return true;
        }
        return false;
      }, {
        timeout: 20000,
        intervals: [500, 1000, 2000],
      })
      .toBeTruthy();

    if (!courseId) {
      throw new Error('Failed to locate commis course');
    }

    // In async model: concierge spawns commis and completes immediately
    // Commis runs in background, completes later
    let events: Array<{ event_type: string }> = [];
    await expect
      .poll(async () => {
        const eventsRes = await request.get(`/api/jarvis/courses/${courseId}/events`);
        if (!eventsRes.ok()) return false;
        const payload = await eventsRes.json();
        events = payload.events ?? [];

        const spawnedCount = events.filter((e) => e.event_type === 'commis_spawned').length;
        const completeCount = events.filter((e) => e.event_type === 'commis_complete').length;

        // Async model: concierge doesn't wait, so no concierge_resumed
        return spawnedCount >= 1 && completeCount >= 1;
      }, {
        timeout: 60000,
        intervals: [1000, 2000, 5000],
      })
      .toBeTruthy();

    const spawnedCount = events.filter((e) => e.event_type === 'commis_spawned').length;
    const completeCount = events.filter((e) => e.event_type === 'commis_complete').length;

    expect(spawnedCount).toBeGreaterThanOrEqual(1);
    expect(completeCount).toBeGreaterThanOrEqual(1);

    let courseStatus: { status: string; result?: string } | null = null;
    await expect
      .poll(async () => {
        const statusRes = await request.get(`/api/jarvis/courses/${courseId}`);
        if (!statusRes.ok()) return false;
        courseStatus = await statusRes.json();
        return courseStatus.status === 'success' || courseStatus.status === 'failed';
      }, {
        timeout: 60000,
        intervals: [1000, 2000, 5000],
      })
      .toBeTruthy();

    expect(courseStatus?.status).toBe('success');
  });
});
