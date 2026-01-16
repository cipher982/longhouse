import { test, expect } from './fixtures';

/**
 * Continuation Flow Smoke Test
 *
 * Verifies the full loop:
 * 1. Supervisor receives request
 * 2. Supervisor spawns worker (via MockLLM trigger)
 * 3. Worker executes (via MockLLM)
 * 4. Supervisor continues and synthesizes result
 *
 * This hits the REAL backend and DB, so it validates schema constraints
 * like the 'continuation' trigger enum length.
 *
 * NOTE: The barrier pattern resumes the ORIGINAL run in place (no new continuation run).
 */
test.describe('Continuation Flow Smoke Test', () => {
  test('full supervisor -> worker -> continuation cycle', async ({ request }) => {
    test.setTimeout(60000);  // 60s for full worker cycle

    console.log('[Smoke] Starting continuation flow test');

    // 1. Trigger the flow - fire and forget (SSE stream stays open during worker execution)
    // We DON'T await this - Playwright's request.post() waits for full response body
    // which would block forever on SSE streams
    const chatPromise = request.post('/api/jarvis/chat', {
      data: {
        message: 'TRIGGER_WORKER', // Triggers spawn_worker in MockLLM
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
            const runsRes = await request.get('/api/jarvis/runs?limit=5');
            const runs = await runsRes.json() as any[];
            // Find the most recent run that's not a worker
            const targetRun = runs.find((r: any) =>
                r.trigger !== 'worker' &&
                (r.status === 'running' || r.status === 'waiting' || r.status === 'success')
            );
            if (targetRun) {
                runId = targetRun.id;
                console.log(`[Smoke] Found run ${runId} with status ${targetRun.status}`);
            }
        }

        if (runId) {
            const res = await request.get(`/api/jarvis/runs/${runId}`);
            const json = await res.json();
            console.log(`[Smoke] Run ${runId} status: ${json.status}`);

            if (json.status === 'success') {
                // Get the final result from events
                const eventsRes = await request.get(`/api/jarvis/runs/${runId}/events`);
                const events = await eventsRes.json();
                const complete = events.events?.find((e: any) => e.event_type === 'supervisor_complete');
                if (complete?.payload?.result) {
                    finalResult = complete.payload.result;
                    return true;
                }
                // Even without supervisor_complete event, success status is enough
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
        message: 'Waiting for run completion with worker result',
        timeout: 55000,
        intervals: [1000, 2000, 3000]
    }).toBeTruthy();

    console.log(`[Smoke] Final Result: ${finalResult}`);

    // 3. Verify Model Inheritance
    // If the fix works, the continuation used gpt-mock and returned the canned response.
    // If the fix failed, it used default (gpt-5.2) and returned a real AI response.
    expect(finalResult).toContain("Task completed successfully via worker");

    console.log('[Smoke] Test PASSED - Full flow completed with correct model');
  });
});
