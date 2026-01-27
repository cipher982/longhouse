import { test, expect } from './fixtures';

/**
 * Continuation Flow Smoke Test
 *
 * Verifies the full loop:
 * 1. Oikos receives request
 * 2. Oikos spawns commis (via MockLLM trigger)
 * 3. Commis executes (via MockLLM)
 * 4. Oikos continues and synthesizes result
 *
 * This hits the REAL backend and DB, so it validates schema constraints
 * like the 'continuation' trigger enum length.
 *
 * NOTE: The barrier pattern resumes the ORIGINAL run in place (no new continuation run).
 */
test.describe('Continuation Flow Smoke Test', () => {
  test('full oikos -> commis -> continuation cycle', async ({ request }) => {
    test.setTimeout(60000);  // 60s for full commis cycle

    console.log('[Smoke] Starting continuation flow test');

    // 1. Trigger the flow - fire and forget (SSE stream stays open during commis execution)
    // We DON'T await this - Playwright's request.post() waits for full response body
    // which would block forever on SSE streams
    const chatPromise = request.post('/api/oikos/chat', {
      data: {
        message: 'TRIGGER_COMMIS', // Triggers spawn_commis in MockLLM
        message_id: crypto.randomUUID(),
        model: 'gpt-mock',
        client_correlation_id: 'smoke-continuation'
      },
    });

    // Suppress unhandled rejection when test completes and request is aborted
    chatPromise.catch(() => {});

    // Give it a moment to hit the server and create the run
    await new Promise(r => setTimeout(r, 1000));

    // 2. Poll for ANY active run and wait for it to complete
    // The run was created, we just need to find it
    console.log(`[Smoke] Polling for active runs...`);

    let runId: number | null = null;
    let finalResult: string = '';

    await expect.poll(async () => {
        // First, find the run we care about
        if (!runId) {
            const runsRes = await request.get('/api/oikos/runs?limit=5');
            const runs = await runsRes.json() as any[];
            // Find the most recent run that's not a commis
            const targetRun = runs.find((c: any) =>
                c.trigger !== 'commis' &&
                (c.status === 'running' || c.status === 'waiting' || c.status === 'success')
            );
            if (targetRun) {
                runId = targetRun.id;
                console.log(`[Smoke] Found run ${runId} with status ${targetRun.status}`);
            }
        }

        if (runId) {
            const res = await request.get(`/api/oikos/runs/${runId}`);
            const json = await res.json();
            console.log(`[Smoke] Run ${runId} status: ${json.status}`);

            if (json.status === 'success') {
                // Get the final result from events
                const eventsRes = await request.get(`/api/oikos/runs/${runId}/events`);
                const events = await eventsRes.json();
                const complete = events.events?.find((e: any) => e.event_type === 'oikos_complete');
                if (complete?.payload?.result) {
                    finalResult = complete.payload.result;
                    return true;
                }
                // Even without oikos_complete event, success status is enough
                // Check for final_result in run data
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
        message: 'Waiting for run completion with commis result',
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
