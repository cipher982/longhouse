import { Page, expect, Locator } from '@playwright/test';

/**
 * Canvas-specific helper functions for E2E tests
 * These helpers provide reusable patterns for canvas interactions,
 * drag-and-drop operations, and workflow execution testing.
 */

export interface CanvasNode {
  element: Locator;
  boundingBox: any;
  type: 'fiche' | 'tool' | 'trigger';
}

export interface ExecutionMonitor {
  connectionLogs: string[];
  executionLogs: string[];
  httpRequestLogs: string[];
  networkRequests: string[];
}

/**
 * Navigate to canvas and wait for it to be ready
 */
export async function navigateToCanvas(page: Page): Promise<void> {
  await page.getByTestId('global-canvas-tab').click();

  // Wait for data-ready signal instead of arbitrary timeout
  await page.waitForFunction(
    () => document.body.getAttribute('data-ready') === 'true',
    {},
    { timeout: 10000 }
  );

  // Verify canvas loaded
  await expect(page.locator('#canvas-container')).toBeVisible({ timeout: 10000 });
  await expect(page.locator('#canvas-container canvas')).toBeVisible({ timeout: 5000 });
}

/**
 * Wait for fiche shelf to load with specified minimum count
 */
export async function waitForFicheShelf(page: Page, minCount: number = 1): Promise<void> {
  const fichePills = page.locator('#fiche-shelf .fiche-pill');
  await expect(fichePills.first()).toBeVisible({ timeout: 10000 });
  const pillCount = await fichePills.count();
  expect(pillCount).toBeGreaterThanOrEqual(minCount);
}

/**
 * Drag an fiche from shelf to canvas at specified position
 */
export async function dragFicheToCanvas(
  page: Page,
  ficheIndex: number = 0,
  position: { x: number; y: number } = { x: 200, y: 150 }
): Promise<void> {
  const fichePill = page.locator('#fiche-shelf .fiche-pill').nth(ficheIndex);
  const canvasArea = page.locator('#canvas-container canvas');

  const nodeCountBefore = await page.locator('.canvas-node, .generic-node').count();

  await fichePill.dragTo(canvasArea, { targetPosition: position });

  // Wait for a new node to appear instead of arbitrary timeout
  await expect(page.locator('.canvas-node, .generic-node')).toHaveCount(nodeCountBefore + 1, { timeout: 5000 });
}

/**
 * Drag a tool from palette to canvas at specified position
 */
export async function dragToolToCanvas(
  page: Page,
  toolName: string,
  position: { x: number; y: number } = { x: 400, y: 150 }
): Promise<void> {
  // Support multiple selector patterns for different tool naming conventions
  const toolSelectors = [
    `[data-testid="palette-tool-${toolName}"]`,
    `.palette-node:has-text("${toolName}")`,
    `[data-tool-name="${toolName}"]`,
    `.tool-palette-item:has-text("${toolName}")`
  ];

  let toolElement = null;
  for (const selector of toolSelectors) {
    const element = page.locator(selector);
    if (await element.count() > 0) {
      toolElement = element;
      break;
    }
  }

  if (!toolElement) {
    throw new Error(`Tool "${toolName}" not found in palette. Available selectors tried: ${toolSelectors.join(', ')}`);
  }

  await expect(toolElement).toBeVisible({ timeout: 10000 });

  const nodeCountBefore = await page.locator('.canvas-node, .generic-node').count();

  const canvasArea = page.locator('#canvas-container canvas');
  await toolElement.dragTo(canvasArea, { targetPosition: position });

  // Wait for a new node to appear instead of arbitrary timeout
  await expect(page.locator('.canvas-node, .generic-node')).toHaveCount(nodeCountBefore + 1, { timeout: 5000 });
}

/**
 * Get all canvas nodes with their bounding boxes
 */
export async function getCanvasNodes(page: Page): Promise<CanvasNode[]> {
  const nodeElements = await page.locator('.canvas-node, .generic-node').all();
  const nodes: CanvasNode[] = [];

  for (const element of nodeElements) {
    const boundingBox = await element.boundingBox();
    // Determine node type based on class names or content
    const nodeType = await determineNodeType(element);

    nodes.push({
      element,
      boundingBox,
      type: nodeType
    });
  }

  return nodes;
}

/**
 * Determine node type based on element attributes
 */
