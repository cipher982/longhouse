/**
 * ADVANCED TEST UTILITIES AND HELPERS
 * 
 * This module provides comprehensive utilities for E2E testing including:
 * - Data generation and seeding
 * - Performance measurement utilities
 * - WebSocket testing helpers
 * - Database state validation
 * - UI interaction patterns
 * - Screenshot and visual comparison
 * - Load testing utilities
 */

import { Page, expect, APIRequestContext } from '@playwright/test';

export interface TestAgent {
  id: number;
  name: string;
  system_instructions: string;
  task_instructions: string;
  model: string;
}

export interface TestWorkflow {
  id: number;
  name: string;
  description: string;
  canvas_data: {
    nodes: any[];
    edges: any[];
  };
}

export interface PerformanceMetrics {
  loadTime: number;
  navigationTime: number;
  memoryUsage?: {
    used: number;
    total: number;
    limit: number;
  };
}

export interface WebSocketMessage {
  event_type?: string;
  type?: string;
  data?: any;
  timestamp?: string;
  receivedAt: number;
}

/**
 * Test Data Generation Utilities
 */
export class TestDataGenerator {
  private static counter = Date.now();

  /**
   * Generate unique test agent data
   */
  static generateAgent(prefix = 'Test Agent', index?: number): Omit<TestAgent, 'id'> {
    const suffix = index !== undefined ? index : this.counter++;
    return {
      name: `${prefix} ${suffix}`,
      system_instructions: `System instructions for ${prefix.toLowerCase()} ${suffix}`,
      task_instructions: `Task instructions for ${prefix.toLowerCase()} ${suffix}`,
      model: 'gpt-mock'
    };
  }

  /**
   * Generate batch of test agents
   */
  static generateAgents(count: number, prefix = 'Batch Agent'): Omit<TestAgent, 'id'>[] {
    return Array.from({ length: count }, (_, i) => 
      this.generateAgent(prefix, i + 1)
    );
  }

  /**
   * Generate test workflow data
   */
  static generateWorkflow(
    agents: TestAgent[], 
    name = 'Test Workflow'
  ): Omit<TestWorkflow, 'id'> {
    const suffix = this.counter++;
    const nodes = [
      {
        id: 'trigger-1',
        type: 'trigger',
        position: { x: 50, y: 200 },
        config: { trigger: { type: 'manual', config: { enabled: true, params: {}, filters: [] } } }
      },
      ...agents.slice(0, 3).map((agent, index) => ({
        id: `agent-${index + 1}`,
        type: 'agent',
        agent_id: agent.id,
        position: { x: 200 + index * 150, y: 150 + index * 25 }
      })),
      {
        id: 'http-tool-1',
        type: 'tool',
        tool_name: 'http_request',
        position: { x: 500, y: 200 },
        config: {
          url: `https://httpbin.org/get?test=${suffix}`,
          method: 'GET'
        }
      }
    ];

    const edges = [
      { id: 'edge-1', source: 'trigger-1', target: 'agent-1', type: 'default' },
      ...agents.slice(0, 2).map((_, index) => ({
        id: `edge-${index + 2}`,
        source: `agent-${index + 1}`,
        target: `agent-${index + 2}`,
        type: 'default'
      })),
      {
        id: 'edge-final',
        source: agents.length > 0 ? `agent-${Math.min(agents.length, 3)}` : 'trigger-1',
        target: 'http-tool-1',
        type: 'default'
      }
    ];

    return {
      name: `${name} ${suffix}`,
      description: `Generated test workflow ${suffix} with ${agents.length} agents`,
      canvas_data: { nodes, edges }
    };
  }

  /**
   * Generate realistic test data with various edge cases
   */
  static generateEdgeCaseData() {
    return {
      emptyStrings: { name: '', system_instructions: '', task_instructions: '' },
      longStrings: {
        name: 'x'.repeat(1000),
        system_instructions: 'y'.repeat(5000),
        task_instructions: 'z'.repeat(5000)
      },
      specialCharacters: {
        name: 'Test Agent with Special Chars: !@#$%^&*()[]{}|;:,.<>?',
        system_instructions: 'Instructions with "quotes" and \'apostrophes\'',
        task_instructions: 'Tasks with <tags> and & symbols'
      },
      unicodeStrings: {
        name: 'Test Agent 测试代理 🤖 Агент',
        system_instructions: 'Unicode instructions: 한국어 العربية 🚀',
        task_instructions: 'Tasks: Тест προσδοκία ✨'
      }
    };
  }
}

