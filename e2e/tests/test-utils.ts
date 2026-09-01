/**
 * Shared test utilities for E2E tests
 *
 * These helpers are designed to be:
 * - DETERMINISTIC: Wait for specific conditions, not timeouts
 * - ISOLATED: Each operation is self-contained
 * - ROBUST: Handle race conditions properly
 */

import { type APIRequestContext } from '@playwright/test';

/**
 * Reset database to clean state (call in beforeEach).
 * STRICT: Throws on failure to fail fast and avoid dirty state propagation.
 * Includes aggressive retry logic to handle lock contention under high concurrency.
 * Adds initial stagger delay to prevent all workers from hitting reset simultaneously.
 */
export async function resetDatabase(request: APIRequestContext): Promise<void> {
  const maxRetries = 5;
  const baseDelay = 200;
  const maxJitter = 300; // Wider spread to reduce concurrent retries

  // Add initial stagger delay (0-500ms) to spread out reset calls across workers
  // This prevents all beforeEach hooks from hitting the backend simultaneously
  await new Promise(r => setTimeout(r, Math.random() * 500));

  for (let attempt = 1; attempt <= maxRetries; attempt++) {
    try {
      const response = await request.post('/api/admin/reset-database', {
        data: { reset_type: 'clear_data' },
        timeout: 15000, // Explicit 15s timeout (backend has 30s statement_timeout)
      });

      if (response.ok()) {
        return;
      }

      // On 500 errors, retry with exponential backoff + wide jitter
      if (response.status() === 500 && attempt < maxRetries) {
        const delay = baseDelay * Math.pow(2, attempt - 1) + Math.random() * maxJitter;
        await new Promise(r => setTimeout(r, delay));
        continue;
      }

      throw new Error(`Database reset failed: ${response.status()} after ${attempt} attempts - tests cannot continue with dirty state`);
    } catch (error) {
      // Handle network errors (timeouts, connection refused) with retry
      if (attempt < maxRetries) {
        const delay = baseDelay * Math.pow(2, attempt - 1) + Math.random() * maxJitter;
        await new Promise(r => setTimeout(r, delay));
        continue;
      }
      throw new Error(`Database reset failed after ${attempt} attempts: ${error}`);
    }
  }
}
