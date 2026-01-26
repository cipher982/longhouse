import { testLog } from './test-logger';

import { Page, expect } from '@playwright/test';
import { ExecutionMonitor, startExecutionMonitoring, analyzeExecutionResults } from './canvas-helpers';

/**
 * Workflow execution and testing helper functions
 * These helpers provide reusable patterns for workflow execution testing,
 * result validation, and common test scenarios.
 */

export interface WorkflowTestConfig {
  ficheName: string;
  ficheInstructions: string;
  toolName: string;
  toolConfig: Record<string, string>;
  expectedHttpRequests?: string[];
  executionTimeoutMs?: number;
}

export interface WorkflowExecutionResult {
  success: boolean;
  monitor: ExecutionMonitor;
  analysis: ReturnType<typeof analyzeExecutionResults>;
  nodeCount: number;
}

/**
 * Create fiches via the dashboard UI (no modal - direct creation)
 */
export async function createFichesViaDashboard(
  page: Page,
  count: number = 1
): Promise<void> {
  await page.locator('.header-nav').click();
  await page.waitForTimeout(1000);

  const createFicheBtn = page.locator('button:has-text("Create Fiche")');

  // Click create fiche button multiple times
  for (let i = 0; i < count; i++) {
    await createFicheBtn.click();
    await page.waitForTimeout(500);
  }

  // Verify fiches were created
  const ficheRows = page.locator('table tbody tr');
  await expect(ficheRows.first()).toBeVisible({ timeout: 5000 });
  const ficheCount = await ficheRows.count();
  expect(ficheCount).toBeGreaterThanOrEqual(count);
}

/**
 * Execute a complete workflow test scenario
 */
export async function executeWorkflowTest(
  page: Page,
  config: WorkflowTestConfig
): Promise<WorkflowExecutionResult> {
  const {
    ficheName,
    ficheInstructions,
    toolName,
    toolConfig,
    expectedHttpRequests = [],
    executionTimeoutMs = 10000
  } = config;

  // 1. Create fiches
  await createFichesViaDashboard(page, 2); // Create a couple fiches

  // 2. Switch to canvas
  await page.getByTestId('global-canvas-tab').click();
  await page.waitForTimeout(2000);

  // 3. Verify canvas loaded
  await expect(page.locator('#canvas-container canvas')).toBeVisible({ timeout: 5000 });
  await expect(page.locator('#fiche-shelf .fiche-pill')).toHaveCount.toBeGreaterThanOrEqual(1, { timeout: 10000 });

  // 4. Create workflow (fiche + tool + connection)
  const { createFicheToolWorkflow } = await import('./canvas-helpers');
  await createFicheToolWorkflow(page, {
    toolName,
    toolConfig
  });

  // 5. Start monitoring
  const monitor = startExecutionMonitoring(page);

  // 6. Execute workflow
  const runButton = page.locator('#run-btn, button:has-text("Run"), .run-button');
  await expect(runButton).toBeVisible({ timeout: 5000 });
  await runButton.click();

  // 7. Wait for execution
  await page.waitForTimeout(executionTimeoutMs);

  // 8. Analyze results
  const analysis = analyzeExecutionResults(monitor);
  const nodeCount = await page.locator('.canvas-node, .generic-node').count();

  // 9. Validate expected HTTP requests
  let httpRequestsValid = true;
  if (expectedHttpRequests.length > 0) {
    httpRequestsValid = expectedHttpRequests.every(expectedReq =>
      monitor.networkRequests.some(req => req.includes(expectedReq))
    );
  }

  const success = analysis.hasExecutionStart &&
                 analysis.hasValidWorkflowId &&
                 httpRequestsValid;

  return {
    success,
    monitor,
    analysis,
    nodeCount
  };
}

/**
 * Common test scenarios for different types of workflows
 */
export const WORKFLOW_SCENARIOS = {
  HTTP_JSON_API: {
    ficheName: 'JSON API Processor',
    ficheInstructions: 'You process JSON API responses and extract key information.',
    toolName: 'HTTP Request',
    toolConfig: {
      url: 'https://jsonplaceholder.typicode.com/posts/1',
      method: 'GET'
    },
    expectedHttpRequests: ['jsonplaceholder.typicode.com']
  },

  HTTP_POST_DATA: {
    ficheName: 'Data Submission Fiche',
    ficheInstructions: 'You handle HTTP POST responses and validate submission results.',
    toolName: 'HTTP Request',
    toolConfig: {
      url: 'https://httpbin.org/post',
      method: 'POST',
      data: '{"test": "data"}'
    },
    expectedHttpRequests: ['httpbin.org']
  },

  WEB_SEARCH: {
    ficheName: 'Search Results Analyst',
    ficheInstructions: 'You analyze web search results and provide summaries.',
    toolName: 'Web Search',
    toolConfig: {
      query: 'artificial intelligence news'
    },
    expectedHttpRequests: []
  }
} as const;

/**
 * Run a predefined workflow scenario
 */
export async function runWorkflowScenario(
  page: Page,
  scenarioName: keyof typeof WORKFLOW_SCENARIOS,
  options: {
    executionTimeoutMs?: number;
    skipHttpValidation?: boolean;
  } = {}
): Promise<WorkflowExecutionResult> {
  const scenario = WORKFLOW_SCENARIOS[scenarioName];
  const config = {
    ...scenario,
    executionTimeoutMs: options.executionTimeoutMs || 10000
  };

  if (options.skipHttpValidation) {
    config.expectedHttpRequests = [];
  }

  return executeWorkflowTest(page, config);
}