async function determineNodeType(element: Locator): Promise<'fiche' | 'tool' | 'trigger'> {
  const classList = await element.getAttribute('class') || '';
  const content = await element.textContent() || '';

  if (classList.includes('fiche') || content.includes('Fiche')) {
    return 'fiche';
  } else if (classList.includes('tool') || content.includes('HTTP') || content.includes('Request')) {
    return 'tool';
  } else {
    return 'trigger';
  }
}

/**
 * Connect two nodes by dragging from output handle to input handle
 */
export async function connectNodes(
  page: Page,
  fromNode: CanvasNode,
  toNode: CanvasNode
): Promise<void> {
  if (!fromNode.boundingBox || !toNode.boundingBox) {
    throw new Error('Node bounding boxes not available for connection');
  }

  // Calculate handle positions
  const outputHandleX = fromNode.boundingBox.x + fromNode.boundingBox.width / 2;
  const outputHandleY = fromNode.boundingBox.y + fromNode.boundingBox.height - 10;

  const inputHandleX = toNode.boundingBox.x + toNode.boundingBox.width / 2;
  const inputHandleY = toNode.boundingBox.y + 10;

  const edgeCountBefore = await page.locator('.react-flow__edge, .canvas-edge').count();

  // Perform drag connection
  await page.mouse.move(outputHandleX, outputHandleY);
  await page.mouse.down();
  await page.mouse.move(inputHandleX, inputHandleY);
  await page.mouse.up();

  // Wait for a new edge to appear instead of arbitrary timeout
  await expect(page.locator('.react-flow__edge, .canvas-edge')).toHaveCount(edgeCountBefore + 1, { timeout: 5000 });
}

/**
 * Configure a tool node by double-clicking and filling configuration
 */
export async function configureTool(
  page: Page,
  toolNode: CanvasNode,
  config: Record<string, string>
): Promise<void> {
  // Double-click to open config
  await toolNode.element.dblclick();

  // Look for configuration modal to appear
  const configModal = page.locator('#tool-config-modal, .modal:has-text("Config"), .config-modal');
  await expect(configModal).toBeVisible({ timeout: 5000 });

  if (await configModal.count() > 0) {
    // Fill configuration fields
    for (const [fieldName, value] of Object.entries(config)) {
      const fieldSelectors = [
        `#${fieldName}-input`,
        `input[name="${fieldName}"]`,
        `input[placeholder*="${fieldName}"]`,
        `textarea[name="${fieldName}"]`
      ];

      for (const selector of fieldSelectors) {
        const field = page.locator(selector);
        if (await field.count() > 0) {
          await field.fill(value);
          break;
        }
      }
    }

    // Save configuration
    const saveBtn = page.locator('button:has-text("Save"), #save-config, .save-btn');
    if (await saveBtn.count() > 0) {
      await saveBtn.click();
      // Wait for modal to close after save
      await expect(configModal).not.toBeVisible({ timeout: 5000 });
    }
  }
}

/**
 * Start monitoring execution events and network requests
 */
export function startExecutionMonitoring(page: Page): ExecutionMonitor {
  const monitor: ExecutionMonitor = {
    connectionLogs: [],
    executionLogs: [],
    httpRequestLogs: [],
    networkRequests: []
  };

  // Monitor console logs for different types of events
  page.on('console', msg => {
    const text = msg.text();
    if (msg.type() === 'log') {
      // Connection logs
      if (text.includes('connection') || text.includes('Connected') || text.includes('edge') || text.includes('handle')) {
        monitor.connectionLogs.push(text);
      }

      // Execution logs
      if (text.includes('execution') || text.includes('workflow') || text.includes('running')) {
        monitor.executionLogs.push(text);
      }

      // HTTP request logs
      if (text.includes('http') || text.includes('request') || text.includes('response') || text.includes('200')) {
        monitor.httpRequestLogs.push(text);
      }
    }
  });

  // Monitor network requests
  page.on('request', request => {
    const url = request.url();
    if (url.includes('api/') || url.includes('jsonplaceholder') || url.includes('httpbin')) {
      monitor.networkRequests.push(`${request.method()} ${url}`);
    }
  });

  page.on('response', response => {
    const url = response.url();
    if (url.includes('api/') || url.includes('jsonplaceholder') || url.includes('httpbin')) {
      monitor.networkRequests.push(`Response ${response.status()} ${url}`);
    }
  });

  return monitor;
}

/**
 * Execute workflow by clicking run button
 */