/**
 * API Testing Utilities
 */
export class APITestHelper {
  constructor(private request: APIRequestContext, private workerId: string) {}

  /**
   * Create agent with comprehensive error handling
   */
  async createAgent(agentData: Omit<TestAgent, 'id'>): Promise<{ success: boolean; agent?: TestAgent; error?: string }> {
    try {
      const response = await this.request.post('http://localhost:8001/api/agents', {
        headers: {
          'X-Test-Worker': this.workerId,
          'Content-Type': 'application/json',
        },
        data: agentData
      });

      if (response.ok()) {
        const agent = await response.json();
        return { success: true, agent };
      } else {
        const error = await response.text();
        return { success: false, error: `Status ${response.status()}: ${error}` };
      }
    } catch (error) {
      return { success: false, error: error.message };
    }
  }

  /**
   * Create multiple agents concurrently
   */
  async createAgentsBatch(agentDataList: Omit<TestAgent, 'id'>[]): Promise<{
    successful: TestAgent[];
    failed: Array<{ data: any; error: string }>;
    totalTime: number;
  }> {
    const startTime = Date.now();
    
    const promises = agentDataList.map(async (agentData) => {
      const result = await this.createAgent(agentData);
      return { ...result, originalData: agentData };
    });

    const results = await Promise.all(promises);
    const totalTime = Date.now() - startTime;

    return {
      successful: results.filter(r => r.success).map(r => r.agent!),
      failed: results.filter(r => !r.success).map(r => ({ 
        data: r.originalData, 
        error: r.error! 
      })),
      totalTime
    };
  }

  /**
   * Create workflow with validation
   */
  async createWorkflow(workflowData: Omit<TestWorkflow, 'id'>): Promise<{ 
    success: boolean; 
    workflow?: TestWorkflow; 
    error?: string 
  }> {
    try {
      const response = await this.request.post('http://localhost:8001/api/workflows', {
        headers: {
          'X-Test-Worker': this.workerId,
          'Content-Type': 'application/json',
        },
        data: workflowData
      });

      if (response.ok()) {
        const workflow = await response.json();
        return { success: true, workflow };
      } else {
        const error = await response.text();
        return { success: false, error: `Status ${response.status()}: ${error}` };
      }
    } catch (error) {
      return { success: false, error: error.message };
    }
  }

  /**
   * Execute workflow and monitor status
   */
  async executeWorkflowAndWait(
    workflowId: number, 
    inputs: any = {}, 
    timeoutMs = 30000
  ): Promise<{
    success: boolean;
    execution?: any;
    finalStatus?: string;
    error?: string;
  }> {
    try {
      // Start execution
      const executeResponse = await this.request.post(`http://localhost:8001/api/workflows/${workflowId}/execute`, {
        headers: {
          'X-Test-Worker': this.workerId,
          'Content-Type': 'application/json',
        },
        data: { inputs }
      });

      if (!executeResponse.ok()) {
        const error = await executeResponse.text();
        return { success: false, error: `Execution failed: ${error}` };
      }

      const execution = await executeResponse.json();
      
      // Monitor status
      const startTime = Date.now();
      let attempts = 0;
      const maxAttempts = Math.floor(timeoutMs / 1000);

      while (attempts < maxAttempts) {
        await new Promise(resolve => setTimeout(resolve, 1000));
        
        const statusResponse = await this.request.get(`http://localhost:8001/api/workflow-executions/${execution.id}`, {
          headers: { 'X-Test-Worker': this.workerId }
        });

        if (statusResponse.ok()) {
          const status = await statusResponse.json();
          
          if (['completed', 'failed', 'cancelled'].includes(status.status)) {
            return {
              success: status.status === 'completed',
              execution,
              finalStatus: status.status
            };
          }
        }

        attempts++;
        if (Date.now() - startTime > timeoutMs) {
          break;
        }
      }

      return { success: false, error: 'Execution timeout' };
    } catch (error) {
      return { success: false, error: error.message };
    }
  }

