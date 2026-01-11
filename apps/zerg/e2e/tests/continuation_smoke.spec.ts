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
 */
test.describe('Continuation Flow Smoke Test', () => {
  test('full supervisor -> worker -> continuation cycle', async ({ request }) => {
    test.setTimeout(30000);

    console.log('[Smoke] Starting continuation flow test');

    // 1. Trigger the flow
    const response = await request.post('/api/jarvis/chat', {
      data: {
        message: 'TRIGGER_WORKER', // Triggers spawn_worker in MockLLM
        model: 'gpt-mock',
        client_correlation_id: 'smoke-continuation'
      }
    });
    expect(response.status()).toBe(200);

    // Get run_id from SSE stream
    const sseText = await response.text();
    const match = sseText.match(/"run_id":\s*(\d+)/);
    const runId = match ? parseInt(match[1]) : null;
    expect(runId).toBeTruthy();
    console.log(`[Smoke] Started Run ID: ${runId}`);

    // 2. Poll for continuation run and its completion
    console.log(`[Smoke] Polling for continuation of run ${runId}...`);

    let continuationRunId: number | null = null;
    let finalResult: string = '';

    await expect.poll(async () => {
        // Find continuation run
        if (!continuationRunId) {
            const runsRes = await request.get('/api/jarvis/runs?limit=10');
            const runs = await runsRes.json();
            // API returns List[JarvisRunSummary] directly
            const continuation = Array.isArray(runs) ? runs.find((r: any) => r.continuation_of_run_id === runId) : (runs as any).data?.find((r: any) => r.continuation_of_run_id === runId);

            if (continuation) {
                continuationRunId = continuation.id;
                console.log(`[Smoke] Found continuation run: ${continuationRunId}`);
            }
        }

        if (continuationRunId) {
            const res = await request.get(`/api/jarvis/runs/${continuationRunId}`);
            const json = await res.json();
            console.log(`[Smoke] Continuation status: ${json.status}`);

            if (json.status === 'success') {
                // Get the final result from events
                const eventsRes = await request.get(`/api/jarvis/runs/${continuationRunId}/events`);
                const events = await eventsRes.json();
                const complete = events.events.find((e: any) => e.event_type === 'supervisor_complete');
                if (complete) {
                    finalResult = complete.payload.result;
                    return true;
                }
            }
        }
        return false;
    }, {
        message: 'Waiting for continuation run completion',
        timeout: 30000,
        intervals: [1000, 2000]
    }).toBeTruthy();

    console.log(`[Smoke] Final Result: ${finalResult}`);

    // 3. Verify Model Inheritance
    // If the fix works, the continuation used gpt-mock and returned the canned response.
    // If the fix failed, it used default (gpt-5.2) and returned a real AI response.
    expect(finalResult).toContain("Task completed successfully via worker");

    console.log('[Smoke] Test PASSED - Full flow completed with correct model');
  });
});
