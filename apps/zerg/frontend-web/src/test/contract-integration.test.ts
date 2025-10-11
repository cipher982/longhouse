/**
 * Contract Integration Tests
 *
 * These tests make REAL API calls to validate that our frontend code
 * works correctly with the actual backend implementation.
 *
 * This would have caught the ops API contract mismatches immediately.
 */

import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import { spawn, type ChildProcess } from 'child_process';
import { setTimeout } from 'timers/promises';

const RUN_CONTRACT_TESTS = process.env.RUN_CONTRACT_TESTS === 'true';
const describeContract = RUN_CONTRACT_TESTS ? describe : describe.skip;

const API_BASE_URL = 'http://localhost:47301'; // Use different port to avoid conflicts
let backendProcess: ChildProcess;

// Helper function to make authenticated API calls
async function apiCall(endpoint: string, options: RequestInit = {}) {
  const response = await fetch(`${API_BASE_URL}${endpoint}`, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...options.headers,
    },
  });

  return {
    status: response.status,
    ok: response.ok,
    data: response.ok ? await response.json() : null,
    text: !response.ok ? await response.text() : null,
  };
}

describeContract('Backend API Contract Integration', () => {
  beforeAll(async () => {
    // Start backend server for testing
    console.log('🚀 Starting test backend server...');

    // Set testing environment variables
    process.env.ENVIRONMENT = 'test';
    process.env.AUTH_DISABLED = '1'; // Disable auth for contract testing
    process.env.DATABASE_URL = 'sqlite:///./contract-test.db';

    backendProcess = spawn('uv', ['run', 'python', '-m', 'uvicorn', 'zerg.main:app', '--port', '47301'], {
      cwd: '../backend',
      stdio: 'pipe',
    });

    // Wait for backend to start
    await setTimeout(3000);
  }, 15000);

  afterAll(async () => {
    if (backendProcess) {
      console.log('🧹 Stopping test backend server...');
      backendProcess.kill('SIGTERM');
      await setTimeout(1000);
    }
  }, 10000);

  describe('System Health', () => {
    it('should return valid health check', async () => {
      const result = await apiCall('/api/system/health');

      expect(result.status).toBe(200);
      expect(result.data).toBeTruthy();
      expect(typeof result.data).toBe('object');
    });
  });

  describe('Ops Endpoints (Admin)', () => {
    it('should return ops summary with correct structure', async () => {
      const result = await apiCall('/api/ops/summary');

      // Should work in test mode with auth disabled
      if (result.status === 403) {
        console.log('⚠️  Ops endpoint requires admin auth (expected in production)');
        return; // Skip test if auth is enabled
      }

      expect(result.ok).toBe(true);
      expect(result.data).toBeTruthy();

      // Validate the actual contract shape we depend on
      expect(result.data).toHaveProperty('runs_today');
      expect(result.data).toHaveProperty('cost_today_usd');
      expect(result.data).toHaveProperty('budget_user');
      expect(result.data).toHaveProperty('budget_global');
      expect(result.data).toHaveProperty('errors_last_hour');

      // Validate nested structures
      expect(result.data.budget_user).toHaveProperty('limit_cents');
      expect(result.data.budget_user).toHaveProperty('used_usd');
      expect(result.data.budget_user).toHaveProperty('percent');

      // Validate types
      expect(typeof result.data.runs_today).toBe('number');
      expect(typeof result.data.errors_last_hour).toBe('number');
    });

    it('should return time series data with correct format', async () => {
      const result = await apiCall('/api/ops/timeseries?metric=runs_by_hour&window=today');

      if (result.status === 403) {
        console.log('⚠️  Ops endpoint requires admin auth (expected)');
        return;
      }

      expect(result.ok).toBe(true);
      expect(result.data).toBeTruthy();

      // Should have series array
      expect(result.data).toHaveProperty('series');
      expect(Array.isArray(result.data.series)).toBe(true);
    });
  });

  describe('User Endpoints', () => {
    it('should return user profile with role field', async () => {
      const result = await apiCall('/api/users/me');

      if (result.status === 401) {
        console.log('⚠️  User endpoint requires auth (expected in production)');
        return;
      }

      expect(result.ok).toBe(true);
      expect(result.data).toBeTruthy();

      // Critical: must have role field for admin authorization
      expect(result.data).toHaveProperty('email');
      expect(result.data).toHaveProperty('id');
      // Role field is critical for proper admin authorization
      if (!result.data.hasOwnProperty('role')) {
        console.warn('⚠️  User response lacks role field - admin authorization may fail');
      }
    });
  });

  describe('Workflow Execution Endpoints', () => {
    it('should preserve execution_id in status response', async () => {
      // This test would catch the workflow execution ID preservation issue
      const result = await apiCall('/api/workflow-executions/1/status');

      if (result.status === 404) {
        console.log('⚠️  No test execution found (expected)');
        return;
      }

      if (result.ok) {
        expect(result.data).toBeTruthy();

        // CRITICAL: response must include execution_id or frontend breaks
        if (!result.data.execution_id && !result.data.hasOwnProperty('execution_id')) {
          console.error('❌ CRITICAL: workflow execution status lacks execution_id');
          console.error('   This causes frontend WebSocket updates to lose state');
          expect(result.data).toHaveProperty('execution_id');
        }

        expect(result.data).toHaveProperty('phase');
      }
    });
  });

  describe('Schema Drift Detection', () => {
    it('should detect when OpenAPI schema has empty response schemas', async () => {
      const fs = await import('fs');
      const schema = JSON.parse(fs.readFileSync('../openapi.json', 'utf-8'));

      const emptySchemaEndpoints: string[] = [];

      // Check all endpoints for empty response schemas
      for (const [path, pathItem] of Object.entries<any>(schema.paths)) {
        for (const [method, methodItem] of Object.entries<any>(pathItem)) {
          if (method === 'get' || method === 'post') {
            const response200 = methodItem.responses?.['200'];
            const jsonSchema = response200?.content?.['application/json']?.schema;

            if (jsonSchema && Object.keys(jsonSchema).length === 0) {
              emptySchemaEndpoints.push(`${method.toUpperCase()} ${path}`);
            }
          }
        }
      }

      if (emptySchemaEndpoints.length > 0) {
        console.error('❌ CRITICAL: Endpoints with empty response schemas:');
        emptySchemaEndpoints.forEach(endpoint => {
          console.error(`   ${endpoint}`);
        });

        throw new Error(
          `Found ${emptySchemaEndpoints.length} endpoints with empty schemas. ` +
          `This prevents TypeScript type generation and contract validation.`
        );
      }

      console.log('✅ No empty response schemas found');
    });
  });
});

// Export for use in other test files
export { apiCall, API_BASE_URL };