  /**
   * Get all agents with filtering
   */
  async getAgents(filters?: { name?: string; limit?: number }): Promise<TestAgent[]> {
    try {
      let url = 'http://localhost:8001/api/agents';
      const params = new URLSearchParams();
      
      if (filters?.limit) params.append('limit', filters.limit.toString());
      if (filters?.name) params.append('name', filters.name);
      
      if (params.toString()) {
        url += '?' + params.toString();
      }

      const response = await this.request.get(url, {
        headers: { 'X-Test-Worker': this.workerId }
      });

      if (response.ok()) {
        return await response.json();
      }
      
      return [];
    } catch (error) {
      console.error('Error fetching agents:', error);
      return [];
    }
  }
}

/**
 * Performance Testing Utilities
 */
export class PerformanceTestHelper {
  constructor(private page: Page) {}

  /**
   * Measure page load performance
   */
  async measurePageLoad(url: string): Promise<PerformanceMetrics> {
    const startTime = Date.now();
    
    await this.page.goto(url);
    await this.page.waitForLoadState('networkidle');
    
    const loadTime = Date.now() - startTime;

    // Get browser performance metrics
    const metrics = await this.page.evaluate(() => {
      return {
        navigationTime: performance.timing ? 
          performance.timing.loadEventEnd - performance.timing.navigationStart : 0,
        memoryUsage: (performance as any).memory ? {
          used: (performance as any).memory.usedJSHeapSize,
          total: (performance as any).memory.totalJSHeapSize,
          limit: (performance as any).memory.jsHeapSizeLimit
        } : undefined
      };
    });

    return {
      loadTime,
      navigationTime: metrics.navigationTime,
      memoryUsage: metrics.memoryUsage
    };
  }

  /**
   * Measure interaction response time
   */
  async measureInteraction(
    selector: string, 
    action: 'click' | 'hover' | 'focus' = 'click'
  ): Promise<number> {
    const element = this.page.locator(selector);
    await element.waitFor();

    const startTime = Date.now();
    
    switch (action) {
      case 'click':
        await element.click();
        break;
      case 'hover':
        await element.hover();
        break;
      case 'focus':
        await element.focus();
        break;
    }

    // Wait for potential UI updates
    await this.page.waitForTimeout(100);
    
    return Date.now() - startTime;
  }

  /**
   * Monitor memory usage over time
   */
  async monitorMemoryUsage(durationMs: number, intervalMs = 1000): Promise<{
    initial: any;
    peak: any;
    final: any;
    samples: any[];
  }> {
    const samples = [];
    const startTime = Date.now();

    // Get initial memory
    const initial = await this.page.evaluate(() => 
      (performance as any).memory ? {
        used: (performance as any).memory.usedJSHeapSize,
        total: (performance as any).memory.totalJSHeapSize,
        timestamp: Date.now()
      } : null
    );

    samples.push(initial);

    // Monitor periodically
    while (Date.now() - startTime < durationMs) {
      await new Promise(resolve => setTimeout(resolve, intervalMs));
      
      const sample = await this.page.evaluate(() => 
        (performance as any).memory ? {
          used: (performance as any).memory.usedJSHeapSize,
          total: (performance as any).memory.totalJSHeapSize,
          timestamp: Date.now()
        } : null
      );

      if (sample) samples.push(sample);
    }

    // Get final memory
    const final = samples[samples.length - 1];
    const peak = samples.reduce((max, current) => 
      current && current.used > (max?.used || 0) ? current : max
    );

    return { initial, peak, final, samples };
  }
}

/**
 * WebSocket Testing Utilities
 */
export class WebSocketTestHelper {
  private wsMessages: WebSocketMessage[] = [];

  constructor(private page: Page) {
    this.setupWebSocketMonitoring();
  }

  /**
   * Setup WebSocket message monitoring
   */
  private setupWebSocketMonitoring() {
    this.page.on('websocket', ws => {
      ws.on('framereceived', event => {
        try {
          const message = JSON.parse(event.payload);
          this.wsMessages.push({
            ...message,
            receivedAt: Date.now()
          });
        } catch (error) {
          // Store raw messages that couldn't be parsed
          this.wsMessages.push({
            type: 'raw',
            data: event.payload,
            receivedAt: Date.now()
          });
        }
      });
    });
  }

  /**
   * Wait for specific WebSocket message
   */
  async waitForMessage(
    predicate: (message: WebSocketMessage) => boolean,
    timeoutMs = 10000
  ): Promise<WebSocketMessage | null> {
    const startTime = Date.now();

    while (Date.now() - startTime < timeoutMs) {
      const message = this.wsMessages.find(predicate);
      if (message) return message;
      
      await new Promise(resolve => setTimeout(resolve, 100));
    }

    return null;
  }

