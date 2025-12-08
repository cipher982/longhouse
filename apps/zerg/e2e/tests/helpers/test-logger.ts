/**
 * Simple test logger that only outputs when DEBUG_E2E=1
 * Reduces noise in test output while allowing debugging when needed
 */

const DEBUG = process.env.DEBUG_E2E === '1';

export const testLog = {
  info: (...args: any[]) => {
    if (DEBUG) console.log('[E2E]', ...args);
  },
  warn: (...args: any[]) => {
    if (DEBUG) console.warn('[E2E WARN]', ...args);
  },
  error: (...args: any[]) => {
    // Always show errors
    console.error('[E2E ERROR]', ...args);
  },
};
