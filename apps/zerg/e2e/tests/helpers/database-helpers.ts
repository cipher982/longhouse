import { testLog } from './test-logger';

import { Page } from '@playwright/test';
import { createApiClient } from './api-client';
import * as fs from 'fs';
import * as path from 'path';

// Load dynamic backend port from .env
function getBackendPort(): number {
  // Check environment variable first
  if (process.env.BACKEND_PORT) {
    return parseInt(process.env.BACKEND_PORT);
  }

  // Load from .env file
  const envPath = path.resolve(__dirname, '../../../.env');
  if (fs.existsSync(envPath)) {
    const envContent = fs.readFileSync(envPath, 'utf8');
    const lines = envContent.split('\n');
    for (const line of lines) {
      const [key, value] = line.split('=');
      if (key === 'BACKEND_PORT') {
        return parseInt(value) || 8001;
      }
    }
  }

  return 8001; // Default fallback
}

/**
 * Database management helpers for E2E tests
 * Provides consistent patterns for database reset and cleanup
 */

export interface DatabaseResetOptions {
  retries?: number;
  timeout?: number;
  skipVerification?: boolean;
}

/**
 * Reset database for a specific worker with retry and verification
 */
export async function resetDatabaseForWorker(
  workerId: string,
  options: DatabaseResetOptions = {}
): Promise<void> {
  const { retries = 3, timeout = 5000, skipVerification = false } = options;
  const apiClient = createApiClient(workerId);

  let attempts = 0;

  while (attempts < retries) {
    try {
      // Reset the database
      await apiClient.resetDatabase();

      if (!skipVerification) {
        // Verify reset was successful
        const agents = await apiClient.listAgents();
        if (agents.length === 0) {
          return; // Success
        }
        testLog.warn(`Database reset attempt ${attempts + 1}: Found ${agents.length} remaining agents`);
      } else {
        return; // Skip verification, assume success
      }
    } catch (error) {
      testLog.warn(`Database reset attempt ${attempts + 1} failed:`, error);
    }

    attempts++;
    if (attempts < retries) {
      await new Promise(resolve => setTimeout(resolve, 1000));
    }
  }

  throw new Error(`Failed to reset database after ${retries} attempts`);
}

/**
 * Reset database using page.request (for use in beforeEach hooks)
 */
export async function resetDatabaseViaRequest(
  page: Page,
  options: DatabaseResetOptions & { workerId?: string } = {}
): Promise<void> {
  const { retries = 3, workerId } = options;
  let attempts = 0;

  // Use single backend port; isolation via X-Test-Worker header
  const basePort = getBackendPort();
  const baseUrl = `http://localhost:${basePort}`;

  while (attempts < retries) {
    try {
      const response = await page.request.post(`${baseUrl}/api/admin/reset-database`, {
        headers: {
          'Content-Type': 'application/json',
          ...(workerId !== undefined ? { 'X-Test-Worker': workerId } : {}),
        },
        data: {
          reset_type: 'clear_data',
        },
      });
      if (response.ok()) {
        return;
      }
      if (response.status() === 422) {
        const body = await response.json().catch(() => ({}));
        const details = Array.isArray(body?.detail) ? body.detail : [body?.detail];
        if (details.some((item) => typeof item?.msg === 'string' && item.msg.toLowerCase().includes('already clean'))) {
          return;
        }
      }
      testLog.warn(`Database reset attempt ${attempts + 1}: HTTP ${response.status()}`);
    } catch (error) {
      testLog.warn(`Database reset attempt ${attempts + 1} failed:`, error);
    }

    attempts++;
    if (attempts < retries) {
      await new Promise(resolve => setTimeout(resolve, 1000));
    }
  }

  throw new Error(`Failed to reset database via request after ${retries} attempts`);
}

/**
 * Ensure database is clean before starting a test
 * Automatically handles worker ID from test context
 */
export async function ensureCleanDatabase(page: Page, workerId: string): Promise<void> {
  try {
    await resetDatabaseForWorker(workerId, { retries: 2, skipVerification: false });
    testLog.info(`✅ Database reset successful for worker ${workerId}`);
  } catch (error) {
    testLog.warn(`⚠️  Database reset failed for worker ${workerId}:`, error);
    // Don't throw - let test proceed in case it's a transient issue
  }
}

/**
 * Create a beforeEach hook that resets the database
 * Usage: test.beforeEach(createDatabaseResetHook());
 */
export function createDatabaseResetHook(options: DatabaseResetOptions = {}) {
  return async ({ page }: { page: Page }) => {
    await resetDatabaseViaRequest(page, options);
  };
}

/**
 * Verify database is actually empty (useful for debugging isolation issues)
 */
export async function verifyDatabaseEmpty(workerId: string): Promise<boolean> {
  try {
    const apiClient = createApiClient(workerId);
    const agents = await apiClient.listAgents();
    return agents.length === 0;
  } catch (error) {
    testLog.warn(`Failed to verify database state for worker ${workerId}:`, error);
    return false;
  }
}

/**
 * Get database statistics for debugging
 */
export async function getDatabaseStats(workerId: string): Promise<{
  agentCount: number;
  workerId: string;
  timestamp: string;
}> {
  const apiClient = createApiClient(workerId);
  const agents = await apiClient.listAgents();

  return {
    agentCount: agents.length,
    workerId,
    timestamp: new Date().toISOString()
  };
}
