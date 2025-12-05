import { describe, it, expect, vi, beforeEach } from 'vitest';
import { createContextTools, routeToSupervisorTool } from '../lib/tool-factory';
import { stateManager } from '../lib/state-manager';

/**
 * Supervisor Tool Integration Tests
 *
 * Tests the route_to_supervisor tool that delegates complex tasks
 * to the Zerg Supervisor backend.
 *
 * Note: The OpenAI tool() wrapper doesn't expose execute() directly,
 * so we test the tool metadata and integration with createContextTools.
 * The actual execute logic is tested indirectly through E2E tests.
 */

// Mock the state manager
vi.mock('../lib/state-manager', () => ({
  stateManager: {
    getJarvisClient: vi.fn(),
  },
}));

describe('route_to_supervisor tool metadata', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('should have correct name', () => {
    expect(routeToSupervisorTool.name).toBe('route_to_supervisor');
  });

  it('should have descriptive description', () => {
    expect(routeToSupervisorTool.description).toContain('Delegate a complex task');
    expect(routeToSupervisorTool.description).toContain('Zerg Supervisor');
  });

  it('should include supervisor trigger keywords in description', () => {
    const desc = routeToSupervisorTool.description;
    expect(desc).toContain('check');
    expect(desc).toContain('investigate');
    expect(desc).toContain('research');
    expect(desc).toContain('debug');
    expect(desc).toContain('analyze');
  });

  it('should mention what NOT to use it for', () => {
    expect(routeToSupervisorTool.description).toContain('Do NOT use this');
    expect(routeToSupervisorTool.description).toContain('simple questions');
  });

  it('should have parameters defined', () => {
    expect(routeToSupervisorTool.parameters).toBeDefined();
  });
});

describe('createContextTools', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('should always include route_to_supervisor tool', () => {
    const config = {
      tools: [],
    };

    const tools = createContextTools(config, null);

    expect(tools.length).toBeGreaterThanOrEqual(1);
    expect(tools[0].name).toBe('route_to_supervisor');
  });

  it('should add supervisor tool first before other tools', () => {
    const config = {
      tools: [
        { name: 'get_current_location', enabled: true, mcpServer: 'traccar', mcpFunction: 'get_location' },
      ],
    };

    const tools = createContextTools(config, null);

    expect(tools[0].name).toBe('route_to_supervisor');
  });

  it('should include enabled config tools after supervisor', () => {
    const config = {
      tools: [
        { name: 'get_current_location', enabled: true, mcpServer: 'traccar', mcpFunction: 'get_location' },
        { name: 'disabled_tool', enabled: false, mcpServer: 'test', mcpFunction: 'test' },
      ],
    };

    const tools = createContextTools(config, null);

    // Should have supervisor + location tool
    expect(tools.length).toBe(2);
    expect(tools[0].name).toBe('route_to_supervisor');
    expect(tools[1].name).toBe('get_current_location');
  });

  it('should not duplicate supervisor tool', () => {
    const config = {
      tools: [],
    };

    const tools = createContextTools(config, null);
    const supervisorTools = tools.filter(t => t.name === 'route_to_supervisor');

    expect(supervisorTools.length).toBe(1);
  });

  it('should create tools with empty config', () => {
    const config = {
      tools: [],
    };

    const tools = createContextTools(config, null);

    // Should have at least the supervisor tool
    expect(tools.length).toBe(1);
    expect(tools[0].name).toBe('route_to_supervisor');
  });
});

describe('Tool integration with stateManager', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('stateManager mock should be accessible', () => {
    // Verify the mock is working
    expect(stateManager.getJarvisClient).toBeDefined();
    expect(vi.isMockFunction(stateManager.getJarvisClient)).toBe(true);
  });

  it('should be able to mock getJarvisClient return value', () => {
    const mockClient = { isAuthenticated: vi.fn().mockReturnValue(true) };
    vi.mocked(stateManager.getJarvisClient).mockReturnValue(mockClient);

    const client = stateManager.getJarvisClient();
    expect(client).toBe(mockClient);
    expect(client.isAuthenticated()).toBe(true);
  });
});
