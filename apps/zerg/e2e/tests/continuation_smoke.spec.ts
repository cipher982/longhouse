import { test, expect } from './fixtures';
import { postSseAndCollect } from './helpers/sse';

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
  test('full oikos -> commis -> continuation cycle', async ({ backendUrl }) => {
    test.setTimeout(60000);  // 60s for full commis cycle

    console.log('[Smoke] Starting continuation flow test');

    const commisId = `continuation-${Date.now()}`;
    const events = await postSseAndCollect({
      backendUrl,
      commisId,
      path: '/api/oikos/chat',
      payload: {
        message: 'TRIGGER_COMMIS', // Triggers spawn_commis in MockLLM
        message_id: crypto.randomUUID(),
        model: 'gpt-mock',
        client_correlation_id: 'smoke-continuation',
      },
      stopEvent: 'oikos_complete',
      timeoutMs: 60000,
    });

    const complete = events.find((evt) => evt.event === 'oikos_complete');
    const completeData = complete?.data as { payload?: { result?: string } } | undefined;
    const result = completeData?.payload?.result ?? '';

    console.log(`[Smoke] Final Result: ${result}`);

    // 3. Verify Model Inheritance
    // If the fix works, the continuation used gpt-mock and returned the canned response.
    // If the fix failed, it used default (gpt-5.2) and returned a real AI response.
    expect(result).toContain("Task completed successfully via commis");
    console.log('[Smoke] Test PASSED - Full flow completed with correct model');
  });
});