export async function executeWorkflow(page: Page): Promise<void> {
  const runButton = page.locator('#run-btn, button:has-text("Run"), .run-button');
  await expect(runButton).toBeVisible({ timeout: 5000 });

  // Wait for API response after clicking run
  await Promise.all([
    page.waitForResponse(
      (r) => r.url().includes('/api/workflow') && r.request().method() === 'POST',
      { timeout: 10000 }
    ),
    runButton.click(),
  ]);
}

/**
 * Wait for workflow execution to complete
 * Monitors for execution_finished WebSocket event or status change
 */
export async function waitForExecutionComplete(
  page: Page,
  timeoutMs: number = 10000
): Promise<void> {
  // Wait for execution status indicator to show completion (success/failed/cancelled)
  // or for logs panel to show "EXECUTION FINISHED" or "EXECUTION COMPLETED"
  await expect(
    page.locator('[data-execution-status="completed"], [data-execution-status="success"], [data-execution-status="failed"], .execution-log:has-text("FINISHED"), .execution-log:has-text("COMPLETED")')
  ).toBeVisible({ timeout: timeoutMs });
}

/**
 * Analyze execution results and provide summary
 */
export function analyzeExecutionResults(monitor: ExecutionMonitor): {
  hasExecutionStart: boolean;
  hasHttpActivity: boolean;
  hasValidWorkflowId: boolean;
  hasSuccessfulHttpResponse: boolean;
  summary: string;
} {
  const hasExecutionStart = monitor.executionLogs.some(log =>
    log.includes('execution') || log.includes('running') || log.includes('workflow')
  );

  const hasHttpActivity = monitor.httpRequestLogs.length > 0 || monitor.networkRequests.length > 0;

  const hasValidWorkflowId = !monitor.executionLogs.some(log =>
    log.includes('404') || log.includes('invalid')
  );

  const hasSuccessfulHttpResponse = monitor.networkRequests.some(request =>
    request.includes('Response 200') || request.includes('Response 201')
  );

  const summary = `
🎯 EXECUTION RESULTS SUMMARY:
  Execution Started: ${hasExecutionStart ? '✅' : '❌'}
  HTTP Activity Detected: ${hasHttpActivity ? '✅' : '❌'}
  Valid Workflow ID: ${hasValidWorkflowId ? '✅' : '❌'}
  Successful HTTP Response: ${hasSuccessfulHttpResponse ? '✅' : '❌'}

📋 Log Counts:
  Connection Logs: ${monitor.connectionLogs.length}
  Execution Logs: ${monitor.executionLogs.length}
  HTTP Request Logs: ${monitor.httpRequestLogs.length}
  Network Requests: ${monitor.networkRequests.length}
  `;

  return {
    hasExecutionStart,
    hasHttpActivity,
    hasValidWorkflowId,
    hasSuccessfulHttpResponse,
    summary
  };
}

/**
 * Complete workflow creation pattern: fiche + tool + connection
 */
export async function createFicheToolWorkflow(
  page: Page,
  options: {
    ficheIndex?: number;
    toolName?: string;
    fichePosition?: { x: number; y: number };
    toolPosition?: { x: number; y: number };
    toolConfig?: Record<string, string>;
  } = {}
): Promise<{ ficheNode: CanvasNode; toolNode: CanvasNode }> {
  const {
    ficheIndex = 0,
    toolName = 'HTTP Request',
    fichePosition = { x: 200, y: 150 },
    toolPosition = { x: 400, y: 150 },
    toolConfig = {}
  } = options;

  // Drag fiche to canvas
  await dragFicheToCanvas(page, ficheIndex, fichePosition);

  // Drag tool to canvas
  await dragToolToCanvas(page, toolName, toolPosition);

  // Get nodes
  const nodes = await getCanvasNodes(page);
  if (nodes.length < 2) {
    throw new Error(`Expected at least 2 nodes, got ${nodes.length}`);
  }

  const ficheNode = nodes.find(n => n.type === 'fiche');
  const toolNode = nodes.find(n => n.type === 'tool');

  if (!ficheNode || !toolNode) {
    throw new Error('Could not identify fiche and tool nodes');
  }

  // Connect nodes
  await connectNodes(page, ficheNode, toolNode);

  // Configure tool if config provided
  if (Object.keys(toolConfig).length > 0) {
    await configureTool(page, toolNode, toolConfig);
  }

  return { ficheNode, toolNode };
}

/**
 * Verify canvas state and node count
 */
export async function verifyCanvasState(
  page: Page,
  expectedNodeCount: number
): Promise<void> {
  const nodes = page.locator('.canvas-node, .generic-node');
  await expect(nodes).toHaveCount(expectedNodeCount, { timeout: 5000 });
}
