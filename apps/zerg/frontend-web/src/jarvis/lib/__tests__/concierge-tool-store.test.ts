import { describe, expect, it, beforeEach, vi, afterEach } from 'vitest';
import { eventBus } from '../event-bus';
import { conciergeToolStore } from '../concierge-tool-store';
import type { ConciergeToolCall } from '../concierge-tool-store';

describe('ConciergeToolStore', () => {
  beforeEach(() => {
    // Clear the store before each test
    conciergeToolStore.clearTools();
    vi.clearAllTimers();
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  describe('tool lifecycle tracking', () => {
    it('tracks tool started event', () => {
      const toolData = {
        courseId: 1,
        toolName: 'get_current_location',
        toolCallId: 'call-123',
        argsPreview: '{}',
        args: { device_id: '1' },
        timestamp: Date.now(),
      };

      eventBus.emit('concierge:tool_started', toolData);

      const state = conciergeToolStore.getState();
      expect(state.tools.size).toBe(1);
      expect(state.isActive).toBe(true);

      const tool = state.tools.get('call-123');
      expect(tool).toBeDefined();
      expect(tool?.toolName).toBe('get_current_location');
      expect(tool?.status).toBe('running');
      expect(tool?.argsPreview).toBe('{}');
      expect(tool?.args).toEqual({ device_id: '1' });
    });

    it('updates tool on completion', () => {
      // Start a tool
      eventBus.emit('concierge:tool_started', {
        courseId: 1,
        toolName: 'web_search',
        toolCallId: 'call-456',
        argsPreview: 'query: "weather"',
        timestamp: Date.now(),
      });

      // Complete the tool
      eventBus.emit('concierge:tool_completed', {
        courseId: 1,
        toolName: 'web_search',
        toolCallId: 'call-456',
        durationMs: 1500,
        resultPreview: 'Found 5 results',
        result: { results: ['result1', 'result2'] },
        timestamp: Date.now(),
      });

      const state = conciergeToolStore.getState();
      const tool = state.tools.get('call-456');

      expect(tool?.status).toBe('completed');
      expect(tool?.durationMs).toBe(1500);
      expect(tool?.resultPreview).toBe('Found 5 results');
      expect(tool?.result).toEqual({ results: ['result1', 'result2'] });
      expect(tool?.completedAt).toBeDefined();
    });

    it('handles tool failure', () => {
      // Start a tool
      eventBus.emit('concierge:tool_started', {
        courseId: 1,
        toolName: 'get_whoop_data',
        toolCallId: 'call-789',
        argsPreview: 'date: "2024-01-01"',
        timestamp: Date.now(),
      });

      // Fail the tool
      eventBus.emit('concierge:tool_failed', {
        courseId: 1,
        toolName: 'get_whoop_data',
        toolCallId: 'call-789',
        durationMs: 500,
        error: 'API timeout',
        errorDetails: { code: 'TIMEOUT', retry: false },
        timestamp: Date.now(),
      });

      const state = conciergeToolStore.getState();
      const tool = state.tools.get('call-789');

      expect(tool?.status).toBe('failed');
      expect(tool?.durationMs).toBe(500);
      expect(tool?.error).toBe('API timeout');
      expect(tool?.errorDetails).toEqual({ code: 'TIMEOUT', retry: false });
    });

    it('tracks tool progress logs', () => {
      // Start a tool
      eventBus.emit('concierge:tool_started', {
        courseId: 1,
        toolName: 'search_notes',
        toolCallId: 'call-111',
        argsPreview: 'query: "test"',
        timestamp: Date.now(),
      });

      // Add progress logs
      eventBus.emit('concierge:tool_progress', {
        courseId: 1,
        toolCallId: 'call-111',
        message: 'Searching vault...',
        level: 'info',
        timestamp: Date.now(),
      });

      eventBus.emit('concierge:tool_progress', {
        courseId: 1,
        toolCallId: 'call-111',
        message: 'Found 3 matches',
        level: 'info',
        data: { count: 3 },
        timestamp: Date.now(),
      });

      const state = conciergeToolStore.getState();
      const tool = state.tools.get('call-111');

      expect(tool?.logs).toHaveLength(2);
      expect(tool?.logs[0].message).toBe('Searching vault...');
      expect(tool?.logs[0].level).toBe('info');
      expect(tool?.logs[1].message).toBe('Found 3 matches');
      expect(tool?.logs[1].data).toEqual({ count: 3 });
    });
  });

  describe('filtering and querying', () => {
    it('filters tools by courseId', () => {
      // Add tools for different courses
      eventBus.emit('concierge:tool_started', {
        courseId: 1,
        toolName: 'tool_a',
        toolCallId: 'call-1',
        timestamp: Date.now(),
      });

      eventBus.emit('concierge:tool_started', {
        courseId: 2,
        toolName: 'tool_b',
        toolCallId: 'call-2',
        timestamp: Date.now(),
      });

      eventBus.emit('concierge:tool_started', {
        courseId: 1,
        toolName: 'tool_c',
        toolCallId: 'call-3',
        timestamp: Date.now(),
      });

      const course1Tools = conciergeToolStore.getToolsForCourse(1);
      const course2Tools = conciergeToolStore.getToolsForCourse(2);

      expect(course1Tools).toHaveLength(2);
      expect(course2Tools).toHaveLength(1);
      expect(course1Tools[0].toolName).toBe('tool_a');
      expect(course1Tools[1].toolName).toBe('tool_c');
      expect(course2Tools[0].toolName).toBe('tool_b');
    });

    it('returns tools sorted by start time', () => {
      const now = Date.now();

      // Add tools with different timestamps (out of order)
      eventBus.emit('concierge:tool_started', {
        courseId: 1,
        toolName: 'tool_b',
        toolCallId: 'call-2',
        timestamp: now + 1000,
      });

      eventBus.emit('concierge:tool_started', {
        courseId: 1,
        toolName: 'tool_a',
        toolCallId: 'call-1',
        timestamp: now,
      });

      eventBus.emit('concierge:tool_started', {
        courseId: 1,
        toolName: 'tool_c',
        toolCallId: 'call-3',
        timestamp: now + 2000,
      });

      const tools = conciergeToolStore.getToolsForCourse(1);

      expect(tools).toHaveLength(3);
      expect(tools[0].toolName).toBe('tool_a');
      expect(tools[1].toolName).toBe('tool_b');
      expect(tools[2].toolName).toBe('tool_c');
    });
  });

  describe('state management', () => {
    it('clears tools on clearTools()', () => {
      // Add some tools
      eventBus.emit('concierge:tool_started', {
        courseId: 1,
        toolName: 'tool_a',
        toolCallId: 'call-1',
        timestamp: Date.now(),
      });

      expect(conciergeToolStore.getState().tools.size).toBe(1);

      // Clear
      conciergeToolStore.clearTools();

      const state = conciergeToolStore.getState();
      expect(state.tools.size).toBe(0);
      expect(state.isActive).toBe(false);
      expect(state.currentCourseId).toBeNull();
    });

    it('loads tools from persisted data', () => {
      const persistedTools: ConciergeToolCall[] = [
        {
          toolCallId: 'call-1',
          toolName: 'tool_a',
          status: 'completed',
          courseId: 1,
          startedAt: Date.now() - 5000,
          completedAt: Date.now() - 3000,
          durationMs: 2000,
          resultPreview: 'Success',
          logs: [],
        },
        {
          toolCallId: 'call-2',
          toolName: 'tool_b',
          status: 'failed',
          courseId: 1,
          startedAt: Date.now() - 2000,
          completedAt: Date.now() - 1000,
          durationMs: 1000,
          error: 'Failed',
          logs: [],
        },
      ];

      conciergeToolStore.loadTools(persistedTools);

      const state = conciergeToolStore.getState();
      expect(state.tools.size).toBe(2);

      const tool1 = state.tools.get('call-1');
      expect(tool1?.toolName).toBe('tool_a');
      expect(tool1?.status).toBe('completed');

      const tool2 = state.tools.get('call-2');
      expect(tool2?.toolName).toBe('tool_b');
      expect(tool2?.status).toBe('failed');
    });

    it('activates on concierge:started event', () => {
      eventBus.emit('concierge:started', {
        courseId: 1,
        task: 'Test task',
        timestamp: Date.now(),
      });

      const state = conciergeToolStore.getState();
      expect(state.isActive).toBe(true);
      expect(state.currentCourseId).toBe(1);
    });

    it('deactivates on concierge:complete event after delay', () => {
      eventBus.emit('concierge:started', {
        courseId: 1,
        task: 'Test task',
        timestamp: Date.now(),
      });

      expect(conciergeToolStore.getState().isActive).toBe(true);

      eventBus.emit('concierge:complete', {
        courseId: 1,
        result: 'Done',
        status: 'success',
        timestamp: Date.now(),
      });

      // Should still be active immediately
      expect(conciergeToolStore.getState().isActive).toBe(true);

      // After delay, should deactivate
      vi.advanceTimersByTime(500);
      expect(conciergeToolStore.getState().isActive).toBe(false);
    });

    it('deactivates immediately on concierge:error', () => {
      eventBus.emit('concierge:started', {
        courseId: 1,
        task: 'Test task',
        timestamp: Date.now(),
      });

      expect(conciergeToolStore.getState().isActive).toBe(true);

      eventBus.emit('concierge:error', {
        message: 'Error occurred',
        timestamp: Date.now(),
      });

      // Should deactivate immediately
      expect(conciergeToolStore.getState().isActive).toBe(false);
    });

    it('clears active state on concierge:cleared', () => {
      eventBus.emit('concierge:started', {
        courseId: 1,
        task: 'Test task',
        timestamp: Date.now(),
      });

      eventBus.emit('concierge:tool_started', {
        courseId: 1,
        toolName: 'tool_a',
        toolCallId: 'call-1',
        timestamp: Date.now(),
      });

      expect(conciergeToolStore.getState().isActive).toBe(true);
      expect(conciergeToolStore.getState().tools.size).toBe(1);

      eventBus.emit('concierge:cleared', {
        timestamp: Date.now(),
      });

      const state = conciergeToolStore.getState();
      expect(state.isActive).toBe(false);
      expect(state.currentCourseId).toBeNull();
      // Tools persist for conversation history
      expect(state.tools.size).toBe(1);
    });
  });

  describe('live duration ticker', () => {
    it('starts ticker for running tools', () => {
      eventBus.emit('concierge:tool_started', {
        courseId: 1,
        toolName: 'tool_a',
        toolCallId: 'call-1',
        timestamp: Date.now(),
      });

      // Ticker should start and notify listeners
      const listener = vi.fn();
      conciergeToolStore.subscribe(listener);

      // Advance time to trigger ticker
      vi.advanceTimersByTime(500);

      // Should have notified listener
      expect(listener).toHaveBeenCalled();
    });

    it('stops ticker when no running tools remain', () => {
      eventBus.emit('concierge:tool_started', {
        courseId: 1,
        toolName: 'tool_a',
        toolCallId: 'call-1',
        timestamp: Date.now(),
      });

      const listener = vi.fn();
      conciergeToolStore.subscribe(listener);

      // Complete the tool
      eventBus.emit('concierge:tool_completed', {
        courseId: 1,
        toolName: 'tool_a',
        toolCallId: 'call-1',
        durationMs: 1000,
        timestamp: Date.now(),
      });

      listener.mockClear();

      // Advance time - ticker should be stopped, no more notifications
      vi.advanceTimersByTime(500);

      // Should not notify since there are no running tools
      expect(listener).not.toHaveBeenCalled();
    });

    it('continues ticker while at least one tool is running', () => {
      // Start two tools
      eventBus.emit('concierge:tool_started', {
        courseId: 1,
        toolName: 'tool_a',
        toolCallId: 'call-1',
        timestamp: Date.now(),
      });

      eventBus.emit('concierge:tool_started', {
        courseId: 1,
        toolName: 'tool_b',
        toolCallId: 'call-2',
        timestamp: Date.now(),
      });

      const listener = vi.fn();
      conciergeToolStore.subscribe(listener);

      // Complete one tool
      eventBus.emit('concierge:tool_completed', {
        courseId: 1,
        toolName: 'tool_a',
        toolCallId: 'call-1',
        durationMs: 1000,
        timestamp: Date.now(),
      });

      listener.mockClear();

      // Ticker should still run because one tool is still running
      vi.advanceTimersByTime(500);
      expect(listener).toHaveBeenCalled();
    });

    it('stops ticker on clearTools()', () => {
      eventBus.emit('concierge:tool_started', {
        courseId: 1,
        toolName: 'tool_a',
        toolCallId: 'call-1',
        timestamp: Date.now(),
      });

      const listener = vi.fn();
      conciergeToolStore.subscribe(listener);

      conciergeToolStore.clearTools();
      listener.mockClear();

      // Advance time - ticker should be stopped
      vi.advanceTimersByTime(500);
      expect(listener).not.toHaveBeenCalled();
    });
  });

  describe('subscription management', () => {
    it('notifies listeners on state change', () => {
      const listener = vi.fn();
      conciergeToolStore.subscribe(listener);

      eventBus.emit('concierge:tool_started', {
        courseId: 1,
        toolName: 'tool_a',
        toolCallId: 'call-1',
        timestamp: Date.now(),
      });

      expect(listener).toHaveBeenCalled();
    });

    it('allows unsubscribing', () => {
      const listener = vi.fn();
      const unsubscribe = conciergeToolStore.subscribe(listener);

      eventBus.emit('concierge:tool_started', {
        courseId: 1,
        toolName: 'tool_a',
        toolCallId: 'call-1',
        timestamp: Date.now(),
      });

      expect(listener).toHaveBeenCalledTimes(1);

      // Unsubscribe
      unsubscribe();
      listener.mockClear();

      eventBus.emit('concierge:tool_started', {
        courseId: 1,
        toolName: 'tool_b',
        toolCallId: 'call-2',
        timestamp: Date.now(),
      });

      expect(listener).not.toHaveBeenCalled();
    });
  });
});