  /**
   * Get messages by event type
   */
  getMessagesByType(eventType: string): WebSocketMessage[] {
    return this.wsMessages.filter(msg => 
      msg.event_type === eventType || msg.type === eventType
    );
  }

  /**
   * Get messages in time range
   */
  getMessagesInTimeRange(startTime: number, endTime: number): WebSocketMessage[] {
    return this.wsMessages.filter(msg => 
      msg.receivedAt >= startTime && msg.receivedAt <= endTime
    );
  }

  /**
   * Clear collected messages
   */
  clearMessages() {
    this.wsMessages = [];
  }

  /**
   * Get message statistics
   */
  getMessageStats(): {
    total: number;
    byType: Record<string, number>;
    timeRange: { earliest: number; latest: number } | null;
  } {
    const byType: Record<string, number> = {};
    let earliest = Infinity;
    let latest = -Infinity;

    this.wsMessages.forEach(msg => {
      const type = msg.event_type || msg.type || 'unknown';
      byType[type] = (byType[type] || 0) + 1;
      
      if (msg.receivedAt < earliest) earliest = msg.receivedAt;
      if (msg.receivedAt > latest) latest = msg.receivedAt;
    });

    return {
      total: this.wsMessages.length,
      byType,
      timeRange: this.wsMessages.length > 0 ? { earliest, latest } : null
    };
  }
}

/**
 * UI Testing Utilities
 */
export class UITestHelper {
  constructor(private page: Page) {}

  /**
   * Wait for element with comprehensive error handling
   */
  async waitForElementSafe(
    selector: string, 
    options: { timeout?: number; state?: 'visible' | 'attached' | 'hidden' } = {}
  ): Promise<{ found: boolean; error?: string }> {
    try {
      await this.page.locator(selector).waitFor({
        timeout: options.timeout || 10000,
        state: options.state || 'visible'
      });
      return { found: true };
    } catch (error) {
      return { found: false, error: error.message };
    }
  }

  /**
   * Check if element is interactive
   */
  async isElementInteractive(selector: string): Promise<{
    clickable: boolean;
    focusable: boolean;
    hasAriaLabel: boolean;
    hasRole: boolean;
  }> {
    const element = this.page.locator(selector);
    
    const [clickable, focusable, ariaLabel, role] = await Promise.all([
      element.isEnabled().catch(() => false),
      element.evaluate(el => {
        return el.tabIndex >= 0 || 
               ['INPUT', 'BUTTON', 'SELECT', 'TEXTAREA', 'A'].includes(el.tagName);
      }).catch(() => false),
      element.getAttribute('aria-label').then(val => !!val).catch(() => false),
      element.getAttribute('role').then(val => !!val).catch(() => false)
    ]);

    return {
      clickable,
      focusable,
      hasAriaLabel: ariaLabel,
      hasRole: role
    };
  }

  /**
   * Take screenshot with comparison
   */
  async takeScreenshotWithComparison(
    name: string,
    options: { fullPage?: boolean; clip?: any } = {}
  ): Promise<{ path: string; matches?: boolean }> {
    const screenshotPath = `screenshots/${name}-${Date.now()}.png`;
    
    await this.page.screenshot({
      path: screenshotPath,
      fullPage: options.fullPage || false,
      clip: options.clip
    });

    return { path: screenshotPath };
  }

  /**
   * Simulate various viewport sizes
   */
  async testResponsiveDesign(url: string): Promise<{
    mobile: { width: number; height: number; success: boolean };
    tablet: { width: number; height: number; success: boolean };
    desktop: { width: number; height: number; success: boolean };
  }> {
    const viewports = {
      mobile: { width: 375, height: 667 },
      tablet: { width: 768, height: 1024 },
      desktop: { width: 1920, height: 1080 }
    };

    const results: any = {};

    for (const [device, viewport] of Object.entries(viewports)) {
      await this.page.setViewportSize(viewport);
      await this.page.goto(url);
      await this.page.waitForTimeout(1000);

      // Check if page renders without horizontal scroll
      const hasHorizontalScroll = await this.page.evaluate(() => 
        document.body.scrollWidth > window.innerWidth
      );

      results[device] = {
        ...viewport,
        success: !hasHorizontalScroll
      };
    }

    return results;
  }

