/**
 * Perfect Chat E2E Test
 *
 * NOTE: This test has been consolidated into happy-paths.spec.ts
 * The "complete user flow: create agent -> open chat -> send message"
 * is covered by the SMOKE test sequence.
 *
 * For core chat flows, see:
 * - happy-paths.spec.ts: SMOKE 1-4 (agent, chat, message, input clear)
 */

import { test, expect } from './fixtures';

// Reset DB before each test
test.beforeEach(async ({ request }) => {
  await request.post('/admin/reset-database', { data: { reset_type: 'clear_data' } });
});

test.describe('Perfect Chat E2E Test', () => {
  // This test is fully covered by happy-paths.spec.ts SMOKE 1-4 tests
  // Keeping this file for reference but skipping to avoid redundancy
  test.skip('Complete user flow: create agent -> open chat -> send message', async ({ page }) => {
    // See happy-paths.spec.ts for the canonical implementation
    // SMOKE 1: Create agent
    // SMOKE 2: Navigate to chat
    // SMOKE 3: Send message
    // SMOKE 4: Input clears after send
    test.skip(true, 'Consolidated into happy-paths.spec.ts SMOKE tests');
  });
});
