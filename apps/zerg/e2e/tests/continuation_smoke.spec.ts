import { test, expect } from './fixtures';

/**
 * Continuation Flow Smoke Test
 *
 * Verifies the full loop:
 * 1. Concierge receives request
 * 2. Concierge spawns commis (via MockLLM trigger)
 * 3. Commis executes (via MockLLM)
 * 4. Concierge continues and synthesizes result
 *
 * This hits the REAL backend and DB, so it validates schema constraints
 * like the 'continuation' trigger enum length.
 *
 * NOTE: The barrier pattern resumes the ORIGINAL course in place (no new continuation course).
 */
test.describe('Continuation Flow Smoke Test', () => {
  test('full concierge -> commis -> continuation cycle', async ({ request }) => {
    test.setTimeout(60000);  // 60s for full commis cycle

    console.log('[Smoke] Starting continuation flow test');

    // 1. Trigger the flow - fire and forget (SSE stream stays open during commis execution)
    // We DON'T await this - Playwright's request.post() waits for full response body
    // which would block forever on SSE streams
    const chatPromise = request.post('/api/jarvis/chat', {
      data: {
        message: 'TRIGGER_COMMIS', // Triggers spawn_commis in MockLLM
        message_id: crypto.randomUUID(),
        model: 'gpt-mock',
        client_correlation_id: 'smoke-continuation'
      },
    });

    // Suppress unhandled rejection when test completes and request is aborted
    chatPromise.catch(() => {});

    // Give it a moment to hit the server and create the course
    await new Promise(r => setTimeout(r, 1000));

    // 2. Poll for ANY active course and wait for it to complete
    // The course was created, we just need to find it
    console.log(`[Smoke] Polling for active courses...`);

    let courseId: number | null = null;
    let finalResult: string = '';

    await expect.poll(async () => {
        // First, find the course we care about
        if (!courseId) {
            const coursesRes = await request.get('/api/jarvis/courses?limit=5');
            const courses = await coursesRes.json() as any[];
            // Find the most recent course that's not a commis
            const targetCourse = courses.find((c: any) =>
                c.trigger !== 'commis' &&
                (c.status === 'running' || c.status === 'waiting' || c.status === 'success')
            );
            if (targetCourse) {
                courseId = targetCourse.id;
                console.log(`[Smoke] Found course ${courseId} with status ${targetCourse.status}`);
            }
        }

        if (courseId) {
            const res = await request.get(`/api/jarvis/courses/${courseId}`);
            const json = await res.json();
            console.log(`[Smoke] Course ${courseId} status: ${json.status}`);

            if (json.status === 'success') {
                // Get the final result from events
                const eventsRes = await request.get(`/api/jarvis/courses/${courseId}/events`);
                const events = await eventsRes.json();
                const complete = events.events?.find((e: any) => e.event_type === 'concierge_complete');
                if (complete?.payload?.result) {
                    finalResult = complete.payload.result;
                    return true;
                }
                // Even without concierge_complete event, success status is enough
                // Check for final_result in course data
                if (json.final_result) {
                    finalResult = json.final_result;
                    return true;
                }
                // Try thread messages
                const threadRes = await request.get(`/api/threads/${json.thread_id}/messages`);
                const threadData = await threadRes.json();
                const lastAssistant = threadData.messages?.reverse().find((m: any) => m.role === 'assistant');
                if (lastAssistant?.content) {
                    finalResult = lastAssistant.content;
                    return true;
                }
            }
        }
        return false;
    }, {
        message: 'Waiting for course completion with commis result',
        timeout: 55000,
        intervals: [1000, 2000, 3000]
    }).toBeTruthy();

    console.log(`[Smoke] Final Result: ${finalResult}`);

    // 3. Verify Model Inheritance
    // If the fix works, the continuation used gpt-mock and returned the canned response.
    // If the fix failed, it used default (gpt-5.2) and returned a real AI response.
    expect(finalResult).toContain("Task completed successfully via commis");

    console.log('[Smoke] Test PASSED - Full flow completed with correct model');
  });
});