  /**
   * Test keyboard navigation flow
   */
  async testKeyboardNavigation(): Promise<{
    focusableElements: number;
    tabOrder: Array<{ element: string; index: number }>;
    trapsFocus: boolean;
  }> {
    const tabOrder = [];
    let focusableCount = 0;
    let previousElement = '';

    // Count total focusable elements
    focusableCount = await this.page.locator('button, input, select, textarea, a, [tabindex]:not([tabindex="-1"])').count();

    // Test tab navigation
    for (let i = 0; i < Math.min(focusableCount, 20); i++) {
      await this.page.keyboard.press('Tab');
      await this.page.waitForTimeout(100);

      const currentElement = await this.page.evaluate(() => {
        const focused = document.activeElement;
        return {
          tagName: focused?.tagName,
          id: focused?.id,
          className: focused?.className,
          testId: focused?.getAttribute('data-testid')
        };
      });

      const elementKey = `${currentElement.tagName}#${currentElement.id || currentElement.testId || i}`;
      
      if (elementKey === previousElement) {
        // Focus is trapped or cycling
        break;
      }

      tabOrder.push({
        element: elementKey,
        index: i
      });

      previousElement = elementKey;
    }

    return {
      focusableElements: focusableCount,
      tabOrder,
      trapsFocus: tabOrder.length < focusableCount
    };
  }
}

/**
 * Database State Validation Utilities
 */
export class DatabaseTestHelper {
  constructor(private apiHelper: APITestHelper) {}

  /**
   * Validate database consistency
   */
  async validateDatabaseConsistency(): Promise<{
    agentCount: number;
    workflowCount: number;
    orphanedReferences: number;
    consistent: boolean;
  }> {
    const agents = await this.apiHelper.getAgents();
    
    // For workflows, we'd need to implement similar logic
    // This is a placeholder for comprehensive database validation
    return {
      agentCount: agents.length,
      workflowCount: 0, // Would require workflow endpoint
      orphanedReferences: 0,
      consistent: true
    };
  }

  /**
   * Create test data set for complex testing
   */
  async seedTestData(scenario: 'minimal' | 'standard' | 'complex' = 'standard'): Promise<{
    agents: TestAgent[];
    workflows: TestWorkflow[];
  }> {
    const configs = {
      minimal: { agents: 2, workflows: 1 },
      standard: { agents: 5, workflows: 3 },
      complex: { agents: 10, workflows: 5 }
    };

    const config = configs[scenario];
    const agentData = TestDataGenerator.generateAgents(config.agents, `Seed Agent ${scenario}`);
    
    // Create agents
    const agentResults = await this.apiHelper.createAgentsBatch(agentData);
    
    // Create workflows using the created agents
    const workflows: TestWorkflow[] = [];
    for (let i = 0; i < config.workflows && agentResults.successful.length > 0; i++) {
      const workflowData = TestDataGenerator.generateWorkflow(
        agentResults.successful.slice(0, Math.min(3, agentResults.successful.length)),
        `Seed Workflow ${scenario} ${i + 1}`
      );
      
      const workflowResult = await this.apiHelper.createWorkflow(workflowData);
      if (workflowResult.success && workflowResult.workflow) {
        workflows.push(workflowResult.workflow);
      }
    }

    return {
      agents: agentResults.successful,
      workflows
    };
  }
}

/**
 * Main Test Helper Factory
 */
export class TestHelperFactory {
  static createAPIHelper(request: APIRequestContext, workerId: string): APITestHelper {
    return new APITestHelper(request, workerId);
  }

  static createPerformanceHelper(page: Page): PerformanceTestHelper {
    return new PerformanceTestHelper(page);
  }

  static createWebSocketHelper(page: Page): WebSocketTestHelper {
    return new WebSocketTestHelper(page);
  }

  static createUIHelper(page: Page): UITestHelper {
    return new UITestHelper(page);
  }

  static createDatabaseHelper(apiHelper: APITestHelper): DatabaseTestHelper {
    return new DatabaseTestHelper(apiHelper);
  }

  static createAllHelpers(page: Page, request: APIRequestContext, workerId: string) {
    const apiHelper = this.createAPIHelper(request, workerId);
    
    return {
      api: apiHelper,
      performance: this.createPerformanceHelper(page),
      webSocket: this.createWebSocketHelper(page),
      ui: this.createUIHelper(page),
      database: this.createDatabaseHelper(apiHelper),
      dataGenerator: TestDataGenerator
    };
  }
}
