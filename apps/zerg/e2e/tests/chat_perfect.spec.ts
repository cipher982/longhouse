/**
 * Perfect Chat E2E Test
 *
 * NOTE: This test has been consolidated into happy-paths.spec.ts
 * The "complete user flow: create fiche -> open chat -> send message"
 * is covered by the SMOKE test sequence.
 *
 * For core chat flows, see:
 * - happy-paths.spec.ts: SMOKE 1-4 (fiche, chat, message, input clear)
 */

import { test, expect } from './fixtures';
import { resetDatabase } from './test-utils';

// Reset DB before each test
// Uses strict reset that throws on failure to fail fast
test.beforeEach(async ({ request }) => {
  await resetDatabase(request);
});

test.describe('Perfect Chat E2E Test', () => {
  // This test is fully covered by happy-paths.spec.ts SMOKE 1-4 tests
  // Keeping this file for reference but skipping to avoid redundancy
  test.skip('Complete user flow: create fiche -> open chat -> send message', async ({ page }) => {
    // See happy-paths.spec.ts for the canonical implementation
    // SMOKE 1: Create fiche
    // SMOKE 2: Navigate to chat
    // SMOKE 3: Send message
    // SMOKE 4: Input clears after send
    test.skip(true, 'Consolidated into happy-paths.spec.ts SMOKE tests');
  });
});