/**
 * Validate workflow execution results with detailed logging
 */
export function validateWorkflowExecution(
  result: WorkflowExecutionResult,
  requirements: {
    minNodeCount?: number;
    requireHttpActivity?: boolean;
    requireExecutionStart?: boolean;
    requireValidWorkflowId?: boolean;
    requireSuccessfulResponse?: boolean;
  } = {}
): void {
  const {
    minNodeCount = 2,
    requireHttpActivity = false,
    requireExecutionStart = true,
    requireValidWorkflowId = true,
    requireSuccessfulResponse = false
  } = requirements;

  testLog.info(result.analysis.summary);
  testLog.info(`\n📊 DETAILED LOGS:`);
  testLog.info(`Connection Logs:`, result.monitor.connectionLogs);
  testLog.info(`Execution Logs:`, result.monitor.executionLogs);
  testLog.info(`HTTP Request Logs:`, result.monitor.httpRequestLogs);
  testLog.info(`Network Requests:`, result.monitor.networkRequests);

  // Core assertions
  expect(result.nodeCount).toBeGreaterThanOrEqual(minNodeCount);

  if (requireExecutionStart) {
    expect(result.analysis.hasExecutionStart).toBe(true);
  }

  if (requireValidWorkflowId) {
    expect(result.analysis.hasValidWorkflowId).toBe(true);
  }

  if (requireHttpActivity) {
    expect(result.analysis.hasHttpActivity).toBe(true);
  }

  if (requireSuccessfulResponse) {
    expect(result.analysis.hasSuccessfulHttpResponse).toBe(true);
  }

  testLog.info('✅ All workflow validation requirements met');
}

/**
 * Test multiple workflow scenarios in sequence
 */
export async function runMultipleWorkflowTests(
  page: Page,
  scenarios: Array<{
    name: string;
    config: WorkflowTestConfig;
    requirements?: Parameters<typeof validateWorkflowExecution>[1];
  }>
): Promise<Array<{ name: string; result: WorkflowExecutionResult; success: boolean }>> {
  const results = [];

  for (const scenario of scenarios) {
    testLog.info(`\n🚀 Starting workflow test: ${scenario.name}`);

    // Reset database for clean state
    const backendPort = process.env.BACKEND_PORT || '8001';
    await page.request.post(`http://localhost:${backendPort}/admin/reset-database`, { data: { reset_type: 'clear_data' } });
    await page.goto('/');
    await page.waitForTimeout(2000);

    try {
      const result = await executeWorkflowTest(page, scenario.config);

      if (scenario.requirements) {
        validateWorkflowExecution(result, scenario.requirements);
      }

      results.push({
        name: scenario.name,
        result,
        success: true
      });

      testLog.info(`✅ ${scenario.name} completed successfully`);
    } catch (error) {
      testLog.error(`❌ ${scenario.name} failed:`, error);
      results.push({
        name: scenario.name,
        result: {
          success: false,
          monitor: { connectionLogs: [], executionLogs: [], httpRequestLogs: [], networkRequests: [] },
          analysis: { hasExecutionStart: false, hasHttpActivity: false, hasValidWorkflowId: false, hasSuccessfulHttpResponse: false, summary: 'Test failed' },
          nodeCount: 0
        },
        success: false
      });
    }
  }

  return results;
}

/**
 * Create a stress test for workflow execution
 */
export async function stressTestWorkflowExecution(
  page: Page,
  config: {
    iterations: number;
    scenario: keyof typeof WORKFLOW_SCENARIOS;
    maxFailures: number;
  }
): Promise<{ successes: number; failures: number; results: WorkflowExecutionResult[] }> {
  const { iterations, scenario, maxFailures } = config;
  const results: WorkflowExecutionResult[] = [];
  let failures = 0;

  for (let i = 0; i < iterations; i++) {
    testLog.info(`\n🔄 Stress test iteration ${i + 1}/${iterations}`);

    try {
      const result = await runWorkflowScenario(page, scenario, {
        executionTimeoutMs: 5000 // Shorter timeout for stress testing
      });

      results.push(result);

      if (!result.success) {
        failures++;
        testLog.warn(`⚠️ Iteration ${i + 1} failed`);

        if (failures >= maxFailures) {
          testLog.error(`❌ Max failures (${maxFailures}) reached, stopping stress test`);
          break;
        }
      }
    } catch (error) {
      failures++;
      testLog.error(`❌ Iteration ${i + 1} error:`, error);

      if (failures >= maxFailures) {
        break;
      }
    }

    // Brief pause between iterations
    await page.waitForTimeout(1000);
  }

  const successes = results.filter(r => r.success).length;

  testLog.info(`\n📊 STRESS TEST RESULTS:`);
  testLog.info(`  Total Iterations: ${results.length}`);
  testLog.info(`  Successes: ${successes}`);
  testLog.info(`  Failures: ${failures}`);
  testLog.info(`  Success Rate: ${((successes / results.length) * 100).toFixed(1)}%`);

  return { successes, failures, results };
}
